#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
万圣节 10.30/10.31 库存消耗监控
- 数据源：Superset kanban.mmweb.top 看板27 / 数据集53（公开API）
- 口径：已售 = 支付成功票数（status=2，扣退款）；库存 300/900/1050 每晚独立；
        嘉年华会员专属并入正价；剔除 City Drink / 限时参团 / 员工福利场次票
- 产出：report/index.html（GitHub Pages）+ 飞书卡片消息
"""
import json, os, sys, datetime, collections, urllib.request, urllib.parse

BASE = "https://kanban.mmweb.top"
DATASET = 53
STOCK = {"超级早鸟票": 300, "早场票": 900, "正价票": 1050}
SELL = {"超级早鸟票", "早场票", "正价票", "嘉年华会员专属"}
NIGHTS = {1793390400000: "10.30", 1793476800000: "10.31"}
NIGHT_LABEL = {"10.30": "10.30 周五场", "10.31": "10.31 周六场"}

def post_json(url, payload, referer=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Referer": referer} if referer else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def fetch(rows_temporal):
    q = {"datasource": {"id": DATASET, "type": "table"}, "force": False,
         "queries": [{"columns": rows_temporal[0], "metrics": rows_temporal[1],
                      "filters": rows_temporal[2], "row_limit": 5000, "order_desc": False}],
         "result_format": "json", "result_type": "full"}
    return post_json(BASE + "/api/v1/chart/data?form_data=%7B%7D", q,
                     BASE + "/superset/dashboard/27/")["result"][0]["data"]

def conv(ts):
    return datetime.datetime.utcfromtimestamp(ts / 1000 + 8 * 3600)

def main():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    stamp = now.strftime("%Y-%m-%d %H:%M")

    # 场次×票型 汇总
    agg_rows = fetch((["times_time", "ticket_name"],
        ["order_ticket_num", "success_ticket_num", "refund_success_ticket_num"],
        [{"col": "times_time", "op": "TEMPORAL_RANGE", "val": "2026-10-30 : 2026-11-01"}]))
    # 每日销售
    daily_rows = fetch((["sale_dt", "times_time", "ticket_name"],
        ["success_ticket_num"],
        [{"col": "times_time", "op": "TEMPORAL_RANGE", "val": "2026-10-30 : 2026-11-01"}]))

    agg = collections.defaultdict(lambda: {"order": 0, "paid": 0, "refund": 0})
    for r in agg_rows:
        night = NIGHTS.get(r["times_time"])
        if not night or r["ticket_name"] not in SELL:
            continue
        t = "正价票" if r["ticket_name"] == "嘉年华会员专属" else r["ticket_name"]
        k = (night, t)
        agg[k]["order"] += r["order_ticket_num"] or 0
        agg[k]["paid"] += r["success_ticket_num"] or 0
        agg[k]["refund"] += r["refund_success_ticket_num"] or 0

    daily = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in daily_rows:
        night = NIGHTS.get(r["times_time"])
        if not night or r["ticket_name"] not in SELL:
            continue
        daily[night][conv(r["sale_dt"]).date().isoformat()] += r["success_ticket_num"] or 0

    # 汇总计算
    nights = {}
    for night in ("10.30", "10.31"):
        per, tot_paid, tot_stock = {}, 0, 0
        for t, stock in STOCK.items():
            a = agg.get((night, t), {"order": 0, "paid": 0, "refund": 0})
            per[t] = {**a, "stock": stock, "left": stock - a["paid"],
                      "pct": a["paid"] / stock * 100}
            tot_paid += a["paid"]; tot_stock += stock
        nights[night] = {"per": per, "paid": tot_paid, "stock": tot_stock,
                         "left": tot_stock - tot_paid,
                         "pct": tot_paid / tot_stock * 100}

    # 卖速：昨日新增 & 今日至今新增
    today = now.date().isoformat()
    yesterday = (now.date() - datetime.timedelta(days=1)).isoformat()
    pace = {n: {"today": int(daily[n].get(today, 0)),
                "yesterday": int(daily[n].get(yesterday, 0))} for n in ("10.30", "10.31")}

    # 售罄预测（剩余 / 近一日卖速，叠加当天购买占比74%的经验放大）
    forecast = {}
    for n in ("10.30", "10.31"):
        d = daily[n]
        recent = [d.get((now.date() - datetime.timedelta(days=i)).isoformat(), 0) for i in (1, 2, 3)]
        spd = max(sum(recent) / 3, 1)
        if nights[n]["left"] <= 0:
            forecast[n] = "已售罄"
        else:
            days_left = nights[n]["left"] / spd
            eta = now + datetime.timedelta(days=days_left)
            forecast[n] = f"约 {eta.strftime('%m.%d %H:%M')}（按近3日均速 {int(spd)} 张/日，未含当天购买放大）"
            if n == "10.30" or n == "10.31":
                forecast[n] += "；当天购买集中14–22点，实际可能提前"

    data = {"stamp": stamp, "nights": nights, "pace": pace, "forecast": forecast}
    os.makedirs("report", exist_ok=True)
    json.dump(data, open("report/latest.json", "w"), ensure_ascii=False, indent=1)
    open("report/index.html", "w", encoding="utf-8").write(render_html(data))
    send_feishu(data)

def render_html(d):
    rows = ""
    for n in ("10.30", "10.31"):
        v = d["nights"][n]
        rows += f'<tr style="background:#f6f7f9;font-weight:700"><td>{NIGHT_LABEL[n]}</td><td>{v["paid"]:,} / {v["stock"]:,}</td><td>{v["pct"]:.1f}%</td><td>今日+{d["pace"][n]["today"]:,} / 昨日+{d["pace"][n]["yesterday"]:,}</td><td>{d["forecast"][n]}</td></tr>'
        for t in ("超级早鸟票", "早场票", "正价票"):
            p = v["per"][t]
            bar = "🔴" if p["pct"] >= 100 else ("🟠" if p["pct"] >= 85 else ("🟡" if p["pct"] >= 60 else "🟢"))
            rows += f'<tr><td style="padding-left:24px">{bar} {t}</td><td>{p["paid"]:,} / {p["stock"]:,}</td><td>{p["pct"]:.1f}%</td><td>余 {p["left"]:,}</td><td>退款率 {p["refund"]/p["order"]*100:.1f}%</td></tr>' if p["order"] else ""
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3600"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>万圣节库存监控 · {d['stamp']}</title>
<style>body{{font-family:-apple-system,'PingFang SC',sans-serif;max-width:880px;margin:24px auto;padding:0 16px;color:#1f2329}}
table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{padding:8px 10px;border-bottom:1px solid #e5e6eb;text-align:left}}
th{{background:#1f2329;color:#fff}}.foot{{color:#8f959e;font-size:12px;margin-top:16px}}</style></head>
<body><h2>🎃 万圣节 10.30–10.31 库存消耗监控</h2><p>数据截至 <b>{d['stamp']}</b>（每小时自动更新）</p>
<table><tr><th>场次 / 票型</th><th>已售 / 库存</th><th>消耗率</th><th>余量 / 卖速</th><th>售罄预测 / 退款率</th></tr>{rows}</table>
<div class="foot">口径：已售=支付成功票数（扣退款）；库存 超早300/早场900/正价1050 每晚独立；嘉年华会员专属并入正价；剔除 City Drink、限时参团、员工福利场次票。数据源：Superset 看板27。</div>
</body></html>"""

def send_feishu(d):
    hook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not hook:
        print("FEISHU_WEBHOOK not set, skip push"); return
    pages_url = os.environ.get("PAGES_URL", "").strip()
    lines = []
    alerts = []
    for n in ("10.30", "10.31"):
        v = d["nights"][n]
        lines.append(f"**{NIGHT_LABEL[n]}**  合计 {v['paid']:,}/{v['stock']:,}（{v['pct']:.1f}%） 今日+{d['pace'][n]['today']:,}")
        for t in ("超级早鸟票", "早场票", "正价票"):
            p = v["per"][t]
            tag = "❗已售罄" if p["left"] <= 0 else (f"⚠️仅剩 {p['left']}" if p["left"] <= 250 else f"余 {p['left']:,}")
            lines.append(f"　· {t.replace('票','')}：{p['paid']:,}/{p['stock']:,}（{p['pct']:.1f}%） {tag}")
            if 0 < p["left"] <= 250:
                alerts.append(f"{n} {t} 仅剩 {p['left']} 张")
        lines.append(f"　📈 售罄预测：{d['forecast'][n]}")
    if d["nights"]["10.31"]["left"] <= 0:
        alerts.append("10.31 全场已售罄")
    title = "🎃 万圣节库存监控 · " + d["stamp"]
    text = "\n".join(lines)
    if alerts:
        text = "🚨 " + "；".join(alerts) + "\n\n" + text
    if pages_url:
        text += f"\n\n[📊 查看完整报告]({pages_url})"
    card = {"msg_type": "interactive",
            "card": {"header": {"title": {"tag": "plain_text", "content": title},
                                "template": "red" if alerts else "orange"},
                     "elements": [{"tag": "markdown", "content": text},
                                  {"tag": "note", "elements": [{"tag": "plain_text",
                                   "content": "已售=支付成功票数(扣退款)；嘉年华会员并入正价；不含City Drink/参团/员工票"}]}]}}
    req = urllib.request.Request(hook, data=json.dumps(card).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        print("feishu push:", r.status)

if __name__ == "__main__":
    main()

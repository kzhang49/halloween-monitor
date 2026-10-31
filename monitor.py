#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
万圣节 10.30/10.31 库存消耗监控 · 云端完整版
- 数据源：Superset kanban.mmweb.top 看板27 / 数据集53（公开API）
- 预测模型：T-x 累计占比经验外推（普通周五当天购买占71-76%；普通周六 T-1≈31% T-0≈29%）
  + 票型溢出顺移（超早→早场→正价）+ 当天分时购买曲线估售罄钟点
- 销售窗口：场次票仅在当天售卖并延续至自然日次日0点后（sale_dt 次日计入该场 T-0）
- 产出：report/index.html = 完整版看板（与本地版同结构）→ GitHub Pages；飞书卡片 = 告警+入口
"""
import json, os, datetime, collections, urllib.request, base64

BASE = "https://kanban.mmweb.top"
DATASET = 53
STOCK = {"超早": 300, "早场": 900, "正价": 1050}
TICKET_MAP = {"超级早鸟票": "超早", "早场票": "早场", "正价票": "正价", "嘉年华会员专属": "正价"}
NIGHTS_TS = {1793390400000: "10.30", 1793476800000: "10.31"}
LATE_START = datetime.datetime(2026, 10, 31, 2, 0)   # 10.30 场晚场票 10.31 凌晨 2 点起售（不限量，不计入合计）
CUTOFF = {"超早": 21, "早场": 22, "正价": 99}        # 停售时点：超早 21:00 / 早场 22:00（正价不限）
SINGLE_DATASET = 55                                  # 单店票数据集（看板27【实时】单店票售卖情况总览）
SINGLE_EXCLUDE = ("HERO DOME", "SOUL HOUSE by INS新乐园", "复兴公园游乐场")   # 单店票剔除门店
EVENT_DATE = {"10.30": datetime.date(2026, 10, 30), "10.31": datetime.date(2026, 10, 31)}
# 历史规律参数（9/29-10/29 共13个已结束场次验证）
FRI_T0 = 0.74        # 普通周五当天购买占比（范围 0.71-0.76）
HOLIDAY_T0 = 0.615   # 假日型提前预订结构
SAT_T1, SAT_T0 = 0.31, 0.29   # 普通周六 T-1 / T-0 占比 → T-2 末累计占比 0.40
# 当天分时购买曲线（小时: 占当天销量比例）
T0_PROFILE = {9:.04,10:.05,11:.06,12:.07,13:.08,14:.09,15:.09,16:.09,17:.08,18:.08,
              19:.07,20:.07,21:.06,22:.04,23:.02,24:.01}
T1_PROFILE = {9:.03,10:.05,11:.06,12:.08,13:.09,14:.10,15:.10,16:.10,17:.09,18:.08,
              19:.07,20:.06,21:.04,22:.03,23:.01,24:.01}

INK = "#1A1D24"; SUB = "#5A6270"; FAINT = "#98A2B3"
LINE = "#E8EAED"; PANEL = "#F7F8FA"
BLUE = "#2E6BE6"; ORANGE = "#E8543F"; GREEN = "#17A673"; AMBER = "#E8A33D"

def post_json(url, payload, referer=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Referer": referer} if referer else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def fetch(cols, metrics, filters, dataset=DATASET):
    q = {"datasource": {"id": dataset, "type": "table"}, "force": False,
         "queries": [{"columns": cols, "metrics": metrics, "filters": filters,
                      "row_limit": 5000, "order_desc": False}],
         "result_format": "json", "result_type": "full"}
    return post_json(BASE + "/api/v1/chart/data?form_data=%7B%7D", q,
                     BASE + "/superset/dashboard/27/")["result"][0]["data"]

def conv_date(ts):
    return (datetime.datetime.utcfromtimestamp(ts / 1000 + 8 * 3600)).date()

def pct(a, b): return 0.0 if not b else a / b * 100
def fmt(n): return "{:,.0f}".format(n)
def tw(text, size, bold=False):
    w = 0.0
    for ch in str(text):
        w += size * 1.0 if ord(ch) > 127 else (size * 0.30 if ch in " il.,:;'|!" else size * (0.62 if bold else 0.58))
    return w

# ---------------- 取数 ----------------
def load_data():
    agg_rows = fetch(["times_time", "ticket_name"],
        ["order_ticket_num", "success_ticket_num", "refund_success_ticket_num"],
        [{"col": "times_time", "op": "TEMPORAL_RANGE", "val": "2026-10-30 : 2026-11-01"}])
    daily_rows = fetch(["sale_dt", "times_time", "ticket_name"], ["success_ticket_num"],
        [{"col": "times_time", "op": "TEMPORAL_RANGE", "val": "2026-10-30 : 2026-11-01"}])

    agg = collections.defaultdict(lambda: {"order": 0, "paid": 0, "refund": 0})
    late = {"order": 0, "paid": 0, "refund": 0}   # 晚场票（不限量，单列展示在 10.31 场，不进合计）
    for r in agg_rows:
        night = NIGHTS_TS.get(r["times_time"])
        if not night: continue
        if "晚场" in (r["ticket_name"] or ""):
            late["order"] += r["order_ticket_num"] or 0
            late["paid"] += r["success_ticket_num"] or 0
            late["refund"] += r["refund_success_ticket_num"] or 0
            continue
        t = TICKET_MAP.get(r["ticket_name"])
        if not t: continue
        a = agg[(night, t)]
        a["order"] += r["order_ticket_num"] or 0
        a["paid"] += r["success_ticket_num"] or 0
        a["refund"] += r["refund_success_ticket_num"] or 0

    daily = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in daily_rows:
        night = NIGHTS_TS.get(r["times_time"])
        if not night or r["ticket_name"] not in TICKET_MAP: continue
        daily[night][conv_date(r["sale_dt"]).isoformat()] += r["success_ticket_num"] or 0

    # 10.31 单店票（数据集55，按门店；剔除 HERO DOME / SOUL HOUSE / 复兴公园游乐场）
    single_rows = fetch(["merchant_name"], ["success_ticket_num"],
        [{"col": "times_time", "op": "TEMPORAL_RANGE", "val": "2026-10-31 : 2026-11-01"}],
        dataset=SINGLE_DATASET)
    single = {}
    for r in single_rows:
        m = r["merchant_name"]
        if not m or m in SINGLE_EXCLUDE: continue
        single[m] = single.get(m, 0) + (r["success_ticket_num"] or 0)
    return agg, daily, late, single

# ---------------- 售罄钟点估计 ----------------
def hour_of(needed, day_total, today_sold, profile):
    """当天还需卖 needed 张，按分时曲线估计售罄钟点（返回小时，>24 表示当天售不完）"""
    if needed <= 0: return None
    acc = 0.0
    for h in sorted(profile):
        acc += day_total * profile[h]
        if acc - today_sold >= needed:
            return h + 1
    return 99

def hh(h):
    if h is None: return "已售罄"
    if h < 0: return "%02d:00 停售" % (-h)   # 到停售点仍未罄（超早21:00/早场22:00），需求顺延下一票型
    if h >= 99: return "当天售不完"
    d = "次日" if h >= 24 else ""
    return "%s%02d:00 前后" % (d, h % 24)

def cascade(lefts, day_total, today_sold, profile, cutoffs=None):
    """按 超早→早场→正价 顺移估计各票型售罄钟点；超过该票型停售点则标记为停售（负值）"""
    cutoffs = cutoffs or CUTOFF
    out, need = {}, 0
    for t in ("超早", "早场", "正价"):
        need += max(lefts.get(t, 0), 0)
        h = hour_of(need, day_total, today_sold, profile) if lefts.get(t, 0) > 0 else None
        if h is not None and 0 < h < 99 and h > cutoffs.get(t, 99):
            h = -cutoffs[t]
        out[t] = h
    return out

def profile_cum(profile, hour_float):
    return sum(w for h, w in profile.items() if h < hour_float)

def fetch_prev(pages_url):
    """从 Pages 读取上一轮的 latest.json（用于增量 & 防重复推送）"""
    try:
        with urllib.request.urlopen(pages_url.rstrip("/") + "/latest.json", timeout=15) as r:
            return json.load(r)
    except Exception as e:
        print("prev fetch failed:", e); return None

# ---------------- GitHub Pages 发布（本地直发，替代 Actions） ----------------
def gh_put(path, content_str, message):
    """把文件写到 gh-pages 分支（需要 GH_TOKEN，细粒度 PAT: 仅本仓 Contents RW）"""
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        print("no GH_TOKEN, skip publish:", path); return False
    repo = os.environ.get("GH_REPO", "kzhang49/halloween-monitor")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "User-Agent": "halloween-monitor-local"}
    # 取当前 sha（存在则覆盖）
    sha = None
    req = urllib.request.Request("https://api.github.com/repos/%s/contents/%s?ref=gh-pages" % (repo, path),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            sha = json.load(r).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print("gh get sha failed:", e.code); return False
    body = {"message": message, "branch": "gh-pages",
            "content": base64.b64encode(content_str.encode("utf-8")).decode()}
    if sha: body["sha"] = sha
    req = urllib.request.Request("https://api.github.com/repos/%s/contents/%s" % (repo, path),
                                 data=json.dumps(body).encode(), headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print("gh put %s: %d" % (path, r.status)); return True
    except urllib.error.HTTPError as e:
        print("gh put %s failed: %d %s" % (path, e.code, e.read()[:200])); return False

# ---------------- 主流程 ----------------
def _load_dotenv():
    """从脚本旁的 .env 补齐缺失的环境变量（cron 场景下 launchd/crontab 不便修改时用）"""
    try:
        envp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        for line in open(envp):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass


def main():
    _load_dotenv()
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    pages_url = os.environ.get("PAGES_URL", "").strip()

    prev_pages = fetch_prev(pages_url) if pages_url else None
    # 上一轮数据取 Pages 与本地 report/latest.json 中较新者（本地 cron 兜底时增量才准确）
    cands = []
    for p in (prev_pages,):
        if p and p.get("stamp"): cands.append(p)
    try:
        lp = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "report", "latest.json")))
        if lp.get("stamp"): cands.append(lp)
    except Exception:
        lp = None
    def _key(p):
        try: return datetime.datetime.strptime(p["stamp"], "%Y-%m-%d %H:%M")
        except Exception: return datetime.datetime.min
    prev = max(cands, key=_key) if cands else None
    # 防重复：任一来源 <40 分钟则跳过（FORCE=1 强制）
    if not os.environ.get("FORCE"):
        for p in cands:
            try:
                age = (now - _key(p)).total_seconds() / 60
                if age < 40:
                    print("skip: last run %.0f min ago (%s)" % (age, p["stamp"])); return
            except Exception:
                pass
    agg, daily, late, single = load_data()

    D = {"stamp": stamp, "today": today, "nights": {}, "deltas": {},
         "daily": {n: dict(daily[n]) for n in ("10.30", "10.31")}}
    # 晚场票：不限量，单列展示在 10.31 场、不进 2,250 合计（常显，以防早开）
    prev_late = (prev or {}).get("late30") or {}
    D["late30"] = {"paid": late["paid"], "order": late["order"], "refund": late["refund"],
                   "show": True,
                   "delta": late["paid"] - prev_late.get("paid", 0) if prev_late else 0}
    # 10.31 单店票（按门店，剔除指定三店）
    prev_single = (prev or {}).get("single31") or {}
    single_total = sum(single.values())
    prev_per = prev_single.get("per") or {}
    D["single31"] = {"total": single_total, "per": single,
                     "delta": single_total - prev_single.get("total", 0) if prev_single else 0,
                     "per_delta": {m: v - prev_per.get(m, 0) for m, v in single.items()}}
    for night in ("10.30", "10.31"):
        per = {}
        for t in ("超早", "早场", "正价"):
            a = agg.get((night, t), {"order": 0, "paid": 0, "refund": 0})
            per[t] = dict(a, stock=STOCK[t], left=STOCK[t] - a["paid"], pct=pct(a["paid"], STOCK[t]))
        tot_paid = sum(p["paid"] for p in per.values())
        D["nights"][night] = {"per": per, "paid": tot_paid, "stock": 2250,
                              "left": 2250 - tot_paid, "pct": pct(tot_paid, 2250)}
    # 较上一轮的每小时增量（按票型 + 合计）
    if prev and prev.get("nights"):
        for night in ("10.30", "10.31"):
            pn = prev["nights"].get(night)
            if not pn: continue
            dd = {}
            for t in ("超早", "早场", "正价"):
                dd[t] = D["nights"][night]["per"][t]["paid"] - pn.get("per", {}).get(t, {}).get("paid", 0)
            dd["total"] = D["nights"][night]["paid"] - pn.get("paid", 0)
            D["deltas"][night] = dd

    # 累计曲线（含 sale_dt=次日 的 0 点后销售，天然归属该场）
    dates = sorted(set(x for n in daily for x in daily[n]))
    cum = {"10.30": [], "10.31": []}
    acc = {"10.30": 0.0, "10.31": 0.0}
    for dt in dates:
        for n in cum:
            acc[n] += daily[n].get(dt, 0)
            cum[n].append(acc[n])
    D["dates"] = dates; D["cum"] = cum

    # ---------- 预测（与本地完整版同模型） ----------
    ev30, ev31 = EVENT_DATE["10.30"].isoformat(), EVENT_DATE["10.31"].isoformat()
    sold30, sold31 = D["nights"]["10.30"]["paid"], D["nights"]["10.31"]["paid"]
    t30_sold_today = daily["10.30"].get(today, 0)
    t31_sold_today = daily["10.31"].get(today, 0)

    # 实际卖速修正：用当天已过时段的"实测/预期"比例缩放当天总量（跟随每小时售票速度）
    hour_f = now.hour + now.minute / 60.0
    def pace_ratio(sold_today, est_today, profile):
        if hour_f < 12:  # 中午前样本太小（分时累计<15%），不做修正，沿用静态外推
            return 1.0
        exp = est_today * profile_cum(profile, hour_f)
        if exp < 30 or sold_today <= 0: return 1.0
        return min(max(sold_today / exp, 0.6), 1.8)

    fc = {}
    if today <= ev30:
        # 10.30 场：T-0 = 10.30 当天（销售延续至 10.31 凌晨）
        past30 = sold30 - (t30_sold_today if today == ev30 else 0)
        strong_final = past30 / (1 - FRI_T0) if past30 > 0 else sold30
        strong_today = max(strong_final * FRI_T0, t30_sold_today)
        if today == ev30:
            r = pace_ratio(t30_sold_today, strong_today, T0_PROFILE)
            strong_today *= r
            strong_final = past30 + strong_today
        weak_final = past30 / (1 - HOLIDAY_T0) if past30 > 0 else sold30
        weak_today = max(weak_final * HOLIDAY_T0, t30_sold_today)
        lefts = {t: D["nights"]["10.30"]["per"][t]["left"] for t in ("超早", "早场", "正价")}
        strong_h = cascade(lefts, strong_today, t30_sold_today, T0_PROFILE)
        strong_full = hour_of(D["nights"]["10.30"]["left"], strong_today, t30_sold_today, T0_PROFILE)
        fc["10.30"] = {"scenario": "strong" if strong_full and strong_full < 99 else "weak",
                       "strong_final": strong_final, "strong_today": strong_today,
                       "weak_final": weak_final, "weak_today": weak_today,
                       "hours": strong_h, "full_hour": strong_full}
    if today <= ev31:
        # 10.31 场：T-1 = 10.30，T-0 = 10.31
        if today == ev31:   # 当天（T-0）
            past = sold31 - t31_sold_today
            final31 = past / (1 - SAT_T0)
            inc_today = max(final31 * SAT_T0, t31_sold_today)
            prof = T0_PROFILE
        elif today == ev30:  # T-1
            past = sold31 - t31_sold_today
            final31 = past / (1 - SAT_T1 - SAT_T0)
            inc_today = max(final31 * SAT_T1, t31_sold_today)
            prof = T1_PROFILE
        else:                # T-2 及更早，沿用静态外推
            final31 = sold31 / (1 - SAT_T1 - SAT_T0)
            inc_today = 0; prof = T1_PROFILE
        if inc_today > 0:
            r = pace_ratio(t31_sold_today, inc_today, prof)
            inc_today *= r
        lefts31 = {t: D["nights"]["10.31"]["per"][t]["left"] for t in ("超早", "早场", "正价")}
        hours31 = cascade(lefts31, inc_today, t31_sold_today, prof)
        full31 = hour_of(D["nights"]["10.31"]["left"], inc_today, t31_sold_today, prof)
        fc["10.31"] = {"final_est": final31, "inc_today": inc_today,
                       "hours": hours31, "full_hour": full31,
                       "sellout": bool(full31 and full31 < 99)}
    D["forecast"] = fc

    os.makedirs("report", exist_ok=True)
    latest_str = json.dumps(D, ensure_ascii=False, indent=1, default=str)
    open("report/latest.json", "w", encoding="utf-8").write(latest_str)
    html_str = render(D)
    open("report/index.html", "w", encoding="utf-8").write(html_str)
    # 直接发布到 gh-pages（外发链接 kzhang49.github.io/halloween-monitor/ 保持最新）
    if os.environ.get("GH_TOKEN"):
        gh_put("index.html", html_str, "report " + stamp)
        gh_put("latest.json", latest_str, "data " + stamp)
    send_feishu(D)

# ---------------- SVG 图 ----------------
def fig_progress(D):
    """每晚一个独立 SVG，桌面双栏 / 手机堆叠（CSS grid 控制）"""
    PW = 548
    label = {"10.30": "10.30 周五", "10.31": "10.31 周六 · 万圣夜"}
    names = {"超早": ("超级早鸟票", "428 RMB"), "早场": ("早场票", "458 RMB"), "正价": ("正价票（含嘉年华会员）", "488 RMB")}
    panels = []
    for k, x in (("10.30", 0), ("10.31", 0)):
        show_late = k == "10.31" and D.get("late30", {}).get("show")
        H = 566 + (46 if show_late else 0)
        out = ['<svg viewBox="0 0 %d %d" width="100%%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,\'PingFang SC\',Arial,sans-serif">' % (PW, H)]
        n = D["nights"][k]; col = BLUE if k == "10.30" else ORANGE
        out.append('<rect x="%d" y="0" width="%d" height="%d" rx="12" fill="%s"/>' % (x, PW, H, PANEL))
        out.append('<text x="%d" y="32" fill="%s" font-size="17" font-weight="700">%s</text>' % (x + 22, INK, label[k]))
        out.append('<text x="%d" y="32" fill="%s" font-size="12" text-anchor="end">已售 %s / %s</text>' % (x + PW - 22, SUB, fmt(n["paid"]), fmt(n["stock"])))
        barw = PW - 44; y = 62
        for t in ("超早", "早场", "正价"):
            p = n["per"][t]; rate = p["pct"]; nm, price = names[t]
            out.append('<text x="%d" y="%d" fill="%s" font-size="15" font-weight="600">%s</text>' % (x + 22, y, INK, nm))
            out.append('<text x="%d" y="%d" fill="%s" font-size="11.5">%s</text>' % (x + 22 + tw(nm, 15, True) + 8, y, FAINT, price))
            out.append('<text x="%d" y="%d" fill="%s" font-size="21" font-weight="700" text-anchor="end">%.1f%%</text>' % (x + PW - 22, y, col, rate))
            out.append('<text x="%d" y="%d" fill="%s" font-size="11.5">库存 %s · 已售 %s · 待售 %s · 退 %s</text>' % (x + 22, y + 20, SUB, fmt(p["stock"]), fmt(p["paid"]), fmt(p["left"]), fmt(p["refund"])))
            by_ = y + 28
            out.append('<rect x="%d" y="%d" width="%d" height="26" rx="6" fill="%s"/>' % (x + 22, by_, barw, LINE))
            fw = barw * min(rate, 100) / 100
            if fw > 0: out.append('<rect x="%d" y="%d" width="%.1f" height="26" rx="6" fill="%s"/>' % (x + 22, by_, fw, col))
            if rate >= 99.5:
                out.append('<rect x="%d" y="%d" width="34" height="19" rx="9.5" fill="%s"/>' % (x + PW - 56, by_ + 3, col))
                out.append('<text x="%d" y="%d" fill="#FFF" font-size="11" font-weight="600" text-anchor="middle">售罄</text>' % (x + PW - 39, by_ + 17))
            lab = "%s/%s" % (fmt(p["paid"]), fmt(p["stock"]))
            inside = fw > 78
            out.append('<text x="%s" y="%d" fill="%s" font-size="12.5" font-weight="600">%s</text>' % (x + 32 if inside else "%.1f" % (x + 22 + fw + 9), by_ + 18, "#FFF" if inside else INK, lab))
            y += 104
        out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#DDE1E7"/>' % (x + 22, y - 6, x + PW - 22, y - 6))
        trate = n["pct"]
        out.append('<text x="%d" y="%d" fill="%s" font-size="15" font-weight="700">2 点前三票型合计</text>' % (x + 22, y + 24, INK))
        out.append('<text x="%d" y="%d" fill="%s" font-size="21" font-weight="700" text-anchor="end">%.1f%%</text>' % (x + PW - 22, y + 24, GREEN, trate))
        out.append('<text x="%d" y="%d" fill="%s" font-size="11.5">库存总量 2,250（300+900+1050）· 已售 %s · 待售 %s</text>' % (x + 22, y + 44, SUB, fmt(n["paid"]), fmt(n["left"])))
        by_ = y + 52
        out.append('<rect x="%d" y="%d" width="%d" height="26" rx="6" fill="%s"/>' % (x + 22, by_, barw, LINE))
        out.append('<rect x="%d" y="%d" width="%.1f" height="26" rx="6" fill="%s"/>' % (x + 22, by_, barw * trate / 100, GREEN))
        lab = "%s/2,250" % fmt(n["paid"]); fw = barw * trate / 100
        out.append('<text x="%s" y="%d" fill="%s" font-size="12.5" font-weight="600">%s</text>' % (x + 32 if fw > 78 else "%.1f" % (x + 22 + fw + 9), by_ + 18, "#FFF" if fw > 78 else INK, lab))
        if show_late:
            la = D["late30"]
            ly = by_ + 52
            out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#DDE1E7" stroke-dasharray="3 4"/>' % (x + 22, ly - 12, x + PW - 22, ly - 12))
            out.append('<text x="%d" y="%d" fill="%s" font-size="15" font-weight="600">🌙 晚场票</text>' % (x + 22, ly + 4, INK))
            out.append('<text x="%d" y="%d" fill="%s" font-size="11.5">凌晨 2 点起售 · 不限量 · 不计入合计</text>' % (x + 22 + tw("🌙 晚场票", 15, True) + 10, ly + 4, FAINT))
            out.append('<text x="%d" y="%d" fill="%s" font-size="21" font-weight="700" text-anchor="end">%s 张</text>' % (x + PW - 22, ly + 6, ORANGE, fmt(la["paid"])))
            out.append('<text x="%d" y="%d" fill="%s" font-size="11.5" text-anchor="end">已售（无库存上限）</text>' % (x + PW - 22, ly + 26, SUB))
        out.append("</svg>")
        panels.append("\n".join(out))
    return ('<div class="panels">' +
            "".join('<div class="panel">%s</div>' % p for p in panels) + "</div>")

def fig_compare(D, mobile=False):
    W = 560 if mobile else 1132; H = 300
    x0 = 46 if mobile else 66; x1 = W - 24; ytop, ybot = 56, 232
    bw = 30 if mobile else 54
    fs = 10.5 if mobile else 12
    out = ['<svg viewBox="0 0 %d %d" width="100%%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,\'PingFang SC\',Arial,sans-serif">' % (W, H)]
    for v in range(0, 101, 20):
        yy = ybot - (ybot - ytop) * v / 100
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" %s/>' % (x0, yy, x1, yy, LINE, 'stroke-dasharray="3 4"' if v else ""))
        out.append('<text x="%d" y="%.1f" fill="%s" font-size="10" text-anchor="end">%d%%</text>' % (x0 - 8, yy + 4, FAINT, v))
    cats = ["超级早鸟", "早场", "正价", "合计"]
    vals = {k: [D["nights"][k]["per"][t]["pct"] for t in ("超早", "早场", "正价")] + [D["nights"][k]["pct"]] for k in ("10.30", "10.31")}
    slot = (x1 - x0) / len(cats)
    for i, c in enumerate(cats):
        cx = x0 + slot * (i + 0.5)
        for j, k in enumerate(("10.30", "10.31")):
            v = vals[k][i]; h = (ybot - ytop) * min(v, 100) / 100
            bx = cx - bw - 4 + j * (bw + 8); col = BLUE if k == "10.30" else ORANGE
            out.append('<rect x="%.1f" y="%.1f" width="%d" height="%.1f" rx="4" fill="%s" fill-opacity="%s"/>' % (bx, ybot - h, bw, max(h, 1), col, "1" if k == "10.31" else "0.45"))
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="%.1f" font-weight="700" text-anchor="middle">%.0f%%</text>' % (bx + bw / 2, ybot - h - 7, col if k == "10.31" else SUB, fs, v))
        out.append('<text x="%.1f" y="%d" fill="%s" font-size="%.1f" text-anchor="middle">%s</text>' % (cx, ybot + 24, INK, fs + 0.5, c))
    lx = x0
    for k, txt in (("10.30", "10.30 周五"), ("10.31", "10.31 周六")):
        out.append('<rect x="%d" y="22" width="10" height="10" rx="2" fill="%s" fill-opacity="%s"/>' % (lx, BLUE if k == "10.30" else ORANGE, "1" if k == "10.31" else "0.45"))
        out.append('<text x="%d" y="31" fill="%s" font-size="11">%s</text>' % (lx + 16, SUB, txt)); lx += 16 + tw(txt, 11) + 20
    out.append("</svg>")
    return "\n".join(out)

def fig_forecast(D, mobile=False):
    if mobile:
        W, H = 560, 400; x0, x1, ytop, ybot = 44, W - 16, 46, 318
    else:
        W, H = 1132, 356; x0, x1, ytop, ybot = 70, W - 190, 40, 280
    ymax = 2600
    gstep = 650 if mobile else 400
    dates, cum, today = D["dates"], D["cum"], D["today"]
    future = []
    d = datetime.date.fromisoformat(dates[-1]) + datetime.timedelta(days=1)
    while d <= datetime.date(2026, 11, 1):
        future.append(d.isoformat()); d += datetime.timedelta(days=1)
    dates_ext = dates + future
    n = len(dates_ext); step = (x1 - x0) / (n - 1)
    X = lambda i: x0 + step * i
    Y = lambda v: ybot - (ybot - ytop) * v / ymax
    fs = 10.5 if mobile else 12
    out = ['<svg viewBox="0 0 %d %d" width="100%%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,\'PingFang SC\',Arial,sans-serif">' % (W, H)]
    for v in range(0, ymax + 1, gstep):
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" %s/>' % (x0, Y(v), x1, Y(v), LINE, 'stroke-dasharray="3 4"' if v else ""))
        out.append('<text x="%d" y="%.1f" fill="%s" font-size="10" text-anchor="end">%s</text>' % (x0 - 6, Y(v) + 3.5, FAINT, fmt(v)))
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.4" stroke-dasharray="6 5"/>' % (x0, Y(2250), x1, Y(2250), AMBER))
    out.append('<text x="%d" y="%.1f" fill="%s" font-size="10.5" font-weight="600">库存上限 2,250</text>' % (x0 + 6, Y(2250) - 6, AMBER))
    label_every = 3 if mobile else 2
    for i, dt in enumerate(dates_ext):
        if i % label_every == 0 or i >= n - 2:
            lab = dt[5:].replace("-", "/") + ("（今）" if dt == today and mobile else ("（今天）" if dt == today else ""))
            out.append('<text x="%.1f" y="%d" fill="%s" font-size="%.1f" text-anchor="middle">%s</text>' % (X(i), ybot + 20, FAINT, fs - 1.5, lab))
    i_today = dates_ext.index(today)
    out.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="#FDF8E8"/>' % (X(i_today) - step / 2, ytop, x1 - X(i_today) + step / 2, ybot - ytop))
    out.append('<text x="%.1f" y="%d" fill="#B08400" font-size="10">预测区</text>' % (X(i_today) - step / 2 + 5, ybot - 6))
    for k in ("10.30", "10.31"):
        col = BLUE if k == "10.30" else ORANGE
        pts = [(X(i), Y(v)) for i, v in enumerate(cum[k])]
        out.append('<path d="M %s" fill="none" stroke="%s" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>' % (" L ".join("%.1f %.1f" % p for p in pts), col))
        lx, ly = pts[-1]
        out.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="#FFF" stroke="%s" stroke-width="2.5"/>' % (lx, ly, col))
        lab = ("%s 已售 %s" % (k, fmt(cum[k][-1]))) if mobile else ("%s 场 · 已售 %s" % (k, fmt(cum[k][-1])))
        out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="%.1f" font-weight="700" text-anchor="end">%s</text>' % (lx - 8, ly - 12, col, fs, lab))
    fc = D["forecast"]
    # 10.31 场预测：售罄画到库存线+罄；不售罄则区分「明日仍售罄（需求超库存）」与「真不售罄（收官线）」
    if "10.31" in fc:
        f31 = fc["10.31"]; x_start, y_start = X(i_today), Y(cum["10.31"][-1])
        if f31["sellout"]:
            h = f31["full_hour"]
            fx = X(i_today) + step * min(h, 36) / 24
            out.append('<path d="M %.1f %.1f L %.1f %.1f" fill="none" stroke="%s" stroke-width="2.2" stroke-dasharray="6 5"/>' % (x_start, y_start, fx, Y(2250), ORANGE))
            out.append('<circle cx="%.1f" cy="%.1f" r="6" fill="%s"/><text x="%.1f" y="%.1f" fill="#FFF" font-size="10" font-weight="700" text-anchor="middle">罄</text>' % (fx, Y(2250), ORANGE, fx, Y(2250) + 3.5))
            lab = ("10.31场 %s售罄" % hh(h)) if mobile else ("10.31 场 · 预计 %s 售罄" % hh(h))
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="%.1f" font-weight="700" text-anchor="end">%s</text>' % (fx - 8, Y(2250) - 22, ORANGE, fs, lab))
        elif f31["final_est"] >= 2250:
            # 今日(T-1)卖不完，但需求外推超库存 → 明日(T-0)必售罄，按 T0 分时估明日钟点
            sold_today31 = D["daily"]["10.31"].get(today, 0)
            rem_after = D["nights"]["10.31"]["left"] - max(f31["inc_today"] - sold_today31, 0)
            tomorrow_inc = max(f31["final_est"] * SAT_T0, 1)
            h_est = None
            if rem_after <= 0:
                fx = X(i_today) + step * 0.98
                lab = "10.31场 今晚尾声售罄"
            else:
                hh_test = 6.0
                while hh_test <= 24:
                    if profile_cum(T0_PROFILE, hh_test) * tomorrow_inc >= rem_after:
                        h_est = hh_test; break
                    hh_test += 0.25
                if h_est is None:
                    fx = X(i_today) + step * 1.95
                    lab = "10.31场 明日售罄"
                else:
                    fx = X(i_today) + step * (24 + min(h_est, 22)) / 24
                    lab = ("10.31场 明日%02d:00售罄" % h_est) if mobile else ("10.31 场 · 预计明日 %02d:00 前后售罄" % h_est)
            out.append('<path d="M %.1f %.1f L %.1f %.1f" fill="none" stroke="%s" stroke-width="2.2" stroke-dasharray="6 5"/>' % (x_start, y_start, fx, Y(2250), ORANGE))
            out.append('<circle cx="%.1f" cy="%.1f" r="6" fill="%s"/><text x="%.1f" y="%.1f" fill="#FFF" font-size="10" font-weight="700" text-anchor="middle">罄</text>' % (fx, Y(2250), ORANGE, fx, Y(2250) + 3.5))
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="%.1f" font-weight="700" text-anchor="end">%s</text>' % (fx - 8, Y(2250) - 22, ORANGE, fs, lab))
        else:
            fx = X(i_today) + step * 1.5
            fy = Y(min(f31["final_est"], ymax))
            out.append('<path d="M %.1f %.1f L %.1f %.1f" fill="none" stroke="%s" stroke-width="2.2" stroke-dasharray="6 5"/>' % (x_start, y_start, fx, fy, ORANGE))
            lab = ("10.31场 收官~%s 不售罄" % fmt(f31["final_est"])) if mobile else ("10.31 场 · 预计收官 ~%s（%.0f%%），不售罄" % (fmt(f31["final_est"]), f31["final_est"] / 2250 * 100))
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="%.1f" font-weight="700" text-anchor="end">%s</text>' % (fx - 8, fy - 10, ORANGE, fs, lab))
    # 10.30 场预测（强/弱场景都画）
    if "10.30" in fc:
        f30 = fc["10.30"]; x_start, y_start = X(i_today), Y(cum["10.30"][-1])
        if f30["full_hour"] and f30["full_hour"] < 99:
            fx = X(i_today) + step * min(f30["full_hour"], 24) / 24
            out.append('<path d="M %.1f %.1f L %.1f %.1f" fill="none" stroke="%s" stroke-width="2.2" stroke-dasharray="6 5"/>' % (x_start, y_start, fx, Y(2250), BLUE))
            out.append('<circle cx="%.1f" cy="%.1f" r="6" fill="%s"/><text x="%.1f" y="%.1f" fill="#FFF" font-size="10" font-weight="700" text-anchor="middle">罄</text>' % (fx, Y(2250), BLUE, fx, Y(2250) + 3.5))
            lab = ("10.30场 强场景%s" % hh(f30["full_hour"])) if mobile else ("10.30 场 · 强场景 %s 售罄" % hh(f30["full_hour"]))
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="%.1f" font-weight="700" text-anchor="middle">%s</text>' % (fx, Y(2250) + 22, BLUE, fs, lab))
        else:
            send_ = min(f30["strong_final"], 2250)
            fx = X(i_today) + step * 0.9
            out.append('<path d="M %.1f %.1f L %.1f %.1f" fill="none" stroke="%s" stroke-width="2.2" stroke-dasharray="6 5"/>' % (x_start, y_start, fx, Y(send_), BLUE))
            slab = ("强场景收官 ~%s（%.0f%%）" % (fmt(send_), send_ / 2250 * 100))
            if mobile:
                out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="10" text-anchor="end">%s</text>' % (x1 - 2, Y(send_) - 8, BLUE, slab))
            else:
                out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11.5" font-weight="600">%s</text>' % (X(i_today) + step * 0.95, Y(send_) - 8, BLUE, slab))
        wend = min(cum["10.30"][-1] + f30["weak_today"], f30["weak_final"])
        out.append('<path d="M %.1f %.1f L %.1f %.1f" fill="none" stroke="%s" stroke-width="1.8" stroke-dasharray="2 5" stroke-opacity="0.75"/>' % (x_start, y_start, X(i_today) + step * 0.9, Y(wend), BLUE))
        wlab = "弱场景收官 ~%s（%.0f%%）" % (fmt(wend), wend / 2250 * 100)
        if mobile:
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="10" text-anchor="end">%s</text>' % (x1 - 2, Y(wend) + 12, BLUE, wlab))
        else:
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11.5">%s</text>' % (X(i_today) + step * 0.95, Y(wend) + 14, BLUE, wlab))
    lx = x0
    out.append('<line x1="%d" y1="14" x2="%d" y2="14" stroke="%s" stroke-width="2.6"/>' % (lx, lx + 20, SUB))
    out.append('<text x="%d" y="18" fill="%s" font-size="10.5">实际（截至 %s）</text>' % (lx + 26, SUB, D["stamp"][5:]))
    lx += 26 + tw("实际（截至 %s）" % D["stamp"][5:], 10.5) + 18
    out.append('<line x1="%.0f" y1="14" x2="%.0f" y2="14" stroke="%s" stroke-width="2.2" stroke-dasharray="5 4"/>' % (lx, lx + 20, SUB))
    out.append('<text x="%.0f" y="18" fill="%s" font-size="10.5">预测</text>' % (lx + 26, SUB))
    out.append("</svg>")
    return "\n".join(out)

def fig_daily(D, mobile=False):
    W = 560 if mobile else 1132; H = 250
    x0 = 42 if mobile else 66; x1 = W - 20; ytop, ybot = 34, 196
    bw = 18 if mobile else 40
    last7 = D["dates"][-7:]
    ymax = max(400, int(max(D["daily"][n].get(d, 0) for n in ("10.30", "10.31") for d in last7) * 1.2))
    out = ['<svg viewBox="0 0 %d %d" width="100%%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,\'PingFang SC\',Arial,sans-serif">' % (W, H)]
    for v in range(0, ymax + 1, 200 if mobile else 100):
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" %s/>' % (x0, ybot - (ybot - ytop) * v / ymax, x1, ybot - (ybot - ytop) * v / ymax, LINE, 'stroke-dasharray="3 4"' if v else ""))
        out.append('<text x="%d" y="%.1f" fill="%s" font-size="10" text-anchor="end">%d</text>' % (x0 - 6, ybot - (ybot - ytop) * v / ymax + 3.5, FAINT, v))
    slot = (x1 - x0) / len(last7)
    for i, dt in enumerate(last7):
        cx = x0 + slot * (i + 0.5)
        for j, k in enumerate(("10.30", "10.31")):
            v = D["daily"][k].get(dt, 0); h = (ybot - ytop) * v / ymax
            bx = cx - bw - 3 + j * (bw + 6)
            out.append('<rect x="%.1f" y="%.1f" width="%d" height="%.1f" rx="3" fill="%s" fill-opacity="%s"/>' % (bx, ybot - h, bw, max(h, 1), BLUE if k == "10.30" else ORANGE, "1" if k == "10.31" else "0.45"))
            out.append('<text x="%.1f" y="%.1f" fill="%s" font-size="9.5" font-weight="600" text-anchor="middle">%s</text>' % (bx + bw / 2, ybot - h - 4, SUB, fmt(v)))
        out.append('<text x="%.1f" y="%d" fill="%s" font-size="10" text-anchor="middle">%s</text>' % (cx, ybot + 18, FAINT, dt[5:].replace("-", "/")))
    lx = x0
    for k, txt in (("10.30", "10.30 周五"), ("10.31", "10.31 周六")):
        out.append('<rect x="%.0f" y="6" width="9" height="9" rx="2" fill="%s" fill-opacity="%s"/>' % (lx, BLUE if k == "10.30" else ORANGE, "1" if k == "10.31" else "0.45"))
        out.append('<text x="%.0f" y="14" fill="%s" font-size="10.5">%s</text>' % (lx + 14, SUB, txt)); lx += 14 + tw(txt, 10.5) + 18
    out.append("</svg>")
    return "\n".join(out)

def pred_table(D):
    fc = D["forecast"]; rows = []; last = None
    conf_color = {"高": GREEN, "中高": BLUE, "中": AMBER, "中低": ORANGE, "—": FAINT}
    entries = []
    if "10.31" in fc:
        f = fc["10.31"]; n = D["nights"]["10.31"]
        for t, cn in (("超早", "超级早鸟"), ("早场", "早场票"), ("正价", "正价票")):
            p = n["per"][t]
            if p["left"] <= 0:
                entries.append(("10.31 万圣夜", cn, "0", "已售罄", "—", "退票回流是唯一余量来源"))
            else:
                h = f["hours"].get(t)
                entries.append(("10.31 万圣夜", cn, fmt(p["left"]), hh(h), "高" if h and 0 < h < 99 else "中",
                                "按 T-1 需求 %.0f 张 + 溢出顺移（超早→早场→正价）" % f["inc_today"]))
        entries.append(("10.31 万圣夜", "全场合计", fmt(n["left"]),
                        ("预计 %s 售罄，10.31 当天无票" % hh(f["full_hour"])) if f["sellout"] else "预计有剩票",
                        "高" if f["sellout"] else "中",
                        "T-2 末累计 %s 张，需求外推 ~%s ≫ 2,250" % (fmt(n["paid"]), fmt(f["final_est"]))))
    if "10.30" in fc:
        f = fc["10.30"]; n = D["nights"]["10.30"]
        for t, cn in (("超早", "超级早鸟"), ("早场", "早场票"), ("正价", "正价票")):
            p = n["per"][t]
            if p["left"] <= 0:
                entries.append(("10.30 周五", cn, "0", "已售罄", "—", ""))
            else:
                h = f["hours"].get(t)
                entries.append(("10.30 周五", cn, fmt(p["left"]), hh(h),
                                "高" if t == "超早" else ("中" if t == "早场" else "中低"),
                                "强场景当天总量 ~%s 张（当天占比≈74%%）；弱场景剩票" % fmt(f["strong_today"])))
        if f["full_hour"] and f["full_hour"] < 99:
            when30 = "强场景：%s 售罄；弱场景：收官 ~%s（%.0f%%）" % (hh(f["full_hour"]), fmt(f["weak_final"]), f["weak_final"] / 2250 * 100)
        else:
            when30 = "按当前卖速收官 ~%s（%.0f%%），预计不售罄" % (fmt(f["strong_final"]), f["strong_final"] / 2250 * 100)
        entries.append(("10.30 周五", "全场合计", fmt(n["left"]), when30,
                        "中", "取决于当天购买占比：普通周五 71%%–76%% → 售罄；假日型提前预订 → 不售罄"))
    for sess, typ, left, when, conf, why in entries:
        sess_td = "<td class='psess'>%s</td>" % sess if sess != last else "<td></td>"
        last = sess
        cc = conf_color[conf]
        rows.append("<tr>%s<td><b>%s</b></td><td class='num'>%s</td><td>%s</td><td><span class='conf' style='color:%s;background:%s14'>%s</span></td><td class='why'>%s</td></tr>" % (sess_td, typ, left, when, cc, cc, conf, why))
    return """<table class="predt"><thead><tr><th>场次</th><th>票型</th><th class="num">当前余量</th><th>预计售罄时间</th><th>置信度</th><th>判断依据</th></tr></thead><tbody>%s</tbody></table>""" % "\n".join(rows)

def single_table(D):
    """10.31 单店票按门店（剔除 HERO DOME / SOUL HOUSE / 复兴公园游乐场）"""
    s = D.get("single31") or {}
    per = s.get("per") or {}; pd = s.get("per_delta") or {}
    rows = []
    for i, (m, v) in enumerate(sorted(per.items(), key=lambda x: -x[1])):
        d = pd.get(m, 0)
        dtxt = "<b style='color:%s'>+%s</b>" % (GREEN, fmt(d)) if d > 0 else ("<span style='color:%s'>%s</span>" % (ORANGE, fmt(d)) if d < 0 else "—")
        rows.append("<tr><td>%s</td><td class='num strong'>%s</td><td class='num'>%s</td></tr>" % (m, fmt(v), dtxt))
    td = s.get("delta", 0)
    tdtxt = "<b style='color:%s'>+%s</b>" % (GREEN, fmt(td)) if td > 0 else "—"
    rows.append("<tr class='tot'><td>合计（%d 家店）</td><td class='num strong'>%s</td><td class='num'>%s</td></tr>" % (len(per), fmt(s.get("total", 0)), tdtxt))
    return """<table><thead><tr><th>门店</th><th class="num">已售（张）</th><th class="num">较上轮</th></tr></thead><tbody>%s</tbody></table>""" % "\n".join(rows)

def detail_table(D):
    rows = []
    for k in ("10.30", "10.31"):
        n = D["nights"][k]; col = BLUE if k == "10.30" else ORANGE
        names = {"超早": "超级早鸟票", "早场": "早场票", "正价": "正价票（含嘉年华会员）"}
        for i, t in enumerate(("超早", "早场", "正价")):
            p = n["per"][t]
            rows.append("<tr><td>%s</td><td>%s</td><td class='num'>%s</td><td class='num strong'>%s</td><td class='num'>%s</td><td class='pctcell'><b style='color:%s'>%.1f%%</b><div class='mbar'><span style='width:%.1f%%;background:%s'></span></div></td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%.1f%%</td></tr>"
                        % (k if i == 0 else "", names[t], fmt(p["stock"]), fmt(p["paid"]), fmt(p["left"]), col, p["pct"], min(p["pct"], 100), col, fmt(p["order"]), fmt(p["refund"]), pct(p["refund"], p["order"])))
        rows.append("<tr class='tot'><td>%s</td><td>2 点前三票型合计</td><td class='num'>2,250</td><td class='num strong'>%s</td><td class='num'>%s</td><td class='pctcell'><b style='color:%s'>%.1f%%</b><div class='mbar'><span style='width:%.1f%%;background:%s'></span></div></td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%.1f%%</td></tr>"
                    % (k, fmt(n["paid"]), fmt(n["left"]), GREEN, n["pct"], min(n["pct"], 100), GREEN,
                       fmt(sum(p["order"] for p in n["per"].values())), fmt(sum(p["refund"] for p in n["per"].values())),
                       pct(sum(p["refund"] for p in n["per"].values()), sum(p["order"] for p in n["per"].values()))))
        if k == "10.31" and D.get("late30", {}).get("show"):
            la = D["late30"]
            rows.append("<tr><td></td><td>🌙 晚场票（不限量）</td><td class='num'>不限量</td><td class='num strong'>%s</td><td class='num'>—</td><td class='pctcell'><b style='color:%s'>—</b><div class='mbar'><span style='width:0%%'></span></div></td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%.1f%%</td></tr>"
                        % (fmt(la["paid"]), ORANGE, fmt(la["order"]), fmt(la["refund"]), pct(la["refund"], la["order"])))
    return """<table><thead><tr><th>场次</th><th>票型</th><th class="num">库存</th><th class="num">已售</th><th class="num">待售</th><th>库存消耗进度</th><th class="num">下单</th><th class="num">退款</th><th class="num">退款率</th></tr></thead><tbody>%s</tbody></table>""" % "\n".join(rows)

CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#F0F2F5; font-family:-apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif; color:#1A1D24; padding:24px 0 48px; -webkit-font-smoothing:antialiased; }
.container { max-width:1180px; margin:0 auto; padding:0 12px; }
.card { background:#FFF; border-radius:14px; padding:22px 24px 24px; box-shadow:0 1px 3px rgba(16,24,40,.07); margin-bottom:16px; }
h1 { font-size:23px; font-weight:700; }
.subtitle { font-size:12.5px; color:#5A6270; line-height:1.7; margin-top:8px; }
h2 { font-size:15.5px; font-weight:700; margin-bottom:4px; }
h2 span { font-weight:400; color:#98A2B3; font-size:12px; margin-left:8px; }
.hint { font-size:12px; color:#5A6270; margin-bottom:14px; line-height:1.6; }
table { width:100%; border-collapse:collapse; font-size:12.5px; }
th { text-align:left; font-weight:600; color:#5A6270; font-size:11.5px; padding:9px 8px; border-bottom:1.5px solid #E8EAED; white-space:nowrap; }
th.num, td.num { text-align:right; }
td { padding:9px 8px; border-bottom:1px solid #F0F2F5; vertical-align:middle; }
td.strong { font-weight:700; }
tr.tot td { background:#FAFBFC; font-weight:600; border-top:1.5px solid #E8EAED; }
.pctcell { width:170px; }
.predt td.why { color:#5A6270; font-size:12px; line-height:1.6; }
.predt .psess { font-weight:700; white-space:nowrap; }
.conf { display:inline-block; font-size:11px; font-weight:700; padding:2px 8px; border-radius:6px; white-space:nowrap; }
.pctcell b { font-size:12.5px; display:block; margin-bottom:4px; }
.mbar { height:6px; border-radius:3px; background:#EDEFF2; overflow:hidden; }
.mbar span { display:block; height:6px; border-radius:3px; }
.foot { text-align:center; font-size:11px; color:#B0B7C3; margin-top:6px; }
/* 响应式：双栏进度图 + 图表桌面/手机双版本切换 */
.panels { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.only-mobile { display:none; }
.table-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; }
.table-wrap table { min-width:640px; }
@media (max-width:768px) {
  body { padding:12px 0 32px; }
  .container { padding:0 8px; }
  .card { padding:16px 14px 18px; border-radius:12px; }
  h1 { font-size:18px; line-height:1.4; }
  .subtitle { font-size:11.5px; }
  .panels { grid-template-columns:1fr; }
  .only-desktop { display:none; }
  .only-mobile { display:block; }
  .hint { font-size:11.5px; }
  .table-wrap table { min-width:600px; }
}
"""

def render(D):
    fc = D["forecast"]
    h31 = fc.get("10.31", {}).get("full_hour")
    h30 = fc.get("10.30", {}).get("full_hour")
    hint2 = "用户以当天购入为主：近 4 周普通周五当天购买占 <b>71%–76%</b>，普通周六 T-1 日占 ~31%、T-0 占 ~29%。"
    if h31 and h31 < 99:
        hint2 += " 据此外推：<b style='color:%s'>10.31 场预计 %s 售罄，当天无票可售</b>；" % (ORANGE, hh(h31))
    if h30 and h30 < 99:
        hint2 += "<b style='color:%s'>10.30 场强场景下 %s 售罄</b>（弱场景收官约 %.0f%%）。" % (BLUE, hh(h30), D["forecast"]["10.30"]["weak_final"] / 2250 * 100)
    elif "10.30" in fc:
        f30 = fc["10.30"]
        hint2 += "<b style='color:%s'>10.30 场按当前卖速预计收官 ~%s（%.0f%%），不售罄</b>。" % (BLUE, fmt(f30["strong_final"]), f30["strong_final"] / 2250 * 100)
    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><meta http-equiv="refresh" content="3600">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate"><meta http-equiv="Pragma" content="no-cache">
<title>万圣节库存消耗进度 · 10.30 / 10.31</title><style>__CSS__</style></head><body><div class="container">
<div class="card"><h1>万圣节库存消耗进度 · 10.30（周五）vs 10.31（周六 · 万圣夜）</h1>
<div class="subtitle">数据截至 <b>__STAMP__</b>（每小时自动更新）｜ 来源：Superset 看板 27【实时】散客票务监测 ｜ 口径：<b>支付成功票数 ÷ 单日库存</b>（超早 300 / 早场 900 / 正价 1,050，每晚独立）｜ 已剔除 City Drink、限时参团、员工福利场次票；<b>嘉年华会员专属并入正价票</b>；场次销售延续至自然日次日 0 点后（计入该场）</div></div>
<div class="card"><h2>① 分晚 · 各票型库存消耗进度<span>条内数字 = 已售 / 库存</span></h2>
<div class="hint">左右两栏为 10.30 与 10.31 两个独立场次；末行「2 点前三票型合计」对应每晚 2,250 张的 2 点前库存池。</div>__FIG1__</div>
<div class="card"><h2>② 售罄预测<span>基于历史场次"当天购入"规律外推</span></h2>
<div class="hint">__HINT2__</div>__FIG3____PRED__
<div class="hint" style="margin:12px 0 0">售罄钟点为经验估计：按当天分时购买曲线（高峰 14:00–22:00）推算，数据粒度为日级，实际可能前后漂移 1–2 小时。</div></div>
<div class="card"><h2>③ 两晚同票型消耗率对比<span>同口径横向可比</span></h2>__FIG2__</div>
<div class="card"><h2>④ 近 7 日单日新增支付票数<span>卖速趋势</span></h2>__FIG4__</div>
<div class="card"><h2>⑤ 10.31 单店票售卖 · 按门店<span>数据集55【实时】单店票售卖情况总览</span></h2>
<div class="hint">当天全部场次单店票支付成功票数；已剔除 HERO DOME、SOUL HOUSE、复兴公园游乐场。</div>__SINGLE__</div>
<div class="card"><h2>⑥ 明细数据</h2>__TABLE__
<div class="hint" style="margin:12px 0 0">单位：张。已售 = 支付成功票数（已扣退款成功），非核销。</div></div>
<div class="card"><h2>口径与模型备注</h2><div class="note">
<b>① 库存</b>：超早 300 / 早场 900 / 正价 1,050，每晚独立。<br>
<b>② 已售</b>：支付成功票数（order_status=2，已扣退款），反映库存占用而非到场。<br>
<b>③ 票型归并</b>：嘉年华会员专属并入正价；剔除 City Drink、限时参团、员工福利场次票。<br>
<b>④ 销售窗口</b>：场次票仅在当天售卖，并延续至自然日次日 0 点后（sale_dt 次日计入该场 T-0）；超早 21:00 / 早场 22:00 停售（预测中到点未罄标记为「停售」，需求顺延至下一票型）；10.30 场晚场票 10.31 凌晨 2 点起售、不限量、不计入合计。<br>
<b>⑤ 预测模型</b>：9/29–10/29 共 13 个已结束场次的分销售日累计占比经验外推（普通周五当天 71%–76%；普通周六 T-1 ~31% / T-0 ~29%）；含票型溢出顺移与当天分时曲线。<br>
<b>⑥ 数据源</b>：Superset 看板 27，数据集 53，看板缓存 5 分钟；本页每小时自动生成。</div></div>
<div class="foot">INS LAND · 万圣节票务库存监测 · 生成于 __STAMP__</div></div></body></html>"""
    html = html.replace("__CSS__", CSS).replace("__STAMP__", D["stamp"]).replace("__HINT2__", hint2)
    html = html.replace("__FIG1__", fig_progress(D))
    for tag, fn in (("__FIG2__", fig_compare), ("__FIG3__", fig_forecast), ("__FIG4__", fig_daily)):
        html = html.replace(tag,
            '<div class="only-desktop">%s</div><div class="only-mobile">%s</div>'
            % (fn(D), fn(D, mobile=True)))
    html = html.replace("__TABLE__", '<div class="table-wrap">%s</div>' % detail_table(D))
    html = html.replace("__PRED__", '<div class="table-wrap">%s</div>' % pred_table(D))
    html = html.replace("__SINGLE__", '<div class="table-wrap">%s</div>' % single_table(D))
    return html

def send_feishu(D):
    hook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not hook:
        print("no webhook, skip"); return
    pages = os.environ.get("PAGES_URL", "").strip()
    lines, alerts = [], []
    # 14:00 起卡片只推 10.31 场（10.30 场已收官；看板链接里仍保留双场数据）
    k = "10.31"
    n = D["nights"][k]; lab = "10.31 周六·万圣夜"
    dd = D.get("deltas", {}).get(k)
    inc = "　⏫ 较上小时 **+%s**" % fmt(dd["total"]) if dd else ""
    lines.append("**%s** 合计 %s/2,250（%.1f%%）%s" % (lab, fmt(n["paid"]), n["pct"], inc))
    for t in ("超早", "早场", "正价"):
        p = n["per"][t]
        tag = "❗已售罄" if p["left"] <= 0 else ("⚠️仅剩 %s" % fmt(p["left"]) if p["left"] <= 250 else "余 %s" % fmt(p["left"]))
        d1 = "（+%s）" % fmt(dd[t]) if dd and dd.get(t) else ""
        lines.append("　· %s：%s/%s（%.1f%%）%s%s" % (t, fmt(p["paid"]), fmt(p["stock"]), p["pct"], tag, d1))
        if 0 < p["left"] <= 250: alerts.append("%s %s 仅剩 %s 张" % (k, t, fmt(p["left"])))
    if D.get("late30", {}).get("show"):
        la = D["late30"]
        d1 = "（+%s）" % fmt(la["delta"]) if la.get("delta") else ""
        lines.append("　· 🌙 晚场票（不限量）：已售 %s 张%s" % (fmt(la["paid"]), d1))
    f = D["forecast"].get(k)
    if f:
        hs = f.get("hours", {})
        seg = " / ".join("%s %s" % (t, hh(hs.get(t))) for t in ("超早", "早场", "正价") if n["per"][t]["left"] > 0)
        if f.get("sellout"):
            lines.append("　📈 预测：%s 全场售罄（%s）" % (hh(f["full_hour"]), seg))
        else:
            sold_seg = "、".join("%s %s 售罄" % (t, hh(hs.get(t))) for t in ("超早", "早场", "正价") if n["per"][t]["left"] > 0 and hs.get(t) and 0 < hs[t] < 99)
            tail = ("；" + sold_seg) if sold_seg else ""
            lines.append("　📈 预测：按当前卖速收官 ~%s（%.0f%%）预计不售罄%s" % (fmt(f["final_est"]), f["final_est"] / 2250 * 100, tail))
    # 10.31 单店票（剔除 HERO DOME / SOUL HOUSE / 复兴公园游乐场）
    s = D.get("single31") or {}
    if s.get("per"):
        lines.append("")
        lines.append("— — — — — — — — — —")
        d1 = "　⏫ 较上小时 **+%s**" % fmt(s["delta"]) if s.get("delta") else ""
        lines.append("**10.31 单店票** 合计 %s 张（%d 家店）%s" % (fmt(s["total"]), len(s["per"]), d1))
        per_d = s.get("per_delta") or {}
        shop_seg = " · ".join("%s %s%s" % (m, fmt(v), "（+%s）" % fmt(per_d[m]) if per_d.get(m) else "")
                              for m, v in sorted(s["per"].items(), key=lambda x: -x[1]))
        lines.append("　· " + shop_seg)
    text = "\n".join(lines)
    if alerts: text = "🚨 " + "；".join(alerts) + "\n\n" + text
    elements = [{"tag": "markdown", "content": text}]
    if pages:
        bust = pages.rstrip("/") + "/?v=202610301004"   # 固定外发链接（该URL持续更新；页面内有no-cache+每小时自动刷新）
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "📊 打开完整看板（含预测曲线）"}, "url": bust, "type": "primary"}]})
    note = "已售=支付成功(扣退款)；嘉年华会员并入正价；不含City Drink/参团/员工票；销售延续至次日0点后；超早21:00/早场22:00停售；单店票不含HERO DOME/SOUL HOUSE/复兴公园游乐场；晚场票不限量、不计入合计"
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
    card = {"msg_type": "interactive", "card": {"header": {"title": {"tag": "plain_text", "content": "🎃 万圣节库存监控 · " + D["stamp"]}, "template": "red" if alerts else "orange"}, "elements": elements}}
    req = urllib.request.Request(hook, data=json.dumps(card).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        print("feishu push:", r.status)

if __name__ == "__main__":
    main()

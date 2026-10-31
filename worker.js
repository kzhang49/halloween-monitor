// 万圣节 10.30/10.31 库存消耗监控 · Cloudflare Worker 版
// 调度：Cron Trigger（UTC 每小时）→ 拉 Superset 看板27 → 计算/预测 → 发布 gh-pages → 推飞书卡片
// env secrets: FEISHU_WEBHOOK, GH_TOKEN, RUN_KEY ; vars: PAGES_URL, GH_REPO
const BASE = "https://kanban.mmweb.top";
const DATASET = 53;
const STOCK = { "超早": 300, "早场": 900, "正价": 1050 };
const TICKET_MAP = { "超级早鸟票": "超早", "早场票": "早场", "正价票": "正价", "嘉年华会员专属": "正价" };
const NIGHTS_TS = { 1793390400000: "10.30", 1793476800000: "10.31" };
const LATE_START_MS = Date.UTC(2026, 9, 30, 18, 0);   // 10.30 场晚场票 10.31 02:00(北京) 起售（不限量，不计入合计）
const CUTOFF = { "超早": 21, "早场": 22, "正价": 99 }; // 停售时点：超早 21:00 / 早场 22:00（正价不限）
const SINGLE_DATASET = 55;                            // 单店票数据集（看板27【实时】单店票售卖情况总览）
const SINGLE_EXCLUDE = ["HERO DOME", "SOUL HOUSE by INS新乐园", "复兴公园游乐场"];   // 单店票剔除门店
const FRI_T0 = 0.74, HOLIDAY_T0 = 0.615, SAT_T1 = 0.31, SAT_T0 = 0.29;
const T0_PROFILE = { 9: .04, 10: .05, 11: .06, 12: .07, 13: .08, 14: .09, 15: .09, 16: .09, 17: .08, 18: .08, 19: .07, 20: .07, 21: .06, 22: .04, 23: .02, 24: .01 };
const T1_PROFILE = { 9: .03, 10: .05, 11: .06, 12: .08, 13: .09, 14: .10, 15: .10, 16: .10, 17: .09, 18: .08, 19: .07, 20: .06, 21: .04, 22: .03, 23: .01, 24: .01 };

const INK = "#1A1D24", SUB = "#5A6270", FAINT = "#98A2B3";
const LINE = "#E8EAED", PANEL = "#F7F8FA";
const BLUE = "#2E6BE6", ORANGE = "#E8543F", GREEN = "#17A673", AMBER = "#E8A33D";

// ---------- 时间工具（统一按北京时间） ----------
function bjNow() { return new Date(Date.now() + 8 * 3600e3); } // 用 getUTC* 读取即为北京时间
function iso(d) { return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0") + "-" + String(d.getUTCDate()).padStart(2, "0"); }
function stampOf(d) { return iso(d) + " " + String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0"); }
function parseStamp(s) { // "YYYY-MM-DD HH:MM" (北京) -> epoch ms
  const m = s.match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
  if (!m) return 0;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 8, +m[5]);
}
function tsToIso(ts) { return iso(new Date(ts + 8 * 3600e3)); }

const pct = (a, b) => (!b ? 0 : a / b * 100);
const fmt = (n) => Math.round(n).toLocaleString("en-US");
function tw(text, size, bold) {
  let w = 0;
  for (const ch of String(text)) {
    w += ch.charCodeAt(0) > 127 ? size : (" il.,:;'|!".includes(ch) ? size * 0.30 : size * (bold ? 0.62 : 0.58));
  }
  return w;
}
const f1 = (v) => (Math.round(v * 10) / 10).toFixed(1);

// ---------- 取数 ----------
async function postJson(url, payload, referer) {
  const r = await fetch(url, {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, referer ? { "Referer": referer } : {}),
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error("POST " + url + " -> " + r.status);
  return r.json();
}
async function fetchRows(cols, metrics, filters, dataset) {
  const q = { datasource: { id: dataset || DATASET, type: "table" }, force: false,
    queries: [{ columns: cols, metrics, filters, row_limit: 5000, order_desc: false }],
    result_format: "json", result_type: "full" };
  const j = await postJson(BASE + "/api/v1/chart/data?form_data=%7B%7D", q, BASE + "/superset/dashboard/27/");
  return j.result[0].data;
}
async function loadData() {
  const aggRows = await fetchRows(["times_time", "ticket_name"],
    ["order_ticket_num", "success_ticket_num", "refund_success_ticket_num"],
    [{ col: "times_time", op: "TEMPORAL_RANGE", val: "2026-10-30 : 2026-11-01" }]);
  const dailyRows = await fetchRows(["sale_dt", "times_time", "ticket_name"], ["success_ticket_num"],
    [{ col: "times_time", op: "TEMPORAL_RANGE", val: "2026-10-30 : 2026-11-01" }]);
  const agg = {};
  const late = { "10.30": { order: 0, paid: 0, refund: 0 }, "10.31": { order: 0, paid: 0, refund: 0 } };   // 各场次晚场票（不限量，单列，不进合计）
  for (const r of aggRows) {
    const night = NIGHTS_TS[r.times_time];
    if (!night) continue;
    if ((r.ticket_name || "").includes("晚场")) {
      const a = late[night];
      a.order += r.order_ticket_num || 0;
      a.paid += r.success_ticket_num || 0;
      a.refund += r.refund_success_ticket_num || 0;
      continue;
    }
    const t = TICKET_MAP[r.ticket_name];
    if (!t) continue;
    const k = night + "|" + t;
    if (!agg[k]) agg[k] = { order: 0, paid: 0, refund: 0 };
    agg[k].order += r.order_ticket_num || 0;
    agg[k].paid += r.success_ticket_num || 0;
    agg[k].refund += r.refund_success_ticket_num || 0;
  }
  const daily = { "10.30": {}, "10.31": {} };
  for (const r of dailyRows) {
    const night = NIGHTS_TS[r.times_time];
    if (!night || !TICKET_MAP[r.ticket_name]) continue;
    const d = tsToIso(r.sale_dt);
    daily[night][d] = (daily[night][d] || 0) + (r.success_ticket_num || 0);
  }
  // 10.31 单店票（数据集55，按门店；剔除 HERO DOME / SOUL HOUSE / 复兴公园游乐场）
  const singleRows = await fetchRows(["merchant_name"], ["success_ticket_num"],
    [{ col: "times_time", op: "TEMPORAL_RANGE", val: "2026-10-31 : 2026-11-01" }], SINGLE_DATASET);
  const single = {};
  for (const r of singleRows) {
    const m = r.merchant_name;
    if (!m || SINGLE_EXCLUDE.includes(m)) continue;
    single[m] = (single[m] || 0) + (r.success_ticket_num || 0);
  }
  return { agg, daily, late, single };
}

// ---------- 售罄钟点估计 ----------
function hourOf(needed, dayTotal, todaySold, profile) {
  if (needed <= 0) return null;
  let acc = 0;
  for (const h of Object.keys(profile).map(Number).sort((a, b) => a - b)) {
    acc += dayTotal * profile[h];
    if (acc - todaySold >= needed) return h + 1;
  }
  return 99;
}
function hh(h) {
  if (h === null || h === undefined) return "已售罄";
  if (h < 0) return String(-h).padStart(2, "0") + ":00 停售";   // 到停售点仍未罄（超早21:00/早场22:00），需求顺延下一票型
  if (h >= 99) return "当天售不完";
  return (h >= 24 ? "次日" : "") + String(h % 24).padStart(2, "0") + ":00 前后";
}
function cascade(lefts, dayTotal, todaySold, profile, cutoffs) {
  cutoffs = cutoffs || CUTOFF;
  const out = {}; let need = 0;
  for (const t of ["超早", "早场", "正价"]) {
    need += Math.max(lefts[t] || 0, 0);
    let h = (lefts[t] || 0) > 0 ? hourOf(need, dayTotal, todaySold, profile) : null;
    if (h !== null && h > 0 && h < 99 && h > (cutoffs[t] || 99)) h = -cutoffs[t];
    out[t] = h;
  }
  return out;
}
function profileCum(profile, hourFloat) {
  let s = 0;
  for (const [h, w] of Object.entries(profile)) if (+h < hourFloat) s += w;
  return s;
}
async function fetchPrev(pagesUrl) {
  try {
    const r = await fetch(pagesUrl.replace(/\/$/, "") + "/latest.json", { cf: { cacheTtl: 0 } });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
}

// ---------- SVG 图 ----------
function figProgress(D) {
  const PW = 548;
  const label = { "10.30": "10.30 周五", "10.31": "10.31 周六 · 万圣夜" };
  const names = { "超早": ["超级早鸟票", "428 RMB"], "早场": ["早场票", "458 RMB"], "正价": ["正价票（含嘉年华会员）", "488 RMB"] };
  const panels = [];
  for (const k of ["10.30", "10.31"]) {
    const showLate = D.late && D.late[k] && D.late[k].show;
    const H = 566 + (showLate ? 46 : 0);
    const x = 0, out = [];
    out.push(`<svg viewBox="0 0 ${PW} ${H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,'PingFang SC',Arial,sans-serif">`);
    const n = D.nights[k], col = k === "10.30" ? BLUE : ORANGE;
    out.push(`<rect x="${x}" y="0" width="${PW}" height="${H}" rx="12" fill="${PANEL}"/>`);
    out.push(`<text x="${x + 22}" y="32" fill="${INK}" font-size="17" font-weight="700">${label[k]}</text>`);
    out.push(`<text x="${x + PW - 22}" y="32" fill="${SUB}" font-size="12" text-anchor="end">已售 ${fmt(n.paid)} / ${fmt(n.stock)}</text>`);
    const barw = PW - 44; let y = 62;
    for (const t of ["超早", "早场", "正价"]) {
      const p = n.per[t], rate = p.pct, [nm, price] = names[t];
      out.push(`<text x="${x + 22}" y="${y}" fill="${INK}" font-size="15" font-weight="600">${nm}</text>`);
      out.push(`<text x="${x + 22 + tw(nm, 15, true) + 8}" y="${y}" fill="${FAINT}" font-size="11.5">${price}</text>`);
      out.push(`<text x="${x + PW - 22}" y="${y}" fill="${col}" font-size="21" font-weight="700" text-anchor="end">${f1(rate)}%</text>`);
      out.push(`<text x="${x + 22}" y="${y + 20}" fill="${SUB}" font-size="11.5">库存 ${fmt(p.stock)} · 已售 ${fmt(p.paid)} · 待售 ${fmt(p.left)} · 退 ${fmt(p.refund)}</text>`);
      const by = y + 28;
      out.push(`<rect x="${x + 22}" y="${by}" width="${barw}" height="26" rx="6" fill="${LINE}"/>`);
      const fw = barw * Math.min(rate, 100) / 100;
      if (fw > 0) out.push(`<rect x="${x + 22}" y="${by}" width="${fw.toFixed(1)}" height="26" rx="6" fill="${col}"/>`);
      if (rate >= 99.5) {
        out.push(`<rect x="${x + PW - 56}" y="${by + 3}" width="34" height="19" rx="9.5" fill="${col}"/>`);
        out.push(`<text x="${x + PW - 39}" y="${by + 17}" fill="#FFF" font-size="11" font-weight="600" text-anchor="middle">售罄</text>`);
      }
      const lab = `${fmt(p.paid)}/${fmt(p.stock)}`, inside = fw > 78;
      out.push(`<text x="${inside ? x + 32 : (x + 22 + fw + 9).toFixed(1)}" y="${by + 18}" fill="${inside ? "#FFF" : INK}" font-size="12.5" font-weight="600">${lab}</text>`);
      y += 104;
    }
    out.push(`<line x1="${x + 22}" y1="${y - 6}" x2="${x + PW - 22}" y2="${y - 6}" stroke="#DDE1E7"/>`);
    const trate = n.pct;
    out.push(`<text x="${x + 22}" y="${y + 24}" fill="${INK}" font-size="15" font-weight="700">2 点前三票型合计</text>`);
    out.push(`<text x="${x + PW - 22}" y="${y + 24}" fill="${GREEN}" font-size="21" font-weight="700" text-anchor="end">${f1(trate)}%</text>`);
    out.push(`<text x="${x + 22}" y="${y + 44}" fill="${SUB}" font-size="11.5">库存总量 2,250（300+900+1050）· 已售 ${fmt(n.paid)} · 待售 ${fmt(n.left)}</text>`);
    const by2 = y + 52;
    out.push(`<rect x="${x + 22}" y="${by2}" width="${barw}" height="26" rx="6" fill="${LINE}"/>`);
    out.push(`<rect x="${x + 22}" y="${by2}" width="${(barw * trate / 100).toFixed(1)}" height="26" rx="6" fill="${GREEN}"/>`);
    const lab2 = `${fmt(n.paid)}/2,250`, fw2 = barw * trate / 100;
    out.push(`<text x="${fw2 > 78 ? x + 32 : (x + 22 + fw2 + 9).toFixed(1)}" y="${by2 + 18}" fill="${fw2 > 78 ? "#FFF" : INK}" font-size="12.5" font-weight="600">${lab2}</text>`);
    if (showLate) {
      const la = D.late[k], ly = by2 + 52;
      out.push(`<line x1="${x + 22}" y1="${ly - 12}" x2="${x + PW - 22}" y2="${ly - 12}" stroke="#DDE1E7" stroke-dasharray="3 4"/>`);
      out.push(`<text x="${x + 22}" y="${ly + 4}" fill="${INK}" font-size="15" font-weight="600">🌙 晚场票</text>`);
      out.push(`<text x="${x + 22 + tw("🌙 晚场票", 15, true) + 10}" y="${ly + 4}" fill="${FAINT}" font-size="11.5">不限量 · 不计入合计</text>`);
      out.push(`<text x="${x + PW - 22}" y="${ly + 6}" fill="${ORANGE}" font-size="21" font-weight="700" text-anchor="end">${fmt(la.paid)} 张</text>`);
      out.push(`<text x="${x + PW - 22}" y="${ly + 26}" fill="${SUB}" font-size="11.5" text-anchor="end">已售（无库存上限）</text>`);
    }
    out.push("</svg>");
    panels.push(out.join("\n"));
  }
  return '<div class="panels">' + panels.map(p => `<div class="panel">${p}</div>`).join("") + "</div>";
}

function figCompare(D, mobile) {
  const W = mobile ? 560 : 1132, H = 300;
  const x0 = mobile ? 46 : 66, x1 = W - 24, ytop = 56, ybot = 232;
  const bw = mobile ? 30 : 54, fs = mobile ? 10.5 : 12;
  const out = [`<svg viewBox="0 0 ${W} ${H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,'PingFang SC',Arial,sans-serif">`];
  for (let v = 0; v <= 100; v += 20) {
    const yy = ybot - (ybot - ytop) * v / 100;
    out.push(`<line x1="${x0}" y1="${yy.toFixed(1)}" x2="${x1}" y2="${yy.toFixed(1)}" stroke="${LINE}" ${v ? 'stroke-dasharray="3 4"' : ""}/>`);
    out.push(`<text x="${x0 - 8}" y="${(yy + 4).toFixed(1)}" fill="${FAINT}" font-size="10" text-anchor="end">${v}%</text>`);
  }
  const cats = ["超级早鸟", "早场", "正价", "合计"];
  const vals = {};
  for (const k of ["10.30", "10.31"]) vals[k] = [...["超早", "早场", "正价"].map(t => D.nights[k].per[t].pct), D.nights[k].pct];
  const slot = (x1 - x0) / cats.length;
  cats.forEach((c, i) => {
    const cx = x0 + slot * (i + 0.5);
    ["10.30", "10.31"].forEach((k, j) => {
      const v = vals[k][i], h = (ybot - ytop) * Math.min(v, 100) / 100;
      const bx = cx - bw - 4 + j * (bw + 8), col = k === "10.30" ? BLUE : ORANGE;
      out.push(`<rect x="${bx.toFixed(1)}" y="${(ybot - h).toFixed(1)}" width="${bw}" height="${Math.max(h, 1).toFixed(1)}" rx="4" fill="${col}" fill-opacity="${k === "10.31" ? "1" : "0.45"}"/>`);
      out.push(`<text x="${(bx + bw / 2).toFixed(1)}" y="${(ybot - h - 7).toFixed(1)}" fill="${k === "10.31" ? col : SUB}" font-size="${fs}" font-weight="700" text-anchor="middle">${Math.round(v)}%</text>`);
    });
    out.push(`<text x="${cx.toFixed(1)}" y="${ybot + 24}" fill="${INK}" font-size="${fs + 0.5}" text-anchor="middle">${c}</text>`);
  });
  let lx = x0;
  for (const [k, txt] of [["10.30", "10.30 周五"], ["10.31", "10.31 周六"]]) {
    out.push(`<rect x="${lx}" y="22" width="10" height="10" rx="2" fill="${k === "10.30" ? BLUE : ORANGE}" fill-opacity="${k === "10.31" ? "1" : "0.45"}"/>`);
    out.push(`<text x="${lx + 16}" y="31" fill="${SUB}" font-size="11">${txt}</text>`);
    lx += 16 + tw(txt, 11) + 20;
  }
  out.push("</svg>");
  return out.join("\n");
}

function figForecast(D, mobile) {
  let W, H, x0, x1, ytop, ybot;
  if (mobile) { W = 560; H = 400; x0 = 44; x1 = W - 16; ytop = 46; ybot = 318; }
  else { W = 1132; H = 356; x0 = 70; x1 = W - 190; ytop = 40; ybot = 280; }
  const ymax = 2600, gstep = mobile ? 650 : 400;
  const dates = D.dates, cum = D.cum, today = D.today;
  const future = [];
  let d = new Date(dates[dates.length - 1] + "T00:00:00Z").getTime() + 86400e3;
  while (iso(new Date(d)) <= "2026-11-01") { future.push(iso(new Date(d))); d += 86400e3; }
  const datesExt = dates.concat(future);
  const n = datesExt.length, step = (x1 - x0) / (n - 1);
  const X = i => x0 + step * i, Y = v => ybot - (ybot - ytop) * v / ymax;
  const fs = mobile ? 10.5 : 12;
  const out = [`<svg viewBox="0 0 ${W} ${H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,'PingFang SC',Arial,sans-serif">`];
  for (let v = 0; v <= ymax; v += gstep) {
    out.push(`<line x1="${x0}" y1="${Y(v).toFixed(1)}" x2="${x1}" y2="${Y(v).toFixed(1)}" stroke="${LINE}" ${v ? 'stroke-dasharray="3 4"' : ""}/>`);
    out.push(`<text x="${x0 - 6}" y="${(Y(v) + 3.5).toFixed(1)}" fill="${FAINT}" font-size="10" text-anchor="end">${fmt(v)}</text>`);
  }
  out.push(`<line x1="${x0}" y1="${Y(2250).toFixed(1)}" x2="${x1}" y2="${Y(2250).toFixed(1)}" stroke="${AMBER}" stroke-width="1.4" stroke-dasharray="6 5"/>`);
  out.push(`<text x="${x0 + 6}" y="${(Y(2250) - 6).toFixed(1)}" fill="${AMBER}" font-size="10.5" font-weight="600">库存上限 2,250</text>`);
  const labelEvery = mobile ? 3 : 2;
  datesExt.forEach((dt, i) => {
    if (i % labelEvery === 0 || i >= n - 2) {
      const lab = dt.slice(5).replace("-", "/") + (dt === today ? (mobile ? "（今）" : "（今天）") : "");
      out.push(`<text x="${X(i).toFixed(1)}" y="${ybot + 20}" fill="${FAINT}" font-size="${fs - 1.5}" text-anchor="middle">${lab}</text>`);
    }
  });
  const iToday = datesExt.indexOf(today);
  if (iToday >= 0) {
    out.push(`<rect x="${(X(iToday) - step / 2).toFixed(1)}" y="${ytop}" width="${(x1 - X(iToday) + step / 2).toFixed(1)}" height="${ybot - ytop}" fill="#FDF8E8"/>`);
    out.push(`<text x="${(X(iToday) - step / 2 + 5).toFixed(1)}" y="${ybot - 6}" fill="#B08400" font-size="10">预测区</text>`);
  }
  for (const k of ["10.30", "10.31"]) {
    const col = k === "10.30" ? BLUE : ORANGE;
    const pts = cum[k].map((v, i) => [X(i), Y(v)]);
    if (!pts.length) continue;
    out.push(`<path d="M ${pts.map(p => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L ")}" fill="none" stroke="${col}" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>`);
    const [lx2, ly2] = pts[pts.length - 1];
    out.push(`<circle cx="${lx2.toFixed(1)}" cy="${ly2.toFixed(1)}" r="4.5" fill="#FFF" stroke="${col}" stroke-width="2.5"/>`);
    const lab = mobile ? `${k} 已售 ${fmt(cum[k][cum[k].length - 1])}` : `${k} 场 · 已售 ${fmt(cum[k][cum[k].length - 1])}`;
    out.push(`<text x="${(lx2 - 8).toFixed(1)}" y="${(ly2 - 12).toFixed(1)}" fill="${col}" font-size="${fs}" font-weight="700" text-anchor="end">${lab}</text>`);
  }
  const fc = D.forecast || {};
  if (iToday >= 0 && fc["10.31"]) {
    const f31 = fc["10.31"];
    const xs = X(iToday), ys = Y(cum["10.31"][cum["10.31"].length - 1]);
    const drawHit = (fx, lab) => {
      out.push(`<path d="M ${xs.toFixed(1)} ${ys.toFixed(1)} L ${fx.toFixed(1)} ${Y(2250).toFixed(1)}" fill="none" stroke="${ORANGE}" stroke-width="2.2" stroke-dasharray="6 5"/>`);
      out.push(`<circle cx="${fx.toFixed(1)}" cy="${Y(2250).toFixed(1)}" r="6" fill="${ORANGE}"/><text x="${fx.toFixed(1)}" y="${(Y(2250) + 3.5).toFixed(1)}" fill="#FFF" font-size="10" font-weight="700" text-anchor="middle">罄</text>`);
      out.push(`<text x="${(fx - 8).toFixed(1)}" y="${(Y(2250) - 22).toFixed(1)}" fill="${ORANGE}" font-size="${fs}" font-weight="700" text-anchor="end">${lab}</text>`);
    };
    if (f31.sellout) {
      const h = f31.full_hour;
      drawHit(X(iToday) + step * Math.min(h, 36) / 24,
        mobile ? `10.31场 ${hh(h)}售罄` : `10.31 场 · 预计 ${hh(h)} 售罄`);
    } else if (f31.final_est >= 2250) {
      // 今日(T-1)卖不完，但需求外推超库存 → 明日(T-0)必售罄，按 T0 分时估明日钟点
      const soldToday31 = (D.daily["10.31"] || {})[today] || 0;
      const remAfter = D.nights["10.31"].left - Math.max(f31.inc_today - soldToday31, 0);
      const tomorrowInc = Math.max(f31.final_est * SAT_T0, 1);
      if (remAfter <= 0) {
        drawHit(X(iToday) + step * 0.98, "10.31场 今晚尾声售罄");
      } else {
        let hEst = null;
        for (let ht = 6.0; ht <= 24; ht += 0.25) {
          if (profileCum(T0_PROFILE, ht) * tomorrowInc >= remAfter) { hEst = ht; break; }
        }
        if (hEst === null) drawHit(X(iToday) + step * 1.95, "10.31场 明日售罄");
        else drawHit(X(iToday) + step * (24 + Math.min(hEst, 22)) / 24,
          mobile ? `10.31场 明日${String(Math.floor(hEst)).padStart(2, "0")}:00售罄` : `10.31 场 · 预计明日 ${String(Math.floor(hEst)).padStart(2, "0")}:00 前后售罄`);
      }
    } else {
      const fx = X(iToday) + step * 1.5;
      const fy = Y(Math.min(f31.final_est, ymax));
      out.push(`<path d="M ${xs.toFixed(1)} ${ys.toFixed(1)} L ${fx.toFixed(1)} ${fy.toFixed(1)}" fill="none" stroke="${ORANGE}" stroke-width="2.2" stroke-dasharray="6 5"/>`);
      const lab = mobile ? `10.31场 收官~${fmt(f31.final_est)} 不售罄` : `10.31 场 · 预计收官 ~${fmt(f31.final_est)}（${Math.round(f31.final_est / 2250 * 100)}%），不售罄`;
      out.push(`<text x="${(fx - 8).toFixed(1)}" y="${(fy - 10).toFixed(1)}" fill="${ORANGE}" font-size="${fs}" font-weight="700" text-anchor="end">${lab}</text>`);
    }
  }
  if (iToday >= 0 && fc["10.30"]) {
    const f30 = fc["10.30"], xs = X(iToday), ys = Y(cum["10.30"][cum["10.30"].length - 1]);
    if (f30.full_hour && f30.full_hour < 99) {
      const fx = X(iToday) + step * Math.min(f30.full_hour, 24) / 24;
      out.push(`<path d="M ${xs.toFixed(1)} ${ys.toFixed(1)} L ${fx.toFixed(1)} ${Y(2250).toFixed(1)}" fill="none" stroke="${BLUE}" stroke-width="2.2" stroke-dasharray="6 5"/>`);
      out.push(`<circle cx="${fx.toFixed(1)}" cy="${Y(2250).toFixed(1)}" r="6" fill="${BLUE}"/><text x="${fx.toFixed(1)}" y="${(Y(2250) + 3.5).toFixed(1)}" fill="#FFF" font-size="10" font-weight="700" text-anchor="middle">罄</text>`);
      const lab = mobile ? `10.30场 强场景${hh(f30.full_hour)}` : `10.30 场 · 强场景 ${hh(f30.full_hour)} 售罄`;
      out.push(`<text x="${fx.toFixed(1)}" y="${(Y(2250) + 22).toFixed(1)}" fill="${BLUE}" font-size="${fs}" font-weight="700" text-anchor="middle">${lab}</text>`);
    } else {
      const send_ = Math.min(f30.strong_final, 2250);
      const fx = X(iToday) + step * 0.9;
      out.push(`<path d="M ${xs.toFixed(1)} ${ys.toFixed(1)} L ${fx.toFixed(1)} ${Y(send_).toFixed(1)}" fill="none" stroke="${BLUE}" stroke-width="2.2" stroke-dasharray="6 5"/>`);
      const slab = `强场景收官 ~${fmt(send_)}（${Math.round(send_ / 2250 * 100)}%）`;
      if (mobile) out.push(`<text x="${x1 - 2}" y="${(Y(send_) - 8).toFixed(1)}" fill="${BLUE}" font-size="10" text-anchor="end">${slab}</text>`);
      else out.push(`<text x="${(X(iToday) + step * 0.95).toFixed(1)}" y="${(Y(send_) - 8).toFixed(1)}" fill="${BLUE}" font-size="11.5" font-weight="600">${slab}</text>`);
    }
    const wend = Math.min(cum["10.30"][cum["10.30"].length - 1] + f30.weak_today, f30.weak_final);
    out.push(`<path d="M ${xs.toFixed(1)} ${ys.toFixed(1)} L ${(X(iToday) + step * 0.9).toFixed(1)} ${Y(wend).toFixed(1)}" fill="none" stroke="${BLUE}" stroke-width="1.8" stroke-dasharray="2 5" stroke-opacity="0.75"/>`);
    const wlab = `弱场景收官 ~${fmt(wend)}（${Math.round(wend / 2250 * 100)}%）`;
    if (mobile) out.push(`<text x="${x1 - 2}" y="${(Y(wend) + 12).toFixed(1)}" fill="${BLUE}" font-size="10" text-anchor="end">${wlab}</text>`);
    else out.push(`<text x="${(X(iToday) + step * 0.95).toFixed(1)}" y="${(Y(wend) + 14).toFixed(1)}" fill="${BLUE}" font-size="11.5">${wlab}</text>`);
  }
  let lx = x0;
  out.push(`<line x1="${lx}" y1="14" x2="${lx + 20}" y2="14" stroke="${SUB}" stroke-width="2.6"/>`);
  out.push(`<text x="${lx + 26}" y="18" fill="${SUB}" font-size="10.5">实际（截至 ${D.stamp.slice(5)}）</text>`);
  lx += 26 + tw(`实际（截至 ${D.stamp.slice(5)}）`, 10.5) + 18;
  out.push(`<line x1="${lx.toFixed(0)}" y1="14" x2="${(lx + 20).toFixed(0)}" y2="14" stroke="${SUB}" stroke-width="2.2" stroke-dasharray="5 4"/>`);
  out.push(`<text x="${(lx + 26).toFixed(0)}" y="18" fill="${SUB}" font-size="10.5">预测</text>`);
  out.push("</svg>");
  return out.join("\n");
}

function figDaily(D, mobile) {
  const W = mobile ? 560 : 1132, H = 250;
  const x0 = mobile ? 42 : 66, x1 = W - 20, ytop = 34, ybot = 196;
  const bw = mobile ? 18 : 40;
  const last7 = D.dates.slice(-7);
  let ymax = 400;
  for (const k of ["10.30", "10.31"]) for (const d of last7) ymax = Math.max(ymax, (D.daily[k][d] || 0) * 1.2);
  ymax = Math.floor(ymax);
  const out = [`<svg viewBox="0 0 ${W} ${H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="-apple-system,'PingFang SC',Arial,sans-serif">`];
  for (let v = 0; v <= ymax; v += (mobile ? 200 : 100)) {
    const yy = ybot - (ybot - ytop) * v / ymax;
    out.push(`<line x1="${x0}" y1="${yy.toFixed(1)}" x2="${x1}" y2="${yy.toFixed(1)}" stroke="${LINE}" ${v ? 'stroke-dasharray="3 4"' : ""}/>`);
    out.push(`<text x="${x0 - 6}" y="${(yy + 3.5).toFixed(1)}" fill="${FAINT}" font-size="10" text-anchor="end">${v}</text>`);
  }
  const slot = (x1 - x0) / last7.length;
  last7.forEach((dt, i) => {
    const cx = x0 + slot * (i + 0.5);
    ["10.30", "10.31"].forEach((k, j) => {
      const v = D.daily[k][dt] || 0, h = (ybot - ytop) * v / ymax;
      const bx = cx - bw - 3 + j * (bw + 6);
      out.push(`<rect x="${bx.toFixed(1)}" y="${(ybot - h).toFixed(1)}" width="${bw}" height="${Math.max(h, 1).toFixed(1)}" rx="3" fill="${k === "10.30" ? BLUE : ORANGE}" fill-opacity="${k === "10.31" ? "1" : "0.45"}"/>`);
      out.push(`<text x="${(bx + bw / 2).toFixed(1)}" y="${(ybot - h - 4).toFixed(1)}" fill="${SUB}" font-size="9.5" font-weight="600" text-anchor="middle">${fmt(v)}</text>`);
    });
    out.push(`<text x="${cx.toFixed(1)}" y="${ybot + 18}" fill="${FAINT}" font-size="10" text-anchor="middle">${dt.slice(5).replace("-", "/")}</text>`);
  });
  let lx = x0;
  for (const [k, txt] of [["10.30", "10.30 周五"], ["10.31", "10.31 周六"]]) {
    out.push(`<rect x="${lx.toFixed(0)}" y="6" width="9" height="9" rx="2" fill="${k === "10.30" ? BLUE : ORANGE}" fill-opacity="${k === "10.31" ? "1" : "0.45"}"/>`);
    out.push(`<text x="${(lx + 14).toFixed(0)}" y="14" fill="${SUB}" font-size="10.5">${txt}</text>`);
    lx += 14 + tw(txt, 10.5) + 18;
  }
  out.push("</svg>");
  return out.join("\n");
}

function predTable(D) {
  const fc = D.forecast || {}, rows = []; let last = null;
  const confColor = { "高": GREEN, "中高": BLUE, "中": AMBER, "中低": ORANGE, "—": FAINT };
  const entries = [];
  if (fc["10.31"]) {
    const f = fc["10.31"], n = D.nights["10.31"];
    for (const [t, cn] of [["超早", "超级早鸟"], ["早场", "早场票"], ["正价", "正价票"]]) {
      const p = n.per[t];
      if (p.left <= 0) entries.push(["10.31 万圣夜", cn, "0", "已售罄", "—", "退票回流是唯一余量来源"]);
      else {
        const h = f.hours[t];
        entries.push(["10.31 万圣夜", cn, fmt(p.left), hh(h), (h && h > 0 && h < 99) ? "高" : "中",
          `按 T-1 需求 ${Math.round(f.inc_today)} 张 + 溢出顺移（超早→早场→正价）`]);
      }
    }
    entries.push(["10.31 万圣夜", "全场合计", fmt(n.left),
      f.sellout ? `预计 ${hh(f.full_hour)} 售罄，10.31 当天无票` : "预计有剩票",
      f.sellout ? "高" : "中",
      `T-2 末累计 ${fmt(n.paid)} 张，需求外推 ~${fmt(f.final_est)} ≫ 2,250`]);
  }
  if (fc["10.30"]) {
    const f = fc["10.30"], n = D.nights["10.30"];
    for (const [t, cn] of [["超早", "超级早鸟"], ["早场", "早场票"], ["正价", "正价票"]]) {
      const p = n.per[t];
      if (p.left <= 0) entries.push(["10.30 周五", cn, "0", "已售罄", "—", ""]);
      else {
        const h = f.hours[t];
        entries.push(["10.30 周五", cn, fmt(p.left), hh(h),
          t === "超早" ? "高" : (t === "早场" ? "中" : "中低"),
          `强场景当天总量 ~${fmt(f.strong_today)} 张（当天占比≈74%）；弱场景剩票`]);
      }
    }
    const when30 = (f.full_hour && f.full_hour < 99)
      ? `强场景：${hh(f.full_hour)} 售罄；弱场景：收官 ~${fmt(f.weak_final)}（${Math.round(f.weak_final / 2250 * 100)}%）`
      : `按当前卖速收官 ~${fmt(f.strong_final)}（${Math.round(f.strong_final / 2250 * 100)}%），预计不售罄`;
    entries.push(["10.30 周五", "全场合计", fmt(n.left), when30, "中",
      "取决于当天购买占比：普通周五 71%–76% → 售罄；假日型提前预订 → 不售罄"]);
  }
  for (const [sess, typ, left, when, conf, why] of entries) {
    const sessTd = sess !== last ? `<td class='psess'>${sess}</td>` : "<td></td>";
    last = sess;
    const cc = confColor[conf];
    rows.push(`<tr>${sessTd}<td><b>${typ}</b></td><td class='num'>${left}</td><td>${when}</td><td><span class='conf' style='color:${cc};background:${cc}14'>${conf}</span></td><td class='why'>${why}</td></tr>`);
  }
  return `<table class="predt"><thead><tr><th>场次</th><th>票型</th><th class="num">当前余量</th><th>预计售罄时间</th><th>置信度</th><th>判断依据</th></tr></thead><tbody>${rows.join("\n")}</tbody></table>`;
}

function singleTable(D) {
  const s = D.single31 || {}, per = s.per || {}, pd = s.per_delta || {};
  const rows = [];
  const entries = Object.entries(per).sort((a, b) => b[1] - a[1]);
  for (const [m, v] of entries) {
    const d = pd[m] || 0;
    const dtxt = d > 0 ? `<b style='color:${GREEN}'>+${fmt(d)}</b>` : (d < 0 ? `<span style='color:${ORANGE}'>${fmt(d)}</span>` : "—");
    rows.push(`<tr><td>${m}</td><td class='num strong'>${fmt(v)}</td><td class='num'>${dtxt}</td></tr>`);
  }
  const td = s.delta || 0;
  const tdtxt = td > 0 ? `<b style='color:${GREEN}'>+${fmt(td)}</b>` : "—";
  rows.push(`<tr class='tot'><td>合计（${entries.length} 家店）</td><td class='num strong'>${fmt(s.total || 0)}</td><td class='num'>${tdtxt}</td></tr>`);
  return `<table><thead><tr><th>门店</th><th class="num">已售（张）</th><th class="num">较上轮</th></tr></thead><tbody>${rows.join("\n")}</tbody></table>`;
}

function detailTable(D) {
  const rows = [];
  const names = { "超早": "超级早鸟票", "早场": "早场票", "正价": "正价票（含嘉年华会员）" };
  for (const k of ["10.30", "10.31"]) {
    const n = D.nights[k], col = k === "10.30" ? BLUE : ORANGE;
    ["超早", "早场", "正价"].forEach((t, i) => {
      const p = n.per[t];
      rows.push(`<tr><td>${i === 0 ? k : ""}</td><td>${names[t]}</td><td class='num'>${fmt(p.stock)}</td><td class='num strong'>${fmt(p.paid)}</td><td class='num'>${fmt(p.left)}</td><td class='pctcell'><b style='color:${col}'>${f1(p.pct)}%</b><div class='mbar'><span style='width:${f1(Math.min(p.pct, 100))}%;background:${col}'></span></div></td><td class='num'>${fmt(p.order)}</td><td class='num'>${fmt(p.refund)}</td><td class='num'>${f1(pct(p.refund, p.order))}%</td></tr>`);
    });
    const so = ["超早", "早场", "正价"].reduce((s, t) => s + n.per[t].order, 0);
    const sr = ["超早", "早场", "正价"].reduce((s, t) => s + n.per[t].refund, 0);
    rows.push(`<tr class='tot'><td>${k}</td><td>2 点前三票型合计</td><td class='num'>2,250</td><td class='num strong'>${fmt(n.paid)}</td><td class='num'>${fmt(n.left)}</td><td class='pctcell'><b style='color:${GREEN}'>${f1(n.pct)}%</b><div class='mbar'><span style='width:${f1(Math.min(n.pct, 100))}%;background:${GREEN}'></span></div></td><td class='num'>${fmt(so)}</td><td class='num'>${fmt(sr)}</td><td class='num'>${f1(pct(sr, so))}%</td></tr>`);
    if (D.late && D.late[k] && D.late[k].show) {
      const la = D.late[k];
      rows.push(`<tr><td></td><td>🌙 晚场票（不限量）</td><td class='num'>不限量</td><td class='num strong'>${fmt(la.paid)}</td><td class='num'>—</td><td class='pctcell'><b style='color:${ORANGE}'>—</b><div class='mbar'><span style='width:0%'></span></div></td><td class='num'>${fmt(la.order)}</td><td class='num'>${fmt(la.refund)}</td><td class='num'>${f1(pct(la.refund, la.order))}%</td></tr>`);
    }
  }
  return `<table><thead><tr><th>场次</th><th>票型</th><th class="num">库存</th><th class="num">已售</th><th class="num">待售</th><th>库存消耗进度</th><th class="num">下单</th><th class="num">退款</th><th class="num">退款率</th></tr></thead><tbody>${rows.join("\n")}</tbody></table>`;
}

const CSS = `
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
`;

function render(D) {
  const fc = D.forecast || {};
  const h31 = fc["10.31"] ? fc["10.31"].full_hour : null;
  const h30 = fc["10.30"] ? fc["10.30"].full_hour : null;
  let hint2 = "用户以当天购入为主：近 4 周普通周五当天购买占 <b>71%–76%</b>，普通周六 T-1 日占 ~31%、T-0 占 ~29%。";
  if (h31 && h31 < 99) hint2 += ` 据此外推：<b style='color:${ORANGE}'>10.31 场预计 ${hh(h31)} 售罄，当天无票可售</b>；`;
  if (h30 && h30 < 99) hint2 += `<b style='color:${BLUE}'>10.30 场强场景下 ${hh(h30)} 售罄</b>（弱场景收官约 ${Math.round(fc["10.30"].weak_final / 2250 * 100)}%）。`;
  else if (fc["10.30"]) {
    const f30 = fc["10.30"];
    hint2 += `<b style='color:${BLUE}'>10.30 场按当前卖速预计收官 ~${fmt(f30.strong_final)}（${Math.round(f30.strong_final / 2250 * 100)}%），不售罄</b>。`;
  }
  let html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
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
<b>④ 销售窗口</b>：场次票仅在当天售卖，并延续至自然日次日 0 点后（sale_dt 次日计入该场 T-0）；超早 21:00 / 早场 22:00 停售（预测中到点未罄标记为「停售」，需求顺延至下一票型）；晚场票不限量、按场次各自单列、不计入合计。<br>
<b>⑤ 预测模型</b>：9/29–10/29 共 13 个已结束场次的分销售日累计占比经验外推（普通周五当天 71%–76%；普通周六 T-1 ~31% / T-0 ~29%）；含票型溢出顺移与当天分时曲线。<br>
<b>⑥ 数据源</b>：Superset 看板 27，数据集 53，看板缓存 5 分钟；本页每小时自动生成（Cloudflare Worker）。</div></div>
<div class="foot">INS LAND · 万圣节票务库存监测 · 生成于 __STAMP__</div></div></body></html>`;
  html = html.replace("__CSS__", CSS).replace(/__STAMP__/g, D.stamp).replace("__HINT2__", hint2);
  html = html.replace("__FIG1__", figProgress(D));
  for (const [tag, fn] of [["__FIG2__", figCompare], ["__FIG3__", figForecast], ["__FIG4__", figDaily]]) {
    html = html.replace(tag, `<div class="only-desktop">${fn(D, false)}</div><div class="only-mobile">${fn(D, true)}</div>`);
  }
  html = html.replace("__TABLE__", `<div class="table-wrap">${detailTable(D)}</div>`);
  html = html.replace("__PRED__", `<div class="table-wrap">${predTable(D)}</div>`);
  html = html.replace("__SINGLE__", `<div class="table-wrap">${singleTable(D)}</div>`);
  return html;
}

async function sendFeishu(env, D) {
  const hook = (env.FEISHU_WEBHOOK || "").trim();
  if (!hook) return;
  const pages = (env.PAGES_URL || "").trim();
  const lines = [], alerts = [];
  // 14:00 起卡片只推 10.31 场（10.30 场已收官；看板链接里仍保留双场数据）
  {
    const k = "10.31", n = D.nights[k], lab = "10.31 周六·万圣夜";
    const dd = (D.deltas || {})[k];
    const inc = dd ? `　⏫ 较上小时 **+${fmt(dd.total)}**` : "";
    lines.push(`**${lab}** 合计 ${fmt(n.paid)}/2,250（${f1(n.pct)}%）${inc}`);
    for (const t of ["超早", "早场", "正价"]) {
      const p = n.per[t];
      const tag = p.left <= 0 ? "❗已售罄" : (p.left <= 250 ? `⚠️仅剩 ${fmt(p.left)}` : `余 ${fmt(p.left)}`);
      const d1 = dd && dd[t] ? `（+${fmt(dd[t])}）` : "";
      lines.push(`　· ${t}：${fmt(p.paid)}/${fmt(p.stock)}（${f1(p.pct)}%）${tag}${d1}`);
      if (p.left > 0 && p.left <= 250) alerts.push(`${k} ${t} 仅剩 ${fmt(p.left)} 张`);
    }
    if (D.late && D.late[k] && D.late[k].show) {
      const la = D.late[k], dl = la.delta ? `（+${fmt(la.delta)}）` : "";
      lines.push(`　· 🌙 晚场票（不限量）：已售 ${fmt(la.paid)} 张${dl}`);
    }
    const f = (D.forecast || {})[k];
    if (f) {
      const hs = f.hours || {};
      const seg = ["超早", "早场", "正价"].filter(t => n.per[t].left > 0).map(t => `${t} ${hh(hs[t])}`).join(" / ");
      if (f.sellout) {
        lines.push(`　📈 预测：${hh(f.full_hour)} 全场售罄（${seg}）`);
      } else {
        const soldSeg = ["超早", "早场", "正价"].filter(t => n.per[t].left > 0 && hs[t] && hs[t] > 0 && hs[t] < 99).map(t => `${t} ${hh(hs[t])} 售罄`).join("、");
        const tail = soldSeg ? "；" + soldSeg : "";
        lines.push(`　📈 预测：按当前卖速收官 ~${fmt(f.final_est)}（${Math.round(f.final_est / 2250 * 100)}%）预计不售罄${tail}`);
      }
    }
  }
  // 10.31 单店票（剔除 HERO DOME / SOUL HOUSE / 复兴公园游乐场）
  const s = D.single31 || {};
  if (s.per && Object.keys(s.per).length) {
    lines.push("");
    lines.push("— — — — — — — — — —");
    const d1 = s.delta ? `　⏫ 较上小时 **+${fmt(s.delta)}**` : "";
    lines.push(`**10.31 单店票** 合计 ${fmt(s.total)} 张（${Object.keys(s.per).length} 家店）${d1}`);
    const pd = s.per_delta || {};
    const shopSeg = Object.entries(s.per).sort((a, b) => b[1] - a[1])
      .map(([m, v]) => `${m} ${fmt(v)}${pd[m] ? `（+${fmt(pd[m])}）` : ""}`).join(" · ");
    lines.push("　· " + shopSeg);
  }
  let text = lines.join("\n");
  if (alerts.length) text = "🚨 " + alerts.join("；") + "\n\n" + text;
  const elements = [{ tag: "markdown", content: text }];
  if (pages) {
    const bust = pages.replace(/\/$/, "") + "/?v=202610301004";
    elements.push({ tag: "action", actions: [{ tag: "button", text: { tag: "plain_text", content: "📊 打开完整看板（含预测曲线）" }, url: bust, type: "primary" }] });
  }
  const note = "已售=支付成功(扣退款)；嘉年华会员并入正价；不含City Drink/参团/员工票；销售延续至次日0点后；超早21:00/早场22:00停售；单店票不含HERO DOME/SOUL HOUSE/复兴公园游乐场；晚场票不限量、不计入合计";
  elements.push({ tag: "note", elements: [{ tag: "plain_text", content: note }] });
  const card = { msg_type: "interactive", card: { header: { title: { tag: "plain_text", content: "🎃 万圣节库存监控 · " + D.stamp }, template: alerts.length ? "red" : "orange" }, elements } };
  const r = await fetch(hook, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(card) });
  return r.status;
}

// ---------- GitHub Pages 发布 ----------
function b64(s) {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  return btoa(bin);
}
async function ghPut(env, path, content, message) {
  const repo = env.GH_REPO || "kzhang49/halloween-monitor";
  const headers = { "Authorization": "Bearer " + env.GH_TOKEN, "Accept": "application/vnd.github+json", "User-Agent": "halloween-monitor-worker" };
  let sha;
  const g = await fetch(`https://api.github.com/repos/${repo}/contents/${path}?ref=gh-pages`, { headers });
  if (g.ok) sha = (await g.json()).sha;
  const body = { message, content: b64(content), branch: "gh-pages" };
  if (sha) body.sha = sha;
  const r = await fetch(`https://api.github.com/repos/${repo}/contents/${path}`, { method: "PUT", headers, body: JSON.stringify(body) });
  if (!r.ok) throw new Error("gh put " + path + " -> " + r.status + " " + (await r.text()).slice(0, 200));
  return true;
}

// ---------- 主流程 ----------
async function run(env, force) {
  const now = bjNow();
  const today = iso(now), stamp = stampOf(now);
  const prev = env.PAGES_URL ? await fetchPrev(env.PAGES_URL) : null;
  if (!force && prev && prev.stamp) {
    const age = (Date.now() - parseStamp(prev.stamp)) / 60000;
    if (age < 40) return { skipped: true, age };
  }
  const { agg, daily, late, single } = await loadData();
  const D = { stamp, today, nights: {}, deltas: {}, daily };
  // 晚场票：按场次各自单列（不限量、不进 2,250 合计；常显，以防早开）
  const prevLateAll = (prev && prev.late) || { "10.30": (prev && prev.late30) || {} };
  D.late = {};
  for (const k of ["10.30", "10.31"]) {
    const pl = prevLateAll[k] || {};
    D.late[k] = { paid: late[k].paid, order: late[k].order, refund: late[k].refund,
      show: true,
      delta: pl.paid !== undefined ? late[k].paid - pl.paid : 0 };
  }
  // 10.31 单店票（按门店，剔除指定三店）
  const prevSingle = (prev && prev.single31) || {};
  const prevPer = prevSingle.per || {};
  const singleTotal = Object.values(single).reduce((a, b) => a + b, 0);
  const perDelta = {};
  for (const [m, v] of Object.entries(single)) perDelta[m] = v - (prevPer[m] || 0);
  D.single31 = { total: singleTotal, per: single,
    delta: prevSingle.total !== undefined ? singleTotal - prevSingle.total : 0,
    per_delta: perDelta };
  for (const night of ["10.30", "10.31"]) {
    const per = {};
    for (const t of ["超早", "早场", "正价"]) {
      const a = agg[night + "|" + t] || { order: 0, paid: 0, refund: 0 };
      per[t] = { order: a.order, paid: a.paid, refund: a.refund, stock: STOCK[t], left: STOCK[t] - a.paid, pct: pct(a.paid, STOCK[t]) };
    }
    const tot = per["超早"].paid + per["早场"].paid + per["正价"].paid;
    D.nights[night] = { per, paid: tot, stock: 2250, left: 2250 - tot, pct: pct(tot, 2250) };
  }
  if (prev && prev.nights) {
    for (const night of ["10.30", "10.31"]) {
      const pn = prev.nights[night];
      if (!pn) continue;
      const dd = {};
      for (const t of ["超早", "早场", "正价"]) dd[t] = D.nights[night].per[t].paid - ((pn.per || {})[t] ? pn.per[t].paid : 0);
      dd.total = D.nights[night].paid - pn.paid;
      D.deltas[night] = dd;
    }
  }
  const dates = [...new Set([].concat(Object.keys(daily["10.30"]), Object.keys(daily["10.31"])))].sort();
  const cum = { "10.30": [], "10.31": [] };
  const acc = { "10.30": 0, "10.31": 0 };
  for (const dt of dates) for (const k of ["10.30", "10.31"]) { acc[k] += daily[k][dt] || 0; cum[k].push(acc[k]); }
  D.dates = dates; D.cum = cum;

  // ---------- 预测 ----------
  const ev30 = "2026-10-30", ev31 = "2026-10-31";
  const sold30 = D.nights["10.30"].paid, sold31 = D.nights["10.31"].paid;
  const t30Today = daily["10.30"][today] || 0, t31Today = daily["10.31"][today] || 0;
  const hourF = now.getUTCHours() + now.getUTCMinutes() / 60;
  function paceRatio(soldToday, estToday, profile) {
    if (hourF < 12) return 1.0;
    const exp = estToday * profileCum(profile, hourF);
    if (exp < 30 || soldToday <= 0) return 1.0;
    return Math.min(Math.max(soldToday / exp, 0.6), 1.8);
  }
  const fc = {};
  if (today <= ev30) {
    const past30 = sold30 - (today === ev30 ? t30Today : 0);
    let strongFinal = past30 > 0 ? past30 / (1 - FRI_T0) : sold30;
    let strongToday = Math.max(strongFinal * FRI_T0, t30Today);
    if (today === ev30) {
      strongToday *= paceRatio(t30Today, strongToday, T0_PROFILE);
      strongFinal = past30 + strongToday;
    }
    const weakFinal = past30 > 0 ? past30 / (1 - HOLIDAY_T0) : sold30;
    const weakToday = Math.max(weakFinal * HOLIDAY_T0, t30Today);
    const lefts = {}; for (const t of ["超早", "早场", "正价"]) lefts[t] = D.nights["10.30"].per[t].left;
    const strongH = cascade(lefts, strongToday, t30Today, T0_PROFILE);
    const strongFull = hourOf(D.nights["10.30"].left, strongToday, t30Today, T0_PROFILE);
    fc["10.30"] = { scenario: strongFull && strongFull < 99 ? "strong" : "weak",
      strong_final: strongFinal, strong_today: strongToday, weak_final: weakFinal, weak_today: weakToday,
      hours: strongH, full_hour: strongFull };
  }
  if (today <= ev31) {
    let final31, incToday, prof;
    if (today === ev31) {
      const past = sold31 - t31Today;
      final31 = past / (1 - SAT_T0);
      incToday = Math.max(final31 * SAT_T0, t31Today);
      prof = T0_PROFILE;
    } else if (today === ev30) {
      const past = sold31 - t31Today;
      final31 = past / (1 - SAT_T1 - SAT_T0);
      incToday = Math.max(final31 * SAT_T1, t31Today);
      prof = T1_PROFILE;
    } else {
      final31 = sold31 / (1 - SAT_T1 - SAT_T0);
      incToday = 0; prof = T1_PROFILE;
    }
    if (incToday > 0) incToday *= paceRatio(t31Today, incToday, prof);
    const lefts31 = {}; for (const t of ["超早", "早场", "正价"]) lefts31[t] = D.nights["10.31"].per[t].left;
    const hours31 = cascade(lefts31, incToday, t31Today, prof);
    const full31 = hourOf(D.nights["10.31"].left, incToday, t31Today, prof);
    fc["10.31"] = { final_est: final31, inc_today: incToday, hours: hours31, full_hour: full31,
      sellout: Boolean(full31 && full31 < 99) };
  }
  D.forecast = fc;

  const html = render(D);
  const latest = JSON.stringify(D);
  if (env.DRY === "1") return { skipped: false, html, latest };
  if (env.GH_TOKEN) {
    await ghPut(env, "index.html", html, "report " + stamp);
    await ghPut(env, "latest.json", latest, "data " + stamp);
  }
  const pushStatus = await sendFeishu(env, D);
  return { skipped: false, stamp, pushStatus };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env, false));
  },
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/run") {
      if ((url.searchParams.get("key") || "") !== (env.RUN_KEY || "")) return new Response("forbidden", { status: 403 });
      ctx.waitUntil(run(env, true));
      return new Response("triggered", { status: 200 });
    }
    return new Response(JSON.stringify({ ok: true, service: "halloween-monitor", bj_time: stampOf(bjNow()) }), { headers: { "Content-Type": "application/json" } });
  },
};
export { run as __run };

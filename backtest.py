# -*- coding: utf-8 -*-
"""
Kiểm tra (backtest) chiến lược tín hiệu vàng trên dữ liệu quá khứ với nhiều bộ thông số.
Chạy: python backtest.py   -> ghi kết quả ra backtest_result.md
Dùng lại các hàm chỉ báo trong gold_bot.py.
"""
import bisect
import itertools
import os
import time
from datetime import datetime, timezone

import gold_bot as g

SPREAD = 0.35          # chi phí mỗi lệnh (USD/oz) – spread + trượt giá ước tính
SPLIT = 0.7            # 70% dữ liệu đầu = giai đoạn "học", 30% cuối = giai đoạn "kiểm chứng"


# ---------------------------- DỮ LIỆU ----------------------------
def td_pages(interval, pages, size=5000):
    """Twelve Data: lấy nhiều trang lùi về quá khứ."""
    import requests
    allb = {}
    end = None
    for p in range(pages):
        params = {"symbol": "XAU/USD", "interval": interval, "outputsize": size,
                  "timezone": "UTC", "order": "asc", "apikey": g.TWELVEDATA_KEY}
        if end:
            params["end_date"] = end
        data = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=60).json()
        vals = data.get("values")
        if not vals:
            print("Twelve Data dừng ở trang", p, data.get("message", ""))
            break
        for v in vals:
            t = int(datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
            allb[t] = {"t": t, "o": float(v["open"]), "h": float(v["high"]),
                       "l": float(v["low"]), "c": float(v["close"])}
        end = vals[0]["datetime"]
        print(interval, "trang", p + 1, "->", len(allb), "nến")
        time.sleep(9)   # gói miễn phí: tối đa 8 lần gọi/phút
    return [allb[k] for k in sorted(allb)]


def load_data():
    data = {}
    if g.TWELVEDATA_KEY:
        src = "Twelve Data XAU/USD (giao ngay)"
        data["M15"] = td_pages("15min", 4)
        data["H1"] = td_pages("1h", 2)
        data["H4"] = td_pages("4h", 1)
    else:
        src = "Yahoo GC=F (vàng tương lai)"
        data["M15"] = g.fetch_yahoo("15m", "60d")
        data["H1"] = g.fetch_yahoo("1h", "730d")
        data["H4"] = g.resample(data["H1"], 240)
    data["D1"] = g.resample(data["H1"], 1440)
    return data, src


# ---------------------------- MÔ PHỎNG ----------------------------
def prepare(ltf, htf):
    closes = [b["c"] for b in ltf]
    ind = {
        "ef": g.ema(closes, 21), "es": g.ema(closes, 50), "et": g.ema(closes, 200),
        "rsi": g.rsi(closes, 14), "atr": g.atr(ltf, 14),
    }
    ind["m"], ind["s"] = g.macd(closes)
    h_closes = [b["c"] for b in htf]
    h_ema = g.ema(h_closes, 200)
    h_starts = [b["t"] for b in htf]
    hu, hd = [], []
    for b in ltf:
        j = bisect.bisect_right(h_starts, b["t"]) - 2
        if j < 0 or h_ema[j] is None:
            hu.append(None); hd.append(None)
        else:
            hu.append(h_closes[j] > h_ema[j]); hd.append(h_closes[j] < h_ema[j])
    ind["hu"], ind["hd"] = hu, hd
    return ind


def run(ltf, ind, p, start, end):
    """Trả về danh sách kết quả từng lệnh tính theo R (1R = khoảng cách SL)."""
    ef, es, et, r, a, m, s = ind["ef"], ind["es"], ind["et"], ind["rsi"], ind["atr"], ind["m"], ind["s"]
    trades = []
    pos, entry, sl, tp, risk = 0, 0, 0, 0, 1
    for i in range(max(start, 1), end):
        b = ltf[i]
        if pos == 1:
            if b["l"] <= sl:
                trades.append(-1 - SPREAD / risk); pos = 0
            elif b["h"] >= tp:
                trades.append((tp - entry - SPREAD) / risk); pos = 0
        elif pos == -1:
            if b["h"] >= sl:
                trades.append(-1 - SPREAD / risk); pos = 0
            elif b["l"] <= tp:
                trades.append((entry - tp - SPREAD) / risk); pos = 0
        if None in (ef[i], es[i], et[i], r[i], a[i], m[i], s[i], m[i - 1], s[i - 1]):
            continue
        if p["htf"] and ind["hu"][i] is None:
            continue
        c = b["c"]
        up = c > et[i] and ef[i] > es[i] and (not p["htf"] or ind["hu"][i])
        dn = c < et[i] and ef[i] < es[i] and (not p["htf"] or ind["hd"][i])
        xu = m[i] > s[i] and m[i - 1] <= s[i - 1]
        xd = m[i] < s[i] and m[i - 1] >= s[i - 1]
        rsi_ok_b = (50 < r[i] < 70) if p["rsi"] else True
        rsi_ok_s = (30 < r[i] < 50) if p["rsi"] else True
        buy = up and xu and rsi_ok_b
        sell = dn and xd and rsi_ok_s
        if buy and pos != 1:
            if pos == -1:
                trades.append((entry - c - SPREAD) / risk)
            risk = a[i] * p["sl"]
            pos, entry, sl, tp = 1, c, c - risk, c + a[i] * p["tp"]
        elif sell and pos != -1:
            if pos == 1:
                trades.append((c - entry - SPREAD) / risk)
            risk = a[i] * p["sl"]
            pos, entry, sl, tp = -1, c, c + risk, c - a[i] * p["tp"]
    return trades


def stats(tr):
    n = len(tr)
    if n == 0:
        return {"n": 0, "wr": 0, "R": 0, "pf": 0, "dd": 0}
    wins = [x for x in tr if x > 0]
    loss = [x for x in tr if x <= 0]
    eq = peak = dd = 0
    for x in tr:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    pf = sum(wins) / -sum(loss) if loss and sum(loss) < 0 else 99
    return {"n": n, "wr": 100.0 * len(wins) / n, "R": sum(tr), "pf": pf, "dd": dd}


# ---------------------------- CHẠY ----------------------------
def main():
    data, src = load_data()
    grid = {"sl": [1.0, 1.5, 2.0], "tp": [1.5, 2.0, 3.0, 4.0], "htf": [True, False], "rsi": [True, False]}
    combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]

    rows = []
    info = []
    for tf, htf_name in (("M15", "H4"), ("H1", "D1")):
        ltf, htf = data[tf], data[htf_name]
        if len(ltf) < 400:
            continue
        ind = prepare(ltf, htf)
        cut = int(len(ltf) * SPLIT)
        d0 = datetime.fromtimestamp(ltf[0]["t"], timezone.utc).strftime("%d/%m/%Y")
        dc = datetime.fromtimestamp(ltf[cut]["t"], timezone.utc).strftime("%d/%m/%Y")
        d1 = datetime.fromtimestamp(ltf[-1]["t"], timezone.utc).strftime("%d/%m/%Y")
        info.append("- %s (lọc xu hướng khung %s): %d nến, %s → %s; kiểm chứng từ %s" % (tf, htf_name, len(ltf), d0, d1, dc))
        for p in combos:
            a = stats(run(ltf, ind, p, 200, cut))
            b = stats(run(ltf, ind, p, cut, len(ltf)))
            full = stats(run(ltf, ind, p, 200, len(ltf)))
            rows.append((tf, p, a, b, full))

    def name(tf, p):
        return "%s SL%.1f TP%.1f %s %s" % (tf, p["sl"], p["tp"], "H-TF" if p["htf"] else "noHTF",
                                         "RSI" if p["rsi"] else "noRSI")

    out = ["# Kết quả backtest chiến lược vàng", "",
           "Chạy lúc: %s UTC" % datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"),
           "Nguồn: %s | chi phí mỗi lệnh: %.2f USD" % (src, SPREAD), ""] + info + ["",
           "R = lời/lỗ tính theo số lần rủi ro (1R = khoảng cách SL). PF = tổng lời / tổng lỗ. DD = sụt giảm lớn nhất (R).", ""]

    def table(title, items):
        out.append("## " + title)
        out.append("")
        out.append("| Bộ thông số | Học: lệnh / thắng% / R | Kiểm chứng: lệnh / thắng% / R / PF | Toàn bộ: R / DD |")
        out.append("|---|---|---|---|")
        for tf, p, a, b, f in items:
            out.append("| %s | %d / %.0f%% / %+.1f | %d / %.0f%% / %+.1f / %.2f | %+.1f / %.1f |" % (
                name(tf, p), a["n"], a["wr"], a["R"], b["n"], b["wr"], b["R"], b["pf"], f["R"], f["dd"]))
        out.append("")

    current = [x for x in rows if x[0] == "M15" and x[1] == {"sl": 1.5, "tp": 3.0, "htf": True, "rsi": True}]
    table("Bộ thông số đang dùng", current)
    # Chọn theo giai đoạn học (có đủ lệnh), xem kết quả ở giai đoạn kiểm chứng
    ranked = sorted([x for x in rows if x[2]["n"] >= 20], key=lambda x: x[2]["R"], reverse=True)
    table("Top 12 bộ tốt nhất ở giai đoạn HỌC (xem có giữ được ở giai đoạn kiểm chứng không)", ranked[:12])
    both = sorted([x for x in rows if x[2]["n"] >= 20 and x[3]["n"] >= 8 and x[2]["R"] > 0 and x[3]["R"] > 0],
                  key=lambda x: min(x[2]["R"] / max(x[2]["n"], 1), x[3]["R"] / max(x[3]["n"], 1)), reverse=True)
    table("Các bộ có lãi ở CẢ HAI giai đoạn (ổn định hơn)", both[:12])

    with open("backtest_result.md", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()

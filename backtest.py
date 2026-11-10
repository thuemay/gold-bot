# -*- coding: utf-8 -*-
"""
Backtest v2 – tìm chiến lược giao dịch HẰNG NGÀY cho vàng.
Thử 4 kiểu chiến lược x 3 khung (M15, M30, H1) x nhiều mức SL/TP.
Chạy: python backtest.py -> ghi kết quả ra backtest_result.md
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


MIN_PER_WEEK = 4.0


# ---------------------------- CHỈ BÁO PHỤ ----------------------------
def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def stdev(vals, n, mean):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        m = mean[i]
        out[i] = (sum((x - m) ** 2 for x in vals[i - n + 1:i + 1]) / n) ** 0.5
    return out


def base_ind(b):
    c = [x["c"] for x in b]
    ind = {"c": c, "e21": g.ema(c, 21), "e50": g.ema(c, 50), "e200": g.ema(c, 200),
           "rsi": g.rsi(c, 14), "atr": g.atr(b, 14)}
    ind["m"], ind["s"] = g.macd(c)
    mid = sma(c, 20)
    sd = stdev(c, 20, mid)
    ind["bbu"] = [None if mid[i] is None else mid[i] + 2 * sd[i] for i in range(len(c))]
    ind["bbl"] = [None if mid[i] is None else mid[i] - 2 * sd[i] for i in range(len(c))]
    ind["bbm"] = mid
    return ind


def ok(*xs):
    return all(x is not None for x in xs)


# ---------------------------- CÁC KIỂU TÍN HIỆU ----------------------------
def sig_macd(b, d, i):
    """Theo xu hướng: EMA21>50, giá > EMA200, MACD cắt lên, RSI 50-70 (bản gốc, bỏ lọc H4)."""
    if not ok(d["e21"][i], d["e50"][i], d["e200"][i], d["m"][i], d["s"][i], d["m"][i-1], d["s"][i-1], d["rsi"][i]):
        return 0
    c = b[i]["c"]
    if c > d["e200"][i] and d["e21"][i] > d["e50"][i] and d["m"][i] > d["s"][i] and d["m"][i-1] <= d["s"][i-1] and 50 < d["rsi"][i] < 70:
        return 1
    if c < d["e200"][i] and d["e21"][i] < d["e50"][i] and d["m"][i] < d["s"][i] and d["m"][i-1] >= d["s"][i-1] and 30 < d["rsi"][i] < 50:
        return -1
    return 0


def sig_pullback(b, d, i):
    """Xu hướng tăng (EMA50>EMA200), giá hồi chạm EMA21 rồi đóng nến tăng trên EMA21 -> MUA."""
    if not ok(d["e21"][i], d["e50"][i], d["e200"][i]):
        return 0
    x = b[i]
    if d["e50"][i] > d["e200"][i] and x["c"] > d["e200"][i] and x["l"] <= d["e21"][i] < x["c"] and x["c"] > x["o"]:
        return 1
    if d["e50"][i] < d["e200"][i] and x["c"] < d["e200"][i] and x["h"] >= d["e21"][i] > x["c"] and x["c"] < x["o"]:
        return -1
    return 0


def sig_meanrev(b, d, i):
    """Đảo chiều: giá thủng dải Bollinger dưới với RSI thấp rồi đóng cửa quay lại trong dải -> MUA."""
    if not ok(d["bbl"][i], d["bbl"][i-1], d["rsi"][i-1]):
        return 0
    if b[i-1]["c"] < d["bbl"][i-1] and b[i]["c"] > d["bbl"][i] and d["rsi"][i-1] < 30:
        return 1
    if b[i-1]["c"] > d["bbu"][i-1] and b[i]["c"] < d["bbu"][i] and d["rsi"][i-1] > 70:
        return -1
    return 0


def make_breakout(bars):
    """Phá vỡ vùng giá phiên Á (00:00-07:00 UTC = 7h-14h VN) trong phiên Âu/Mỹ (07:00-16:00 UTC)."""
    day_hi, day_lo, traded = {}, {}, set()
    for x in bars:
        dt = datetime.fromtimestamp(x["t"], timezone.utc)
        if dt.hour < 7:
            k = dt.date()
            day_hi[k] = max(day_hi.get(k, x["h"]), x["h"])
            day_lo[k] = min(day_lo.get(k, x["l"]), x["l"])

    def sig(b, d, i):
        dt = datetime.fromtimestamp(b[i]["t"], timezone.utc)
        k = dt.date()
        if not (7 <= dt.hour < 16) or k not in day_hi or k in traded:
            return 0
        if b[i]["c"] > day_hi[k]:
            traded.add(k); return 1
        if b[i]["c"] < day_lo[k]:
            traded.add(k); return -1
        return 0
    return sig, traded


# ---------------------------- MÔ PHỎNG ----------------------------
def run(b, d, sigs, p, start, end, eod=False):
    a = d["atr"]
    tr = []
    pos, entry, sl, tp, risk = 0, 0, 0, 0, 1
    for i in range(max(start, 1), end):
        x = b[i]
        if pos == 1:
            if x["l"] <= sl:
                tr.append(-1 - SPREAD / risk); pos = 0
            elif x["h"] >= tp:
                tr.append((tp - entry - SPREAD) / risk); pos = 0
        elif pos == -1:
            if x["h"] >= sl:
                tr.append(-1 - SPREAD / risk); pos = 0
            elif x["l"] <= tp:
                tr.append((entry - tp - SPREAD) / risk); pos = 0
        if pos and eod and datetime.fromtimestamp(x["t"], timezone.utc).hour >= 20:
            tr.append(((x["c"] - entry) * pos - SPREAD) / risk); pos = 0
        sgn = sigs[i]
        if sgn == 0 or a[i] is None or sgn == pos:
            continue
        if pos:
            tr.append(((x["c"] - entry) * pos - SPREAD) / risk)
        risk = a[i] * p["sl"]
        pos, entry = sgn, x["c"]
        sl = entry - sgn * risk
        tp = entry + sgn * a[i] * p["tp"]
    return tr


def stats(tr):
    n = len(tr)
    if not n:
        return {"n": 0, "wr": 0, "R": 0, "pf": 0, "dd": 0, "streak": 0}
    w = [x for x in tr if x > 0]
    l = [x for x in tr if x <= 0]
    eq = pk = dd = 0
    st = mx = 0
    for x in tr:
        eq += x; pk = max(pk, eq); dd = max(dd, pk - eq)
        st = st + 1 if x <= 0 else 0
        mx = max(mx, st)
    pf = sum(w) / -sum(l) if l and sum(l) < 0 else 99
    return {"n": n, "wr": 100.0 * len(w) / n, "R": sum(tr), "pf": pf, "dd": dd, "streak": mx}


def main():
    data, src = load_data()
    frames = {"M15": data["M15"], "M30": g.resample(data["M15"], 30), "H1": data["H1"]}
    grid = [{"sl": s, "tp": t} for s, t in itertools.product([1.0, 1.5, 2.0], [1.0, 1.5, 2.0, 3.0])]
    rows, info = [], []
    for tf, b in frames.items():
        if len(b) < 500:
            continue
        d = base_ind(b)
        cut = int(len(b) * SPLIT)
        weeks = (b[-1]["t"] - b[200]["t"]) / (7 * 86400.0)
        info.append("- %s: %d nến, %s → %s (%.0f tuần), kiểm chứng từ %s" % (
            tf, len(b), datetime.fromtimestamp(b[0]["t"], timezone.utc).strftime("%d/%m/%Y"),
            datetime.fromtimestamp(b[-1]["t"], timezone.utc).strftime("%d/%m/%Y"), weeks,
            datetime.fromtimestamp(b[cut]["t"], timezone.utc).strftime("%d/%m/%Y")))
        strategies = {"Theo xu hướng MACD": (sig_macd, False),
                      "Hồi về EMA21": (sig_pullback, False),
                      "Đảo chiều Bollinger": (sig_meanrev, False)}
        if tf != "H1":
            strategies["Phá vỡ phiên Á"] = (None, True)
        for name, (fn, eod) in strategies.items():
            if fn is None:
                fn, _ = make_breakout(b)
            sigs = [0] + [fn(b, d, i) for i in range(1, len(b))]
            for p in grid:
                full = stats(run(b, d, sigs, p, 200, len(b), eod))
                lrn = stats(run(b, d, sigs, p, 200, cut, eod))
                tst = stats(run(b, d, sigs, p, cut, len(b), eod))
                rows.append({"tf": tf, "name": name, "p": p, "full": full, "lrn": lrn, "tst": tst,
                             "pw": full["n"] / weeks})

    out = ["# Backtest v2 – chiến lược giao dịch hằng ngày", "",
           "Chạy lúc %s UTC | Nguồn: %s | chi phí %.2f USD/lệnh" % (
               datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M"), src, SPREAD), ""] + info + [""]

    def table(title, items):
        out.extend(["## " + title, "",
                    "| Chiến lược | Khung | SL/TP (xATR) | Lệnh/tuần | Học: thắng% / R | Kiểm chứng: lệnh / thắng% / R / PF | Toàn bộ R | DD | Thua liền max |",
                    "|---|---|---|---|---|---|---|---|---|"])
        for r in items:
            f, l, t = r["full"], r["lrn"], r["tst"]
            out.append("| %s | %s | %.1f / %.1f | %.1f | %.0f%% / %+.1f | %d / %.0f%% / %+.1f / %.2f | %+.1f | %.1f | %d |" % (
                r["name"], r["tf"], r["p"]["sl"], r["p"]["tp"], r["pw"], l["wr"], l["R"],
                t["n"], t["wr"], t["R"], t["pf"], f["R"], f["dd"], f["streak"]))
        out.append("")

    daily = [r for r in rows if r["pw"] >= MIN_PER_WEEK]
    good = sorted([r for r in daily if r["lrn"]["R"] > 0 and r["tst"]["R"] > 0],
                  key=lambda r: r["tst"]["pf"], reverse=True)
    table("Có lãi ở CẢ HAI giai đoạn và >= %.0f lệnh/tuần" % MIN_PER_WEEK, good[:15])
    best_each = {}
    for r in sorted(daily, key=lambda r: r["full"]["R"], reverse=True):
        best_each.setdefault((r["name"], r["tf"]), r)
    table("Bộ tốt nhất của từng chiến lược/khung (>= %.0f lệnh/tuần)" % MIN_PER_WEEK,
          sorted(best_each.values(), key=lambda r: r["full"]["R"], reverse=True))

    with open("backtest_result.md", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()

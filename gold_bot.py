# -*- coding: utf-8 -*-
"""
Bot Telegram báo tín hiệu MUA/BÁN vàng (XAUUSD) – chạy song song 2 chiến lược
đã kiểm tra trên dữ liệu quá khứ (backtest 11/2026):
  1) Hồi về EMA21 – khung 1 giờ  (SL 1 x ATR, TP 3 x ATR)
  2) Phá vỡ phiên Á – khung 15 phút (SL 2 x ATR, TP 2 x ATR, đóng lệnh trước 3h sáng VN)

Chạy:            python gold_bot.py
Lấy chat id:     python gold_bot.py --chatid
Chỉ cần thư viện: requests   (pip install requests)
"""
import bisect
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ====================== CẤU HÌNH – SỬA Ở ĐÂY ======================
# Điền ÍT NHẤT MỘT kênh nhận tin. Kênh nào để trống thì bỏ qua.

# 1) Discord (khuyên dùng – không bị chặn ở VN): dán Webhook URL của kênh Discord
DISCORD_WEBHOOK = ""

# 2) Gmail: gửi email cho chính bạn. Cần "Mật khẩu ứng dụng" (App Password) 16 ký tự của Gmail
GMAIL_USER = ""              # ví dụ: tenban@gmail.com
GMAIL_APP_PASSWORD = ""      # mật khẩu ứng dụng, KHÔNG phải mật khẩu đăng nhập
EMAIL_TO = ""                # để trống = gửi về chính GMAIL_USER

# 3) Telegram (bị nhà mạng VN chặn – chỉ dùng nếu máy có VPN)
TELEGRAM_TOKEN = ""
CHAT_ID = ""

# Nguồn giá:
#  - Có key Twelve Data (miễn phí tại twelvedata.com) -> dùng giá vàng giao ngay XAU/USD (khớp sàn nhất)
#  - Để trống -> dùng Yahoo, vàng tương lai GC=F (giá cao hơn giao ngay khoảng vài chục giá)
TWELVEDATA_KEY = ""

# Chiến lược 1: Hồi về EMA21 (khung 1 giờ)
PULLBACK_ON = True
PB_SL, PB_TP = 1.0, 3.0      # SL/TP = số lần ATR(14) khung 1 giờ

# Chiến lược 2: Phá vỡ phiên Á (khung 15 phút) – giờ UTC (VN = UTC + 7)
BREAKOUT_ON = True
BO_SL, BO_TP = 2.0, 2.0      # SL/TP = số lần ATR(14) khung 15 phút
ASIA_END_UTC = 7             # vùng giá phiên Á: 00:00–07:00 UTC (7h–14h VN)
BO_END_UTC = 16              # chỉ vào lệnh phá vỡ đến 16:00 UTC (23h VN)
BO_CLOSE_UTC = 20            # lệnh còn mở thì đóng lúc 20:00 UTC (3h sáng VN)

HEARTBEAT_MIN = 60           # không có tín hiệu thì cứ 60 phút báo tình hình 1 lần (0 = tắt)
TF_MIN = 15                  # nhịp kiểm tra / khung nhỏ: 15 phút
# =================================================================


def _env(name, default):
    v = os.environ.get(name, "").strip()
    return v if v else default


# Khi chạy trên GitHub Actions, thông tin bí mật lấy từ Secrets (biến môi trường)
DISCORD_WEBHOOK = _env("DISCORD_WEBHOOK", DISCORD_WEBHOOK)
GMAIL_USER = _env("GMAIL_USER", GMAIL_USER)
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD)
EMAIL_TO = _env("EMAIL_TO", EMAIL_TO)
TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN", TELEGRAM_TOKEN)
CHAT_ID = _env("CHAT_ID", CHAT_ID)
TWELVEDATA_KEY = _env("TWELVEDATA_KEY", TWELVEDATA_KEY)

VN_TZ = timezone(timedelta(hours=7))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "gold_bot_state.json")
UA = {"User-Agent": "Mozilla/5.0 (gold-signal-bot)"}

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def log(msg):
    print(datetime.now(VN_TZ).strftime("%d/%m %H:%M:%S"), msg, flush=True)


# ------------------------- LẤY DỮ LIỆU -------------------------
def fetch_twelvedata(interval, size):
    r = requests.get("https://api.twelvedata.com/time_series", params={
        "symbol": "XAU/USD", "interval": interval, "outputsize": size,
        "timezone": "UTC", "order": "asc", "apikey": TWELVEDATA_KEY,
    }, timeout=30)
    data = r.json()
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError("Twelve Data lỗi: %s" % data.get("message", data))
    bars = []
    for v in data["values"]:
        t = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        bars.append({"t": int(t.timestamp()), "o": float(v["open"]), "h": float(v["high"]),
                     "l": float(v["low"]), "c": float(v["close"])})
    return bars


def fetch_yahoo(interval, rng):
    last_err = None
    for host in ("query1", "query2", "query1"):
        try:
            r = requests.get("https://%s.finance.yahoo.com/v8/finance/chart/GC=F" % host,
                             params={"interval": interval, "range": rng}, headers=UA, timeout=30)
            res = r.json()["chart"]["result"][0]
            break
        except Exception as ex:
            last_err = ex
            time.sleep(3)
    else:
        raise RuntimeError("Yahoo lỗi: %s" % last_err)
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(res.get("timestamp", [])):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append({"t": int(t), "o": o, "h": h, "l": l, "c": c})
    return bars


def resample(bars, minutes):
    sec = minutes * 60
    out = []
    for b in bars:
        key = b["t"] - b["t"] % sec
        if out and out[-1]["t"] == key:
            last = out[-1]
            last["h"] = max(last["h"], b["h"])
            last["l"] = min(last["l"], b["l"])
            last["c"] = b["c"]
        else:
            out.append({"t": key, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return out


def get_data():
    """Trả về (nến 15 phút, nến 1 giờ, tên nguồn) – gồm cả nến đang chạy."""
    if TWELVEDATA_KEY:
        m15 = fetch_twelvedata("15min", 1500)
        h1 = fetch_twelvedata("1h", 700)
        src = "Twelve Data XAU/USD"
    else:
        m15 = fetch_yahoo("15m", "30d")
        h1 = fetch_yahoo("1h", "60d")
        src = "Yahoo GC=F (vàng tương lai)"
    return m15, h1, src


def split_closed(raw, tf_min=15):
    """Tách nến đã đóng chắc chắn + giá mới nhất.
    Nguồn giá trễ (Yahoo trễ ~10 phút) có thể trả nến cuối chưa đủ dữ liệu,
    nên nến cuối cùng chỉ được tính là đã đóng khi đã qua giờ đóng hơn 2 phút
    VÀ đã có nến mới hơn phía sau."""
    now = time.time()
    live = raw[-1]["c"] if raw else None
    closed = [b for b in raw if b["t"] + tf_min * 60 <= now]
    if closed and closed[-1] is raw[-1]:
        closed = closed[:-1]
    return closed, live


# ------------------------- CHỈ BÁO -------------------------
def _smooth(vals, n, alpha):
    out = [None] * len(vals)
    i0 = next((i for i, v in enumerate(vals) if v is not None), None)
    if i0 is None or len(vals) - i0 < n:
        return out
    s = sum(vals[i0:i0 + n]) / n
    out[i0 + n - 1] = s
    for i in range(i0 + n, len(vals)):
        s = vals[i] * alpha + s * (1 - alpha)
        out[i] = s
    return out


def ema(vals, n):
    return _smooth(vals, n, 2.0 / (n + 1))


def rma(vals, n):
    return _smooth(vals, n, 1.0 / n)


def rsi(closes, n):
    gains, losses = [None], [None]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    g, l = rma(gains, n), rma(losses, n)
    out = []
    for a, b in zip(g, l):
        if a is None or b is None:
            out.append(None)
        elif b == 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + a / b))
    return out


def atr(bars, n):
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return rma(tr, n)


def macd(closes):
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    return line, ema(line, 9)


def cross_up(a, b, i):
    return None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i] > b[i] and a[i - 1] <= b[i - 1]


def cross_down(a, b, i):
    return None not in (a[i], b[i], a[i - 1], b[i - 1]) and a[i] < b[i] and a[i - 1] >= b[i - 1]


# ------------------------- MÔ PHỎNG TÍN HIỆU -------------------------

# ------------------------- TÍN HIỆU -------------------------
def utc_hour(t):
    return datetime.fromtimestamp(t, timezone.utc).hour


def signals_pullback(bars):
    """Xu hướng tăng (EMA50 > EMA200, giá > EMA200): nến chạm EMA21 rồi đóng cửa tăng trên EMA21 -> MUA."""
    c = [b["c"] for b in bars]
    e21, e50, e200 = ema(c, 21), ema(c, 50), ema(c, 200)
    sig = [0] * len(bars)
    for i, x in enumerate(bars):
        if None in (e21[i], e50[i], e200[i]):
            continue
        if e50[i] > e200[i] and x["c"] > e200[i] and x["l"] <= e21[i] < x["c"] and x["c"] > x["o"]:
            sig[i] = 1
        elif e50[i] < e200[i] and x["c"] < e200[i] and x["h"] >= e21[i] > x["c"] and x["c"] < x["o"]:
            sig[i] = -1
    return sig


def signals_breakout(bars):
    """Đóng nến 15 phút vượt đỉnh phiên Á -> MUA, thủng đáy -> BÁN (mỗi ngày tối đa 1 lệnh)."""
    hi, lo, traded = {}, {}, set()
    for x in bars:
        dt = datetime.fromtimestamp(x["t"], timezone.utc)
        if dt.hour < ASIA_END_UTC:
            k = dt.date()
            hi[k] = max(hi.get(k, x["h"]), x["h"])
            lo[k] = min(lo.get(k, x["l"]), x["l"])
    sig = [0] * len(bars)
    for i, x in enumerate(bars):
        dt = datetime.fromtimestamp(x["t"], timezone.utc)
        k = dt.date()
        if not (ASIA_END_UTC <= dt.hour < BO_END_UTC) or k not in hi or k in traded:
            continue
        if x["c"] > hi[k]:
            sig[i] = 1; traded.add(k)
        elif x["c"] < lo[k]:
            sig[i] = -1; traded.add(k)
    return sig


def simulate(bars, sig, slm, tpm, eod=False):
    """Mô phỏng lệnh qua toàn bộ nến -> (danh sách sự kiện, lệnh đang mở)."""
    a = atr(bars, 14)
    ev = []
    pos, entry, sl, tp, et = 0, None, None, None, None
    for i, x in enumerate(bars):
        if pos:
            side = "MUA" if pos == 1 else "BÁN"
            hit_sl = x["l"] <= sl if pos == 1 else x["h"] >= sl
            hit_tp = x["h"] >= tp if pos == 1 else x["l"] <= tp
            if hit_sl:
                ev.append({"type": "LOSS", "i": i, "side": side, "entry": entry, "price": sl, "entry_t": et}); pos = 0
            elif hit_tp:
                ev.append({"type": "WIN", "i": i, "side": side, "entry": entry, "price": tp, "entry_t": et}); pos = 0
            elif eod and utc_hour(x["t"]) >= BO_CLOSE_UTC:
                ev.append({"type": "EOD", "i": i, "side": side, "entry": entry, "price": x["c"], "entry_t": et}); pos = 0
        s = sig[i]
        if s == 0 or s == pos or a[i] is None:
            continue
        rev = pos != 0
        pos, entry, et = s, x["c"], x["t"]
        sl = entry - s * a[i] * slm
        tp = entry + s * a[i] * tpm
        ev.append({"type": "BUY" if s == 1 else "SELL", "i": i, "entry": entry, "sl": sl, "tp": tp,
                   "rev": rev, "entry_t": et})
    return ev, {"pos": pos, "entry": entry, "sl": sl, "tp": tp, "entry_t": et}


def strategies():
    out = []
    if PULLBACK_ON:
        out.append({"key": "PB", "name": "Hồi về EMA21 (H1)", "tf": 60, "src": "h1",
                    "sig": signals_pullback, "sl": PB_SL, "tp": PB_TP, "eod": False})
    if BREAKOUT_ON:
        out.append({"key": "BO", "name": "Phá vỡ phiên Á (M15)", "tf": 15, "src": "m15",
                    "sig": signals_breakout, "sl": BO_SL, "tp": BO_TP, "eod": True})
    return out

def _retry(name, fn):
    for attempt in range(3):
        try:
            fn()
            return True
        except Exception as ex:
            log("Gửi %s lỗi (lần %d): %s" % (name, attempt + 1, ex))
            time.sleep(5)
    return False


def _discord(text):
    r = requests.post(DISCORD_WEBHOOK, json={"content": text}, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(r.text)


def _gmail(text):
    import smtplib
    from email.mime.text import MIMEText
    first = text.split("\n")[0]
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = first[:80]
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO or GMAIL_USER
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD.replace(" ", ""))
        s.send_message(msg)


def _telegram(text):
    url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=30)
    if not r.ok:
        raise RuntimeError(r.text)


def channels():
    ch = []
    if DISCORD_WEBHOOK:
        ch.append(("Discord", _discord))
    if GMAIL_USER and GMAIL_APP_PASSWORD:
        ch.append(("Gmail", _gmail))
    if TELEGRAM_TOKEN and CHAT_ID:
        ch.append(("Telegram", _telegram))
    return ch


def send(text):
    ok = False
    for name, fn in channels():
        ok = _retry(name, lambda: fn(text)) or ok
    return ok


def print_chat_ids():
    r = requests.get("https://api.telegram.org/bot%s/getUpdates" % TELEGRAM_TOKEN, timeout=30).json()
    if not r.get("ok"):
        print("Token sai hoặc lỗi:", r)
        return
    found = {}
    for u in r.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            found[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not found:
        print("Chưa thấy tin nhắn nào. Mở Telegram, nhắn /start cho bot rồi chạy lại lệnh này.")
    for cid, name in found.items():
        print("CHAT_ID =", cid, "  (", name, ")")



def fmt_time(t):
    return datetime.fromtimestamp(t, VN_TZ).strftime("%H:%M %d/%m")


def fmt(x):
    return "%.2f" % x


def event_text(st, e, bars, live):
    t = fmt_time(bars[e["i"]]["t"] + st["tf"] * 60)
    if e["type"] in ("BUY", "SELL"):
        buy = e["type"] == "BUY"
        risk = abs(e["entry"] - e["sl"])
        lines = ["%s VÀNG – %s" % ("🟢 MUA (BUY)" if buy else "🔴 BÁN (SELL)", st["name"]),
                 "Nến đóng lúc %s" % t,
                 "Giá vào: %s" % fmt(e["entry"]),
                 "SL: %s  (cách %s)" % (fmt(e["sl"]), fmt(risk)),
                 "TP: %s  (cách %s)" % (fmt(e["tp"]), fmt(abs(e["tp"] - e["entry"])))]
        if st["eod"]:
            lines.append("Chưa chạm SL/TP thì đóng lệnh trước 3h sáng.")
        if live is not None:
            d = live - e["entry"]
            lines.append("Giá hiện tại: %s (%s%s)" % (fmt(live), "+" if d >= 0 else "", fmt(d)))
            chase = d if buy else -d
            if (buy and live <= e["sl"]) or (not buy and live >= e["sl"]):
                lines.append("⛔ Giá đã vượt SL – BỎ QUA lệnh này.")
            elif chase > 0.5 * risk:
                lines.append("⚠️ Giá đã chạy xa – không đuổi, chờ hồi về gần %s hoặc bỏ qua." % fmt(e["entry"]))
        if e["rev"]:
            lines.append("↩️ Đóng lệnh ngược chiều của chiến lược này (nếu đang giữ).")
        return "\n".join(lines)
    head = {"WIN": "✅ CHẠM TP", "LOSS": "❌ CHẠM SL", "EOD": "🌙 ĐÓNG CUỐI NGÀY"}[e["type"]]
    return "%s – %s\nLệnh %s vào %s, đóng ở %s (%s)" % (
        head, st["name"], e["side"], fmt(e["entry"]), fmt(e["price"]), t)


def pos_line(st, s, show):
    if s["pos"] and show:
        return "• %s: đang %s %s | SL %s | TP %s" % (
            st["name"], "MUA" if s["pos"] == 1 else "BÁN", fmt(s["entry"]), fmt(s["sl"]), fmt(s["tp"]))
    return "• %s: không có lệnh" % st["name"]


def startup_text(src, lines):
    return "\n".join(["📊 Bot tín hiệu vàng đang chạy (2 chiến lược)",
                      "Nguồn giá: %s" % src,
                      "1) Hồi về EMA21 – khung 1 giờ, SL %.1f×ATR, TP %.1f×ATR" % (PB_SL, PB_TP),
                      "2) Phá vỡ phiên Á – khung 15 phút, SL %.1f×ATR, TP %.1f×ATR, đóng trước 3h sáng" % (BO_SL, BO_TP),
                      "Mỗi lệnh chỉ nên chịu lỗ tối đa 0,5–1% vốn."] + lines)


def heartbeat_text(t, live, lines):
    return "\n".join(["⏱ %s – Chưa có điểm vào mới" % fmt_time(t),
                      "Giá hiện tại: %s" % fmt(live)] + lines)

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)



def run_once(startup=False):
    m15_raw, h1_raw, src = get_data()
    data = {"m15": split_closed(m15_raw, 15), "h1": split_closed(h1_raw, 60)}
    live = m15_raw[-1]["c"] if m15_raw else None
    m15 = data["m15"][0]
    if len(m15) < 50:
        log("Chưa đủ dữ liệu.")
        return
    state = load_state()
    new_state = {}
    lines, sent_any, need_startup = [], False, startup

    for st in strategies():
        bars = data[st["src"]][0]
        ss = state.get(st["key"], {})
        if len(bars) < 210:
            log("%s: chưa đủ dữ liệu (%d nến)" % (st["name"], len(bars)))
            new_state[st["key"]] = ss
            continue
        ev, cur = simulate(bars, st["sig"](bars), st["sl"], st["tp"], st["eod"])
        last_t = ss.get("last_t")
        sent = set(ss.get("sent", []))
        if last_t is None:
            need_startup = True
            new = []
            if cur["entry_t"] is not None:
                sent.add(cur["entry_t"])
        else:
            new = [e for e in ev if bars[e["i"]]["t"] > last_t]
        for e in new:
            if e["type"] in ("BUY", "SELL"):
                sent.add(e["entry_t"])
            elif e["entry_t"] not in sent:
                continue      # lệnh chưa từng báo -> bỏ qua
            txt = event_text(st, e, bars, live)
            log(txt.replace("\n", " | "))
            send(txt)
            sent_any = True
        lines.append(pos_line(st, cur, cur["entry_t"] in sent))
        new_state[st["key"]] = {"last_t": bars[-1]["t"], "sent": sorted(sent)[-50:]}

    # Báo định kỳ
    last_hb = state.get("last_hb")
    hb = (m15[-1]["t"] + 15 * 60) // (HEARTBEAT_MIN * 60) if HEARTBEAT_MIN > 0 else None
    if need_startup:
        send(startup_text(src, lines))
        last_hb = hb
    elif sent_any:
        last_hb = hb
    elif hb is not None and hb != last_hb:
        send(heartbeat_text(m15[-1]["t"] + 15 * 60, live, lines))
        last_hb = hb

    new_state["last_hb"] = last_hb
    save_state(new_state)
    log("Đã kiểm tra: giá %s | %s" % (fmt(live), " ; ".join(lines)))


def main():
    if "--chatid" in sys.argv:
        print_chat_ids()
        return
    if not channels():
        print("Chưa có kênh nhận tin. Điền TELEGRAM_TOKEN + CHAT_ID (hoặc Discord/Gmail).")
        return
    log("Kênh nhận tin: %s" % ", ".join(n for n, _ in channels()))
    if "--once" in sys.argv:
        run_once(startup=not os.path.exists(STATE_FILE))
        return
    log("Bot khởi động...")
    try:
        run_once(startup=True)
    except Exception as ex:
        log("Lỗi: %s" % ex)
    period = TF_MIN * 60
    while True:
        now = time.time()
        time.sleep(max(5, (now // period + 1) * period + 30 - now))
        try:
            run_once()
        except Exception as ex:
            log("Lỗi: %s (sẽ thử lại ở nến sau)" % ex)


if __name__ == "__main__":
    main()

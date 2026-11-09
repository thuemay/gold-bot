# -*- coding: utf-8 -*-
"""
Bot Telegram báo tín hiệu MUA/BÁN vàng (XAUUSD) – cùng logic với chỉ báo
"Gold Buy/Sell Signals (XAUUSD)" trên TradingView:
  EMA 21/50/200 + xu hướng khung H4 + MACD cắt Signal + RSI, SL/TP theo ATR.

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

EMA_FAST, EMA_SLOW, EMA_TREND = 21, 50, 200
USE_HTF = True               # lọc theo xu hướng khung lớn (H4)
RSI_LEN, RSI_OB, RSI_OS = 14, 70, 30
ATR_LEN, SL_MULT, TP_MULT = 14, 1.5, 3.0
TF_MIN = 15                  # khung chính: 15 phút
HTF_MIN = 240                # khung lớn: 4 giờ
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
    if TWELVEDATA_KEY:
        ltf = fetch_twelvedata("%dmin" % TF_MIN, 1500)
        htf = fetch_twelvedata("4h", 600)
        src = "Twelve Data XAU/USD"
    else:
        ltf = fetch_yahoo("%dm" % TF_MIN, "30d")
        htf = resample(fetch_yahoo("1h", "200d"), HTF_MIN)
        src = "Yahoo GC=F (vàng tương lai)"
    # Bỏ nến chưa đóng
    now = time.time()
    ltf = [b for b in ltf if b["t"] + TF_MIN * 60 <= now]
    return ltf, htf, src


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
def simulate(ltf, htf):
    closes = [b["c"] for b in ltf]
    ef, es, et = ema(closes, EMA_FAST), ema(closes, EMA_SLOW), ema(closes, EMA_TREND)
    r = rsi(closes, RSI_LEN)
    a = atr(ltf, ATR_LEN)
    m, s = macd(closes)

    h_closes = [b["c"] for b in htf]
    h_ema = ema(h_closes, EMA_TREND)
    h_starts = [b["t"] for b in htf]

    def htf_prev(t):
        # Nến H4 ĐÃ ĐÓNG ngay trước nến H4 chứa thời điểm t (giống [1] + lookahead_on)
        j = bisect.bisect_right(h_starts, t) - 1 - 1
        if j < 0 or h_ema[j] is None:
            return None, None
        return h_closes[j], h_ema[j]

    pos, entry, sl, tp = 0, None, None, None
    wins = losses = 0
    events = []
    trend = "ĐI NGANG"
    for i in range(1, len(ltf)):
        b = ltf[i]
        # Kiểm tra chạm SL/TP (cùng nến chạm cả hai -> tính thua)
        if pos == 1:
            if b["l"] <= sl:
                losses += 1; pos = 0
                events.append({"type": "LOSS", "side": "MUA", "i": i, "entry": entry, "price": sl})
            elif b["h"] >= tp:
                wins += 1; pos = 0
                events.append({"type": "WIN", "side": "MUA", "i": i, "entry": entry, "price": tp})
        elif pos == -1:
            if b["h"] >= sl:
                losses += 1; pos = 0
                events.append({"type": "LOSS", "side": "BÁN", "i": i, "entry": entry, "price": sl})
            elif b["l"] <= tp:
                wins += 1; pos = 0
                events.append({"type": "WIN", "side": "BÁN", "i": i, "entry": entry, "price": tp})

        if None in (ef[i], es[i], et[i], r[i], a[i]):
            continue
        hc, he = htf_prev(b["t"])
        if USE_HTF and hc is None:
            continue
        htf_up = (not USE_HTF) or hc > he
        htf_dn = (not USE_HTF) or hc < he
        c = b["c"]
        t_up = c > et[i] and ef[i] > es[i] and htf_up
        t_dn = c < et[i] and ef[i] < es[i] and htf_dn
        trend = "TĂNG" if t_up else "GIẢM" if t_dn else "ĐI NGANG"

        buy = t_up and cross_up(m, s, i) and 50 < r[i] < RSI_OB
        sell = t_dn and cross_down(m, s, i) and RSI_OS < r[i] < 50

        if buy and pos != 1:
            reversed_ = pos == -1
            pos, entry, sl, tp = 1, c, c - a[i] * SL_MULT, c + a[i] * TP_MULT
            events.append({"type": "BUY", "i": i, "entry": entry, "sl": sl, "tp": tp,
                           "rsi": r[i], "rev": reversed_})
        elif sell and pos != -1:
            reversed_ = pos == 1
            pos, entry, sl, tp = -1, c, c + a[i] * SL_MULT, c - a[i] * TP_MULT
            events.append({"type": "SELL", "i": i, "entry": entry, "sl": sl, "tp": tp,
                           "rsi": r[i], "rev": reversed_})

    last = len(ltf) - 1
    status = {"pos": pos, "entry": entry, "sl": sl, "tp": tp, "wins": wins, "losses": losses,
              "trend": trend, "close": ltf[last]["c"], "rsi": r[last], "atr": a[last],
              "t": ltf[last]["t"]}
    return events, status


# ------------------------- TELEGRAM -------------------------
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


def event_text(e, ltf):
    t = fmt_time(ltf[e["i"]]["t"] + TF_MIN * 60)
    if e["type"] in ("BUY", "SELL"):
        side = "🟢 MUA (BUY)" if e["type"] == "BUY" else "🔴 BÁN (SELL)"
        lines = ["%s VÀNG – nến M%d đóng lúc %s" % (side, TF_MIN, t),
                 "Giá vào: %s" % fmt(e["entry"]),
                 "SL: %s  (cách %s)" % (fmt(e["sl"]), fmt(abs(e["entry"] - e["sl"]))),
                 "TP: %s  (cách %s)" % (fmt(e["tp"]), fmt(abs(e["tp"] - e["entry"]))),
                 "RSI: %.1f" % e["rsi"]]
        if e["rev"]:
            lines.append("⚠️ Đảo chiều: đóng lệnh ngược chiều đang mở (nếu có).")
        return "\n".join(lines)
    if e["type"] == "WIN":
        return "✅ Lệnh %s vào %s đã CHẠM TP %s (%s)" % (e["side"], fmt(e["entry"]), fmt(e["price"]), t)
    return "❌ Lệnh %s vào %s đã CHẠM SL %s (%s)" % (e["side"], fmt(e["entry"]), fmt(e["price"]), t)


def status_text(st, src):
    total = st["wins"] + st["losses"]
    wr = st["wins"] * 100.0 / total if total else 0
    lines = ["📊 Bot tín hiệu vàng đang chạy",
             "Nguồn giá: %s" % src,
             "Nến gần nhất: %s – giá %s" % (fmt_time(st["t"] + TF_MIN * 60), fmt(st["close"])),
             "Xu hướng: %s | RSI %.1f | ATR %s" % (st["trend"], st["rsi"] or 0, fmt(st["atr"] or 0))]
    if st["pos"] != 0:
        lines.append("Lệnh đang mở: %s %s | SL %s | TP %s" % (
            "MUA" if st["pos"] == 1 else "BÁN", fmt(st["entry"]), fmt(st["sl"]), fmt(st["tp"])))
    else:
        lines.append("Hiện không có lệnh – chờ tín hiệu.")
    lines.append("Thống kê dữ liệu gần đây: thắng %d / thua %d (%.0f%%)" % (st["wins"], st["losses"], wr))
    return "\n".join(lines)


# ------------------------- VÒNG LẶP -------------------------
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
    ltf, htf, src = get_data()
    if len(ltf) < EMA_TREND + 10:
        log("Chưa đủ dữ liệu (%d nến)." % len(ltf))
        return
    events, st = simulate(ltf, htf)
    state = load_state()
    last_t = state.get("last_t")

    if last_t is None:
        # Lần đầu: không gửi lại tín hiệu cũ
        new = []
    else:
        new = [e for e in events if ltf[e["i"]]["t"] > last_t]

    for e in new:
        txt = event_text(e, ltf)
        log(txt.replace("\n", " | "))
        send(txt)

    if startup:
        send(status_text(st, src))
    log("Đã kiểm tra: giá %s, xu hướng %s, %d sự kiện mới." % (fmt(st["close"]), st["trend"], len(new)))
    save_state({"last_t": ltf[-1]["t"]})


def main():
    if "--chatid" in sys.argv:
        print_chat_ids()
        return
    if not channels():
        print("Chưa có kênh nhận tin. Mở gold_bot.py (chuột phải > Open with > Sublime)")
        print("và điền DISCORD_WEBHOOK hoặc GMAIL_USER + GMAIL_APP_PASSWORD.")
        return
    log("Kênh nhận tin: %s" % ", ".join(n for n, _ in channels()))

    if "--once" in sys.argv:
        # Chế độ GitHub Actions: kiểm tra 1 lần rồi thoát
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
        nxt = (now // period + 1) * period + 30   # 30 giây sau khi nến đóng
        time.sleep(max(5, nxt - now))
        try:
            run_once()
        except Exception as ex:
            log("Lỗi: %s (sẽ thử lại ở nến sau)" % ex)


if __name__ == "__main__":
    main()

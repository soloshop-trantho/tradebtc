import json
import os
import time
from urllib.request import urlopen
from urllib.parse import urlencode

SYMBOL = os.environ.get("SYMBOL", "BTCUSDT")
INTERVALS = os.environ.get("INTERVALS", "1h,4h").split(",")
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "alerted.json"


def fetch_klines(symbol, interval, limit=10):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    with urlopen(url) as res:
        data = json.loads(res.read())
    return [
        {"t": d[0], "o": float(d[1]), "h": float(d[2]), "l": float(d[3]), "c": float(d[4]), "closeTime": d[6]}
        for d in data
    ]


def detect_hammer(candles, idx):
    """Trả về 'hammer' (sau xu hướng giảm) hoặc 'hammer_like' (bóng dưới dài, không rõ xu hướng), hoặc None."""
    if idx < 2:
        return None
    cur = candles[idx]
    body = abs(cur["c"] - cur["o"])
    rng = (cur["h"] - cur["l"]) or 0.0001
    upper_wick = cur["h"] - max(cur["o"], cur["c"])
    lower_wick = min(cur["o"], cur["c"]) - cur["l"]

    lookback = [c["c"] for c in candles[max(0, idx - 5):idx]]
    trend_down = len(lookback) > 1 and lookback[-1] < lookback[0]

    if lower_wick > body * 2 and upper_wick < body * 0.5:
        return "hammer" if trend_down else "hammer_like"
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_state(state):
    trimmed = sorted(state)[-500:]  # chỉ giữ 500 mục gần nhất, tránh file phình to
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?{urlencode({'chat_id': TELEGRAM_CHAT_ID, 'text': text})}"
    urlopen(url)


def main():
    alerted = load_state()
    changed = False
    now_ms = int(time.time() * 1000)

    for interval in INTERVALS:
        candles = fetch_klines(SYMBOL, interval, limit=10)
        idx = len(candles) - 1
        if candles[idx]["closeTime"] > now_ms:
            idx -= 1  # bỏ nến đang chạy dở, chỉ xét nến đã đóng
        if idx < 2:
            continue

        pattern = detect_hammer(candles, idx)
        if pattern:
            key = f"{SYMBOL}_{interval}_{candles[idx]['t']}"
            if key not in alerted:
                alerted.add(key)
                changed = True
                label = "Búa (Hammer) sau xu hướng giảm" if pattern == "hammer" else "dạng nến Búa (bóng dưới dài)"
                send_telegram(f"🔨 {SYMBOL} khung {interval}: xuất hiện {label}")
                print(f"Đã gửi cảnh báo: {SYMBOL} {interval} {pattern}")
            else:
                print(f"Đã báo trước đó, bỏ qua: {SYMBOL} {interval}")
        else:
            print(f"Không có mô hình Hammer: {SYMBOL} {interval}")

    if changed:
        save_state(alerted)


if __name__ == "__main__":
    main()

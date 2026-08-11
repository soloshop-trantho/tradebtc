import json
import os
import time
from urllib.request import urlopen
from urllib.parse import urlencode

SYMBOLS = [s.strip() for s in os.environ.get("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
INTERVALS = [i.strip() for i in os.environ.get("INTERVALS", "1h,4h,1d").split(",") if i.strip()]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "alerted.json"

SL_BUFFER_PCT = 0.007       # đệm SL dưới đáy râu Hammer 0.7%
BB_TOUCH_BUFFER = 0.0015    # cho phép sai số 0.15% khi coi là "chạm" dải Bollinger
TWEEZER_TOLERANCE = 0.0015  # sai số cho phép giữa 2 đỉnh/đáy để coi là "bằng nhau"


# ==================== DỮ LIỆU ====================

def fetch_klines(symbol, interval, limit=250):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    with urlopen(url) as res:
        data = json.loads(res.read())
    return [
        {
            "t": d[0], "o": float(d[1]), "h": float(d[2]), "l": float(d[3]),
            "c": float(d[4]), "v": float(d[5]), "closeTime": d[6],
        }
        for d in data
    ]


# ==================== CHỈ BÁO ====================

def sma_series(values, period):
    n = len(values)
    out = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(values[i - period + 1:i + 1]) / period
    return out


def stddev_series(values, sma_vals, period):
    n = len(values)
    out = [None] * n
    for i in range(period - 1, n):
        mean = sma_vals[i]
        var = sum((v - mean) ** 2 for v in values[i - period + 1:i + 1]) / period
        out[i] = var ** 0.5
    return out


def bollinger_bands(closes, period=20, mult=2):
    sma_vals = sma_series(closes, period)
    sd_vals = stddev_series(closes, sma_vals, period)
    upper = [sma_vals[i] + mult * sd_vals[i] if sma_vals[i] is not None else None for i in range(len(closes))]
    lower = [sma_vals[i] - mult * sd_vals[i] if sma_vals[i] is not None else None for i in range(len(closes))]
    return upper, lower


def ema_series(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    k = 2 / (period + 1)
    sma = sum(values[:period]) / period
    result[period - 1] = sma
    prev = sma
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        result[i] = prev
    return result


def rsi_series(closes, period=14):
    n = len(closes)
    out = [None] * n
    gains = 0.0
    losses = 0.0
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        if i <= period:
            gains += gain
            losses += loss
            if i == period:
                out[i] = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
        else:
            gains = (gains * (period - 1) + gain) / period
            losses = (losses * (period - 1) + loss) / period
            out[i] = 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)
    return out


# ==================== MÔ HÌNH NẾN ====================

def detect_hammer(candles, idx):
    if idx < 2:
        return None
    cur = candles[idx]
    body = abs(cur["c"] - cur["o"])
    upper_wick = cur["h"] - max(cur["o"], cur["c"])
    lower_wick = min(cur["o"], cur["c"]) - cur["l"]

    lookback = [c["c"] for c in candles[max(0, idx - 5):idx]]
    trend_down = len(lookback) > 1 and lookback[-1] < lookback[0]

    if lower_wick > body * 2 and upper_wick < body * 0.5:
        return "hammer" if trend_down else "hammer_like"
    return None


def detect_tweezer(candles, idx, upper_band, lower_band):
    if idx < 1:
        return None
    cur = candles[idx]
    prev = candles[idx - 1]
    avg_range = ((prev["h"] - prev["l"]) + (cur["h"] - cur["l"])) / 2 or 0.0001

    low_diff = abs(prev["l"] - cur["l"])
    if (low_diff < avg_range * TWEEZER_TOLERANCE * 10
            and prev["c"] < prev["o"] and cur["c"] > cur["o"]):
        lb = lower_band[idx]
        if lb is not None and min(prev["l"], cur["l"]) <= lb * (1 + BB_TOUCH_BUFFER):
            return "tweezer_bottom"

    high_diff = abs(prev["h"] - cur["h"])
    if (high_diff < avg_range * TWEEZER_TOLERANCE * 10
            and prev["c"] > prev["o"] and cur["c"] < cur["o"]):
        ub = upper_band[idx]
        if ub is not None and max(prev["h"], cur["h"]) >= ub * (1 - BB_TOUCH_BUFFER):
            return "tweezer_top"

    return None


# ==================== BỐI CẢNH CHO HAMMER ====================

def check_bullish_divergence(candles, rsi_vals, idx, lookback=30):
    cur_low = candles[idx]["l"]
    cur_rsi = rsi_vals[idx]
    if cur_rsi is None:
        return False
    search_start = max(0, idx - lookback)
    search_end = idx - 2
    if search_end <= search_start:
        return False
    prev_low = None
    prev_low_idx = None
    for i in range(search_start, search_end):
        if prev_low is None or candles[i]["l"] < prev_low:
            prev_low = candles[i]["l"]
            prev_low_idx = i
    if prev_low_idx is None or rsi_vals[prev_low_idx] is None:
        return False
    return cur_low < prev_low and cur_rsi > rsi_vals[prev_low_idx]


def find_resistances(candles, idx, lookback=100, max_levels=2):
    price = candles[idx]["c"]
    swings = []
    start = max(2, idx - lookback)
    end = idx - 2
    for i in range(start, end):
        h = candles[i]["h"]
        if (h > candles[i - 1]["h"] and h > candles[i - 2]["h"]
                and h > candles[i + 1]["h"] and h > candles[i + 2]["h"]):
            swings.append(h)
    above = sorted(set(v for v in swings if v > price))
    return above[:max_levels]


def analyze_context(candles, idx):
    closes = [c["c"] for c in candles]
    vols = [c["v"] for c in candles]
    cur = candles[idx]

    lookback_vols = vols[max(0, idx - 20):idx]
    avg_vol = sum(lookback_vols) / len(lookback_vols) if lookback_vols else 0
    vol_ratio = (cur["v"] / avg_vol) if avg_vol else 0
    volume_spike = vol_ratio >= 1.5

    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)
    price = cur["c"]
    near_ema = []
    for name, series in [("EMA20", ema20), ("EMA50", ema50), ("EMA200", ema200)]:
        val = series[idx]
        if val and abs(price - val) / val < 0.006:
            near_ema.append(name)

    lookback_lows = [c["l"] for c in candles[max(0, idx - 30):idx]]
    support_level = min(lookback_lows) if lookback_lows else None
    near_support = support_level is not None and abs(cur["l"] - support_level) / support_level < 0.006

    trend_up = False
    if len(closes) > 60 and ema50[idx] and ema50[idx - 10]:
        trend_up = ema50[idx] > ema50[idx - 10]

    return {
        "vol_ratio": vol_ratio,
        "volume_spike": volume_spike,
        "near_ema": near_ema,
        "near_support": near_support,
        "trend_up": trend_up,
    }


def fmt(v):
    return f"{v:,.4f}" if v < 10 else f"{v:,.2f}"


# ==================== TRẠNG THÁI ====================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        data.setdefault("alerted", [])
        data.setdefault("pending", {})
        return data
    return {"alerted": [], "pending": {}}


def save_state(state):
    state["alerted"] = sorted(set(state["alerted"]))[-1500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?{urlencode({'chat_id': TELEGRAM_CHAT_ID, 'text': text})}"
    urlopen(url)


# ==================== SOẠN TIN NHẮN ====================

def build_watch_message(symbol, interval, oversold, divergence):
    reason = "RSI quá bán" if oversold else "RSI phân kỳ dương"
    return (
        f"🔎 {symbol} khung {interval}: Hammer đạt 3/4 điều kiện checklist\n"
        f"— Xu hướng giảm ngắn hạn: ✅\n"
        f"— Vùng giá (hỗ trợ/EMA50-200/trendline): ✅\n"
        f"— {reason}: ✅\n"
        f"— Đang chờ nến kế tiếp đóng cửa để xác nhận (điều kiện 4/4)...\n"
        f"Sẽ báo kết quả ở lần kiểm tra tiếp theo."
    )


def build_reject_message(symbol, interval):
    return f"❌ {symbol} khung {interval}: Hammer xuất hiện nhưng nến xác nhận KHÔNG tăng — huỷ tín hiệu mua theo checklist."


def build_info_message(symbol, interval, pattern, ctx, c1, c2, c3):
    label = "Búa (Hammer) sau xu hướng giảm" if pattern == "hammer" else "dạng nến Búa (bóng dưới dài)"
    vol_txt = f"gấp {ctx['vol_ratio']:.1f} lần trung bình" + (" — tăng đột biến" if ctx["volume_spike"] else "")
    checklist = f"{'✅' if c1 else '❌'} Xu hướng | {'✅' if c2 else '❌'} Vùng giá | {'✅' if c3 else '❌'} RSI"
    return (
        f"🔨 {symbol} khung {interval}: xuất hiện {label}\n"
        f"📊 Volume: {vol_txt}\n"
        f"📋 Checklist: {checklist} — CHƯA đủ điều kiện vào lệnh theo chiến lược của bạn"
    )


def build_trade_plan(symbol, interval, pending, confirm_candle):
    hammer_low = pending["hammer_low"]
    hammer_high = pending["hammer_high"]
    body_top = pending["hammer_body_top"]
    resistances = pending["resistances"]

    entry_safe = confirm_candle["c"]
    entry_limit = (hammer_high + body_top) / 2
    sl = hammer_low * (1 - SL_BUFFER_PCT)
    risk_safe = entry_safe - sl

    if resistances:
        tp1 = resistances[0]
        tp2 = resistances[1] if len(resistances) > 1 else None
    else:
        tp1 = entry_safe + risk_safe * 1.5 if risk_safe > 0 else None
        tp2 = entry_safe + risk_safe * 3 if risk_safe > 0 else None

    rr1 = (tp1 - entry_safe) / risk_safe if (tp1 and risk_safe > 0) else None

    lines = [
        f"✅ {symbol} khung {interval}: TÍN HIỆU MUA — đủ 4/4 điều kiện checklist Hammer",
        "",
        f"🎯 Entry an toàn: {fmt(entry_safe)} (giá đóng cửa nến xác nhận)",
        f"🎯 Entry tối ưu (chờ limit): {fmt(entry_limit)} (test lại nửa trên thân/râu Hammer)",
        f"🛑 Stop Loss: {fmt(sl)} (dưới đáy râu Hammer ~{SL_BUFFER_PCT*100:.1f}%)",
    ]

    if tp1:
        rr_txt = f" — R:R ≈ 1:{rr1:.2f}" if rr1 else ""
        lines.append(f"🏁 TP1 (chốt 50% vị thế): {fmt(tp1)}{rr_txt}")
    else:
        lines.append("🏁 TP1: chưa xác định được vùng kháng cự gần — tự theo dõi thêm")

    if tp2:
        lines.append(f"🏁 TP2 (gồng lời phần còn lại): {fmt(tp2)} — hoặc dùng EMA20/50 làm điểm thoát, chỉ đóng lệnh khi nến đóng cửa dưới EMA")
    else:
        lines.append("🏁 TP2: chưa có vùng kháng cự xa hơn — gồng lời theo EMA20/50, thoát khi đóng nến dưới EMA")

    lines.append("")
    lines.append("⚠️ Khi đạt TP1: chốt 50% vị thế và dời Stop Loss về điểm hoà vốn (entry).")
    lines.append("⚠️ Tính toán tự động theo bộ quy tắc bạn cung cấp — không phải lời khuyên đầu tư. Tự quyết định khối lượng vào lệnh và quản lý rủi ro của riêng bạn.")
    return "\n".join(lines)


def build_tweezer_message(symbol, interval, kind, candles, idx, upper_band, lower_band):
    cur = candles[idx]
    prev = candles[idx - 1]
    if kind == "tweezer_top":
        band_val = upper_band[idx]
        lines = [
            f"⚠️ {symbol} khung {interval}: NHÍP ĐỈNH (Tweezer Top) tại dải TRÊN Bollinger",
            f"📍 Đỉnh nến: {fmt(max(prev['h'], cur['h']))} — Dải trên hiện tại: {fmt(band_val) if band_val else 'N/A'}",
            "📉 Tín hiệu: khả năng ĐẢO CHIỀU GIẢM (cân nhắc SHORT/bán)",
            "— Nến xanh đẩy giá lên vùng quá mua, nến đỏ ngay sau từ chối mức giá cao, phe bán nhập cuộc.",
        ]
    else:
        band_val = lower_band[idx]
        lines = [
            f"⚠️ {symbol} khung {interval}: NHÍP ĐÁY (Tweezer Bottom) tại dải DƯỚI Bollinger",
            f"📍 Đáy nến: {fmt(min(prev['l'], cur['l']))} — Dải dưới hiện tại: {fmt(band_val) if band_val else 'N/A'}",
            "📈 Tín hiệu: khả năng ĐẢO CHIỀU TĂNG (cân nhắc LONG/mua)",
            "— Nến đỏ ép giá xuống vùng quá bán, nến xanh ngay sau kéo ngược lại, phe mua bắt đáy.",
        ]
    lines.append("⚠️ Đây là mô tả trạng thái kỹ thuật tự động, không phải lời khuyên đầu tư — tự quản lý rủi ro của riêng bạn.")
    return "\n".join(lines)


# ==================== XỬ LÝ 1 COIN + 1 KHUNG GIỜ ====================

def process_symbol_interval(symbol, interval, state, alerted):
    changed = False
    now_ms = int(time.time() * 1000)

    try:
        candles = fetch_klines(symbol, interval, limit=250)
    except Exception as e:
        print(f"Lỗi tải dữ liệu {symbol} {interval}: {e}")
        return False

    idx = len(candles) - 1
    if candles[idx]["closeTime"] > now_ms:
        idx -= 1
    if idx < 60:
        print(f"Chưa đủ dữ liệu: {symbol} {interval}")
        return False

    closes = [c["c"] for c in candles]
    pending_all = state["pending"]
    pending_key = f"{symbol}_{interval}"
    pending = pending_all.get(pending_key)
    handled_confirmation = False

    # ---------- 1) Xử lý xác nhận Hammer đang chờ ----------
    if pending and candles[idx]["t"] > pending["candle_time"]:
        confirm_candle = candles[idx]
        is_bullish = confirm_candle["c"] > confirm_candle["o"]
        if is_bullish:
            send_telegram(build_trade_plan(symbol, interval, pending, confirm_candle))
            print(f"XÁC NHẬN MUA (Hammer) gửi đi: {symbol} {interval}")
        else:
            send_telegram(build_reject_message(symbol, interval))
            print(f"Huỷ tín hiệu Hammer (không xác nhận): {symbol} {interval}")
        del pending_all[pending_key]
        changed = True
        handled_confirmation = True

    # ---------- 2) Phát hiện Hammer mới ----------
    if not handled_confirmation:
        pattern = detect_hammer(candles, idx)
        if pattern:
            key = f"hammer_{symbol}_{interval}_{candles[idx]['t']}"
            if key not in alerted:
                alerted.add(key)
                changed = True
                ctx = analyze_context(candles, idx)
                rsi_vals = rsi_series(closes, 14)
                cur_rsi = rsi_vals[idx]
                oversold = cur_rsi is not None and cur_rsi < 30
                divergence = check_bullish_divergence(candles, rsi_vals, idx)

                c1 = pattern == "hammer"
                c2 = ctx["near_support"] or ctx["trend_up"] or ("EMA50" in ctx["near_ema"]) or ("EMA200" in ctx["near_ema"])
                c3 = oversold or divergence

                if c1 and c2 and c3:
                    resistances = find_resistances(candles, idx)
                    pending_all[pending_key] = {
                        "candle_time": candles[idx]["t"],
                        "hammer_low": candles[idx]["l"],
                        "hammer_high": candles[idx]["h"],
                        "hammer_body_top": max(candles[idx]["o"], candles[idx]["c"]),
                        "resistances": resistances,
                    }
                    send_telegram(build_watch_message(symbol, interval, oversold, divergence))
                    print(f"PENDING chờ xác nhận Hammer: {symbol} {interval}")
                else:
                    send_telegram(build_info_message(symbol, interval, pattern, ctx, c1, c2, c3))
                    print(f"Đã gửi cảnh báo Hammer (chưa đủ checklist): {symbol} {interval}")
            else:
                print(f"Hammer đã báo trước đó, bỏ qua: {symbol} {interval}")
        else:
            print(f"Không có mô hình Hammer: {symbol} {interval}")

    # ---------- 3) Phát hiện Tweezer Top/Bottom tại dải Bollinger ----------
    upper_band, lower_band = bollinger_bands(closes, period=20, mult=2)
    tweezer = detect_tweezer(candles, idx, upper_band, lower_band)
    if tweezer:
        key = f"{tweezer}_{symbol}_{interval}_{candles[idx]['t']}"
        if key not in alerted:
            alerted.add(key)
            changed = True
            send_telegram(build_tweezer_message(symbol, interval, tweezer, candles, idx, upper_band, lower_band))
            print(f"Đã gửi cảnh báo {tweezer}: {symbol} {interval}")
        else:
            print(f"{tweezer} đã báo trước đó, bỏ qua: {symbol} {interval}")
    else:
        print(f"Không có Tweezer tại dải Bollinger: {symbol} {interval}")

    return changed


# ==================== VÒNG LẶP CHÍNH ====================

def main():
    state = load_state()
    alerted = set(state["alerted"])
    any_changed = False

    for symbol in SYMBOLS:
        for interval in INTERVALS:
            changed = process_symbol_interval(symbol, interval, state, alerted)
            any_changed = any_changed or changed

    if any_changed:
        state["alerted"] = list(alerted)
        save_state(state)


if __name__ == "__main__":
    main()

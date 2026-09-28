# Liquidity Reversal Bot V1
# Separate experimental scanner. It does NOT modify the user's BTC/ETH V4 FIXED bot.
# Strategy reconstruction from the observed LINK signal:
# 1H direction -> 15m liquidity sweep -> 15m CHoCH -> 10m imbalance/FVG
# -> 5m retest/confirmation -> order-book bias -> next liquidity target.

import os
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

BASE = "https://contract.mexc.com/api/v1/contract"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]
SYMBOLS = [x.strip().upper() for x in os.getenv(
    "MEXC_SYMBOLS",
    "LINK_USDT,BTC_USDT,ETH_USDT,INJ_USDT,TAO_USDT,PENDLE_USDT,AAVE_USDT,PYTH_USDT,ADA_USDT,NEAR_USDT"
).split(",") if x.strip()]

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
LEVERAGE = int(os.getenv("LEVERAGE", "30"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "7"))
DEPTH_LEVELS = int(os.getenv("DEPTH_LEVELS", "20"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))
KYIV = ZoneInfo("Europe/Kyiv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("liquidity-reversal-bot")
session = requests.Session()
session.headers.update({"User-Agent": "Liquidity-Reversal-Bot/1.0", "Cache-Control": "no-cache"})
last_signal = {}
last_error = {}


def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(KYIV).strftime("%Y-%m-%d %H:%M")


def get_json(path, params=None):
    r = session.get(f"{BASE}/{path}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"MEXC error: {data}")
    return data


def fetch_klines(symbol, interval, limit=250):
    data = get_json(f"kline/{symbol}", {"interval": interval, "limit": limit}).get("data")
    if not data:
        raise RuntimeError(f"empty kline {symbol} {interval}")
    times = data["time"]
    rows = []
    for i, ts in enumerate(times):
        rows.append({
            "ts": int(ts),
            "o": float(data["open"][i]),
            "h": float(data["high"][i]),
            "l": float(data["low"][i]),
            "c": float(data["close"][i]),
            "v": float(data.get("vol", [0] * len(times))[i]),
        })
    rows.sort(key=lambda x: x["ts"])
    now = int(time.time())
    step = {"Min5": 300, "Min15": 900, "Min60": 3600}[interval]
    return [x for x in rows if x["ts"] + step <= now]


def build_10m_from_5m(rows):
    buckets = {}
    for x in rows:
        b = (x["ts"] // 600) * 600
        buckets.setdefault(b, []).append(x)
    out = []
    now = int(time.time())
    for b, rs in sorted(buckets.items()):
        if b + 600 <= now and len(rs) >= 2:
            out.append({
                "ts": b, "o": rs[0]["o"],
                "h": max(x["h"] for x in rs),
                "l": min(x["l"] for x in rs),
                "c": rs[-1]["c"],
                "v": sum(x["v"] for x in rs),
            })
    return out


def atr(rows, n=14):
    if len(rows) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(rows)):
        prev = rows[i - 1]["c"]
        x = rows[i]
        trs.append(max(x["h"] - x["l"], abs(x["h"] - prev), abs(x["l"] - prev)))
    return sum(trs[-n:]) / n


def ema(rows, n):
    if not rows:
        return 0.0
    k = 2.0 / (n + 1)
    e = rows[0]["c"]
    for x in rows[1:]:
        e = x["c"] * k + e * (1 - k)
    return e


def swing_highs(rows, left=2, right=2):
    out = []
    for i in range(left, len(rows) - right):
        h = rows[i]["h"]
        if all(h > rows[j]["h"] for j in range(i-left, i)) and all(h >= rows[j]["h"] for j in range(i+1, i+right+1)):
            out.append((i, h))
    return out


def swing_lows(rows, left=2, right=2):
    out = []
    for i in range(left, len(rows) - right):
        l = rows[i]["l"]
        if all(l < rows[j]["l"] for j in range(i-left, i)) and all(l <= rows[j]["l"] for j in range(i+1, i+right+1)):
            out.append((i, l))
    return out


def detect_liquidity_sweep(rows, lookback=12, recent=8):
    if len(rows) < lookback + 2:
        return None
    start = max(lookback, len(rows) - recent)
    for i in range(len(rows) - 1, start - 1, -1):
        x = rows[i]
        prior = rows[i-lookback:i]
        prior_low = min(r["l"] for r in prior)
        prior_high = max(r["h"] for r in prior)
        if x["l"] < prior_low and x["c"] > prior_low:
            return {"side": "LONG", "level": prior_low, "bar": x, "index": i}
        if x["h"] > prior_high and x["c"] < prior_high:
            return {"side": "SHORT", "level": prior_high, "bar": x, "index": i}
    return None


def detect_choch(rows, side, after_index=None, window=24):
    if len(rows) < window + 5:
        return None
    if after_index is None:
        after_index = max(0, len(rows) - 8)
    for end in range(len(rows) - 1, after_index, -1):
        base = rows[:end-1]
        highs = swing_highs(base)
        lows = swing_lows(base)
        if side == "LONG" and highs:
            level = highs[-1][1]
            if rows[end]["c"] > level:
                return level
        if side == "SHORT" and lows:
            level = lows[-1][1]
            if rows[end]["c"] < level:
                return level
    return None


def find_fvg(rows, side, search=10):
    start = max(2, len(rows) - search)
    for i in range(len(rows) - 1, start - 1, -1):
        a, _, c = rows[i-2], rows[i-1], rows[i]
        if side == "LONG" and c["l"] > a["h"]:
            return {"low": a["h"], "high": c["l"], "mid": (a["h"] + c["l"]) / 2, "ts": c["ts"]}
        if side == "SHORT" and c["h"] < a["l"]:
            return {"low": c["h"], "high": a["l"], "mid": (c["h"] + a["l"]) / 2, "ts": c["ts"]}
    return None


def retest_confirm(rows, side, zone):
    if not zone or len(rows) < 3:
        return False
    x = rows[-1]
    if side == "LONG":
        touched = x["l"] <= zone["high"] and x["h"] >= zone["low"]
        return touched and x["c"] > x["o"] and x["c"] >= zone["mid"]
    touched = x["h"] >= zone["low"] and x["l"] <= zone["high"]
    return touched and x["c"] < x["o"] and x["c"] <= zone["mid"]


def orderbook_bias(symbol, side):
    try:
        data = get_json(f"depth/{symbol}", {"limit": DEPTH_LEVELS})
        d = data.get("data", {})
        bids = [(float(p), float(v)) for p, v, *_ in d.get("bids", [])]
        asks = [(float(p), float(v)) for p, v, *_ in d.get("asks", [])]
        if not bids or not asks:
            return {"ok": False, "ratio": 1.0, "shelf": None}
        bid_vol = sum(v for _, v in bids)
        ask_vol = sum(v for _, v in asks)
        ratio = bid_vol / max(ask_vol, 1e-12)
        if side == "LONG":
            return {"ok": ratio >= 1.15, "ratio": ratio, "shelf": max(bids, key=lambda x: x[1])[0]}
        return {"ok": ratio <= 1 / 1.15, "ratio": ratio, "shelf": max(asks, key=lambda x: x[1])[0]}
    except Exception:
        return {"ok": False, "ratio": 1.0, "shelf": None}


def next_liquidity_target(rows, side, entry):
    if side == "LONG":
        levels = [p for _, p in swing_highs(rows[:-2]) if p > entry]
        target = min(levels) if levels else None
        return target if target and target > entry * 1.003 else entry * 1.015
    levels = [p for _, p in swing_lows(rows[:-2]) if p < entry]
    target = max(levels) if levels else None
    return target if target and target < entry * 0.997 else entry * 0.985


def analyze(symbol):
    h1 = fetch_klines(symbol, "Min60", 180)
    m15 = fetch_klines(symbol, "Min15", 220)
    m5 = fetch_klines(symbol, "Min5", 240)
    m10 = build_10m_from_5m(m5)
    if min(len(h1), len(m15), len(m10), len(m5)) < 30:
        return None

    price = m5[-1]["c"]
    a15 = atr(m15)
    if a15 <= 0:
        return None

    h1_side = "LONG" if ema(h1[-80:], 20) > ema(h1[-100:], 50) else "SHORT"
    sweep = detect_liquidity_sweep(m15, 12, 8)
    if not sweep:
        return None
    side = sweep["side"]
    if side != h1_side:
        return None

    score = 4
    reasons = [f"15m {side} liquidity sweep", "1H trend aligned"]

    choch = detect_choch(m15, side, after_index=sweep["index"])
    if choch is None:
        return None
    score += 2
    reasons.append("15m CHoCH confirmed")

    zone = find_fvg(m10, side, 10)
    if not zone:
        return None
    score += 1
    reasons.append("10m imbalance/FVG found")

    if not retest_confirm(m5, side, zone):
        return None
    score += 2
    reasons.append("5m zone retest confirmed")

    ob = orderbook_bias(symbol, side)
    if ob["ok"]:
        score += 1
        reasons.append("order-book bias aligned")

    if score < MIN_SCORE:
        return None

    entry = zone["mid"]
    if side == "LONG":
        sl = min(sweep["level"], zone["low"]) - a15 * 0.20
    else:
        sl = max(sweep["level"], zone["high"]) + a15 * 0.20

    risk = abs(entry - sl)
    if risk <= 0 or risk / entry > 0.025:
        return None

    tp = next_liquidity_target(m15, side, entry)
    if side == "LONG" and tp <= entry + risk * 1.5:
        tp = entry + risk * 1.5
    if side == "SHORT" and tp >= entry - risk * 1.5:
        tp = entry - risk * 1.5

    signal_key = (symbol, side, round(entry, 8), m5[-1]["ts"])
    if last_signal.get(symbol) == signal_key:
        return None
    last_signal[symbol] = signal_key

    return {
        "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp,
        "score": score, "price": price, "sweep": sweep["level"], "choch": choch,
        "zone": zone, "orderbook_ratio": ob["ratio"], "reasons": reasons, "ts": m5[-1]["ts"],
    }


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_IDS:
        log.warning("Telegram not configured")
        return False
    ok = True
    for chat_id in CHAT_IDS:
        try:
            r = session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=HTTP_TIMEOUT,
            )
            if not r.ok or not r.json().get("ok"):
                ok = False
                log.error("Telegram error chat=%s status=%s", chat_id, r.status_code)
        except Exception as e:
            ok = False
            log.error("Telegram exception chat=%s: %s", chat_id, e)
    return ok


def format_signal(s):
    coin = s["symbol"].replace("_USDT", "USDT")
    z = s["zone"]
    entry_text = f"{z['low']:.8g} - {z['high']:.8g}"
    side_emoji = "🟢 LONG" if s["side"] == "LONG" else "🔴 SHORT"
    return (
        f"{side_emoji} {coin} Futures\n"
        f"Timeframe: 1H / 15m / 10m / 5m\n\n"
        f"Точка входу: {entry_text}\n"
        f"SL: {s['sl']:.8g}\n"
        f"TP: {s['tp']:.8g}\n"
        f"Плече: {LEVERAGE}x\n"
        f"Score: {s['score']}/10\n\n"
        f"Логіка: ліквідність → CHoCH → imbalance → ретест → ціль ліквідності\n"
        f"Order book ratio: {s['orderbook_ratio']:.2f}\n"
        f"Час: {kyiv(s['ts'])} Kyiv\n\n"
        f"Причини:\n- " + "\n- ".join(s["reasons"]) +
        "\n\nСигнал тільки для аналізу — без автоматичної торгівлі."
    )


def main():
    log.info("Liquidity Reversal Bot STARTED")
    log.info("Symbols: %s", ", ".join(SYMBOLS))
    log.info("Rule: liquidity sweep -> 15m CHoCH -> 10m imbalance -> 5m retest -> orderbook/target")
    log.info("Score threshold: %s | leverage label: %sx", MIN_SCORE, LEVERAGE)
    if TELEGRAM_TOKEN and CHAT_IDS:
        send_telegram("Liquidity Reversal Bot запущений.\nСканую MEXC Futures: 1H / 15m / 10m / 5m.")
    while True:
        for symbol in SYMBOLS:
            try:
                s = analyze(symbol)
                if s:
                    log.info("SIGNAL %s %s score=%s entry=%s sl=%s tp=%s",
                             symbol, s["side"], s["score"], s["entry"], s["sl"], s["tp"])
                    send_telegram(format_signal(s))
                else:
                    log.info("WAIT %s", symbol)
            except Exception as e:
                msg = str(e)
                if last_error.get(symbol) != msg:
                    log.error("ERROR %s: %s", symbol, msg)
                    last_error[symbol] = msg
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

"""Live alerts for TradingView paper trading. Runs in GitHub Actions (.github/workflows/bot.yml).
Alert-only: you place the trade yourself in TradingView paper trading.
Data comes from Yahoo (or Alpaca if keys are in .env)."""
import csv, math, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import data
from strategy import find_signal, simulate

SYMBOLS = ["SPY", "QQQ"]          # alerted
WATCH_ONLY = ["AMZN", "NVDA"]     # scored daily, never alerted, to see if they deserve alerts
ACCOUNT, RISK_PCT = 100_000, 0.01  # TradingView paper default balance; risk 1% per trade
NY = ZoneInfo("America/New_York")
HERE = os.path.dirname(__file__)
RESULTS = os.path.join(HERE, "results.csv")
TOPIC = os.getenv("NTFY_TOPIC")
if not TOPIC:
    sys.exit("Missing NTFY_TOPIC (.env locally, repo secret in GitHub)")


TV_SYMBOL = {"SPY": "AMEX:SPY", "QQQ": "NASDAQ:QQQ"}


def tv_link(sym):
    return f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(TV_SYMBOL[sym])}"


def notify(title, msg, sym=None):
    headers = {"Title": title}
    if sym:
        # Tap opens the TradingView app (tested: the app scheme can't jump to a symbol); button opens the chart on the web.
        headers.update({"Click": "tradingview://",
                        "Actions": f"view, Open app, tradingview://; view, {sym} chart (web), {tv_link(sym)}"})
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=msg.encode(), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # an alert failure must not stop scanning/logging
        print("ntfy failed:", e)


def append(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def today_bars(sym):
    now = datetime.now(NY)
    days = data.by_day(data.load(sym, now - timedelta(days=6), now, live=True))
    if len(days) < 2 or days[-1][0] != now.date():
        return None, None  # holiday / no data yet
    day = days[-1][1]
    return days[-2][1], day[day.index < now.replace(second=0, microsecond=0)]  # closed bars only


def alert_text(sym, sig):
    entry, stop, target = sig["entry"], sig["stop"], sig["target"]
    qty = math.floor(ACCOUNT * RISK_PCT / abs(stop - entry))
    gain, loss = qty * abs(target - entry), qty * abs(stop - entry)
    side = "SELL SHORT" if sig["side"] == "short" else "BUY"
    msg = (f"{side} {qty} shares {sym} now (~{entry:.2f})\n"
           f"Target {target:.2f}: +${gain:,.0f} (price moves {abs(target-entry)/entry:.2%})\n"
           f"Stop {stop:.2f}: -${loss:,.0f} (price moves {abs(stop-entry)/entry:.2%})\n"
           f"Reward/risk {gain/loss:.1f}x | tap to open TradingView app")
    return msg, qty


def scan(start, end, sent):
    """Alert on today's setup if its entry bar falls in [start, end] (HH:MM, NY time).
    No saved state: find_signal returns the day's first setup, so each one is alerted once,
    and the two cloud runs split the day by entry time so they never double-alert."""
    for sym in SYMBOLS:
        prev, day = today_bars(sym)
        if day is None or day.empty:
            continue
        sig = find_signal(prev, day)
        if not sig or sym in sent or not (start <= sig["time"].strftime("%H:%M") <= end):
            continue
        if sig["bar"] < len(day) - 3:
            continue  # older than ~2 min: we weren't running yet, too late to act on
        sent.add(sym)
        msg, _ = alert_text(sym, sig)
        print(datetime.now(NY), msg)
        notify(f"TJR setup: {sig['side']} {sym}", msg, sym=sym)


def score_day():
    """Re-run today's setups on the full day and record what each would have made."""
    lines = []
    for sym in SYMBOLS + WATCH_ONLY:
        prev, day = today_bars(sym)
        sig = find_signal(prev, day) if day is not None else None
        if not sig:
            continue
        R = simulate(day, sig)
        tag = "" if sym in SYMBOLS else " (watch-only)"
        lines.append(f"{sym}{tag} {sig['side']} @ {sig['time']:%H:%M}: {R:+.2f}R (${R*ACCOUNT*RISK_PCT:+,.0f})")
        append(RESULTS, ["date", "time", "sym", "side", "entry", "stop", "target", "R", "alerted"],
               [sig["time"].date(), sig["time"].strftime("%H:%M"), sym, sig["side"],
                round(sig["entry"], 2), round(sig["stop"], 2), round(sig["target"], 2), round(R, 2),
                sym in SYMBOLS])
    notify("Today's scorecard", "\n".join(lines) if lines else "No setups today.")
    print("scored:", lines or "no setups today")


def wait_until(hhmm):
    while datetime.now(NY).strftime("%H:%M") < hhmm:
        time.sleep(20)


if __name__ == "__main__":
    # live.py scan 09:30 12:29   -> alerts for setups entered 09:30-12:29
    # live.py score              -> end-of-day scorecard (run after 15:56)
    if datetime.now(NY).weekday() >= 5:
        sys.exit("Weekend.")
    if sys.argv[1:2] == ["score"]:
        wait_until("15:57")
        score_day()
        sys.exit()
    start, end = sys.argv[2], sys.argv[3]
    wait_until(start)
    stop_at = (datetime.strptime(end, "%H:%M") + timedelta(minutes=2)).strftime("%H:%M")  # catch the last bar
    sent, errors = set(), 0
    while datetime.now(NY).strftime("%H:%M") <= stop_at:
        time.sleep(60 - datetime.now().second + 3)  # wake ~3s after each minute closes
        try:
            scan(start, end, sent)
        except Exception as e:
            print("scan error:", e)
            errors += 1
            if errors == 3:  # a blip is normal; tell the phone if it keeps failing
                notify("Paper trader error", str(e)[:300])

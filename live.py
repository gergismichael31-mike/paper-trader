"""Live alerts for paper trading. Runs in GitHub Actions (.github/workflows/bot.yml).
Each alert has an "Approve trade" button. If Alpaca PAPER keys are set (ALPACA_KEY/ALPACA_SECRET),
tapping it places a bracket order (entry + stop + target) in the Alpaca paper account, which shows in
TradingView once Alpaca is connected there. Without keys the bot stays alert-only. Prices come from Yahoo."""
import csv, json, math, os, secrets, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import data
from strategy import find_signal, find_pb, simulate

INDEXES = ["SPY", "QQQ"]
PB_UNIVERSE = ["NVDA", "AMZN", "TSLA", "META", "AAPL", "MSFT", "AMD", "GOOGL"]  # PB's usual large caps
ACCOUNT, RISK_PCT = 100_000, 0.01  # TradingView paper default balance; risk 1% per trade
NY = ZoneInfo("America/New_York")
HERE = os.path.dirname(__file__)
RESULTS = os.path.join(HERE, "results.csv")
TOPIC = os.getenv("NTFY_TOPIC")
if not TOPIC:
    sys.exit("Missing NTFY_TOPIC (.env locally, repo secret in GitHub)")
APPROVE_TOPIC = f"{TOPIC}-ok"   # the Approve button posts the alert's one-time token here
APPROVE_PAGE = "https://gergismichael31-mike.github.io/paper-trader/approve.html"  # docs/approve.html
APPROVE_SECS = 300              # an approval older than 5 min is refused: the setup is stale by then

trading = None
if data.KEY and data.SECRET:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    trading = TradingClient(data.KEY, data.SECRET, paper=True)  # PAPER ONLY - never change this
pending = {}  # one-time token -> (sym, sig, qty, deadline)
_paper_checked = False


def paper_only():
    """Second lock besides paper=True: refuse to touch any account that isn't an Alpaca PAPER account."""
    global _paper_checked
    if not _paper_checked:
        url = str(getattr(trading._base_url, "value", trading._base_url))
        acct = trading.get_account().account_number
        if "paper-api.alpaca.markets" not in url or not str(acct).startswith("PA"):
            notify("Trading BLOCKED", f"Not a paper account ({acct} @ {url}). No orders will be placed.")
            raise RuntimeError(f"refusing non-paper account {acct} @ {url}")
        _paper_checked = True


TV_SYMBOL = {"SPY": "AMEX:SPY", "QQQ": "NASDAQ:QQQ"}


def tv_link(sym):
    return f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(TV_SYMBOL.get(sym, sym))}"


def notify(title, msg, sym=None, token=None):
    headers = {"Title": title}
    if sym:
        # Tap opens the TradingView app (tested: the app scheme can't jump to a symbol); button opens the chart on the web.
        actions = [f"view, Open app, tradingview://", f"view, {sym} chart (web), {tv_link(sym)}"]
        if token:
            # opens a tiny page that posts the approval, then jumps into the TradingView app to watch the trade
            actions.insert(0, f"view, Approve trade, {APPROVE_PAGE}#{APPROVE_TOPIC}/{token}, clear=true")
        headers.update({"Click": "tradingview://", "Actions": "; ".join(actions)})
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
    # 1% risk, but never more shares than the account can pay for (tight stops would otherwise balloon size)
    qty = min(math.floor(ACCOUNT * RISK_PCT / abs(stop - entry)), math.floor(ACCOUNT / entry))
    gain, loss = qty * abs(target - entry), qty * abs(stop - entry)
    side = "SELL SHORT" if sig["side"] == "short" else "BUY"
    msg = (f"{side} {qty} shares {sym} now (~{entry:.2f})\n"
           f"Target {target:.2f}: +${gain:,.0f} (price moves {abs(target-entry)/entry:.2%})\n"
           f"Stop {stop:.2f}: -${loss:,.0f} (price moves {abs(stop-entry)/entry:.2%})\n"
           f"Reward/risk {gain/loss:.1f}x | tap to open TradingView app")
    return msg, qty


def pb_picks():
    """PB trades the day's biggest premarket gainer (long) and loser (short) among his large caps."""
    gaps = {}
    for sym in PB_UNIVERSE:
        prev, day = today_bars(sym)
        if day is None:
            continue
        pre, pc = day.between_time("04:00", "09:29"), prev.between_time("09:30", "15:59").close
        if len(pre) and len(pc):
            gaps[sym] = pre.close.iloc[-1] / pc.iloc[-1] - 1
    if not gaps:
        return {}
    return {"long": max(gaps, key=gaps.get), "short": min(gaps, key=gaps.get)}


def plan(picks):
    """(strategy, symbol, side or None, alerted?) for today. Watch-only rows are only scored."""
    rows = [("TJR", s, None, True) for s in INDEXES]
    rows += [("PB", sym, side, True) for side, sym in picks.items()]
    rows += [("PB", s, None, False) for s in INDEXES] + [("TJR", s, None, False) for s in ["AMZN", "NVDA"]]
    return rows


def detect(name, side, prev, day):
    return find_signal(prev, day) if name == "TJR" else find_pb(prev, day, side)


def scan(start, end, sent, picks):
    """Alert on a setup if its entry bar falls in [start, end] (HH:MM, NY time).
    No saved state: each detector returns the day's first setup, so each one is alerted once,
    and the two cloud runs split the day by entry time so they never double-alert."""
    for name, sym, side, alerted in plan(picks):
        if not alerted or (name, sym) in sent:
            continue
        prev, day = today_bars(sym)
        if day is None or day.empty:
            continue
        sig = detect(name, side, prev, day)
        if not sig or not (start <= sig["time"].strftime("%H:%M") <= end):
            continue
        if sig["bar"] < len(day) - 3:
            continue  # older than ~2 min: we weren't running yet, too late to act on
        sent.add((name, sym))
        msg, qty = alert_text(sym, sig)
        why = {"long": " (top premarket gainer)", "short": " (top premarket loser)"}.get(side, "") if name == "PB" else ""
        token = None
        if trading and qty >= 1:
            token = secrets.token_hex(8)  # unguessable, single use: only this alert's button can place this order
            pending[token] = (sym, sig, qty, time.time() + APPROVE_SECS)
            msg += "\nTap Approve within 5 min to place it (paper)"
        print(datetime.now(NY), name, msg)
        notify(f"{name} setup: {sig['side']} {sym}{why}", msg, sym=sym, token=token)


def check_approvals(since):
    """Place the paper order for any approved token. Unknown or reused tokens are ignored."""
    url = f"https://ntfy.sh/{APPROVE_TOPIC}/json?poll=1&since={since}"
    for line in urllib.request.urlopen(url, timeout=10).read().decode().splitlines():
        ev = json.loads(line)
        if ev.get("event") == "message" and ev.get("message", "").strip() in pending:
            place(ev["message"].strip())
    for tok in [t for t, v in pending.items() if time.time() > v[3]]:
        pending.pop(tok)  # expired: Approve did nothing in time


def place(token):
    sym, sig, qty, deadline = pending.pop(token)
    if time.time() > deadline:
        return notify(f"Not placed: {sym}", "Approved too late (over 5 min), setup is stale.")
    try:
        paper_only()
        order = trading.submit_order(MarketOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.SELL if sig["side"] == "short" else OrderSide.BUY,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(sig["target"], 2)),
            stop_loss=StopLossRequest(stop_price=round(sig["stop"], 2))))
        notify(f"Order placed: {sig['side']} {qty} {sym}",
               f"Paper order sent. Stop {sig['stop']:.2f}, target {sig['target']:.2f} are attached.\nOrder {order.id}")
    except Exception as e:  # e.g. price already past the stop/target: Alpaca rejects the bracket
        notify(f"Not placed: {sym}", f"Alpaca refused the order: {str(e)[:250]}")


def flatten():
    """Day trades only: close every paper position and cancel leftover stop/target orders before the bell."""
    if not trading:
        return ""
    paper_only()
    trading.close_all_positions(cancel_orders=True)
    a = trading.get_account()
    return f"\nAlpaca paper P&L today: {float(a.equity) - float(a.last_equity):+,.2f} (equity {float(a.equity):,.2f})"


def score_day(pnl=""):
    """Re-run today's setups on the full day and record what each would have made."""
    lines, picks = [], pb_picks()
    for name, sym, side, alerted in plan(picks):
        prev, day = today_bars(sym)
        sig = detect(name, side, prev, day) if day is not None else None
        if not sig:
            continue
        R = simulate(day, sig)
        tag = "" if alerted else " (watch-only)"
        lines.append(f"{name} {sym}{tag} {sig['side']} @ {sig['time']:%H:%M}: {R:+.2f}R (${R*ACCOUNT*RISK_PCT:+,.0f})")
        append(RESULTS, ["date", "time", "strategy", "sym", "side", "entry", "stop", "target", "R", "alerted"],
               [sig["time"].date(), sig["time"].strftime("%H:%M"), name, sym, sig["side"],
                round(sig["entry"], 2), round(sig["stop"], 2), round(sig["target"], 2), round(R, 2), alerted])
    head = f"PB picks: {picks.get('long', '-')} (long), {picks.get('short', '-')} (short)"
    notify("Today's scorecard", head + "\n" + ("\n".join(lines) if lines else "No setups today.") + pnl)
    print("scored:", head, lines or "no setups today")


def wait_until(hhmm):
    while datetime.now(NY).strftime("%H:%M") < hhmm:
        time.sleep(20)


if __name__ == "__main__":
    # live.py scan 09:30 12:29   -> alerts for setups entered 09:30-12:29
    # live.py score              -> end-of-day scorecard (run after 15:56)
    if datetime.now(NY).weekday() >= 5:
        sys.exit("Weekend.")
    if sys.argv[1:2] == ["score"]:
        wait_until("15:55")
        pnl = flatten()
        wait_until("15:57")
        score_day(pnl)
        sys.exit()
    start, end = sys.argv[2], sys.argv[3]
    wait_until(start)
    stop_at = (datetime.strptime(end, "%H:%M") + timedelta(minutes=2)).strftime("%H:%M")  # catch the last bar
    sent, errors, picks, last_min, since = set(), 0, None, None, int(time.time())
    # keep going past the window while an alert can still be approved
    while datetime.now(NY).strftime("%H:%M") <= stop_at or pending:
        time.sleep(5)
        try:
            if pending:
                check_approvals(since)
            now = datetime.now(NY)
            if now.strftime("%H:%M") != last_min and now.second >= 3 and now.strftime("%H:%M") <= stop_at:
                last_min = now.strftime("%H:%M")  # scan once per minute, ~3s after the bar closes
                if not picks:
                    picks = pb_picks()  # premarket is over by now, so the picks are fixed for the day
                    if start == "09:30":  # daily heartbeat: if this doesn't arrive by ~9:35, the bot isn't running
                        notify("Bot is on", f"Watching TJR on {', '.join(INDEXES)} and PB on "
                               f"{picks.get('long', '-')} (long) / {picks.get('short', '-')} (short) until 3:44 PM."
                               + (" Approve buttons live." if trading else ""))
                scan(start, end, sent, picks)
        except Exception as e:
            print("loop error:", e)
            errors += 1
            if errors == 3:  # a blip is normal; tell the phone if it keeps failing
                notify("Paper trader error", str(e)[:300])

"""TJR 4-step liquidity-sweep setup, coded for SPY/QQQ 1-minute bars (America/New_York).

1. Sweep: a 5m bar (09:30-10:29) trades through a liquidity level
   (previous-day RTH high/low, premarket high/low).
2. Reversal: a 5m candle closes through the latest 5m swing point (BOS)
   or through the latest opposing 5m fair value gap (inverse FVG).
3+4. Continuation: on the 1m chart, a pullback breaks structure against us
   (close beyond the latest 1m swing), then breaks back our way -> entry at that close.
Stop: beyond the pullback extreme. Target: nearest opposing liquidity >= 1R away, else 2R.

Only the short side is written; longs run the same code on price-mirrored bars
(prices negated, high<->low), so both sides follow identical rules.
"""
import pandas as pd

LAST_ENTRY = "15:44"   # TJR stops at ~10:30; we scan all day (last entry leaves time before the 15:55 exit)
STOP_BUFFER = 0.02
MIN_RR, FALLBACK_RR = 1.0, 2.0


def mirror(df):
    m = pd.DataFrame({"open": -df.open, "high": -df.low, "low": -df.high, "close": -df.close}, index=df.index)
    if "volume" in df:
        m["volume"] = df.volume
    return m


def pivots(h, l):
    """Swing highs/lows as lists of (index, price); a pivot at i is only known after bar i+1 closes."""
    # strict on the left, tie allowed on the right, so equal-low/high bars still form one swing
    ph = [(i, h[i]) for i in range(1, len(h) - 1) if h[i] > h[i - 1] and h[i] >= h[i + 1]]
    pl = [(i, l[i]) for i in range(1, len(l) - 1) if l[i] < l[i - 1] and l[i] <= l[i + 1]]
    return ph, pl


def last_known(piv, k):
    """Most recent pivot price confirmed by the close of bar k-1 (i.e. pivot index <= k-2)."""
    best = None
    for i, p in piv:
        if i + 1 <= k - 1:
            best = p
        else:
            break
    return best


def find_short(day, above, below):
    """day: today's 1m bars from 04:00. above/below: liquidity levels. Returns signal dict or None."""
    m5 = day.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    t5, h5, l5, c5 = m5.index, m5.high.values, m5.low.values, m5.close.values
    _, pl5 = pivots(h5, l5)
    bull_fvg = [(i, h5[i - 2]) for i in range(2, len(m5)) if l5[i] > h5[i - 2]]  # (index, gap bottom)

    swept, sweep_hi, confirm_end = False, None, None
    for i, t in enumerate(t5):
        if t.strftime("%H:%M") < "09:30":
            continue
        if t.strftime("%H:%M") > LAST_ENTRY:
            return None
        if not swept:
            if any(h5[i] > lv for lv in above):
                swept, sweep_hi = True, h5[i]
            continue
        sweep_hi = max(sweep_hi, h5[i])
        sl = last_known(pl5, i)
        fvg = last_known(bull_fvg, i + 1)  # an FVG is known as soon as its 3rd candle closes
        if (sl is not None and c5[i] < sl) or (fvg is not None and c5[i] < fvg):
            confirm_end = t + pd.Timedelta(minutes=5)
            break
    if confirm_end is None:
        return None

    t1, h1, l1, c1 = day.index, day.high.values, day.low.values, day.close.values
    ph1, pl1 = pivots(h1, l1)
    start = t1.searchsorted(confirm_end)
    pulled_back = False
    for k in range(start, len(t1)):
        if t1[k].strftime("%H:%M") > LAST_ENTRY or h1[k] > sweep_hi:
            return None  # too late, or the sweep high got taken out -> setup failed
        if not pulled_back:
            ph = last_known(ph1, k)
            pulled_back = ph is not None and c1[k] > ph
            continue
        pl = last_known(pl1, k)
        if pl is not None and c1[k] < pl:
            entry = c1[k]
            stop = h1[start:k + 1].max() + STOP_BUFFER
            risk = stop - entry
            # a 5m swing low only counts once the 5m bar after it has closed (no peeking ahead)
            done = t1[k] + pd.Timedelta(minutes=1)
            pool = sorted(list(below) + [p for i, p in pl5 if t5[i] + pd.Timedelta(minutes=10) <= done], reverse=True)
            target = next((lv for lv in pool if entry - lv >= MIN_RR * risk), entry - FALLBACK_RR * risk)
            return {"time": t1[k], "bar": k, "entry": entry, "stop": stop, "target": target}
    return None


def levels(prev_day, day):
    rth_prev = prev_day.between_time("09:30", "15:59")
    pre = day.between_time("04:00", "09:29")
    hi = [rth_prev.high.max()] + ([pre.high.max()] if len(pre) else [])
    lo = [rth_prev.low.min()] + ([pre.low.min()] if len(pre) else [])
    return hi, lo


def find_signal(prev_day, day):
    """Earliest short or long setup today. Prices returned in real (unmirrored) terms."""
    hi, lo = levels(prev_day, day)
    s = find_short(day, hi, lo)
    if s:
        s["side"] = "short"
    l = find_short(mirror(day), [-x for x in lo], [-x for x in hi])
    if l:
        l.update(side="long", entry=-l["entry"], stop=-l["stop"], target=-l["target"])
    sigs = [x for x in (s, l) if x]
    return min(sigs, key=lambda x: x["time"]) if sigs else None


def simulate(day, sig, exit_time="15:55"):
    """R multiple of a signal: stop checked before target within a bar (conservative)."""
    d = day if sig["side"] == "short" else mirror(day)
    entry, stop, target = (sig["entry"], sig["stop"], sig["target"]) if sig["side"] == "short" else \
        (-sig["entry"], -sig["stop"], -sig["target"])
    risk = stop - entry
    for t, row in d.iloc[sig["bar"] + 1:].iterrows():
        if row.high >= stop:
            return -1.0
        if row.low <= target:
            return (entry - target) / risk
        if t.strftime("%H:%M") >= exit_time:
            return (entry - row.close) / risk
    return (entry - d.close.iloc[-1]) / risk


# ---------------------------------------------------------------------------
# PB Invest: premarket high/low break-and-retest, with VWAP + 9 EMA filter (5m chart).
# Long: a 5m candle closes above the premarket high; within 30 min price taps back to the
# level and holds it (1m close above), while above VWAP and the 5m 9 EMA -> buy.
# Stop just under the level; target 2R (PB: "at least 1:2"). Shorts mirror at the premarket low.
PB_TOL = 0.0005        # "tap" = within 0.05% of the level (~$0.35 on SPY); PB says as close as possible
PB_STOP = 0.001        # stop 0.1% through the level (stand-in for his "5m close back through it")
PB_RR = 2.0
PB_WINDOW = ("09:35", "11:30")   # his examples are all in the morning
PB_RETEST_MIN = 30


def find_pb_long(day, level):
    if level is None or pd.isna(level):
        return None
    rth = day.between_time("09:30", "15:59")
    if rth.empty:
        return None
    tp = (rth.high + rth.low + rth.close) / 3
    vwap = ((tp * rth.volume).cumsum() / rth.volume.cumsum()).values
    m5 = day.resample("5min", label="left", closed="left").agg({"close": "last"}).dropna()
    ema = m5.close.ewm(span=9, adjust=False).mean().values
    c5, t5 = m5.close.values, m5.index
    five = pd.Timedelta(minutes=5)

    armed = next((t + five for t, c in zip(t5, c5) if t.strftime("%H:%M") >= "09:30" and c > level), None)
    if armed is None:
        return None
    t1, l1, c1 = rth.index, rth.low.values, rth.close.values
    for k in range(t1.searchsorted(armed), len(t1)):
        t = t1[k]
        if t >= armed + pd.Timedelta(minutes=PB_RETEST_MIN) or t.strftime("%H:%M") > PB_WINDOW[1]:
            return None
        j = t5.searchsorted(t + pd.Timedelta(minutes=1) - five, side="right") - 1  # last closed 5m bar
        if c5[j] < level:
            return None  # closed back below the level: setup failed
        if t.strftime("%H:%M") < PB_WINDOW[0]:
            continue
        if l1[k] <= level * (1 + PB_TOL) and c1[k] > level and c1[k] > vwap[k] and c5[j] > ema[j]:
            entry = c1[k]
            stop = level * (1 - PB_STOP)
            return {"time": t, "bar": day.index.get_loc(t), "entry": entry, "stop": stop,
                    "target": entry + PB_RR * (entry - stop)}
    return None


def find_pb(prev_day, day, side=None):
    """side='long' / 'short' restricts to one direction (PB: calls on gainers, puts on losers)."""
    pre = day.between_time("04:00", "09:29")
    if pre.empty:
        return None
    lg = find_pb_long(day, pre.high.max()) if side != "short" else None
    if lg:
        lg["side"] = "long"
    sh = find_pb_long(mirror(day), -pre.low.min()) if side != "long" else None
    if sh:
        sh.update(side="short", entry=-sh["entry"], stop=-sh["stop"], target=-sh["target"])
    sigs = [x for x in (lg, sh) if x]
    return min(sigs, key=lambda x: x["time"]) if sigs else None


STRATEGIES = {"TJR": find_signal, "PB": find_pb}

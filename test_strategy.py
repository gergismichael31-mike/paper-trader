"""Synthetic textbook short: sweep premarket/PDH, 5m BOS down, 1m pullback up, 1m BOS down."""
import os
import numpy as np
import pandas as pd
from strategy import find_signal, mirror, simulate


def path(day, pts):
    """1m bars through (HH:MM, price) waypoints, linear between them."""
    idx = pd.date_range(f"{day} {pts[0][0]}", f"{day} {pts[-1][0]}", freq="1min", tz="America/New_York")
    mins = lambda t: (t - idx[0]).total_seconds() / 60
    x = [mins(pd.Timestamp(f"{day} {t}", tz="America/New_York")) for t, _ in pts]
    c = np.interp([mins(t) for t in idx], x, [p for _, p in pts])
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + .02, "low": np.minimum(o, c) - .02, "close": c, "volume": 1000.0}, index=idx)


prev = path("2026-11-19", [("09:30", 100), ("12:00", 101), ("15:59", 99.5)])
day = path("2026-11-20", [("04:00", 100), ("07:00", 100.5), ("09:29", 100.2),
                          ("09:30", 100.3), ("09:36", 100.0), ("09:46", 101.4), ("09:50", 100.6), ("09:51", 100.75),
                          ("09:57", 99.6), ("10:01", 100.3), ("10:03", 100.0), ("10:06", 100.9), ("10:12", 99.9),
                          ("11:30", 99.0), ("15:59", 99.2)])

s = find_signal(prev, day)
assert s and s["side"] == "short", s
assert s["stop"] > s["entry"] > s["target"] and s["time"].strftime("%H:%M") <= "10:29", s
assert simulate(day, s) > 0, "price falls to 99 afterwards, short should win"

m = mirror(day)
l = find_signal(mirror(prev), m)
assert l and l["side"] == "long" and abs(l["entry"] + s["entry"]) < 1e-9 and l["time"] == s["time"], l
assert simulate(m, l) > 0
print("ok", s)

# PB: premarket high 100.5; 5m closes above it, pulls back to tap 100.5 while above VWAP/EMA, then runs.
from strategy import find_pb
pbday = path("2026-11-20", [("04:00", 100), ("07:00", 100.5), ("09:29", 100.2), ("09:30", 100.2),
                            ("09:40", 101.2), ("09:48", 100.52), ("09:52", 101.6), ("11:00", 102.5), ("15:59", 102.4)])
p = find_pb(prev, pbday)
assert p and p["side"] == "long" and p["stop"] < 100.5 < p["entry"] < p["target"], p
assert simulate(pbday, p) == 2.0, simulate(pbday, p)
pm = find_pb(mirror(prev), mirror(pbday))
assert pm and pm["side"] == "short" and abs(pm["entry"] + p["entry"]) < 1e-9 and pm["time"] == p["time"], pm
assert pm["stop"] > pm["entry"] > pm["target"] and abs(pm["stop"] + p["stop"]) < 1e-9, pm  # short stop above entry
assert simulate(mirror(pbday), pm) == 2.0
print("pb ok", p)

# Approve-time price check (live.rr_left): today's NVDA short fill at the target must be refused.
os.environ.setdefault("NTFY_TOPIC", "test")
import live
nv = {"entry": 142.71, "stop": 142.88, "target": 142.38}
assert live.rr_left(nv, 142.38) == 0           # already at target -> refuse
assert live.rr_left(nv, 142.90) == 0           # past the stop -> refuse
assert live.rr_left(nv, 142.71) > 1.9          # at entry -> ~2x, place
assert live.rr_left(nv, 142.55) < 1.0          # halfway to target -> refuse
lg = {"entry": 100.0, "stop": 99.0, "target": 102.0}
assert live.rr_left(lg, 100.2) > 1.0 and live.rr_left(lg, 101.5) < 1.0
print("rr_left ok")

# Early-close times: a 13:00 close moves last entry / flatten / score to 12:44 / 12:55 / 12:57.
assert [live.shift("13:00", m) for m in (-16, -5, -3)] == ["12:44", "12:55", "12:57"]
assert min("15:44", live.shift("13:00", -16)) == "12:44" and "12:30" <= "12:44"
print("early close ok")

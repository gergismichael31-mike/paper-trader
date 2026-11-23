"""Synthetic textbook short: sweep premarket/PDH, 5m BOS down, 1m pullback up, 1m BOS down."""
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
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + .02, "low": np.minimum(o, c) - .02, "close": c}, index=idx)


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

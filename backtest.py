"""Usage: .venv/bin/python backtest.py [days] [SYM ...]   (Yahoo: max ~29 days; Alpaca keys: years)"""
import sys
from datetime import datetime, timedelta
import pandas as pd
import data
from strategy import STRATEGIES, simulate

days = int(sys.argv[1]) if len(sys.argv) > 1 else 29
symbols = sys.argv[2:] or ["SPY", "QQQ"]
end = datetime.now().astimezone() - timedelta(minutes=20)
start = end - timedelta(days=days)
rows = []
for sym in symbols:
    sessions = data.by_day(data.load(sym, start, end))
    for (_, prev), (d, day) in zip(sessions, sessions[1:]):
        if len(day.between_time("09:30", "15:59")) < 300:  # skip half days / gaps in data
            continue
        for name, find in STRATEGIES.items():
            sig = find(prev, day)
            if sig:
                rows.append({"strategy": name, "date": d, "sym": sym, "side": sig["side"],
                             "time": sig["time"].strftime("%H:%M"), "entry": round(sig["entry"], 2),
                             "stop": round(sig["stop"], 2), "target": round(sig["target"], 2),
                             "R": round(simulate(day, sig), 2)})

t = pd.DataFrame(rows)
if t.empty:
    sys.exit("No setups found.")
t.to_csv("backtest_results.csv", index=False)
summary = t.groupby(["strategy", "sym"]).R.agg(trades="count", win=lambda r: f"{(r > 0).mean():.0%}", total="sum")
summary["total"] = summary.total.map("{:+.1f}R".format)
print(summary.to_string())
for name, g in t.groupby("strategy"):
    print(f"{name}: {len(g)} trades | win {(g.R > 0).mean():.0%} | total {g.R.sum():+.1f}R | {g.R.mean():+.2f}R/trade")

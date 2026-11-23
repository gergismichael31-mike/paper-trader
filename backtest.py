"""Usage: .venv/bin/python backtest.py [days]   (Yahoo: max ~29 days; Alpaca keys: years)"""
import sys
from datetime import datetime, timedelta
import pandas as pd
import data
from strategy import find_signal, simulate

days = int(sys.argv[1]) if len(sys.argv) > 1 else 29
end = datetime.now().astimezone() - timedelta(minutes=20)
start = end - timedelta(days=days)
rows = []
for sym in ["SPY", "QQQ"]:
    sessions = data.by_day(data.load(sym, start, end))
    for (_, prev), (d, day) in zip(sessions, sessions[1:]):
        if len(day.between_time("09:30", "15:59")) < 300:  # skip half days / gaps in data
            continue
        sig = find_signal(prev, day)
        if sig:
            rows.append({"date": d, "sym": sym, "side": sig["side"], "time": sig["time"].strftime("%H:%M"),
                         "entry": round(sig["entry"], 2), "stop": round(sig["stop"], 2),
                         "target": round(sig["target"], 2), "R": round(simulate(day, sig), 2)})

t = pd.DataFrame(rows)
if t.empty:
    sys.exit("No setups found.")
print(t.to_string(index=False))
wins = t[t.R > 0]
print(f"\n{len(t)} trades | win rate {len(wins)/len(t):.0%} | avg win {wins.R.mean():.2f}R | "
      f"avg loss {t[t.R <= 0].R.mean():.2f}R | total {t.R.sum():+.1f}R | expectancy {t.R.mean():+.2f}R/trade")
t.to_csv("backtest_results.csv", index=False)

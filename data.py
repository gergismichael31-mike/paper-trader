"""1-minute bars in America/New_York with columns open/high/low/close, incl. premarket.
Uses Alpaca when keys are in .env, otherwise Yahoo (last ~30 days only)."""
import os
from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
NY = "America/New_York"
KEY, SECRET = os.getenv("ALPACA_KEY"), os.getenv("ALPACA_SECRET")


def _clean(df):
    df = df[["open", "high", "low", "close"]].copy()
    df.index = pd.DatetimeIndex(df.index).tz_convert(NY)
    return df.sort_index()[~df.index.duplicated()]


def load(symbol, start, end, live=False):
    if KEY and SECRET:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
        # Free plan: SIP (all exchanges) for history older than 15 min, IEX for real-time.
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end,
                               feed=DataFeed.IEX if live else DataFeed.SIP)
        df = StockHistoricalDataClient(KEY, SECRET).get_stock_bars(req).df
        return _clean(df.droplevel(0)) if len(df) else df
    import yfinance as yf
    parts = []
    s = start
    while s < end:  # Yahoo serves 1m data in <=7-day chunks
        e = min(s + timedelta(days=7), end)
        parts.append(yf.Ticker(symbol).history(start=s, end=e, interval="1m", prepost=True))
        s = e
    df = pd.concat(parts)
    df.columns = [c.lower() for c in df.columns]
    return _clean(df)


def by_day(df):
    return [(d, g) for d, g in df.groupby(df.index.date)]

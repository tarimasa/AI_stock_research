"""
data_fetcher.py
yfinance を使い銘柄の株価・テクニカル指標・ファンダメンタルズを取得する。
"""

import os
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def fetch_ohlcv(ticker: str, days: int = 90) -> pd.DataFrame:
    """直近 days 日分の日足 OHLCV データを返す。"""
    end = datetime.today()
    start = end - timedelta(days=days + 10)  # 余裕を持って取得
    t = yf.Ticker(ticker)
    df = t.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
    df = df.dropna(subset=["Close"])
    return df.tail(days)


def fetch_info(ticker: str) -> dict:
    """ticker.info から PER/PBR/配当利回りなどを返す。"""
    t = yf.Ticker(ticker)
    try:
        return t.info
    except Exception:
        return {}


def _calc_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def fetch_current_price(ticker: str) -> float:
    """
    リアルタイム（または直近）株価を取得する。
    fast_info → 1分足 → 日足 の順にフォールバックする。
    """
    t = yf.Ticker(ticker)
    try:
        price = t.fast_info.last_price
        if price and float(price) > 0:
            return float(price)
    except Exception:
        pass
    try:
        df = t.history(period="1d", interval="1m")
        if not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    # 最終フォールバック: 日足
    df = fetch_ohlcv(ticker, days=5)
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


def fetch_stock_data_with_df(ticker: str) -> tuple[dict, pd.DataFrame]:
    """
    fetch_stock_data と同じ結果を返すが、計算に使った OHLCV DataFrame も返す。
    呼び出し元が同じ df を MACD 等の追加計算に再利用でき、二重取得を避けられる。
    """
    if DRY_RUN:
        return _dummy_stock_data(ticker), pd.DataFrame()

    df = fetch_ohlcv(ticker, days=90)
    info = fetch_info(ticker)

    if df.empty:
        return {"code": ticker, "error": "no data"}, df

    close = df["Close"]
    current_price = fetch_current_price(ticker)
    if current_price <= 0:
        current_price = float(close.iloc[-1])
    prev_price = float(close.iloc[-1])
    change_pct = (current_price - prev_price) / prev_price * 100

    rsi_14 = _calc_rsi(close, 14)

    ma25 = float(close.tail(25).mean()) if len(close) >= 25 else current_price
    ma75 = float(close.tail(75).mean()) if len(close) >= 75 else current_price
    ma25_diff_pct = (current_price - ma25) / ma25 * 100
    ma75_diff_pct = (current_price - ma75) / ma75 * 100

    vol = df["Volume"]
    avg_vol_5 = float(vol.tail(5).mean()) if len(vol) >= 5 else float(vol.iloc[-1])
    volume_ratio = float(vol.iloc[-1]) / avg_vol_5 if avg_vol_5 > 0 else 1.0

    return {
        "code": ticker,
        "price": round(current_price, 2),
        "change_pct": round(change_pct, 2),
        "per": info.get("trailingPE"),
        "pbr": info.get("priceToBook"),
        "dividend_yield": round((info.get("dividendYield") or 0) * 100, 2),
        "rsi_14": round(rsi_14, 1),
        "ma25_diff_pct": round(ma25_diff_pct, 2),
        "ma75_diff_pct": round(ma75_diff_pct, 2),
        "volume_ratio": round(volume_ratio, 2),
        "week52_high": info.get("fiftyTwoWeekHigh"),
        "week52_low": info.get("fiftyTwoWeekLow"),
    }, df


def fetch_stock_data(ticker: str) -> dict:
    """
    Returns:
    {
        "code": "7203.T",
        "price": 2850,
        "change_pct": 1.2,
        "per": 8.2,
        "pbr": 1.1,
        "dividend_yield": 2.8,
        "rsi_14": 38.5,
        "ma25_diff_pct": -2.1,
        "ma75_diff_pct": 3.4,
        "volume_ratio": 1.35,
        "week52_high": 3200,
        "week52_low": 2100,
    }
    """
    data, _ = fetch_stock_data_with_df(ticker)
    return data


def fetch_market_data() -> dict:
    """日経平均・ドル円などマクロ指標を返す。日経の25日SMAトレンドも含む。"""
    if DRY_RUN:
        return {
            "nikkei": 38500,
            "nikkei_change": -0.5,
            "usdjpy": 148.5,
            "nikkei_sma25": 38000,
            "nikkei_vs_sma25_pct": 1.3,
            "nikkei_trend": "上昇",
        }
    nikkei_data = fetch_stock_data("^N225")
    usdjpy_data = fetch_stock_data("USDJPY=X")

    # 日経25日移動平均との乖離でトレンドを判定
    nikkei_sma25 = 0
    nikkei_vs_sma25_pct = 0.0
    nikkei_trend = "不明"
    try:
        nikkei_df = fetch_ohlcv("^N225", days=30)
        if len(nikkei_df) >= 25:
            nikkei_sma25 = round(float(nikkei_df["Close"].tail(25).mean()))
            nikkei_price = nikkei_data.get("price", 0)
            if nikkei_sma25 > 0:
                nikkei_vs_sma25_pct = round((nikkei_price - nikkei_sma25) / nikkei_sma25 * 100, 2)
                nikkei_trend = "上昇" if nikkei_price >= nikkei_sma25 else "下落"
    except Exception as e:
        print(f"[data_fetcher] 日経SMA25取得失敗: {e}")

    return {
        "nikkei": nikkei_data.get("price", 0),
        "nikkei_change": nikkei_data.get("change_pct", 0),
        "usdjpy": usdjpy_data.get("price", 0),
        "nikkei_sma25": nikkei_sma25,
        "nikkei_vs_sma25_pct": nikkei_vs_sma25_pct,
        "nikkei_trend": nikkei_trend,
    }


def _dummy_stock_data(ticker: str) -> dict:
    """DRY_RUN 用のダミーデータ。"""
    return {
        "code": ticker,
        "price": 2850.0,
        "change_pct": 1.2,
        "per": 12.5,
        "pbr": 1.1,
        "dividend_yield": 2.8,
        "rsi_14": 38.5,
        "ma25_diff_pct": -5.2,
        "ma75_diff_pct": 3.4,
        "volume_ratio": 1.35,
        "week52_high": 3200.0,
        "week52_low": 2100.0,
    }

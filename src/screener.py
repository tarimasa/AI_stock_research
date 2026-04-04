"""
screener.py
スコアリングにより Claude API に渡す銘柄を上位 MAX_STOCKS 本に絞る。
pandas_ta を使ってテクニカル指標を計算する。
"""

import os

import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv

from data_fetcher import fetch_info, fetch_ohlcv

load_dotenv()

MAX_STOCKS = int(os.environ.get("MAX_STOCKS_TO_ANALYZE", 10))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def score_stock(df: pd.DataFrame, info: dict) -> float:
    """
    df: yfinance から取得した日足 OHLCV データ（直近 90 日分）
    info: yfinance ticker.info（PER, PBR 等）
    Returns: 0〜110 のスコア
    """
    if df.empty or len(df) < 26:
        return 0.0

    score = 0.0

    # テクニカル指標の計算（pandas_ta で統一）
    df = df.copy()
    df.ta.rsi(length=14, append=True)
    df.ta.macd(append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.sma(length=25, append=True)
    df.ta.sma(length=75, append=True)

    latest = df.iloc[-1]
    close = float(latest["Close"])

    rsi = float(latest.get("RSI_14", 50) or 50)
    macd = float(latest.get("MACD_12_26_9", 0) or 0)
    macd_signal = float(latest.get("MACDs_12_26_9", 0) or 0)
    bb_lower = float(latest.get("BBL_20_2.0", 0) or 0)
    sma25 = float(latest.get("SMA_25", close) or close)
    # sma75 は 75 日未満のデータでは NaN になる場合がある
    sma75_raw = latest.get("SMA_75")
    sma75 = float(sma75_raw) if sma75_raw and not pd.isna(sma75_raw) else close

    ma25_diff_pct = (close - sma25) / sma25 * 100 if sma25 else 0

    # RSI スコア（40点）
    if 25 <= rsi <= 35:
        score += 40
    elif 35 < rsi <= 50:
        score += 25
    elif 50 < rsi <= 65:
        score += 10
    # RSI > 70 は除外（過熱圏）

    # MA 乖離スコア（25点）
    if -8 <= ma25_diff_pct <= -4:
        score += 25
    elif -4 < ma25_diff_pct <= -2:
        score += 15
    elif ma25_diff_pct > 0:
        score += 5

    # MACD シグナル（20点）
    if macd > macd_signal:
        score += 20
    elif macd > 0:
        score += 10

    # ボリンジャーバンド（15点）
    if bb_lower and close <= bb_lower:
        score += 15

    # ファンダメンタルズボーナス（最大10点）
    per = info.get("trailingPE") or 999
    pbr = info.get("priceToBook") or 999
    if per < 15:
        score += 5
    if pbr < 1.5:
        score += 5

    return score


def screen(stocks: list, market_data: dict | None = None) -> list:
    """
    watchlist の全銘柄をスコアリングし上位 MAX_STOCKS 件を返す。
    market_data を渡すと日経トレンドによるフィルターが有効になる。
    """
    if DRY_RUN:
        return _dummy_screen(stocks)

    nikkei_trend = (market_data or {}).get("nikkei_trend", "不明")
    nikkei_vs_sma25 = (market_data or {}).get("nikkei_vs_sma25_pct", 0)

    # 日経が25日線を下回っている場合: スコアペナルティ＋最大銘柄数を半減
    in_downtrend = (nikkei_trend == "下落")
    max_stocks = max(1, MAX_STOCKS // 2) if in_downtrend else MAX_STOCKS
    score_multiplier = 0.7 if in_downtrend else 1.0

    if in_downtrend:
        print(f"[screener] ⚠️ 日経下落トレンド（SMA25比 {nikkei_vs_sma25:+.1f}%）: "
              f"最大銘柄数 {MAX_STOCKS}→{max_stocks}、スコア×{score_multiplier}")

    scored = []
    for stock in stocks:
        try:
            df = fetch_ohlcv(stock["code"])
            info = fetch_info(stock["code"])
            s = score_stock(df, info) * score_multiplier
            if s > 0:
                scored.append({**stock, "score": round(s, 1)})
        except Exception as e:
            print(f"[screener] {stock['code']} スキップ: {e}")

    return sorted(scored, key=lambda x: x["score"], reverse=True)[:max_stocks]


def _dummy_screen(stocks: list) -> list:
    """DRY_RUN 用のダミースクリーニング結果。"""
    dummy_scores = [45, 40, 35, 30, 25, 20, 15, 10, 5, 0]
    result = []
    for i, stock in enumerate(stocks):
        score = dummy_scores[i] if i < len(dummy_scores) else 0
        if score > 0:
            result.append({**stock, "score": score})
    return result[:MAX_STOCKS]

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


def score_stock(df: pd.DataFrame, info: dict) -> tuple[float, dict]:
    """
    df: yfinance から取得した日足 OHLCV データ（直近 252 日分）
    info: yfinance ticker.info（PER, PBR, 配当利回り等）
    Returns: (スコア 0〜175, 追加シグナル dict)
    """
    if df.empty or len(df) < 26:
        return 0.0, {}

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
    sma75_raw = latest.get("SMA_75")
    sma75 = float(sma75_raw) if sma75_raw and not pd.isna(sma75_raw) else close

    ma25_diff_pct = (close - sma25) / sma25 * 100 if sma25 else 0

    # ── 既存スコア ──────────────────────────────────

    # RSI スコア（40点）: 売られすぎ圏を高評価
    if 25 <= rsi <= 35:
        score += 40
    elif 35 < rsi <= 50:
        score += 25
    elif 50 < rsi <= 65:
        score += 10
    # RSI > 70 は加点なし（過熱圏）

    # MA 乖離スコア（25点）: 25日線を少し下回った押し目
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

    # ボリンジャーバンド（15点）: 下限タッチで反発期待
    if bb_lower and close <= bb_lower:
        score += 15

    # ファンダメンタルズ（最大10点）
    per = info.get("trailingPE") or 999
    pbr = info.get("priceToBook") or 999
    if per < 15:
        score += 5
    if pbr < 1.5:
        score += 5

    # ── 追加スコア（新規） ──────────────────────────

    # 出来高比率（最大20点）: 20日平均比で今日の出来高が急増 → 資金流入シグナル
    vol_ratio = 1.0
    if "Volume" in df.columns and len(df) >= 20:
        vol_mean20 = df["Volume"].iloc[-21:-1].mean()
        if vol_mean20 > 0:
            vol_ratio = float(latest["Volume"]) / vol_mean20
        if vol_ratio >= 2.0:
            score += 20  # 2倍以上の急増出来高
        elif vol_ratio >= 1.5:
            score += 12
        elif vol_ratio >= 1.2:
            score += 6

    # 52週安値圏スコア（最大25点）: 年間安値に近いほど割安
    week52_pos = 0.5  # デフォルト中央
    if len(df) >= 60:
        high52 = df["High"].max()
        low52 = df["Low"].min()
        if high52 > low52:
            week52_pos = (close - low52) / (high52 - low52)
        if week52_pos < 0.20:
            score += 25  # 52週安値圏20%以内
        elif week52_pos < 0.35:
            score += 15
        elif week52_pos < 0.50:
            score += 8

    # 配当利回りボーナス（最大15点）: 高配当は値下がりリスク軽減
    div_yield = info.get("dividendYield") or 0
    if div_yield >= 0.04:
        score += 15  # 4%以上
    elif div_yield >= 0.03:
        score += 10  # 3%以上
    elif div_yield >= 0.02:
        score += 5   # 2%以上

    # 75日線との位置（10点）: 25日線は下 × 75日線は上 = 短期調整の押し目
    if sma75 and close < sma25 and close > sma75:
        score += 10

    extra_signals = {
        "vol_ratio": round(vol_ratio, 2),
        "week52_pos_pct": round(week52_pos * 100, 1),
        "div_yield_pct": round(div_yield * 100, 2),
        "ma25_diff_pct": round(ma25_diff_pct, 1),
    }

    return score, extra_signals


def screen(stocks: list, market_data: dict | None = None) -> list:
    """
    watchlist の全銘柄をスコアリングし上位 MAX_STOCKS 件を返す。
    market_data を渡すと日経トレンドによるフィルターが有効になる。
    重複コードを自動除去してから処理する。
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

    # 重複コードを除去
    seen_codes: set = set()
    unique_stocks = []
    for s in stocks:
        if s["code"] not in seen_codes:
            seen_codes.add(s["code"])
            unique_stocks.append(s)

    scored = []
    for stock in unique_stocks:
        try:
            # 52週スコア用に250日分取得（既存の90日から拡張）
            df = fetch_ohlcv(stock["code"], days=252)
            info = fetch_info(stock["code"])
            s, extra = score_stock(df, info)
            s *= score_multiplier
            if s > 0:
                scored.append({**stock, "score": round(s, 1), **extra})
        except Exception as e:
            print(f"[screener] {stock['code']} スキップ: {e}")

    return sorted(scored, key=lambda x: x["score"], reverse=True)[:max_stocks]


def _dummy_screen(stocks: list) -> list:
    """DRY_RUN 用のダミースクリーニング結果。"""
    dummy_scores = [95, 80, 70, 60, 55, 45, 40, 35, 25, 15]
    result = []
    seen: set = set()
    for i, stock in enumerate(stocks):
        if stock["code"] in seen:
            continue
        seen.add(stock["code"])
        score = dummy_scores[len(result)] if len(result) < len(dummy_scores) else 0
        if score > 0:
            result.append({
                **stock,
                "score": score,
                "vol_ratio": 1.4,
                "week52_pos_pct": 28.0,
                "div_yield_pct": 2.5,
                "ma25_diff_pct": -3.2,
            })
    return result[:MAX_STOCKS]

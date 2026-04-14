"""
screener.py
スコアリングにより Claude API に渡す銘柄を上位 MAX_STOCKS 本に絞る。
pandas_ta を使ってテクニカル指標を計算する。
ThreadPoolExecutor による並列フェッチで 50〜100 銘柄を高速処理。
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv

from data_fetcher import fetch_info, fetch_ohlcv
import jquants_fetcher

load_dotenv()

MAX_STOCKS = int(os.environ.get("MAX_STOCKS_TO_ANALYZE", 10))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
# 並列ワーカー数: yfinance のレートリミットを考慮して 6 に設定。
# 環境変数 SCREENER_WORKERS で上書き可能。
SCREENER_WORKERS = int(os.environ.get("SCREENER_WORKERS", 6))


def score_stock(df: pd.DataFrame, info: dict, market_data: dict | None = None) -> tuple[float, dict]:
    """
    df: yfinance から取得した日足 OHLCV データ（直近 252 日分）
    info: yfinance ticker.info（PER, PBR, 配当利回り等）
    market_data: fetch_market_data() の結果（nikkei_return_20d, vix 等）
    Returns: (スコア 0〜190, 追加シグナル dict)
    """
    if df.empty or len(df) < 26:
        return 0.0, {}

    score = 0.0

    # テクニカル指標の計算（pandas_ta で統一）
    df = df.copy()
    df.ta.rsi(length=14, append=True)
    df.ta.rsi(length=5, append=True)   # 短期RSI: 1〜3日反転シグナルに有効
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

    # 配当利回り: スコアには使わない（短期売買が目的のため保持前提スコアは除外）
    # ただし表示用に正規化して保持する
    # バグ修正: yfinanceが日本株で百分率(5.59)を返す場合があるため小数に正規化
    div_yield_raw = info.get("dividendYield") or 0
    if div_yield_raw > 1.0:
        # 5.59% → 0.0559 に補正（百分率で返ってきた場合の防御）
        div_yield_raw /= 100

    # 75日線との位置（10点）: 25日線は下 × 75日線は上 = 短期調整の押し目
    if sma75 and close < sma25 and close > sma75:
        score += 10

    # ── 決算発表日接近スコア（-10〜+12点）──────────────────────────
    # 決算前後のドリフト現象（Pre-Earnings Announcement Drift）:
    #   好業績期待が高い銘柄では決算発表の1〜3週前から先行買いが入る傾向がある。
    #   ただし直前（5日以内）はサプライズリスクでギャップが大きくなるため減点。
    earnings_score = 0
    days_to_earnings = None
    earnings_ts = info.get("earningsTimestampStart") or info.get("earningsTimestamp")
    if earnings_ts:
        try:
            earnings_date = datetime.fromtimestamp(int(earnings_ts)).date()
            days_to_earnings = (earnings_date - date.today()).days
            if 1 <= days_to_earnings <= 5:
                earnings_score = -10   # 直前リスク: ギャップ大・ポジション取りにくい
            elif 6 <= days_to_earnings <= 20:
                earnings_score = 12    # 先行買いフェーズ: ドリフト恩恵
            elif 21 <= days_to_earnings <= 45:
                earnings_score = 5     # 早期ポジション: まだ上昇余地あり
            # 0以下（決算後）や46日以上先はスコアなし
            score += earnings_score
        except Exception:
            pass

    # ── 配当権利確定日接近スコア（最大25点）────────────────────────
    # 短期売買での活用理由:
    #   権利付最終日の1〜2週前は機関投資家・個人投資家の「配当取り買い」で
    #   統計的に有意な上昇バイアスが確認されている（Kato & Loewenstein 1995、
    #   日本株の ex-day 前後の超過リターン研究）。
    #   ※目的は配当受取ではなく、この「買い需要」を短期的に利用すること。
    #   権利落ち後は買い圧力が消え価格調整が入るため、権利付最終日前後での売却を推奨。
    # yfinance の exDividendDate は「権利落ち日」に相当（日本では権利付最終日の翌営業日）
    days_to_ex = None
    ex_div_score = 0
    ex_date_ts = info.get("exDividendDate")
    if ex_date_ts:
        try:
            if hasattr(ex_date_ts, "date"):
                ex_date = ex_date_ts.date()
            else:
                ex_date = datetime.fromtimestamp(int(ex_date_ts)).date()
            days_to_ex = (ex_date - date.today()).days
            # 権利付最終日 = 権利落ち日の前営業日なので -1 で近似
            days_to_last_buy = days_to_ex - 1
            if 3 <= days_to_last_buy <= 10:
                ex_div_score = 25   # 権利直前1〜2週: 買い需要ピーク
            elif 11 <= days_to_last_buy <= 20:
                ex_div_score = 18   # 2〜4週前: 機関の仕込み本格化
            elif 21 <= days_to_last_buy <= 35:
                ex_div_score = 10   # 1〜1.5ヶ月前: 先行買い開始
            # 0以下（権利落ち後）や36日以上先はスコアなし
            score += ex_div_score
        except Exception:
            pass

    # ── 日経225比 相対強度スコア（最大15点）──────────────────────────
    # 日経がx%下落した中で当該銘柄がより小さい下落 or 上昇 = 底堅さ → 反発期待
    # 日経より大幅アンダーパフォーム（-5〜-15%）= 押し目・キャッチアップ余地
    rel_strength = None
    rel_strength_score = 0
    nikkei_return_20d = (market_data or {}).get("nikkei_return_20d", 0)
    if len(df) >= 20:
        p_now = float(df["Close"].iloc[-1])
        p_20d = float(df["Close"].iloc[-20])
        stock_return_20d = (p_now - p_20d) / p_20d * 100 if p_20d > 0 else 0
        rel_strength = round(stock_return_20d - nikkei_return_20d, 1)
        # 小幅〜中幅アンダーパフォーム = 押し目でキャッチアップ期待
        if -15 <= rel_strength <= -5:
            rel_strength_score = 15
        elif -5 < rel_strength < 0:
            rel_strength_score = 8
        elif 0 <= rel_strength <= 5:
            rel_strength_score = 3
        # rel_strength < -15: 何か問題がある可能性 → 加点なし
        # rel_strength > 5: 既に先行している → 追いかけない
        score += rel_strength_score

    # ── 短期売買特化シグナル（1〜3日の勝率向上） ──────────────────────────
    # 既存スコアは「割安かつ反転余地あり」を示すが、「いつ反転するか」は示さない。
    # 以下のシグナルは「今まさに動き始めているか」を捉える短期モメンタム系。

    # 方向性出来高スコア（最大+20、最大-10）:
    #   上昇日の出来高急増 = 機関の買い集め（Accumulation）→ 強気
    #   下落日の出来高急増 = 売り浴びせ（Distribution） → 弱気でペナルティ
    directional_vol_score = 0
    if len(df) >= 2:
        prev_close_dv = float(df["Close"].iloc[-2])
        if close > prev_close_dv and vol_ratio >= 1.5:
            directional_vol_score = 20   # 上昇日 + 出来高急増: 買い集めシグナル
        elif close > prev_close_dv and vol_ratio >= 1.2:
            directional_vol_score = 10   # 上昇日 + 出来高増加
        elif close < prev_close_dv and vol_ratio >= 1.5:
            directional_vol_score = -10  # 下落日の急増出来高: 分散売りシグナル
    score += directional_vol_score

    # 短期RSIスコア（最大30点）:
    #   5日RSIは14日RSIより遥かに敏感で、1〜3日の反転タイミングに直結する。
    #   RSI14=35（軽い売られすぎ）でも RSI5=15（超売られすぎ）なら直近の急落後の反発期待が高い。
    rsi5 = float(latest.get("RSI_5", 50) or 50)
    rsi5_score = 0
    if rsi5 <= 20:
        rsi5_score = 30   # 超売られすぎ: 強い短期反転シグナル
    elif rsi5 <= 30:
        rsi5_score = 20   # 売られすぎ: 反転期待大
    elif rsi5 <= 40:
        rsi5_score = 10   # 軽度売られすぎ
    score += rsi5_score

    # 5日高値ブレイクアウトスコア（最大30点）:
    #   直近5日間の高値を出来高を伴って上抜けた = 短期レジスタンス突破。
    #   「反発待ち」ではなく「モメンタムが始まった」ことを示す最も信頼性の高い短期シグナル。
    breakout_score = 0
    if len(df) >= 6:
        high5d = df["High"].iloc[-6:-1].max()   # 昨日までの5日高値
        if close > high5d and vol_ratio >= 1.3:
            breakout_score = 30   # 高値ブレイク + 出来高増加: モメンタム確認
        elif close > high5d:
            breakout_score = 15   # 高値ブレイク（出来高なし）: 弱いが陽転シグナル
    score += breakout_score

    # 足型シグナルスコア（最大20点）:
    #   前日の足型で翌日以降の短期反転を予測する先行シグナル。
    #   陽線包み足（Bullish Engulfing）: 売り圧力を完全に上回った強気サイン。
    #   ハンマー足（Hammer）: 下ひげが長く底打ち感を示す。
    candle_score = 0
    candle_pattern = "none"
    if len(df) >= 2:
        prev = df.iloc[-2]
        prev_open = float(prev["Open"])
        prev_close2 = float(prev["Close"])
        prev_high = float(prev["High"])
        prev_low = float(prev["Low"])
        prev_body = abs(prev_close2 - prev_open)
        if prev_body > 0:
            prev_lower_wick = min(prev_close2, prev_open) - prev_low
            prev_upper_wick = prev_high - max(prev_close2, prev_open)
            # 陽線包み足: 前日陰線 × 当日終値が前日始値を超え × 当日始値が前日終値未満
            is_bullish_engulfing = (
                prev_close2 < prev_open and          # 前日陰線
                close > prev_open and                 # 当日終値 > 前日始値
                float(latest["Open"]) < prev_close2   # 当日始値 < 前日終値
            )
            # ハンマー足: 下ひげ ≥ 実体×2 かつ 上ひげ ≤ 実体×0.5
            is_hammer = (
                prev_lower_wick >= prev_body * 2 and
                prev_upper_wick <= prev_body * 0.5
            )
            if is_bullish_engulfing:
                candle_score = 20
                candle_pattern = "bullish_engulfing"
            elif is_hammer:
                candle_score = 15
                candle_pattern = "hammer"
    score += candle_score

    extra_signals = {
        "vol_ratio": round(vol_ratio, 2),
        "week52_pos_pct": round(week52_pos * 100, 1),
        "days_to_ex_dividend": days_to_ex,       # 権利落ち日まで（負=過去、Noneは不明）
        "div_yield_pct": round(div_yield_raw * 100, 2),  # 表示用のみ（スコア対象外）
        "ma25_diff_pct": round(ma25_diff_pct, 1),
        "rel_strength_vs_nikkei": rel_strength,  # 日経比20日相対強度（%）
        "days_to_earnings": days_to_earnings,    # 決算発表まで（負=過去、Noneは不明）
        # 短期売買特化シグナル（追加）
        "directional_vol_score": directional_vol_score,   # 方向性出来高（正=買い/負=売り）
        "rsi5": round(rsi5, 1),                           # 5日RSI（短期感応度）
        "breakout_5d": breakout_score > 0,                # 5日高値ブレイクアウト
        "candle_pattern": candle_pattern,                 # 足型シグナル
        "sma25": round(sma25, 1),                         # 25日移動平均（price_calculator が使用）
    }

    return score, extra_signals


def _fetch_and_score(stock: dict, score_multiplier: float, market_data: dict | None = None) -> dict | None:
    """単一銘柄のデータ取得・スコアリング（ThreadPoolExecutor から呼ばれる）。"""
    try:
        df = fetch_ohlcv(stock["code"], days=252)
        info = fetch_info(stock["code"])
        s, extra = score_stock(df, info, market_data)

        # J-Quants 財務成長スコアを追加（APIキー設定時のみ有効）
        fin_score, fin_desc = jquants_fetcher.get_financial_growth_score(stock["code"])
        s += fin_score
        extra["financial_growth_desc"] = fin_desc  # Claudeへの説明文（空文字=データなし）

        s *= score_multiplier
        if s > 0:
            return {**stock, "score": round(s, 1), **extra}
    except Exception as e:
        print(f"[screener] {stock['code']} スキップ: {e}")
    return None


def screen(stocks: list, market_data: dict | None = None) -> list:
    """
    watchlist の全銘柄を並列スコアリングし上位 MAX_STOCKS 件を返す。

    処理時間の目安（SCREENER_WORKERS=6 の場合）:
      ~30銘柄  → 約 20〜40 秒
      ~50銘柄  → 約 30〜60 秒
      ~100銘柄 → 約 60〜120 秒
    いずれも GitHub Actions の 30 分タイムアウト以内に収まる。

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

    print(f"[screener] {len(unique_stocks)}銘柄を並列スクリーニング開始 "
          f"(workers={SCREENER_WORKERS})")

    scored = []
    with ThreadPoolExecutor(max_workers=SCREENER_WORKERS) as executor:
        futures = {
            executor.submit(_fetch_and_score, stock, score_multiplier, market_data): stock
            for stock in unique_stocks
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                scored.append(result)

    result_sorted = sorted(scored, key=lambda x: x["score"], reverse=True)[:max_stocks]
    print(f"[screener] スクリーニング完了: {len(scored)}/{len(unique_stocks)}銘柄スコアあり → "
          f"上位{len(result_sorted)}銘柄をClaudeに渡す")
    return result_sorted


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
                "days_to_ex_dividend": 14,
                "div_yield_pct": 2.5,
                "ma25_diff_pct": -3.2,
                "rel_strength_vs_nikkei": -6.8,
                "days_to_earnings": 12,
                "financial_growth_desc": "",
                "directional_vol_score": 10,
                "rsi5": 28.5,
                "breakout_5d": False,
                "candle_pattern": "none",
                "sma25": 2820.0,
            })
    return result[:MAX_STOCKS]

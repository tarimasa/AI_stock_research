"""
screener.py
スコアリングにより Claude API に渡す銘柄を上位 MAX_STOCKS 本に絞る。
pandas_ta を使ってテクニカル指標を計算する。
ThreadPoolExecutor による並列フェッチで 50〜100 銘柄を高速処理。
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv

# J-Quants データ取得（yfinance の fetch_ohlcv / fetch_info を置き換え）
from data_fetcher import fetch_bulk_daily, load_bulk_history, fetch_info
import jquants_fetcher

# Layer 2: データ強化モジュール
from earnings_signal import get_upcoming_earnings
from master_manager import get_master

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
    if rsi5 <= 10:
        rsi5_score = 40   # 極端売られすぎ: バックテストEV最高シグナル
    elif rsi5 <= 20:
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


def _fetch_and_score(
    stock: dict,
    score_multiplier: float,
    market_data: dict | None = None,
    upcoming_earnings: dict | None = None,
) -> dict | None:
    """
    J-Quants OHLCV で単一銘柄をスコアリングする（ThreadPoolExecutor から呼ばれる）。
    yfinance の fetch_ohlcv / fetch_info を load_bulk_history / earnings_signal に置き換え。
    """
    try:
        code_raw = stock["code"]  # "7203.T" 形式
        code4 = code_raw.replace(".T", "").replace(".t", "").strip()[:4]

        # J-Quants OHLCV（bulk CSV 優先、なければ API、さらに yfinance フォールバック）
        df = load_bulk_history(code4, days=252)

        # info 辞書の構築:
        # JQUANTS_API_KEY 未設定時は yfinance から PER/PBR/権利落ち日/配当を補完する。
        # これにより GitHub Actions（J-Quants 使用）との採点ロジックを統一する。
        info: dict = {}
        if not os.environ.get("JQUANTS_API_KEY"):
            try:
                info = fetch_info(f"{code4}.T")
            except Exception:
                pass

        if upcoming_earnings:
            earn_info = upcoming_earnings.get(code4)
            if earn_info:
                try:
                    earn_date = datetime.strptime(
                        earn_info["earnings_date"], "%Y-%m-%d"
                    )
                    info["earningsTimestamp"] = int(earn_date.timestamp())
                except (ValueError, KeyError):
                    pass

        s, extra = score_stock(df, info, market_data)

        # J-Quants 財務成長スコアを追加（APIキー設定時のみ有効）
        fin_score, fin_desc = jquants_fetcher.get_financial_growth_score(code_raw)
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
    ウォッチリスト銘柄を J-Quants OHLCV で並列スコアリングし上位 MAX_STOCKS 件を返す。

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

    # 決算カレンダーを事前取得（全スレッドで共有する）
    upcoming_earnings: dict = {}
    try:
        upcoming_earnings = get_upcoming_earnings(days_ahead=45)
        print(f"[screener] 決算情報取得: {len(upcoming_earnings)}銘柄")
    except Exception as e:
        print(f"[screener] 決算情報取得失敗（続行）: {e}")

    print(f"[screener] {len(unique_stocks)}銘柄を並列スクリーニング開始 "
          f"(workers={SCREENER_WORKERS})")

    scored = []
    with ThreadPoolExecutor(max_workers=SCREENER_WORKERS) as executor:
        futures = {
            executor.submit(
                _fetch_and_score, stock, score_multiplier, market_data, upcoming_earnings
            ): stock
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


# ─────────────────────────────────────────────────────────────────────────────
# ヘルパー: テクニカル指標ユーティリティ（全銘柄スキャンで利用）
# ─────────────────────────────────────────────────────────────────────────────

def calc_breakout_5d(hist: pd.DataFrame) -> bool:
    """直近5日高値（前日まで）を当日終値が上抜けたかどうかを返す。"""
    if len(hist) < 6:
        return False
    high5d = hist["High"].iloc[-6:-1].max()
    close = float(hist["Close"].iloc[-1])
    return close > high5d


def calc_directional_vol_score(hist: pd.DataFrame) -> float:
    """
    方向性出来高スコアを返す（-100〜+100 程度）。
    直近5日間の上昇日出来高と下落日出来高の差を、5日平均出来高×5 で正規化。
    正 = 買い越し傾向、負 = 売り越し傾向。
    """
    if len(hist) < 5:
        return 0.0
    last5 = hist.tail(5).copy()
    changes = last5["Close"].diff()
    up_vol = float(last5["Volume"][changes > 0].sum())
    down_vol = float(last5["Volume"][changes < 0].sum())
    avg_vol_5 = float(last5["Volume"].mean())
    if avg_vol_5 == 0:
        return 0.0
    return (up_vol - down_vol) / (avg_vol_5 * 5) * 100


def _find_code_column(df: pd.DataFrame) -> str:
    """DataFrame のコード列名を検索して返す。"""
    for col in ["Code", "code", "CODE"]:
        if col in df.columns:
            return col
    return df.columns[0]


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 / Phase 3: 全銘柄スキャン（Stage 1 フィルタ）
# ─────────────────────────────────────────────────────────────────────────────

def _exclude_non_targets(df: pd.DataFrame, master: dict) -> pd.DataFrame:
    """
    短期トレードに不適切な銘柄（ETF・低流動性・ボロ株等）を除外する。
    ~3,900銘柄 → ~1,500〜2,500 銘柄に絞り込む。
    """
    ETF_REIT_MARKETS = {
        "ETF・ETN",
        "REIT・ベンチャーファンド・カントリーファンド・インフラファンド",
    }
    exclude_codes: set = set()
    code_col = _find_code_column(df)

    for _, row in df.iterrows():
        raw_code = str(row.get(code_col, "")).strip()
        code4 = raw_code[:4]
        if not code4.isdigit():
            exclude_codes.add(code4)
            continue

        info = master.get(code4, {})
        market = info.get("market", "")
        sector = info.get("sector33", "")
        volume = float(row.get("Volume", 0) or 0)
        close = float(row.get("Close", 0) or 0)

        if market in ETF_REIT_MARKETS or "ETF" in sector or "REIT" in sector:
            exclude_codes.add(code4)
        elif volume < 50_000:
            exclude_codes.add(code4)
        elif close < 100 or close > 50_000:
            exclude_codes.add(code4)
        elif close * volume < 5_000_000:
            exclude_codes.add(code4)

    mask = ~df[code_col].astype(str).str[:4].isin(exclude_codes)
    return df[mask].copy()


def _load_all_bulk_history() -> pd.DataFrame | None:
    """
    data/bulk/ 以下の全 bulk CSV を読み込み、long-form DataFrame を返す。
    ファイルが存在しない場合は None を返す。
    """
    bulk_dir = Path(__file__).parent.parent / "data" / "bulk"
    if not bulk_dir.exists():
        return None

    frames = []
    for csv_file in sorted(bulk_dir.glob("daily_*.csv")):
        try:
            chunk = pd.read_csv(csv_file, dtype={"Code": str})
            frames.append(chunk)
        except Exception as e:
            print(f"[screener] bulk CSV 読み込みエラー {csv_file.name}: {e}")

    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True)
    date_col = next((c for c in combined.columns if "date" in c.lower()), None)
    if date_col:
        combined[date_col] = pd.to_datetime(combined[date_col])
        if date_col != "Date":
            combined = combined.rename(columns={date_col: "Date"})

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in combined.columns:
            combined[col] = pd.to_numeric(combined[col], errors="coerce")

    return combined


def _calc_technicals_vectorized(bulk_df: pd.DataFrame) -> pd.DataFrame:
    """
    全銘柄のテクニカル指標を groupby + rolling で一括計算する（Layer 5 / 5-2 最適化）。
    個別ループ比で約30倍高速。

    Args:
        bulk_df: long-form DataFrame (Code, Date, Open, High, Low, Close, Volume)

    Returns:
        銘柄別の最新テクニカル指標 DataFrame
    """
    df = bulk_df.sort_values(["Code", "Date"]).copy()

    def _rsi_series(series: pd.Series, length: int) -> pd.Series:
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0).rolling(length, min_periods=length).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(length, min_periods=length).mean()
        rs = gain / loss.replace(0, float("nan"))
        return 100.0 - (100.0 / (1.0 + rs))

    grouped = df.groupby("Code", group_keys=False)

    df["rsi5"] = grouped["Close"].transform(lambda x: _rsi_series(x, 5))
    df["rsi14"] = grouped["Close"].transform(lambda x: _rsi_series(x, 14))
    df["sma25"] = grouped["Close"].transform(
        lambda x: x.rolling(25, min_periods=25).mean()
    )
    df["avg_vol_20"] = grouped["Volume"].transform(
        lambda x: x.rolling(20, min_periods=5).mean()
    )

    # 5日高値ブレイク（前日までの5日間）
    df["high_5d_prev"] = grouped["High"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=5).max()
    )
    df["breakout_5d"] = df["Close"] > df["high_5d_prev"]

    # 52週ポジション
    df["high_52w"] = grouped["High"].transform(
        lambda x: x.rolling(252, min_periods=60).max()
    )
    df["low_52w"] = grouped["Low"].transform(
        lambda x: x.rolling(252, min_periods=60).min()
    )
    w52_range = (df["high_52w"] - df["low_52w"]).replace(0, float("nan"))
    df["w52_pos"] = ((df["Close"] - df["low_52w"]) / w52_range * 100).fillna(50.0)

    # 方向性出来高スコア（5日）
    df["change"] = grouped["Close"].transform(lambda x: x.diff())
    df["up_vol"] = df["Volume"].where(df["change"] > 0, 0.0)
    df["down_vol"] = df["Volume"].where(df["change"] < 0, 0.0)
    df["up_vol_5"] = grouped["up_vol"].transform(
        lambda x: x.rolling(5, min_periods=1).sum()
    )
    df["down_vol_5"] = grouped["down_vol"].transform(
        lambda x: x.rolling(5, min_periods=1).sum()
    )
    avg_vol_5 = grouped["Volume"].transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    )
    denom = (avg_vol_5 * 5).replace(0, float("nan"))
    df["dvs"] = ((df["up_vol_5"] - df["down_vol_5"]) / denom * 100).fillna(0.0)

    # 出来高比率
    df["vol_ratio"] = (
        df["Volume"] / df["avg_vol_20"].replace(0, float("nan"))
    ).fillna(1.0)

    # 最新行のみ抽出
    latest = df.groupby("Code").tail(1).copy()
    keep = ["Code", "Close", "Volume", "rsi5", "rsi14", "sma25",
            "breakout_5d", "dvs", "vol_ratio", "w52_pos"]
    available = [c for c in keep if c in latest.columns]
    result = latest[available].rename(columns={"Close": "close", "Volume": "volume"})
    result = result.copy()
    result["code"] = result["Code"].astype(str).str[:4]
    return result


def _calc_technicals_individual(
    today_df: pd.DataFrame,
    master: dict,
    max_workers: int = 6,
) -> list[dict]:
    """
    銘柄ごとに過去データを個別取得してテクニカル指標を計算する（フォールバック）。
    bulk CSV が存在しない場合に使用。ThreadPool で並列処理。
    """
    code_col = _find_code_column(today_df)

    pre_candidates = [
        {
            "code": str(row.get(code_col, ""))[:4],
            "close": float(row.get("Close", 0) or 0),
            "volume": float(row.get("Volume", 0) or 0),
        }
        for _, row in today_df.iterrows()
        if str(row.get(code_col, ""))[:4].isdigit() and float(row.get("Close", 0) or 0) > 0
    ]

    def _calc_one(pc: dict) -> dict | None:
        code4 = pc["code"]
        try:
            hist = load_bulk_history(code4, days=60)
            if hist.empty or len(hist) < 20:
                return None

            rsi5_s = ta.rsi(hist["Close"], length=5)
            rsi14_s = ta.rsi(hist["Close"], length=14)
            sma25_s = ta.sma(hist["Close"], length=25)

            rsi5 = float(rsi5_s.iloc[-1]) if rsi5_s is not None and len(rsi5_s) > 0 else 50.0
            rsi14 = float(rsi14_s.iloc[-1]) if rsi14_s is not None and len(rsi14_s) > 0 else 50.0
            sma25 = float(sma25_s.iloc[-1]) if sma25_s is not None and len(sma25_s) > 0 else pc["close"]

            breakout = calc_breakout_5d(hist)
            dvs = calc_directional_vol_score(hist)

            avg_vol_20 = float(hist["Volume"].tail(20).mean()) if len(hist) >= 20 else pc["volume"]
            vol_ratio = pc["volume"] / avg_vol_20 if avg_vol_20 > 0 else 1.0

            high_52w = float(hist["High"].max())
            low_52w = float(hist["Low"].min())
            w52_range = high_52w - low_52w
            w52_pos = (pc["close"] - low_52w) / w52_range * 100 if w52_range > 0 else 50.0

            info_m = master.get(code4, {})
            return {
                "code": code4,
                "name": info_m.get("name", ""),
                "sector": info_m.get("sector33", ""),
                "close": pc["close"],
                "volume": pc["volume"],
                "rsi5": round(rsi5, 1),
                "rsi14": round(rsi14, 1),
                "sma25": round(sma25, 1),
                "breakout_5d": breakout,
                "dvs": round(dvs, 1),
                "vol_ratio": round(vol_ratio, 2),
                "w52_pos": round(w52_pos, 1),
            }
        except Exception as e:
            print(f"[screener] テクニカル計算エラー {code4}: {e}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_calc_one, pc): pc for pc in pre_candidates}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                results.append(result)
    return results


def _calc_technicals_for_fullscan(
    filtered_df: pd.DataFrame,
    master: dict,
) -> list[dict]:
    """
    ベクトル化（bulk CSV 優先）→ 個別取得（フォールバック）でテクニカルを計算する。
    """
    bulk_df = _load_all_bulk_history()

    if bulk_df is not None and not bulk_df.empty:
        print(f"[screener] ベクトル化テクニカル計算 ({len(bulk_df)}行)")
        tech_df = _calc_technicals_vectorized(bulk_df)

        code_col = _find_code_column(filtered_df)
        today_codes = set(filtered_df[code_col].astype(str).str[:4].tolist())

        results = []
        for _, row in tech_df.iterrows():
            code4 = str(row.get("code", ""))[:4]
            if code4 not in today_codes:
                continue
            info_m = master.get(code4, {})
            results.append({
                "code": code4,
                "name": info_m.get("name", ""),
                "sector": info_m.get("sector33", ""),
                "close": float(row.get("close", 0) or 0),
                "volume": float(row.get("volume", 0) or 0),
                "rsi5": round(float(row.get("rsi5", 50) or 50), 1),
                "rsi14": round(float(row.get("rsi14", 50) or 50), 1),
                "sma25": round(float(row.get("sma25", 0) or 0), 1),
                "breakout_5d": bool(row.get("breakout_5d", False)),
                "dvs": round(float(row.get("dvs", 0) or 0), 1),
                "vol_ratio": round(float(row.get("vol_ratio", 1) or 1), 2),
                "w52_pos": round(float(row.get("w52_pos", 50) or 50), 1),
            })
        return results
    else:
        print("[screener] bulk CSV なし。個別取得にフォールバック。")
        return _calc_technicals_individual(filtered_df, master)


def _apply_stage1_filters(stocks: list[dict]) -> list[dict]:
    """
    テクニカル条件でフィルタし stage1_score と stage1_signals を付与する。
    スコア > 0 の銘柄のみ通過。
    """
    passed = []

    for s in stocks:
        score = 0.0
        signals: list[str] = []

        breakout = bool(s.get("breakout_5d", False))
        dvs = float(s.get("dvs", 0) or 0)
        rsi5 = float(s.get("rsi5", 50) or 50)
        rsi14 = float(s.get("rsi14", 50) or 50)
        vol_ratio = float(s.get("vol_ratio", 1) or 1)
        w52_pos = float(s.get("w52_pos", 50) or 50)
        close_val = float(s.get("close", 0) or 0)
        sma25_val = float(s.get("sma25", 0) or 0)

        # 短期シグナル（最重要）
        if breakout and dvs > 0 and rsi5 <= 20:
            score += 120
            signals.append("breakout+dvs正+rsi5<20(最優秀)")
        elif breakout and dvs > 0 and rsi5 <= 30:
            score += 80
            signals.append("breakout+dvs正+rsi5低")
        elif breakout and dvs > 0:
            score += 50
            signals.append("breakout+dvs正")
        elif breakout:
            # DVSが正でなくてもブレイクアウト単独で加点（下落相場での押し目反発を拾う）
            score += 20
            signals.append("breakout")

        # 方向性出来高（DVS単独での加点: バックテストでDVS>20がEV+0.506%）
        if dvs > 20:
            score += 30
            signals.append(f"DVS={dvs:.0f}(強い買い越し)")
        elif dvs > 10:
            score += 20
            signals.append(f"DVS={dvs:.0f}(買い越し)")
        elif dvs > 0:
            score += 10
            signals.append(f"DVS={dvs:.0f}(弱い買い越し)")

        # 出来高急増（1.3倍まで緩和）
        if vol_ratio >= 2.0:
            score += 40
            signals.append(f"出来高{vol_ratio:.1f}倍")
        elif vol_ratio >= 1.5:
            score += 20
            signals.append(f"出来高{vol_ratio:.1f}倍")
        elif vol_ratio >= 1.3:
            score += 10
            signals.append(f"出来高{vol_ratio:.1f}倍")

        # 52週安値圏（40%まで緩和）
        if w52_pos <= 20:
            score += 30
            signals.append(f"52w安値圏{w52_pos:.0f}%")
        elif w52_pos <= 40:
            score += 15
            signals.append(f"52w安値圏{w52_pos:.0f}%")

        # RSI売られすぎ（40まで緩和）
        if rsi14 <= 25:
            score += 25
            signals.append(f"RSI14={rsi14:.0f}")
        elif rsi14 <= 35:
            score += 15
            signals.append(f"RSI14={rsi14:.0f}")
        elif rsi14 <= 40:
            score += 8
            signals.append(f"RSI14={rsi14:.0f}")

        # RSI5 売られすぎ（バックテスト最優秀シグナル: RSI5<20+出来高1.5倍 EV+0.708%）
        if rsi5 <= 10:
            score += 60
            signals.append(f"RSI5={rsi5:.0f}(極端売られすぎ)")
        elif rsi5 <= 20:
            score += 40
            signals.append(f"RSI5={rsi5:.0f}(超売られすぎ)")
        elif rsi5 <= 30:
            score += 15
            signals.append(f"RSI5={rsi5:.0f}(売られすぎ)")

        # RSI5<20 + 出来高1.5倍 = バックテスト最高EV複合シグナル
        if rsi5 <= 20 and vol_ratio >= 1.5:
            score += 30
            signals.append("RSI5<20+出来高急増(複合最優秀)")

        # SMA25押し目（下落相場でも拾える）
        if sma25_val > 0 and close_val > 0:
            sma25_diff = (close_val - sma25_val) / sma25_val * 100
            if -8 <= sma25_diff <= -2:
                score += 15
                signals.append(f"SMA25比{sma25_diff:.1f}%押し目")

        # 弱気シグナルはペナルティ（事実上除外）
        if dvs <= -10:
            score -= 200
            signals.append("dvs負(除外)")

        if score <= 0:
            continue

        s_copy = s.copy()
        s_copy["stage1_score"] = score
        s_copy["stage1_signals"] = signals
        passed.append(s_copy)

    return passed


def _apply_stage1_filters_relaxed(stocks: list[dict]) -> list[dict]:
    """
    通常フィルタで0件になった場合の緩和版フィルタ。スコア上位5件を返す。
    スコアが0でも全銘柄を並べて上位を返すため、必ず結果が出る。
    """
    for s in stocks:
        dvs = float(s.get("dvs", 0) or 0)
        rsi5 = float(s.get("rsi5", 50) or 50)
        rsi14 = float(s.get("rsi14", 50) or 50)
        vol_ratio = float(s.get("vol_ratio", 1) or 1)
        w52_pos = float(s.get("w52_pos", 50) or 50)
        breakout = bool(s.get("breakout_5d", False))
        score = 0.0
        signals = ["緩和フィルタ"]
        if dvs > 0:
            score += 20
            signals.append(f"DVS={dvs:.0f}")
        if rsi14 <= 45:
            score += 15
            signals.append(f"RSI14={rsi14:.0f}")
        if rsi5 <= 35:
            score += 10
            signals.append(f"RSI5={rsi5:.0f}")
        if vol_ratio >= 1.2:
            score += 10
            signals.append(f"出来高{vol_ratio:.1f}倍")
        if w52_pos <= 40:
            score += 10
            signals.append(f"52w={w52_pos:.0f}%")
        if breakout:
            score += 10
            signals.append("breakout")
        s["stage1_score"] = score
        s["stage1_signals"] = signals
    return sorted(stocks, key=lambda x: x["stage1_score"], reverse=True)[:5]


def run_full_scan(target_date: str | None = None) -> list[dict]:
    """
    全上場銘柄をスキャンし Stage 1 フィルタを通過した候補を返す（Layer 4 / Phase 3）。

    処理フロー:
        1. 全銘柄当日 OHLCV 一括取得（1 API コール）
        2. ETF・低流動性等を除外（~3,900 → ~2,000 銘柄）
        3. テクニカル指標をベクトル化で一括計算（bulk CSV 使用時は ~2秒）
        4. Stage 1 フィルタでスコアリング
        5. 上位10件を返す（Stage 2 トークン制限対策: 20 → 10 件）

    Returns:
        list[dict]: 最大10件の候補銘柄（Stage 2 への入力）
    """
    if DRY_RUN:
        return []

    print("[screener] 全銘柄スキャン開始...")

    # 1. 全銘柄当日データ一括取得（1 API コール）
    today_df = fetch_bulk_daily(date=target_date)
    if today_df is None or today_df.empty:
        print("[screener] bulk daily 取得失敗。フルスキャンをスキップ。")
        return []
    print(f"[screener] 当日データ取得: {len(today_df)}銘柄")

    # 2. 銘柄マスタ・決算カレンダーを取得
    master = get_master()
    upcoming_earnings = get_upcoming_earnings(days_ahead=45)
    print(f"[screener] マスタ: {len(master)}銘柄、決算予定: {len(upcoming_earnings)}銘柄")

    # 3. 対象外銘柄を除外
    filtered_df = _exclude_non_targets(today_df, master)
    print(f"[screener] 除外後: {len(filtered_df)}銘柄（{len(today_df) - len(filtered_df)}件除外）")

    # 4. テクニカル指標計算（ベクトル化 or 個別取得）
    scored = _calc_technicals_for_fullscan(filtered_df, master)
    print(f"[screener] テクニカル計算完了: {len(scored)}銘柄")

    if not scored:
        return []

    # 5. Stage 1 フィルタ適用
    filtered = _apply_stage1_filters(scored)
    print(f"[screener] Stage 1 通過: {len(filtered)}銘柄")

    # フォールバック: 0件なら緩和フィルタ
    if not filtered:
        print("[screener] 候補0件。緩和フィルタを適用。")
        filtered = _apply_stage1_filters_relaxed(scored)

    # 6. スコア降順 → 上位10件（Stage 2 トークン制限対策）
    result = sorted(filtered, key=lambda x: x["stage1_score"], reverse=True)[:10]
    print(f"[screener] フルスキャン完了: {len(result)}銘柄を Stage 2 に渡す")
    return result

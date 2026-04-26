"""
noon_screener.py
昼休み（11:30〜12:30 JST）中に実行する後場寄付向けスクリーナー。

設計:
  1. 既存の Stage1 スクリーニング（日足ベース）を実行して候補を得る。
  2. 候補各銘柄について本日前場（9:00〜11:30）の15分足を集計。
  3. 昼特化シグナルを計算してスコアを再計算。
  4. 上位 N 件を返す → Claude に後場寄付推奨を生成させる。

昼特化シグナル:
  - gap_break: 寄付ギャップの確認（+2%〜+5%の健全なギャップ）
  - morning_breakout: 前場高値が昨日高値を上抜け（モメンタム継続）
  - morning_reversal: 前場後半で下げ止まり反発（ハンマー型）
  - intraday_vol_surge: 前場出来高が20日平均の前場相当を上回る
  - gap_fade_risk: 寄付ギャップを埋めに行く動き（売り推奨寄り）
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from data_fetcher import get_morning_session_metrics

NOON_MAX_STOCKS = int(os.environ.get("NOON_MAX_STOCKS", 8))
NOON_WORKERS = int(os.environ.get("NOON_WORKERS", 4))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def _calc_noon_signals(stock: dict) -> dict:
    """
    Stage1 候補 1 銘柄に前場メトリクスを付与して昼スコアを計算する。

    Args:
        stock: Stage1 通過銘柄（close, sma25, rsi5 等を含む）

    Returns:
        stock に以下を追加した dict:
          - morning: get_morning_session_metrics() の戻り値
          - noon_score: 昼特化スコア
          - noon_signals: シグナル列（list[str]）
        前場データが取れなかった銘柄は None を返す（呼び出し側で除外）。
    """
    raw_code = str(stock.get("code", ""))
    code4 = raw_code.replace(".T", "")[:4]
    ticker = f"{code4}.T"

    morning = get_morning_session_metrics(ticker)
    if "error" in morning or not morning.get("open_9"):
        return None

    noon_score = 0.0
    signals: list[str] = []

    gap_pct = morning.get("gap_pct", 0) or 0
    morning_return = morning.get("morning_return_pct", 0) or 0
    range_pct = morning.get("range_pct", 0) or 0
    open_9 = morning.get("open_9", 0) or 0
    high = morning.get("high", 0) or 0
    low = morning.get("low", 0) or 0
    close_1130 = morning.get("close_1130", 0) or 0

    # ── 寄付ギャップ評価 ─────────────────────────────────────────
    # +2〜+5% = 健全な上昇ギャップ（モメンタム買い）
    # +5%超 = 過熱、寄付天井のリスク
    # -2〜0% = 無風、評価なし
    # -2%超下げ = 弱気ギャップ、推奨不可
    if 2.0 <= gap_pct <= 5.0:
        noon_score += 30
        signals.append(f"健全ギャップ+{gap_pct:.1f}%")
    elif 0.5 <= gap_pct < 2.0:
        noon_score += 15
        signals.append(f"小幅ギャップ+{gap_pct:.1f}%")
    elif gap_pct > 5.0:
        noon_score -= 10
        signals.append(f"過大ギャップ+{gap_pct:.1f}%(過熱)")
    elif gap_pct < -2.0:
        noon_score -= 40
        signals.append(f"弱気ギャップ{gap_pct:.1f}%(除外)")

    # ── 前場リターン評価 ─────────────────────────────────────────
    # 寄付→前引の値動きが買い方向 → 後場寄付買いでモメンタム継続狙い
    if morning_return >= 2.0:
        noon_score += 25
        signals.append(f"前場+{morning_return:.1f}%(強いモメンタム)")
    elif morning_return >= 0.5:
        noon_score += 15
        signals.append(f"前場+{morning_return:.1f}%(上昇)")
    elif morning_return <= -2.0:
        noon_score -= 20
        signals.append(f"前場{morning_return:.1f}%(下落)")

    # ── 前場レンジ評価 ─────────────────────────────────────────
    # レンジが広い + 引値が高値圏 = 強い買い（上ヒゲ短い）
    # レンジが広い + 引値が安値圏 = 売り優勢
    if range_pct > 0 and high > 0 and low > 0:
        # 前引値が前場高値からどれだけ戻っているか（0=高値近辺、1=安値近辺）
        pos_in_range = (high - close_1130) / (high - low) if (high - low) > 0 else 0.5
        if pos_in_range <= 0.3 and range_pct >= 1.5:
            noon_score += 20
            signals.append("前引高値圏(強い引け)")
        elif pos_in_range >= 0.7 and range_pct >= 1.5:
            noon_score -= 15
            signals.append("前引安値圏(弱い引け)")

    # ── 前場出来高スパイク評価 ─────────────────────────────────────────
    # 前場出来高が既に日足平均の50%を超えている = 1日換算で通常の倍以上の出来高
    morning_vol = morning.get("volume", 0) or 0
    daily_avg_vol = stock.get("volume", 0) or stock.get("Volume", 0) or 0
    if daily_avg_vol > 0:
        vol_ratio_morning = morning_vol / daily_avg_vol
        if vol_ratio_morning >= 0.7:
            noon_score += 25
            signals.append(f"前場出来高スパイク{vol_ratio_morning:.1f}x")
        elif vol_ratio_morning >= 0.5:
            noon_score += 15
            signals.append(f"前場出来高増加{vol_ratio_morning:.1f}x")

    # ── Stage1 ベーススコアを継承（25%の重みで加点）─────────────────────
    stage1_score = stock.get("stage1_score", 0) or 0
    noon_score += stage1_score * 0.25

    stock_out = {
        **stock,
        "morning": morning,
        "noon_score": round(noon_score, 1),
        "noon_signals": signals,
        # price フィールドを前場引値で上書き（後場寄付の価格計算で使用）
        "close": close_1130,
        "price": close_1130,
    }
    return stock_out


def apply_noon_filter(stage1_candidates: list[dict]) -> list[dict]:
    """
    Stage1 通過候補に前場データを付与し、昼スコア上位 NOON_MAX_STOCKS 件を返す。

    Args:
        stage1_candidates: screener.run_full_scan() または screener.screen() の結果。

    Returns:
        昼スコア降順の上位候補（最大 NOON_MAX_STOCKS 件）。
    """
    if not stage1_candidates:
        return []

    print(f"[noon_screener] {len(stage1_candidates)} 候補に前場データを付与中...")
    enriched: list[dict] = []

    with ThreadPoolExecutor(max_workers=NOON_WORKERS) as executor:
        futures = {
            executor.submit(_calc_noon_signals, s): s for s in stage1_candidates
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    enriched.append(result)
            except Exception as e:
                src = futures[future]
                print(f"[noon_screener] {src.get('code', '')} 前場取得失敗: {e}")

    # 弱気ギャップや負のスコアは除外
    filtered = [s for s in enriched if s.get("noon_score", 0) > 0]
    # スコア降順 + コード昇順で決定論的にソート（境界の同点銘柄を安定させる）
    result = sorted(
        filtered, key=lambda x: (-x.get("noon_score", 0), str(x.get("code", "")))
    )[:NOON_MAX_STOCKS]

    print(f"[noon_screener] 前場付与完了: {len(enriched)}銘柄 → 通過 {len(result)}銘柄")
    return result

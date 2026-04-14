"""
backtest_evaluator.py
backtest_logger.py に蓄積されたシグナルデータを分析し、
シグナル別勝率・期待値レポートを生成する。
"""

import sys
from pathlib import Path

# src/ 直下から直接呼ばれる場合のパス設定
sys.path.insert(0, str(Path(__file__).parent))

from backtest_logger import get_signal_stats, _load_log


def generate_evaluation_report() -> str:
    """
    シグナル別勝率をテキストレポートとして返す。
    LINE通知やコンソール出力に使用する。
    """
    stats = get_signal_stats()
    total = stats.get("total", 0)
    overall = stats.get("overall_win_rate")

    if total == 0:
        return "バックテストデータがまだ十分に蓄積されていません。"

    lines = [
        f"📊 バックテスト評価レポート",
        f"確定シグナル総数: {total}件",
        f"総合勝率: {overall}%" if overall is not None else "総合勝率: 集計中",
        "",
        "【シグナル別勝率】",
    ]

    by_signal = stats.get("by_signal", {})
    signal_labels = {
        "breakout_5d=true":    "5日高値ブレイク",
        "dvs_positive":        "方向性出来高(正)",
        "rsi5_le_30":          "RSI5≤30(売られすぎ)",
        "vol_ratio_ge_1.5":    "出来高1.5倍以上",
        "bullish_engulfing":   "陽線包み足",
        "hammer":              "ハンマー足",
        "short_term_holding":  "短期保有(≤3日)",
        "medium_term_holding": "中期保有(>3日)",
    }

    for key, label in signal_labels.items():
        s = by_signal.get(key)
        if not s:
            continue
        lines.append(
            f"  {label}: {s['win_rate']}% ({s['wins']}/{s['count']}件)"
        )

    # 有効シグナルの組み合わせ推奨
    lines.append("")
    lines.append("【高勝率シグナル組み合わせ (Top3)】")
    top3 = _get_top_combos()
    if top3:
        for i, combo in enumerate(top3[:3], 1):
            lines.append(f"  {i}. {combo['label']}: {combo['win_rate']}% ({combo['count']}件)")
    else:
        lines.append("  データ蓄積中...")

    return "\n".join(lines)


def _get_top_combos() -> list:
    """複合シグナルの勝率を計算して降順に返す。"""
    log = _load_log()
    decided = [e for e in log if e.get("outcome") in ("win", "loss")]
    if len(decided) < 5:
        return []

    combos = [
        {
            "label": "ブレイク+DVS正+RSI5≤30",
            "filter": lambda e: (
                e.get("signals", {}).get("breakout_5d") is True and
                (e.get("signals", {}).get("directional_vol_score") or 0) > 0 and
                (e.get("signals", {}).get("rsi5") or 999) <= 30
            ),
        },
        {
            "label": "ブレイク+出来高1.5倍",
            "filter": lambda e: (
                e.get("signals", {}).get("breakout_5d") is True and
                (e.get("signals", {}).get("vol_ratio") or 0) >= 1.5
            ),
        },
        {
            "label": "DVS正+RSI5≤30",
            "filter": lambda e: (
                (e.get("signals", {}).get("directional_vol_score") or 0) > 0 and
                (e.get("signals", {}).get("rsi5") or 999) <= 30
            ),
        },
        {
            "label": "陽線包み足+出来高増加",
            "filter": lambda e: (
                e.get("signals", {}).get("candle_pattern") == "bullish_engulfing" and
                (e.get("signals", {}).get("vol_ratio") or 0) >= 1.2
            ),
        },
        {
            "label": "短期+ブレイク",
            "filter": lambda e: (
                e.get("holding_days", 10) <= 3 and
                e.get("signals", {}).get("breakout_5d") is True
            ),
        },
    ]

    results = []
    for combo in combos:
        matched = [e for e in decided if combo["filter"](e)]
        if len(matched) < 3:
            continue
        wins = sum(1 for e in matched if e["outcome"] == "win")
        results.append({
            "label": combo["label"],
            "count": len(matched),
            "wins": wins,
            "win_rate": round(wins / len(matched) * 100, 1),
        })

    return sorted(results, key=lambda x: (x["win_rate"], x["count"]), reverse=True)


if __name__ == "__main__":
    print(generate_evaluation_report())

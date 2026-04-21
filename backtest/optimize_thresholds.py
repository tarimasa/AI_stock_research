#!/usr/bin/env python3
"""
backtest/optimize_thresholds.py

run_backtest.py の出力（results.csv）を使って閾値を最適化する。

【分析内容】
  1. 単一シグナルの最良レンジ特定（期待値最大化）
  2. 2シグナル組み合わせのグリッドサーチ（条件AND）
  3. Stage1スコアの最適カットオフ探索
  4. 月別・曜日別のパフォーマンス分析
  5. 現行スクリーナーとの比較と最適閾値提案

【使用方法】
  cd /home/user/AI_stock_research
  python backtest/optimize_thresholds.py

  # 出力ファイル
  data/backtest/optimal_thresholds.json  -- 提案する最適閾値
  data/backtest/combo_analysis.csv       -- 組み合わせ分析結果
  data/backtest/optimization_report.txt  -- テキストレポート
"""

import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "backtest"


# ── データ読み込み ─────────────────────────────────────────────────────────────

def load_results() -> pd.DataFrame:
    """run_backtest.py が出力した results.csv を読み込む。"""
    results_path = DATA_DIR / "results.csv"
    if not results_path.exists():
        print(f"[optimizer] results.csv が見つかりません: {results_path}")
        print("  先に run_backtest.py --analyze を実行してください。")
        sys.exit(1)

    df = pd.read_csv(results_path, encoding="utf-8-sig", dtype={"Code": str})
    df["Date"] = pd.to_datetime(df["Date"])

    # stage1_score > 0 のものだけ（シグナルが出た銘柄）
    df = df[df["stage1_score"] > 0].copy()
    print(
        f"[optimizer] データ読み込み: {len(df):,}件 "
        f"({df['Code'].nunique()}銘柄 × {df['Date'].nunique()}日)"
    )
    return df


# ── 基本統計ユーティリティ ────────────────────────────────────────────────────

def stats(df: pd.DataFrame, min_n: int = 1) -> dict | None:
    """基本統計量を計算する。n < min_n の場合は None を返す。"""
    n = len(df)
    if n < min_n:
        return None
    tp_rate  = (df["outcome"] == "tp").mean() * 100
    sl_rate  = (df["outcome"] == "sl").mean() * 100
    win_rate = (df["return_pct"] > 0).mean() * 100
    avg_ret  = float(df["return_pct"].mean())
    std_ret  = float(df["return_pct"].std()) if n > 1 else 0.0
    sharpe   = avg_ret / std_ret if std_ret > 0 else 0.0
    # 期待値スコア = 期待値 × log(サンプル数+1) でスケール感を調整
    ev_score = avg_ret * np.log1p(n)
    return {
        "n":         n,
        "tp_rate":   round(tp_rate, 2),
        "sl_rate":   round(sl_rate, 2),
        "win_rate":  round(win_rate, 2),
        "ev":        round(avg_ret, 4),
        "median":    round(float(df["return_pct"].median()), 4),
        "sharpe":    round(sharpe, 4),
        "ev_score":  round(ev_score, 4),
    }


# ── 1. 単一シグナル最適レンジ ─────────────────────────────────────────────────

# 現行スクリーナーの閾値（比較用）
CURRENT_THRESHOLDS = {
    "rsi14": {
        "desc": "RSI-14 売られすぎ（Stage1加点: ≤25→+25pt, ≤35→+15pt, ≤40→+8pt）",
        "good_range": (0, 40),
    },
    "rsi5": {
        "desc": "RSI-5 短期反転（Stage1加点: ≤20→+20pt, ≤30→+10pt）",
        "good_range": (0, 30),
    },
    "vol_ratio": {
        "desc": "出来高比率（Stage1加点: ≥2.0→+40pt, ≥1.5→+20pt, ≥1.3→+10pt）",
        "good_range": (1.3, 99),
    },
    "w52_pos": {
        "desc": "52週安値圏（Stage1加点: ≤20%→+30pt, ≤40%→+15pt）",
        "good_range": (0, 40),
    },
    "dvs": {
        "desc": "DVS（Stage1加点: >30→+20pt, >10→+10pt | 除外: ≤-10→-200pt）",
        "good_range": (0, 100),
    },
    "ma25_diff_pct": {
        "desc": "MA25乖離率（Stage1加点: -8〜-2%→+15pt）",
        "good_range": (-8, -2),
    },
    "stage1_score": {
        "desc": "Stage1スコアカットオフ（現行: >0）",
        "good_range": (0, 999),
    },
    "breakout_5d": {
        "desc": "5日高値ブレイクアウト",
        "good_range": (True, True),
    },
}


def analyze_single_signals(df: pd.DataFrame, min_n: int = 30) -> dict:
    """各シグナルの最適レンジを探索する。"""
    print("\n[optimizer] 単一シグナル分析...")
    results = {}

    # ── 連続値シグナル ──────────────────────────────────────────────────────
    signal_search_space: dict[str, list] = {
        "rsi14":        [5, 10, 15, 20, 25, 28, 30, 32, 35, 40, 45, 50],
        "rsi5":         [5, 10, 15, 18, 20, 22, 25, 28, 30, 35, 40],
        "vol_ratio":    [0.8, 1.0, 1.2, 1.3, 1.5, 1.8, 2.0, 2.5, 3.0],
        "w52_pos":      [10, 15, 20, 25, 30, 35, 40, 50, 60],
        "dvs":          [-30, -20, -10, 0, 5, 10, 15, 20, 30],
        "ma25_diff_pct": [-12, -8, -6, -4, -2, 0, 2, 5],
        "stage1_score": [0, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100],
    }

    for col, thresholds in signal_search_space.items():
        if col not in df.columns:
            continue

        col_results = []
        # "上限カットオフ" → col < threshold の場合
        for thr in thresholds:
            subset = df[df[col] < thr] if col in ("rsi14", "rsi5", "w52_pos") else df[df[col] >= thr]
            s = stats(subset, min_n)
            if s:
                s["threshold"] = thr
                s["direction"] = "below" if col in ("rsi14", "rsi5", "w52_pos") else "above"
                col_results.append(s)

        if col_results:
            best = max(col_results, key=lambda x: x["ev_score"])
            results[col] = {
                "all": col_results,
                "best": best,
                "current": CURRENT_THRESHOLDS.get(col, {}),
            }

    # ── ブレイクアウト ──────────────────────────────────────────────────────
    if "breakout_5d" in df.columns:
        s_true  = stats(df[df["breakout_5d"].astype(bool)], min_n)
        s_false = stats(df[~df["breakout_5d"].astype(bool)], min_n)
        results["breakout_5d"] = {"True": s_true, "False": s_false}

    return results


# ── 2. 2シグナル組み合わせグリッドサーチ ──────────────────────────────────────

def grid_search_combinations(df: pd.DataFrame, min_n: int = 50) -> pd.DataFrame:
    """
    2変数の閾値組み合わせを探索して最も期待値が高い条件を見つける。
    対象: stage1_score × (rsi5 or rsi14 or dvs or vol_ratio)
    """
    print("\n[optimizer] 組み合わせグリッドサーチ中...")

    # 探索するペアと閾値グリッド
    search_pairs = [
        # (col1, thresholds1, direction1, col2, thresholds2, direction2)
        ("stage1_score", [10, 20, 30, 40, 60, 80],    "above",
         "rsi5",         [15, 20, 25, 30, 40],         "below"),
        ("stage1_score", [10, 20, 30, 40, 60, 80],    "above",
         "rsi14",        [20, 25, 30, 35, 40],         "below"),
        ("stage1_score", [10, 20, 30, 40, 60, 80],    "above",
         "dvs",          [0, 5, 10, 20, 30],           "above"),
        ("stage1_score", [10, 20, 30, 40, 60, 80],    "above",
         "vol_ratio",    [1.2, 1.3, 1.5, 2.0],         "above"),
        ("rsi5",         [15, 20, 25, 30],             "below",
         "dvs",          [0, 5, 10, 20],               "above"),
        ("rsi14",        [25, 30, 35, 40],             "below",
         "vol_ratio",    [1.2, 1.3, 1.5, 2.0],         "above"),
        ("breakout_5d",  [True],                       "eq",
         "rsi5",         [15, 20, 25, 30],             "below"),
        ("breakout_5d",  [True],                       "eq",
         "vol_ratio",    [1.2, 1.3, 1.5, 2.0],         "above"),
    ]

    rows = []
    for col1, vals1, dir1, col2, vals2, dir2 in search_pairs:
        if col1 not in df.columns or col2 not in df.columns:
            continue
        for v1, v2 in product(vals1, vals2):
            # マスク構築
            if dir1 == "above":
                m1 = df[col1] >= v1
            elif dir1 == "below":
                m1 = df[col1] < v1
            else:  # eq
                m1 = df[col1].astype(bool) == v1

            if dir2 == "above":
                m2 = df[col2] >= v2
            elif dir2 == "below":
                m2 = df[col2] < v2
            else:
                m2 = df[col2].astype(bool) == v2

            subset = df[m1 & m2]
            s = stats(subset, min_n)
            if s:
                rows.append({
                    "col1":       col1,
                    "val1":       v1,
                    "dir1":       dir1,
                    "col2":       col2,
                    "val2":       v2,
                    "dir2":       dir2,
                    "condition":  f"{col1}{'>=' if dir1=='above' else '<'}{v1} & {col2}{'>=' if dir2=='above' else '<'}{v2}",
                    **s,
                })

    if not rows:
        print("  グリッドサーチ結果なし（データ不足の可能性）")
        return pd.DataFrame()

    combo_df = pd.DataFrame(rows).sort_values("ev_score", ascending=False)
    print(f"  {len(combo_df)}件の組み合わせを評価")
    return combo_df


# ── 3. Stage1スコア最適カットオフ ────────────────────────────────────────────

def optimize_score_cutoff(df: pd.DataFrame, min_n: int = 100) -> pd.DataFrame:
    """
    stage1_score の最適カットオフをスキャンする。
    カットオフを上げると精度は上がるがシグナル数が減る。
    """
    print("\n[optimizer] Stage1スコア最適カットオフ探索...")
    rows = []
    for cutoff in range(0, 151, 5):
        subset = df[df["stage1_score"] >= cutoff]
        s = stats(subset, min_n)
        if s:
            rows.append({"cutoff": cutoff, **s})

    cutoff_df = pd.DataFrame(rows) if rows else pd.DataFrame()
    if not cutoff_df.empty:
        best = cutoff_df.loc[cutoff_df["ev_score"].idxmax()]
        print(
            f"  最適カットオフ: {best['cutoff']} "
            f"(期待値={best['ev']:+.3f}%, n={best['n']:,})"
        )
    return cutoff_df


# ── 4. 時系列・曜日別分析 ─────────────────────────────────────────────────────

def analyze_temporal(df: pd.DataFrame, min_n: int = 30) -> dict:
    """月別・曜日別のパフォーマンスを分析する。"""
    print("\n[optimizer] 時系列分析...")
    result = {}

    # 月別
    df = df.copy()
    df["month"] = df["Date"].dt.to_period("M").astype(str)
    monthly = {}
    for month, grp in df.groupby("month"):
        s = stats(grp, min_n)
        if s:
            monthly[month] = s
    result["monthly"] = monthly

    # 曜日別
    df["weekday"] = df["Date"].dt.day_name()
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    weekly = {}
    for wd in weekday_order:
        grp = df[df["weekday"] == wd]
        s = stats(grp, min_n)
        if s:
            weekly[wd] = s
    result["weekday"] = weekly

    return result


# ── 5. 最適閾値の提案 ────────────────────────────────────────────────────────

def propose_optimal_rules(
    df: pd.DataFrame,
    single_results: dict,
    combo_df: pd.DataFrame,
    cutoff_df: pd.DataFrame,
    min_n: int = 50,
) -> dict:
    """
    分析結果をもとに最適な閾値を提案する。
    期待値 > 0 かつ n >= min_n の条件を採用。
    """
    proposals = {}

    # ── Stage1スコアカットオフ ────────────────────────────────────────────
    if not cutoff_df.empty:
        valid = cutoff_df[
            (cutoff_df["ev"] > 0) & (cutoff_df["n"] >= min_n)
        ]
        if not valid.empty:
            best = valid.loc[valid["ev_score"].idxmax()]
            proposals["stage1_score_cutoff"] = {
                "current":   0,
                "proposed":  int(best["cutoff"]),
                "ev":        best["ev"],
                "tp_rate":   best["tp_rate"],
                "sl_rate":   best["sl_rate"],
                "n":         int(best["n"]),
                "rationale": f"スコア >= {best['cutoff']} で期待値が最大化",
            }

    # ── 各シグナルの最良閾値 ──────────────────────────────────────────────
    signal_proposal_map = {
        "rsi14": {
            "current_desc": "RSI14 ≤ 40 で加点",
            "col": "rsi14", "direction": "below",
        },
        "rsi5": {
            "current_desc": "RSI5 ≤ 30 で加点",
            "col": "rsi5", "direction": "below",
        },
        "vol_ratio": {
            "current_desc": "出来高比率 ≥ 1.3 で加点",
            "col": "vol_ratio", "direction": "above",
        },
        "w52_pos": {
            "current_desc": "52週安値圏 ≤ 40% で加点",
            "col": "w52_pos", "direction": "below",
        },
        "dvs": {
            "current_desc": "DVS > 0 を条件（≤ -10 で除外）",
            "col": "dvs", "direction": "above",
        },
    }

    for key, info in signal_proposal_map.items():
        col = info["col"]
        if col not in single_results:
            continue
        all_results = single_results[col].get("all", [])
        valid = [
            r for r in all_results
            if r["ev"] > 0 and r["n"] >= min_n
        ]
        if not valid:
            continue
        best = max(valid, key=lambda x: x["ev_score"])
        proposals[key] = {
            "current_desc": info["current_desc"],
            "proposed_threshold": best["threshold"],
            "direction": best["direction"],
            "ev": best["ev"],
            "tp_rate": best["tp_rate"],
            "sl_rate": best["sl_rate"],
            "n": best["n"],
        }

    # ── ベスト組み合わせ ──────────────────────────────────────────────────
    if not combo_df.empty:
        valid_combos = combo_df[
            (combo_df["ev"] > 0) & (combo_df["n"] >= min_n)
        ].head(5)
        if not valid_combos.empty:
            proposals["top_combinations"] = valid_combos[[
                "condition", "n", "tp_rate", "sl_rate", "ev", "ev_score"
            ]].to_dict("records")

    return proposals


# ── レポート出力 ─────────────────────────────────────────────────────────────

def _fmt_stats(s: dict | None) -> str:
    if not s:
        return "データ不足"
    return (
        f"n={s['n']:>5}  TP={s['tp_rate']:.1f}%  SL={s['sl_rate']:.1f}%  "
        f"EV={s['ev']:+.3f}%  Sharpe={s['sharpe']:.3f}"
    )


def print_report(
    single: dict,
    combo_df: pd.DataFrame,
    cutoff_df: pd.DataFrame,
    temporal: dict,
    proposals: dict,
) -> None:
    lines = []
    lines.append("=" * 70)
    lines.append("閾値最適化レポート")
    lines.append(f"生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)

    # ── 単一シグナル ──────────────────────────────────────────────────────
    lines.append("\n■ 単一シグナル最良閾値")
    lines.append(f"  {'シグナル':<20} {'閾値':>6} {'条件':>6}  統計")
    lines.append(f"  {'-'*20} {'-'*6} {'-'*6}  {'-'*45}")
    for col, res in single.items():
        if col == "breakout_5d" or "best" not in res:
            continue
        best = res["best"]
        direction = "<" if best.get("direction") == "below" else "≥"
        lines.append(
            f"  {col:<20} {best['threshold']:>6} {direction:>6}  {_fmt_stats(best)}"
        )
    # ブレイクアウト
    if "breakout_5d" in single:
        bo = single["breakout_5d"]
        lines.append(f"  {'breakout_5d=True':<20} {'':>6} {'':>6}  {_fmt_stats(bo.get('True'))}")
        lines.append(f"  {'breakout_5d=False':<20} {'':>6} {'':>6}  {_fmt_stats(bo.get('False'))}")

    # ── Stage1カットオフ ──────────────────────────────────────────────────
    lines.append("\n■ Stage1スコア カットオフ最適化")
    if not cutoff_df.empty:
        lines.append(f"  {'カットオフ':>10} {'件数':>6} {'TP率':>7} {'SL率':>7} {'期待値':>8} {'ev_score':>10}")
        lines.append(f"  {'-'*10} {'-'*6} {'-'*7} {'-'*7} {'-'*8} {'-'*10}")
        for _, row in cutoff_df.iterrows():
            mark = "★" if row["ev"] > 0 else " "
            lines.append(
                f"  {row['cutoff']:>10} {int(row['n']):>6} "
                f"{row['tp_rate']:>6.1f}% {row['sl_rate']:>6.1f}% "
                f"{row['ev']:>+7.3f}% {row['ev_score']:>10.2f}{mark}"
            )

    # ── 組み合わせ Top10 ──────────────────────────────────────────────────
    lines.append("\n■ 2シグナル組み合わせ Top10（ev_score順）")
    if not combo_df.empty:
        lines.append(f"  {'条件':<45} {'件数':>5} {'TP率':>6} {'SL率':>6} {'期待値':>7}")
        lines.append(f"  {'-'*45} {'-'*5} {'-'*6} {'-'*6} {'-'*7}")
        for _, row in combo_df.head(10).iterrows():
            lines.append(
                f"  {row['condition']:<45} {int(row['n']):>5} "
                f"{row['tp_rate']:>5.1f}% {row['sl_rate']:>5.1f}% "
                f"{row['ev']:>+6.3f}%"
            )
    else:
        lines.append("  データ不足のため分析不可")

    # ── 月別 ───────────────────────────────────────────────────────────────
    lines.append("\n■ 月別パフォーマンス")
    monthly = temporal.get("monthly", {})
    if monthly:
        lines.append(f"  {'月':>8}  {'件数':>5} {'TP率':>7} {'SL率':>7} {'期待値':>8}")
        lines.append(f"  {'-'*8}  {'-'*5} {'-'*7} {'-'*7} {'-'*8}")
        for month, s in sorted(monthly.items()):
            mark = "★" if s["ev"] > 0 else " "
            lines.append(
                f"  {month:>8}  {s['n']:>5} {s['tp_rate']:>6.1f}% "
                f"{s['sl_rate']:>6.1f}% {s['ev']:>+7.3f}%{mark}"
            )

    # ── 曜日別 ────────────────────────────────────────────────────────────
    lines.append("\n■ 曜日別パフォーマンス（購入翌日）")
    weekly = temporal.get("weekday", {})
    if weekly:
        lines.append(f"  {'曜日':<12}  {'件数':>5} {'TP率':>7} {'SL率':>7} {'期待値':>8}")
        lines.append(f"  {'-'*12}  {'-'*5} {'-'*7} {'-'*7} {'-'*8}")
        for wd, s in weekly.items():
            mark = "★" if s["ev"] > 0 else " "
            lines.append(
                f"  {wd:<12}  {s['n']:>5} {s['tp_rate']:>6.1f}% "
                f"{s['sl_rate']:>6.1f}% {s['ev']:>+7.3f}%{mark}"
            )

    # ── 最適閾値提案 ──────────────────────────────────────────────────────
    lines.append("\n■ 最適閾値提案サマリー")
    if proposals:
        if "stage1_score_cutoff" in proposals:
            p = proposals["stage1_score_cutoff"]
            lines.append(f"\n  [Stage1スコアカットオフ]")
            lines.append(f"    現行: > 0")
            lines.append(f"    提案: >= {p['proposed']}  (期待値={p['ev']:+.3f}%, n={p['n']:,})")
            lines.append(f"    根拠: {p['rationale']}")

        lines.append(f"\n  [シグナル別 最適閾値]")
        for key in ["rsi14", "rsi5", "vol_ratio", "w52_pos", "dvs"]:
            if key not in proposals:
                continue
            p = proposals[key]
            dir_sym = "<" if p["direction"] == "below" else "≥"
            lines.append(
                f"    {key:<16}: 現行={CURRENT_THRESHOLDS.get(key, {}).get('good_range', '?')}  "
                f"→ 提案={dir_sym}{p['proposed_threshold']}  "
                f"(EV={p['ev']:+.3f}%, TP={p['tp_rate']:.1f}%, n={p['n']:,})"
            )

        if "top_combinations" in proposals:
            lines.append(f"\n  [最良条件の組み合わせ Top5]")
            for c in proposals["top_combinations"]:
                lines.append(
                    f"    {c['condition']:<45} n={c['n']:>5} "
                    f"TP={c['tp_rate']:.1f}% EV={c['ev']:+.3f}%"
                )
    else:
        lines.append("  有意な提案なし（データ不足の可能性があります）")

    lines.append("\n" + "=" * 70)
    lines.append("【注意事項】")
    lines.append("  - バックテストは過去データに対する検証であり、将来の利益を保証しません。")
    lines.append("  - 損切り -2% / 利確 +3% の条件は翌日の始値エントリーを前提としています。")
    lines.append("  - 両方ヒット（同一日にTP・SLタッチ）の場合はSL発動として保守的に計算しています。")
    lines.append("  - n < 50 の条件は統計的に不安定です。信頼性には n ≥ 100 を推奨します。")
    lines.append("=" * 70)

    report_text = "\n".join(lines)
    print(report_text)

    report_path = DATA_DIR / "optimization_report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\nレポート保存: {report_path}")


def main():
    df = load_results()

    # 分析実行
    single   = analyze_single_signals(df)
    combo_df = grid_search_combinations(df)
    cutoff_df = optimize_score_cutoff(df)
    temporal = analyze_temporal(df)
    proposals = propose_optimal_rules(df, single, combo_df, cutoff_df)

    # レポート出力
    print_report(single, combo_df, cutoff_df, temporal, proposals)

    # JSON保存
    proposal_path = DATA_DIR / "optimal_thresholds.json"
    with open(proposal_path, "w", encoding="utf-8") as f:
        # DataFrameはシリアライズできないので除外
        serializable_proposals = {
            k: v for k, v in proposals.items()
            if k != "top_combinations" or isinstance(v, list)
        }
        json.dump(serializable_proposals, f, ensure_ascii=False, indent=2)
    print(f"提案閾値JSON: {proposal_path}")

    # 組み合わせCSV保存
    if not combo_df.empty:
        combo_path = DATA_DIR / "combo_analysis.csv"
        combo_df.to_csv(combo_path, index=False, encoding="utf-8-sig")
        print(f"組み合わせ分析: {combo_path}")


if __name__ == "__main__":
    main()

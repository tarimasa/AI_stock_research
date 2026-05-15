#!/usr/bin/env python3
"""
backtest/limit_fill_sweep.py

戦略C（条件付き寄成行）の許容ギャップ閾値 max_gap_pct を振って性能を比較する。

limit_fill_analyzer.py のサブセット。データロード・シグナル計算・約定判定までは
1 回だけ行い、閾値ごとに戦略C のリターンだけを再計算するため高速。

使い方:
  python3 backtest/limit_fill_sweep.py
  python3 backtest/limit_fill_sweep.py --period val
  python3 backtest/limit_fill_sweep.py --gaps 0.5 1.0 1.5 2.0 2.5 3.0 5.0
  python3 backtest/limit_fill_sweep.py --period both   # train と val を並べる

出力:
  backtest/data/limit_fill_sweep.csv  -- 比較表
  コンソール: 比較レポート
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

from run_backtest import (
    TRAIN_START, TRAIN_END, VAL_START, VAL_END,
    load_all_data, apply_basic_filter, calc_all_signals,
)
from limit_fill_analyzer import (
    add_next_bars, add_future_bars, add_limit_fill_columns,
    simulate_holding,
)

DATA_DIR = PROJECT_ROOT / "backtest" / "data"


def _stats_row(label: str, ret: pd.Series, total: int) -> dict:
    taken = ret.dropna()
    n_taken = len(taken)
    return {
        "label":           label,
        "n_signals":       total,
        "n_taken":         n_taken,
        "participate_pct": round(n_taken / total * 100, 2) if total else 0.0,
        "ev_when_taken":   round(float(taken.mean()), 4) if n_taken else 0.0,
        "win_rate":        round((taken > 0).mean() * 100, 2) if n_taken else 0.0,
        "ev_per_signal":   round(float(taken.sum() / total), 4) if total else 0.0,
        "total_pnl_pct":   round(float(taken.sum()), 2),
    }


def prepare_signals(args) -> pd.DataFrame:
    """データロード・シグナル計算・約定判定までを 1 回だけ実行。"""
    all_data = load_all_data()
    if all_data.empty:
        sys.exit("[sweep] データがありません。先に run_backtest.py --download を実行。")

    if not args.no_filter:
        latest = all_data["Date"].max()
        valid_codes = set(
            apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique()
        )
        all_data = all_data[all_data["Code"].isin(valid_codes)].copy()

    df = calc_all_signals(all_data)
    df = add_next_bars(df)
    df = add_future_bars(df, days=args.holding_days)
    df = add_limit_fill_columns(df, limit_pct=args.limit_pct)
    df = df[df["stage1_score"] >= args.min_score].copy()
    df = df[df["next_open"].notna()].copy()
    return df


def slice_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    if period == "train":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) &
                  (df["Date"] <= pd.Timestamp(TRAIN_END))].copy()
    if period == "val":
        return df[(df["Date"] >= pd.Timestamp(VAL_START)) &
                  (df["Date"] <= pd.Timestamp(VAL_END))].copy()
    return df


def sweep_one_period(df: pd.DataFrame, gaps: list[float], args) -> pd.DataFrame:
    """与えた df で戦略 C を gaps 全てに対して評価、A / B も基準として並べる。"""
    df = df.copy()

    # A. 指値のみ
    ret_limit, _ = simulate_holding(df, "limit_entry",
                                      args.sl_pct, args.tp_pct,
                                      args.holding_days, args.cost_pct)
    # B. 寄成行 (常時)
    ret_open, _ = simulate_holding(df, "next_open",
                                     args.sl_pct, args.tp_pct,
                                     args.holding_days, args.cost_pct)

    rows = []
    valid = df["ret_valid_mask"] if "ret_valid_mask" in df else ret_open.notna()
    total = int(valid.sum())

    rows.append(_stats_row("A. 指値のみ (現行)",       ret_limit[valid], total))
    rows.append(_stats_row("B. 寄成行 (常時)",          ret_open[valid],  total))

    next_open = df["next_open"]
    gap_pct   = df["gap_pct"]
    for g in gaps:
        entry_c = np.where(gap_pct <= g, next_open, np.nan)
        df["_entry_c"] = entry_c
        ret_c, _ = simulate_holding(df, "_entry_c",
                                      args.sl_pct, args.tp_pct,
                                      args.holding_days, args.cost_pct)
        rows.append(_stats_row(f"C. gap<= +{g:.1f}%", ret_c[valid], total))
    df.drop(columns=["_entry_c"], errors="ignore", inplace=True)
    return pd.DataFrame(rows)


def print_table(title: str, summary: pd.DataFrame) -> None:
    print(f"\n{title}")
    print(f"  {'戦略':<22} {'参戦数':>8} {'参戦率':>7} "
          f"{'約定時EV':>9} {'勝率':>7} {'純EV':>9} {'累積%':>10}")
    print(f"  {'-'*22} {'-'*8} {'-'*7} {'-'*9} {'-'*7} {'-'*9} {'-'*10}")

    best_idx = summary["ev_per_signal"].idxmax()
    for i, r in summary.iterrows():
        mark = " ★" if i == best_idx else "  "
        print(f"  {r['label']:<22} {r['n_taken']:>8,} "
              f"{r['participate_pct']:>6.2f}% "
              f"{r['ev_when_taken']:>+8.3f}% "
              f"{r['win_rate']:>6.1f}% "
              f"{r['ev_per_signal']:>+8.3f}% "
              f"{r['total_pnl_pct']:>+9.1f}%{mark}")
    print("  ★ = 純EV (=機会損失込みの全シグナル平均) 最大の戦略")


def parse_args():
    p = argparse.ArgumentParser(description="戦略Cのギャップ閾値スイープ")
    p.add_argument("--gaps", type=float, nargs="+",
                   default=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0],
                   help="評価する gap_pct の上限（複数指定可）")
    p.add_argument("--limit-pct",    type=float, default=-0.7)
    p.add_argument("--sl-pct",       type=float, default=-5.0)
    p.add_argument("--tp-pct",       type=float, default=7.5)
    p.add_argument("--holding-days", type=int,   default=3)
    p.add_argument("--cost-pct",     type=float, default=0.20)
    p.add_argument("--min-score",    type=float, default=60.0)
    p.add_argument("--period",       choices=["train", "val", "all", "both"],
                   default="both")
    p.add_argument("--no-filter",    action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    print(f"[sweep] params: limit_pct={args.limit_pct:+.2f}%, "
          f"TP/SL=+{args.tp_pct}/{args.sl_pct}%, "
          f"holding={args.holding_days}d, min_score={args.min_score}")
    df_all = prepare_signals(args)

    periods = ["train", "val"] if args.period == "both" else [args.period]

    combined_rows = []
    for period in periods:
        df_p = slice_period(df_all, period)
        if df_p.empty:
            print(f"\n[sweep] {period}: シグナルが 0 件です（スキップ）")
            continue
        n = len(df_p)
        dates = df_p["Date"].nunique()
        codes = df_p["Code"].nunique()
        title = (f"【{period}】 シグナル {n:,} 件 "
                 f"({codes}銘柄 × {dates}日)")
        summary = sweep_one_period(df_p, args.gaps, args)
        print_table(title, summary)

        # CSV出力用に period を付ける
        summary.insert(0, "period", period)
        combined_rows.append(summary)

    if combined_rows:
        out = pd.concat(combined_rows, ignore_index=True)
        out_path = DATA_DIR / "limit_fill_sweep.csv"
        out.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"\nCSV: {out_path}")


if __name__ == "__main__":
    main()

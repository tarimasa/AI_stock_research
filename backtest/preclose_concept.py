#!/usr/bin/env python3
"""
backtest/preclose_concept.py

「T 終値エントリー」(大引け前推奨を想定) vs 「T+1 寄付/指値エントリー」(現行) の比較。

検証したい仮説:
  シグナル日 T に 14:30 頃推奨を出し T の大引け で買えば、T+1 の gap-up を
  取り逃さずに済む。一方 T→T+1 の overnight gap-down リスクを背負うため、
  平均で見て本当に得なのか?

シミュレーション:
  Strategy A (現行): T+1 寄付で ATR×1.5 指値、約定しなければ不参戦
  Strategy B (提案): T 終値で必ずエントリー (大引け成行を想定)
  Strategy C (折衷): T 終値 × 0.998 指値 (大引け前の約数分の指値、控えめ)

  どれも保有 T+1 〜 T+3 (TP+7.5% / SL-5% / 期日決済)

【過学習対策】
  Strategy 別パラメータの追加チューニングはせず、現行設定で純比較。
  TRAIN / VAL を別表示。

使い方:
  python3 backtest/preclose_concept.py
  python3 backtest/preclose_concept.py --period val

出力:
  backtest/data/preclose_concept.csv  -- 戦略 × 期間の集計
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
    add_next_bars, add_future_bars, tick_round_series, simulate_holding,
)
from compare_three_strategies import add_atr

DATA_DIR = PROJECT_ROOT / "backtest" / "data"


def evaluate(df: pd.DataFrame, entry_col: str, args) -> dict:
    """戦略 1 つを評価。entry_col の価格でエントリーして N 日保有。"""
    ret, _ = simulate_holding(
        df, entry_col, sl_pct=args.sl_pct, tp_pct=args.tp_pct,
        days=args.holding_days, cost_pct=args.cost_pct,
    )
    n_total = len(df)
    taken = ret.dropna()
    n_taken = len(taken)
    return {
        "n_signals":       n_total,
        "n_taken":         n_taken,
        "participate_pct": round(n_taken / n_total * 100, 2) if n_total else 0.0,
        "ev_when_taken":   round(float(taken.mean()), 4) if n_taken else 0.0,
        "win_rate":        round((taken > 0).mean() * 100, 2) if n_taken else 0.0,
        "ev_per_signal":   round(float(taken.sum() / n_total), 4) if n_total else 0.0,
        "total_pnl_pct":   round(float(taken.sum()), 2),
    }


def prepare(args) -> pd.DataFrame:
    all_data = load_all_data()
    if all_data.empty:
        sys.exit("[preclose] データなし")
    if not args.no_filter:
        latest = all_data["Date"].max()
        valid_codes = set(
            apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique()
        )
        all_data = all_data[all_data["Code"].isin(valid_codes)].copy()

    df = calc_all_signals(all_data)
    df = add_atr(df, n=14)
    df = add_next_bars(df)
    df = add_future_bars(df, days=args.holding_days)
    df = df[df["stage1_score"] >= args.min_score].copy()
    df = df[df["next_open"].notna() & df["atr14"].notna()].copy()

    # ── 各戦略のエントリー価格を準備 ───────────────────────────────────────
    # A: ATR×1.5 指値 (現行採用予定)
    df["limit_atr"] = tick_round_series(df["Close"] - 1.5 * df["atr14"])
    fill_at_open  = df["next_open"] <= df["limit_atr"]
    fill_intraday = (~fill_at_open) & (df["next_low"] <= df["limit_atr"])
    df["entry_A"] = np.where(
        fill_at_open, df["next_open"],
        np.where(fill_intraday, df["limit_atr"], np.nan),
    )

    # B: T 終値 (大引け成行)
    df["entry_B"] = df["Close"]

    # C: T 終値 × 0.998 で大引け前指値
    df["entry_C"] = tick_round_series(df["Close"] * 0.998)
    # 大引け前の刺さり判定は厳密にはザラ場安値が必要だが、当日 Low <= 指値 を近似で使用
    # (14:30〜15:00 の狭い窓だと刺さらないケースも多いはずだが、ここでは上限見積もり)
    fill_intraday_C = df["Low"] <= df["entry_C"]
    df["entry_C"] = np.where(fill_intraday_C, df["entry_C"], np.nan)

    return df


def slice_period(df: pd.DataFrame, period: str) -> pd.DataFrame:
    if period == "train":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) &
                  (df["Date"] <= pd.Timestamp(TRAIN_END))]
    if period == "val":
        return df[(df["Date"] >= pd.Timestamp(VAL_START)) &
                  (df["Date"] <= pd.Timestamp(VAL_END))]
    return df


def report(period: str, df_p: pd.DataFrame, args) -> list[dict]:
    print(f"\n【{period.upper()}】 シグナル {len(df_p):,} 件 "
          f"({df_p['Code'].nunique()}銘柄 × {df_p['Date'].nunique()}日)")
    print(f"  {'戦略':<32} {'参戦数':>8} {'参戦率':>7} "
          f"{'約定時EV':>9} {'勝率':>7} {'純EV':>9} {'累積%':>10}")
    print(f"  {'-'*32} {'-'*8} {'-'*7} {'-'*9} {'-'*7} {'-'*9} {'-'*10}")

    rows = []
    strategies = [
        ("A. T+1寄付ATR×1.5指値 (現行採用予定)", "entry_A"),
        ("B. T終値で成行 (大引け推奨)",        "entry_B"),
        ("C. T終値×0.998指値 (大引け前)",      "entry_C"),
    ]
    best_idx = None
    best_ev = -1e9
    stats_list = []
    for label, col in strategies:
        st = evaluate(df_p, col, args)
        st["strategy"] = label
        st["period"] = period
        stats_list.append(st)
        if st["ev_per_signal"] > best_ev:
            best_ev = st["ev_per_signal"]
            best_idx = len(stats_list) - 1

    for i, st in enumerate(stats_list):
        mark = " ★" if i == best_idx else "  "
        print(f"  {st['strategy']:<32} {st['n_taken']:>8,} "
              f"{st['participate_pct']:>6.2f}% "
              f"{st['ev_when_taken']:>+8.3f}% "
              f"{st['win_rate']:>6.1f}% "
              f"{st['ev_per_signal']:>+8.4f}% "
              f"{st['total_pnl_pct']:>+9.1f}%{mark}")
        rows.append(st)
    return rows


def parse_args():
    p = argparse.ArgumentParser(description="T終値エントリー vs T+1寄付の比較")
    p.add_argument("--sl-pct",       type=float, default=-5.0)
    p.add_argument("--tp-pct",       type=float, default=7.5)
    p.add_argument("--holding-days", type=int,   default=3)
    p.add_argument("--cost-pct",     type=float, default=0.20)
    p.add_argument("--min-score",    type=float, default=60.0)
    p.add_argument("--period",       choices=["train", "val", "both"], default="both")
    p.add_argument("--no-filter",    action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    print(f"[preclose] params: TP/SL=+{args.tp_pct}/{args.sl_pct}%, "
          f"holding={args.holding_days}d, min_score={args.min_score}")
    df_all = prepare(args)
    print(f"[preclose] 評価対象シグナル: {len(df_all):,}件")

    periods = ["train", "val"] if args.period == "both" else [args.period]
    all_rows = []
    for p in periods:
        df_p = slice_period(df_all, p)
        if not df_p.empty:
            all_rows.extend(report(p, df_p, args))

    print()
    print("  ★ = その期間で純EVが最大の戦略")
    print("  参戦率: 戦略Bは100%、A/Cは指値が刺さった日のみ")
    print("  純EV: 全シグナル基準（不参戦は0%寄与で平均）")

    pd.DataFrame(all_rows).to_csv(
        DATA_DIR / "preclose_concept.csv", index=False, encoding="utf-8-sig",
    )
    print(f"\nCSV: {DATA_DIR / 'preclose_concept.csv'}")


if __name__ == "__main__":
    main()

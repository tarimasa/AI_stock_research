#!/usr/bin/env python3
"""
backtest/limit_pct_optimization.py

指値オフセット (limit_pct) の最適値を学習期間で決定し、検証期間で out-of-sample テストする。

【手順】
  1. TRAIN_START 〜 TRAIN_END のみで limit_pct のグリッドサーチ
  2. 学習期間で純EV が最大かつサンプル数 >= MIN_SAMPLES の値を採用
  3. 採用値を VAL_START 〜 VAL_END に一度だけ適用 (再チューニング無し)
  4. 近傍値 (best ±0.25%, ±0.5%) の挙動を並べ、過学習の兆候を確認
     (best のEVが孤立した山なら偶然ピーク。スムーズに減衰するなら本物)

【過学習回避の配慮】
  - VAL データはステップ 1-2 の決定に使わない (look-ahead 禁止)
  - グリッド粒度は 0.25% (現行 -0.7% から 1 ステップ離れる程度の解像度)
    細かすぎるとノイズ拾う、粗すぎると最適点を外す
  - 最低サンプル数 (MIN_SAMPLES) を強制し、深すぎる指値で n が少ない値を除外
  - 学習期間で隣接 2 点との比較を確認できるレポート

【使い方】
  python3 backtest/limit_pct_optimization.py
  python3 backtest/limit_pct_optimization.py --min-samples 5000
  python3 backtest/limit_pct_optimization.py --grid-step 0.5

【出力】
  backtest/data/limit_pct_train_sweep.csv  -- 学習期間の全グリッド結果
  backtest/data/limit_pct_oos_result.csv   -- 検証期間 (out-of-sample) 結果
  コンソール: レポート
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

DATA_DIR = PROJECT_ROOT / "backtest" / "data"


# ── 1 つの limit_pct について評価する ─────────────────────────────────────────

def evaluate_limit_pct(
    df: pd.DataFrame, limit_pct: float, args
) -> dict:
    """
    指値オフセットを 1 つ与えて指値のみ (戦略A) のリターン統計を返す。
    df は事前に add_next_bars / add_future_bars / Stage1 フィルタ済みを想定。
    """
    df = df.copy()
    # 約定判定 (add_limit_fill_columns の核ロジックを inline 化して高速化)
    limit_price = tick_round_series(df["Close"] * (1.0 + limit_pct / 100.0))

    next_open = df["next_open"]
    next_low  = df["next_low"]
    fill_at_open  = next_open <= limit_price
    fill_intraday = (~fill_at_open) & (next_low <= limit_price)
    invalid = next_open.isna()

    entry = pd.Series(np.nan, index=df.index, dtype="float64")
    entry[fill_at_open & ~invalid]  = next_open[fill_at_open & ~invalid]
    entry[fill_intraday & ~invalid] = limit_price[fill_intraday & ~invalid]
    df["_entry"] = entry

    ret, outcome = simulate_holding(
        df, "_entry",
        sl_pct=args.sl_pct, tp_pct=args.tp_pct,
        days=args.holding_days, cost_pct=args.cost_pct,
    )

    valid = ret.notna() | df["next_open"].notna()  # T+1 がある行
    n_signals = int(df["next_open"].notna().sum())
    taken = ret.dropna()
    n_taken = len(taken)

    return {
        "limit_pct":       limit_pct,
        "n_signals":       n_signals,
        "n_taken":         n_taken,
        "participate_pct": round(n_taken / n_signals * 100, 2) if n_signals else 0.0,
        "ev_when_taken":   round(float(taken.mean()), 4) if n_taken else 0.0,
        "win_rate":        round((taken > 0).mean() * 100, 2) if n_taken else 0.0,
        "tp_rate":         round((outcome == "tp").sum() / n_taken * 100, 2) if n_taken else 0.0,
        "sl_rate":         round((outcome == "sl").sum() / n_taken * 100, 2) if n_taken else 0.0,
        "ev_per_signal":   round(float(taken.sum() / n_signals), 4) if n_signals else 0.0,
        "total_pnl_pct":   round(float(taken.sum()), 2),
    }


# ── データ準備 (limit_pct に依存しない部分) ─────────────────────────────────

def prepare(args) -> pd.DataFrame:
    all_data = load_all_data()
    if all_data.empty:
        sys.exit("[opt] データなし。run_backtest.py --download を先に。")

    if not args.no_filter:
        latest = all_data["Date"].max()
        valid_codes = set(
            apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique()
        )
        all_data = all_data[all_data["Code"].isin(valid_codes)].copy()

    df = calc_all_signals(all_data)
    df = add_next_bars(df)
    df = add_future_bars(df, days=args.holding_days)
    df = df[df["stage1_score"] >= args.min_score].copy()
    df = df[df["next_open"].notna()].copy()
    return df


def slice_period(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return df[(df["Date"] >= pd.Timestamp(start)) &
              (df["Date"] <= pd.Timestamp(end))].copy()


# ── レポート ──────────────────────────────────────────────────────────────

def print_train_sweep(rows: list[dict], min_samples: int) -> pd.DataFrame:
    print("\n【① 学習期間 (TRAIN) でのグリッドサーチ】")
    print(f"  期間: {TRAIN_START} 〜 {TRAIN_END}")
    print(f"  最低サンプル数: {min_samples:,}")
    print(f"  {'limit_pct':>10} {'参戦数':>8} {'参戦率':>7} "
          f"{'約定時EV':>9} {'勝率':>7} {'TP率':>7} {'SL率':>7} {'純EV':>9}")
    print(f"  {'-'*10} {'-'*8} {'-'*7} {'-'*9} {'-'*7} {'-'*7} {'-'*7} {'-'*9}")
    df = pd.DataFrame(rows).sort_values("limit_pct", ascending=False)
    for _, r in df.iterrows():
        flag = " " if r["n_taken"] >= min_samples else "✗"
        print(f"  {r['limit_pct']:>+9.2f}% {r['n_taken']:>8,} "
              f"{r['participate_pct']:>6.2f}% "
              f"{r['ev_when_taken']:>+8.3f}% "
              f"{r['win_rate']:>6.1f}% "
              f"{r['tp_rate']:>6.1f}% "
              f"{r['sl_rate']:>6.1f}% "
              f"{r['ev_per_signal']:>+8.3f}%{flag}")
    print("  ✗ = サンプル数不足で採用対象外")
    return df


def choose_best(train_df: pd.DataFrame, min_samples: int) -> float:
    """純EV 最大 (かつサンプル数 >= min_samples) の limit_pct を返す。"""
    eligible = train_df[train_df["n_taken"] >= min_samples].copy()
    if eligible.empty:
        sys.exit("[opt] サンプル数が min_samples を満たすグリッド点がありません。")
    best_row = eligible.loc[eligible["ev_per_signal"].idxmax()]
    return float(best_row["limit_pct"])


def print_robustness(train_df: pd.DataFrame, best: float) -> None:
    """best の近傍 (±0.25, ±0.50) の学習EV を並べてピークの滑らかさを確認。"""
    print("\n【② 学習期間での近傍ロバストネス確認】")
    print("  best が孤立スパイクでないか (≒ 過学習でないか) を視認する")
    print(f"  {'offset':>8} {'limit_pct':>10} {'n':>8} {'純EV':>9}")
    print(f"  {'-'*8} {'-'*10} {'-'*8} {'-'*9}")
    for offset in [-0.50, -0.25, 0.0, +0.25, +0.50]:
        target = round(best + offset, 2)
        sub = train_df[np.isclose(train_df["limit_pct"], target, atol=1e-6)]
        if sub.empty:
            print(f"  {offset:>+7.2f}% {target:>+9.2f}% {'-':>8} {'-':>9} (グリッド外)")
            continue
        r = sub.iloc[0]
        mark = " ★" if offset == 0 else ""
        print(f"  {offset:>+7.2f}% {target:>+9.2f}% {r['n_taken']:>8,} "
              f"{r['ev_per_signal']:>+8.3f}%{mark}")


def print_oos(current_row: dict, best_row: dict, neighborhood: list[dict]) -> None:
    """採用値で VAL 期間を評価。現行 -0.7% との比較と近傍を出す。"""
    print("\n【③ 検証期間 (VAL) での out-of-sample 評価】")
    print(f"  期間: {VAL_START} 〜 {VAL_END}")
    print(f"  ※ ここで初めて VAL データを参照 (学習で決定済みの値を一発適用)")
    print(f"  {'limit_pct':>10} {'参戦数':>8} {'参戦率':>7} "
          f"{'約定時EV':>9} {'勝率':>7} {'純EV':>9}")
    print(f"  {'-'*10} {'-'*8} {'-'*7} {'-'*9} {'-'*7} {'-'*9}")
    for r in [current_row] + neighborhood:
        tag = ""
        if r["limit_pct"] == best_row["limit_pct"]:
            tag = " ← 学習で採用"
        elif r["limit_pct"] == current_row["limit_pct"] and r is current_row:
            tag = " ← 現行"
        print(f"  {r['limit_pct']:>+9.2f}% {r['n_taken']:>8,} "
              f"{r['participate_pct']:>6.2f}% "
              f"{r['ev_when_taken']:>+8.3f}% "
              f"{r['win_rate']:>6.1f}% "
              f"{r['ev_per_signal']:>+8.3f}%{tag}")

    delta = best_row["ev_per_signal"] - current_row["ev_per_signal"]
    print(f"\n  → 純EV 改善幅 (VAL): {delta:+.4f}% / 件 "
          f"(学習採用 {best_row['limit_pct']:+.2f}% vs 現行 {current_row['limit_pct']:+.2f}%)")
    print(f"  → トレード数の変化: {current_row['n_taken']:,} → {best_row['n_taken']:,} "
          f"({(best_row['n_taken']/current_row['n_taken']-1)*100:+.1f}%)")


# ── メイン ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="limit_pct の学習/検証分割最適化")
    p.add_argument("--grid-start", type=float, default=-3.0,
                   help="グリッドの最深 limit_pct (既定: -3.0%%)")
    p.add_argument("--grid-end",   type=float, default=0.0,
                   help="グリッドの最浅 limit_pct (既定: 0.0%%)")
    p.add_argument("--grid-step",  type=float, default=0.25,
                   help="グリッド刻み幅 (既定: 0.25%%)")
    p.add_argument("--min-samples", type=int, default=5000,
                   help="採用候補とするための最低サンプル数 (既定: 5,000)")
    p.add_argument("--current-limit-pct", type=float, default=-0.7,
                   help="現行の指値オフセット (比較表示用, 既定: -0.7%%)")
    p.add_argument("--sl-pct",       type=float, default=-5.0)
    p.add_argument("--tp-pct",       type=float, default=7.5)
    p.add_argument("--holding-days", type=int,   default=3)
    p.add_argument("--cost-pct",     type=float, default=0.20)
    p.add_argument("--min-score",    type=float, default=60.0)
    p.add_argument("--no-filter",    action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    print(f"[opt] params: TP/SL=+{args.tp_pct}/{args.sl_pct}%, "
          f"holding={args.holding_days}d, "
          f"min_score={args.min_score}, min_samples={args.min_samples}")

    df_all = prepare(args)
    df_train = slice_period(df_all, TRAIN_START, TRAIN_END)
    df_val   = slice_period(df_all, VAL_START, VAL_END)
    print(f"[opt] train signals: {len(df_train):,} / val signals: {len(df_val):,}")

    # ── ① TRAIN sweep ────────────────────────────────────────────────────
    grid = []
    x = args.grid_start
    while x <= args.grid_end + 1e-9:
        grid.append(round(x, 2))
        x += args.grid_step

    train_rows = []
    for limit_pct in grid:
        train_rows.append(evaluate_limit_pct(df_train, limit_pct, args))
    train_df = print_train_sweep(train_rows, args.min_samples)

    train_df.to_csv(DATA_DIR / "limit_pct_train_sweep.csv",
                    index=False, encoding="utf-8-sig")

    # ── ② 学習で最良を選択 + 近傍 ────────────────────────────────────────
    best = choose_best(train_df, args.min_samples)
    print(f"\n  → 学習で採用: limit_pct = {best:+.2f}%")
    print_robustness(train_df, best)

    # ── ③ VAL に一発適用 ─────────────────────────────────────────────────
    targets = sorted({
        round(args.current_limit_pct, 2),
        round(best - 0.5, 2), round(best - 0.25, 2),
        round(best, 2),
        round(best + 0.25, 2), round(best + 0.5, 2),
    })
    val_rows = [evaluate_limit_pct(df_val, lp, args) for lp in targets]
    val_df = pd.DataFrame(val_rows)
    val_df.to_csv(DATA_DIR / "limit_pct_oos_result.csv",
                  index=False, encoding="utf-8-sig")

    current_row = next(r for r in val_rows if r["limit_pct"] == round(args.current_limit_pct, 2))
    best_row    = next(r for r in val_rows if r["limit_pct"] == round(best, 2))
    neighborhood = [r for r in val_rows if r is not current_row]
    print_oos(current_row, best_row, neighborhood)

    print(f"\nCSV: {DATA_DIR / 'limit_pct_train_sweep.csv'}")
    print(f"CSV: {DATA_DIR / 'limit_pct_oos_result.csv'}")


if __name__ == "__main__":
    main()

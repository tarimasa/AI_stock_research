#!/usr/bin/env python3
"""
backtest/overnight_only.py

「T 大引け前にシグナル検知 → T 終値で買い → T+1 寄付で売る」というユーザー仮説の
直接検証。3 日保有戦略 (preclose_concept.py) と違って overnight gap 1 本勝負。

検証する出口:
  exit_open_T1     : T+1 寄付で全決済 (純粋に overnight gap だけ)
  exit_close_T1    : T+1 終値で全決済 (overnight + 当日場中)
  exit_close_T1_TP : T+1 中に TP/SL 触れたらその場、なければ T+1 終値

切り口:
  - 全 Stage1 シグナル
  - stage1_score の分位 (上位 10%/25%/中央/下位)
  - 曜日別 (月→火, 金→月 など、特に Fri→Mon の overnight gap が違うか)
  - TRAIN / VAL 期間別

出力:
  backtest/data/overnight_only.csv  -- 戦略 × 切り口 × 期間の集計
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
from limit_fill_analyzer import add_next_bars

DATA_DIR = PROJECT_ROOT / "backtest" / "data"

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def compute_exits(df: pd.DataFrame, sl_pct: float, tp_pct: float, cost_pct: float):
    """3 種類の出口リターン (%) を計算。"""
    entry = df["Close"]  # T 大引け成行 想定 (overnight 直前)

    # ── 1) T+1 寄付決済 ─────────────────────────────────────────────────
    overnight = (df["next_open"] - entry) / entry * 100 - cost_pct

    # ── 2) T+1 終値決済 ─────────────────────────────────────────────────
    close_t1  = (df["next_close"] - entry) / entry * 100 - cost_pct

    # ── 3) T+1 中の TP/SL → なければ終値決済 ────────────────────────────
    #    gap-down で寄り時点で既に SL 越え: SL 値で決済 (寄り≤SL のとき)
    sl_price = entry * (1 + sl_pct / 100)
    tp_price = entry * (1 + tp_pct / 100)

    exit_3 = pd.Series(np.nan, index=df.index)
    # ① 寄り≤SL : SL 価格 (or 寄り、悪い方) で決済
    cond_open_sl = df["next_open"] <= sl_price
    exit_3.loc[cond_open_sl] = (
        np.minimum(df.loc[cond_open_sl, "next_open"], sl_price) / entry.loc[cond_open_sl] - 1
    ) * 100
    # ② 寄り≥TP : TP 価格 (or 寄り、良い方の小さい方→保守的に寄り) で決済
    cond_open_tp = (~cond_open_sl) & (df["next_open"] >= tp_price)
    exit_3.loc[cond_open_tp] = (
        df.loc[cond_open_tp, "next_open"] / entry.loc[cond_open_tp] - 1
    ) * 100
    # ③ 場中安値≤SL : SL 約定
    cond_low_sl = (~cond_open_sl) & (~cond_open_tp) & (df["next_low"] <= sl_price)
    exit_3.loc[cond_low_sl] = sl_pct
    # ④ 場中高値≥TP : TP 約定 (③ と同時のときは SL 優先 = 保守的)
    cond_high_tp = (~cond_open_sl) & (~cond_open_tp) & (~cond_low_sl) & (df["next_high"] >= tp_price)
    exit_3.loc[cond_high_tp] = tp_pct
    # ⑤ 触れず: T+1 終値決済
    cond_close = exit_3.isna()
    exit_3.loc[cond_close] = (df.loc[cond_close, "next_close"] / entry.loc[cond_close] - 1) * 100
    exit_3 = exit_3 - cost_pct

    return overnight, close_t1, exit_3


def stats(label: str, ret: pd.Series) -> dict:
    valid = ret.dropna()
    n = len(valid)
    if n == 0:
        return {"切り口": label, "n": 0}
    return {
        "切り口": label,
        "n": n,
        "EV(%)": round(float(valid.mean()), 3),
        "中央値": round(float(valid.median()), 3),
        "勝率(%)": round(float((valid > 0).mean() * 100), 1),
        "gap-up率(%)": round(float((valid > 0).mean() * 100), 1),
        "stdev": round(float(valid.std()), 3),
        "p10": round(float(valid.quantile(0.10)), 2),
        "p90": round(float(valid.quantile(0.90)), 2),
    }


def prepare(args) -> pd.DataFrame:
    all_data = load_all_data()
    if not args.no_filter:
        latest = all_data["Date"].max()
        valid_codes = set(
            apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique()
        )
        all_data = all_data[all_data["Code"].isin(valid_codes)].copy()

    df = calc_all_signals(all_data)
    df = add_next_bars(df)
    df = df[df["stage1_score"] >= args.min_score].copy()
    df = df[df["next_open"].notna() & df["next_close"].notna()].copy()
    df["weekday"] = df["Date"].dt.weekday  # 0=Mon
    df["overnight"], df["t1_close"], df["t1_tpsl"] = compute_exits(
        df, args.sl_pct, args.tp_pct, args.cost_pct,
    )
    return df


def slice_period(df, period):
    if period == "train":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    if period == "val":
        return df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))]
    return df


def print_table(title: str, rows: list[dict]):
    print(f"\n{title}")
    if not rows:
        print("  (データなし)")
        return
    keys = ["切り口", "n", "EV(%)", "中央値", "勝率(%)", "stdev", "p10", "p90"]
    header = "  " + " | ".join(f"{k:>10}" for k in keys)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print("  " + " | ".join(
            f"{r.get(k, ''):>10}" if not isinstance(r.get(k), (int, float))
            else f"{r.get(k, 0):>10,}" if isinstance(r.get(k), int)
            else f"{r.get(k, 0):>10.3f}"
            for k in keys
        ))


def report(period: str, df_p: pd.DataFrame, args) -> list[dict]:
    print(f"\n{'='*78}")
    print(f"【{period.upper()}】 シグナル {len(df_p):,} 件  期間: "
          f"{df_p['Date'].min().date()} ~ {df_p['Date'].max().date()}")
    print(f"{'='*78}")

    out_rows = []

    # ── A. 出口別の全体集計 ─────────────────────────────────────────────
    rows = [
        stats("出口: T+1寄付 (overnight only)", df_p["overnight"]),
        stats("出口: T+1終値 (1日保有)",        df_p["t1_close"]),
        stats("出口: T+1中TP/SL→終値",         df_p["t1_tpsl"]),
    ]
    print_table("[A] 出口戦略別 (全 Stage1 シグナル)", rows)
    for r in rows: r["period"] = period; r["section"] = "A_出口別"; out_rows.append(r)

    # ── B. stage1_score 分位 (overnight only) ───────────────────────────
    print("\n[B] stage1_score 分位 × 出口: T+1寄付")
    quantiles = [(0.00, 0.50, "下位50%"), (0.50, 0.75, "中位50-75%"),
                 (0.75, 0.90, "上位25%"), (0.90, 1.00, "上位10%")]
    rows = []
    q_low, q_high = df_p["stage1_score"].quantile([0.0, 1.0])
    print(f"     stage1_score range: {q_low:.0f} ~ {q_high:.0f}")
    for lo, hi, lab in quantiles:
        thr_lo = df_p["stage1_score"].quantile(lo)
        thr_hi = df_p["stage1_score"].quantile(hi)
        if lo == 0.0:
            sub = df_p[df_p["stage1_score"] <= thr_hi]
        elif hi == 1.0:
            sub = df_p[df_p["stage1_score"] > thr_lo]
        else:
            sub = df_p[(df_p["stage1_score"] > thr_lo) & (df_p["stage1_score"] <= thr_hi)]
        rows.append(stats(f"{lab} (score>{thr_lo:.0f})", sub["overnight"]))
    print_table("", rows)
    for r in rows: r["period"] = period; r["section"] = "B_score分位"; out_rows.append(r)

    # ── C. 曜日別 (overnight only) ──────────────────────────────────────
    print("\n[C] 曜日別 × 出口: T+1寄付 (= 翌営業日寄付)")
    rows = []
    for d in range(5):
        sub = df_p[df_p["weekday"] == d]
        if len(sub) > 0:
            # 月曜シグナル → 火曜寄付、金曜シグナル → 月曜寄付
            from_d = WEEKDAY_JA[d]
            to_d = WEEKDAY_JA[(d + 3) % 7] if d == 4 else WEEKDAY_JA[d + 1]
            rows.append(stats(f"{from_d}終→{to_d}寄", sub["overnight"]))
    print_table("", rows)
    for r in rows: r["period"] = period; r["section"] = "C_曜日別"; out_rows.append(r)

    return out_rows


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--sl-pct",    type=float, default=-5.0)
    p.add_argument("--tp-pct",    type=float, default=7.5)
    p.add_argument("--cost-pct",  type=float, default=0.20)
    p.add_argument("--min-score", type=float, default=60.0)
    p.add_argument("--no-filter", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    print(f"[overnight] params: TP/SL=+{args.tp_pct}/{args.sl_pct}%, "
          f"cost={args.cost_pct}%, min_score={args.min_score}")
    df = prepare(args)
    print(f"[overnight] 評価対象: {len(df):,} 件")

    all_rows = []
    for p in ["train", "val"]:
        df_p = slice_period(df, p)
        if not df_p.empty:
            all_rows.extend(report(p, df_p, args))

    pd.DataFrame(all_rows).to_csv(DATA_DIR / "overnight_only.csv", index=False, encoding="utf-8-sig")
    print(f"\nCSV: {DATA_DIR / 'overnight_only.csv'}")


if __name__ == "__main__":
    main()

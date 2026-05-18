#!/usr/bin/env python3
"""
backtest/overnight_event_filter.py

A: イベント (決算/大型ニュース) 強化 proxy + SL/TP スイープを VAL/TRAIN で評価。

決算履歴 API が手元にないため、価格 + 出来高の多変量シグナルで「決算/大型
ニュース当日」を検出する精緻な proxy を構築:

  event_score = 以下の and 条件を点数化
    +1: vol_ratio  >= 3.0          (出来高 3 倍超)
    +1: vol_ratio  >= 5.0          (出来高 5 倍超: +2 点合計)
    +1: range_pct  >= 8.0          (1 日レンジ 8% 超)
    +1: |gap_today| >= 5.0         (寄りからの巨大ギャップ)
    +1: |day_return| >= 5.0        (寄り引け 5% 超の値動き)
    +1: pre_vol_ratio_max >= 2.0   (直前 3 日にも出来高異常)

  event_likely = event_score >= 3   (3 点以上を「ほぼ確実にイベント日」とする)
  event_possible = event_score >= 2 (2 点以上で広めに除外)

SL/TP スイープ (T+1 寄付決済 + TP-SL hybrid):
  - SL/TP のセットを 8 通り試行
  - SL = -2 / -3 / -5 / -7%
  - TP = +3 / +5 / +7 / +10%
  - 保有日数 = 1 (T+1 のみ) と 3 (T+1〜T+3)

出力:
  backtest/data/overnight_event_filter.csv -- ルール × SL/TP × 期間 の集計
"""

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

DATA_DIR = PROJECT_ROOT / "backtest" / "data"
COST = 0.20


def add_event_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Code", "Date"]).reset_index(drop=True)
    g = df.groupby("Code", group_keys=False)

    df["day_return"] = (df["Close"] - df["Open"]) / df["Open"] * 100
    df["range_pct"]  = (df["High"] - df["Low"]) / df["Close"] * 100
    df["vol_ma20"]   = g["Volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["vol_ratio"]  = df["Volume"] / df["vol_ma20"].replace(0, np.nan)
    df["prev_close"] = g["Close"].shift(1)
    df["gap_today"]  = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100

    # ── イベント proxy スコア (T 日時点で観測可能な情報のみ) ─────────
    s = pd.Series(0, index=df.index, dtype=int)
    s += (df["vol_ratio"] >= 3.0).astype(int)
    s += (df["vol_ratio"] >= 5.0).astype(int)
    s += (df["range_pct"] >= 8.0).astype(int)
    s += (df["gap_today"].abs() >= 5.0).astype(int)
    s += (df["day_return"].abs() >= 5.0).astype(int)

    # 直前 3 日に出来高異常があったか (リーク疑い → 決算前ドリフト)
    pre_vol_max = g["vol_ratio"].transform(lambda x: x.shift(1).rolling(3, min_periods=1).max())
    s += (pre_vol_max >= 2.0).astype(int)

    df["event_score"]  = s
    df["event_likely"] = s >= 3
    df["event_loose"]  = s >= 2

    # ── 未来バー ──────────────────────────────────────────────────
    df["next_open"]  = g["Open"].shift(-1)
    df["next_high"]  = g["High"].shift(-1)
    df["next_low"]   = g["Low"].shift(-1)
    df["next_close"] = g["Close"].shift(-1)
    df["d2_high"]    = g["High"].shift(-2)
    df["d2_low"]     = g["Low"].shift(-2)
    df["d2_close"]   = g["Close"].shift(-2)
    df["d3_high"]    = g["High"].shift(-3)
    df["d3_low"]     = g["Low"].shift(-3)
    df["d3_close"]   = g["Close"].shift(-3)
    return df


def compute_exit_n_days(d: pd.DataFrame, sl: float, tp: float, hold_days: int, cost: float) -> pd.Series:
    """T 終値で買い、T+1 から hold_days 日保有。
    各日の Low<=SL なら SL、High>=TP なら TP、なければ最終日終値で決済。"""
    entry = d["Close"]
    sl_p = entry * (1 + sl / 100)
    tp_p = entry * (1 + tp / 100)
    ret = pd.Series(np.nan, index=d.index)

    high_cols = ["next_high", "d2_high", "d3_high"][:hold_days]
    low_cols  = ["next_low",  "d2_low",  "d3_low"][:hold_days]
    close_col = ["next_close", "d2_close", "d3_close"][hold_days - 1]
    open_col  = "next_open"

    # T+1 寄付時点で既に SL/TP の場合
    if hold_days >= 1:
        o_sl = d[open_col] <= sl_p
        ret.loc[o_sl] = (np.minimum(d.loc[o_sl, open_col], sl_p) / entry.loc[o_sl] - 1) * 100
        o_tp = (~o_sl) & (d[open_col] >= tp_p)
        ret.loc[o_tp] = (d.loc[o_tp, open_col] / entry.loc[o_tp] - 1) * 100

    # 各日の Low/High チェック
    remain = ret.isna()
    for hi_col, lo_col in zip(high_cols, low_cols):
        if not remain.any(): break
        l_sl = remain & (d[lo_col] <= sl_p)
        ret.loc[l_sl] = sl
        remain = ret.isna()
        h_tp = remain & (d[hi_col] >= tp_p)
        ret.loc[h_tp] = tp
        remain = ret.isna()

    # 最終日終値で残った分を決済
    ret.loc[ret.isna()] = (d.loc[ret.isna(), close_col] / entry.loc[ret.isna()] - 1) * 100
    return ret - cost


def compute_e1_open(d: pd.DataFrame, cost: float) -> pd.Series:
    return (d["next_open"] - d["Close"]) / d["Close"] * 100 - cost


def summ(s: pd.Series, cap=5.0) -> dict:
    s = s.dropna()
    if len(s) == 0: return {"n": 0}
    w = s.clip(-cap, cap)
    return {
        "n":      len(s),
        "W平均":   round(float(w.mean()), 3),
        "中央値":  round(float(s.median()), 3),
        "勝率":    round(float((s > 0).mean() * 100), 1),
        "stdev":  round(float(s.std()), 2),
    }


def fmt(st: dict) -> str:
    if st.get("n", 0) == 0: return "(n=0)"
    return (f"n={st['n']:>6,} W平均={st['W平均']:+6.3f}% "
            f"中央={st['中央値']:+6.3f}% 勝率={st['勝率']:>5.1f}% std={st['stdev']:>5.1f}")


def main():
    print("[event] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()

    df = calc_all_signals(all_data)
    df = add_event_features(df)
    df = df[df["next_open"].notna() & df["d3_close"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    print(f"[event] 評価行数: {len(df):,}")

    # ── R2 ベースシグナル ─────────────────────────────────────────────
    base_mask = (df["gap_today"] <= -3) & (df["ma25_diff_pct"] <= -8) & (df["day_return"] <= -3)
    df["is_r2"] = base_mask

    val   = df[(df["Date"] >= pd.Timestamp(VAL_START))   & (df["Date"] <= pd.Timestamp(VAL_END))]
    train = df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]

    # ── 1. イベントフィルタの寄与 ─────────────────────────────────────
    print("\n" + "=" * 88)
    print("【1. イベント proxy 強化版の効果 (出口: T+1 寄付決済)】")
    print("=" * 88)
    for period_name, period_df in [("TRAIN", train), ("VAL", val)]:
        print(f"\n[{period_name}]")
        r2 = period_df[period_df["is_r2"]].copy()
        e1 = compute_e1_open(r2, COST)
        print(f"  R2 全件          : {fmt(summ(e1))}")
        print(f"  R2 score<3 (likely 除外): "
              f"{fmt(summ(e1[~r2['event_likely']]))}  ({(~r2['event_likely']).sum()}/{len(r2)}件)")
        print(f"  R2 score<2 (loose 除外): "
              f"{fmt(summ(e1[~r2['event_loose']]))}  ({(~r2['event_loose']).sum()}/{len(r2)}件)")

        # スコア別 EV (細かい挙動)
        print("  --- スコア別 EV (T+1 寄付決済) ---")
        for s_val in range(0, 7):
            sub = r2[r2["event_score"] == s_val]
            if len(sub) > 0:
                e = compute_e1_open(sub, COST)
                print(f"    score={s_val}: {fmt(summ(e))}")

    # ── 2. SL/TP スイープ (R2 score<3 で固定し、TP/SL を変える) ─────
    print("\n" + "=" * 88)
    print("【2. SL/TP スイープ (R2 + event_score<3 で TRAIN/VAL を見る)】")
    print("=" * 88)

    sl_grid = [-2, -3, -5, -7]
    tp_grid = [3, 5, 7, 10]
    hold_grid = [1, 3]

    rows = []
    for period_name, period_df in [("TRAIN", train), ("VAL", val)]:
        r2 = period_df[period_df["is_r2"] & ~period_df["event_likely"]]
        print(f"\n[{period_name}] R2 score<3 サンプル: {len(r2):,} 件")
        # 比較ベース: 純粋 T+1 寄付決済
        st_base = summ(compute_e1_open(r2, COST))
        print(f"  baseline (T+1 寄付決済): {fmt(st_base)}")
        st_base["period"] = period_name
        st_base["sl"] = "-"; st_base["tp"] = "-"; st_base["hold"] = "T+1寄付"
        rows.append(st_base)

        print(f"  {'保有':<8} {'SL':>5} {'TP':>5} {'n':>6} {'W平均':>8} {'中央':>8} {'勝率':>6} {'std':>5}")
        for hd in hold_grid:
            for sl in sl_grid:
                for tp in tp_grid:
                    ret = compute_exit_n_days(r2, sl, tp, hd, COST)
                    st = summ(ret)
                    if st.get("n", 0) > 0:
                        st["period"] = period_name; st["sl"] = sl; st["tp"] = tp; st["hold"] = f"T+1〜T+{hd}"
                        rows.append(st)
                        print(f"  T+1〜T+{hd:<2}  {sl:>+5}% {tp:>+4}%  "
                              f"{st['n']:>6} {st['W平均']:>+7.3f}% {st['中央値']:>+7.3f}% "
                              f"{st['勝率']:>5.1f}% {st['stdev']:>5.1f}")

    pd.DataFrame(rows).to_csv(DATA_DIR / "overnight_event_filter.csv", index=False, encoding="utf-8-sig")
    print(f"\nCSV: {DATA_DIR / 'overnight_event_filter.csv'}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
backtest/overnight_clean_recheck.py

株式分割 artifact (例: 9434 SoftBank, gap -97% × 数週連発) を除外した上で
R1/R2/E7 を再評価し、過去分析の信頼性を確認 + top-N 選別を再検証する。

データクリーニング:
  - |gap_today| <= 15%  (-15%超ギャップは分割疑い)
  - |overnight_ret| <= 20% (1晩で±20%超は分割疑い)

100万円 資金前提での top-N シミュレーションも実施。
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


def prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Code", "Date"]).reset_index(drop=True)
    g = df.groupby("Code", group_keys=False)
    df["day_return"]  = (df["Close"] - df["Open"]) / df["Open"] * 100
    df["vol_ma20"]   = g["Volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["vol_ratio"]  = df["Volume"] / df["vol_ma20"].replace(0, np.nan)
    df["prev_close"] = g["Close"].shift(1)
    df["gap_today"]  = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - g["Close"].shift(1)).abs(),
        (df["Low"]  - g["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = g["Date"].transform(lambda x: tr.loc[x.index].rolling(14, min_periods=7).mean())
    df["relative_atr"] = df["atr14"] / df["Close"] * 100
    df["traded_value"] = df["Close"] * df["Volume"]
    df["next_open"]  = g["Open"].shift(-1)
    df["overnight"]  = (df["next_open"] - df["Close"]) / df["Close"] * 100
    df["e1"]         = df["overnight"] - COST
    return df


def split_filter(df: pd.DataFrame) -> pd.DataFrame:
    """株式分割 artifact を除外。"""
    before = len(df)
    df = df[df["gap_today"].abs() <= 15].copy()       # 15% 超ギャップ
    df = df[df["overnight"].abs() <= 20].copy()        # 20% 超 overnight
    after = len(df)
    print(f"  分割artifact除外: {before:,} → {after:,} ({before-after:,}件除外)")
    return df


def summ(s, cap=5.0):
    s = s.dropna()
    if len(s) == 0: return {"n": 0}
    w = s.clip(-cap, cap)
    return {
        "n": len(s),
        "W平均": round(float(w.mean()), 3),
        "中央値": round(float(s.median()), 3),
        "勝率":  round(float((s > 0).mean() * 100), 1),
        "stdev": round(float(s.std()), 2),
        "raw_mean": round(float(s.mean()), 3),
    }


def fmt(st):
    if st.get("n", 0) == 0: return "(n=0)"
    return (f"n={st['n']:>5,} 生平均={st['raw_mean']:+6.3f}% "
            f"W平均={st['W平均']:+6.3f}% 中央={st['中央値']:+6.3f}% "
            f"勝率={st['勝率']:>5.1f}% std={st['stdev']:>5.1f}")


def slice_period(df, period):
    if period == "TRAIN":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    return df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))]


def main():
    print("[clean] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()
    df = calc_all_signals(all_data)
    df = prep(df)
    df = df[df["next_open"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    print(f"[clean] フィルタ後: {len(df):,}行 (Close>=500 & Vol>=100k)")
    df = split_filter(df)

    # ── 過去分析の再現 (クリーン後) ───────────────────────────────────
    print("\n" + "=" * 92)
    print("【1. 過去分析の再現 (株式分割 artifact 除外後)】")
    print("=" * 92)

    rules = {
        "R1 (gap≤-2 & MA25≤-5)":             lambda p: (p["gap_today"] <= -2) & (p["ma25_diff_pct"] <= -5),
        "R1+10億 (TV≥1B)":                   lambda p: (p["gap_today"] <= -2) & (p["ma25_diff_pct"] <= -5) & (p["traded_value"] >= 1e9),
        "R1+30億 (TV≥3B)":                   lambda p: (p["gap_today"] <= -2) & (p["ma25_diff_pct"] <= -5) & (p["traded_value"] >= 3e9),
        "R2 (gap≤-3 & MA25≤-8 & dayret≤-3)": lambda p: (p["gap_today"] <= -3) & (p["ma25_diff_pct"] <= -8) & (p["day_return"] <= -3),
        "R2+中型 (500-3000円)":               lambda p: (p["gap_today"] <= -3) & (p["ma25_diff_pct"] <= -8) & (p["day_return"] <= -3) & (p["Close"] >= 500) & (p["Close"] < 3000),
    }

    for period in ["TRAIN", "VAL"]:
        period_df = slice_period(df, period)
        n_days = period_df["Date"].nunique()
        print(f"\n[{period}] {n_days}営業日")
        for rname, rfn in rules.items():
            sub = period_df[rfn(period_df)]
            st = summ(sub["e1"])
            per_day = st.get("n", 0) / n_days
            print(f"  {rname:<40} {fmt(st)} ({per_day:.1f}件/日)")

    # ── R1 月次再検証 (クリーン後) ────────────────────────────────────
    val = slice_period(df, "VAL")
    r1_clean = val[(val["gap_today"] <= -2) & (val["ma25_diff_pct"] <= -5) & (val["traded_value"] >= 1e9)].copy()
    print(f"\n【2. R1+10億 月次 EV (クリーン後)】")
    r1_clean["YM"] = r1_clean["Date"].dt.to_period("M")
    for ym, sub in r1_clean.groupby("YM"):
        st = summ(sub["e1"])
        print(f"  {ym}: n={st['n']:>4} 生平均={st['raw_mean']:+6.3f}% "
              f"W平均={st['W平均']:+6.3f}% 中央={st['中央値']:+6.3f}% 勝率={st['勝率']:>4.1f}%")

    # ── top-N 選別 (クリーン後) ───────────────────────────────────────
    print("\n" + "=" * 92)
    print("【3. R1+10億 top-N 選別 (クリーン後)】")
    print("=" * 92)

    criteria = [
        ("S1: gap_today 深い順",       "gap_today",       True),
        ("S2: ma25_diff_pct 深い順",    "ma25_diff_pct",   True),
        ("S3: day_return 深い順",      "day_return",      True),
        ("S6: relative_atr 高い順",    "relative_atr",    False),
        ("S10: random (対照)",         "random",          False),
    ]

    for period in ["TRAIN", "VAL"]:
        period_df = slice_period(df, period).copy()
        r1 = period_df[
            (period_df["gap_today"] <= -2) &
            (period_df["ma25_diff_pct"] <= -5) &
            (period_df["traded_value"] >= 1e9)
        ].copy()
        n_days = r1["Date"].nunique()
        st_base = summ(r1["e1"])
        print(f"\n[{period}] R1+10億 baseline: {fmt(st_base)}, "
              f"日次平均{len(r1)/n_days:.1f}件")
        print(f"  {'criterion':<28} {'N=1':>17} {'N=3':>17} {'N=5':>17}")
        print("  " + "-" * 82)
        for label, col, asc in criteria:
            if col == "random":
                np.random.seed(42)
                r1["_rank"] = np.random.rand(len(r1))
                rank_col = "_rank"
                asc_eff = False
            else:
                rank_col = col
                asc_eff = asc
            ranked = r1.groupby("Date")[rank_col].rank(ascending=asc_eff, method="first")
            line = f"  {label:<28}"
            for n in [1, 3, 5]:
                sub = r1[ranked <= n]
                st = summ(sub["e1"])
                ev = st.get("W平均", 0)
                wr = st.get("勝率", 0)
                line += f"  {ev:+5.2f}%/{wr:>4.1f}%/{st.get('n',0):>4}"
            print(line)

    # ── S2 (MA25 深い順) で top-3 を取った時の累積 PnL (100 万) ──────
    print("\n" + "=" * 92)
    print("【4. 100万資金で日次 S2 top-3 抽出時の累積 PnL (両期間)】")
    print("=" * 92)

    capital = 1_000_000
    for period in ["TRAIN", "VAL"]:
        period_df = slice_period(df, period).copy()
        r1 = period_df[
            (period_df["gap_today"] <= -2) &
            (period_df["ma25_diff_pct"] <= -5) &
            (period_df["traded_value"] >= 1e9)
        ].copy()
        for N in [1, 2, 3, 5]:
            ranked = r1.groupby("Date")["ma25_diff_pct"].rank(ascending=True, method="first")
            sub = r1[ranked <= N].copy()
            # 1ポジ均等 (1日にN銘柄、各 capital/N)
            daily_avg = sub.groupby("Date")["e1"].mean()
            daily_pnl_pct = daily_avg
            cum_pct = daily_pnl_pct.sum()  # 単純加算 (複利なし、各日100万リセット想定)
            n_days_active = sub["Date"].nunique()
            n_total_days = period_df["Date"].nunique()
            n_winning_days = (daily_avg > 0).sum()
            mean_daily = daily_avg.mean()
            print(f"  [{period}] top-{N}: 活動 {n_days_active}/{n_total_days}日, "
                  f"トレード {len(sub)}件, 平均日次{mean_daily:+.3f}%, "
                  f"日次勝率 {n_winning_days/n_days_active*100:.1f}%, "
                  f"累積{cum_pct:+.1f}% (¥{cum_pct/100*capital:+,.0f})")

    # ── 100万円で買える銘柄か確認 (R1 終値分布) ──────────────────────
    print("\n" + "=" * 92)
    print("【5. 100万資金での現実的ポジション設計 (R1+10億 VAL クリーン)】")
    print("=" * 92)
    val_r1 = slice_period(df, "VAL")
    val_r1 = val_r1[
        (val_r1["gap_today"] <= -2) &
        (val_r1["ma25_diff_pct"] <= -5) &
        (val_r1["traded_value"] >= 1e9)
    ].copy()
    px = val_r1["Close"]
    print(f"R1+10億 銘柄 終値分布 (n={len(val_r1)}):")
    print(f"  中央: {px.median():.0f}円  p25: {px.quantile(0.25):.0f}円  "
          f"p75: {px.quantile(0.75):.0f}円  最大: {px.max():.0f}円")
    val_r1["unit_cost"] = val_r1["Close"] * 100
    print(f"\n1単元(100株)購入額:")
    print(f"  中央: ¥{val_r1['unit_cost'].median():,.0f}  "
          f"p25: ¥{val_r1['unit_cost'].quantile(0.25):,.0f}  "
          f"p75: ¥{val_r1['unit_cost'].quantile(0.75):,.0f}")
    for n_pos in [2, 3, 4, 5]:
        per_pos = capital / n_pos
        affordable = (val_r1["unit_cost"] <= per_pos).mean() * 100
        print(f"  {n_pos}ポジ均等(各¥{per_pos:>7,.0f}): 単元で買える銘柄率 {affordable:>5.1f}%")

    # 単元未満 (S株) 想定での top-3 シミュレーション
    print("\n  ※単元未満買付 (S株) を使う場合: 任意金額で買えるので 100% 銘柄に投資可能")


if __name__ == "__main__":
    main()

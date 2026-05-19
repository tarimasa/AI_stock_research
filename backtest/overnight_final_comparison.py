#!/usr/bin/env python3
"""
backtest/overnight_final_comparison.py

クリーンデータ (株式分割 artifact 除外) で 3 つの戦略を横並び評価:

  C: 現行運用ルール (本番採用予定)
     条件: stage1_score >= 60
     エントリー: T+1 寄付で ATR×1.5 指値 (max_gap_pct≤1.0)
     出口: TP +7.5% / SL -5% / 期日 T+3 終値
     コスト: 0.20%

  R2+中型 (新候補)
     条件: gap_today≤-3 & MA25≤-8 & dayret≤-3 & Close∈[500,3000)
     エントリー: T 終値で 成行
     出口: T+1 寄付決済
     コスト: 0.20%

  E7+30億 (新候補)
     条件: relative_atr top 10% & gap_today≤-2 & traded_value≥3B
     エントリー: T 終値で 成行
     出口: T+1 寄付決済
     コスト: 0.20%

評価:
  - 全期間 EV/件 (clean)
  - 月次安定性 (TRAIN + VAL)
  - 100万円資金での累積 PnL
  - 1件あたり期待リターン vs 1日あたり期待リターン

意思決定基準:
  新ロジックを実装する条件 = C のクリーン EV を「日次期待リターンで上回る」
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
from limit_fill_analyzer import (
    add_next_bars, add_future_bars, tick_round_series, simulate_holding,
)

DATA_DIR = PROJECT_ROOT / "backtest" / "data"
COST = 0.20


def prep_base(df: pd.DataFrame) -> pd.DataFrame:
    """共通の特徴量。"""
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
    return df


def split_filter(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["gap_today"].abs() <= 15) & (df["overnight"].abs() <= 20)].copy()


def slice_period(df, period):
    if period == "TRAIN":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    return df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))]


def summ(s, cap=5.0):
    s = s.dropna()
    if len(s) == 0: return {"n": 0}
    w = s.clip(-cap, cap)
    return {
        "n": len(s),
        "生平均": round(float(s.mean()), 3),
        "W平均": round(float(w.mean()), 3),
        "中央値": round(float(s.median()), 3),
        "勝率":  round(float((s > 0).mean() * 100), 1),
        "stdev": round(float(s.std()), 2),
    }


def fmt(st):
    if st.get("n", 0) == 0: return "(n=0)"
    return (f"n={st['n']:>6,} 生平均={st['生平均']:+6.3f}% "
            f"W平均={st['W平均']:+6.3f}% 中央={st['中央値']:+6.3f}% "
            f"勝率={st['勝率']:>5.1f}% std={st['stdev']:>5.1f}")


def monthly_table(df_sig: pd.DataFrame, ret_col: str, label: str):
    print(f"\n  ▼ {label} 月次 EV (生平均, クリーン)")
    df_sig = df_sig.copy()
    df_sig["YM"] = df_sig["Date"].dt.to_period("M")
    for ym, sub in df_sig.groupby("YM"):
        ret = sub[ret_col].dropna()
        if len(ret) == 0:
            continue
        win = (ret > 0).mean() * 100
        print(f"    {ym}: n={len(ret):>5} 生平均={ret.mean():+6.3f}% "
              f"中央={ret.median():+6.3f}% 勝率={win:>5.1f}%")


def simulate_current_rule(df: pd.DataFrame) -> pd.DataFrame:
    """現行運用ルール (Stage1>=60 + ATR×1.5 指値 + TP/SL/期日3) のシミュレーション。"""
    # 必要な future bars を追加
    df = add_next_bars(df)
    df = add_future_bars(df, days=3)
    df = df[df["next_open"].notna() & df["atr14"].notna()].copy()
    df = df[df["stage1_score"] >= 60].copy()

    # ATR×1.5 指値
    df["limit_atr"] = tick_round_series(df["Close"] - 1.5 * df["atr14"])
    # max_gap_pct ≤ 1.0 (寄付ギャップ過熱排除)
    df["next_gap_pct"] = (df["next_open"] - df["Close"]) / df["Close"] * 100
    df = df[df["next_gap_pct"] <= 1.0].copy()

    # 約定判定: 寄り≤limit or 寄り>limit & 日中安値≤limit
    fill_at_open  = df["next_open"] <= df["limit_atr"]
    fill_intraday = (~fill_at_open) & (df["next_low"] <= df["limit_atr"])
    df["entry_price"] = np.where(
        fill_at_open, df["next_open"],
        np.where(fill_intraday, df["limit_atr"], np.nan),
    )
    df_filled = df[df["entry_price"].notna()].copy()

    # TP+7.5/SL-5/3日 シミュレーション
    ret, _ = simulate_holding(
        df_filled, "entry_price",
        sl_pct=-5.0, tp_pct=7.5, days=3, cost_pct=COST,
    )
    df_filled["e_current"] = ret
    return df_filled


def main():
    print("[final] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()
    df = calc_all_signals(all_data)
    df = prep_base(df)
    df = df[df["next_open"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    df = split_filter(df)
    print(f"[final] クリーン後: {len(df):,} 行")

    # ─────────────────────────────────────────────────────────────────
    # C: 現行運用ルール
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("【C: 現行運用ルール (stage1>=60 + ATR×1.5 指値 + TP+7.5/SL-5/3日)】")
    print("=" * 92)

    df_current = simulate_current_rule(df.copy())
    # 元の Stage1 候補数 (約定前)
    df_stage1 = df[df["stage1_score"] >= 60].copy()

    for period in ["TRAIN", "VAL"]:
        per = slice_period(df_current, period)
        per_stage1 = slice_period(df_stage1, period)
        n_days = per_stage1["Date"].nunique()
        n_signals = len(per_stage1)
        n_filled = len(per)

        print(f"\n[{period}] {n_days}営業日")
        print(f"  Stage1>=60 候補: {n_signals:,} ({n_signals/n_days:.1f}件/日)")
        print(f"  ATR×1.5指値 約定: {n_filled:,} ({n_filled/n_days:.1f}件/日, 約定率 {n_filled/n_signals*100:.1f}%)")
        st = summ(per["e_current"])
        print(f"  約定時のみ EV: {fmt(st)}")
        # 純 EV per signal (約定しない分を 0 として平均)
        net_per_signal = per["e_current"].sum() / n_signals
        print(f"  シグナルあたり純EV (不参戦=0%): {net_per_signal:+.4f}%")
        # 日次期待リターン (100% capital, 全約定銘柄を均等配分)
        daily_pnl = per.groupby("Date")["e_current"].mean()
        print(f"  日次平均PnL (全約定均等配分): {daily_pnl.mean():+.3f}% (std={daily_pnl.std():.2f}, "
              f"活動 {len(daily_pnl)}/{n_days}日)")

        if period == "VAL":
            monthly_table(per, "e_current", "C: 現行ルール VAL")

    # ─────────────────────────────────────────────────────────────────
    # R2+中型
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("【R2+中型 (gap≤-3 & MA25≤-8 & dayret≤-3 & 500-3000円, T+1寄付決済)】")
    print("=" * 92)

    df["e_overnight"] = df["overnight"] - COST
    r2_med = df[
        (df["gap_today"] <= -3) &
        (df["ma25_diff_pct"] <= -8) &
        (df["day_return"] <= -3) &
        (df["Close"] >= 500) & (df["Close"] < 3000)
    ].copy()

    for period in ["TRAIN", "VAL"]:
        per = slice_period(r2_med, period)
        n_days = slice_period(df, period)["Date"].nunique()
        print(f"\n[{period}] {n_days}営業日")
        print(f"  R2+中型 シグナル: {len(per):,} ({len(per)/n_days:.1f}件/日)")
        st = summ(per["e_overnight"])
        print(f"  EV/件: {fmt(st)}")
        daily_pnl = per.groupby("Date")["e_overnight"].mean()
        print(f"  日次平均PnL (シグナル日のみ均等配分): {daily_pnl.mean():+.3f}% (活動 {len(daily_pnl)}/{n_days}日)")
        # 全営業日ベース (活動しない日は 0)
        full_daily = daily_pnl.reindex(slice_period(df, period)["Date"].unique(), fill_value=0)
        print(f"  全営業日 PnL 平均 (活動外=0%): {full_daily.mean():+.3f}%")

    # 月次 (TRAIN + VAL)
    print()
    monthly_table(slice_period(r2_med, "TRAIN"), "e_overnight", "R2+中型 TRAIN")
    monthly_table(slice_period(r2_med, "VAL"), "e_overnight", "R2+中型 VAL")

    # ─────────────────────────────────────────────────────────────────
    # E7+30億
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("【E7+30億 (relative_atr top10% & gap≤-2 & TV≥3B, T+1寄付決済)】")
    print("=" * 92)

    # 期間ごとに atr の閾値計算
    e7_train_thr = slice_period(df, "TRAIN")["relative_atr"].quantile(0.90)
    e7_val_thr   = slice_period(df, "VAL")["relative_atr"].quantile(0.90)
    print(f"  ATR閾値: TRAIN q90={e7_train_thr:.2f}%, VAL q90={e7_val_thr:.2f}%")

    e7_train = slice_period(df, "TRAIN")
    e7_train = e7_train[
        (e7_train["relative_atr"] >= e7_train_thr) &
        (e7_train["gap_today"] <= -2) &
        (e7_train["traded_value"] >= 3e9)
    ].copy()
    e7_val = slice_period(df, "VAL")
    e7_val = e7_val[
        (e7_val["relative_atr"] >= e7_val_thr) &
        (e7_val["gap_today"] <= -2) &
        (e7_val["traded_value"] >= 3e9)
    ].copy()

    for period_name, per in [("TRAIN", e7_train), ("VAL", e7_val)]:
        n_days = slice_period(df, period_name)["Date"].nunique()
        print(f"\n[{period_name}] {n_days}営業日")
        print(f"  E7+30億 シグナル: {len(per):,} ({len(per)/n_days:.1f}件/日)")
        st = summ(per["e_overnight"])
        print(f"  EV/件: {fmt(st)}")
        daily_pnl = per.groupby("Date")["e_overnight"].mean()
        print(f"  日次平均PnL (活動日のみ): {daily_pnl.mean():+.3f}% (活動 {len(daily_pnl)}/{n_days}日)")

    monthly_table(e7_train, "e_overnight", "E7+30億 TRAIN")
    monthly_table(e7_val, "e_overnight", "E7+30億 VAL")

    # ─────────────────────────────────────────────────────────────────
    # 横並びサマリと 100万シミュレーション
    # ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("【100万円資金での累積 PnL シミュレーション (clean データ)】")
    print("=" * 92)

    capital = 1_000_000

    def simulate_100man(per_df: pd.DataFrame, ret_col: str, label: str, period_name: str):
        """各シグナル日に均等配分で 100% capital を投入する想定。"""
        n_total_days = slice_period(df, period_name)["Date"].nunique()
        daily_pnl_pct = per_df.groupby("Date")[ret_col].mean()
        n_active = len(daily_pnl_pct)
        cum_simple = daily_pnl_pct.sum()
        # 複利
        cum_compound = ((1 + daily_pnl_pct / 100).prod() - 1) * 100
        annual_simple = cum_simple * (250 / n_total_days)
        annual_compound = ((1 + cum_compound / 100) ** (250 / n_total_days) - 1) * 100
        max_dd = ((1 + daily_pnl_pct/100).cumprod() / (1 + daily_pnl_pct/100).cumprod().cummax() - 1).min() * 100
        winrate_daily = (daily_pnl_pct > 0).mean() * 100
        print(f"  [{period_name}] {label}")
        print(f"    活動: {n_active}/{n_total_days}日 ({n_active/n_total_days*100:.0f}%) "
              f"日次勝率 {winrate_daily:.1f}%")
        print(f"    単利累積: {cum_simple:+.1f}% (¥{cum_simple/100*capital:+,.0f})")
        print(f"    複利累積: {cum_compound:+.1f}% (¥{cum_compound/100*capital:+,.0f})")
        print(f"    年率(単利): {annual_simple:+.1f}%  年率(複利): {annual_compound:+.1f}%")
        print(f"    最大DD(複利): {max_dd:.1f}%")

    for period in ["TRAIN", "VAL"]:
        print(f"\n  ───────── {period} ─────────")
        per_c = slice_period(df_current, period)
        simulate_100man(per_c, "e_current", "C: 現行ルール", period)
        per_r2 = slice_period(r2_med, period)
        simulate_100man(per_r2, "e_overnight", "R2+中型", period)
        per_e7 = e7_train if period == "TRAIN" else e7_val
        simulate_100man(per_e7, "e_overnight", "E7+30億", period)


if __name__ == "__main__":
    main()

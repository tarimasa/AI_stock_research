#!/usr/bin/env python3
"""
backtest/realistic_ops_pnl.py

現行ルールを「実運用そのもの」で再シミュレーション。

過去の backtest との違い:
  従来: Stage1>=60 全 200-300 候補に ATR×1.5 指値を出し、
       約定した ~15件/日 を集計 (= 99% 活動率)
  実運用: LLM が選ぶ 1-2 件のみに指値を出す → 約定確率は ~8% × 2 件 ≒ 15% 日

検証する Scenario:
  S-A1: LLM が stage1_score 最上位 1 件を毎日推奨 (実運用相当)
  S-A2: LLM が stage1_score 最上位 2 件を推奨
  S-A3: LLM が R-composite (s1高+rsi5低) 最上位 2 件
  S-A4: LLM が R-vol (出来高高) 最上位 2 件
  S-A5: LLM が R-atr (高ボラ) 最上位 2 件
  S-baseline: 従来 backtest 想定 (全 stage1 候補に指値、約定全採用)

各 Scenario で:
  - 推奨数/日
  - うち何件が約定するか (実 fill 率)
  - 約定日 / 全営業日 比率 (= 実活動率)
  - 1 件あたり EV (約定時)
  - 日次 PnL (1ポジ均等配分、100万円資金)
  - 単利・複利累積、最大DD

意思決定:
  実運用 PnL がベースラインの何%か明確にする。
  もし大幅に低ければ「現行ルール圧勝」の前提が崩れる可能性。
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
CAPITAL = 1_000_000


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
    df["next_open"] = g["Open"].shift(-1)
    df["overnight"] = (df["next_open"] - df["Close"]) / df["Close"] * 100
    return df


def split_filter(df):
    return df[(df["gap_today"].abs() <= 15) & (df["overnight"].abs() <= 20)].copy()


def slice_period(df, period):
    if period == "TRAIN":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    return df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))]


def build_recommendations(df: pd.DataFrame, criterion: str, n: int, ascending: bool) -> pd.DataFrame:
    """
    日ごとに criterion で上位 n 件を「推奨」する。
    実運用では LLM が選ぶ部分のシミュレーション。
    """
    if criterion == "all_filled":
        # 従来 backtest 想定: 全候補を推奨扱い
        return df.copy()
    ranked = df.groupby("Date")[criterion].rank(ascending=ascending, method="first")
    return df[ranked <= n].copy()


def apply_atr_limit_and_holding(df: pd.DataFrame) -> pd.DataFrame:
    """推奨に対して ATR×1.5 指値を発注、約定したら TP+7.5/SL-5/3日保有。"""
    df = df.copy()
    df["limit_atr"] = tick_round_series(df["Close"] - 1.5 * df["atr14"])
    df["next_gap_pct"] = (df["next_open"] - df["Close"]) / df["Close"] * 100
    # max_gap_pct ≤ 1.0
    df = df[df["next_gap_pct"] <= 1.0].copy()
    # 寄付 <= 指値 or 寄付後 安値 <= 指値
    fill_at_open  = df["next_open"] <= df["limit_atr"]
    fill_intraday = (~fill_at_open) & (df["next_low"] <= df["limit_atr"])
    df["entry_price"] = np.where(
        fill_at_open, df["next_open"],
        np.where(fill_intraday, df["limit_atr"], np.nan),
    )
    df_filled = df[df["entry_price"].notna()].copy()
    ret, _ = simulate_holding(
        df_filled, "entry_price",
        sl_pct=-5.0, tp_pct=7.5, days=3, cost_pct=COST,
    )
    df_filled["ret"] = ret
    return df, df_filled


def simulate_realistic(period_df: pd.DataFrame, n_total_days: int, label: str,
                        capital: float = CAPITAL, n_pos: int = 2) -> dict:
    """
    実運用シミュレーション:
      - 1日に N 件推奨、ATR×1.5指値発注
      - 約定したものを TP/SL/3日保有
      - 約定数が N 未満ならその割合で資金を使う (残額は cash)
      - 1ポジあたり capital/n_pos 円
    """
    all_dates = sorted(period_df["Date"].unique())

    # 日次 PnL (% on capital)
    daily_pnl_pct = []
    daily_filled_count = []
    fill_count = 0
    rec_count = 0

    for date in all_dates:
        # この日の推奨銘柄
        recs_today = period_df[period_df["Date"] == date]
        # 推奨銘柄のうち、ATR×1.5 指値が約定したもの
        filled_today = recs_today[recs_today["entry_price"].notna()]
        rec_count += len(recs_today)
        fill_count += len(filled_today)

        if len(filled_today) == 0:
            daily_pnl_pct.append(0.0)  # 取引なし
            daily_filled_count.append(0)
            continue

        # 各約定銘柄は capital/n_pos の資金を割り当てる
        # 約定数 m, 推奨数 n_pos → m × (capital/n_pos) を投入、残りは cash
        m = min(len(filled_today), n_pos)
        # 上位 m を取る (推奨順位は既に build_recommendations で適用済)
        invested = filled_today.iloc[:m]
        # 各ポジから得る %PnL on capital
        per_pos_pnl_pct = invested["ret"].sum() * (1 / n_pos)  # 各 1/n_pos 配分
        daily_pnl_pct.append(per_pos_pnl_pct)
        daily_filled_count.append(m)

    daily_pnl_pct = pd.Series(daily_pnl_pct, index=all_dates)
    daily_filled = pd.Series(daily_filled_count, index=all_dates)

    # 全営業日に揃える (取引なしの日も 0 として加える)
    full_dates = sorted(period_df["Date"].unique())
    if len(daily_pnl_pct) < n_total_days:
        # 推奨ゼロの日も 0 として含める
        idx_full = pd.date_range(min(full_dates), max(full_dates), freq="B")
    n_active_days = (daily_pnl_pct != 0).sum()

    cum_simple = daily_pnl_pct.sum()
    # 複利 (1 ポジでも fill した日に資金が動く想定)
    cum_compound = ((1 + daily_pnl_pct / 100).prod() - 1) * 100
    # DD
    eq = (1 + daily_pnl_pct / 100).cumprod()
    dd = (eq / eq.cummax() - 1).min() * 100
    winrate = (daily_pnl_pct > 0).sum() / max(1, n_active_days) * 100

    return {
        "label": label,
        "推奨総数": rec_count,
        "約定総数": fill_count,
        "fill率": round(fill_count / max(1, rec_count) * 100, 1),
        "活動日": n_active_days,
        "全営業日": n_total_days,
        "活動率": round(n_active_days / n_total_days * 100, 1),
        "日次平均": round(daily_pnl_pct.mean(), 4),
        "日次勝率(活動日のみ)": round(winrate, 1),
        "単利累積": round(cum_simple, 1),
        "複利累積": round(cum_compound, 1),
        "最大DD": round(dd, 1),
        "PnL円": round(cum_simple / 100 * capital, 0),
    }


def main():
    print("[realistic] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()
    df = calc_all_signals(all_data)
    df = prep(df)
    df = add_next_bars(df)
    df = add_future_bars(df, days=3)
    df = df[df["next_open"].notna() & df["atr14"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    df = split_filter(df)
    print(f"[realistic] クリーン後: {len(df):,}")

    # stage1>=60 のみ残す
    df = df[df["stage1_score"] >= 60].copy()
    print(f"[realistic] stage1>=60: {len(df):,}")

    # 全候補に ATR×1.5 指値、約定 entry_price 計算
    df_with_entry, df_filled_all = apply_atr_limit_and_holding(df)
    print(f"[realistic] 約定: {len(df_filled_all):,}")

    # ret を df_with_entry にマージ (約定したものだけ ret あり)
    df_with_entry = df_with_entry.merge(
        df_filled_all[["Code", "Date", "ret"]],
        on=["Code", "Date"], how="left"
    )

    # 推奨は df_with_entry (entry_price NaN 含む) に対して行い、約定だけ採用
    # composite ranking
    df_with_entry["composite"] = (
        df_with_entry.groupby("Date")["stage1_score"].rank(ascending=False) +
        df_with_entry.groupby("Date")["rsi5"].rank(ascending=True)
    ) / 2

    scenarios = [
        ("S-A1: stage1 top-1",          "stage1_score",    False, 1),
        ("S-A2: stage1 top-2",          "stage1_score",    False, 2),
        ("S-A3: composite top-2",       "composite",       True,  2),
        ("S-A4: vol_ratio top-2",       "vol_ratio",       False, 2),
        ("S-A5: relative_atr top-2",    "relative_atr",    False, 2),
        ("S-A6: stage1 top-3",          "stage1_score",    False, 3),
        ("S-baseline: 全候補 (従来)",     "all_filled",      False, 999),
    ]

    for period in ["TRAIN", "VAL"]:
        per = slice_period(df_with_entry, period)
        n_days = per["Date"].nunique()
        print(f"\n{'='*108}")
        print(f"【{period}】 stage1>=60: {len(per):,} 件, {n_days}営業日")
        print(f"{'='*108}")
        print(f"  {'シナリオ':<28} {'推奨':>7} {'約定':>6} {'fill%':>6} "
              f"{'活動日':>7} {'活動率':>6} {'日次%':>7} {'日次勝率':>7} "
              f"{'単利%':>7} {'複利%':>9} {'DD%':>7} {'PnL円':>11}")
        print("  " + "-" * 105)

        for label, crit, asc, n in scenarios:
            if crit == "all_filled":
                # 従来 baseline: stage1 全候補に発注、約定取得した全件均等保有 (15件/日均等)
                # 旧 simulate と同等
                df_recs = per
                df_filled = df_recs[df_recs["entry_price"].notna()].copy()
                daily_pnl_pct = df_filled.groupby("Date")["ret"].mean()
                n_active = len(daily_pnl_pct)
                cum_simple = daily_pnl_pct.sum()
                cum_compound = ((1 + daily_pnl_pct/100).prod() - 1) * 100
                eq = (1 + daily_pnl_pct/100).cumprod()
                dd = (eq / eq.cummax() - 1).min() * 100
                winrate = (daily_pnl_pct > 0).mean() * 100
                row = {
                    "推奨総数": len(df_recs),
                    "約定総数": len(df_filled),
                    "fill率": round(len(df_filled) / max(1, len(df_recs)) * 100, 1),
                    "活動日": n_active,
                    "全営業日": n_days,
                    "活動率": round(n_active / n_days * 100, 1),
                    "日次平均": round(daily_pnl_pct.mean(), 4),
                    "日次勝率(活動日のみ)": round(winrate, 1),
                    "単利累積": round(cum_simple, 1),
                    "複利累積": round(cum_compound, 1),
                    "最大DD": round(dd, 1),
                    "PnL円": round(cum_simple / 100 * CAPITAL, 0),
                }
            else:
                recs = build_recommendations(per, crit, n, asc)
                row = simulate_realistic(recs, n_days, label, n_pos=n)
            print(f"  {label:<28} "
                  f"{row['推奨総数']:>7,} {row['約定総数']:>6,} "
                  f"{row['fill率']:>5.1f}% "
                  f"{row['活動日']:>6}/{row['全営業日']} "
                  f"{row['活動率']:>5.1f}% "
                  f"{row['日次平均']:>+6.3f}% "
                  f"{row['日次勝率(活動日のみ)']:>6.1f}% "
                  f"{row['単利累積']:>+6.1f}% "
                  f"{row['複利累積']:>+8.1f}% "
                  f"{row['最大DD']:>+6.1f}% "
                  f"¥{row['PnL円']:>+11,.0f}")


if __name__ == "__main__":
    main()

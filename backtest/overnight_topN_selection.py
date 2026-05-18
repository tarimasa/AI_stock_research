#!/usr/bin/env python3
"""
backtest/overnight_topN_selection.py

R1 (gap≤-2 & MA25≤-5 & 売買代金≥10億) で 1 日 15-20 候補出る中から
top N (N=1,2,3,5) だけ取るとき、どのランク基準が EV を維持/改善するか検証。

検証する選別基準:
  S1. gap_today 深い順       (より大きく gap-down)
  S2. ma25_diff_pct 深い順    (より大きく MA25 から乖離)
  S3. day_return 深い順      (より大きく日中値下げ)
  S4. vol_ratio 高い順       (出来高 spike が大きい)
  S5. stage1_score 高い順    (既存指標)
  S6. relative_atr 高い順    (高ボラ銘柄)
  S7. composite rank (S1+S2+S3 平均ランク)
  S8. close_in_range 高い順  (引け強い = reversal hint)
  S9. lower_shadow 長い順    (下ヒゲ反発線)
  S10. random (対照)

各基準で top N を取った時の VAL/TRAIN 平均 EV、勝率、stdev を比較。
ベースライン: その日の R1 候補全件平均 (= 既知の +0.85%/件)

加えて 100 万円資金での現実的なポジション設計:
  - R1 銘柄の終値分布
  - 1 単元 (100 株) で何銘柄持てるか
  - 単元未満買付 (S株) 利用時の運用パターン
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
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    df["close_in_range"] = ((df["Close"] - df["Low"]) / rng).clip(0, 1)
    df["day_return"]  = (df["Close"] - df["Open"]) / df["Open"] * 100
    df["lower_shadow"] = (df[["Open", "Close"]].min(axis=1) - df["Low"]) / df["Close"] * 100
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
    return df


def e1(d, cost=COST):
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
    return (f"n={st['n']:>5,} W平均={st['W平均']:+6.3f}% "
            f"中央={st['中央値']:+6.3f}% 勝率={st['勝率']:>5.1f}% std={st['stdev']:>5.1f}")


def top_n_by(df: pd.DataFrame, criterion: str, n: int, ascending: bool = False) -> pd.Series:
    """日ごとに criterion で上位 n を選び、そのリターンを返す。"""
    if criterion == "random":
        seed = pd.Series(np.random.RandomState(42).rand(len(df)), index=df.index)
        df = df.assign(_rank=seed)
        ranked = df.groupby("Date")["_rank"].rank(ascending=ascending, method="first")
    else:
        ranked = df.groupby("Date")[criterion].rank(ascending=ascending, method="first")
    return ranked


def main():
    print("[topN] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()

    df = calc_all_signals(all_data)
    df = prep(df)
    df = df[df["next_open"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()

    # R1 + 売買代金≥10億
    df = df[
        (df["gap_today"] <= -2) &
        (df["ma25_diff_pct"] <= -5) &
        (df["traded_value"] >= 1e9)
    ].copy()
    df["e1"] = e1(df)
    print(f"[topN] R1+10億 候補: {len(df):,} 行")
    daily = df.groupby("Date").size()
    print(f"        1日あたり: 平均 {daily.mean():.1f}, 中央 {daily.median():.0f}, "
          f"p10 {daily.quantile(0.1):.0f}, p90 {daily.quantile(0.9):.0f}")

    # ── ランク基準と並び順 (ascending=True なら値が小さい順) ─────────
    criteria = [
        ("S1.  gap_today 深い順",       "gap_today",       True),
        ("S2.  ma25_diff_pct 深い順",    "ma25_diff_pct",   True),
        ("S3.  day_return 深い順",      "day_return",      True),
        ("S4.  vol_ratio 高い順",       "vol_ratio",       False),
        ("S5.  stage1_score 高い順",    "stage1_score",    False),
        ("S6.  relative_atr 高い順",    "relative_atr",    False),
        ("S8.  close_in_range 高い順",  "close_in_range",  False),
        ("S9.  lower_shadow 長い順",    "lower_shadow",    False),
        ("S10. random (対照)",          "random",          False),
    ]

    # S7: composite (S1+S2+S3 平均ランク, 値小さい=深い順)
    df["s7_composite"] = (
        df.groupby("Date")["gap_today"].rank(ascending=True) +
        df.groupby("Date")["ma25_diff_pct"].rank(ascending=True) +
        df.groupby("Date")["day_return"].rank(ascending=True)
    ) / 3
    criteria.append(("S7.  composite (深い順)", "s7_composite", True))

    for period in ["TRAIN", "VAL"]:
        period_df = df[
            (df["Date"] >= pd.Timestamp(eval(f"{period}_START"))) &
            (df["Date"] <= pd.Timestamp(eval(f"{period}_END")))
        ].copy()
        n_days = period_df["Date"].nunique()
        print(f"\n{'='*98}")
        print(f"【{period}】 R1+10億 {len(period_df):,}件 / {n_days}営業日 / "
              f"baseline (全件) {fmt(summ(period_df['e1']))}")
        print(f"{'='*98}")
        print(f"  {'criterion':<28} {'N=1':>10} {'N=2':>10} {'N=3':>10} {'N=5':>10}")
        print("  " + "-" * 80)

        for label, col, asc in criteria:
            ranked = top_n_by(period_df, col, 5, ascending=asc)
            line = f"  {label:<28}"
            for n in [1, 2, 3, 5]:
                sub = period_df[ranked <= n]
                st = summ(sub["e1"])
                ev = st.get("W平均", float("nan"))
                wr = st.get("勝率", float("nan"))
                line += f" {ev:+5.2f}%/{wr:>4.1f}%"
            print(line)

    # ── 100万円資金でのポジション設計分析 ──────────────────────────
    print("\n" + "=" * 98)
    print("【100 万円資金でのポジション設計分析 (R1 銘柄 VAL 期間)】")
    print("=" * 98)

    val = df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))].copy()
    px = val["Close"]
    print(f"\nR1 候補銘柄の終値分布 (VAL n={len(val)}):")
    print(f"  最小: {px.min():>7.0f}円  p10: {px.quantile(0.1):>7.0f}円  "
          f"中央: {px.median():>7.0f}円  p90: {px.quantile(0.9):>7.0f}円  "
          f"最大: {px.max():>7.0f}円")

    # 1 単元 (100 株) 投資額
    val["unit_cost"] = val["Close"] * 100
    print(f"\n1 単元(100株)購入額の分布:")
    print(f"  中央: ¥{val['unit_cost'].median():>10,.0f}  "
          f"p25: ¥{val['unit_cost'].quantile(0.25):>10,.0f}  "
          f"p75: ¥{val['unit_cost'].quantile(0.75):>10,.0f}")

    # 100 万円で何銘柄持てるか (中央値ベース)
    capital = 1_000_000
    for n_pos in [2, 3, 5, 10]:
        per_pos = capital / n_pos
        # 単元購入できる銘柄の割合
        affordable = (val["unit_cost"] <= per_pos).mean() * 100
        print(f"  {n_pos}ポジ均等 (1ポジ ¥{per_pos:>7,.0f}): "
              f"単元購入可能銘柄率 {affordable:>5.1f}%")

    # ── S7 composite で日次 top-3 した時の VAL シミュレーション ───────
    print("\n" + "=" * 98)
    print("【最良ランクで日次 top-N 抽出時の VAL シミュレーション】")
    print("=" * 98)

    # 最も EV を保つ criterion を表示用に確定 (s2 か s7 などになる想定)
    for crit_label, crit_col, crit_asc in criteria:
        if "composite" not in crit_label:
            continue
        ranked = top_n_by(val, crit_col, 5, ascending=crit_asc)
        for n in [1, 2, 3, 5]:
            sub = val[ranked <= n].copy()
            n_trades = len(sub)
            n_days_active = sub["Date"].nunique()
            n_total_days = val["Date"].nunique()
            participation = n_days_active / n_total_days * 100

            # 年間 PnL シミュレーション (1ポジ均等)
            # 1 ポジ 100万 ÷ N とする
            avg_ret = sub["e1"].mean()
            # 単純化: 全期間 (140日) 平均 N 件 → 1日 1万 capital あたりリターン
            # 仮に資金 100万 を N ポジに均等配分 → 1ポジ (100万/N) → リターン (avg_ret/100) × 100万 = avg_ret × 10000
            daily_pnl = (sub.groupby("Date")["e1"].mean()) / 100 * capital
            total_pnl = daily_pnl.sum()
            print(f"\n  ▶ {crit_label}, top-{n}:")
            print(f"    トレード件数: {n_trades} / 活動日 {n_days_active}/{n_total_days} ({participation:.0f}%)")
            print(f"    平均EV: {avg_ret:+.3f}% / トレード")
            print(f"    勝率: {(sub['e1']>0).mean()*100:.1f}%")
            print(f"    VAL 期間累積 PnL (1ポジ均等想定): "
                  f"¥{total_pnl:>+10,.0f}  ({total_pnl/capital*100:+.1f}%)")
            print(f"    年率換算 ({(n_total_days/250)*100:.0f}% 営業日換算): "
                  f"{total_pnl/capital*100 * (250/n_total_days):+.1f}%")

    print("\n[topN] CSV 出力なし (画面のみ)")


if __name__ == "__main__":
    main()

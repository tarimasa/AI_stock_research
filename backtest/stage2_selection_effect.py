#!/usr/bin/env python3
"""
backtest/stage2_selection_effect.py

「Stage2 (Claude LLM) で 1-2 件に絞る」効果の理論検証。

問い: Stage1>=60 + ATR×1.5指値 で約定した複数銘柄 (1日 5-10 件) の中から、
      どんな基準で絞れば baseline (全件平均) を上回れるか?

検証:
  ベースライン: 全約定銘柄の平均 EV
  ランダム top-N: シャッフルしてN件取った時の平均
  特徴量ランキング:
    R-stage1: stage1_score 高い順
    R-rsi5:   rsi5 低い順 (深い oversold)
    R-rsi14:  rsi14 低い順
    R-ma25:   ma25_diff_pct 深い順
    R-dvs:    dvs 高い順
    R-vol:    vol_ratio 高い順
    R-atr:    relative_atr 高い順
    R-breakout: breakout_5d ある銘柄を優先
    R-composite: stage1 + 浅い rsi5 の合成ランク
  完全予見 (上限):
    Perfect: 各日 actual return が最大の銘柄を 1 件取る
    Worst:   各日 actual return が最小の銘柄

評価指標:
  - 件あたり EV (生平均/W平均/中央値/勝率)
  - 日次 PnL (100万円資金、選んだ N 件均等配分)
  - VAL 累積 PnL 比較

意思決定:
  もし「上位ランキング基準で +0.5%/件 以上の改善」が両期間で出れば LLM は
  そのロジックを実装する価値あり。逆に random と差がなければ LLM 絞り込みは
  「期待値中立」(平均並み) で運用すべし。
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
    return df


def split_filter(df):
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
    }


def simulate_current_rule(df: pd.DataFrame) -> pd.DataFrame:
    """現行ルールでの約定銘柄を返す。"""
    df = add_next_bars(df)
    df = add_future_bars(df, days=3)
    df = df[df["next_open"].notna() & df["atr14"].notna()].copy()
    df = df[df["stage1_score"] >= 60].copy()
    df["limit_atr"] = tick_round_series(df["Close"] - 1.5 * df["atr14"])
    df["next_gap_pct"] = (df["next_open"] - df["Close"]) / df["Close"] * 100
    df = df[df["next_gap_pct"] <= 1.0].copy()
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
    df_filled["e_current"] = ret
    return df_filled


def main():
    print("[stage2] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()
    df = calc_all_signals(all_data)
    df = prep(df)
    df = df[df["next_open"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    df = split_filter(df)
    print(f"[stage2] クリーン後: {len(df):,} 行")

    # 現行ルールで約定したものに絞る
    df_filled = simulate_current_rule(df.copy())
    print(f"[stage2] 現行ルール約定銘柄: {len(df_filled):,}")

    # ─────────────────────────────────────────────────────────────────
    # 各期間で baseline と各ランキング戦略を比較
    # ─────────────────────────────────────────────────────────────────
    for period in ["TRAIN", "VAL"]:
        per = slice_period(df_filled, period)
        n_days = per["Date"].nunique()
        avg_per_day = len(per) / n_days
        print(f"\n{'='*98}")
        print(f"【{period}】 約定 {len(per):,} 件 / {n_days}営業日 / 平均 {avg_per_day:.1f}件/日")
        print(f"{'='*98}")

        base = per["e_current"]
        st_base = summ(base)
        print(f"  ベースライン (全約定平均): n={st_base['n']:,} 生平均={st_base['生平均']:+.3f}% "
              f"W平均={st_base['W平均']:+.3f}% 中央={st_base['中央値']:+.3f}% 勝率={st_base['勝率']:.1f}%")

        # ── ランキング戦略 ────────────────────────────────────────
        criteria = [
            ("R-stage1 (高い順)",      "stage1_score",  False),
            ("R-rsi5 (低い順=深押)",   "rsi5",          True),
            ("R-rsi14 (低い順)",       "rsi14",         True),
            ("R-ma25 (深乖離順)",      "ma25_diff_pct", True),
            ("R-dvs (高い順)",         "dvs",           False),
            ("R-vol (高い順)",         "vol_ratio",     False),
            ("R-atr (高い順)",         "relative_atr",  False),
        ]

        # composite: stage1 + (1-rsi5/100) の rank 平均
        per = per.copy()
        per["composite"] = (
            per.groupby("Date")["stage1_score"].rank(ascending=False) +
            per.groupby("Date")["rsi5"].rank(ascending=True)
        ) / 2

        criteria.append(("R-composite (s1高+rsi5低)", "composite", True))

        # Perfect foresight (上限)
        per["_perfect"] = per["e_current"]  # already actual
        criteria.append(("◆ Perfect (上限)",          "_perfect",     False))
        criteria.append(("◆ Worst (下限)",             "_perfect",      True))

        # random (対照)
        np.random.seed(42)
        per["_rand"] = np.random.rand(len(per))
        criteria.append(("R-random (対照)",           "_rand",         False))

        print(f"\n  {'criterion':<26} {'N=1':>16} {'N=2':>16} {'N=3':>16} {'N=5':>16}")
        print("  " + "-" * 92)
        for label, col, asc in criteria:
            ranked = per.groupby("Date")[col].rank(ascending=asc, method="first")
            line = f"  {label:<26}"
            for n in [1, 2, 3, 5]:
                sub = per[ranked <= n]["e_current"]
                if len(sub) == 0:
                    line += f"  {'(empty)':>14}"
                    continue
                ev = sub.mean()
                wr = (sub > 0).mean() * 100
                line += f"  {ev:+5.2f}%/{wr:>4.1f}%"
            print(line)

        # ── 100万円資金 シミュレーション (N=2, baseline+各 criterion) ──
        print(f"\n  ▼ 100万円資金 累積 PnL (N=2 で日次均等配分)")
        capital = 1_000_000
        # baseline (全約定均等配分)
        daily_all = per.groupby("Date")["e_current"].mean()
        n_active = len(daily_all)
        cum = daily_all.sum()
        comp = ((1 + daily_all/100).prod() - 1) * 100
        print(f"    baseline (全約定均等): 活動{n_active}/{n_days}日 "
              f"単利{cum:+.1f}% 複利{comp:+.1f}% (¥{cum/100*capital:+,.0f})")

        for label, col, asc in criteria:
            if "Perfect" in label or "Worst" in label or "random" in label:
                pass  # 上限・下限と対照は表示する
            elif "stage1" not in label and "composite" not in label:
                continue  # 紙面節約: stage1/composite/perfect/worst のみ表示
            ranked = per.groupby("Date")[col].rank(ascending=asc, method="first")
            sub = per[ranked <= 2].copy()
            daily = sub.groupby("Date")["e_current"].mean()
            cum = daily.sum()
            comp = ((1 + daily/100).prod() - 1) * 100
            print(f"    {label:<26}: 活動{len(daily)}/{n_days}日 "
                  f"単利{cum:+.1f}% 複利{comp:+.1f}% (¥{cum/100*capital:+,.0f})")

    # ─────────────────────────────────────────────────────────────────
    # 特徴量の overnight 予測力 (約定銘柄に限定して)
    # ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*98}")
    print("【補足: 約定銘柄群で各特徴量と e_current のランク相関】")
    print(f"{'='*98}")

    feats = ["stage1_score", "rsi5", "rsi14", "ma25_diff_pct", "dvs",
             "vol_ratio", "relative_atr", "gap_today", "day_return"]

    for period in ["TRAIN", "VAL"]:
        per = slice_period(df_filled, period)
        print(f"\n[{period}] n={len(per):,}")
        for f in feats:
            if f not in per.columns:
                continue
            sub = per[[f, "e_current"]].dropna()
            if len(sub) < 100:
                continue
            corr = sub[f].rank().corr(sub["e_current"].rank())
            print(f"  {f:<20}: rank相関 = {corr:+.4f}")


if __name__ == "__main__":
    main()

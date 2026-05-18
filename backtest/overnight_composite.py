#!/usr/bin/env python3
"""
backtest/overnight_composite.py

overnight_features.py で予測力を測った単体特徴量を組み合わせて、
「T 終値で買い、T+1 寄付/終値/TP-SL で出る」 EV を最大化する複合ルールを設計。

VAL で diff の符号がはっきりした特徴量 (Winsorize top-bot diff):
  反転系 (BOT 10% が prevail = oversold mean reversion):
    - ma25_diff_pct  (deeply below MA25 → bounce)
    - gap_today      (gap-down today → gap-up tomorrow)
    - day_return     (today losers → bounce)
    - dvs            (selling pressure → bounce)
  順張り系 (TOP 10% が prevail):
    - upper_shadow   (long upper shadow → ?)
    - relative_atr   (high vol → mean revert)
    - range_pct      (wide range → ?)

設計するルール:
  R1: oversold MR   = gap_today ≤ -2% AND ma25_diff_pct ≤ -5%
  R2: deep MR       = gap_today ≤ -3% AND ma25_diff_pct ≤ -8% AND day_return ≤ -3%
  R3: volatility    = relative_atr ≥ q90
  R4: composite MR  = (gap_today, ma25_diff_pct, day_return, dvs) の rank 平均 上位X%
  R5: stage1強 + MR  = stage1_score >= 80 AND gap_today ≤ -1%

出口別 EV (手数料 0.2% 引き):
  E1: T+1 寄付決済 (overnight only)
  E2: T+1 終値決済 (1日保有)
  E3: TP +3% / SL -2% / 期日 T+2 終値

評価軸:
  - 各ルールの参戦数 (年あたり想定)
  - 各出口の EV(W平均) / 中央値 / 勝率
  - TRAIN/VAL 比較
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


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Code", "Date"]).reset_index(drop=True)
    g = df.groupby("Code", group_keys=False)
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    df["close_in_range"] = ((df["Close"] - df["Low"]) / rng).clip(0, 1)
    df["day_return"]     = (df["Close"] - df["Open"]) / df["Open"] * 100
    df["upper_shadow"]   = (df["High"] - df[["Open", "Close"]].max(axis=1)) / df["Close"] * 100
    df["range_pct"]      = (df["High"] - df["Low"]) / df["Close"] * 100
    df["vol_ma20"]       = g["Volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["vol_ratio"]      = df["Volume"] / df["vol_ma20"].replace(0, np.nan)
    df["prev_close"]     = g["Close"].shift(1)
    df["gap_today"]      = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100

    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - g["Close"].shift(1)).abs(),
        (df["Low"]  - g["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = g["Date"].transform(lambda x: tr.loc[x.index].rolling(14, min_periods=7).mean())
    df["relative_atr"] = df["atr14"] / df["Close"] * 100

    # future bars
    df["next_open"]  = g["Open"].shift(-1)
    df["next_high"]  = g["High"].shift(-1)
    df["next_low"]   = g["Low"].shift(-1)
    df["next_close"] = g["Close"].shift(-1)
    df["d2_close"]   = g["Close"].shift(-2)
    return df


def compute_exits(d: pd.DataFrame, sl: float, tp: float, cost: float):
    """3 種類の出口リターン(%) を計算。"""
    entry = d["Close"]
    # E1: T+1 寄付決済
    e1 = (d["next_open"] - entry) / entry * 100 - cost
    # E2: T+1 終値決済
    e2 = (d["next_close"] - entry) / entry * 100 - cost
    # E3: T+1 中 TP/SL → 触れなければ T+2 終値で決済
    sl_p = entry * (1 + sl / 100)
    tp_p = entry * (1 + tp / 100)
    e3 = pd.Series(np.nan, index=d.index)
    o_sl = d["next_open"] <= sl_p
    e3.loc[o_sl] = (np.minimum(d.loc[o_sl, "next_open"], sl_p) / entry.loc[o_sl] - 1) * 100
    o_tp = (~o_sl) & (d["next_open"] >= tp_p)
    e3.loc[o_tp] = (d.loc[o_tp, "next_open"] / entry.loc[o_tp] - 1) * 100
    l_sl = (~o_sl) & (~o_tp) & (d["next_low"] <= sl_p)
    e3.loc[l_sl] = sl
    h_tp = (~o_sl) & (~o_tp) & (~l_sl) & (d["next_high"] >= tp_p)
    e3.loc[h_tp] = tp
    remain = e3.isna()
    e3.loc[remain] = (d.loc[remain, "d2_close"] / entry.loc[remain] - 1) * 100
    e3 = e3 - cost
    return e1, e2, e3


def stats(s: pd.Series, cap=5.0) -> dict:
    s = s.dropna()
    if len(s) == 0:
        return {}
    w = s.clip(-cap, cap)
    return {
        "n":     len(s),
        "W平均":  round(float(w.mean()), 3),
        "中央値": round(float(s.median()), 3),
        "勝率":   round(float((s > 0).mean() * 100), 1),
        "stdev": round(float(s.std()), 2),
    }


def eval_rule(name: str, df_p: pd.DataFrame, mask: pd.Series, sl, tp, cost) -> list[dict]:
    sub = df_p[mask].copy()
    if len(sub) == 0:
        return []
    e1, e2, e3 = compute_exits(sub, sl, tp, cost)
    rows = []
    for exit_name, ret in [
        ("E1: T+1寄付", e1),
        ("E2: T+1終値", e2),
        (f"E3: TP+{tp}/SL{sl}/期日T+2", e3),
    ]:
        st = stats(ret)
        st["rule"] = name
        st["exit"] = exit_name
        rows.append(st)
    return rows


def make_rules(df: pd.DataFrame) -> dict:
    """ルール毎の bool mask を作る。"""
    rules = {}

    rules["R0: 全 (ベースライン)"] = pd.Series(True, index=df.index)

    rules["R1: oversold (gap≤-2 & MA25≤-5)"] = (
        (df["gap_today"] <= -2) & (df["ma25_diff_pct"] <= -5)
    )
    rules["R2: deep oversold (gap≤-3 & MA25≤-8 & dayret≤-3)"] = (
        (df["gap_today"] <= -3) & (df["ma25_diff_pct"] <= -8) & (df["day_return"] <= -3)
    )
    rules["R3: 高ボラ (relative_atr top 10%)"] = (
        df["relative_atr"] >= df["relative_atr"].quantile(0.90)
    )

    # R4: 反転系 4 指標の rank 平均
    rank_gap = df["gap_today"].rank(ascending=True)
    rank_ma  = df["ma25_diff_pct"].rank(ascending=True)
    rank_dr  = df["day_return"].rank(ascending=True)
    rank_dvs = df["dvs"].rank(ascending=True)
    mr_score = (rank_gap + rank_ma + rank_dr + rank_dvs) / 4
    rules["R4: 反転 composite (上位5%)"] = mr_score >= mr_score.quantile(0.95)
    rules["R4b: 反転 composite (上位1%)"] = mr_score >= mr_score.quantile(0.99)

    rules["R5: Stage1強+gap- (s1>=80 & gap≤-1)"] = (
        (df["stage1_score"] >= 80) & (df["gap_today"] <= -1)
    )

    rules["R6: 現行 Stage1 (s1>=60)"] = df["stage1_score"] >= 60

    # R7: upper shadow + 高ボラ
    rules["R7: 上ヒゲ+高ボラ"] = (
        (df["upper_shadow"] >= df["upper_shadow"].quantile(0.90)) &
        (df["relative_atr"] >= df["relative_atr"].quantile(0.75))
    )

    # R8: 反転 + 上ヒゲなし (純粋な投売り検出)
    rules["R8: 反転 + 上ヒゲ無"] = (
        (df["gap_today"] <= -2) & (df["ma25_diff_pct"] <= -5) &
        (df["upper_shadow"] <= 0.3)
    )

    return rules


def main():
    SL = -2.0
    TP = 3.0
    COST = 0.20

    print(f"[composite] TP/SL=+{TP}/{SL}%, cost={COST}%")
    print("[composite] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()

    df = calc_all_signals(all_data)
    df = add_features(df)
    df = df[df["next_open"].notna() & df["next_close"].notna() & df["d2_close"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    print(f"[composite] 評価行数: {len(df):,}")

    rules = make_rules(df)

    train = df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    val   = df[(df["Date"] >= pd.Timestamp(VAL_START))   & (df["Date"] <= pd.Timestamp(VAL_END))]
    train_days = train["Date"].nunique()
    val_days   = val["Date"].nunique()

    all_rows = []
    for period_name, period_df, n_days in [("TRAIN", train, train_days), ("VAL", val, val_days)]:
        print(f"\n{'='*108}")
        print(f"【{period_name}】 {len(period_df):,} 行 / {n_days} 営業日 / "
              f"{period_df['Date'].min().date()} 〜 {period_df['Date'].max().date()}")
        print(f"{'='*108}")
        print(f"  {'rule':<50} {'exit':<25} {'n':>7} {'件/日':>6} "
              f"{'W平均':>8} {'中央値':>8} {'勝率':>6} {'stdev':>6}")
        print("  " + "-" * 106)

        rule_masks = make_rules(period_df)
        for rname, mask_global in rules.items():
            # period_df の局所マスクを使い直す (rank 系がデータ範囲依存のため)
            mask = rule_masks[rname]
            rows = eval_rule(rname, period_df, mask, SL, TP, COST)
            for r in rows:
                r["period"] = period_name
                r["per_day"] = round(r["n"] / n_days, 2) if r.get("n") else 0
                print(f"  {r['rule']:<50} {r['exit']:<25} "
                      f"{r['n']:>7,} {r['per_day']:>6.1f} "
                      f"{r['W平均']:>+7.3f}% {r['中央値']:>+7.3f}% "
                      f"{r['勝率']:>5.1f}% {r['stdev']:>6.2f}")
                all_rows.append(r)
            print()

    pd.DataFrame(all_rows).to_csv(
        DATA_DIR / "overnight_composite.csv", index=False, encoding="utf-8-sig",
    )
    print(f"\nCSV: {DATA_DIR / 'overnight_composite.csv'}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
backtest/overnight_robustness.py

R1/R2 (oversold mean reversion) ルールのロバスト性検証 7 項目:

1. イベント混入率: 大型 news/決算 と思しき日 (vol≥3x & |dayret|≥3%) の割合と EV 寄与
2. クリーンサブセット EV (イベント除外後)
3. 月次 EV 安定性: VAL 期間を月別に分解して山谷を見る
4. 銘柄集中度: 同一銘柄が何度シグナルを出してるか上位 N、その寄与
5. セクター集中度 (33業種)
6. 出来高/価格分布: 取引コスト・スリッページ現実性
7. 連続シグナル: 同一銘柄が連日シグナル (落ち続け) の場合の EV

出口は最良だった E3 (TP+3%/SL-2%/期日 T+2 終値) で評価。
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

SL = -2.0
TP = 3.0
COST = 0.20


def prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Code", "Date"]).reset_index(drop=True)
    g = df.groupby("Code", group_keys=False)
    df["day_return"] = (df["Close"] - df["Open"]) / df["Open"] * 100
    df["vol_ma20"] = g["Volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["vol_ratio"] = df["Volume"] / df["vol_ma20"].replace(0, np.nan)
    df["prev_close"] = g["Close"].shift(1)
    df["gap_today"] = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100
    df["next_open"] = g["Open"].shift(-1)
    df["next_high"] = g["High"].shift(-1)
    df["next_low"] = g["Low"].shift(-1)
    df["next_close"] = g["Close"].shift(-1)
    df["d2_close"] = g["Close"].shift(-2)
    df["traded_value"] = df["Close"] * df["Volume"]
    return df


def compute_e3(d: pd.DataFrame, sl, tp, cost):
    entry = d["Close"]
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
    return e3 - cost


def compute_e1(d: pd.DataFrame, cost):
    return (d["next_open"] - d["Close"]) / d["Close"] * 100 - cost


def summ(s: pd.Series, cap=5.0) -> str:
    s = s.dropna()
    if len(s) == 0: return "(n=0)"
    w = s.clip(-cap, cap)
    return (f"n={len(s):>6,} W平均={w.mean():+6.3f}% 中央値={s.median():+6.3f}% "
            f"勝率={(s>0).mean()*100:>5.1f}% std={s.std():>5.1f}")


def main():
    print("[robust] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    n_total_codes = all_data["Code"].nunique()
    n_valid_codes = len(valid)
    print(f"  全銘柄: {n_total_codes:,} / 最新日フィルタ後: {n_valid_codes:,}")

    # ── 0. Survivorship bias 検証 (フィルタしない版でも EV を計算) ──────
    all_unfilt = calc_all_signals(all_data)
    all_unfilt = prep(all_unfilt)
    all_unfilt = all_unfilt[
        all_unfilt["next_open"].notna() & all_unfilt["next_close"].notna() &
        all_unfilt["d2_close"].notna()
    ].copy()
    all_unfilt = all_unfilt[(all_unfilt["Close"] >= 500) & (all_unfilt["Volume"] >= 100_000)].copy()

    # フィルタ後
    df = all_unfilt[all_unfilt["Code"].isin(valid)].copy()

    for label, frame in [("(A) survivor のみ(現フィルタ)", df),
                         ("(B) 上場廃止含む全銘柄", all_unfilt)]:
        train = frame[(frame["Date"] >= pd.Timestamp(TRAIN_START)) & (frame["Date"] <= pd.Timestamp(TRAIN_END))]
        val   = frame[(frame["Date"] >= pd.Timestamp(VAL_START))   & (frame["Date"] <= pd.Timestamp(VAL_END))]
        for period_name, period_df in [("TRAIN", train), ("VAL", val)]:
            r1 = period_df[(period_df["gap_today"] <= -2) & (period_df["ma25_diff_pct"] <= -5)]
            r2 = period_df[(period_df["gap_today"] <= -3) & (period_df["ma25_diff_pct"] <= -8) & (period_df["day_return"] <= -3)]
            print(f"\n{label} [{period_name}]")
            print(f"  R1 E3: {summ(compute_e3(r1, SL, TP, COST))}")
            print(f"  R2 E3: {summ(compute_e3(r2, SL, TP, COST))}")
            print(f"  R1 E1: {summ(compute_e1(r1, COST))}")

    # ── 以降は survivor フィルタ後で詳細分析 ─────────────────────────
    print("\n" + "="*88)
    print("以降の詳細分析は (A) survivor 後で実施")
    print("="*88)

    val = df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))].copy()
    r1 = val[(val["gap_today"] <= -2) & (val["ma25_diff_pct"] <= -5)].copy()
    r2 = val[(val["gap_today"] <= -3) & (val["ma25_diff_pct"] <= -8) & (val["day_return"] <= -3)].copy()
    r1["e3"] = compute_e3(r1, SL, TP, COST)
    r2["e3"] = compute_e3(r2, SL, TP, COST)
    r1["e1"] = compute_e1(r1, COST)
    r2["e1"] = compute_e1(r2, COST)
    r1["is_event"] = (r1["vol_ratio"] >= 3.0) & (r1["day_return"].abs() >= 3.0)
    r2["is_event"] = (r2["vol_ratio"] >= 3.0) & (r2["day_return"].abs() >= 3.0)

    # ── 1. イベント混入率 ─────────────────────────────────────────────
    print("\n【1. 大型イベント proxy (vol≥3x & |dayret|≥3%) 混入率と寄与】")
    for name, sub in [("R1", r1), ("R2", r2)]:
        ev_rate = sub["is_event"].mean() * 100
        print(f"  {name}: 全 {len(sub)} 件中 イベント {sub['is_event'].sum()} 件 ({ev_rate:.1f}%)")
        print(f"    イベント: {summ(sub.loc[sub['is_event'], 'e3'])}")
        print(f"    クリーン: {summ(sub.loc[~sub['is_event'], 'e3'])}")
        print(f"    E1 クリーン: {summ(sub.loc[~sub['is_event'], 'e1'])}")

    # ── 2. 月次 EV 安定性 (VAL) ──────────────────────────────────────
    print("\n【2. 月次 EV 安定性 (VAL, R2 E3)】")
    r2["YM"] = r2["Date"].dt.to_period("M")
    monthly = r2.groupby("YM")["e3"].agg(["count", "mean", "median"])
    monthly["winrate"] = r2.groupby("YM")["e3"].apply(lambda x: (x > 0).mean() * 100)
    print(monthly.round(3).to_string())

    print("\n  同 R1 E3:")
    r1["YM"] = r1["Date"].dt.to_period("M")
    monthly_r1 = r1.groupby("YM")["e3"].agg(["count", "mean", "median"])
    monthly_r1["winrate"] = r1.groupby("YM")["e3"].apply(lambda x: (x > 0).mean() * 100)
    print(monthly_r1.round(3).to_string())

    # ── 3. 銘柄集中度 ─────────────────────────────────────────────────
    print("\n【3. 銘柄集中度 (R2 VAL, 上位 10 銘柄)】")
    top_codes = r2.groupby("Code").agg(
        n=("e3", "count"), ev_mean=("e3", "mean"), ev_median=("e3", "median"),
        wr=("e3", lambda x: (x > 0).mean() * 100),
    ).sort_values("n", ascending=False).head(10)
    print(top_codes.round(3).to_string())
    n_unique = r2["Code"].nunique()
    print(f"  R2 シグナル銘柄数: {n_unique} / 計 {len(r2)} 件 → 1銘柄あたり平均 {len(r2)/n_unique:.1f} 回")

    # ── 4. 連続シグナル (同一銘柄が直前 5 日にも R2 シグナル発生) ──────
    print("\n【4. 連続シグナル: 直前 5 営業日にも R2 検出された場合の EV】")
    r2_sorted = r2.sort_values(["Code", "Date"]).reset_index(drop=True)
    r2_sorted["prev_date"] = r2_sorted.groupby("Code")["Date"].shift(1)
    r2_sorted["days_since_last"] = (r2_sorted["Date"] - r2_sorted["prev_date"]).dt.days
    r2_sorted["is_repeat"] = r2_sorted["days_since_last"].fillna(999) <= 7
    print(f"  初回シグナル (前回から>7営業日): {summ(r2_sorted.loc[~r2_sorted['is_repeat'], 'e3'])}")
    print(f"  連続シグナル (前回から≤7日):    {summ(r2_sorted.loc[ r2_sorted['is_repeat'], 'e3'])}")

    # ── 5. 出来高/価格分布 (R2 VAL) ───────────────────────────────────
    print("\n【5. R2 VAL 銘柄の取引可能性 (Close × Volume = 売買代金)】")
    tv = r2["traded_value"]
    print(f"  売買代金 (円) 分布:")
    print(f"    最小: ¥{tv.min():>15,.0f}")
    print(f"    p10:  ¥{tv.quantile(0.10):>15,.0f}")
    print(f"    中央: ¥{tv.median():>15,.0f}")
    print(f"    p90:  ¥{tv.quantile(0.90):>15,.0f}")
    print(f"    最大: ¥{tv.max():>15,.0f}")
    # 1売買代金 < 1億円 は流動性懸念
    low_liq = (tv < 100_000_000).sum()
    print(f"  売買代金 < 1億円: {low_liq}/{len(r2)} 件 ({low_liq/len(r2)*100:.1f}%)")
    print(f"  R2 (流動性 ≥ 1億円のみ) E3: {summ(r2.loc[tv >= 100_000_000, 'e3'])}")
    print(f"  R2 (流動性 ≥ 5億円のみ) E3: {summ(r2.loc[tv >= 500_000_000, 'e3'])}")

    # ── 6. R2 のサンプル銘柄 (実際の挙動を見るため上位 15 件出力) ──────
    print("\n【6. R2 VAL 直近 15 件のサンプル (実銘柄の挙動)】")
    sample = r2.sort_values("Date").tail(15)[
        ["Date", "Code", "Close", "gap_today", "day_return", "ma25_diff_pct",
         "vol_ratio", "is_event", "e1", "e3"]
    ]
    sample = sample.copy()
    sample["Date"] = sample["Date"].dt.strftime("%Y-%m-%d")
    sample = sample.round(2)
    print(sample.to_string(index=False))

    # ── 7. 売り抜けタイミング ─────────────────────────────────────────
    print("\n【7. 出口比較 (R2 VAL, 各出口の EV)】")
    for ex_name, fn in [("T+1 寄付 (一晩のみ)", lambda d: compute_e1(d, COST)),
                        ("TP+3/SL-2/期日T+2", lambda d: compute_e3(d, SL, TP, COST))]:
        for sub_name, sub in [("全 R2", r2), ("R2 クリーン", r2[~r2["is_event"]]),
                              ("R2 流動性≥5億", r2[r2["traded_value"] >= 500_000_000])]:
            print(f"  {ex_name:<22} | {sub_name:<14}: {summ(fn(sub))}")

    print("\n[robust] 完了")


if __name__ == "__main__":
    main()

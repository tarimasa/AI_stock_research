#!/usr/bin/env python3
"""
backtest/overnight_def_sweep.py

D + E + F を一括検証。

D: R1 (緩い条件) の詳細検証
  R1 = gap_today≤-2 AND ma25_diff_pct≤-5
  R2 の検証項目 (survivor, monthly, event-score) を R1 にも適用

E: 別のシグナルロジック探索 (継続系/逆張りバリエーション)
  E1: overnight_bias_5 top 10% (継続)
  E2: hammer (lower_shadow 長 + close_in_range 高)
  E3: inside_bar_breakout (今日 range < 昨日 & 陽線)
  E4: 3日連騰 + 出来高増 (継続)
  E5: 52週安値接触 + 反発線
  E6: stage1強 + 当日急落 (Stage1 通過の中で逆張り)
  E7: 高ボラ + ギャップダウン (relative_atr top 10% AND gap ≤ -2)
  E8: 当日 close_in_range top (引け強) + 出来高増

F: ユニバースフィルタ (R1, R2, 最良 E に適用)
  F1: 売買代金 ≥ 3億 / 10億 / 30億 / 100億
  F2: 終値 500-1500 / 1500-5000 / 5000+ 円
  F3: 終値 × 出来高 (大型/中型/小型) との交差検証

全評価は T+1 寄付決済 (シンプル) で測定。
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
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    df["close_in_range"] = ((df["Close"] - df["Low"]) / rng).clip(0, 1)
    df["upper_shadow"]  = (df["High"] - df[["Open", "Close"]].max(axis=1)) / df["Close"] * 100
    df["lower_shadow"]  = (df[["Open", "Close"]].min(axis=1) - df["Low"]) / df["Close"] * 100
    df["range_pct"]     = (df["High"] - df["Low"]) / df["Close"] * 100
    df["body_pct"]      = (df["Close"] - df["Open"]) / df["Close"] * 100

    df["vol_ma20"]   = g["Volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["vol_ratio"]  = df["Volume"] / df["vol_ma20"].replace(0, np.nan)
    df["prev_close"] = g["Close"].shift(1)
    df["gap_today"]  = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100

    # ATR
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - g["Close"].shift(1)).abs(),
        (df["Low"]  - g["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = g["Date"].transform(lambda x: tr.loc[x.index].rolling(14, min_periods=7).mean())
    df["relative_atr"] = df["atr14"] / df["Close"] * 100

    # overnight_bias_5
    overnight = (df["Open"] - g["Close"].shift(1)) / g["Close"].shift(1) * 100
    df["overnight_bias_5"]  = g["Open"].transform(
        lambda x: overnight.loc[x.index].rolling(5, min_periods=3).mean()
    )

    # 連騰本数
    is_up = (df["Close"] > df["Open"]).astype(int)
    def _streak(s):
        out, c = [], 0
        for v in s:
            c = c + 1 if v == 1 else 0
            out.append(c)
        return pd.Series(out, index=s.index)
    df["up_streak"] = g.apply(lambda d: _streak(is_up.loc[d.index])).reset_index(level=0, drop=True)

    # 昨日 range
    df["prev_range"] = g["range_pct"].shift(1)

    # 52週(252日) 安値
    df["low_252"] = g["Low"].transform(lambda x: x.rolling(252, min_periods=60).min())
    df["near_52w_low"] = df["Low"] <= df["low_252"] * 1.02

    # event score (前回と同じ)
    s = pd.Series(0, index=df.index, dtype=int)
    s += (df["vol_ratio"] >= 3.0).astype(int)
    s += (df["vol_ratio"] >= 5.0).astype(int)
    s += (df["range_pct"] >= 8.0).astype(int)
    s += (df["gap_today"].abs() >= 5.0).astype(int)
    s += (df["day_return"].abs() >= 5.0).astype(int)
    pre_vol_max = g["vol_ratio"].transform(lambda x: x.shift(1).rolling(3, min_periods=1).max())
    s += (pre_vol_max >= 2.0).astype(int)
    df["event_score"] = s

    # 売買代金
    df["traded_value"] = df["Close"] * df["Volume"]

    # 未来バー
    df["next_open"]  = g["Open"].shift(-1)
    df["next_close"] = g["Close"].shift(-1)
    return df


def e1_open(d, cost=COST):
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


def slice_period(df, period):
    if period == "TRAIN":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    return df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))]


def section_D(df: pd.DataFrame, rows: list):
    """R1 の R2 同等検証"""
    print("\n" + "=" * 92)
    print("D: R1 (gap≤-2 & MA25≤-5) の詳細検証")
    print("=" * 92)

    for period in ["TRAIN", "VAL"]:
        p = slice_period(df, period)
        n_days = p["Date"].nunique()
        print(f"\n[{period}] {n_days}営業日")

        r1 = p[(p["gap_today"] <= -2) & (p["ma25_diff_pct"] <= -5)].copy()
        e1 = e1_open(r1)
        st = summ(e1)
        print(f"  R1 全件: {fmt(st)}  ({st.get('n',0)/n_days:.1f}件/日)")
        rows.append({**st, "rule": "D_R1_all", "period": period})

        # event_score 別
        print("  --- R1 event_score 別 ---")
        for sc in range(0, 7):
            sub = r1[r1["event_score"] == sc]
            if len(sub) > 0:
                e = e1_open(sub)
                print(f"    score={sc}: {fmt(summ(e))}")

        # 月次安定性
        if period == "VAL":
            print("  --- R1 月次 EV ---")
            r1["YM"] = r1["Date"].dt.to_period("M")
            r1["e1"] = e1
            for ym, sub in r1.groupby("YM"):
                print(f"    {ym}: n={len(sub):>4}, W平均={sub['e1'].clip(-5,5).mean():+6.3f}%, "
                      f"中央={sub['e1'].median():+6.3f}%, 勝率={(sub['e1']>0).mean()*100:>4.1f}%")


def section_E(df: pd.DataFrame, rows: list):
    """新シグナル探索"""
    print("\n" + "=" * 92)
    print("E: 別のシグナルロジック探索 (T+1 寄付決済)")
    print("=" * 92)

    for period in ["TRAIN", "VAL"]:
        p = slice_period(df, period)
        n_days = p["Date"].nunique()
        print(f"\n[{period}] {n_days}営業日, 全{len(p):,}行")

        signals = {
            "E1: overnight_bias_5 top10% (継続)":
                p["overnight_bias_5"] >= p["overnight_bias_5"].quantile(0.90),
            "E2: hammer (下ヒゲ長 + 引け強)":
                (p["lower_shadow"] >= 2.0) & (p["close_in_range"] >= 0.7)
                & (p["body_pct"].abs() <= 1.5),
            "E3: inside-bar (range<昨日 & 陽線)":
                (p["range_pct"] < p["prev_range"] * 0.6) & (p["body_pct"] > 1.0),
            "E4: 3日連騰+出来高増":
                (p["up_streak"] >= 3) & (p["vol_ratio"] >= 1.5),
            "E5: 52週安値接触":
                p["near_52w_low"],
            "E6: Stage1強+当日急落 (s1≥80 & dayret≤-3)":
                (p["stage1_score"] >= 80) & (p["day_return"] <= -3),
            "E7: 高ボラ+ギャップダウン (atr top10% & gap≤-2)":
                (p["relative_atr"] >= p["relative_atr"].quantile(0.90))
                & (p["gap_today"] <= -2),
            "E8: 引け強(close_in_range top10%) + 出来高2x":
                (p["close_in_range"] >= 0.95) & (p["vol_ratio"] >= 2.0),
            "E9: 下ヒゲハンマー + ギャップダウン":
                (p["lower_shadow"] >= 2.0) & (p["close_in_range"] >= 0.7)
                & (p["gap_today"] <= -2),
            "E10: 反転前兆: 連騰0 & dayret<-3 & 下ヒゲ長":
                (p["up_streak"] == 0) & (p["day_return"] <= -3) & (p["lower_shadow"] >= 1.0),
        }

        print(f"  {'signal':<46} {'n':>7} {'件/日':>6} "
              f"{'W平均':>8} {'中央':>8} {'勝率':>6} {'std':>5}")
        print("  " + "-" * 88)
        for name, mask in signals.items():
            sub = p[mask]
            if len(sub) == 0:
                continue
            e = e1_open(sub)
            st = summ(e)
            per_day = st["n"] / n_days
            print(f"  {name:<46} {st['n']:>7,} {per_day:>6.1f} "
                  f"{st['W平均']:>+7.3f}% {st['中央値']:>+7.3f}% "
                  f"{st['勝率']:>5.1f}% {st['stdev']:>5.1f}")
            rows.append({**st, "rule": name, "period": period, "per_day": round(per_day, 2)})


def section_F(df: pd.DataFrame, rows: list, top_signals: dict):
    """universe filter sweep (上位 E シグナルにも適用)"""
    print("\n" + "=" * 92)
    print("F: ユニバースフィルタ sweep (R1, R2, 上位 E シグナル)")
    print("=" * 92)

    for period in ["TRAIN", "VAL"]:
        p = slice_period(df, period)
        print(f"\n[{period}]")

        base_signals = {
            "R1 (gap≤-2 & MA25≤-5)": (p["gap_today"] <= -2) & (p["ma25_diff_pct"] <= -5),
            "R2 (gap≤-3 & MA25≤-8 & dayret≤-3)":
                (p["gap_today"] <= -3) & (p["ma25_diff_pct"] <= -8) & (p["day_return"] <= -3),
        }
        # 上位 E シグナル (関数引数で渡される)
        for name, mask_fn in top_signals.items():
            base_signals[name] = mask_fn(p)

        liquidity_tiers = [
            ("全件",               lambda d: pd.Series(True, index=d.index)),
            ("売買代金≥3億",         lambda d: d["traded_value"] >= 3e8),
            ("売買代金≥10億",        lambda d: d["traded_value"] >= 10e8),
            ("売買代金≥30億",        lambda d: d["traded_value"] >= 30e8),
            ("売買代金≥100億",       lambda d: d["traded_value"] >= 100e8),
            ("中型(500-3000円)",   lambda d: (d["Close"] >= 500) & (d["Close"] < 3000)),
            ("大型(≥3000円)",      lambda d: d["Close"] >= 3000),
        ]

        for sig_name, sig_mask in base_signals.items():
            print(f"\n  ▶ {sig_name}")
            print(f"    {'universe':<22} {'n':>7} {'W平均':>8} {'中央':>8} "
                  f"{'勝率':>6} {'std':>5}")
            for tier_name, tier_fn in liquidity_tiers:
                sub = p[sig_mask & tier_fn(p)]
                if len(sub) < 30:  # サンプル少なすぎは略
                    continue
                e = e1_open(sub)
                st = summ(e)
                print(f"    {tier_name:<22} {st['n']:>7,} "
                      f"{st['W平均']:>+7.3f}% {st['中央値']:>+7.3f}% "
                      f"{st['勝率']:>5.1f}% {st['stdev']:>5.1f}")
                rows.append({**st, "rule": f"{sig_name} × {tier_name}", "period": period})


def main():
    print("[def_sweep] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid)].copy()

    df = calc_all_signals(all_data)
    df = prep(df)
    df = df[df["next_open"].notna() & df["next_close"].notna()].copy()
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    print(f"[def_sweep] 評価行数: {len(df):,}")

    rows = []
    section_D(df, rows)
    section_E(df, rows)

    # E の TOP3 を F に渡す (VAL EV ベース、件数 200+ のもの)
    e_rows = [r for r in rows if r["rule"].startswith("E") and r.get("period") == "VAL"
              and r.get("n", 0) >= 200]
    e_rows_sorted = sorted(e_rows, key=lambda x: x.get("W平均", -99), reverse=True)
    top3 = e_rows_sorted[:3]
    print(f"\n[def_sweep] F に渡す E の上位 3 シグナル (VAL, n≥200):")
    for r in top3:
        print(f"  {r['rule']}: VAL W平均 {r['W平均']:+.3f}%, n={r['n']}")

    # mask 再構築
    top_masks = {}
    for r in top3:
        name = r["rule"]
        if "E1:" in name:
            top_masks[name] = lambda p: p["overnight_bias_5"] >= p["overnight_bias_5"].quantile(0.90)
        elif "E2:" in name:
            top_masks[name] = lambda p: (p["lower_shadow"] >= 2.0) & (p["close_in_range"] >= 0.7) & (p["body_pct"].abs() <= 1.5)
        elif "E3:" in name:
            top_masks[name] = lambda p: (p["range_pct"] < p["prev_range"] * 0.6) & (p["body_pct"] > 1.0)
        elif "E4:" in name:
            top_masks[name] = lambda p: (p["up_streak"] >= 3) & (p["vol_ratio"] >= 1.5)
        elif "E5:" in name:
            top_masks[name] = lambda p: p["near_52w_low"]
        elif "E6:" in name:
            top_masks[name] = lambda p: (p["stage1_score"] >= 80) & (p["day_return"] <= -3)
        elif "E7:" in name:
            top_masks[name] = lambda p: (p["relative_atr"] >= p["relative_atr"].quantile(0.90)) & (p["gap_today"] <= -2)
        elif "E8:" in name:
            top_masks[name] = lambda p: (p["close_in_range"] >= 0.95) & (p["vol_ratio"] >= 2.0)
        elif "E9:" in name:
            top_masks[name] = lambda p: (p["lower_shadow"] >= 2.0) & (p["close_in_range"] >= 0.7) & (p["gap_today"] <= -2)
        elif "E10:" in name:
            top_masks[name] = lambda p: (p["up_streak"] == 0) & (p["day_return"] <= -3) & (p["lower_shadow"] >= 1.0)

    section_F(df, rows, top_masks)

    pd.DataFrame(rows).to_csv(
        DATA_DIR / "overnight_def_sweep.csv", index=False, encoding="utf-8-sig",
    )
    print(f"\nCSV: {DATA_DIR / 'overnight_def_sweep.csv'}")


if __name__ == "__main__":
    main()

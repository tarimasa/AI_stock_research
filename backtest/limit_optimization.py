#!/usr/bin/env python3
"""
backtest/limit_optimization.py

「指値をどうしたら約定率が上がり、かつ EV を維持できるか」のバックテスト。

検証する指値設計:
  Group 1: 純 ATR×k (k 倍率スイープ)
    k = 0.5, 0.75, 1.0, 1.25, 1.5 (現状), 1.75, 2.0
    指値 = Close - k × ATR14

  Group 2: 深さクリップ (ATR×1.5 だが最大深さを %で上限)
    cap = -3%, -4%, -5%, -6%, -7%, -8%, -10%
    指値 = max(Close - 1.5×ATR, Close × (1 + cap/100))

  Group 3: vol-adaptive (高ボラ銘柄ほど k を下げる)
    relative_ATR が q90 以上 → k=0.75
    relative_ATR が q75 以上 → k=1.0
    それ以外 → k=1.5
    (高ボラ銘柄での約定率向上を狙う)

  Group 4: 固定 %
    -2%, -3%, -4%, -5% (現行比較用 baseline)

評価:
  - 約定率 (fill rate)
  - 約定時 EV (TP+7.5/SL-5/3日保有)
  - 日次 PnL (vol_ratio top-2 選別 + 100万円資金)
  - 単利累積 PnL (TRAIN/VAL)
  - 最大DD

LLM 絞り込みは S-A4 (vol_ratio top-2) で固定。
これは improve案 A 適用後の想定運用に相当。
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
N_POS = 2  # LLM が 2 件選ぶ想定


def prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Code", "Date"]).reset_index(drop=True)
    g = df.groupby("Code", group_keys=False)
    df["vol_ma20"] = g["Volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["vol_ratio"] = df["Volume"] / df["vol_ma20"].replace(0, np.nan)
    df["prev_close"] = g["Close"].shift(1)
    df["gap_today"] = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - g["Close"].shift(1)).abs(),
        (df["Low"]  - g["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = g["Date"].transform(lambda x: tr.loc[x.index].rolling(14, min_periods=7).mean())
    df["relative_atr"] = df["atr14"] / df["Close"] * 100
    return df


def slice_period(df, period):
    if period == "TRAIN":
        return df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    return df[(df["Date"] >= pd.Timestamp(VAL_START)) & (df["Date"] <= pd.Timestamp(VAL_END))]


def split_filter(df):
    df = df.copy()
    g = df.groupby("Code", group_keys=False)
    df["_overnight"] = (g["Open"].shift(-1) - df["Close"]) / df["Close"] * 100
    df = df[(df["gap_today"].abs() <= 15) & (df["_overnight"].abs() <= 20)]
    return df.drop(columns=["_overnight"])


def evaluate_limit_method(
    df: pd.DataFrame, limit_col: str, period: str, n_total_days: int,
    selection_col: str = "vol_ratio",
    selection_ascending: bool = False,
) -> dict:
    """
    指定の limit_col を「指値」として使い、fill判定 + TP/SL/3日保有 で EV 計算。
    その後 vol_ratio top-2 で LLM 選別シミュレーションを行う。

    Returns: 戦略の各種統計
    """
    d = df.copy()
    # max_gap_pct ≤ 1.0
    d["next_gap_pct"] = (d["next_open"] - d["Close"]) / d["Close"] * 100
    d = d[d["next_gap_pct"] <= 1.0]

    # fill 判定
    fill_at_open  = d["next_open"] <= d[limit_col]
    fill_intraday = (~fill_at_open) & (d["next_low"] <= d[limit_col])
    d["entry_price"] = np.where(
        fill_at_open, d["next_open"],
        np.where(fill_intraday, d[limit_col], np.nan),
    )

    # 全候補に対する fill 率 (LLM 絞り込み前)
    n_total_signals = len(d)
    n_fills_total = d["entry_price"].notna().sum()
    fill_rate_total = n_fills_total / max(1, n_total_signals) * 100

    # TP+7.5/SL-5/3日保有
    d_filled = d[d["entry_price"].notna()].copy()
    if len(d_filled) > 0:
        ret, _ = simulate_holding(
            d_filled, "entry_price",
            sl_pct=-5.0, tp_pct=7.5, days=3, cost_pct=COST,
        )
        d_filled["ret"] = ret
        # 約定時 EV (全約定の平均)
        ev_per_fill = float(d_filled["ret"].mean())
    else:
        ev_per_fill = 0.0
        d_filled["ret"] = pd.Series(dtype=float)

    # LLM 絞り込み: 各日に selection_col で top-N_POS を推奨
    # 推奨銘柄のうち約定したものだけを採用
    d = d.merge(d_filled[["Code", "Date", "ret"]], on=["Code", "Date"], how="left")
    ranked = d.groupby("Date")[selection_col].rank(ascending=selection_ascending, method="first")
    recs = d[ranked <= N_POS].copy()

    n_recs = len(recs)
    n_filled_in_recs = recs["ret"].notna().sum()
    fill_rate_recs = n_filled_in_recs / max(1, n_recs) * 100

    # 日次 PnL: 推奨 N 件中 m 件約定 → 各 (1/N)×capital で投入
    # 約定したものの ret を集計、未約定は 0
    recs["ret_filled"] = recs["ret"].fillna(0.0)
    # 日次 = 推奨銘柄の ret の平均 (1/N × N = 1.0 weight, 約定しない分は 0)
    daily_pnl = recs.groupby("Date")["ret"].apply(
        lambda x: x.fillna(0).sum() / N_POS
    )

    # 全営業日に揃える: 推奨ゼロの日 (= stage1 候補なしの日) も 0 として
    all_dates = df["Date"].unique()
    daily_pnl = daily_pnl.reindex(all_dates, fill_value=0.0)
    daily_pnl = daily_pnl.sort_index()

    n_active = (daily_pnl != 0).sum()
    activity_rate = n_active / n_total_days * 100

    cum_simple = float(daily_pnl.sum())
    cum_compound = float(((1 + daily_pnl/100).prod() - 1) * 100)
    eq = (1 + daily_pnl/100).cumprod()
    dd = float((eq / eq.cummax() - 1).min() * 100)
    winrate_active = float((daily_pnl[daily_pnl != 0] > 0).mean() * 100) if n_active > 0 else 0

    return {
        "period": period,
        "推奨総数": n_recs,
        "約定総数": int(n_filled_in_recs),
        "推奨内約定率": round(fill_rate_recs, 1),
        "全体約定率": round(fill_rate_total, 1),
        "約定時EV": round(ev_per_fill, 3),
        "活動日": int(n_active),
        "全営業日": n_total_days,
        "活動率": round(activity_rate, 1),
        "日次勝率": round(winrate_active, 1),
        "単利累積": round(cum_simple, 1),
        "複利累積": round(cum_compound, 1),
        "最大DD": round(dd, 1),
        "PnL円": round(cum_simple / 100 * CAPITAL, 0),
    }


def main():
    print("[limit_opt] データ準備中...")
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
    df = df[df["stage1_score"] >= 60].copy()
    print(f"[limit_opt] stage1>=60 クリーン後: {len(df):,}")

    # ── 各指値方式の limit_col を作る ─────────────────────────────────
    limit_configs = []

    # Group 1: ATR×k
    for k in [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]:
        col = f"L_atr{k}"
        df[col] = tick_round_series(df["Close"] - k * df["atr14"])
        limit_configs.append((f"G1: ATR×{k:.2f}", col))

    # Group 2: ATR×1.5 + 上限クリップ
    for cap in [3, 4, 5, 6, 7, 8, 10]:
        col = f"L_cap{cap}"
        # max(close - 1.5*atr, close * (1 - cap/100))
        # 注: max は「より浅い」値 (higher price) を選ぶ
        deep = df["Close"] - 1.5 * df["atr14"]
        clip = df["Close"] * (1 - cap / 100)
        df[col] = tick_round_series(np.maximum(deep, clip))
        limit_configs.append((f"G2: ATR×1.5 cap-{cap}%", col))

    # Group 3: vol-adaptive
    train_atr_q75 = slice_period(df, "TRAIN")["relative_atr"].quantile(0.75)
    train_atr_q90 = slice_period(df, "TRAIN")["relative_atr"].quantile(0.90)
    print(f"[limit_opt] TRAIN ATR q75 = {train_atr_q75:.2f}%, q90 = {train_atr_q90:.2f}%")
    def _vol_adaptive_k(r_atr):
        return np.where(r_atr >= train_atr_q90, 0.75,
                np.where(r_atr >= train_atr_q75, 1.0, 1.5))
    df["L_volad"] = tick_round_series(
        df["Close"] - _vol_adaptive_k(df["relative_atr"]) * df["atr14"]
    )
    limit_configs.append(("G3: vol-adaptive k=0.75/1.0/1.5", "L_volad"))

    # Group 4: 固定 %
    for pct in [2, 3, 4, 5]:
        col = f"L_pct{pct}"
        df[col] = tick_round_series(df["Close"] * (1 - pct / 100))
        limit_configs.append((f"G4: 固定 -{pct}%", col))

    # ── 各期間で全 limit 方式を評価 ──────────────────────────────────
    rows = []
    for period in ["TRAIN", "VAL"]:
        per = slice_period(df, period)
        n_days = per["Date"].nunique()
        print(f"\n{'='*112}")
        print(f"【{period}】 stage1>=60: {len(per):,}件 / {n_days}営業日 / "
              f"LLM 絞り込み: vol_ratio top-{N_POS} (= 改善案A 適用後)")
        print(f"{'='*112}")
        print(f"  {'指値方式':<32} {'推奨':>5} {'約定':>5} {'fill%':>6} "
              f"{'EV/件':>7} {'活動率':>6} {'日次勝率':>7} {'単利%':>7} "
              f"{'複利%':>9} {'DD%':>7} {'PnL円':>11}")
        print("  " + "-" * 109)
        for label, col in limit_configs:
            st = evaluate_limit_method(per, col, period, n_days)
            st["label"] = label
            rows.append(st)
            print(f"  {label:<32} {st['推奨総数']:>5,} {st['約定総数']:>5,} "
                  f"{st['推奨内約定率']:>5.1f}% "
                  f"{st['約定時EV']:>+6.3f}% "
                  f"{st['活動率']:>5.1f}% "
                  f"{st['日次勝率']:>6.1f}% "
                  f"{st['単利累積']:>+6.1f}% "
                  f"{st['複利累積']:>+8.1f}% "
                  f"{st['最大DD']:>+6.1f}% "
                  f"¥{st['PnL円']:>+11,.0f}")

    # ── 両期間で robust な「最良 5」を表示 ──────────────────────────
    print(f"\n{'='*112}")
    print("【両期間で安定的に上位の指値方式 (単利累積で TRAIN/VAL 平均、Top 5)】")
    print(f"{'='*112}")
    df_rows = pd.DataFrame(rows)
    train_rows = df_rows[df_rows["period"] == "TRAIN"].set_index("label")
    val_rows   = df_rows[df_rows["period"] == "VAL"].set_index("label")
    common = train_rows.index.intersection(val_rows.index)
    avg_cum = pd.DataFrame({
        "TRAIN_単利": train_rows.loc[common, "単利累積"],
        "VAL_単利":   val_rows.loc[common, "単利累積"],
        "TRAIN_fill%": train_rows.loc[common, "推奨内約定率"],
        "VAL_fill%":   val_rows.loc[common, "推奨内約定率"],
        "TRAIN_活動率": train_rows.loc[common, "活動率"],
        "VAL_活動率":   val_rows.loc[common, "活動率"],
    })
    avg_cum["平均単利"] = (avg_cum["TRAIN_単利"] + avg_cum["VAL_単利"]) / 2
    avg_cum = avg_cum.sort_values("平均単利", ascending=False)
    print(avg_cum.round(1).to_string())

    df_rows.to_csv(DATA_DIR / "limit_optimization.csv", index=False, encoding="utf-8-sig")
    print(f"\nCSV: {DATA_DIR / 'limit_optimization.csv'}")


if __name__ == "__main__":
    main()

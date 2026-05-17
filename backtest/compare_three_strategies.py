#!/usr/bin/env python3
"""
backtest/compare_three_strategies.py

3 つの方向で limit_pct 戦略を比較する:

  ① エクイティカーブ比較
     -0.7% (現行) / -3.5% / -4.0% で 1 シグナル等資金配分の累積リターン・最大DD・Sharpe を計算

  ② シグナル強度層別の最適 limit_pct
     stage1_score を 4 段階にバケット化、各バケットで TRAIN 最適 limit_pct を探索、
     VAL に一発適用。「強シグナルは浅い指値で十分か?」を検証

  ③ ATR 連動の動的指値
     N 日 ATR × k を当日終値から引いた値を指値とする。
     k を TRAIN で最適化、VAL で OOS 評価。「ボラに応じた指値深さ」が固定 % より勝てるか

【共通の方針】
  - データロード/シグナル計算は 1 回だけ実行 (高速化)
  - 全分析で TRAIN 期間で意思決定、VAL は一発評価 (look-ahead 禁止)
  - TP/SL/holding は固定 (+7.5/-5/3日) — 多重最適化による過学習を回避

【使い方】
  python3 backtest/compare_three_strategies.py
  python3 backtest/compare_three_strategies.py --skip-equity   # ②③のみ

【出力】
  backtest/data/compare_*.csv  -- 各セクションの結果
  コンソール: 統合レポート
"""

import argparse
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


# ── 共通: 指値→約定→保有でリターン列を返す ──────────────────────────────

def compute_returns_for_limit(
    df: pd.DataFrame, limit_price_col: str, args
) -> pd.Series:
    """
    与えた指値価格列で約定判定し simulate_holding を回してリターンを返す。
    df は事前に add_next_bars / add_future_bars 済みを想定。
    """
    next_open = df["next_open"]
    next_low  = df["next_low"]
    limit_p   = df[limit_price_col]

    fill_at_open  = next_open <= limit_p
    fill_intraday = (~fill_at_open) & (next_low <= limit_p)
    invalid = next_open.isna() | limit_p.isna()

    entry = pd.Series(np.nan, index=df.index, dtype="float64")
    entry[fill_at_open & ~invalid]  = next_open[fill_at_open & ~invalid]
    entry[fill_intraday & ~invalid] = limit_p[fill_intraday & ~invalid]
    df = df.copy()
    df["_entry"] = entry

    ret, _ = simulate_holding(
        df, "_entry", sl_pct=args.sl_pct, tp_pct=args.tp_pct,
        days=args.holding_days, cost_pct=args.cost_pct,
    )
    return ret


def add_limit_price_pct(df: pd.DataFrame, limit_pct: float, out_col: str) -> pd.DataFrame:
    df[out_col] = tick_round_series(df["Close"] * (1.0 + limit_pct / 100.0))
    return df


# ── ATR 計算 ──────────────────────────────────────────────────────────────

def add_atr(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """
    ATR(n) を Close と同じスケール (円) で付与。
    TR = max(High-Low, |High-prev_close|, |Low-prev_close|)
    """
    df = df.sort_values(["Code", "Date"]).copy()
    g = df.groupby("Code", group_keys=False)
    prev_close = g["Close"].shift(1)
    tr = pd.concat([
        (df["High"] - df["Low"]).abs(),
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.groupby(df["Code"]).transform(
        lambda x: x.rolling(n, min_periods=n).mean()
    )
    df["atr14_pct"] = df["atr14"] / df["Close"] * 100  # 終値に対する % 表示用
    return df


# ── 簡易エクイティカーブ ──────────────────────────────────────────────────

def equity_metrics(df_signals: pd.DataFrame, ret_col: str) -> dict:
    """
    シグナル発生日 × 各日均等配分 の単純エクイティカーブを構築。
      ・各日に参戦したトレードの平均リターンをその日のリターンとする
      ・参戦 0 件の日はリターン 0%
      ・初期 100 → 累積 (1 + r/100) の連乗
    """
    work = df_signals[["Date", ret_col]].copy()
    work["Date"] = pd.to_datetime(work["Date"])
    # 参戦日のみ抽出 (NaN は不参戦扱い)
    taken = work.dropna(subset=[ret_col])
    if taken.empty:
        return {"n_trades": 0, "final_pct": 0.0, "max_dd_pct": 0.0,
                "sharpe": 0.0, "n_active_days": 0,
                "best_day_pct": 0.0, "worst_day_pct": 0.0,
                "equity_series": None}

    daily_mean = taken.groupby(taken["Date"].dt.normalize())[ret_col].mean()
    all_dates = pd.date_range(daily_mean.index.min(), daily_mean.index.max(), freq="B")
    daily_ret = daily_mean.reindex(all_dates).fillna(0.0)

    equity = (1.0 + daily_ret / 100.0).cumprod() * 100.0
    peak = equity.cummax()
    dd = (equity / peak - 1.0) * 100.0
    mu = daily_ret.mean()
    sd = daily_ret.std(ddof=0)
    sharpe = mu / sd * np.sqrt(252) if sd > 0 else 0.0

    return {
        "n_trades":      len(taken),
        "n_active_days": int((daily_ret != 0).sum()),
        "final_pct":     round(float(equity.iloc[-1] - 100.0), 2),
        "max_dd_pct":    round(float(dd.min()), 2),
        "sharpe":        round(float(sharpe), 3),
        "best_day_pct":  round(float(daily_ret.max()), 3),
        "worst_day_pct": round(float(daily_ret.min()), 3),
        "equity_series": equity,
    }


# ── ① エクイティカーブ比較 ──────────────────────────────────────────────

def section1_equity(df_all: pd.DataFrame, args) -> None:
    print("\n" + "=" * 78)
    print(" ① エクイティカーブ比較 (1日1シグナル等資金配分の簡易モデル)")
    print("=" * 78)

    candidates = [-0.7, -3.5, -4.0]
    rows = []
    for limit_pct in candidates:
        col = f"_limit_{limit_pct}"
        df = add_limit_price_pct(df_all.copy(), limit_pct, col)
        df["_ret"] = compute_returns_for_limit(df, col, args)
        for period_name, start, end in [
            ("TRAIN", TRAIN_START, TRAIN_END),
            ("VAL",   VAL_START,   VAL_END),
        ]:
            mask = ((df["Date"] >= pd.Timestamp(start)) &
                    (df["Date"] <= pd.Timestamp(end)))
            sub = df[mask]
            metrics = equity_metrics(sub, "_ret")
            metrics["limit_pct"] = limit_pct
            metrics["period"]    = period_name
            metrics["n_signals"] = int(mask.sum())
            rows.append(metrics)

    # コンソール表示
    print(f"\n  {'limit_pct':>10} {'period':>6} {'参戦数':>8} {'最終%':>8} "
          f"{'最大DD%':>9} {'Sharpe':>7} {'最良日%':>9} {'最悪日%':>9}")
    print(f"  {'-'*10} {'-'*6} {'-'*8} {'-'*8} {'-'*9} {'-'*7} {'-'*9} {'-'*9}")
    for r in rows:
        print(f"  {r['limit_pct']:>+9.2f}% {r['period']:>6} "
              f"{r['n_trades']:>8,} "
              f"{r['final_pct']:>+7.2f}% "
              f"{r['max_dd_pct']:>+8.2f}% "
              f"{r['sharpe']:>+6.3f} "
              f"{r['best_day_pct']:>+8.3f}% "
              f"{r['worst_day_pct']:>+8.3f}%")

    # CSV (equity_series は別保存)
    out = pd.DataFrame([{k: v for k, v in r.items() if k != "equity_series"}
                         for r in rows])
    out.to_csv(DATA_DIR / "compare_equity_metrics.csv",
               index=False, encoding="utf-8-sig")

    # equity series を縦並びで保存
    series_rows = []
    for r in rows:
        if r["equity_series"] is None:
            continue
        for d, v in r["equity_series"].items():
            series_rows.append({"limit_pct": r["limit_pct"],
                                 "period": r["period"],
                                 "Date": d, "equity": v})
    if series_rows:
        pd.DataFrame(series_rows).to_csv(
            DATA_DIR / "compare_equity_curves.csv",
            index=False, encoding="utf-8-sig",
        )


# ── ② シグナル強度層別の最適 limit_pct ───────────────────────────────

def section2_by_strength(df_all: pd.DataFrame, args) -> None:
    print("\n" + "=" * 78)
    print(" ② シグナル強度層別の最適 limit_pct")
    print("=" * 78)
    print("  各 Stage1 スコア帯について TRAIN で最適 limit_pct を探索、VAL で OOS 評価")

    buckets = [(60, 80), (80, 100), (100, 150), (150, 9999)]
    bucket_labels = ["60-79", "80-99", "100-149", "150+"]
    grid = [round(x * 0.25, 2) for x in range(-20, 1)]  # -5.0 〜 0.0 step 0.25

    df_train_all = df_all[(df_all["Date"] >= pd.Timestamp(TRAIN_START)) &
                            (df_all["Date"] <= pd.Timestamp(TRAIN_END))]
    df_val_all   = df_all[(df_all["Date"] >= pd.Timestamp(VAL_START)) &
                            (df_all["Date"] <= pd.Timestamp(VAL_END))]

    print(f"\n  {'スコア帯':>9} {'TRAIN n':>9} {'最適 limit':>11} "
          f"{'TRAIN純EV':>10} {'VAL n':>8} {'VAL@採用':>10} {'VAL@-0.7%':>11}")
    print(f"  {'-'*9} {'-'*9} {'-'*11} {'-'*10} {'-'*8} {'-'*10} {'-'*11}")

    rows = []
    for (lo, hi), label in zip(buckets, bucket_labels):
        df_t = df_train_all[(df_train_all["stage1_score"] >= lo) &
                              (df_train_all["stage1_score"] < hi)].copy()
        df_v = df_val_all[(df_val_all["stage1_score"] >= lo) &
                            (df_val_all["stage1_score"] < hi)].copy()
        if df_t.empty:
            print(f"  {label:>9} {'-':>9} {'-':>11} {'-':>10} "
                  f"{'-':>8} {'-':>10} {'-':>11}")
            continue

        # TRAIN sweep
        train_results = []
        for limit_pct in grid:
            col = "_lp"
            df_t = add_limit_price_pct(df_t, limit_pct, col)
            ret = compute_returns_for_limit(df_t, col, args)
            taken = ret.dropna()
            if len(taken) < 500:  # 層別なのでサンプル下限を緩和
                continue
            train_results.append({
                "limit_pct": limit_pct,
                "n_taken": len(taken),
                "ev_per_signal": float(taken.sum() / len(df_t)),
            })
        if not train_results:
            print(f"  {label:>9} {len(df_t):>9,} {'-':>11} {'-':>10} "
                  f"{len(df_v):>8,} {'-':>10} {'-':>11}")
            continue
        tdf = pd.DataFrame(train_results)
        best_row = tdf.loc[tdf["ev_per_signal"].idxmax()]
        best_lp = float(best_row["limit_pct"])
        best_ev_train = float(best_row["ev_per_signal"])

        # VAL eval at best
        col = "_lp"
        df_v = add_limit_price_pct(df_v, best_lp, col)
        ret_v = compute_returns_for_limit(df_v, col, args)
        n_val_taken = int(ret_v.notna().sum())
        val_ev_best = float(ret_v.dropna().sum() / len(df_v)) if len(df_v) else 0.0

        # VAL eval at -0.7% (current baseline)
        df_v = add_limit_price_pct(df_v, -0.7, col)
        ret_v_curr = compute_returns_for_limit(df_v, col, args)
        val_ev_curr = float(ret_v_curr.dropna().sum() / len(df_v)) if len(df_v) else 0.0

        print(f"  {label:>9} {len(df_t):>9,} {best_lp:>+10.2f}% "
              f"{best_ev_train:>+9.4f}% {len(df_v):>8,} "
              f"{val_ev_best:>+9.4f}% {val_ev_curr:>+10.4f}%")
        rows.append({
            "bucket": label, "n_train": len(df_t), "best_limit_pct": best_lp,
            "train_ev_best": round(best_ev_train, 4),
            "n_val": len(df_v),
            "val_ev_at_best": round(val_ev_best, 4),
            "val_ev_at_current": round(val_ev_curr, 4),
        })

    pd.DataFrame(rows).to_csv(DATA_DIR / "compare_by_signal_strength.csv",
                                index=False, encoding="utf-8-sig")


# ── ③ ATR 連動の動的指値 ──────────────────────────────────────────────

def section3_atr(df_all: pd.DataFrame, args) -> None:
    print("\n" + "=" * 78)
    print(" ③ ATR 連動の動的指値")
    print("=" * 78)
    print("  指値 = Close - k × ATR14  を呼値丸め。k を TRAIN で最適化、VAL で OOS 評価")

    # ATR ベース指値: k ∈ {0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5}
    ks = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]

    # ATR は事前に df_all に付与済みの想定 (main で行う)
    df_train = df_all[(df_all["Date"] >= pd.Timestamp(TRAIN_START)) &
                       (df_all["Date"] <= pd.Timestamp(TRAIN_END))].copy()
    df_val   = df_all[(df_all["Date"] >= pd.Timestamp(VAL_START)) &
                       (df_all["Date"] <= pd.Timestamp(VAL_END))].copy()

    # ATR が NaN の行は除外 (期初の warmup 期間)
    df_train = df_train[df_train["atr14"].notna()].copy()
    df_val   = df_val[df_val["atr14"].notna()].copy()

    print(f"\n  TRAIN: ATR14 平均 = {df_train['atr14_pct'].mean():.2f}% / "
          f"中央値 {df_train['atr14_pct'].median():.2f}%")
    print(f"  VAL:   ATR14 平均 = {df_val['atr14_pct'].mean():.2f}% / "
          f"中央値 {df_val['atr14_pct'].median():.2f}%")

    print(f"\n  TRAIN: k スイープ")
    print(f"  {'k':>6} {'参戦数':>8} {'参戦率':>7} "
          f"{'平均指値%':>10} {'約定時EV':>9} {'純EV':>9}")
    print(f"  {'-'*6} {'-'*8} {'-'*7} {'-'*10} {'-'*9} {'-'*9}")

    train_rows = []
    for k in ks:
        df_t = df_train.copy()
        # 指値 = Close - k × ATR
        df_t["_lp"] = tick_round_series(df_t["Close"] - k * df_t["atr14"])
        ret = compute_returns_for_limit(df_t, "_lp", args)
        n_total = len(df_t)
        taken = ret.dropna()
        n_taken = len(taken)
        # 指値が終値から何%下か (平均)
        offset_pct = ((df_t["_lp"] / df_t["Close"] - 1) * 100).mean()
        ev_when_taken = float(taken.mean()) if n_taken else 0.0
        ev_per_signal = float(taken.sum() / n_total) if n_total else 0.0
        train_rows.append({
            "k": k, "n_taken": n_taken,
            "participate_pct": n_taken / n_total * 100 if n_total else 0,
            "avg_offset_pct": offset_pct,
            "ev_when_taken": ev_when_taken,
            "ev_per_signal": ev_per_signal,
        })
        mark = " " if n_taken >= 5000 else "✗"
        print(f"  {k:>6.2f} {n_taken:>8,} "
              f"{n_taken/n_total*100:>6.2f}% "
              f"{offset_pct:>+9.3f}% "
              f"{ev_when_taken:>+8.4f}% "
              f"{ev_per_signal:>+8.4f}%{mark}")

    eligible = [r for r in train_rows if r["n_taken"] >= 5000]
    if not eligible:
        print("  → サンプル数を満たす k がありません")
        return
    best = max(eligible, key=lambda r: r["ev_per_signal"])
    best_k = best["k"]
    print(f"\n  → TRAIN 採用: k = {best_k}")

    print(f"\n  VAL: 採用 k={best_k} と現行 -0.7% 固定指値の比較")
    print(f"  {'戦略':<22} {'参戦数':>8} {'参戦率':>7} "
          f"{'約定時EV':>9} {'純EV':>9}")
    print(f"  {'-'*22} {'-'*8} {'-'*7} {'-'*9} {'-'*9}")

    val_rows = []
    # ATR動的(採用k)
    df_v = df_val.copy()
    df_v["_lp"] = tick_round_series(df_v["Close"] - best_k * df_v["atr14"])
    ret_atr = compute_returns_for_limit(df_v, "_lp", args)
    n_total_v = len(df_v)
    taken = ret_atr.dropna()
    print(f"  {'A. ATR動的 (k=' + str(best_k) + ')':<22} {len(taken):>8,} "
          f"{len(taken)/n_total_v*100:>6.2f}% "
          f"{float(taken.mean()):>+8.4f}% "
          f"{float(taken.sum()/n_total_v):>+8.4f}%")
    val_rows.append({"strategy": f"ATR動的 k={best_k}",
                       "n_taken": len(taken),
                       "ev_per_signal": float(taken.sum()/n_total_v)})

    # 現行 -0.7%
    df_v["_lp"] = tick_round_series(df_v["Close"] * 0.993)
    ret_curr = compute_returns_for_limit(df_v, "_lp", args)
    taken = ret_curr.dropna()
    print(f"  {'B. 固定 -0.7% (現行)':<22} {len(taken):>8,} "
          f"{len(taken)/n_total_v*100:>6.2f}% "
          f"{float(taken.mean()):>+8.4f}% "
          f"{float(taken.sum()/n_total_v):>+8.4f}%")
    val_rows.append({"strategy": "固定 -0.7%",
                       "n_taken": len(taken),
                       "ev_per_signal": float(taken.sum()/n_total_v)})

    # 固定 -3.5%
    df_v["_lp"] = tick_round_series(df_v["Close"] * (1 - 3.5/100))
    ret_35 = compute_returns_for_limit(df_v, "_lp", args)
    taken = ret_35.dropna()
    print(f"  {'C. 固定 -3.5%':<22} {len(taken):>8,} "
          f"{len(taken)/n_total_v*100:>6.2f}% "
          f"{float(taken.mean()):>+8.4f}% "
          f"{float(taken.sum()/n_total_v):>+8.4f}%")
    val_rows.append({"strategy": "固定 -3.5%",
                       "n_taken": len(taken),
                       "ev_per_signal": float(taken.sum()/n_total_v)})

    pd.DataFrame(train_rows).to_csv(
        DATA_DIR / "compare_atr_train_sweep.csv",
        index=False, encoding="utf-8-sig",
    )
    pd.DataFrame(val_rows).to_csv(
        DATA_DIR / "compare_atr_val.csv",
        index=False, encoding="utf-8-sig",
    )


# ── メイン ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="3軸の戦略比較")
    p.add_argument("--sl-pct",       type=float, default=-5.0)
    p.add_argument("--tp-pct",       type=float, default=7.5)
    p.add_argument("--holding-days", type=int,   default=3)
    p.add_argument("--cost-pct",     type=float, default=0.20)
    p.add_argument("--min-score",    type=float, default=60.0)
    p.add_argument("--no-filter",    action="store_true")
    p.add_argument("--skip-equity",  action="store_true")
    p.add_argument("--skip-strength",action="store_true")
    p.add_argument("--skip-atr",     action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    print(f"[compare] params: TP/SL=+{args.tp_pct}/{args.sl_pct}%, "
          f"holding={args.holding_days}d, min_score={args.min_score}")

    all_data = load_all_data()
    if all_data.empty:
        sys.exit("[compare] データなし")

    if not args.no_filter:
        latest = all_data["Date"].max()
        valid_codes = set(
            apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique()
        )
        all_data = all_data[all_data["Code"].isin(valid_codes)].copy()

    df = calc_all_signals(all_data)
    df = add_atr(df, n=14)
    df = add_next_bars(df)
    df = add_future_bars(df, days=args.holding_days)
    df = df[df["stage1_score"] >= args.min_score].copy()
    df = df[df["next_open"].notna()].copy()
    print(f"[compare] 対象シグナル数: {len(df):,}")

    if not args.skip_equity:
        section1_equity(df, args)
    if not args.skip_strength:
        section2_by_strength(df, args)
    if not args.skip_atr:
        section3_atr(df, args)

    print(f"\n出力CSV: {DATA_DIR}/compare_*.csv")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
backtest/walk_forward_validation.py

ウォークフォワード検証: 戦略パラメータが「将来も通用するか」を out-of-sample で検証。

【目的】
  multi_day_backtest.py のグリッドサーチは全期間で in-sample なので、
  カーブフィッティングの可能性を排除できない。
  ウォークフォワードでは:
    1. train_months ヶ月で最適戦略を探索
    2. 直後の test_months ヶ月にその戦略を適用（未来データ）
    3. ウィンドウを 1 ヶ月スライドして繰り返す
  これにより毎月の out-of-sample EV が得られ、戦略の安定性を評価できる。

【使用方法】
  cd /home/user/AI_stock_research
  python backtest/walk_forward_validation.py [オプション]

  オプション:
    --train-months N  トレーニング期間（既定 6 ヶ月）
    --test-months N   テスト期間（既定 1 ヶ月）
    --min-n N         探索時のシグナル件数下限（既定 30）
    --commission PCT  往復取引コスト%（既定 0.20%）
    --holding-mode    overnight or half_day（既定 overnight）

【出力】
  data/backtest/walk_forward_results.csv
  コンソール: 月別 OOS EV、平均、標準偏差、勝率
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

from run_backtest import (
    BACKTEST_START,
    BACKTEST_END,
    WARMUP_CALENDAR_DAYS,
    ROUND_TRIP_COST_PCT,
    load_all_data,
    calc_all_signals,
    apply_basic_filter,
)
from multi_day_backtest import (
    SIGNAL_FILTERS,
    TP_LIST,
    SL_LIST,
    DAYS_LIST,
    prepare_future_prices,
    compute_outcomes,
)

DATA_DIR = PROJECT_ROOT / "data" / "backtest"


def find_best_strategy(
    train_df: pd.DataFrame,
    holding_mode: str,
    cost_pct: float,
    min_n: int,
) -> dict | None:
    """
    train 期間でベスト戦略（filter, tp, sl, max_days）を探す。
    EV 最大化を基準にする。
    """
    base = train_df[(train_df["stage1_score"] > 0) & train_df["n1_open"].notna()].copy()
    if base.empty:
        return None

    best = None
    for max_days in DAYS_LIST:
        valid_col = f"n{max_days}_close"
        df_v = base[base[valid_col].notna()]
        if df_v.empty:
            continue

        # 各 (tp, sl) で全フィルタを評価
        for tp in TP_LIST:
            for sl in SL_LIST:
                outcomes = compute_outcomes(
                    df_v, tp, sl, max_days,
                    holding_mode=holding_mode, cost_pct=cost_pct,
                )
                for fname, ffunc in SIGNAL_FILTERS.items():
                    try:
                        mask = ffunc(df_v)
                    except Exception:
                        continue
                    sub = outcomes[mask]
                    if len(sub) < min_n:
                        continue
                    ev = float(sub.mean())
                    if best is None or ev > best["ev"]:
                        best = {
                            "filter": fname,
                            "tp_pct": tp,
                            "sl_pct": sl,
                            "max_days": max_days,
                            "ev": ev,
                            "n": int(len(sub)),
                        }
    return best


def evaluate_on_test(
    test_df: pd.DataFrame,
    strategy: dict,
    holding_mode: str,
    cost_pct: float,
) -> dict | None:
    """test 期間に固定戦略を適用して EV/勝率を返す。"""
    base = test_df[(test_df["stage1_score"] > 0) & test_df["n1_open"].notna()].copy()
    if base.empty:
        return None

    max_days = strategy["max_days"]
    valid_col = f"n{max_days}_close"
    df_v = base[base[valid_col].notna()]
    if df_v.empty:
        return None

    outcomes = compute_outcomes(
        df_v, strategy["tp_pct"], strategy["sl_pct"], max_days,
        holding_mode=holding_mode, cost_pct=cost_pct,
    )
    ffunc = SIGNAL_FILTERS.get(strategy["filter"])
    if ffunc is None:
        return None
    try:
        mask = ffunc(df_v)
    except Exception:
        return None
    sub = outcomes[mask]
    if len(sub) == 0:
        return {"n": 0, "ev": 0.0, "win_rate": 0.0}

    return {
        "n": int(len(sub)),
        "ev": round(float(sub.mean()), 4),
        "win_rate": round(float((sub > 0).mean() * 100), 2),
        "tp_rate": round(float((sub == strategy["tp_pct"]).mean() * 100), 2),
        "sl_rate": round(float((sub == strategy["sl_pct"]).mean() * 100), 2),
    }


def run_walk_forward(
    df_full: pd.DataFrame,
    train_months: int,
    test_months: int,
    min_n: int,
    holding_mode: str,
    cost_pct: float,
) -> pd.DataFrame:
    """全期間でスライドしながら out-of-sample 評価を行う。"""
    if df_full.empty:
        return pd.DataFrame()

    start = pd.Timestamp(df_full["Date"].min()).normalize()
    end = pd.Timestamp(df_full["Date"].max()).normalize()
    print(f"[walk_forward] データ期間: {start.date()} 〜 {end.date()}")

    # 月初リスト
    months = pd.date_range(start.replace(day=1), end, freq="MS")
    if len(months) < train_months + test_months:
        print(f"[walk_forward] 期間が短すぎます。最低 {train_months + test_months}ヶ月必要 "
              f"(現在 {len(months)}ヶ月)")
        return pd.DataFrame()

    results = []
    n_windows = len(months) - train_months - test_months + 1
    print(f"[walk_forward] ウィンドウ数: {n_windows}")

    for i in range(n_windows):
        train_start = months[i]
        train_end = months[i + train_months] - pd.Timedelta(days=1)
        test_start = months[i + train_months]
        if i + train_months + test_months >= len(months):
            test_end = end
        else:
            test_end = months[i + train_months + test_months] - pd.Timedelta(days=1)

        train_df = df_full[
            (df_full["Date"] >= train_start) &
            (df_full["Date"] <= train_end)
        ].copy()
        test_df = df_full[
            (df_full["Date"] >= test_start) &
            (df_full["Date"] <= test_end)
        ].copy()

        if train_df.empty or test_df.empty:
            continue

        best = find_best_strategy(train_df, holding_mode, cost_pct, min_n)
        if best is None:
            print(f"  {test_start.date()}: train で最適戦略見つからず → skip")
            continue

        oos = evaluate_on_test(test_df, best, holding_mode, cost_pct)
        if oos is None:
            continue

        record = {
            "test_month": test_start.strftime("%Y-%m"),
            "train_period": f"{train_start.date()} 〜 {train_end.date()}",
            "best_filter": best["filter"],
            "best_tp": best["tp_pct"],
            "best_sl": best["sl_pct"],
            "best_days": best["max_days"],
            "train_ev": round(best["ev"], 4),
            "train_n": best["n"],
            "oos_ev": oos["ev"],
            "oos_n": oos["n"],
            "oos_win_rate": oos["win_rate"],
            "oos_tp_rate": oos["tp_rate"],
            "oos_sl_rate": oos["sl_rate"],
        }
        results.append(record)
        print(f"  {test_start.strftime('%Y-%m')}: filter={best['filter'][:20]:<20} "
              f"TP{best['tp_pct']:+.1f}/SL{best['sl_pct']:+.1f}/{best['max_days']}d "
              f"train_EV={best['ev']:+.3f}% → oos_EV={oos['ev']:+.3f}% "
              f"(n={oos['n']}, win={oos['win_rate']:.0f}%)")

    return pd.DataFrame(results)


def summarize(df: pd.DataFrame) -> None:
    if df.empty:
        print("結果なし")
        return

    print("\n" + "=" * 72)
    print("ウォークフォワード集計（Out-of-Sample）")
    print("=" * 72)

    n = len(df)
    avg_oos = df["oos_ev"].mean()
    std_oos = df["oos_ev"].std()
    median_oos = df["oos_ev"].median()
    pos_months = (df["oos_ev"] > 0).sum()
    pos_ratio = pos_months / n * 100

    avg_train = df["train_ev"].mean()
    decay = avg_train - avg_oos   # train→test の劣化幅

    print(f"対象月数        : {n}")
    print(f"平均 OOS EV     : {avg_oos:+.4f}%/trade")
    print(f"中央値 OOS EV   : {median_oos:+.4f}%/trade")
    print(f"OOS 標準偏差    : {std_oos:.4f}%")
    print(f"プラス月数      : {pos_months} / {n}  ({pos_ratio:.1f}%)")
    print(f"")
    print(f"参考: 平均 train EV: {avg_train:+.4f}%")
    print(f"      平均 decay  : {decay:+.4f}% (train→OOS の劣化幅)")
    if abs(decay) > avg_train * 0.5 and avg_train > 0:
        print(f"      ⚠️ decay が train EV の50%超 → カーブフィッティング懸念")
    elif avg_oos > 0:
        print(f"      ✅ OOS でも EV+ → ロバスト")

    # 戦略の選好（最頻値）
    print("\n選ばれた戦略の傾向:")
    for col in ["best_filter", "best_tp", "best_sl", "best_days"]:
        top = df[col].value_counts().head(3)
        print(f"  {col}:")
        for val, count in top.items():
            print(f"    {val}: {count}回 ({count/n*100:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="ウォークフォワード検証")
    parser.add_argument("--train-months", type=int, default=6)
    parser.add_argument("--test-months", type=int, default=1)
    parser.add_argument("--min-n", type=int, default=30)
    parser.add_argument("--commission", type=float, default=ROUND_TRIP_COST_PCT)
    parser.add_argument(
        "--holding-mode", choices=["overnight", "half_day"], default="overnight",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("ウォークフォワード検証（Out-of-Sample 安定性チェック）")
    print(f"  train_months={args.train_months}, test_months={args.test_months}")
    print(f"  min_n={args.min_n}, holding_mode={args.holding_mode}, "
          f"cost={args.commission:.2f}%")
    print("=" * 72)

    all_data = load_all_data()
    if all_data.empty:
        print("データなし。先に run_backtest.py --download を実行。")
        return

    # 基本フィルタ
    latest_date = all_data["Date"].max()
    valid_codes = set(
        apply_basic_filter(all_data[all_data["Date"] == latest_date])["Code"].unique()
    )
    df_full = all_data[all_data["Code"].isin(valid_codes)].copy()
    print(f"フィルタ後: {df_full['Code'].nunique()}銘柄")

    # シグナル計算
    df_signals = calc_all_signals(df_full)

    # 未来価格付与
    max_days_needed = max(DAYS_LIST)
    df_future = prepare_future_prices(df_signals, max_days=max_days_needed)

    # ウォークフォワード実行
    results = run_walk_forward(
        df_future,
        train_months=args.train_months,
        test_months=args.test_months,
        min_n=args.min_n,
        holding_mode=args.holding_mode,
        cost_pct=args.commission,
    )

    summarize(results)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "walk_forward_results.csv"
    if not results.empty:
        results.to_csv(out_path, index=False, encoding="utf-8-sig")
        print(f"\n保存: {out_path}  ({len(results)}行)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
backtest/multi_day_backtest.py

1〜5日保有の複数パターンバックテスト。
以下の全組み合わせで期待値・勝率・総利益を計算し、最も利益が出る戦略を探索する。

  - 保有日数: 1, 2, 3, 5 日
  - 利確ライン: +1.5%, +2%, +3%, +5%, +7%, +10%
  - 損切ライン: -1%, -1.5%, -2%, -3%, -5%
  - シグナルフィルタ: 25種類

合計 4 × 6 × 5 × 25 = 3,000 通りを一括検証。

【使用方法】
  python3 backtest/multi_day_backtest.py

【前提条件】
  data/backtest/daily_*.parquet がダウンロード済みであること。
  なければ先に run_backtest.py --download を実行。

【出力ファイル】
  data/backtest/strategy_results.csv   -- 全戦略の成績（3,000行）
  data/backtest/best_strategies.csv    -- 期待値TOP20
  data/backtest/top_totalreturn.csv    -- 総利益TOP20
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

# run_backtest.py の共通関数を再利用
from run_backtest import (
    BACKTEST_START,
    BACKTEST_END,
    WARMUP_CALENDAR_DAYS,
    ROUND_TRIP_COST_PCT,
    load_all_data,
    calc_all_signals,
    apply_basic_filter,
)

DATA_DIR = PROJECT_ROOT / "data" / "backtest"

# ── グリッドサーチのパラメータ ──────────────────────────────────────────────────
TP_LIST   = [1.5, 2.0, 3.0, 5.0, 7.0, 10.0]  # 利確ライン（%）
SL_LIST   = [-1.0, -1.5, -2.0, -3.0, -5.0]   # 損切ライン（%）
DAYS_LIST = [1, 2, 3, 5]                       # 最大保有日数

# ── シグナルフィルタ定義 ────────────────────────────────────────────────────────
# (フィルタ名, 条件関数) のリスト。Stage1通過済みデータに追加適用する。
def _weekday(df):
    return pd.to_datetime(df["Date"]).dt.dayofweek

SIGNAL_FILTERS = {
    # ─ ベースライン ─
    "① 全シグナル（ベースライン）":
        lambda df: pd.Series(True, index=df.index),

    # ─ RSI-5 フィルタ ─
    "② RSI5 < 40（現行）":
        lambda df: df["rsi5"] < 40,
    "③ RSI5 < 30":
        lambda df: df["rsi5"] < 30,
    "④ RSI5 < 25":
        lambda df: df["rsi5"] < 25,
    "⑤ RSI5 < 20（超売られすぎ）":
        lambda df: df["rsi5"] < 20,
    "⑥ RSI5 < 15":
        lambda df: df["rsi5"] < 15,
    "⑦ RSI5 < 10（極端売られすぎ）":
        lambda df: df["rsi5"] < 10,

    # ─ DVS（方向性出来高）フィルタ ─
    "⑧ DVS > 0（買い越し）":
        lambda df: df["dvs"] > 0,
    "⑨ DVS > 10":
        lambda df: df["dvs"] > 10,
    "⑩ DVS > 20（強い買い越し）":
        lambda df: df["dvs"] > 20,

    # ─ ブレイクアウト ─
    "⑪ ブレイクアウトあり":
        lambda df: df["breakout_5d"].astype(bool),

    # ─ 出来高フィルタ ─
    "⑫ 出来高1.5倍以上":
        lambda df: df["vol_ratio"] >= 1.5,
    "⑬ 出来高2倍以上":
        lambda df: df["vol_ratio"] >= 2.0,

    # ─ 52週安値圏 ─
    "⑭ 52週安値圏20%以内":
        lambda df: df["w52_pos"] <= 20,
    "⑮ 52週安値圏40%以内":
        lambda df: df["w52_pos"] <= 40,

    # ─ 複合条件（AND） ─
    "⑯ RSI5<20 + DVS>0":
        lambda df: (df["rsi5"] < 20) & (df["dvs"] > 0),
    "⑰ RSI5<20 + DVS>20":
        lambda df: (df["rsi5"] < 20) & (df["dvs"] > 20),
    "⑱ RSI5<20 + 出来高1.5倍":
        lambda df: (df["rsi5"] < 20) & (df["vol_ratio"] >= 1.5),
    "⑲ RSI5<15 + DVS>0":
        lambda df: (df["rsi5"] < 15) & (df["dvs"] > 0),
    "⑳ RSI5<30 + ブレイクアウト":
        lambda df: (df["rsi5"] < 30) & df["breakout_5d"].astype(bool),
    "㉑ スコア>=20 + RSI5<20":
        lambda df: (df["stage1_score"] >= 20) & (df["rsi5"] < 20),

    # ─ 曜日フィルタ ─
    "㉒ 月曜エントリー除外":
        lambda df: _weekday(df) != 0,
    "㉓ 水・木のみ":
        lambda df: _weekday(df).isin([2, 3]),
    "㉔ RSI5<20 + 月曜除外":
        lambda df: (df["rsi5"] < 20) & (_weekday(df) != 0),
    "㉕ RSI5<15 + DVS>0 + 月曜除外":
        lambda df: (df["rsi5"] < 15) & (df["dvs"] > 0) & (_weekday(df) != 0),
}


# ── 未来価格の事前計算 ────────────────────────────────────────────────────────

def prepare_future_prices(df: pd.DataFrame, max_days: int = 5) -> pd.DataFrame:
    """
    各(Code, Date)に対し、翌1〜max_days営業日のOHLCを列として追加する。
    JQuantsは営業日のみのため、shift(-N)で自動的に営業日ベースになる。
    """
    print(f"[multi] 未来価格を計算中（最大{max_days}日先）...")
    df = df.sort_values(["Code", "Date"]).copy()
    g = df.groupby("Code", group_keys=False)

    for i in range(1, max_days + 1):
        df[f"n{i}_open"]  = g["Open"].shift(-i)
        df[f"n{i}_high"]  = g["High"].shift(-i)
        df[f"n{i}_low"]   = g["Low"].shift(-i)
        df[f"n{i}_close"] = g["Close"].shift(-i)

    print(f"[multi] 未来価格の計算完了")
    return df


# ── 1戦略のアウトカム計算 ─────────────────────────────────────────────────────

def compute_outcomes(
    df: pd.DataFrame,
    tp_pct: float,
    sl_pct: float,
    max_days: int,
    holding_mode: str = "overnight",
    cost_pct: float = ROUND_TRIP_COST_PCT,
) -> pd.Series:
    """
    各シグナル行のリターンを計算して返す（ベクトル化）。

    ロジック:
      エントリー = 翌日始値（n1_open）
      max_days日以内にTP/SLに到達したら決済、到達しなければmax_days日目で決済。
      同日にTPとSLの両方ヒット → SL優先（保守的）。
      逆順パスにより、最も早い決済日が自動的に確定する。

    holding_mode:
      "overnight": TP/SL未到達時は max_days 日目の終値で決済
      "half_day":  TP/SL未到達時は max_days 日目の (始値+終値)/2 で決済（後場寄付近似）

    cost_pct: 往復取引コスト%。結果から減算する。
    """
    entry    = df["n1_open"]
    tp_price = entry * (1 + tp_pct / 100)
    sl_price = entry * (1 + sl_pct / 100)

    # TP/SL 未到達時のデフォルト決済価格
    last_close_col = f"n{max_days}_close"
    if holding_mode == "half_day":
        last_open_col = f"n{max_days}_open"
        default_exit = (df[last_open_col] + df[last_close_col]) / 2.0
    else:
        default_exit = df[last_close_col]

    result = ((default_exit - entry) / entry * 100).copy()

    # 逆順パス: 早い日ほど後から上書きされ最終的に残る（最早決済が勝つ）
    for day in range(max_days, 0, -1):
        high = df[f"n{day}_high"]
        low  = df[f"n{day}_low"]

        tp_hit = (high >= tp_price) & (low > sl_price)
        sl_hit = (low <= sl_price)

        # TP上書き（SLより先に適用）
        result = result.where(~tp_hit, other=tp_pct)
        # SL上書き（同日両ヒット時もSL優先）
        result = result.where(~sl_hit, other=sl_pct)

    # 取引コストを減算（現実的 EV）
    return result - cost_pct


# ── 統計量計算 ────────────────────────────────────────────────────────────────

def _calc_stats(
    returns: pd.Series,
    tp_pct: float,
    sl_pct: float,
    max_days: int,
    filter_name: str,
) -> dict:
    n = len(returns)
    if n == 0:
        return None

    tp_rate  = (returns == tp_pct).mean() * 100
    sl_rate  = (returns == sl_pct).mean() * 100
    win_rate = (returns > 0).mean() * 100
    ev       = float(returns.mean())
    std      = float(returns.std()) if n > 1 else 0.0
    sharpe   = ev / std if std > 0 else 0.0
    total_ev = ev * n   # 全シグナルを取った場合の累積期待値

    return {
        "filter":     filter_name,
        "tp_pct":     tp_pct,
        "sl_pct":     sl_pct,
        "max_days":   max_days,
        "n":          n,
        "tp_rate":    round(tp_rate, 2),
        "sl_rate":    round(sl_rate, 2),
        "win_rate":   round(win_rate, 2),
        "ev":         round(ev, 4),
        "std":        round(std, 4),
        "sharpe":     round(sharpe, 4),
        "total_ev":   round(total_ev, 1),
    }


# ── グリッドサーチ ────────────────────────────────────────────────────────────

def run_grid_search(
    df: pd.DataFrame,
    min_n: int = 50,
    holding_mode: str = "overnight",
    cost_pct: float = ROUND_TRIP_COST_PCT,
) -> pd.DataFrame:
    """
    TP/SL/保有日数/シグナルフィルタの全組み合わせで成績を計算する。

    holding_mode: "overnight" or "half_day"
    cost_pct: 往復取引コスト%
    """
    max_days_all = max(DAYS_LIST)

    # Stage1通過かつ翌日始値があるものだけを対象
    base_mask = (df["stage1_score"] > 0) & df["n1_open"].notna()
    df_base = df[base_mask].copy()
    print(f"[multi] グリッドサーチ対象: {len(df_base):,}シグナル "
          f"(mode={holding_mode}, cost={cost_pct:.2f}%)")

    total_combos = len(TP_LIST) * len(SL_LIST) * len(DAYS_LIST)
    print(f"[multi] 組み合わせ数: TP{len(TP_LIST)} × SL{len(SL_LIST)} × 日数{len(DAYS_LIST)} "
          f"× フィルタ{len(SIGNAL_FILTERS)} = {total_combos * len(SIGNAL_FILTERS):,}通り")
    print("[multi] グリッドサーチ開始（数分かかります）...\n")

    # フィルタマスクを事前計算（繰り返し計算を避ける）
    filter_masks = {}
    for fname, ffunc in SIGNAL_FILTERS.items():
        try:
            mask = ffunc(df_base)
            filter_masks[fname] = mask
        except Exception as e:
            print(f"  [警告] フィルタ '{fname}' 計算エラー: {e}")

    results = []
    combo_idx = 0
    t0 = time.time()

    for max_days in DAYS_LIST:
        # max_days日後のデータが必要。なければNaN → 除外
        valid_col = f"n{max_days}_close"
        df_valid = df_base[df_base[valid_col].notna()].copy()

        for tp in TP_LIST:
            for sl in SL_LIST:
                combo_idx += 1

                # この(tp, sl, max_days)組み合わせの全アウトカムを計算
                outcome_series = compute_outcomes(
                    df_valid, tp, sl, max_days,
                    holding_mode=holding_mode, cost_pct=cost_pct,
                )

                for fname, fmask_full in filter_masks.items():
                    # df_valid に合わせたマスク
                    fmask = fmask_full[fmask_full.index.isin(df_valid.index)]
                    fmask = fmask.reindex(df_valid.index, fill_value=False)

                    subset = outcome_series[fmask]
                    if len(subset) < min_n:
                        continue

                    s = _calc_stats(subset, tp, sl, max_days, fname)
                    if s:
                        results.append(s)

                # 進捗表示
                if combo_idx % 10 == 0:
                    elapsed = time.time() - t0
                    pct = combo_idx / total_combos * 100
                    print(f"  進捗: {combo_idx}/{total_combos} ({pct:.0f}%) "
                          f"経過 {elapsed:.0f}秒  結果数 {len(results):,}", end="\r")

    elapsed = time.time() - t0
    print(f"\n[multi] グリッドサーチ完了: {len(results):,}戦略 / {elapsed:.0f}秒")
    return pd.DataFrame(results)


# ── レポート出力 ─────────────────────────────────────────────────────────────

def print_top_strategies(df: pd.DataFrame, title: str, sort_col: str, n: int = 20) -> None:
    top = df.nlargest(n, sort_col)
    print(f"\n{'='*72}")
    print(f"  {title}  TOP {n}")
    print(f"{'='*72}")
    print(f"  {'フィルタ':<28} {'TP%':>5} {'SL%':>6} {'日数':>4} "
          f"{'件数':>6} {'TP率':>6} {'SL率':>6} {'期待値':>7} {'総EV':>9}")
    print(f"  {'-'*28} {'-'*5} {'-'*6} {'-'*4} "
          f"{'-'*6} {'-'*6} {'-'*6} {'-'*7} {'-'*9}")

    for _, row in top.iterrows():
        filter_short = str(row["filter"])[:28]
        print(
            f"  {filter_short:<28} {row['tp_pct']:>+5.1f} {row['sl_pct']:>+5.1f}% "
            f"{int(row['max_days']):>4}日 {int(row['n']):>6} "
            f"{row['tp_rate']:>5.1f}% {row['sl_rate']:>5.1f}% "
            f"{row['ev']:>+6.3f}% {row['total_ev']:>9.0f}"
        )


def generate_report(results_df: pd.DataFrame) -> None:
    """成績レポートを出力してCSVに保存する。"""

    # EV > 0 の戦略のみ
    positive = results_df[results_df["ev"] > 0].copy()
    print(f"\n[multi] 期待値プラスの戦略数: {len(positive):,} / {len(results_df):,}")

    if positive.empty:
        print("[multi] 期待値プラスの戦略が見つかりませんでした。")
        print("  → SLを広げるか保有日数を増やして再検討してください。")
    else:
        print_top_strategies(positive, "期待値（EV）ランキング", "ev")
        print_top_strategies(positive, "総利益（EV × 件数）ランキング", "total_ev")
        print_top_strategies(positive, "シャープレシオ ランキング", "sharpe")

    # 総合レポートを表示
    print(f"\n{'='*72}")
    print("  ■ TP/SL組み合わせ別 平均期待値（全フィルタ平均）")
    print(f"{'='*72}")
    combo_avg = (
        results_df.groupby(["tp_pct", "sl_pct"])["ev"]
        .mean()
        .unstack("sl_pct")
        .round(3)
    )
    # SL列の順序を整理
    combo_avg = combo_avg[[c for c in sorted(combo_avg.columns)]]
    print(combo_avg.to_string())

    print(f"\n{'='*72}")
    print("  ■ 保有日数別 平均期待値（全TP/SL・フィルタ平均）")
    print(f"{'='*72}")
    days_avg = results_df.groupby("max_days")["ev"].agg(["mean", "count"]).round(4)
    print(days_avg.to_string())

    # ── CSV保存 ───────────────────────────────────────────────────────────────
    all_path = DATA_DIR / "strategy_results.csv"
    results_df.sort_values("ev", ascending=False).to_csv(
        all_path, index=False, encoding="utf-8-sig"
    )
    print(f"\n全戦略CSV: {all_path}  ({len(results_df):,}行)")

    best_path = DATA_DIR / "best_strategies.csv"
    if not positive.empty:
        positive.nlargest(20, "ev").to_csv(best_path, index=False, encoding="utf-8-sig")
        print(f"EV TOP20: {best_path}")

        top_total_path = DATA_DIR / "top_totalreturn.csv"
        positive.nlargest(20, "total_ev").to_csv(
            top_total_path, index=False, encoding="utf-8-sig"
        )
        print(f"総利益TOP20: {top_total_path}")

    # ── 最優秀戦略の要約 ──────────────────────────────────────────────────────
    if not positive.empty:
        best = positive.loc[positive["ev"].idxmax()]
        print(f"\n{'='*72}")
        print("  ★ 最優秀戦略（期待値ベスト）")
        print(f"{'='*72}")
        print(f"  フィルタ  : {best['filter']}")
        print(f"  利確      : +{best['tp_pct']:.1f}%")
        print(f"  損切      : {best['sl_pct']:.1f}%")
        print(f"  最大保有日: {int(best['max_days'])}日")
        print(f"  件数      : {int(best['n']):,}件")
        print(f"  TP達成率  : {best['tp_rate']:.1f}%")
        print(f"  SL発動率  : {best['sl_rate']:.1f}%")
        print(f"  勝率      : {best['win_rate']:.1f}%")
        print(f"  期待値/trade: {best['ev']:+.4f}%")
        print(f"  全シグナル累積EV: {best['total_ev']:+.1f}%")


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="複数パターン・複数保有日数のバックテスト")
    parser.add_argument(
        "--holding-mode",
        choices=["overnight", "half_day"],
        default="overnight",
        help="保有モード: overnight(日跨ぎ) / half_day(後場寄付手仕舞い近似)",
    )
    parser.add_argument(
        "--cost-pct",
        type=float,
        default=ROUND_TRIP_COST_PCT,
        help=f"往復取引コスト%% (既定: {ROUND_TRIP_COST_PCT:.2f}%%)",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("複数パターン バックテスト（1〜5日保有）")
    print(f"期間: {BACKTEST_START} 〜 {BACKTEST_END}")
    print(f"保有モード: {args.holding_mode}   往復コスト: {args.cost_pct:.2f}%")
    print("=" * 72)

    # ── データ読み込み ─────────────────────────────────────────────────────
    all_data = load_all_data()
    if all_data.empty:
        print("データがありません。先に run_backtest.py --download を実行してください。")
        return

    start_dt = pd.Timestamp(BACKTEST_START)
    end_dt   = pd.Timestamp(BACKTEST_END)

    warmup_start = start_dt - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
    df_full = all_data[all_data["Date"] >= warmup_start].copy()

    # 基本フィルタ
    latest_date = df_full["Date"].max()
    valid_codes = set(
        apply_basic_filter(df_full[df_full["Date"] == latest_date])["Code"].unique()
    )
    df_full = df_full[df_full["Code"].isin(valid_codes)].copy()
    print(f"[multi] フィルタ後: {df_full['Code'].nunique()}銘柄")

    # ── シグナル計算 ───────────────────────────────────────────────────────
    df_signals = calc_all_signals(df_full)

    # ── 未来価格の付与 ─────────────────────────────────────────────────────
    max_days_needed = max(DAYS_LIST)
    df_future = prepare_future_prices(df_signals, max_days=max_days_needed)

    # バックテスト期間に絞り込み
    df_test = df_future[
        (df_future["Date"] >= start_dt) &
        (df_future["Date"] <= end_dt)
    ].copy()

    print(f"[multi] テスト期間: {len(df_test):,}行 "
          f"({df_test['Code'].nunique()}銘柄 × {df_test['Date'].nunique()}日)")

    # ── グリッドサーチ ─────────────────────────────────────────────────────
    results_df = run_grid_search(
        df_test, holding_mode=args.holding_mode, cost_pct=args.cost_pct
    )

    if results_df.empty:
        print("[multi] 結果が取得できませんでした。")
        return

    # ── レポート出力 ───────────────────────────────────────────────────────
    generate_report(results_df)


if __name__ == "__main__":
    main()

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
学習期間（TRAIN_START〜TRAIN_END）で最良戦略を探索し、
検証期間（VAL_START〜VAL_END）で汎化性能を確認する。

【使用方法】
  python3 backtest/multi_day_backtest.py

【前提条件】
  data/backtest/daily_*.parquet がダウンロード済みであること。
  なければ先に run_backtest.py --download を実行。

【出力ファイル】
  data/backtest/strategy_results.csv   -- 全戦略の成績（学習+検証列付き）
  data/backtest/best_strategies.csv    -- 期待値TOP20（学習EV基準）
  data/backtest/top_totalreturn.csv    -- 総利益TOP20（学習EV基準）
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

# run_backtest.py の共通関数・定数を再利用
from run_backtest import (
    BACKTEST_START,
    BACKTEST_END,
    WARMUP_CALENDAR_DAYS,
    TRAIN_START,
    TRAIN_END,
    VAL_START,
    VAL_END,
    load_all_data,
    calc_all_signals,
    apply_basic_filter,
)

DATA_DIR = PROJECT_ROOT / "backtest" / "data"

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

    # ─ 直近5日リターン（平均回帰） ─
    "㉖ 直近5日-3%以下":
        lambda df: df["return_5d"] < -3,
    "㉗ 直近5日-5%以下":
        lambda df: df["return_5d"] < -5,
    "㉘ 直近5日-10%以下":
        lambda df: df["return_5d"] < -10,

    # ─ 価格帯フィルタ ─
    "㉙ 低位株(〜500円)":
        lambda df: df["Close"] <= 500,
    "㉚ 中位株(500〜3000円)":
        lambda df: (df["Close"] > 500) & (df["Close"] <= 3000),
    "㉛ 高位株(3000円〜)":
        lambda df: df["Close"] > 3000,

    # ─ 曜日 × DVS/RSI5 複合 ─
    "㉜ 水・木 + DVS>10":
        lambda df: _weekday(df).isin([2, 3]) & (df["dvs"] > 10),
    "㉝ 水・木 + RSI5<20":
        lambda df: _weekday(df).isin([2, 3]) & (df["rsi5"] < 20),
    "㉞ RSI5<10 + 水・木":
        lambda df: (df["rsi5"] < 10) & _weekday(df).isin([2, 3]),

    # ─ MA200乖離率（長期トレンド） ─
    "㉟ MA200割れ(-5%以下)":
        lambda df: df["ma200_diff_pct"] < -5,
    "㊱ MA200割れ(-10%以下)":
        lambda df: df["ma200_diff_pct"] < -10,
    "㊲ MA200上方(+5%以上)":
        lambda df: df["ma200_diff_pct"] > 5,

    # ─ カレンダー効果 ─
    "㊳ 月初め(1〜3営業日)":
        lambda df: df["biz_rank_in_month"] <= 3,
    "㊴ 月末除外(最終2営業日を除く)":
        lambda df: df["biz_rank_from_end"] > 2,
    "㊵ 月初め + DVS>0":
        lambda df: (df["biz_rank_in_month"] <= 3) & (df["dvs"] > 0),

    # ─ 直近下落 × 買いシグナル 複合 ─
    "㊶ 直近5日-5% + DVS>0":
        lambda df: (df["return_5d"] < -5) & (df["dvs"] > 0),
    "㊷ 直近5日-5% + RSI5<20":
        lambda df: (df["return_5d"] < -5) & (df["rsi5"] < 20),
    "㊸ 直近5日-3% + 月曜除外":
        lambda df: (df["return_5d"] < -3) & (_weekday(df) != 0),
    "㊹ 直近5日-5% + 水・木":
        lambda df: (df["return_5d"] < -5) & _weekday(df).isin([2, 3]),
    "㊺ 直近5日-5% + DVS>0 + 月曜除外":
        lambda df: (df["return_5d"] < -5) & (df["dvs"] > 0) & (_weekday(df) != 0),
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
) -> pd.Series:
    """
    各シグナル行のリターンを計算して返す（ベクトル化）。

    ロジック:
      エントリー = 翌日始値（n1_open）
      max_days日以内にTP/SLに到達したら決済、到達しなければmax_days日目の終値で決済。
      同日にTPとSLの両方ヒット → SL優先（保守的）。
      逆順パスにより、最も早い決済日が自動的に確定する。
    """
    entry    = df["n1_open"]
    tp_price = entry * (1 + tp_pct / 100)
    sl_price = entry * (1 + sl_pct / 100)

    # デフォルト: max_days日目の終値で決済
    last_close_col = f"n{max_days}_close"
    result = ((df[last_close_col] - entry) / entry * 100).copy()

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

    return result


# ── 統計量計算 ────────────────────────────────────────────────────────────────

def _calc_stats(
    returns: pd.Series,
    tp_pct: float,
    sl_pct: float,
    max_days: int,
    filter_name: str,
) -> dict:
    """学習データの統計量を計算してdictで返す。"""
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
        "filter":      filter_name,
        "tp_pct":      tp_pct,
        "sl_pct":      sl_pct,
        "max_days":    max_days,
        "train_n":     n,
        "train_ev":    round(ev, 4),
        "train_tp_rate": round(tp_rate, 2),
        "train_sl_rate": round(sl_rate, 2),
        "train_sharpe":  round(sharpe, 4),
        # 後方互換のため旧フィールド名も保持
        "n":          n,
        "tp_rate":    round(tp_rate, 2),
        "sl_rate":    round(sl_rate, 2),
        "win_rate":   round(win_rate, 2),
        "ev":         round(ev, 4),
        "std":        round(std, 4),
        "sharpe":     round(sharpe, 4),
        "total_ev":   round(total_ev, 1),
    }


def _calc_val_stats(
    val_returns: pd.Series,
    tp_pct: float,
    sl_pct: float,
) -> dict:
    """検証データの統計量を計算してdictで返す。"""
    n = len(val_returns)
    if n == 0:
        return {
            "val_n": 0,
            "val_ev": 0.0,
            "val_tp_rate": 0.0,
            "val_sl_rate": 0.0,
            "generalize": False,
        }
    tp_rate = (val_returns == tp_pct).mean() * 100
    sl_rate = (val_returns == sl_pct).mean() * 100
    ev      = float(val_returns.mean())
    return {
        "val_n":       n,
        "val_ev":      round(ev, 4),
        "val_tp_rate": round(tp_rate, 2),
        "val_sl_rate": round(sl_rate, 2),
        "generalize":  ev > 0,
    }


# ── グリッドサーチ ────────────────────────────────────────────────────────────

def run_grid_search(
    df_train: pd.DataFrame,
    df_val: pd.DataFrame,
    min_n: int = 50,
) -> pd.DataFrame:
    """
    TP/SL/保有日数/シグナルフィルタの全組み合わせで成績を計算する。
    学習データ（df_train）で戦略を探索し、検証データ（df_val）で汎化性能を確認する。
    """
    max_days_all = max(DAYS_LIST)

    # Stage1通過かつ翌日始値があるものだけを対象
    train_mask = (df_train["stage1_score"] > 0) & df_train["n1_open"].notna()
    df_train_base = df_train[train_mask].copy()

    val_mask = (df_val["stage1_score"] > 0) & df_val["n1_open"].notna()
    df_val_base = df_val[val_mask].copy()

    print(f"[multi] グリッドサーチ対象（学習）: {len(df_train_base):,}シグナル")
    print(f"[multi] グリッドサーチ対象（検証）: {len(df_val_base):,}シグナル")

    total_combos = len(TP_LIST) * len(SL_LIST) * len(DAYS_LIST)
    print(f"[multi] 組み合わせ数: TP{len(TP_LIST)} × SL{len(SL_LIST)} × 日数{len(DAYS_LIST)} "
          f"× フィルタ{len(SIGNAL_FILTERS)} = {total_combos * len(SIGNAL_FILTERS):,}通り")
    print("[multi] グリッドサーチ開始（数分かかります）...\n")

    # フィルタマスクを事前計算（繰り返し計算を避ける）
    train_filter_masks = {}
    val_filter_masks   = {}
    for fname, ffunc in SIGNAL_FILTERS.items():
        try:
            train_filter_masks[fname] = ffunc(df_train_base)
            val_filter_masks[fname]   = ffunc(df_val_base) if not df_val_base.empty else pd.Series(dtype=bool)
        except Exception as e:
            print(f"  [警告] フィルタ '{fname}' 計算エラー: {e}")

    results = []
    combo_idx = 0
    t0 = time.time()

    for max_days in DAYS_LIST:
        valid_col = f"n{max_days}_close"
        df_train_valid = df_train_base[df_train_base[valid_col].notna()].copy()
        df_val_valid   = df_val_base[df_val_base[valid_col].notna()].copy() if not df_val_base.empty else pd.DataFrame()

        for tp in TP_LIST:
            for sl in SL_LIST:
                combo_idx += 1

                # 学習データのアウトカム
                train_outcomes = compute_outcomes(df_train_valid, tp, sl, max_days)

                # 検証データのアウトカム（データがある場合のみ）
                val_outcomes = (
                    compute_outcomes(df_val_valid, tp, sl, max_days)
                    if not df_val_valid.empty
                    else pd.Series(dtype=float)
                )

                for fname in train_filter_masks:
                    # 学習フィルタ
                    fmask_train = train_filter_masks[fname]
                    fmask_train = fmask_train[fmask_train.index.isin(df_train_valid.index)]
                    fmask_train = fmask_train.reindex(df_train_valid.index, fill_value=False)
                    train_subset = train_outcomes[fmask_train]

                    if len(train_subset) < min_n:
                        continue

                    s = _calc_stats(train_subset, tp, sl, max_days, fname)
                    if s is None:
                        continue

                    # 検証フィルタ
                    if fname in val_filter_masks and not val_outcomes.empty:
                        fmask_val = val_filter_masks[fname]
                        fmask_val = fmask_val[fmask_val.index.isin(df_val_valid.index)]
                        fmask_val = fmask_val.reindex(df_val_valid.index, fill_value=False)
                        val_subset = val_outcomes[fmask_val]
                    else:
                        val_subset = pd.Series(dtype=float)

                    vs = _calc_val_stats(val_subset, tp, sl)
                    s.update(vs)
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
    print(f"\n{'='*80}")
    print(f"  {title}  TOP {n}")
    print(f"{'='*80}")
    print(f"  {'フィルタ':<28} {'TP%':>5} {'SL%':>6} {'日数':>4} "
          f"{'設計件数':>8} {'設計EV':>8} {'検証件数':>8} {'検証EV':>8} {'汎化':>4}")
    print(f"  {'-'*28} {'-'*5} {'-'*6} {'-'*4} "
          f"{'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*4}")

    for _, row in top.iterrows():
        filter_short = str(row["filter"])[:28]
        train_ev = row.get("train_ev", row.get("ev", float("nan")))
        train_n  = int(row.get("train_n", row.get("n", 0)))
        val_n    = int(row.get("val_n", 0))
        val_ev   = row.get("val_ev", float("nan"))
        generalize = "✅" if row.get("generalize", False) else "❌"
        print(
            f"  {filter_short:<28} {row['tp_pct']:>+5.1f} {row['sl_pct']:>+5.1f}% "
            f"{int(row['max_days']):>4}日 {train_n:>8} "
            f"{train_ev:>+7.3f}% {val_n:>8} {val_ev:>+7.3f}% {generalize:>4}"
        )


def generate_report(results_df: pd.DataFrame) -> None:
    """成績レポートを出力してCSVに保存する。学習EV基準でソートし過学習チェックを行う。"""

    # train_ev 列が存在しない場合は ev 列にフォールバック
    if "train_ev" not in results_df.columns and "ev" in results_df.columns:
        results_df = results_df.copy()
        results_df["train_ev"] = results_df["ev"]
    if "train_n" not in results_df.columns and "n" in results_df.columns:
        results_df = results_df.copy()
        results_df["train_n"] = results_df["n"]

    # 学習EVがプラスの戦略
    positive = results_df[results_df["train_ev"] > 0].copy()
    total    = len(results_df)
    print(f"\n[multi] 学習期待値プラスの戦略数: {len(positive):,} / {total:,}")

    if positive.empty:
        print("[multi] 期待値プラスの戦略が見つかりませんでした。")
        print("  → SLを広げるか保有日数を増やして再検討してください。")
    else:
        print_top_strategies(positive, "期待値（設計EV）ランキング", "train_ev")
        print_top_strategies(positive, "総利益（EV × 件数）ランキング", "total_ev")
        print_top_strategies(positive, "シャープレシオ ランキング", "train_sharpe")

    # 総合レポートを表示
    print(f"\n{'='*80}")
    print("  ■ TP/SL組み合わせ別 平均期待値（全フィルタ平均）")
    print(f"{'='*80}")
    combo_avg = (
        results_df.groupby(["tp_pct", "sl_pct"])["train_ev"]
        .mean()
        .unstack("sl_pct")
        .round(3)
    )
    # SL列の順序を整理
    combo_avg = combo_avg[[c for c in sorted(combo_avg.columns)]]
    print(combo_avg.to_string())

    print(f"\n{'='*80}")
    print("  ■ 保有日数別 平均期待値（全TP/SL・フィルタ平均）")
    print(f"{'='*80}")
    days_avg = results_df.groupby("max_days")["train_ev"].agg(["mean", "count"]).round(4)
    print(days_avg.to_string())

    # ── 過学習チェック ────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("  【過学習チェック】")
    print(f"{'='*80}")
    if "val_ev" in results_df.columns:
        overfit = results_df[
            (results_df["train_ev"] > 0) & (results_df["val_ev"] <= 0)
        ]
        generalize_ok = results_df[
            (results_df["train_ev"] > 0) & (results_df["val_ev"] > 0)
        ]
        no_val = results_df[
            (results_df["train_ev"] > 0) & (results_df["val_n"] == 0)
        ] if "val_n" in results_df.columns else pd.DataFrame()

        print(f"  学習EV>0 の戦略:         {len(positive):,}件")
        print(f"  うち 検証EV>0（汎化OK）: {len(generalize_ok):,}件")
        print(f"  うち 検証EV≤0（過学習）: {len(overfit):,}件")
        if not no_val.empty:
            print(f"  うち 検証データなし:     {len(no_val):,}件")

        if not overfit.empty:
            print(f"\n  過学習戦略 TOP10（設計EVが高いが検証EVがマイナス）:")
            print(f"  {'フィルタ':<28} {'TP%':>5} {'SL%':>6} {'日数':>4} "
                  f"{'設計EV':>8} {'検証EV':>8}")
            print(f"  {'-'*28} {'-'*5} {'-'*6} {'-'*4} {'-'*8} {'-'*8}")
            for _, row in overfit.nlargest(10, "train_ev").iterrows():
                filter_short = str(row["filter"])[:28]
                print(
                    f"  {filter_short:<28} {row['tp_pct']:>+5.1f} {row['sl_pct']:>+5.1f}% "
                    f"{int(row['max_days']):>4}日 "
                    f"{row['train_ev']:>+7.3f}% {row['val_ev']:>+7.3f}%"
                )
    else:
        print("  検証データ列（val_ev）が存在しません。")

    # ── CSV保存 ───────────────────────────────────────────────────────────────
    all_path = DATA_DIR / "strategy_results.csv"
    # 学習EVでソート
    sort_col = "train_ev" if "train_ev" in results_df.columns else "ev"
    results_df.sort_values(sort_col, ascending=False).to_csv(
        all_path, index=False, encoding="utf-8-sig"
    )
    print(f"\n全戦略CSV: {all_path}  ({len(results_df):,}行)")

    best_path = DATA_DIR / "best_strategies.csv"
    if not positive.empty:
        positive.nlargest(20, "train_ev").to_csv(best_path, index=False, encoding="utf-8-sig")
        print(f"EV TOP20: {best_path}")

        top_total_path = DATA_DIR / "top_totalreturn.csv"
        positive.nlargest(20, "total_ev").to_csv(
            top_total_path, index=False, encoding="utf-8-sig"
        )
        print(f"総利益TOP20: {top_total_path}")

    # ── 最優秀戦略の要約 ──────────────────────────────────────────────────────
    if not positive.empty:
        best = positive.loc[positive["train_ev"].idxmax()]
        print(f"\n{'='*80}")
        print("  ★ 最優秀戦略（設計期待値ベスト）")
        print(f"{'='*80}")
        print(f"  フィルタ  : {best['filter']}")
        print(f"  利確      : +{best['tp_pct']:.1f}%")
        print(f"  損切      : {best['sl_pct']:.1f}%")
        print(f"  最大保有日: {int(best['max_days'])}日")
        print(f"  設計件数  : {int(best.get('train_n', best.get('n', 0))):,}件")
        print(f"  TP達成率  : {best.get('train_tp_rate', best.get('tp_rate', 0)):.1f}%")
        print(f"  SL発動率  : {best.get('train_sl_rate', best.get('sl_rate', 0)):.1f}%")
        print(f"  勝率      : {best.get('win_rate', 0):.1f}%")
        print(f"  設計EV/trade: {best['train_ev']:+.4f}%")
        print(f"  全シグナル累積EV: {best['total_ev']:+.1f}%")
        if "val_ev" in best:
            val_n  = int(best.get("val_n", 0))
            val_ev = best.get("val_ev", float("nan"))
            generalize = "✅汎化OK" if best.get("generalize", False) else "❌過学習"
            print(f"  検証件数  : {val_n:,}件")
            print(f"  検証EV/trade: {val_ev:+.4f}%  {generalize}")


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("複数パターン バックテスト（1〜5日保有）【学習/検証 分割評価】")
    print(f"学習期間: {TRAIN_START} 〜 {TRAIN_END}")
    print(f"検証期間: {VAL_START} 〜 {VAL_END}")
    print("=" * 80)

    # ── データ読み込み ─────────────────────────────────────────────────────
    all_data = load_all_data()
    if all_data.empty:
        print("データがありません。先に run_backtest.py --download を実行してください。")
        return

    train_start_dt = pd.Timestamp(TRAIN_START)
    train_end_dt   = pd.Timestamp(TRAIN_END)
    val_start_dt   = pd.Timestamp(VAL_START)
    val_end_dt     = pd.Timestamp(VAL_END)

    # ウォームアップはTRAIN_STARTの180日前から
    warmup_start = train_start_dt - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
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

    # ── 学習/検証に分割 ───────────────────────────────────────────────────
    df_train = df_future[
        (df_future["Date"] >= train_start_dt) &
        (df_future["Date"] <= train_end_dt)
    ].copy()

    df_val = df_future[
        (df_future["Date"] >= val_start_dt) &
        (df_future["Date"] <= val_end_dt)
    ].copy()

    print(f"[multi] 学習期間: {len(df_train):,}行 "
          f"({df_train['Code'].nunique()}銘柄 × {df_train['Date'].nunique()}日)")
    print(f"[multi] 検証期間: {len(df_val):,}行 "
          f"({df_val['Code'].nunique()}銘柄 × {df_val['Date'].nunique()}日)")

    # ── グリッドサーチ（学習データで戦略探索、検証データで汎化確認） ──────
    results_df = run_grid_search(df_train, df_val)

    if results_df.empty:
        print("[multi] 結果が取得できませんでした。")
        return

    # ── レポート出力 ───────────────────────────────────────────────────────
    generate_report(results_df)


if __name__ == "__main__":
    main()

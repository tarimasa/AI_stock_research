#!/usr/bin/env python3
"""
backtest/limit_fill_analyzer.py

「指値の取り逃し」分析スクリプト。

現行ロジックでは推奨価格 = 当日終値 × 0.993（-0.7%）の指値を翌日に出すため、
寄りで指値より高く始まると約定せず、その後上昇したら丸ごと取り逃す。
本スクリプトは過去データで以下を測定する:

  1. 指値の約定率（fill rate）
  2. 約定したトレードの期待値（EV）
  3. 不約定（取り逃し）シグナルを「もし寄成行で買っていた場合」の期待値
     = 取り逃しの機会損失
  4. 注文戦略を変えた場合の累積 PnL 比較
     A. 現行          : 指値のみ（不約定は不参戦）
     B. 寄成行        : 翌日寄付で必ず買う
     C. 条件付き寄成行: ギャップアップが +X% 以内なら寄成、超過なら見送り
     D. 分割注文      : 指値半分 + 寄成行半分

使い方:
  cd ~/AI_stock_research
  source venv/bin/activate
  python3 backtest/limit_fill_analyzer.py

主なオプション:
  --limit-pct -0.7      指値のオフセット%（既定: -0.7% = 前日終値×0.993）
  --sl-pct -5.0         損切り%（既定: -5%）
  --tp-pct 7.5          利確%（既定: +7.5%）
  --holding-days 3      最大保有日数（既定: 3日）
  --max-gap-pct 1.0     C戦略の許容ギャップアップ%（既定: +1.0%）
  --cost-pct 0.20       往復取引コスト%（既定: 0.20%）
  --min-score 60        Stage1 スコア下限（既定: 60 = 実運用デフォルト）
  --period {train,val,all}
                        集計対象期間（既定: all）

出力:
  backtest/data/limit_fill_signals.csv  -- 各シグナルの詳細（fill_status, 各種 return）
  backtest/data/limit_fill_summary.csv  -- シナリオ比較表
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

from price_calculator import _tick_round  # 単一価格用（テスト互換確認）
from run_backtest import (
    TRAIN_START, TRAIN_END, VAL_START, VAL_END,
    WARMUP_CALENDAR_DAYS,
    load_all_data, apply_basic_filter, calc_all_signals,
)

DATA_DIR = PROJECT_ROOT / "backtest" / "data"


# ── 呼値丸め（ベクトル化版） ─────────────────────────────────────────────────

def tick_round_series(s: pd.Series) -> pd.Series:
    """
    price_calculator._tick_round と同じ東証呼値ルールをベクトル化する。
      ・〜3,000円   : 1円単位
      ・3,001〜5,000円: 5円単位
      ・5,001〜30,000円: 10円単位
      ・30,001円〜  : 50円単位
    丸めは round-half-up（_tick_round と同等）。
    """
    s = pd.to_numeric(s, errors="coerce")
    out = pd.Series(np.nan, index=s.index, dtype="float64")
    m1 = s <= 3000
    m2 = (s > 3000) & (s <= 5000)
    m3 = (s > 5000) & (s <= 30000)
    m4 = s > 30000
    out.loc[m1] = np.floor(s[m1] / 1.0 + 0.5) * 1.0
    out.loc[m2] = np.floor(s[m2] / 5.0 + 0.5) * 5.0
    out.loc[m3] = np.floor(s[m3] / 10.0 + 0.5) * 10.0
    out.loc[m4] = np.floor(s[m4] / 50.0 + 0.5) * 50.0
    return out


# ── 将来バー（保有期間内の High/Low/Close）の取得 ──────────────────────────

def add_future_bars(df: pd.DataFrame, days: int) -> pd.DataFrame:
    """
    各行に対して翌営業日から +days 営業日先までの High/Low/Close を列追加する。
    JQuants データは営業日のみで構成されるため、shift(-k) で k 営業日先を参照可能。
    """
    df = df.sort_values(["Code", "Date"]).copy()
    g = df.groupby("Code", group_keys=False)
    for k in range(1, days + 1):
        df[f"high_d{k}"]  = g["High"].shift(-k)
        df[f"low_d{k}"]   = g["Low"].shift(-k)
        df[f"close_d{k}"] = g["Close"].shift(-k)
        df[f"date_d{k}"]  = g["Date"].shift(-k)
    return df


# ── 約定判定 ──────────────────────────────────────────────────────────────

def add_limit_fill_columns(df: pd.DataFrame, limit_pct: float) -> pd.DataFrame:
    """
    各シグナルに対する指値約定状況を列追加する。

      limit_price   : 当日終値 × (1 + limit_pct/100) を呼値丸めした指値
      gap_pct       : 翌日始値の対前日終値ギャップ%（+ ならギャップアップ）
      fill_status   : 'fill_open'(寄付で約定) / 'fill_intraday'(場中で約定) / 'missed'(不約定)
      limit_entry   : 約定価格（不約定は NaN）

    約定ルール:
      - 翌日始値 <= 指値    → 寄付で指値より下にギャップ → 始値で約定（買い側に有利）
      - 上記NG かつ 翌日安値 <= 指値 → 場中で指値到達 → 指値価格で約定
      - 両方NG                       → 不約定（missed）
    """
    df = df.copy()
    df["limit_price"] = tick_round_series(df["Close"] * (1.0 + limit_pct / 100.0))

    # 翌日 OHL は事前に add_next_bars() で付与されている前提
    next_open = df["next_open"]
    next_low  = df["next_low"]

    df["gap_pct"] = ((next_open / df["Close"]) - 1.0) * 100.0

    fill_at_open    = next_open <= df["limit_price"]
    fill_intraday   = (~fill_at_open) & (next_low <= df["limit_price"])
    missed          = ~fill_at_open & ~fill_intraday
    # next_open が NaN（最終日など）は missed と区別するため "invalid"
    invalid = next_open.isna()

    status = pd.Series("missed", index=df.index, dtype=object)
    status[fill_at_open]  = "fill_open"
    status[fill_intraday] = "fill_intraday"
    status[invalid]       = "invalid"
    df["fill_status"] = status

    df["limit_entry"] = np.where(
        fill_at_open & ~invalid, next_open,
        np.where(fill_intraday & ~invalid, df["limit_price"], np.nan),
    )
    return df


def add_next_bars(df: pd.DataFrame) -> pd.DataFrame:
    """next_open / next_low / next_high / next_close 列を付与する。"""
    df = df.sort_values(["Code", "Date"]).copy()
    g = df.groupby("Code", group_keys=False)
    df["next_open"]  = g["Open"].shift(-1)
    df["next_high"] = g["High"].shift(-1)
    df["next_low"]  = g["Low"].shift(-1)
    df["next_close"] = g["Close"].shift(-1)
    return df


# ── N日保有シミュレーション ──────────────────────────────────────────────

def simulate_holding(
    df: pd.DataFrame,
    entry_col: str,
    sl_pct: float,
    tp_pct: float,
    days: int,
    cost_pct: float,
) -> tuple[pd.Series, pd.Series]:
    """
    entry_col の価格でエントリーしたとして N 日保有のリターン（手数料込み手取り）を返す。

    決済優先順位:
      ・各日 Low <= SL価格 → SL（保守的に同日両ヒット時も SL 優先）
      ・上記NG かつ High >= TP価格 → TP
      ・どちらにも当たらず最終日まで保有 → 最終日 Close で決済
      ・必要なバーが NaN（期間終端で先のバー無し）の行は NaN を返す

    Returns:
        (return_net_pct, outcome) — どちらも index は df と一致
    """
    entry = df[entry_col].astype(float)
    tp_price = entry * (1.0 + tp_pct / 100.0)
    sl_price = entry * (1.0 + sl_pct / 100.0)

    n = len(df)
    return_gross = pd.Series(np.nan, index=df.index, dtype="float64")
    outcome      = pd.Series("invalid", index=df.index, dtype=object)
    decided      = pd.Series(False, index=df.index)

    # エントリー価格が NaN の行（不約定等）はそのまま invalid のまま
    entry_valid = entry.notna()

    for k in range(1, days + 1):
        hi = df[f"high_d{k}"]
        lo = df[f"low_d{k}"]
        cl = df[f"close_d{k}"]

        active = entry_valid & ~decided & lo.notna() & hi.notna()

        sl_today = active & (lo <= sl_price)
        tp_today = active & ~sl_today & (hi >= tp_price)

        return_gross.loc[sl_today] = sl_pct
        outcome.loc[sl_today]      = "sl"
        decided.loc[sl_today]      = True

        return_gross.loc[tp_today] = tp_pct
        outcome.loc[tp_today]      = "tp"
        decided.loc[tp_today]      = True

        if k == days:
            still = entry_valid & ~decided & cl.notna()
            return_gross.loc[still] = (cl[still] / entry[still] - 1.0) * 100.0
            outcome.loc[still]      = "exit_close"
            decided.loc[still]      = True

    return_net = return_gross - cost_pct
    return return_net, outcome


# ── シナリオ比較 ──────────────────────────────────────────────────────────

def build_scenarios(
    df: pd.DataFrame,
    sl_pct: float,
    tp_pct: float,
    days: int,
    cost_pct: float,
    max_gap_pct: float,
) -> pd.DataFrame:
    """
    各シグナル行について、戦略別のリターンを列追加する。

      ret_limit      : 戦略A 指値のみ。約定したら simulate_holding、未約定は NaN（不参戦）
      ret_open       : 戦略B 寄成行で必ず買う
      ret_cond_open  : 戦略C 寄成行だが gap_pct <= max_gap_pct のときだけ買う
      ret_split      : 戦略D 半分指値 + 半分寄成（指値が不約定なら寄成側の半分のみ）

    PnL集計用に、不参戦行は NaN として残し「シグナル全体に対する純EV」算出時には
    NaN→0 で平均する（=「参戦できなかった分は 0% で機会損失」と扱う）。
    """
    df = df.copy()

    # 各シナリオの「実際の」エントリー価格列を構築
    df["entry_limit"]    = df["limit_entry"]                                     # NaN なら不参戦
    df["entry_open"]     = df["next_open"]                                        # 常にエントリー
    df["entry_cond"]     = np.where(df["gap_pct"] <= max_gap_pct, df["next_open"], np.nan)

    # A. 指値のみ
    df["ret_limit"], df["outcome_limit"] = simulate_holding(
        df, "entry_limit", sl_pct, tp_pct, days, cost_pct
    )
    # B. 寄成行
    df["ret_open"], df["outcome_open"] = simulate_holding(
        df, "entry_open", sl_pct, tp_pct, days, cost_pct
    )
    # C. 条件付き寄成行（gap <= max_gap_pct のみ参戦）
    df["ret_cond_open"], df["outcome_cond_open"] = simulate_holding(
        df, "entry_cond", sl_pct, tp_pct, days, cost_pct
    )
    # D. 分割（指値約定時のみ 0.5 × ret_limit、常に 0.5 × ret_open）
    #    指値が約定すれば両方の半分を取得、指値不約定なら寄成半分のみ
    #    （取引コストは 0.5 × cost_pct を 2 回分 = cost_pct 相当として簡略化済み）
    half_limit = df["ret_limit"].where(df["ret_limit"].notna(), 0.0) * 0.5
    half_open  = df["ret_open"].where(df["ret_open"].notna(), 0.0) * 0.5
    # ret_open が NaN（=データ端で T+1 が無い）の行はシナリオ全体を NaN にする
    invalid_mask = df["ret_open"].isna()
    df["ret_split"] = (half_limit + half_open).where(~invalid_mask, np.nan)

    return df


# ── サマリー統計 ──────────────────────────────────────────────────────────

def _strategy_stats(ret: pd.Series, total_signals: int) -> dict:
    """
    1戦略のリターン系列から統計をまとめる。

      n_taken       : 参戦できたトレード数（NaN を除く件数）
      participate%  : 参戦率 = n_taken / total_signals
      ev_when_taken : 約定したトレードのみの平均リターン
      ev_per_signal : シグナル全体に対する平均リターン（不参戦は 0% 寄与）
      win_rate      : 約定したトレードのうち return>0 の割合
      total_pnl     : ev_per_signal × total_signals
    """
    taken = ret.dropna()
    n_taken = len(taken)
    return {
        "n_signals":     total_signals,
        "n_taken":       n_taken,
        "participate_pct": round(n_taken / total_signals * 100, 2) if total_signals else 0.0,
        "ev_when_taken": round(float(taken.mean()), 4) if n_taken else 0.0,
        "win_rate":      round((taken > 0).mean() * 100, 2) if n_taken else 0.0,
        "ev_per_signal": round(float(taken.sum() / total_signals), 4) if total_signals else 0.0,
        "total_pnl_pct": round(float(taken.sum()), 2),
    }


def summarize(df: pd.DataFrame, max_gap_pct: float) -> pd.DataFrame:
    """シナリオ別の比較表を DataFrame で返す。"""
    valid = df[df["ret_open"].notna()].copy()  # T+1 が取れる行のみ
    n = len(valid)

    rows = []
    rows.append({"strategy": "A. 指値のみ (現行)",
                 **_strategy_stats(valid["ret_limit"], n)})
    rows.append({"strategy": "B. 寄成行 (常時)",
                 **_strategy_stats(valid["ret_open"], n)})
    rows.append({"strategy": f"C. 条件付き寄成 (gap<=+{max_gap_pct:.1f}%)",
                 **_strategy_stats(valid["ret_cond_open"], n)})
    rows.append({"strategy": "D. 分割 50/50",
                 **_strategy_stats(valid["ret_split"], n)})
    return pd.DataFrame(rows)


def fill_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """fill_status 別の件数・割合と、寄成行を取った場合のリターンを集計する。"""
    valid = df[df["ret_open"].notna()].copy()
    n = len(valid)
    rows = []
    for status in ("fill_open", "fill_intraday", "missed"):
        sub = valid[valid["fill_status"] == status]
        ns = len(sub)
        rows.append({
            "fill_status":   status,
            "n":             ns,
            "share_pct":     round(ns / n * 100, 2) if n else 0.0,
            "avg_gap_pct":   round(float(sub["gap_pct"].mean()), 3) if ns else 0.0,
            "ev_if_open":    round(float(sub["ret_open"].mean()), 4) if ns else 0.0,
            "ev_if_limit":   (
                round(float(sub["ret_limit"].mean()), 4)
                if ns and sub["ret_limit"].notna().any() else None
            ),
        })
    return pd.DataFrame(rows)


def gap_bucket_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    """ギャップ%別の寄成リターン分布を集計する（戦略C の閾値判断材料）。"""
    valid = df[df["ret_open"].notna()].copy()
    if valid.empty:
        return pd.DataFrame()

    bins   = [-100, -2, -1, -0.5, 0, 0.5, 1.0, 2.0, 3.0, 5.0, 100]
    labels = ["≦-2%", "(-2,-1]", "(-1,-0.5]", "(-0.5,0]",
              "(0,+0.5]", "(+0.5,+1]", "(+1,+2]", "(+2,+3]", "(+3,+5]", ">+5%"]
    valid["gap_bucket"] = pd.cut(valid["gap_pct"], bins=bins, labels=labels,
                                  include_lowest=True)
    g = valid.groupby("gap_bucket", observed=True)["ret_open"]
    out = g.agg(["count", "mean", "median",
                 lambda x: (x > 0).mean() * 100]).rename(
        columns={"count": "n", "mean": "ev_open",
                 "median": "median_open", "<lambda_0>": "win_rate"}
    )
    return out.reset_index().round(3)


# ── レポート出力 ──────────────────────────────────────────────────────────

def print_report(df: pd.DataFrame, summary: pd.DataFrame,
                 fill_break: pd.DataFrame, gap_break: pd.DataFrame,
                 args) -> None:
    print("\n" + "=" * 78)
    print(" 指値の取り逃し分析レポート")
    print("=" * 78)
    print(f"  指値オフセット : {args.limit_pct:+.2f}%  "
          f"(= 当日終値 × {1 + args.limit_pct/100:.4f})")
    print(f"  保有日数       : 最大 {args.holding_days} 日")
    print(f"  TP / SL        : +{args.tp_pct:.2f}% / {args.sl_pct:+.2f}%")
    print(f"  往復コスト     : {args.cost_pct:.2f}%（純EVから減算済）")
    print(f"  Stage1 スコア   : >= {args.min_score}")
    print(f"  集計期間       : {args.period}")
    print(f"  対象シグナル数 : {len(df):,}件 "
          f"({df['Code'].nunique()}銘柄 × {df['Date'].nunique()}日)")

    # ── ① 約定状況のブレークダウン ─────────────────────────────────────
    print("\n【① 約定状況のブレークダウン】")
    print(f"  {'状態':<14} {'件数':>8} {'シェア':>8} "
          f"{'平均ギャップ':>11} {'寄成り時EV':>11} {'指値時EV':>10}")
    print(f"  {'-'*14} {'-'*8} {'-'*8} {'-'*11} {'-'*11} {'-'*10}")
    for _, r in fill_break.iterrows():
        ev_lim_val = r["ev_if_limit"]
        if ev_lim_val is None or (isinstance(ev_lim_val, float) and pd.isna(ev_lim_val)):
            ev_lim = "    -   "
        else:
            ev_lim = f"{ev_lim_val:+.3f}%"
        print(f"  {r['fill_status']:<14} {r['n']:>8,} "
              f"{r['share_pct']:>7.2f}% "
              f"{r['avg_gap_pct']:>+10.3f}% "
              f"{r['ev_if_open']:>+10.3f}% "
              f"{ev_lim:>10}")
    miss_row = fill_break[fill_break["fill_status"] == "missed"].iloc[0]
    print(f"\n  → 取り逃し率: {miss_row['share_pct']:.2f}% "
          f"(寄成りで入っていたら平均 {miss_row['ev_if_open']:+.3f}% / 件 取れた可能性)")

    # ── ② シナリオ比較 ─────────────────────────────────────────────────
    n_compare = int(summary.iloc[0]["n_signals"]) if len(summary) else 0
    print(f"\n【② 注文戦略の比較（比較対象シグナル: {n_compare:,}件 = T+1バー有り）】")
    print(f"  {'戦略':<32} {'参戦数':>7} {'参戦率':>7} "
          f"{'約定時EV':>9} {'勝率':>7} {'純EV':>9} {'累積%':>9}")
    print(f"  {'-'*32} {'-'*7} {'-'*7} {'-'*9} {'-'*7} {'-'*9} {'-'*9}")
    for _, r in summary.iterrows():
        print(f"  {r['strategy']:<32} {r['n_taken']:>7,} "
              f"{r['participate_pct']:>6.2f}% "
              f"{r['ev_when_taken']:>+8.3f}% "
              f"{r['win_rate']:>6.1f}% "
              f"{r['ev_per_signal']:>+8.3f}% "
              f"{r['total_pnl_pct']:>+8.1f}%")
    print("\n  ※ 純EV = 全シグナルに対する平均リターン（不参戦は 0% 寄与で計算）")
    print("  ※ 累積% = 純EV × シグナル数。資金配分一定での合計リターン目安。")

    # ── ③ ギャップ別の寄成りリターン ───────────────────────────────────
    if not gap_break.empty:
        print("\n【③ ギャップ%別の寄成りリターン（戦略Cの閾値選定用）】")
        print(f"  {'gap範囲':<12} {'件数':>7} {'平均EV':>9} "
              f"{'中央値':>9} {'勝率%':>7}")
        print(f"  {'-'*12} {'-'*7} {'-'*9} {'-'*9} {'-'*7}")
        for _, r in gap_break.iterrows():
            print(f"  {str(r['gap_bucket']):<12} {int(r['n']):>7,} "
                  f"{r['ev_open']:>+8.3f}% "
                  f"{r['median_open']:>+8.3f}% "
                  f"{r['win_rate']:>6.1f}%")


# ── メイン ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="指値取り逃し分析")
    p.add_argument("--limit-pct",    type=float, default=-0.7,
                   help="指値のオフセット%%（既定: -0.7 = 終値×0.993）")
    p.add_argument("--sl-pct",       type=float, default=-5.0)
    p.add_argument("--tp-pct",       type=float, default=7.5)
    p.add_argument("--holding-days", type=int,   default=3)
    p.add_argument("--max-gap-pct",  type=float, default=1.0,
                   help="C戦略の許容ギャップアップ%%（既定: +1.0%%）")
    p.add_argument("--cost-pct",     type=float, default=0.20)
    p.add_argument("--min-score",    type=float, default=60.0,
                   help="Stage1 スコア下限（既定: 60 = 運用デフォルト）")
    p.add_argument("--period", choices=["train", "val", "all"], default="all")
    p.add_argument("--no-filter", action="store_true",
                   help="銘柄基本フィルタを無効化（ETF/低出来高を含める）")
    return p.parse_args()


def main():
    args = parse_args()

    all_data = load_all_data()
    if all_data.empty:
        print("[limit_fill] データがありません。run_backtest.py --download を先に実行。")
        return

    # 銘柄フィルタ
    if not args.no_filter:
        latest = all_data["Date"].max()
        valid_codes = set(
            apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique()
        )
        all_data = all_data[all_data["Code"].isin(valid_codes)].copy()
        print(f"[limit_fill] フィルタ後: {all_data['Code'].nunique()}銘柄")

    # シグナル計算
    df = calc_all_signals(all_data)

    # 翌日バー / 将来バーを付与
    df = add_next_bars(df)
    df = add_future_bars(df, days=args.holding_days)

    # 指値約定列
    df = add_limit_fill_columns(df, args.limit_pct)

    # Stage1 通過のみに絞る
    df = df[df["stage1_score"] >= args.min_score].copy()

    # 期間絞り込み
    if args.period == "train":
        df = df[(df["Date"] >= pd.Timestamp(TRAIN_START)) &
                (df["Date"] <= pd.Timestamp(TRAIN_END))]
    elif args.period == "val":
        df = df[(df["Date"] >= pd.Timestamp(VAL_START)) &
                (df["Date"] <= pd.Timestamp(VAL_END))]
    df = df[df["next_open"].notna()].copy()

    if df.empty:
        print("[limit_fill] 対象シグナルが 0 件です。--min-score を下げる等を検討。")
        return

    # シナリオ別リターン計算
    df = build_scenarios(
        df,
        sl_pct=args.sl_pct, tp_pct=args.tp_pct,
        days=args.holding_days, cost_pct=args.cost_pct,
        max_gap_pct=args.max_gap_pct,
    )

    summary    = summarize(df, args.max_gap_pct)
    fill_break = fill_breakdown(df)
    gap_break  = gap_bucket_breakdown(df)

    print_report(df, summary, fill_break, gap_break, args)

    # CSV 出力
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_cols = [
        "Code", "Date", "Close", "stage1_score",
        "limit_price", "next_open", "next_high", "next_low", "next_close",
        "gap_pct", "fill_status", "limit_entry",
        "ret_limit", "ret_open", "ret_cond_open", "ret_split",
        "outcome_limit", "outcome_open", "outcome_cond_open",
    ]
    save_cols = [c for c in save_cols if c in df.columns]
    signals_csv = DATA_DIR / "limit_fill_signals.csv"
    df[save_cols].to_csv(signals_csv, index=False, encoding="utf-8-sig")
    print(f"\nシグナル詳細: {signals_csv}  ({len(df):,}行)")

    summary_csv = DATA_DIR / "limit_fill_summary.csv"
    summary.to_csv(summary_csv, index=False, encoding="utf-8-sig")
    print(f"シナリオ比較: {summary_csv}")


if __name__ == "__main__":
    main()

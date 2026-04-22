#!/usr/bin/env python3
"""
backtest/check_recent_signals.py

直近N日間でStage1シグナル（RSI5<20 + 出来高1.5倍 等）に達した銘柄を確認する。
バックテスト済みparquetデータから算出するため、APIキー不要。

【使用方法】
  cd ~/AI_stock_research
  source venv/bin/activate
  python3 backtest/check_recent_signals.py
  python3 backtest/check_recent_signals.py --days 60  # 直近60日
"""

import sys
import argparse
from pathlib import Path

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

from run_backtest import load_all_data, calc_all_signals, apply_basic_filter, DATA_DIR

WATCHLIST_CODES = {
    "7203","7267","7270","6758","6501","6752","6503","9984","9433","9432",
    "4502","4519","4568","4523","4543","7733","8306","8316","8411","8766",
    "8604","6861","6857","6920","6971","6762","6594","6645","6702","6098",
    "2413","7974","7832","6367","4063","4901","8801","9020","9022","5401",
    "5020","4911","4452","9983","3382","1925","7011","5108","2802","7751",
    "9201","9735",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="直近何日分を確認するか（デフォルト: 30）")
    parser.add_argument("--rsi5", type=float, default=20.0, help="RSI5閾値（デフォルト: 20）")
    parser.add_argument("--vol", type=float, default=1.5, help="出来高比率閾値（デフォルト: 1.5）")
    parser.add_argument("--watchlist-only", action="store_true", help="ウォッチリスト銘柄のみ")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  直近シグナル確認（過去{args.days}日間）")
    print(f"  条件: RSI5 < {args.rsi5}  出来高比率 >= {args.vol}")
    if args.watchlist_only:
        print("  対象: ウォッチリスト銘柄のみ（{len(WATCHLIST_CODES)}銘柄）")
    print(f"{'='*60}\n")

    # データ読み込み
    print("[check] データ読み込み中...")
    df = load_all_data()
    if df is None or df.empty:
        print("[check] データなし。先に run_backtest.py --download を実行してください。")
        sys.exit(1)

    print(f"[check] 読み込み完了: {len(df):,}行")

    # フィルタ適用
    df = apply_basic_filter(df)

    # シグナル計算
    print("[check] テクニカルシグナル計算中...")
    df = calc_all_signals(df)

    # 直近N日に絞る
    df["Date"] = pd.to_datetime(df["Date"])
    cutoff = df["Date"].max() - pd.Timedelta(days=args.days)
    recent = df[df["Date"] >= cutoff].copy()
    print(f"[check] 直近{args.days}日: {recent['Date'].min().date()} 〜 {recent['Date'].max().date()}")
    print(f"[check] 対象行数: {len(recent):,}行")

    # ウォッチリストフィルタ
    if args.watchlist_only:
        recent = recent[recent["Code"].astype(str).str[:4].isin(WATCHLIST_CODES)]
        print(f"[check] ウォッチリストフィルタ後: {len(recent):,}行")

    # ─── シグナル別集計 ─────────────────────────────────────────────────────

    # 最優秀複合シグナル: RSI5 < 20 + 出来高1.5倍
    sig_combo = recent[(recent["rsi5"] < args.rsi5) & (recent["vol_ratio"] >= args.vol)].copy()

    # RSI5 単独
    sig_rsi5 = recent[recent["rsi5"] < args.rsi5].copy()

    # DVS > 20
    sig_dvs = recent[recent["dvs"] > 20].copy()

    # RSI5 < 10（極端売られすぎ）
    sig_rsi5_extreme = recent[recent["rsi5"] < 10].copy()

    print(f"\n{'─'*60}")
    print(f"  ★ 最優秀: RSI5<{args.rsi5} + 出来高{args.vol}倍以上: {len(sig_combo)}件")
    print(f"  RSI5<{args.rsi5}（単独）: {len(sig_rsi5)}件")
    print(f"  RSI5<10（極端売られすぎ）: {len(sig_rsi5_extreme)}件")
    print(f"  DVS>20（強い買い越し）: {len(sig_dvs)}件")
    print(f"{'─'*60}\n")

    def _show_hits(hits: pd.DataFrame, label: str, top_n: int = 20):
        if hits.empty:
            print(f"  [{label}] 該当なし")
            return

        # 最新日の重複除去（同銘柄の最近のシグナルを取る）
        hits = hits.sort_values("Date", ascending=False).drop_duplicates("Code")

        print(f"\n{'='*60}")
        print(f"  {label}  （直近{min(top_n, len(hits))}件）")
        print(f"{'='*60}")
        print(f"  {'日付':10}  {'コード':6}  {'RSI5':6}  {'出来高比':7}  {'DVS':6}  {'52週':6}  {'Close':>8}")
        print(f"  {'-'*10}  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*8}")

        for _, row in hits.head(top_n).iterrows():
            code = str(row.get("Code", ""))[:4]
            date_str = pd.Timestamp(row["Date"]).strftime("%Y-%m-%d")
            rsi5_val = row.get("rsi5", float("nan"))
            vol = row.get("vol_ratio", float("nan"))
            dvs = row.get("dvs", float("nan"))
            w52 = row.get("w52_pos", float("nan"))
            close = row.get("Close", row.get("AdjC", float("nan")))

            wl_mark = "★" if code in WATCHLIST_CODES else " "
            print(f"  {date_str}  {wl_mark}{code:5}  {rsi5_val:6.1f}  {vol:7.2f}x  {dvs:6.1f}  {w52:5.1f}%  {close:>8.0f}")

    _show_hits(sig_combo, f"RSI5<{args.rsi5} + 出来高{args.vol}倍（最優秀複合シグナル）")
    _show_hits(sig_rsi5_extreme, "RSI5<10（極端売られすぎ）")
    _show_hits(sig_dvs, "DVS>20（強い買い越し出来高）")

    # ウォッチリスト銘柄の状況サマリ
    print(f"\n{'='*60}")
    print("  ウォッチリスト50銘柄の最新シグナル状況")
    print(f"{'='*60}")

    wl_latest = (
        df[df["Code"].astype(str).str[:4].isin(WATCHLIST_CODES)]
        .sort_values("Date")
        .groupby("Code")
        .tail(1)
        .copy()
    )

    if wl_latest.empty:
        print("  データなし")
    else:
        wl_latest["code4"] = wl_latest["Code"].astype(str).str[:4]
        wl_latest = wl_latest.sort_values("rsi5", ascending=True)
        print(f"\n  RSI5が低い順（トップ15）:")
        print(f"  {'コード':6}  {'RSI5':6}  {'出来高比':7}  {'DVS':6}  {'52週':6}  {'Close':>8}  {'シグナル'}")
        print(f"  {'-'*6}  {'-'*6}  {'-'*7}  {'-'*6}  {'-'*6}  {'-'*8}  {'-'*20}")
        for _, row in wl_latest.head(15).iterrows():
            rsi5_val = row.get("rsi5", float("nan"))
            vol = row.get("vol_ratio", float("nan"))
            dvs = row.get("dvs", float("nan"))
            w52 = row.get("w52_pos", float("nan"))
            close = row.get("Close", row.get("AdjC", float("nan")))
            code = str(row.get("Code", ""))[:4]

            signals = []
            if rsi5_val < 10:   signals.append("RSI5<10★")
            elif rsi5_val < 20: signals.append("RSI5<20★")
            elif rsi5_val < 30: signals.append("RSI5<30")
            if vol >= 1.5:      signals.append(f"出来高{vol:.1f}x")
            if dvs > 20:        signals.append("DVS強")
            sig_str = " ".join(signals) if signals else "-"

            print(f"  {code:6}  {rsi5_val:6.1f}  {vol:7.2f}x  {dvs:6.1f}  {w52:5.1f}%  {close:>8.0f}  {sig_str}")

    # データの最新日付
    latest_date = df["Date"].max()
    print(f"\n  ※ データ最終日: {latest_date.date()}")
    print(f"  ★ = ウォッチリスト銘柄")
    print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
backtest/equity_curve_simulation.py

¥100 万円スタート × 単元株（既定 100 株） × IFDOCO 自動執行 × 3 営業日強制決済
を忠実に日次シミュレートし、エクイティカーブを生成する。

【目的】
  実運用ルール（手動 IFDOCO + 3 日強制決済 + 単元株縛り）と同じ条件で、
  バックテスト期間にこの戦略をどれだけ運用できたかを「円ベース」で検証する。

【使用方法】
  cd /home/user/AI_stock_research
  JQUANTS_API_KEY=<key> python backtest/equity_curve_simulation.py [オプション]

  オプション:
    --initial-capital N      初期資金（円）。既定 1,000,000
    --max-concurrent N       同時保有上限（既定 3）
    --max-per-sector N       同一セクター上限（既定 1）
    --shares-per-position N  1 ポジションの株数（単元株、既定 100）
    --sl-pct PCT             損切り%（既定 -5.0）
    --tp-pct PCT             利確%（既定 +7.5）
    --max-days N             強制決済までの営業日数（既定 3）
    --commission PCT         往復取引コスト%（既定 0.20%）
    --signal-filter NAME     使用するシグナルフィルタ。既定: 'breakout+rsi5_low'
                             利用可能: see SIGNAL_FILTERS の keys
    --start YYYY-MM-DD       開始日（既定: BACKTEST_START）
    --end YYYY-MM-DD         終了日（既定: BACKTEST_END）

【出力】
  data/backtest/equity_curve.csv  -- 日次エクイティ
  data/backtest/trades_log.csv    -- 個別トレード履歴
  コンソール: 最終 P/L、Sharpe、最大 DD、勝率 などサマリー

【前提】
  data/backtest/daily_*.parquet がダウンロード済みであること。
  なければ先に run_backtest.py --download を実行（または別ブランチで取得した
  2 年超のデータを data/backtest/ に配置）。
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

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

# ── シグナルフィルタ（multi_day_backtest と同種） ─────────────────────────────

SIGNAL_FILTERS = {
    "all":                lambda df: pd.Series(True, index=df.index),
    "rsi5_lt_30":         lambda df: df["rsi5"] < 30,
    "rsi5_lt_20":         lambda df: df["rsi5"] < 20,
    "breakout":           lambda df: df["breakout_5d"].astype(bool),
    "breakout+dvs_pos":   lambda df: df["breakout_5d"].astype(bool) & (df["dvs"] > 0),
    "breakout+rsi5_low":  lambda df: df["breakout_5d"].astype(bool) & (df["rsi5"] < 30),
    "rsi5_lt_20+vol_15":  lambda df: (df["rsi5"] < 20) & (df["vol_ratio"] >= 1.5),
    "score_ge_60":        lambda df: df["stage1_score"] >= 60,
}


def _load_master_for_sectors() -> dict:
    """master キャッシュからセクター辞書を返す（無ければ空）。"""
    cache_path = PROJECT_ROOT / "data" / "master_cache.json"
    if not cache_path.exists():
        return {}
    try:
        import json
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_sector(code: str, master: dict) -> str:
    code4 = str(code)[:4]
    return (master.get(code4, {}) or {}).get("sector33", "")


# ── シミュレーション本体 ──────────────────────────────────────────────────────

class Position:
    __slots__ = ("code", "sector", "entry_date", "entry_price", "shares",
                 "tp_price", "sl_price", "exit_deadline_idx")

    def __init__(self, code, sector, entry_date, entry_price, shares,
                 tp_price, sl_price, exit_deadline_idx):
        self.code = code
        self.sector = sector
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.shares = shares
        self.tp_price = tp_price
        self.sl_price = sl_price
        self.exit_deadline_idx = exit_deadline_idx

    @property
    def cost(self) -> float:
        return self.entry_price * self.shares


def simulate(
    df: pd.DataFrame,
    signal_filter,
    initial_capital: float = 1_000_000.0,
    max_concurrent: int = 3,
    max_per_sector: int = 1,
    shares_per_position: int = 100,
    sl_pct: float = -5.0,
    tp_pct: float = +7.5,
    max_days: int = 3,
    cost_pct: float = ROUND_TRIP_COST_PCT,
    master: dict | None = None,
) -> dict:
    """
    日次シミュレーション。

    手順（各営業日 d）:
      1. 保有ポジションの当日 OHLC で TP/SL/期限到達を判定し決済
      2. 残金・空きポジ枠で当日のシグナルから新規エントリー候補を選定
         - stage1_score 降順
         - max_concurrent 内
         - max_per_sector 内
         - 株価 × shares_per_position が残金以下
      3. 新規はその日の翌日始値（next_open）でエントリー
      4. 当日終値ベースの time-mark equity を記録

    Returns:
        {"equity_curve": DataFrame, "trades": DataFrame, "stats": dict}
    """
    master = master or {}

    # 全シグナル日を時系列でソート
    df = df.sort_values(["Date", "Code"]).copy()
    df["sector"] = df["Code"].astype(str).str[:4].apply(
        lambda c: _get_sector(c, master)
    )

    all_dates = sorted(df["Date"].unique())
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    cash = initial_capital
    open_positions: list[Position] = []
    trade_log: list[dict] = []
    equity_records: list[dict] = []

    # 銘柄×日付のクイックルックアップ（OHLC）
    df_indexed = df.set_index(["Code", "Date"])

    for d_idx, d in enumerate(all_dates):
        # ── 1. 保有ポジの決済判定 ─────────────────────────────────
        still_open: list[Position] = []
        for pos in open_positions:
            try:
                row = df_indexed.loc[(pos.code, d)]
            except KeyError:
                # 当日データなし: skip 保有継続
                still_open.append(pos)
                continue

            # row は Series（単一行）or DataFrame（重複行）
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]

            high = float(row.get("High", 0))
            low = float(row.get("Low", 0))
            close = float(row.get("Close", 0))
            o = float(row.get("Open", close))

            exit_price = None
            exit_reason = None

            # SL 優先（保守的: 同日両ヒット時 SL）
            if low <= pos.sl_price:
                exit_price = pos.sl_price
                exit_reason = "SL"
            elif high >= pos.tp_price:
                exit_price = pos.tp_price
                exit_reason = "TP"
            elif d_idx >= pos.exit_deadline_idx:
                # 強制決済日: 当日始値
                exit_price = o
                exit_reason = "TIMEOUT"

            if exit_price is not None:
                gross_pnl = (exit_price - pos.entry_price) * pos.shares
                cost_jpy = pos.cost * (cost_pct / 100.0)
                net_pnl = gross_pnl - cost_jpy
                cash += pos.cost + net_pnl
                trade_log.append({
                    "code": pos.code,
                    "sector": pos.sector,
                    "entry_date": pos.entry_date,
                    "entry_price": round(pos.entry_price, 2),
                    "exit_date": d,
                    "exit_price": round(exit_price, 2),
                    "shares": pos.shares,
                    "gross_pnl": round(gross_pnl, 0),
                    "cost_jpy": round(cost_jpy, 0),
                    "net_pnl": round(net_pnl, 0),
                    "return_pct": round((exit_price - pos.entry_price) / pos.entry_price * 100, 2),
                    "exit_reason": exit_reason,
                    "hold_days": d_idx - date_to_idx.get(pos.entry_date, d_idx),
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        # ── 2. 当日シグナルから新規エントリー候補 ─────────────────
        today_df = df[df["Date"] == d]
        if today_df.empty:
            # equity マークだけ
            equity_records.append({
                "date": d,
                "cash": cash,
                "positions_value": _mark_to_market(open_positions, df_indexed, d),
                "n_positions": len(open_positions),
            })
            continue

        # 当日のシグナル + 翌日始値あり + フィルタ通過
        candidates = today_df[
            today_df["stage1_score"].fillna(0) > 0
        ].copy()
        if candidates.empty:
            equity_records.append({
                "date": d,
                "cash": cash,
                "positions_value": _mark_to_market(open_positions, df_indexed, d),
                "n_positions": len(open_positions),
            })
            continue

        try:
            mask = signal_filter(candidates)
            candidates = candidates[mask]
        except Exception:
            candidates = pd.DataFrame()

        # 翌日始値を取る（n+1 日目の Open）
        # df_indexed から当日 +1 営業日の始値を取る
        if d_idx + 1 < len(all_dates):
            next_d = all_dates[d_idx + 1]
            next_opens = df[df["Date"] == next_d].set_index("Code")["Open"].to_dict()
        else:
            next_opens = {}

        candidates = candidates.copy()
        candidates["next_open"] = candidates["Code"].map(next_opens)
        candidates = candidates.dropna(subset=["next_open"])
        candidates = candidates.sort_values("stage1_score", ascending=False)

        # 既保有銘柄・セクターを除外、ポジ枠と残金で絞る
        held_codes = {p.code for p in open_positions}
        sector_count: dict[str, int] = {}
        for p in open_positions:
            if p.sector:
                sector_count[p.sector] = sector_count.get(p.sector, 0) + 1

        for _, row in candidates.iterrows():
            if len(open_positions) >= max_concurrent:
                break
            code = row["Code"]
            if code in held_codes:
                continue
            sector = row.get("sector", "") or ""
            if sector and sector_count.get(sector, 0) >= max_per_sector:
                continue
            entry_price = float(row["next_open"])
            cost = entry_price * shares_per_position
            if cash < cost:
                continue

            pos = Position(
                code=code,
                sector=sector,
                entry_date=all_dates[d_idx + 1] if d_idx + 1 < len(all_dates) else d,
                entry_price=entry_price,
                shares=shares_per_position,
                tp_price=entry_price * (1 + tp_pct / 100.0),
                sl_price=entry_price * (1 + sl_pct / 100.0),
                exit_deadline_idx=min(d_idx + max_days, len(all_dates) - 1),
            )
            cash -= cost
            open_positions.append(pos)
            held_codes.add(code)
            if sector:
                sector_count[sector] = sector_count.get(sector, 0) + 1

        equity_records.append({
            "date": d,
            "cash": cash,
            "positions_value": _mark_to_market(open_positions, df_indexed, d),
            "n_positions": len(open_positions),
        })

    # 残ポジを最終日の Close で清算
    if open_positions and all_dates:
        final_d = all_dates[-1]
        for pos in open_positions:
            try:
                row = df_indexed.loc[(pos.code, final_d)]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]
                exit_price = float(row.get("Close", pos.entry_price))
            except KeyError:
                exit_price = pos.entry_price
            gross_pnl = (exit_price - pos.entry_price) * pos.shares
            cost_jpy = pos.cost * (cost_pct / 100.0)
            net_pnl = gross_pnl - cost_jpy
            cash += pos.cost + net_pnl
            trade_log.append({
                "code": pos.code,
                "sector": pos.sector,
                "entry_date": pos.entry_date,
                "entry_price": round(pos.entry_price, 2),
                "exit_date": final_d,
                "exit_price": round(exit_price, 2),
                "shares": pos.shares,
                "gross_pnl": round(gross_pnl, 0),
                "cost_jpy": round(cost_jpy, 0),
                "net_pnl": round(net_pnl, 0),
                "return_pct": round((exit_price - pos.entry_price) / pos.entry_price * 100, 2),
                "exit_reason": "FORCED_END",
                "hold_days": -1,
            })

    equity_df = pd.DataFrame(equity_records)
    if not equity_df.empty:
        equity_df["equity"] = equity_df["cash"] + equity_df["positions_value"]
    trades_df = pd.DataFrame(trade_log)

    stats = _compute_stats(equity_df, trades_df, initial_capital)
    return {"equity_curve": equity_df, "trades": trades_df, "stats": stats}


def _mark_to_market(positions: list, df_indexed: pd.DataFrame, d) -> float:
    total = 0.0
    for pos in positions:
        try:
            row = df_indexed.loc[(pos.code, d)]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            close = float(row.get("Close", pos.entry_price))
        except KeyError:
            close = pos.entry_price
        total += close * pos.shares
    return total


def _compute_stats(equity_df: pd.DataFrame, trades_df: pd.DataFrame, initial: float) -> dict:
    if equity_df.empty:
        return {}
    final_equity = float(equity_df["equity"].iloc[-1])
    total_return_pct = (final_equity - initial) / initial * 100

    # Max drawdown
    running_max = equity_df["equity"].cummax()
    drawdown = (equity_df["equity"] - running_max) / running_max * 100
    max_dd = float(drawdown.min())

    # Daily returns → Sharpe
    daily_ret = equity_df["equity"].pct_change().fillna(0)
    sharpe = (daily_ret.mean() / daily_ret.std() * (252 ** 0.5)) if daily_ret.std() > 0 else 0.0

    # 勝敗統計
    if not trades_df.empty:
        n_trades = len(trades_df)
        wins = (trades_df["net_pnl"] > 0).sum()
        losses = (trades_df["net_pnl"] < 0).sum()
        win_rate = wins / n_trades * 100 if n_trades else 0.0
        avg_win = trades_df.loc[trades_df["net_pnl"] > 0, "net_pnl"].mean() if wins else 0.0
        avg_loss = trades_df.loc[trades_df["net_pnl"] < 0, "net_pnl"].mean() if losses else 0.0
        profit_factor = (
            trades_df.loc[trades_df["net_pnl"] > 0, "net_pnl"].sum() /
            abs(trades_df.loc[trades_df["net_pnl"] < 0, "net_pnl"].sum())
            if losses else float("inf")
        )
        avg_hold = trades_df.loc[trades_df["hold_days"] >= 0, "hold_days"].mean() if n_trades else 0
    else:
        n_trades = 0
        wins = losses = 0
        win_rate = avg_win = avg_loss = profit_factor = avg_hold = 0.0

    return {
        "initial_capital": initial,
        "final_equity": round(final_equity, 0),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_annual": round(sharpe, 2),
        "n_trades": n_trades,
        "wins": int(wins),
        "losses": int(losses),
        "win_rate_pct": round(win_rate, 1),
        "avg_win_jpy": round(float(avg_win), 0) if wins else 0,
        "avg_loss_jpy": round(float(avg_loss), 0) if losses else 0,
        "profit_factor": round(float(profit_factor), 2) if losses else None,
        "avg_hold_days": round(float(avg_hold), 1),
    }


# ── メイン ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="エクイティカーブシミュレーション")
    parser.add_argument("--initial-capital", type=float, default=1_000_000)
    parser.add_argument("--max-concurrent", type=int, default=3)
    parser.add_argument("--max-per-sector", type=int, default=1)
    parser.add_argument("--shares-per-position", type=int, default=100)
    parser.add_argument("--sl-pct", type=float, default=-5.0)
    parser.add_argument("--tp-pct", type=float, default=+7.5)
    parser.add_argument("--max-days", type=int, default=3)
    parser.add_argument("--commission", type=float, default=ROUND_TRIP_COST_PCT,
                        help=f"往復取引コスト%% (既定: {ROUND_TRIP_COST_PCT:.2f}%%)")
    parser.add_argument("--signal-filter", default="breakout+rsi5_low",
                        choices=list(SIGNAL_FILTERS.keys()))
    parser.add_argument("--start", default=BACKTEST_START)
    parser.add_argument("--end", default=BACKTEST_END)
    args = parser.parse_args()

    print("=" * 72)
    print("エクイティカーブシミュレーション")
    print(f"  初期資金: ¥{args.initial_capital:,.0f}")
    print(f"  期間: {args.start} 〜 {args.end}")
    print(f"  シグナル: {args.signal_filter}")
    print(f"  単元株: {args.shares_per_position}  同時保有: {args.max_concurrent}  "
          f"同一セクター: {args.max_per_sector}")
    print(f"  TP +{args.tp_pct}%  SL {args.sl_pct}%  強制決済: {args.max_days}日")
    print(f"  往復コスト: {args.commission:.2f}%")
    print("=" * 72)

    # データ読み込み
    all_data = load_all_data()
    if all_data.empty:
        print("データなし。先に run_backtest.py --download を実行。")
        return

    start_dt = pd.Timestamp(args.start)
    end_dt = pd.Timestamp(args.end)
    warmup_start = start_dt - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
    df_full = all_data[all_data["Date"] >= warmup_start].copy()

    # 基本フィルタ
    latest_date = df_full["Date"].max()
    valid_codes = set(
        apply_basic_filter(df_full[df_full["Date"] == latest_date])["Code"].unique()
    )
    df_full = df_full[df_full["Code"].isin(valid_codes)].copy()
    print(f"フィルタ後: {df_full['Code'].nunique()}銘柄")

    # シグナル計算
    df_signals = calc_all_signals(df_full)

    # シミュレーション期間に絞る
    df_test = df_signals[
        (df_signals["Date"] >= start_dt) &
        (df_signals["Date"] <= end_dt)
    ].copy()
    print(f"シミュレーション期間: {df_test['Date'].nunique()}日 × {df_test['Code'].nunique()}銘柄")

    # マスターキャッシュからセクター情報を取得
    master = _load_master_for_sectors()
    if master:
        print(f"master セクター辞書: {len(master)}件")

    signal_filter = SIGNAL_FILTERS[args.signal_filter]

    print("\nシミュレーション実行中...")
    result = simulate(
        df_test,
        signal_filter=signal_filter,
        initial_capital=args.initial_capital,
        max_concurrent=args.max_concurrent,
        max_per_sector=args.max_per_sector,
        shares_per_position=args.shares_per_position,
        sl_pct=args.sl_pct,
        tp_pct=args.tp_pct,
        max_days=args.max_days,
        cost_pct=args.commission,
        master=master,
    )

    stats = result["stats"]
    equity_df = result["equity_curve"]
    trades_df = result["trades"]

    # ── サマリー表示 ───────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("シミュレーション結果")
    print("=" * 72)
    print(f"初期資金       : ¥{stats['initial_capital']:>14,.0f}")
    print(f"最終資産       : ¥{stats['final_equity']:>14,.0f}")
    print(f"総リターン     : {stats['total_return_pct']:>+14.2f}%")
    print(f"年率シャープ   : {stats['sharpe_annual']:>14.2f}")
    print(f"最大ドローダウン: {stats['max_drawdown_pct']:>+14.2f}%")
    print(f"")
    print(f"トレード数     : {stats['n_trades']:>14,}")
    print(f"勝ち / 負け    : {stats['wins']:>6} / {stats['losses']:>5}")
    print(f"勝率           : {stats['win_rate_pct']:>14.1f}%")
    print(f"平均勝ち       : ¥{stats['avg_win_jpy']:>+14,.0f}")
    print(f"平均負け       : ¥{stats['avg_loss_jpy']:>+14,.0f}")
    if stats.get("profit_factor"):
        print(f"プロフィットファクター: {stats['profit_factor']:>9.2f}")
    print(f"平均保有日数   : {stats['avg_hold_days']:>14.1f}日")

    # ── 出口理由の内訳 ───────────────────────────────────────────────
    if not trades_df.empty:
        print("\n出口理由の内訳:")
        reason_stats = trades_df.groupby("exit_reason").agg(
            n=("net_pnl", "size"),
            avg_pnl=("net_pnl", "mean"),
            total_pnl=("net_pnl", "sum"),
        ).round(0)
        print(reason_stats.to_string())

    # ── 出力ファイル ───────────────────────────────────────────────
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    eq_path = DATA_DIR / "equity_curve.csv"
    tr_path = DATA_DIR / "trades_log.csv"
    if not equity_df.empty:
        equity_df.to_csv(eq_path, index=False, encoding="utf-8-sig")
        print(f"\nエクイティカーブ: {eq_path}  ({len(equity_df)}行)")
    if not trades_df.empty:
        trades_df.to_csv(tr_path, index=False, encoding="utf-8-sig")
        print(f"トレードログ    : {tr_path}  ({len(trades_df)}行)")


if __name__ == "__main__":
    main()

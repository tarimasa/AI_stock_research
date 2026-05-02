#!/usr/bin/env python3
"""
backtest/run_backtest.py

JQuantsの全銘柄データを使ったバックテスト実行スクリプト。
学習期間（TRAIN_START〜TRAIN_END）で閾値を設計し、
検証期間（VAL_START〜VAL_END）で汎化性能を検証する。

【概要】
  1. JQuants APIから全銘柄の日足OHLCVをダウンロード（初回のみ）
  2. 既存のStage1スクリーナーロジックで各日のシグナルを計算
  3. 翌営業日の始値エントリー、損切-2% / 利確+3%でのトレード結果を算出
  4. シグナル別の勝率・期待値を学習/検証に分けて集計してCSV出力

【使用方法】
  cd /home/user/AI_stock_research
  JQUANTS_API_KEY=<key> python backtest/run_backtest.py [オプション]

  オプション:
    --download   JQuantsからデータをダウンロードする（初回のみ必要）
    --force      既存データを上書きダウンロードする
    --analyze    バックテストを実行して分析する
    --no-filter  銘柄フィルタを適用しない（全コードを対象にする）

【出力ファイル】
  data/backtest/daily_YYYYMM.parquet  -- 月別OHLCVデータ
  data/backtest/results.csv           -- Stage1通過銘柄の学習期間シグナル+結果
  data/backtest/threshold_summary.csv -- 閾値別集計統計

【注意】
  JQuantsフリープランは日次データに約12週の遅延があります。
  2026/4/21時点では、2026/1/26以前のデータが取得可能です。
  それ以降のデータが必要な場合はLightプラン以上が必要です。
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# src/ のモジュールを参照できるようにパスを追加
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# ── 設定 ──────────────────────────────────────────────────────────────────────
TRAIN_START = os.environ.get("TRAIN_START", "2021-05-01")
TRAIN_END   = os.environ.get("TRAIN_END",   "2025-09-30")
VAL_START   = os.environ.get("VAL_START",   "2025-10-01")
VAL_END     = os.environ.get("VAL_END",     "2026-04-30")
BACKTEST_START = TRAIN_START   # 後方互換 (multi_day_backtest imports this)
BACKTEST_END   = VAL_END
# ウォームアップ用のカレンダー日数（RSI-14/SMA-25/52週高安値等の計算に必要）
WARMUP_CALENDAR_DAYS = 180  # 約9ヶ月（52週ポジション計算のため余裕を持って確保）

SL_PCT = -2.0  # 損切りライン
TP_PCT = +3.0  # 利確ライン

DATA_DIR = PROJECT_ROOT / "backtest" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── JQuants クライアント ──────────────────────────────────────────────────────

def _get_jquants_client():
    """JQuants V2クライアントを返す。APIキー未設定時はNone。"""
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        print("[backtest] JQUANTS_API_KEY が未設定です。")
        return None
    try:
        import jquantsapi
        return jquantsapi.ClientV2(api_key=api_key)
    except ImportError:
        print("[backtest] jquantsapi が未インストールです: pip install jquants-api-client")
        return None
    except Exception as e:
        print(f"[backtest] JQuantsクライアント初期化失敗: {e}")
        return None


# ── 営業日カレンダー ──────────────────────────────────────────────────────────

def get_trading_days(start: str, end: str, client=None) -> list[str]:
    """
    指定期間の営業日リストを返す（YYYY-MM-DD形式）。
    JQuantsカレンダーAPIを優先し、失敗時は土日除外で代替。
    """
    if client:
        try:
            cal_df = client.get_mkt_calendar(
                from_yyyymmdd=start.replace("-", ""),
                to_yyyymmdd=end.replace("-", ""),
            )
            if cal_df is not None and not cal_df.empty:
                date_col = next((c for c in cal_df.columns if "date" in c.lower()), None)
                holiday_col = next(
                    (c for c in cal_df.columns if "holiday" in c.lower() or "Holiday" in c), None
                )
                if date_col:
                    if holiday_col:
                        cal_df = cal_df[cal_df[holiday_col].astype(str) == "0"]
                    raw = cal_df[date_col].astype(str).tolist()
                    result = []
                    for d in raw:
                        d = d.strip()
                        if len(d) == 8 and d.isdigit():
                            result.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
                        elif len(d) >= 10:
                            result.append(d[:10])
                    return sorted(result)
        except Exception as e:
            print(f"[backtest] カレンダーAPI失敗、土日除外で代替: {e}")

    # フォールバック: 土日除外（祝日は含む可能性があるが許容）
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt   = datetime.strptime(end, "%Y-%m-%d").date()
    days = []
    d = start_dt
    while d <= end_dt:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


# ── データダウンロード ─────────────────────────────────────────────────────────

_OHLCV_DEBUG_PRINTED = False  # 1回だけカラム名を表示するフラグ


def _normalize_ohlcv(df: pd.DataFrame, date_str: str) -> pd.DataFrame:
    """
    JQuantsのDataFrameをOHLCV標準形式に変換する。
    カラム名の大文字小文字・調整済み/未調整のバリエーションに対応。
    調整済み価格（AdjustmentClose等）を優先し、なければ通常価格を使用。
    """
    global _OHLCV_DEBUG_PRINTED
    df = df.copy()

    # 全カラム名を小文字でインデックス化（大文字小文字を吸収）
    col_lower_map: dict[str, str] = {c.lower(): c for c in df.columns}

    if not _OHLCV_DEBUG_PRINTED:
        print(f"[backtest] APIカラム名（初回確認）: {list(df.columns)[:15]}")
        _OHLCV_DEBUG_PRINTED = True

    # 優先順位付きカラム候補（調整済み > 通常 > 短縮形 > 小文字表記）
    # JQuants V2 Lightプランの短縮形: AdjO/AdjH/AdjL/AdjC, O/H/L/C/Vo
    field_candidates: dict[str, list[str]] = {
        "Open":   ["adjo", "adjustmentopen",   "open",   "o"],
        "High":   ["adjh", "adjustmenthigh",   "high",   "h"],
        "Low":    ["adjl", "adjustmentlow",    "low",    "l"],
        "Close":  ["adjc", "adjustmentclose",  "close",  "c"],
        "Volume": ["adjvo","adjustmentvolume", "volume", "vo"],
        "Code":   ["code"],
    }

    for dst, candidates in field_candidates.items():
        for src_lower in candidates:
            if src_lower in col_lower_map:
                df[dst] = df[col_lower_map[src_lower]]
                break

    # Code列を4桁に統一
    if "Code" in df.columns:
        df["Code"] = df["Code"].astype(str).str[:4]

    # Date列を付与
    df["Date"] = date_str

    keep = [c for c in ["Code", "Date", "Open", "High", "Low", "Close", "Volume"]
            if c in df.columns]
    df = df[keep].copy()

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Close/Openが取れなかった場合は空DataFrameを返す
    if "Close" not in df.columns or "Open" not in df.columns:
        return pd.DataFrame()

    return df.dropna(subset=["Close", "Open"])


def _save_monthly_chunk(month_str: str, frames: list) -> None:
    """月別データをParquetに保存する。"""
    combined = pd.concat(frames, ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"])
    output = DATA_DIR / f"daily_{month_str}.parquet"
    combined.to_parquet(output, index=False)
    print(f"  保存: {output.name} ({len(combined):,}行)")


def download_backtest_data(force: bool = False) -> None:
    """
    JQuantsから全銘柄OHLCVをダウンロードしてParquetに保存する。
    ウォームアップ期間（WARMUP_CALENDAR_DAYS日前）からBACKTEST_ENDまでを取得。

    すでに存在する月のファイルはスキップ（--force で上書き）。
    """
    client = _get_jquants_client()
    if client is None:
        print("[backtest] JQuants APIが使えないためスキップします。")
        return

    # データ取得範囲
    start_dt = datetime.strptime(BACKTEST_START, "%Y-%m-%d").date()
    data_start = start_dt - timedelta(days=WARMUP_CALENDAR_DAYS)
    data_start_str = data_start.strftime("%Y-%m-%d")

    print(f"[backtest] ダウンロード対象: {data_start_str} 〜 {BACKTEST_END}")

    # 既存ファイルの確認
    existing_months: set[str] = set()
    for f in DATA_DIR.glob("daily_*.parquet"):
        existing_months.add(f.stem.replace("daily_", ""))

    # 営業日リストを取得
    trading_days = get_trading_days(data_start_str, BACKTEST_END, client)
    print(f"[backtest] 対象営業日: {len(trading_days)}日")

    current_month: str | None = None
    current_frames: list = []
    total_fetched = 0

    for i, day_str in enumerate(trading_days, 1):
        month_str = day_str[:7].replace("-", "")

        if not force and month_str in existing_months:
            if month_str != current_month:
                if current_frames:
                    _save_monthly_chunk(current_month, current_frames)
                    current_frames = []
                print(f"  {month_str}: スキップ（既存データあり）")
                current_month = month_str
            continue

        if month_str != current_month:
            if current_frames:
                _save_monthly_chunk(current_month, current_frames)
                current_frames = []
            current_month = month_str
            print(f"  {month_str}: ダウンロード中...")

        try:
            date_yyyymmdd = day_str.replace("-", "")
            df = client.get_eq_bars_daily(date_yyyymmdd=date_yyyymmdd)
            if df is None or df.empty:
                print(f"    [スキップ] {day_str}: APIが空データを返しました（祝日の可能性）")
                continue
            normalized = _normalize_ohlcv(df, day_str)
            if not normalized.empty:
                current_frames.append(normalized)
                total_fetched += 1
            else:
                print(f"    [スキップ] {day_str}: 正規化後データなし（カラム不足の可能性）")
        except Exception as e:
            print(f"    [警告] {day_str} 取得失敗: {type(e).__name__}: {e}")

        if i % 50 == 0:
            print(f"  進捗: {i}/{len(trading_days)}日 ({total_fetched}日取得済み)")

        time.sleep(0.15)  # レートリミット対策

    # 最後の月を保存
    if current_frames:
        _save_monthly_chunk(current_month, current_frames)

    print(f"\n[backtest] ダウンロード完了: {total_fetched}日分")


# ── データ読み込み ─────────────────────────────────────────────────────────────

def load_all_data() -> pd.DataFrame:
    """data/backtest/daily_*.parquet を全て読み込んでDataFrameを返す。"""
    frames = []
    for f in sorted(DATA_DIR.glob("daily_*.parquet")):
        try:
            frames.append(pd.read_parquet(f))
        except Exception as e:
            print(f"[backtest] {f.name} 読み込みエラー: {e}")

    if not frames:
        print("[backtest] データなし。--download でダウンロードしてください。")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"])
    combined = combined.sort_values(["Code", "Date"]).reset_index(drop=True)

    print(
        f"[backtest] データ読み込み: {len(combined):,}行 | "
        f"{combined['Code'].nunique()}銘柄 | "
        f"{combined['Date'].min().date()} 〜 {combined['Date'].max().date()}"
    )
    return combined


# ── 銘柄フィルタ ──────────────────────────────────────────────────────────────

def _get_etf_reit_codes() -> set[int]:
    """ETF・REITの典型コードレンジを返す。完全網羅ではないが主要なものを除外。"""
    codes: set[int] = set()
    # ETF (1300-1699), インフラファンド (9281-9287), REIT (2971,3279,3451..., 8951-8989)
    for c in range(1300, 1700):
        codes.add(c)
    for c in range(8951, 8990):
        codes.add(c)
    for c in range(9281, 9288):
        codes.add(c)
    return codes


_ETF_REIT_CODES = _get_etf_reit_codes()


def apply_basic_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    短期トレード非対象銘柄を除外する。
    - 出来高50,000株未満
    - 株価 100円未満 or 50,000円超
    - 売買代金 500万円未満
    - 4桁数字以外のコード（ETF・外国株等）
    - ETF/REITコードレンジ
    """
    code_int = pd.to_numeric(df["Code"], errors="coerce")
    mask = (
        df["Code"].str.match(r"^\d{4}$", na=False) &
        (df["Volume"] >= 50_000) &
        (df["Close"] >= 100) &
        (df["Close"] <= 50_000) &
        (df["Close"] * df["Volume"] >= 5_000_000) &
        ~code_int.isin(_ETF_REIT_CODES)
    )
    return df[mask].copy()


# ── テクニカルシグナル計算（ベクトル化） ─────────────────────────────────────

def calc_all_signals(bulk_df: pd.DataFrame) -> pd.DataFrame:
    """
    全銘柄・全日付のテクニカルシグナルをローリング計算する。
    各日のシグナルは当日以前のデータのみを使用（ルックアヘッドバイアスなし）。

    計算項目:
      rsi5, rsi14, sma25, ma25_diff_pct, vol_ratio,
      breakout_5d, w52_pos, dvs, stage1_score
    """
    print(f"[backtest] テクニカルシグナル計算中（{len(bulk_df):,}行）...")
    df = bulk_df.sort_values(["Code", "Date"]).copy()

    def _rsi(series: pd.Series, n: int) -> pd.Series:
        delta = series.diff()
        gain = delta.where(delta > 0, 0.0).rolling(n, min_periods=n).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(n, min_periods=n).mean()
        rs = gain / loss.replace(0, float("nan"))
        return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)

    g = df.groupby("Code", group_keys=False)

    df["rsi5"]       = g["Close"].transform(lambda x: _rsi(x, 5))
    df["rsi14"]      = g["Close"].transform(lambda x: _rsi(x, 14))
    df["sma25"]      = g["Close"].transform(lambda x: x.rolling(25, min_periods=25).mean())
    df["avg_vol_20"] = g["Volume"].transform(lambda x: x.rolling(20, min_periods=5).mean())

    # MA25乖離率
    df["ma25_diff_pct"] = (
        (df["Close"] - df["sma25"]) / df["sma25"].replace(0, float("nan")) * 100
    ).fillna(0.0)

    # 出来高比率
    df["vol_ratio"] = (
        df["Volume"] / df["avg_vol_20"].replace(0, float("nan"))
    ).fillna(1.0).clip(upper=20.0)  # 外れ値クリップ

    # 5日高値ブレイクアウト（前日までの5日高値を当日終値が上抜け）
    df["high_5d_prev"] = g["High"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=5).max()
    )
    df["breakout_5d"] = (df["Close"] > df["high_5d_prev"]).fillna(False)

    # 52週安値圏ポジション（0〜100%）
    df["high_52w"] = g["High"].transform(lambda x: x.rolling(252, min_periods=60).max())
    df["low_52w"]  = g["Low"].transform(lambda x: x.rolling(252, min_periods=60).min())
    w52_range = (df["high_52w"] - df["low_52w"]).replace(0, float("nan"))
    df["w52_pos"] = ((df["Close"] - df["low_52w"]) / w52_range * 100).fillna(50.0)

    # 方向性出来高スコア（DVS）
    df["chg"]       = g["Close"].transform(lambda x: x.diff())
    df["up_vol"]    = df["Volume"].where(df["chg"] > 0, 0.0)
    df["down_vol"]  = df["Volume"].where(df["chg"] < 0, 0.0)
    df["up_vol_5"]  = g["up_vol"].transform(lambda x: x.rolling(5, min_periods=1).sum())
    df["down_vol_5"] = g["down_vol"].transform(lambda x: x.rolling(5, min_periods=1).sum())
    avg_vol_5 = g["Volume"].transform(lambda x: x.rolling(5, min_periods=1).mean())
    denom = (avg_vol_5 * 5).replace(0, float("nan"))
    df["dvs"] = ((df["up_vol_5"] - df["down_vol_5"]) / denom * 100).fillna(0.0)

    # ── Stage1スコアをベクトル化計算 ─────────────────────────────────────────
    score = pd.Series(0.0, index=df.index)

    # ブレイクアウト複合シグナル（最重要）
    bo  = df["breakout_5d"]
    dvs = df["dvs"]
    r5  = df["rsi5"]

    score[bo & (dvs > 0) & (r5 <= 30)] += 100   # breakout + dvs正 + rsi5低
    score[bo & (dvs > 0) & ~(r5 <= 30)] += 60   # breakout + dvs正
    score[bo & ~(dvs > 0)] += 20                 # breakout単独

    # DVS単独加点
    score[dvs > 30] += 20
    score[(dvs > 10) & (dvs <= 30)] += 10

    # 出来高急増
    vr = df["vol_ratio"]
    score[vr >= 2.0] += 40
    score[(vr >= 1.5) & (vr < 2.0)] += 20
    score[(vr >= 1.3) & (vr < 1.5)] += 10

    # 52週安値圏
    w = df["w52_pos"]
    score[w <= 20] += 30
    score[(w > 20) & (w <= 40)] += 15

    # RSI14 売られすぎ
    r14 = df["rsi14"]
    score[r14 <= 25] += 25
    score[(r14 > 25) & (r14 <= 35)] += 15
    score[(r14 > 35) & (r14 <= 40)] += 8

    # RSI5 売られすぎ
    score[r5 <= 20] += 20
    score[(r5 > 20) & (r5 <= 30)] += 10

    # SMA25押し目
    ma_diff = df["ma25_diff_pct"]
    score[(ma_diff >= -8) & (ma_diff <= -2) & (df["sma25"] > 0)] += 15

    # DVS強売りペナルティ（実質除外）
    score[dvs <= -10] -= 200

    df["stage1_score"] = score.round(1)

    # ── 追加ファクター ────────────────────────────────────────────────────────
    # 直近5日リターン（平均回帰シグナル）
    df["return_5d"] = g["Close"].transform(
        lambda x: x.pct_change(5) * 100
    ).fillna(0.0).clip(-50.0, 50.0)

    # SMA200 & MA200乖離率（長期トレンド軸）
    df["sma200"] = g["Close"].transform(
        lambda x: x.rolling(200, min_periods=60).mean()
    )
    df["ma200_diff_pct"] = (
        (df["Close"] - df["sma200"]) / df["sma200"].replace(0, float("nan")) * 100
    ).fillna(0.0)

    # 月内営業日順位（月初め・月末効果の検証用）
    unique_dates = sorted(pd.to_datetime(df["Date"].unique()))
    from collections import defaultdict
    month_dates: dict = defaultdict(list)
    for d in unique_dates:
        month_dates[(d.year, d.month)].append(d)
    date_to_rank_start: dict = {}
    date_to_rank_end: dict = {}
    for dates in month_dates.values():
        sorted_dates = sorted(dates)
        for i, d in enumerate(sorted_dates):
            date_to_rank_start[d] = i + 1
            date_to_rank_end[d] = len(sorted_dates) - i
    date_col = pd.to_datetime(df["Date"])
    df["biz_rank_in_month"]   = date_col.map(date_to_rank_start).fillna(99).astype(int)
    df["biz_rank_from_end"]   = date_col.map(date_to_rank_end).fillna(99).astype(int)

    # 不要な中間列を削除
    drop_cols = ["avg_vol_20", "high_5d_prev", "high_52w", "low_52w",
                 "chg", "up_vol", "down_vol", "up_vol_5", "down_vol_5"]
    df.drop(columns=[c for c in drop_cols if c in df.columns], inplace=True)

    print(f"[backtest] シグナル計算完了")
    return df


# ── トレード結果計算 ──────────────────────────────────────────────────────────

def calc_trade_outcomes(
    df: pd.DataFrame,
    sl_pct: float = SL_PCT,
    tp_pct: float = TP_PCT,
    holding_mode: str = None,
    cost_pct: float = None,
) -> pd.DataFrame:
    """
    各(Code, Date)シグナルの翌営業日トレード結果を計算する。

    エントリー: 翌日始値（next_open）
    イグジット（優先順位）:
      1. High が TP価格以上 かつ Low が SL価格より高い → TP達成 (+tp_pct%)
      2. Low が SL価格以下 → SL発動 (sl_pct%)   ← 両方ヒット時もSL優先（保守的）
      3. それ以外 → 翌日の決済価格（holding_mode による）

    holding_mode:
      "overnight" (既定): TP/SL未到達時は翌日終値で決済 → 1日保有
      "half_day":         TP/SL未到達時は (翌日始値+翌日終値)/2 で決済 → 半日保有近似
                          （発注窓: 朝9:00寄付 → 12:30後場寄付で手仕舞い）

    cost_pct: 往復取引コスト（%）。None の場合はモジュール定数を使う。
              リターンからこの値を減算する（現実的 EV 算出）。

    金曜日→月曜日の翌営業日対応はDataFrameのソート順（JQuantsは営業日のみ）で自動処理。
    """
    if holding_mode is None:
        holding_mode = HOLDING_MODE
    if cost_pct is None:
        cost_pct = ROUND_TRIP_COST_PCT

    print(f"[backtest] トレード結果計算中 (mode={holding_mode}, cost={cost_pct:.2f}%)...")
    df = df.sort_values(["Code", "Date"]).copy()
    g = df.groupby("Code", group_keys=False)

    df["next_date"]  = g["Date"].shift(-1)
    df["next_open"]  = g["Open"].shift(-1)
    df["next_high"]  = g["High"].shift(-1)
    df["next_low"]   = g["Low"].shift(-1)
    df["next_close"] = g["Close"].shift(-1)

    entry    = df["next_open"]
    tp_price = entry * (1.0 + tp_pct / 100.0)
    sl_price = entry * (1.0 + sl_pct / 100.0)

    tp_hit = df["next_high"] >= tp_price
    sl_hit = df["next_low"]  <= sl_price

    # 結果分類（SL優先：両ヒット時もSLとみなす）
    outcome = pd.Series("neutral", index=df.index, dtype=str)
    outcome[tp_hit & ~sl_hit] = "tp"
    outcome[sl_hit]            = "sl"  # sl_hit & tp_hit も含む
    df["outcome"] = outcome

    # イグジット価格（TP/SL未到達時）
    if holding_mode == "half_day":
        # 後場寄付 ≒ (翌日始値+翌日終値)/2 で近似（日足データの限界）
        neutral_exit_price = (df["next_open"] + df["next_close"]) / 2.0
    else:
        neutral_exit_price = df["next_close"]

    # リターン計算
    neutral_return = ((neutral_exit_price - entry) / entry * 100).round(3)
    return_pct_gross = neutral_return.copy()
    return_pct_gross[tp_hit & ~sl_hit] = tp_pct
    return_pct_gross[sl_hit]            = sl_pct

    # 取引コストを減算（現実的な手取り EV）
    return_pct_net = return_pct_gross - cost_pct

    df["return_pct_gross"] = return_pct_gross.round(3)
    df["return_pct"]       = return_pct_net.round(3)
    df["cost_pct"]         = cost_pct
    df["eod_return_pct"]   = ((df["next_close"] - entry) / entry * 100).round(3)
    df["holding_mode"]     = holding_mode

    print(f"[backtest] トレード結果計算完了 (rows={len(df)})")
    return df


# ── 閾値分析 ─────────────────────────────────────────────────────────────────

def _stats(df: pd.DataFrame) -> dict:
    """基本統計量を計算する。"""
    n = len(df)
    if n == 0:
        return {"n": 0, "tp_rate": 0.0, "sl_rate": 0.0,
                "win_rate": 0.0, "expected_value": 0.0,
                "avg_return": 0.0, "median_return": 0.0}
    tp_rate = (df["outcome"] == "tp").mean() * 100
    sl_rate = (df["outcome"] == "sl").mean() * 100
    win_rate = (df["return_pct"] > 0).mean() * 100
    avg_return = float(df["return_pct"].mean())
    return {
        "n":              n,
        "tp_rate":        round(tp_rate, 2),
        "sl_rate":        round(sl_rate, 2),
        "win_rate":       round(win_rate, 2),
        "expected_value": round(avg_return, 4),
        "avg_return":     round(avg_return, 4),
        "median_return":  round(float(df["return_pct"].median()), 4),
    }


def _analyze_bins(df: pd.DataFrame, col: str, bins: list[tuple]) -> list[dict]:
    """列をビン分割して各区間の統計を返す。"""
    results = []
    for lo, hi in bins:
        mask = (df[col] >= lo) & (df[col] < hi)
        stats = _stats(df[mask])
        stats["range"] = f"[{lo}, {hi})"
        stats["col"]   = col
        results.append(stats)
    return results


def analyze_thresholds(df: pd.DataFrame) -> dict:
    """
    Stage1通過銘柄のシグナル別・閾値別の勝率・期待値を分析する。
    各シグナル変数をビン分割して統計を算出する。
    """
    print("\n[backtest] 閾値分析中...")

    df_pass = df[df["stage1_score"] > 0].copy()
    n_total = len(df_pass)
    print(f"  Stage1通過シグナル数: {n_total:,}件 ({df_pass['Code'].nunique()}銘柄)")

    if n_total == 0:
        return {}

    analysis: dict = {"overall": _stats(df_pass)}

    # ── 各シグナルのビン分析 ─────────────────────────────────────────────────
    signal_bins: dict[str, list[tuple]] = {
        "rsi14": [
            (0, 15), (15, 20), (20, 25), (25, 30),
            (30, 35), (35, 40), (40, 50), (50, 60), (60, 100),
        ],
        "rsi5": [
            (0, 10), (10, 15), (15, 20), (20, 25),
            (25, 30), (30, 40), (40, 50), (50, 100),
        ],
        "vol_ratio": [
            (0, 0.8), (0.8, 1.0), (1.0, 1.2), (1.2, 1.3),
            (1.3, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 99),
        ],
        "w52_pos": [
            (0, 10), (10, 20), (20, 30), (30, 40),
            (40, 50), (50, 70), (70, 100),
        ],
        "dvs": [
            (-100, -20), (-20, -10), (-10, 0), (0, 10),
            (10, 20), (20, 30), (30, 50), (50, 100),
        ],
        "ma25_diff_pct": [
            (-15, -8), (-8, -5), (-5, -3), (-3, -1),
            (-1, 0), (0, 2), (2, 5), (5, 15),
        ],
        "stage1_score": [
            (0, 10), (10, 20), (20, 30), (30, 40),
            (40, 60), (60, 80), (80, 100), (100, 200),
        ],
    }

    for signal, bins in signal_bins.items():
        if signal in df_pass.columns:
            analysis[signal] = _analyze_bins(df_pass, signal, bins)

    # ブレイクアウト有無
    analysis["breakout_5d"] = {
        "True":  _stats(df_pass[df_pass["breakout_5d"]]),
        "False": _stats(df_pass[~df_pass["breakout_5d"].astype(bool)]),
    }

    return analysis


# ── レポート生成 ─────────────────────────────────────────────────────────────

def _parse_range(range_str: str) -> tuple[float, float]:
    """
    "[15, 20)" のような範囲文字列を (15.0, 20.0) にパースして返す。
    形式: "[lo, hi)" または "(lo, hi]" など括弧は無視してloとhiを抽出する。
    """
    # 括弧・スペースを除去してカンマ分割
    inner = range_str.strip().lstrip("[({").rstrip("])}")
    parts = [p.strip() for p in inner.split(",")]
    lo = float(parts[0])
    hi = float(parts[1])
    return lo, hi


def _print_bin_table(title: str, bins_data: list[dict], min_n: int = 30) -> None:
    """ビン分析結果をテーブル表示する。"""
    print(f"\n  {title}:")
    print(f"  {'範囲':>16} {'件数':>6} {'TP率':>7} {'SL率':>7} {'期待値':>8}")
    print(f"  {'-'*16} {'-'*6} {'-'*7} {'-'*7} {'-'*8}")
    for b in bins_data:
        n = b["n"]
        mark = "★" if n >= min_n and b["expected_value"] > 0 else " "
        print(
            f"  {b['range']:>16} {n:>6} "
            f"{b['tp_rate']:>6.1f}% {b['sl_rate']:>6.1f}% "
            f"{b['expected_value']:>+7.3f}%{mark}"
        )


def generate_report(
    df_train: pd.DataFrame,
    df_val: pd.DataFrame,
    analysis_train: dict,
) -> None:
    """
    バックテスト結果をコンソール出力とCSVに保存する。
    学習データで設計した閾値を検証データで評価して汎化性能を確認する。
    ★印 = n≥30 かつ期待値>0 の良好な条件。
    """
    print("\n" + "=" * 70)
    print("バックテスト結果レポート（学習/検証 分割評価）")
    print(f"  学習期間: {TRAIN_START} 〜 {TRAIN_END}")
    print(f"  検証期間: {VAL_START} 〜 {VAL_END}")
    print(f"  損切ライン: {SL_PCT}%  利確ライン: +{TP_PCT}%")
    holding_mode = df["holding_mode"].iloc[0] if "holding_mode" in df.columns and len(df) else HOLDING_MODE
    cost_pct = df["cost_pct"].iloc[0] if "cost_pct" in df.columns and len(df) else ROUND_TRIP_COST_PCT
    print(f"  保有モード: {holding_mode}   往復コスト: {cost_pct:.2f}%（EVから減算済）")
    print("=" * 70)

    overall_train = analysis_train.get("overall", {})
    print(f"\n【全体統計（Stage1通過銘柄）】")
    print(f"  学習期間 総シグナル数: {overall_train.get('n', 0):,}件")
    print(f"  学習 TP達成率(+{TP_PCT}%到達): {overall_train.get('tp_rate', 0):.1f}%")
    print(f"  学習 SL発動率({SL_PCT}%到達):  {overall_train.get('sl_rate', 0):.1f}%")
    print(f"  学習 勝率(return>0):         {overall_train.get('win_rate', 0):.1f}%")
    print(f"  学習 期待値(平均リターン):   {overall_train.get('expected_value', 0):+.3f}%")
    print(f"  学習 中央値リターン:         {overall_train.get('median_return', 0):+.3f}%")

    signal_labels = {
        "rsi14":        "RSI-14",
        "rsi5":         "RSI-5",
        "vol_ratio":    "出来高比率",
        "w52_pos":      "52週安値圏(%)",
        "dvs":          "DVSスコア",
        "ma25_diff_pct":"MA25乖離率(%)",
        "stage1_score": "Stage1スコア",
    }

    # 検証データのシグナル別統計を事前に計算
    df_val_pass = df_val[df_val["stage1_score"] > 0].copy() if not df_val.empty else pd.DataFrame()

    # ── シグナルビン別の学習vs検証テーブル ──────────────────────────────────
    print(f"\n【シグナル別閾値分析（学習 vs 検証）】")
    print(f"  ★ = 学習n≥30 かつ学習EV>0 (良好条件)")
    print(f"  判定: ✅再現=検証EV>0 / ⚠️低下=検証EV≤0 / ❓件数不足=検証n<30\n")

    signal_bins_map: dict[str, list[tuple]] = {
        "rsi14": [
            (0, 15), (15, 20), (20, 25), (25, 30),
            (30, 35), (35, 40), (40, 50), (50, 60), (60, 100),
        ],
        "rsi5": [
            (0, 10), (10, 15), (15, 20), (20, 25),
            (25, 30), (30, 40), (40, 50), (50, 100),
        ],
        "vol_ratio": [
            (0, 0.8), (0.8, 1.0), (1.0, 1.2), (1.2, 1.3),
            (1.3, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 99),
        ],
        "w52_pos": [
            (0, 10), (10, 20), (20, 30), (30, 40),
            (40, 50), (50, 70), (70, 100),
        ],
        "dvs": [
            (-100, -20), (-20, -10), (-10, 0), (0, 10),
            (10, 20), (20, 30), (30, 50), (50, 100),
        ],
        "ma25_diff_pct": [
            (-15, -8), (-8, -5), (-5, -3), (-3, -1),
            (-1, 0), (0, 2), (2, 5), (5, 15),
        ],
        "stage1_score": [
            (0, 10), (10, 20), (20, 30), (30, 40),
            (40, 60), (60, 80), (80, 100), (100, 200),
        ],
    }

    for key, label in signal_labels.items():
        if key not in analysis_train or not isinstance(analysis_train[key], list):
            continue
        train_bins = analysis_train[key]
        bins_def   = signal_bins_map.get(key, [])

        print(f"\n  {label}:")
        hdr = (f"  {'範囲':>16} {'設計n':>7} {'設計EV':>8} "
               f"{'検証n':>7} {'検証EV':>8} {'判定':>6}")
        print(hdr)
        print(f"  {'-'*16} {'-'*7} {'-'*8} {'-'*7} {'-'*8} {'-'*6}")

        for b_train, (lo, hi) in zip(train_bins, bins_def):
            train_n  = b_train["n"]
            train_ev = b_train["expected_value"]
            range_str = b_train["range"]

            # 検証データでの同ビン統計
            if not df_val_pass.empty and key in df_val_pass.columns:
                val_mask = (df_val_pass[key] >= lo) & (df_val_pass[key] < hi)
                val_sub  = df_val_pass[val_mask]
                val_stats = _stats(val_sub)
                val_n  = val_stats["n"]
                val_ev = val_stats["expected_value"]
            else:
                val_n  = 0
                val_ev = 0.0

            # 学習で「良好」なビンのみ判定表示
            is_good_train = (train_n >= 30) and (train_ev > 0)
            train_mark = "★" if is_good_train else " "

            if is_good_train:
                if val_n < 30:
                    judgment = "❓件数不足"
                elif val_ev > 0:
                    judgment = "✅再現"
                else:
                    judgment = "⚠️低下"
            else:
                judgment = ""

            print(
                f"  {range_str:>16} {train_n:>7} {train_ev:>+7.3f}%{train_mark} "
                f"{val_n:>7} {val_ev:>+7.3f}%  {judgment}"
            )

    # ブレイクアウト
    if "breakout_5d" in analysis_train:
        bo_train = analysis_train["breakout_5d"]
        print("\n  ブレイクアウト有無:")
        for flag, train_stats in bo_train.items():
            # 検証データ
            if not df_val_pass.empty and "breakout_5d" in df_val_pass.columns:
                if flag == "True":
                    val_sub = df_val_pass[df_val_pass["breakout_5d"].astype(bool)]
                else:
                    val_sub = df_val_pass[~df_val_pass["breakout_5d"].astype(bool)]
                val_stats = _stats(val_sub)
            else:
                val_stats = {"n": 0, "tp_rate": 0.0, "sl_rate": 0.0, "expected_value": 0.0}

            print(
                f"    {flag:>5}: 設計 n={train_stats['n']:>5} "
                f"TP={train_stats['tp_rate']:.1f}% SL={train_stats['sl_rate']:.1f}% "
                f"EV={train_stats['expected_value']:+.3f}%  |  "
                f"検証 n={val_stats['n']:>5} EV={val_stats['expected_value']:+.3f}%"
            )

    # ── 検証データ全体統計 ────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("【検証データ全体統計】")
    if not df_val_pass.empty:
        val_overall = _stats(df_val_pass)
        print(f"  検証期間 総シグナル数: {val_overall.get('n', 0):,}件")
        print(f"  検証 TP達成率(+{TP_PCT}%到達): {val_overall.get('tp_rate', 0):.1f}%")
        print(f"  検証 SL発動率({SL_PCT}%到達):  {val_overall.get('sl_rate', 0):.1f}%")
        print(f"  検証 勝率(return>0):         {val_overall.get('win_rate', 0):.1f}%")
        print(f"  検証 期待値(平均リターン):   {val_overall.get('expected_value', 0):+.3f}%")
        print(f"  検証 中央値リターン:         {val_overall.get('median_return', 0):+.3f}%")

        # 学習vs検証の比較サマリー
        print(f"\n  比較サマリー:")
        print(f"  {'指標':<20} {'学習':>10} {'検証':>10}")
        print(f"  {'-'*20} {'-'*10} {'-'*10}")
        metrics = [
            ("シグナル数",   "n",              "n"),
            ("TP達成率(%)",  "tp_rate",        "tp_rate"),
            ("SL発動率(%)",  "sl_rate",        "sl_rate"),
            ("勝率(%)",      "win_rate",       "win_rate"),
            ("期待値(%)",    "expected_value", "expected_value"),
            ("中央値(%)",    "median_return",  "median_return"),
        ]
        for label, t_key, v_key in metrics:
            t_val = overall_train.get(t_key, 0)
            v_val = val_overall.get(v_key, 0)
            print(f"  {label:<20} {t_val:>10.3f} {v_val:>10.3f}")
    else:
        print("  検証データなし（VAL_START〜VAL_END にデータがありません）")

    # ── CSV保存 ───────────────────────────────────────────────────────────────
    # 学習データのみ保存（optimize_thresholds.py 互換）
    df_train_pass = df_train[df_train["stage1_score"] > 0].copy()
    results_path = DATA_DIR / "results.csv"
    save_cols = [
        "Code", "Date", "Close", "Volume",
        "rsi5", "rsi14", "sma25", "ma25_diff_pct",
        "vol_ratio", "breakout_5d", "w52_pos", "dvs", "stage1_score",
        "next_date", "next_open", "next_high", "next_low", "next_close",
        "outcome", "return_pct", "return_pct_gross", "cost_pct",
        "eod_return_pct", "holding_mode",
    ]
    save_cols = [c for c in save_cols if c in df_train_pass.columns]
    df_train_pass[save_cols].to_csv(results_path, index=False, encoding="utf-8-sig")
    print(f"\n詳細データ（学習期間のみ）: {results_path}  ({len(df_train_pass):,}行)")

    # 閾値サマリー（学習データ）
    rows = []
    for signal, bins_data in analysis_train.items():
        if signal == "overall" or not isinstance(bins_data, list):
            continue
        for b in bins_data:
            rows.append({"signal": signal, **b})

    if rows:
        summary_df = pd.DataFrame(rows)
        summary_path = DATA_DIR / "threshold_summary.csv"
        summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
        print(f"閾値サマリー: {summary_path}  ({len(summary_df)}行)")

    print(f"\n次のステップ: python backtest/optimize_thresholds.py")


# ── メイン実行 ────────────────────────────────────────────────────────────────

def run_backtest(apply_filter: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    バックテストのメイン処理。
    学習期間（TRAIN_START〜TRAIN_END）と検証期間（VAL_START〜VAL_END）に分割して返す。

    Returns:
        (df_train, df_val): 学習期間のシグナルDataFrameと検証期間のシグナルDataFrame
    """
    all_data = load_all_data()
    if all_data.empty:
        return pd.DataFrame(), pd.DataFrame()

    train_start_dt = pd.Timestamp(TRAIN_START)
    train_end_dt   = pd.Timestamp(TRAIN_END)
    val_start_dt   = pd.Timestamp(VAL_START)
    val_end_dt     = pd.Timestamp(VAL_END)

    # ウォームアップ込みのデータ範囲（TRAIN_STARTの180日前から）
    warmup_start = train_start_dt - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
    df_full = all_data[all_data["Date"] >= warmup_start].copy()

    # 基本フィルタ適用
    if apply_filter:
        # 代表日（最終日）でフィルタして有効コードを取得
        latest_date = df_full["Date"].max()
        valid_codes = set(
            apply_basic_filter(df_full[df_full["Date"] == latest_date])["Code"].unique()
        )
        df_full = df_full[df_full["Code"].isin(valid_codes)].copy()
        print(f"[backtest] フィルタ後: {df_full['Code'].nunique()}銘柄")

    # シグナル計算
    df_signals = calc_all_signals(df_full)

    # トレード結果計算（手数料・保有モードを指定可能）
    df_results = calc_trade_outcomes(
        df_signals, holding_mode=holding_mode, cost_pct=cost_pct
    )

    # 学習期間に絞り込み
    df_train = df_results[
        (df_results["Date"] >= train_start_dt) &
        (df_results["Date"] <= train_end_dt) &
        df_results["next_open"].notna()
    ].copy()

    # 検証期間に絞り込み
    df_val = df_results[
        (df_results["Date"] >= val_start_dt) &
        (df_results["Date"] <= val_end_dt) &
        df_results["next_open"].notna()
    ].copy()

    print(
        f"[backtest] 学習期間: {len(df_train):,}行 | "
        f"{df_train['Code'].nunique()}銘柄 × {df_train['Date'].nunique()}日"
    )
    print(
        f"[backtest] 検証期間: {len(df_val):,}行 | "
        f"{df_val['Code'].nunique()}銘柄 × {df_val['Date'].nunique()}日"
    )
    return df_train, df_val


def main():
    parser = argparse.ArgumentParser(description="株式スクリーナー バックテスト（学習/検証分割）")
    parser.add_argument("--download",  action="store_true", help="JQuantsからデータダウンロード")
    parser.add_argument("--force",     action="store_true", help="既存データを上書き")
    parser.add_argument("--analyze",   action="store_true", help="バックテスト実行")
    parser.add_argument("--no-filter", action="store_true", help="銘柄フィルタを無効化")
    parser.add_argument(
        "--holding-mode",
        choices=["overnight", "half_day"],
        default=HOLDING_MODE,
        help="保有モード: overnight(1日, 既定) / half_day(後場寄付手仕舞い近似)",
    )
    parser.add_argument(
        "--cost-pct",
        type=float,
        default=None,
        help=f"往復取引コスト%%（既定: {ROUND_TRIP_COST_PCT:.2f}%% = 手数料{COMMISSION_PCT_ONEWAY}%%+スリッページ{SLIPPAGE_PCT_ONEWAY}%%×往復）",
    )
    args = parser.parse_args()

    # デフォルト: 両方実行
    if not args.download and not args.analyze:
        args.download = True
        args.analyze  = True

    if args.download:
        download_backtest_data(force=args.force)

    if args.analyze:
        df_train, df_val = run_backtest(apply_filter=not args.no_filter)
        if df_train.empty:
            print("[backtest] 学習データがありません。")
            return
        analysis = analyze_thresholds(df_train)  # 設計データのみで閾値探索
        generate_report(df_train, df_val, analysis)


if __name__ == "__main__":
    main()

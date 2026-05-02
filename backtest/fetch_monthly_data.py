#!/usr/bin/env python3
"""
backtest/fetch_monthly_data.py

指定した月の全銘柄OHLCVをJQuants APIから取得し、
backtest/data/daily_YYYYMM.parquet として保存する。

使用方法:
  JQUANTS_API_KEY=<key> python backtest/fetch_monthly_data.py --month 202604
  JQUANTS_API_KEY=<key> python backtest/fetch_monthly_data.py  # デフォルト: 前月

GitHub Actions から呼ばれる想定のため、依存ライブラリは requirements.txt のみ。
"""

import argparse
import os
import sys
import time
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

DATA_DIR = PROJECT_ROOT / "backtest" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── JQuantsクライアント ────────────────────────────────────────────────────────

def _get_client():
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if not api_key:
        print("[fetch] JQUANTS_API_KEY が未設定です。終了します。", file=sys.stderr)
        sys.exit(1)
    try:
        import jquantsapi
        return jquantsapi.ClientV2(api_key=api_key)
    except ImportError:
        print("[fetch] jquantsapi が未インストールです: pip install jquants-api-client", file=sys.stderr)
        sys.exit(1)


# ── カラム正規化 ──────────────────────────────────────────────────────────────

_DEBUG_COLUMNS_PRINTED = False


def _normalize(df: pd.DataFrame, date_str: str) -> pd.DataFrame:
    global _DEBUG_COLUMNS_PRINTED
    df = df.copy()
    col_lower = {c.lower(): c for c in df.columns}

    if not _DEBUG_COLUMNS_PRINTED:
        print(f"[fetch] APIカラム名（初回）: {list(df.columns)[:12]}")
        _DEBUG_COLUMNS_PRINTED = True

    # 調整済み価格優先。Light プランの短縮形 (AdjO/AdjC 等) にも対応
    candidates = {
        "Open":   ["adjo", "adjustmentopen",   "open",   "o"],
        "High":   ["adjh", "adjustmenthigh",   "high",   "h"],
        "Low":    ["adjl", "adjustmentlow",    "low",    "l"],
        "Close":  ["adjc", "adjustmentclose",  "close",  "c"],
        "Volume": ["adjvo","adjustmentvolume", "volume", "vo"],
        "Code":   ["code"],
    }
    for dst, srcs in candidates.items():
        for s in srcs:
            if s in col_lower:
                df[dst] = df[col_lower[s]]
                break

    if "Code" in df.columns:
        df["Code"] = df["Code"].astype(str).str[:4]
    df["Date"] = date_str

    keep = [c for c in ["Code", "Date", "Open", "High", "Low", "Close", "Volume"]
            if c in df.columns]
    df = df[keep].copy()
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "Close" not in df.columns or "Open" not in df.columns:
        return pd.DataFrame()
    return df.dropna(subset=["Close", "Open"])


# ── 営業日リスト ──────────────────────────────────────────────────────────────

def _trading_days(year: int, month: int, client) -> list[str]:
    """JQuantsカレンダーAPIを使って営業日を取得。失敗時は土日除外フォールバック。"""
    start = f"{year:04d}{month:02d}01"
    last_day = monthrange(year, month)[1]
    end = f"{year:04d}{month:02d}{last_day:02d}"
    try:
        cal = client.get_mkt_calendar(from_yyyymmdd=start, to_yyyymmdd=end)
        if cal is not None and not cal.empty:
            date_col = next((c for c in cal.columns if "date" in c.lower()), None)
            holiday_col = next(
                (c for c in cal.columns if "holiday" in c.lower() or "Holiday" in c), None
            )
            if date_col:
                if holiday_col:
                    cal = cal[cal[holiday_col].astype(str) == "0"]
                raw = cal[date_col].astype(str).tolist()
                result = []
                for d in raw:
                    d = d.strip()
                    if len(d) == 8 and d.isdigit():
                        result.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
                    elif len(d) >= 10:
                        result.append(d[:10])
                return sorted(result)
    except Exception as e:
        print(f"[fetch] カレンダーAPI失敗、土日除外で代替: {e}")

    # フォールバック
    days = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


# ── メイン処理 ────────────────────────────────────────────────────────────────

def fetch_month(year: int, month: int, force: bool = False) -> Path:
    month_str = f"{year:04d}{month:02d}"
    output = DATA_DIR / f"daily_{month_str}.parquet"

    if output.exists() and not force:
        print(f"[fetch] {output.name} は既に存在します。--force で上書きできます。")
        return output

    client = _get_client()
    days = _trading_days(year, month, client)
    print(f"[fetch] {year}/{month:02d} 対象営業日: {len(days)}日")

    frames = []
    errors = 0
    for i, day_str in enumerate(days, 1):
        yyyymmdd = day_str.replace("-", "")
        try:
            df = client.get_eq_bars_daily(date_yyyymmdd=yyyymmdd)
            if df is None or df.empty:
                print(f"  {day_str}: データなし（祝日の可能性）")
                continue
            norm = _normalize(df, day_str)
            if not norm.empty:
                frames.append(norm)
            else:
                print(f"  {day_str}: 正規化後データなし")
        except Exception as e:
            msg = str(e)
            print(f"  {day_str}: 取得失敗 ({type(e).__name__}: {msg[:100]})")
            errors += 1
            # 404 / データ未提供 → 即終了しない、継続
        time.sleep(0.2)

        if i % 10 == 0:
            print(f"  進捗: {i}/{len(days)}日 ({len(frames)}日取得)")

    if not frames:
        print(f"[fetch] 取得データが0件です。プランの遅延制限を確認してください。")
        print("  JQuants フリープランは約12週（84日）の遅延があります。")
        print("  Lightプラン以上で当月〜直近データが取得可能です。")
        sys.exit(1)

    combined = pd.concat(frames, ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"])
    combined.to_parquet(output, index=False)
    print(f"[fetch] 保存: {output.name} ({len(combined):,}行, {combined['Code'].nunique()}銘柄, {errors}件エラー)")
    return output


def _default_month() -> tuple[int, int]:
    """デフォルトは前月を返す。"""
    today = date.today()
    first = today.replace(day=1)
    last_month = first - timedelta(days=1)
    return last_month.year, last_month.month


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JQuantsから月次OHLCVをダウンロード")
    parser.add_argument(
        "--month",
        type=str,
        default=None,
        help="対象月 (YYYYMM形式, 例: 202604)。省略時は前月。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存ファイルを上書きする",
    )
    args = parser.parse_args()

    if args.month:
        if len(args.month) != 6 or not args.month.isdigit():
            print("--month は YYYYMM 形式で指定してください（例: 202604）", file=sys.stderr)
            sys.exit(1)
        year, month = int(args.month[:4]), int(args.month[4:])
    else:
        year, month = _default_month()
        print(f"[fetch] --month 未指定: デフォルト {year}/{month:02d} を使用")

    fetch_month(year, month, force=args.force)

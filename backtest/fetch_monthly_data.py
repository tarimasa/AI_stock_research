#!/usr/bin/env python3
"""
backtest/fetch_monthly_data.py

指定した月（単月または範囲）の全銘柄OHLCVをJQuants APIから取得し、
backtest/data/daily_YYYYMM.parquet として保存する。

使用方法:
  # 単月
  JQUANTS_API_KEY=<key> python backtest/fetch_monthly_data.py --month 202604

  # 範囲（202105〜202409 の全月）
  JQUANTS_API_KEY=<key> python backtest/fetch_monthly_data.py --months 202105-202409

  # デフォルト（前月）
  JQUANTS_API_KEY=<key> python backtest/fetch_monthly_data.py
"""

import argparse
import os
import sys
import time
from calendar import monthrange
from datetime import date, timedelta
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

    days = []
    d = date(year, month, 1)
    while d.month == month:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


# ── 月リスト生成 ──────────────────────────────────────────────────────────────

def _iter_months(start_ym: str, end_ym: str):
    """YYYYMM 形式の開始〜終了から (year, month) を順に yield する。"""
    y, m = int(start_ym[:4]), int(start_ym[4:])
    ey, em = int(end_ym[:4]), int(end_ym[4:])
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def _parse_months_arg(arg: str) -> list[tuple[int, int]]:
    """
    YYYYMM       → [(year, month)]
    YYYYMM-YYYYMM → [(y1,m1), ..., (y2,m2)]
    """
    arg = arg.strip()
    if "-" in arg:
        parts = arg.split("-")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError(f"範囲形式は YYYYMM-YYYYMM で指定してください: {arg}")
        start_ym, end_ym = parts
    else:
        start_ym = end_ym = arg

    if len(start_ym) != 6 or len(end_ym) != 6:
        raise ValueError(f"月は YYYYMM 形式で指定してください: {arg}")

    return list(_iter_months(start_ym, end_ym))


# ── 単月取得 ──────────────────────────────────────────────────────────────────

def fetch_month(year: int, month: int, client, force: bool = False) -> bool:
    """1ヶ月分を取得して parquet 保存。スキップ時は False を返す。"""
    month_str = f"{year:04d}{month:02d}"
    output = DATA_DIR / f"daily_{month_str}.parquet"

    if output.exists() and not force:
        print(f"[fetch] {output.name} はスキップ（既存。--force で上書き可）")
        return False

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
        except Exception as e:
            print(f"  {day_str}: 取得失敗 ({type(e).__name__}: {str(e)[:80]})")
            errors += 1
        time.sleep(0.2)

        if i % 10 == 0:
            print(f"  進捗: {i}/{len(days)}日 ({len(frames)}日取得)")

    if not frames:
        print(f"[fetch] {year}/{month:02d}: 取得データが0件。プラン遅延制限を確認してください。")
        return False

    combined = pd.concat(frames, ignore_index=True)
    combined["Date"] = pd.to_datetime(combined["Date"])
    combined.to_parquet(output, index=False)
    print(f"[fetch] 保存: {output.name} ({len(combined):,}行, {combined['Code'].nunique()}銘柄, エラー{errors}件)")
    return True


# ── デフォルト月（前月）────────────────────────────────────────────────────────

def _default_month() -> tuple[int, int]:
    today = date.today()
    first = today.replace(day=1)
    last_month = first - timedelta(days=1)
    return last_month.year, last_month.month


# ── エントリーポイント ────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="JQuantsから月次OHLCVをダウンロード",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  単月:   --month 202604
  範囲:   --months 202105-202409
  前月:   （引数なし）
""",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--month",
        type=str,
        metavar="YYYYMM",
        help="単月指定 (例: 202604)",
    )
    group.add_argument(
        "--months",
        type=str,
        metavar="YYYYMM-YYYYMM",
        help="範囲指定 (例: 202105-202409)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="既存ファイルを上書きする",
    )
    args = parser.parse_args()

    # 取得対象月リストを決定
    if args.months:
        try:
            targets = _parse_months_arg(args.months)
        except ValueError as e:
            print(f"エラー: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.month:
        try:
            targets = _parse_months_arg(args.month)
        except ValueError as e:
            print(f"エラー: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        y, m = _default_month()
        print(f"[fetch] 引数未指定: デフォルト {y}/{m:02d} を使用")
        targets = [(y, m)]

    print(f"[fetch] 取得対象: {len(targets)}ヶ月 "
          f"({targets[0][0]:04d}/{targets[0][1]:02d} 〜 {targets[-1][0]:04d}/{targets[-1][1]:02d})")

    client = _get_client()
    success = 0
    skipped = 0
    failed = 0

    for year, month in targets:
        result = fetch_month(year, month, client, force=args.force)
        if result:
            success += 1
        else:
            # 既存スキップ or データ0件
            output = DATA_DIR / f"daily_{year:04d}{month:02d}.parquet"
            if output.exists():
                skipped += 1
            else:
                failed += 1

    print(f"\n[fetch] 完了: 取得{success}件 / スキップ{skipped}件 / 失敗{failed}件")
    if failed > 0:
        sys.exit(1)

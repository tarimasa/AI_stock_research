"""
earnings_signal.py
J-Quants 決算発表予定日データから「決算前ドリフト」シグナルを生成する。
screener.py の決算日スコアリングを yfinance 依存から脱却させる。
"""

from datetime import datetime, timedelta, timezone

from data_fetcher import fetch_earnings_calendar

JST = timezone(timedelta(hours=9))

# インメモリキャッシュ（プロセス内）: 1 回のみ API 取得
_cache: dict | None = None


def get_upcoming_earnings(days_ahead: int = 45) -> dict:
    """
    今後 days_ahead 日以内に決算発表を控える銘柄の辞書を返す。

    Returns:
        {
            "7203": {"earnings_date": "2026-05-10", "days_until": 14},
            ...
        }
        キー: 4 桁銘柄コード、値: 決算日と残り日数
    """
    global _cache
    if _cache is None:
        _cache = _fetch_and_build()
    return _cache


def get_earnings_info_for_score(code: str, upcoming: dict | None = None) -> dict:
    """
    score_stock() に渡す info 辞書用の決算情報を返す。
    yfinance の info["earningsTimestamp"] の代替。

    Returns:
        {"earningsTimestamp": int|None}  # Unix 秒
    """
    if upcoming is None:
        upcoming = get_upcoming_earnings()

    code4 = code.replace(".T", "")[:4]
    earn_info = upcoming.get(code4)
    if not earn_info:
        return {}

    try:
        earn_date = datetime.strptime(earn_info["earnings_date"], "%Y-%m-%d")
        return {"earningsTimestamp": int(earn_date.timestamp())}
    except (ValueError, KeyError):
        return {}


def _fetch_and_build() -> dict:
    """API から決算カレンダーを取得してキャッシュ辞書を構築する。"""
    df = fetch_earnings_calendar()
    if df is None or df.empty:
        return {}

    today = datetime.now(JST).date()
    upcoming: dict = {}

    for _, row in df.iterrows():
        try:
            date_val = row.get("Date", row.get("date", ""))
            date_str = str(date_val)[:10]
            earnings_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue

        if earnings_date < today:
            continue

        days_until = (earnings_date - today).days

        raw_code = str(row.get("Code", "")).strip()
        if len(raw_code) == 5 and raw_code.endswith("0") and raw_code.isdigit():
            code4 = raw_code[:4]
        else:
            code4 = raw_code[:4]

        if not code4 or not code4.isdigit():
            continue

        # 同一銘柄で複数の決算日がある場合は直近を採用
        existing = upcoming.get(code4)
        if existing is None or days_until < existing["days_until"]:
            upcoming[code4] = {
                "earnings_date": earnings_date.isoformat(),
                "days_until": days_until,
            }

    print(f"[earnings_signal] 決算予定: {len(upcoming)}銘柄")
    return upcoming


def refresh_cache() -> None:
    """キャッシュを強制クリアして次回呼び出し時に再取得させる。"""
    global _cache
    _cache = None

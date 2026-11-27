"""
jquants_fetcher.py
J-Quants API V2（日本取引所グループ公式）から財務データを取得するモジュール。

【V2 API について（2025年12月22日以降登録者向け）】
- 認証: API キーのみ（メール/パスワード不要）
- API キーの取得: https://jpx-jquants.com/ → ダッシュボード → API キー発行

【無料プラン（フリープラン）で取得できるデータ】
  ✅ 上場銘柄一覧 (get_eq_master)        → 全上場銘柄のセクター・市場区分
  ✅ 財務サマリー (get_fin_summary)       → 四半期ごとの売上・利益（遅延あり）
  ✅ 日次株価データ (get_eq_bars_daily)   → 12週遅延（yfinanceで代替）
  ❌ 信用残 weekly_margin_interest       → Standard プラン（¥3,300/月）のみ
  ❌ 空売り比率 short_selling            → Standard プランのみ

【環境変数】
  JQUANTS_API_KEY = "<ダッシュボードで発行したAPIキー>"
  GitHub Secrets に追加することで GitHub Actions でも利用可能。

【非攻撃的アクセス方針】
  - キャッシュ TTL: 銘柄一覧 24h、財務データ 7日
  - API 呼び出し失敗時は None 返却でグレースフルフォールバック
  - ライブラリのデフォルトレートリミットに従う
"""

import os
import time
from datetime import date, datetime, timedelta
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

JQUANTS_API_KEY = os.environ.get("JQUANTS_API_KEY", "")

# jquants-api-client をオプション依存として扱う
try:
    import jquantsapi
    _LIB_AVAILABLE = True
except ImportError:
    _LIB_AVAILABLE = False

# インメモリキャッシュ（プロセス内）
_cache: dict = {}
_CACHE_TTL_FINANCIAL = timedelta(days=7)
_CACHE_TTL_LISTED = timedelta(hours=24)


def is_available() -> bool:
    """J-Quants V2 API が使える状態かどうかを返す。"""
    return bool(JQUANTS_API_KEY) and _LIB_AVAILABLE


def _get_client():
    """V2 クライアントを返す（APIキー認証）。"""
    if not is_available():
        return None
    try:
        return jquantsapi.ClientV2(api_key=JQUANTS_API_KEY)
    except Exception as e:
        print(f"[jquants] V2クライアント初期化失敗: {e}")
        return None


def _is_cache_valid(key: str, ttl: timedelta) -> bool:
    entry = _cache.get(key)
    if not entry:
        return False
    return datetime.now() - entry["ts"] < ttl


def _set_cache(key: str, value) -> None:
    _cache[key] = {"ts": datetime.now(), "data": value}


def _get_cache(key: str):
    return _cache.get(key, {}).get("data")


# ─────────────────────────────────────────────────────────────
# 無料プラン: 財務サマリー（決算期ごとの売上・利益）
# ─────────────────────────────────────────────────────────────

def fetch_financial_growth(code: str) -> dict:
    """
    J-Quants 無料プランの財務サマリーから前年同期比成長率を計算して返す。

    戻り値:
    {
        "revenue_growth_pct": 12.5,     # 売上高 YoY 成長率（%）
        "profit_growth_pct": 23.1,      # 営業利益 YoY 成長率（%）
        "latest_date": "2025-11-14",    # 直近の開示日
        "available": True               # データ取得成功かどうか
    }
    データ未取得時は全フィールドが None / available=False。
    """
    cache_key = f"fin_growth_{code}"
    if _is_cache_valid(cache_key, _CACHE_TTL_FINANCIAL):
        return _get_cache(cache_key)

    empty = {"revenue_growth_pct": None, "profit_growth_pct": None,
             "latest_date": None, "available": False}

    client = _get_client()
    if client is None:
        return empty

    code4 = code.replace(".T", "")
    try:
        # V2 API: get_fin_summary で財務サマリーを取得
        df = client.get_fin_summary(code=code4)
        if df is None or df.empty:
            _set_cache(cache_key, empty)
            return empty

        # 開示日でソートして最新と1年前を比較
        date_col = next((c for c in df.columns if "Date" in c or "date" in c), None)
        if date_col:
            df = df.sort_values(date_col)

        if len(df) < 2:
            _set_cache(cache_key, empty)
            return empty

        # 売上高カラムを探す（V2の列名はAPIバージョンで変わることがある）
        sales_col = next((c for c in df.columns
                          if any(k in c for k in ["NetSales", "Sales", "Revenue"])), None)
        profit_col = next((c for c in df.columns
                           if any(k in c for k in ["OperatingProfit", "OperatingIncome"])), None)

        latest = df.iloc[-1]
        # 4期前（約1年前）と比較
        prev = df.iloc[-5] if len(df) >= 5 else df.iloc[0]

        rev_growth = None
        profit_growth = None

        if sales_col:
            latest_sales = float(latest[sales_col] or 0)
            prev_sales = float(prev[sales_col] or 0)
            if prev_sales > 0:
                rev_growth = round((latest_sales - prev_sales) / prev_sales * 100, 1)

        if profit_col:
            latest_profit = float(latest[profit_col] or 0)
            prev_profit = float(prev[profit_col] or 0)
            if abs(prev_profit) > 0:
                profit_growth = round((latest_profit - prev_profit) / abs(prev_profit) * 100, 1)

        latest_date = str(latest.get(date_col, ""))[:10] if date_col else None
        result = {
            "revenue_growth_pct": rev_growth,
            "profit_growth_pct": profit_growth,
            "latest_date": latest_date,
            "available": True,
        }
        _set_cache(cache_key, result)
        return result

    except Exception as e:
        print(f"[jquants] {code} 財務成長率取得失敗: {e}")
        _set_cache(cache_key, empty)
        return empty


def get_financial_growth_score(code: str) -> tuple[int, str]:
    """
    財務成長率からスコアとシグナル説明を返す（screener から呼ぶ）。

    Returns:
        (score: int, description: str)
        score: -5 〜 +20 点
    短期売買目的でも、業績が急成長している銘柄は継続上昇しやすい。
    業績急悪化は見送りシグナル。
    """
    if not is_available():
        return 0, ""

    data = fetch_financial_growth(code)
    if not data.get("available"):
        return 0, ""

    profit_growth = data.get("profit_growth_pct")
    rev_growth = data.get("revenue_growth_pct")

    # 営業利益成長優先、なければ売上成長で代替
    growth = profit_growth if profit_growth is not None else rev_growth
    label = "営業利益" if profit_growth is not None else "売上高"

    if growth is None:
        return 0, ""

    if growth >= 30:
        return 20, f"{label}成長{growth:+.0f}%（急成長・モメンタム強い）"
    elif growth >= 15:
        return 12, f"{label}成長{growth:+.0f}%"
    elif growth >= 5:
        return 6, f"{label}成長{growth:+.0f}%（緩成長）"
    elif growth >= -5:
        return 2, f"{label}横ばい（{growth:+.0f}%）"
    elif growth >= -20:
        return 0, f"{label}減少{growth:.0f}%（注意）"
    else:
        return -5, f"{label}大幅減少{growth:.0f}%（要注意）"


# ─────────────────────────────────────────────────────────────
# 無料プラン: 上場銘柄一覧（全銘柄スクリーニング拡張用）
# ─────────────────────────────────────────────────────────────

def fetch_all_listed_stocks(market_codes: list[str] | None = None) -> list[dict]:
    """
    J-Quants から東証の上場銘柄一覧を取得する。
    market_codes: ["Prime", "Standard", "Growth"] で絞り込み可能。
    未指定時は Prime のみ返す。

    戻り値: [{"code": "7203.T", "name": "トヨタ自動車", "sector": "自動車"}, ...]
    """
    cache_key = "listed_stocks"
    if _is_cache_valid(cache_key, _CACHE_TTL_LISTED):
        cached = _get_cache(cache_key)
        if cached:
            return cached

    if not is_available():
        return []

    client = _get_client()
    if client is None:
        return []

    target_markets = market_codes or ["Prime"]

    try:
        df = client.get_eq_master()
        if df is None or df.empty:
            return []

        # カラム名の正規化（V2はバージョンで変わることがある）
        market_col = next((c for c in df.columns
                           if "Market" in c and "Name" in c), None)
        sector_col = next((c for c in df.columns
                           if "Sector" in c and "Name" in c and "33" in c), None)
        code_col = next((c for c in df.columns if c in ["Code", "code"]), None)
        name_col = next((c for c in df.columns
                         if c in ["CompanyName", "Name", "company_name"]), None)

        if not code_col or not name_col:
            print("[jquants] 上場銘柄一覧: 期待するカラムが見つかりません")
            return []

        # 市場区分でフィルタ
        if market_col:
            df = df[df[market_col].str.contains("|".join(target_markets), na=False)]

        results = []
        for _, row in df.iterrows():
            code = str(row[code_col]).zfill(4)
            if not code.isdigit():
                continue
            results.append({
                "code": f"{code}.T",
                "name": str(row[name_col]),
                "sector": str(row[sector_col]) if sector_col else "不明",
            })

        print(f"[jquants] 上場銘柄一覧取得: {len(results)}銘柄")
        _set_cache(cache_key, results)
        return results

    except Exception as e:
        print(f"[jquants] 上場銘柄一覧取得失敗: {e}")
        return []


# ─────────────────────────────────────────────────────────────
# 有料プラン（Standard）: 信用残・空売り（スキャフォール）
# ─────────────────────────────────────────────────────────────

def fetch_margin_balance(code: str) -> dict:
    """
    信用残データを返す（Standard プラン ¥3,300/月 が必要）。
    フリープランでは常に available=False を返す。
    """
    return {"code": code, "margin_buy": None, "margin_sell": None,
            "margin_ratio": None, "date": None, "available": False}


# ─────────────────────────────────────────────────────────────────────────────
# 前場四本値 (/equities/bars/daily/am)
# ─────────────────────────────────────────────────────────────────────────────
# 仕様（jquants-api-client 2.8.0 の実装で確認）:
#   - エンドポイント: GET /v2/equities/bars/daily/am
#   - 日付パラメータなし。code 省略で全銘柄の「直近の前場」を返す
#   - 列: Date, Code(5桁), MO(前場始値), MH(前場高値), ML(前場安値),
#         MC(前場終値), MVo(前場出来高), MVa(前場売買代金)
#   - 前場終了（11:30）後、当日分が反映されるまでは Date が前営業日のまま
#     → Date == 当日 になった時点を「反映完了」とみなしてポーリングする
#   ※ 提供プラン・反映時刻は公式仕様ページで要確認（契約外なら 401/403 が返る）

JST = None
try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    pass


def now_jst() -> datetime:
    return datetime.now(JST) if JST else datetime.utcnow() + timedelta(hours=9)


def _to_float(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if f == f else None  # NaN 除外
    except (TypeError, ValueError):
        return None


class MorningBarsUnavailable(Exception):
    """契約プラン外・認証エラーなど、待っても取得できない状態。"""


def fetch_morning_bars_today() -> dict | None:
    """
    前場四本値を全銘柄一括で1回取得する。

    Returns:
        当日分が反映済みなら {code4: {"open","high","low","close","volume","value"}}。
        まだ前営業日のデータしか無い（未反映）場合は None。
    Raises:
        MorningBarsUnavailable: 401/403 等、ポーリングしても無駄な場合。
    """
    client = _get_client()
    if client is None:
        raise MorningBarsUnavailable("J-Quants クライアント未初期化（APIキー未設定 or ライブラリ未導入）")

    try:
        df = client.get_eq_bars_daily_am()
    except Exception as e:
        msg = str(e)
        if any(s in msg for s in ("401", "403", "Forbidden", "Unauthorized")):
            raise MorningBarsUnavailable(f"前場四本値 API にアクセスできません（契約プラン外の可能性）: {msg[:200]}")
        print(f"[jquants] 前場四本値 取得エラー（リトライ対象）: {type(e).__name__}: {msg[:200]}")
        return None

    if df is None or df.empty or "Date" not in df.columns or df["Date"].isna().all():
        return None

    today = now_jst().date()
    latest = df["Date"].max()
    latest_date = latest.date() if hasattr(latest, "date") else None
    if latest_date != today:
        return None

    today_df = df[df["Date"] == latest]
    result: dict = {}
    for row in today_df.to_dict("records"):
        code4 = str(row.get("Code", ""))[:4]
        o, c = _to_float(row.get("MO")), _to_float(row.get("MC"))
        if not code4 or not o or not c:
            continue  # 前場で約定なし
        result[code4] = {
            "date": latest_date.isoformat(),
            "open": o,
            "high": _to_float(row.get("MH")) or max(o, c),
            "low": _to_float(row.get("ML")) or min(o, c),
            "close": c,
            "volume": _to_float(row.get("MVo")) or 0.0,
            "value": _to_float(row.get("MVa")) or 0.0,
        }
    return result or None


def wait_for_morning_bars(deadline: datetime, interval_sec: int = 60) -> dict | None:
    """
    当日の前場四本値が反映されるまでポーリングし、反映され次第すぐ返す。

    Args:
        deadline: この時刻(JST aware)を過ぎたら諦めて None を返す。
        interval_sec: ポーリング間隔（秒）。
    Returns:
        fetch_morning_bars_today() の結果。期限切れ・利用不可なら None（呼び出し側でフォールバック）。
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            bars = fetch_morning_bars_today()
        except MorningBarsUnavailable as e:
            print(f"[jquants] {e} → フォールバック")
            return None
        if bars:
            print(f"[jquants] 前場四本値 反映確認 ({now_jst():%H:%M:%S} JST, {attempt}回目, {len(bars)}銘柄)")
            return bars
        if now_jst() + timedelta(seconds=interval_sec) > deadline:
            print(f"[jquants] 前場四本値 未反映のまま期限 {deadline:%H:%M} JST 到達（{attempt}回試行）→ フォールバック")
            return None
        print(f"[jquants] 前場四本値 未反映 ({now_jst():%H:%M:%S} JST) → {interval_sec}秒後に再試行")
        time.sleep(interval_sec)

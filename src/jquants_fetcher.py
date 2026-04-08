"""
jquants_fetcher.py
J-Quants API（日本取引所グループ公式・無料）を使って
信用残・空売り比率・業種別データを取得するモジュール。

【セットアップ手順】
1. https://jpx-jquants.com/ でアカウント登録（無料プランあり）
2. メールアドレス・パスワードを取得
3. 環境変数に設定:
   JQUANTS_EMAIL=your@email.com
   JQUANTS_PASSWORD=yourpassword
4. pip install jquants-api-client

【無料プランの制限】
- 価格データ: 12週間遅延（リアルタイム不可）
- 財務データ・信用残: 利用可能（遅延あり）
- 短期売買目的なら Standard プラン（¥3,300/月）が必要

【本モジュールの動作】
- 環境変数が設定されていれば実データを取得
- 未設定の場合はダミーデータ（None/空）を返しスキップ
- screener.py の score_stock() から将来的に呼び出す予定
"""

import os
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

JQUANTS_EMAIL = os.environ.get("JQUANTS_EMAIL", "")
JQUANTS_PASSWORD = os.environ.get("JQUANTS_PASSWORD", "")
_JQUANTS_AVAILABLE = bool(JQUANTS_EMAIL and JQUANTS_PASSWORD)

# jquants-api-client をオプション依存として扱う
try:
    import jquantsapi
    _JQUANTS_INSTALLED = True
except ImportError:
    _JQUANTS_INSTALLED = False


def is_available() -> bool:
    """J-Quants APIが使える状態かどうかを返す。"""
    return _JQUANTS_AVAILABLE and _JQUANTS_INSTALLED


def _get_client():
    """認証済みクライアントを返す（内部使用）。"""
    if not is_available():
        return None
    try:
        client = jquantsapi.Client(mail_address=JQUANTS_EMAIL, password=JQUANTS_PASSWORD)
        return client
    except Exception as e:
        print(f"[jquants] クライアント初期化失敗: {e}")
        return None


def fetch_margin_balance(code: str) -> dict:
    """
    信用残データを返す（週次・遅延データ）。

    戻り値:
    {
        "code": "7203",
        "margin_buy": 1234567,      # 信用買い残（株数）
        "margin_sell": 234567,      # 信用売り残（株数）
        "margin_ratio": 5.27,       # 信用倍率（買い残 / 売り残）
        "date": "2025-03-28"        # データ日付
    }
    信用倍率が高い（>10）= 過熱・整理売りリスク
    信用倍率が低い（<1）= 売り残多、踏み上げ（ショートスクイーズ）期待

    J-Quanstなしの場合はすべてNoneを返す。
    """
    if not is_available():
        return {"code": code, "margin_buy": None, "margin_sell": None,
                "margin_ratio": None, "date": None}

    client = _get_client()
    if client is None:
        return {"code": code, "margin_buy": None, "margin_sell": None,
                "margin_ratio": None, "date": None}

    try:
        code4 = code.replace(".T", "")
        # 直近4週分取得して最新値を使う
        end = date.today()
        start = end - timedelta(weeks=4)
        df = client.get_weekly_margin_interest(
            code=code4,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
        )
        if df is None or df.empty:
            raise ValueError("空のデータ")
        latest = df.sort_values("Date").iloc[-1]
        buy = float(latest.get("MarginBuy", 0) or 0)
        sell = float(latest.get("MarginSell", 0) or 0)
        ratio = round(buy / sell, 2) if sell > 0 else None
        return {
            "code": code,
            "margin_buy": int(buy),
            "margin_sell": int(sell),
            "margin_ratio": ratio,
            "date": str(latest.get("Date", ""))[:10],
        }
    except Exception as e:
        print(f"[jquants] {code} 信用残取得失敗: {e}")
        return {"code": code, "margin_buy": None, "margin_sell": None,
                "margin_ratio": None, "date": None}


def fetch_short_selling_ratio(code: str) -> dict:
    """
    空売り比率データを返す（日次・遅延データ）。

    戻り値:
    {
        "code": "7203",
        "short_ratio": 38.5,   # 空売り比率（%）
        "date": "2025-03-28"
    }
    空売り比率が高い（>40%）= 売り方の多い状況、踏み上げリスクあり
    空売り比率が低い（<20%）= 安定した買い優勢の相場

    J-Quantsなしの場合はNoneを返す。
    """
    if not is_available():
        return {"code": code, "short_ratio": None, "date": None}

    client = _get_client()
    if client is None:
        return {"code": code, "short_ratio": None, "date": None}

    try:
        code4 = code.replace(".T", "")
        end = date.today()
        start = end - timedelta(weeks=2)
        df = client.get_short_selling(
            sector33code=None,  # 個別銘柄取得
            date_from=start.isoformat(),
            date_to=end.isoformat(),
        )
        if df is None or df.empty:
            raise ValueError("空のデータ")
        stock_df = df[df["Code"] == code4]
        if stock_df.empty:
            raise ValueError(f"{code4}のデータなし")
        latest = stock_df.sort_values("Date").iloc[-1]
        ratio = float(latest.get("ShortSellingRatio", 0) or 0)
        return {
            "code": code,
            "short_ratio": round(ratio, 1),
            "date": str(latest.get("Date", ""))[:10],
        }
    except Exception as e:
        print(f"[jquants] {code} 空売り比率取得失敗: {e}")
        return {"code": code, "short_ratio": None, "date": None}


def fetch_foreign_investor_flows() -> dict:
    """
    外国人投資家の売買動向（週次）を返す。

    戻り値:
    {
        "net_buy_stocks": 123456789,    # 現物株 外国人純買い（円）
        "net_buy_futures": -987654321,  # 先物 外国人純買い（円）
        "date": "2025-03-28"
    }
    外国人の現物・先物同時純買い → 強い上昇シグナル
    同時純売り → 警戒（外国人売りが日本株を押し下げる傾向）

    JPXサイトからの取得はJ-Quantsで代替可能。
    未設定の場合はNoneを返す。
    """
    if not is_available():
        return {"net_buy_stocks": None, "net_buy_futures": None, "date": None}

    client = _get_client()
    if client is None:
        return {"net_buy_stocks": None, "net_buy_futures": None, "date": None}

    try:
        end = date.today()
        start = end - timedelta(weeks=2)
        df = client.get_breakdown(
            date_from=start.isoformat(),
            date_to=end.isoformat(),
        )
        if df is None or df.empty:
            raise ValueError("空のデータ")
        # 外国法人の純買いを集計
        foreign = df[df["Section"] == "外国法人"]
        if foreign.empty:
            raise ValueError("外国法人データなし")
        latest_date = foreign["Date"].max()
        day_data = foreign[foreign["Date"] == latest_date]
        net_stocks = int(day_data.get("NetBuyStocks", [0]).sum())
        return {
            "net_buy_stocks": net_stocks,
            "net_buy_futures": None,  # 先物は別エンドポイントが必要
            "date": str(latest_date)[:10],
        }
    except Exception as e:
        print(f"[jquants] 外国人動向取得失敗: {e}")
        return {"net_buy_stocks": None, "net_buy_futures": None, "date": None}


def get_margin_signal_score(code: str) -> tuple[int, str]:
    """
    信用残データからスコアとシグナル文字列を返す（screener.pyから呼び出し用）。

    Returns:
        (score: int, description: str)
        score 0〜20点
    """
    data = fetch_margin_balance(code)
    ratio = data.get("margin_ratio")
    if ratio is None:
        return 0, ""

    if ratio < 1.0:
        # 信用倍率1未満: 空売り優勢 → 踏み上げ（ショートスクイーズ）期待
        return 20, f"信用倍率{ratio}（売り残多・踏み上げ期待）"
    elif ratio < 2.0:
        return 12, f"信用倍率{ratio}（バランス型）"
    elif ratio < 5.0:
        return 5, f"信用倍率{ratio}（買い残やや多）"
    else:
        # 信用倍率5超: 買い残過多 → 整理売り圧力
        return 0, f"信用倍率{ratio}（買い残過多・注意）"


# ──────────────────────────────────────────────────────────────
# 【将来の実装計画】
# 1. screener.py の score_stock() に get_margin_signal_score() を組み込む
#    → 信用倍率が低い（踏み上げ期待）銘柄をスコア加算
#
# 2. claude_analyzer.py の build_user_prompt() に外国人動向を追加
#    → 「外国人純買い継続中」の文脈でClaudeが強気判断
#
# 3. GitHub Actions で JQUANTS_EMAIL / JQUANTS_PASSWORD を Secrets に追加
#    → 自動レポートで信用残・空売り比率を毎日取得
#
# 4. J-Quants の Standard プラン（¥3,300/月）でリアルタイムデータを利用
# ──────────────────────────────────────────────────────────────

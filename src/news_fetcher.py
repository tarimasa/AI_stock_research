"""
news_fetcher.py
RSS フィードから過去 24 時間のニュースを取得する。
  - fetch_news_for_stock(): 特定銘柄に関連するニュース
  - fetch_market_news(): 市場全体・地政学・マクロに影響するニュース
"""

import os
from datetime import datetime, timedelta, timezone

import feedparser
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# 個別銘柄ニュース用フィード
_STOCK_RSS_FEEDS = [
    "https://finance.yahoo.co.jp/rss/news",
    "https://www.nikkei.com/rss/",
]

# 市場全体・マクロ・地政学ニュース用フィード
# Google News RSS は公開クローリング可能・認証不要
_MARKET_RSS_FEEDS = [
    # 日本株・相場全般
    "https://news.google.com/rss/search?q=日経平均+株価+相場&hl=ja&gl=JP&ceid=JP:ja",
    # 地政学・原油・国際情勢
    "https://news.google.com/rss/search?q=原油+地政学+中東&hl=ja&gl=JP&ceid=JP:ja",
    # 米国経済・FRB・金利
    "https://news.google.com/rss/search?q=FRB+米国経済+金利&hl=ja&gl=JP&ceid=JP:ja",
    # 為替
    "https://news.google.com/rss/search?q=円安+円高+為替&hl=ja&gl=JP&ceid=JP:ja",
]


def fetch_market_news(hours: int = 16, max_headlines: int = 15) -> list[dict]:
    """
    市場全体に影響するマクロ・地政学ニュースを取得する。
    日本株・原油・米経済・為替に関するヘッドラインを返す。

    Returns: [
        {
            "title": "...",
            "published": "2026-04-08T06:00:00",
            "source": "カテゴリ名"
        }, ...
    ]
    """
    if DRY_RUN:
        return _dummy_market_news()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen_titles: set[str] = set()
    results = []

    categories = ["日本株・相場", "地政学・原油", "米国経済・金利", "為替"]
    for feed_url, category in zip(_MARKET_RSS_FEEDS, categories):
        try:
            feed = feedparser.parse(feed_url)
            count = 0
            for entry in feed.entries:
                if count >= 5:  # 各フィードから最大5件
                    break
                title = entry.get("title", "").strip()
                if not title or title in seen_titles:
                    continue

                published_parsed = entry.get("published_parsed")
                if published_parsed:
                    pub_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                    pub_str = pub_dt.strftime("%Y-%m-%dT%H:%M:%S")
                else:
                    pub_str = ""

                seen_titles.add(title)
                results.append({
                    "title": title,
                    "published": pub_str,
                    "source": category,
                })
                count += 1
        except Exception as e:
            print(f"[news_fetcher] マーケットRSS取得エラー ({category}): {e}")

    return results[:max_headlines]


def fetch_news_for_stock(stock_code: str, stock_name: str, hours: int = 24) -> list[dict]:
    """
    特定銘柄に関するニュースを返す。

    Returns: [
        {
            "title": "...",
            "summary": "...",
            "published": "2026-03-28T06:00:00",
            "url": "https://..."
        }
    ]
    """
    if DRY_RUN:
        return _dummy_news(stock_name)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    results = []
    code_num = stock_code.split(".")[0]

    for feed_url in _STOCK_RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                text = title + " " + summary

                if stock_name not in text and code_num not in text:
                    continue

                published_parsed = entry.get("published_parsed")
                if published_parsed:
                    pub_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                    pub_str = pub_dt.strftime("%Y-%m-%dT%H:%M:%S")
                else:
                    pub_str = ""

                results.append({
                    "title": title,
                    "summary": summary[:200],
                    "published": pub_str,
                    "url": entry.get("link", ""),
                })
        except Exception as e:
            print(f"[news_fetcher] RSS 取得エラー ({feed_url}): {e}")

    return results


def _dummy_market_news() -> list[dict]:
    """DRY_RUN 用のダミー市場ニュース。"""
    return [
        {"title": "米国とイランが停戦合意、原油先物が急落", "published": "2026-04-08T05:00:00", "source": "地政学・原油"},
        {"title": "日経平均、前日比+200円で推移", "published": "2026-04-08T01:00:00", "source": "日本株・相場"},
        {"title": "FRB議長「利下げは慎重に」、米国債利回り上昇", "published": "2026-04-08T03:00:00", "source": "米国経済・金利"},
        {"title": "ドル円147円台、円安一服", "published": "2026-04-08T04:00:00", "source": "為替"},
    ]


def _dummy_news(stock_name: str) -> list[dict]:
    """DRY_RUN 用の個別銘柄ダミーニュース。"""
    return [
        {
            "title": f"{stock_name}、好決算を発表",
            "summary": f"{stock_name}は本日、前年比増益の決算を発表した。",
            "published": "2026-03-28T06:00:00",
            "url": "https://example.com/news/dummy",
        }
    ]

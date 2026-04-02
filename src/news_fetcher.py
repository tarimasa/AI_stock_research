"""
news_fetcher.py
RSS フィードから過去 24 時間のニュースを取得し、銘柄コード・名称でフィルタリングする。
"""

import os
from datetime import datetime, timedelta, timezone

import feedparser
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

RSS_FEEDS = [
    "https://finance.yahoo.co.jp/rss/news",
    "https://www.nikkei.com/rss/",
]


def fetch_news_for_stock(stock_code: str, stock_name: str, hours: int = 24) -> list[dict]:
    """
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
    # 銘柄コードから数字部分のみ（例: "7203.T" → "7203"）
    code_num = stock_code.split(".")[0]

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                text = title + " " + summary

                # 銘柄名 or コードが含まれるか
                if stock_name not in text and code_num not in text:
                    continue

                # 時刻フィルタ
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


def _dummy_news(stock_name: str) -> list[dict]:
    """DRY_RUN 用のダミーニュース。"""
    return [
        {
            "title": f"{stock_name}、好決算を発表",
            "summary": f"{stock_name}は本日、前年比増益の決算を発表した。",
            "published": "2026-03-28T06:00:00",
            "url": "https://example.com/news/dummy",
        }
    ]

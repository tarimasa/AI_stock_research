"""
main.py
オーケストレーター。GitHub Actions から実行される。
実行順: スクリーニング → ニュース取得 → Claude 分析 → ポートフォリオ確認 → LINE 送信
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# src/ 配下のモジュールを直接 import できるようにパスを追加
sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

import claude_analyzer
import data_fetcher
import line_notifier
import news_fetcher
import portfolio_tracker
import screener


def load_watchlist() -> dict:
    watchlist_path = Path(__file__).parent.parent / "config" / "watchlist.json"
    return json.loads(watchlist_path.read_text(encoding="utf-8"))


def main() -> None:
    print("=== AI株式リサーチBot 起動 ===")

    try:
        # Step 1: ウォッチリスト読み込み
        watchlist = load_watchlist()
        stocks = watchlist["stocks"]
        print(f"[main] ウォッチリスト: {len(stocks)} 銘柄")

        # Step 2: 市場データ取得
        print("[main] 市場データ取得中...")
        market_data = data_fetcher.fetch_market_data()
        print(f"[main] 日経平均: {market_data.get('nikkei')} ({market_data.get('nikkei_change')}%)")

        # Step 3: スクリーニング
        print("[main] スクリーニング中...")
        screened = screener.screen(stocks)
        print(f"[main] スクリーニング通過: {len(screened)} 銘柄")

        if not screened:
            print("[main] スクリーニング通過銘柄なし。処理を終了します。")
            return

        # Step 4: 各銘柄の詳細データ取得 & ニュース付与
        print("[main] 銘柄詳細データ & ニュース取得中...")
        enriched_stocks = []
        for stock in screened:
            stock_data = data_fetcher.fetch_stock_data(stock["code"])
            news = news_fetcher.fetch_news_for_stock(stock["code"], stock["name"])
            enriched_stocks.append({**stock, **stock_data, "news": news})

        # Step 5: Claude 分析
        print("[main] Claude による分析中...")
        analysis = claude_analyzer.analyze(enriched_stocks, market_data)
        print(f"[main] 市場状況: {analysis.get('market_condition')}")

        # Step 6: ポートフォリオ確認
        print("[main] ポートフォリオ確認中...")
        portfolio_result = portfolio_tracker.check_portfolio()

        # Step 7: LINE 送信
        print("[main] LINE 送信中...")
        line_notifier.send_daily_report(analysis, portfolio_result)
        print("[main] 送信完了")

    except Exception as e:
        print(f"[main] エラー発生: {e}", file=sys.stderr)
        try:
            line_notifier.send_error_notification(str(e))
        except Exception as notify_err:
            print(f"[main] エラー通知の送信にも失敗: {notify_err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

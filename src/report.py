"""
report.py
AI株式リサーチレポートの生成と送信を行う共通モジュール。
GitHub Actions（main.py）とWebhookオンデマンド更新（webhook_server.py）の両方から呼び出す。
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import claude_analyzer
import data_fetcher
import line_notifier
import news_fetcher
import portfolio_tracker
import screener
import signal_tracker


def _load_watchlist() -> dict:
    watchlist_path = Path(__file__).parent.parent / "config" / "watchlist.json"
    return json.loads(watchlist_path.read_text(encoding="utf-8"))


def run_report() -> None:
    """スクリーニング → Claude 分析 → LINE 送信のフルパイプラインを実行する。"""
    print("=== AI株式リサーチBot 起動 ===")

    # Step 1: ウォッチリスト読み込み
    watchlist = _load_watchlist()
    stocks = watchlist["stocks"]
    print(f"[report] ウォッチリスト: {len(stocks)} 銘柄")

    # Step 2: 市場データ取得（日経SMAトレンド含む）
    print("[report] 市場データ取得中...")
    market_data = data_fetcher.fetch_market_data()
    print(
        f"[report] 日経平均: {market_data.get('nikkei')} ({market_data.get('nikkei_change')}%) "
        f"SMA25比: {market_data.get('nikkei_vs_sma25_pct'):+.1f}% "
        f"トレンド: {market_data.get('nikkei_trend')}"
    )

    # Step 3: スクリーニング（market_data を渡してトレンドフィルターを適用）
    print("[report] スクリーニング中...")
    screened = screener.screen(stocks, market_data)
    print(f"[report] スクリーニング通過: {len(screened)} 銘柄")

    if not screened:
        print("[report] スクリーニング通過銘柄なし。")
        line_notifier.push_message([{
            "type": "text",
            "text": "📊 本日はスクリーニング通過銘柄がありませんでした。",
        }])
        return

    # Step 4: 各銘柄の詳細データ取得 & ニュース付与
    print("[report] 銘柄詳細データ & ニュース取得中...")
    enriched_stocks = []
    for stock in screened:
        stock_data = data_fetcher.fetch_stock_data(stock["code"])
        news = news_fetcher.fetch_news_for_stock(stock["code"], stock["name"])
        enriched_stocks.append({**stock, **stock_data, "news": news})

    # Step 5: Claude 分析
    print("[report] Claude による分析中...")
    analysis = claude_analyzer.analyze(enriched_stocks, market_data)
    print(f"[report] 市場状況: {analysis.get('market_condition')}")
    # Claudeの分析結果にもトレンド情報を付与（signal_trackerが参照）
    analysis["nikkei_trend"] = market_data.get("nikkei_trend", "")

    # Step 5.5: 過去シグナルの結果更新 & 今日のシグナル記録
    print("[report] シグナル記録・勝率更新中...")
    try:
        closed = signal_tracker.update_signal_outcomes()
        signal_tracker.record_signals(analysis)
        summary = signal_tracker.get_win_rate_summary()
        win_rate_str = f"{summary['win_rate']}%" if summary["win_rate"] is not None else "集計中"
        print(
            f"[report] バックテスト: 勝率 {win_rate_str} "
            f"(勝:{summary['wins']} 負:{summary['losses']} 保留:{summary['open']})"
        )
        if closed:
            print(f"[report] 今回クローズ: {[s['code'] + '→' + s['status'] for s in closed]}")
    except Exception as e:
        print(f"[report] シグナル記録失敗（続行）: {e}")

    # Step 6: ポートフォリオ確認
    print("[report] ポートフォリオ確認中...")
    portfolio_result = portfolio_tracker.check_portfolio()

    # Step 7: LINE 送信
    print("[report] LINE 送信中...")
    line_notifier.send_daily_report(analysis, portfolio_result)
    print("[report] 送信完了")

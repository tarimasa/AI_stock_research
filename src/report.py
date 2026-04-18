"""
report.py
AI株式リサーチレポートの生成と送信を行う共通モジュール。
GitHub Actions（main.py）とWebhookオンデマンド更新（webhook_server.py）の両方から呼び出す。

J-Quants 統合:
- FULL_SCAN_ENABLED=true のとき run_full_scan() による全銘柄スキャンを使用
- FULL_SCAN_ENABLED=false（デフォルト）のときは従来のウォッチリスト方式
- 移行期間中は両方を並行稼働して差分ログを出力（SCAN_COMPARE=true）
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import backtest_logger
import claude_analyzer
import data_fetcher
import line_notifier
import macro_preprocessor
import news_fetcher
import portfolio_tracker
import price_calculator
import screener
import signal_tracker

# フルスキャンモード切り替えフラグ
FULL_SCAN_ENABLED = os.environ.get("FULL_SCAN_ENABLED", "false").lower() == "true"
# 移行期間中の並行比較フラグ
SCAN_COMPARE = os.environ.get("SCAN_COMPARE", "false").lower() == "true"


def _load_watchlist() -> dict:
    watchlist_path = Path(__file__).parent.parent / "config" / "watchlist.json"
    return json.loads(watchlist_path.read_text(encoding="utf-8"))


def run_report() -> None:
    """スクリーニング → Claude 分析 → LINE 送信のフルパイプラインを実行する。"""
    print("=== AI株式リサーチBot 起動 ===")
    print(f"[report] モード: {'全銘柄スキャン' if FULL_SCAN_ENABLED else 'ウォッチリスト'}")

    # Step 1: 市場データ取得（日経SMAトレンド含む）
    print("[report] 市場データ取得中...")
    market_data = data_fetcher.fetch_market_data()
    print(
        f"[report] 日経平均: {market_data.get('nikkei')} ({market_data.get('nikkei_change')}%) "
        f"SMA25比: {market_data.get('nikkei_vs_sma25_pct'):+.1f}% "
        f"トレンド: {market_data.get('nikkei_trend')}"
    )

    # Step 1.5: マクロ前処理（VIX・米株・金・原油・金利フラグを生成）
    # Layer 2: 海外投資家動向をマクロフラグに追加
    try:
        foreign = macro_preprocessor.get_foreign_investor_trend()
        market_data["foreign_flag"] = foreign["flag"]
        print(f"[report] 海外投資家: {foreign['flag']}")
    except Exception as e:
        print(f"[report] 海外投資家動向取得失敗（続行）: {e}")

    macro_result = macro_preprocessor.preprocess_macro(market_data)
    print(f"[report] マクロ判定: {macro_result['condition']} / {macro_result['flags_text']}")

    # Step 2: スクリーニング（モードによって切り替え）
    scan_info = None
    if FULL_SCAN_ENABLED:
        screened = _run_fullscan_mode(market_data)
        stage1_count = len(screened)
        scan_info = f"J-Quants全銘柄スキャン → Stage1通過 {stage1_count}件"
    else:
        screened = _run_watchlist_mode(market_data)

    # 移行期間: 両方を並行稼働して差分を比較
    if SCAN_COMPARE and not FULL_SCAN_ENABLED:
        _compare_scan_results(screened, market_data)

    if not screened:
        print("[report] スクリーニング通過銘柄なし。")
        line_notifier.push_message([{
            "type": "text",
            "text": "📊 本日はスクリーニング通過銘柄がありませんでした。",
        }])
        return

    # Step 3: 各銘柄の候補価格を事前計算（LLMの計算ミス排除）
    vix = macro_result.get("vix", 20.0)
    for stock in screened:
        current_price = (
            stock.get("price")
            or stock.get("close")
            or stock.get("current_price")
            or 0
        )
        sma25 = stock.get("sma25")
        if current_price > 0:
            stock["price_candidates"] = price_calculator.calc_all_candidates(
                current_price, sma25, vix=vix
            )

    # Step 4: 各銘柄の詳細データ取得 & ニュース付与
    print("[report] 銘柄詳細データ & ニュース取得中...")
    enriched_stocks = []
    for stock in screened:
        # フルスキャン銘柄は .T なしの4桁コードになっている場合がある
        code_for_news = stock.get("code", "")
        if not code_for_news.endswith(".T") and len(code_for_news) == 4:
            code_for_news = f"{code_for_news}.T"

        stock_data = {}
        try:
            stock_data = data_fetcher.fetch_stock_data(code_for_news)
        except Exception as e:
            print(f"[report] {code_for_news} 詳細取得失敗（続行）: {e}")

        news = []
        try:
            name = stock.get("name", stock.get("code", ""))
            news = news_fetcher.fetch_news_for_stock(code_for_news, name)
        except Exception:
            pass

        enriched_stocks.append({**stock, **stock_data, "news": news})

    # Step 4.5: マーケット全体ニュース取得
    print("[report] 市場ニュース取得中...")
    market_news = []
    try:
        market_news = news_fetcher.fetch_market_news(hours=16, max_headlines=15)
        print(f"[report] ニュース取得: {len(market_news)}件")
    except Exception as e:
        print(f"[report] ニュース取得失敗（続行）: {e}")

    # Step 5: Claude 分析
    print("[report] Claude による分析中...")
    analysis = claude_analyzer.analyze(enriched_stocks, market_data, market_news, macro_result)
    print(f"[report] 市場状況: {analysis.get('market_condition')}")
    analysis["nikkei_trend"] = market_data.get("nikkei_trend", "")

    # Step 5.5: シグナル記録・勝率更新
    print("[report] シグナル記録・勝率更新中...")
    try:
        closed = signal_tracker.update_signal_outcomes()
        signal_tracker.record_signals(analysis)
        backtest_logger.log_recommendations(analysis, enriched_stocks, macro_result)
        backtest_logger.update_outcomes(closed)
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
    line_notifier.send_daily_report(analysis, portfolio_result, scan_info=scan_info)
    print("[report] 送信完了")


def _run_watchlist_mode(market_data: dict) -> list:
    """従来のウォッチリスト方式でスクリーニングする。"""
    watchlist = _load_watchlist()
    stocks = watchlist["stocks"]
    print(f"[report] ウォッチリスト: {len(stocks)} 銘柄")
    print("[report] スクリーニング中（ウォッチリスト方式）...")
    screened = screener.screen(stocks, market_data)
    print(f"[report] スクリーニング通過: {len(screened)} 銘柄")
    return screened


def _run_fullscan_mode(market_data: dict) -> list:
    """全銘柄スキャン方式でスクリーニングする（Layer 4 / Phase 3）。
    データ取得失敗時はウォッチリストモードにフォールバックする。
    """
    print("[report] スクリーニング中（全銘柄スキャン方式）...")
    candidates = screener.run_full_scan()

    if not candidates:
        print("[report] フルスキャン結果なし → ウォッチリストモードにフォールバック")
        return _run_watchlist_mode(market_data)

    print(f"[report] Stage 1 通過: {len(candidates)} 銘柄")

    for c in candidates:
        # price フィールドを close から補完
        if "price" not in c and "close" in c:
            c["price"] = c["close"]
        # フィールド名をウォッチリスト形式に統一（Claude プロンプト互換）
        if "dvs" in c and "directional_vol_score" not in c:
            c["directional_vol_score"] = c["dvs"]
        if "w52_pos" in c and "week52_pos_pct" not in c:
            c["week52_pos_pct"] = c["w52_pos"]
        if "rsi14" in c and "rsi_14" not in c:
            c["rsi_14"] = c["rsi14"]

    return candidates


def _compare_scan_results(watchlist_results: list, market_data: dict) -> None:
    """移行期間中: 旧スクリーナーと全銘柄スキャンを比較して差分をログ出力する。"""
    try:
        print("[report] [移行比較] 全銘柄スキャンを並行実行中...")
        new_candidates = screener.run_full_scan()
        old_codes = {c.get("code", "")[:4] for c in watchlist_results}
        new_codes = {c.get("code", "")[:4] for c in new_candidates}
        print(f"[report] [移行比較] 旧のみ: {old_codes - new_codes}")
        print(f"[report] [移行比較] 新のみ: {new_codes - old_codes}")
        print(f"[report] [移行比較] 共通: {old_codes & new_codes}")
    except Exception as e:
        print(f"[report] [移行比較] 失敗（続行）: {e}")

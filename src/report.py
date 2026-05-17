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
import noon_screener
import portfolio_tracker
import price_calculator
import screener
import sector_filter
import signal_tracker
from master_manager import get_master

# フルスキャンモード切り替えフラグ
FULL_SCAN_ENABLED = os.environ.get("FULL_SCAN_ENABLED", "false").lower() == "true"
# 移行期間中の並行比較フラグ
SCAN_COMPARE = os.environ.get("SCAN_COMPARE", "false").lower() == "true"
# セクター集中上限（推奨+保有合計、1セクターあたり）
MAX_PER_SECTOR = int(os.environ.get("MAX_PER_SECTOR", 1))


def _load_watchlist() -> dict:
    watchlist_path = Path(__file__).parent.parent / "config" / "watchlist.json"
    return json.loads(watchlist_path.read_text(encoding="utf-8"))


def _build_name_lookup(enriched_stocks: list) -> dict:
    """
    4桁コード → 正式名称 の辞書を構築する（優先順位: enriched_stocks > master > watchlist）。
    Claudeが返す name は LLM の幻覚で誤ることがあるため、この辞書で上書きする。
    """
    lookup: dict = {}

    try:
        watchlist = _load_watchlist()
        for item in watchlist.get("stocks", []):
            code4 = item.get("code", "").replace(".T", "")[:4]
            name = item.get("name", "")
            if code4 and name:
                lookup[code4] = name
    except Exception:
        pass

    try:
        master = get_master()
        for code4, info in master.items():
            name = info.get("name", "")
            if code4 and name:
                lookup[code4] = name
    except Exception:
        pass

    # enriched_stocks を最優先（フルスキャンで使われたその日の名称と一致させるため）
    for s in enriched_stocks or []:
        code4 = str(s.get("code", "")).replace(".T", "")[:4]
        name = s.get("name", "") or ""
        if code4 and name:
            lookup[code4] = name

    return lookup


def _build_authoritative_name_lookup() -> dict:
    """
    enriched_stocks に依存しない権威ソース（watchlist + master）のみで lookup を構築する。
    enriched_stocks の name が空のときに信頼できる引き元として使う。
    """
    lookup: dict = {}
    try:
        watchlist = _load_watchlist()
        for item in watchlist.get("stocks", []):
            code4 = item.get("code", "").replace(".T", "")[:4]
            name = item.get("name", "")
            if code4 and name:
                lookup[code4] = name
    except Exception:
        pass

    try:
        master = get_master()
        for code4, info in master.items():
            name = info.get("name", "")
            if code4 and name:
                lookup[code4] = name
    except Exception:
        pass

    return lookup


def _fill_stock_names_from_lookup(stocks: list, lookup: dict) -> int:
    """
    stocks の各 dict について name が空なら lookup から充填する。
    Returns: 充填件数。
    """
    if not stocks or not lookup:
        return 0
    filled = 0
    for s in stocks:
        if s.get("name"):
            continue
        raw_code = str(s.get("code", ""))
        code4 = raw_code.replace(".T", "")[:4]
        if not code4 or not code4.isdigit():
            continue
        name = lookup.get(code4)
        if name:
            s["name"] = name
            filled += 1
    return filled


def _diagnose_missing_names(stocks: list, lookup: dict) -> None:
    """
    Stage1 候補のうち lookup にすら無いコードを警告ログ出力する。
    マスターキャッシュが古い／J-Quants が当該銘柄を返していない可能性を可視化する。
    """
    missing = []
    for s in stocks or []:
        code4 = str(s.get("code", "")).replace(".T", "")[:4]
        if not code4 or not code4.isdigit():
            continue
        if code4 not in lookup:
            missing.append(code4)
    if missing:
        print(
            f"[report] WARNING: 名前が引けない銘柄 {len(missing)}件: {missing[:10]}... "
            f"master 強制更新を検討してください。lookup_size={len(lookup)}"
        )


def _fix_recommendation_names(analysis: dict, enriched_stocks: list) -> None:
    """
    Claudeが返した推奨・エグジット警告の銘柄名を権威ソースで上書きする。
    あわせて enriched_stocks (=stage1_stocks) の空 name も lookup で充填する。

    Claude は銘柄名を誤ることがある（特にマイナー銘柄や名前が長い銘柄）ため、
    master_manager と watchlist を使って再引きする。
    """
    lookup = _build_name_lookup(enriched_stocks)
    if not lookup:
        return

    # enriched_stocks の空 name を埋める（Stage1詳細表示用）
    auth_lookup = _build_authoritative_name_lookup()
    filled = _fill_stock_names_from_lookup(enriched_stocks, auth_lookup)
    if filled:
        print(f"[report] Stage1 銘柄名を lookup から充填: {filled}件")
    _diagnose_missing_names(enriched_stocks, auth_lookup)
    # lookup 自体も更新（enriched_stocks に充填された名前を反映）
    lookup = _build_name_lookup(enriched_stocks)

    def _fix_in_list(items: list, label: str) -> None:
        for item in items or []:
            raw_code = str(item.get("code", ""))
            code4 = raw_code.replace(".T", "")[:4]
            if not code4 or not code4.isdigit():
                continue
            authoritative = lookup.get(code4)
            if not authoritative:
                continue
            original = item.get("name", "")
            if original != authoritative:
                if original:
                    print(f"[report] {label} 銘柄名修正: {code4} '{original}' → '{authoritative}'")
                item["name"] = authoritative

    _fix_in_list(analysis.get("recommendations", []), "推奨")
    _fix_in_list(analysis.get("all_recommendations", []), "推奨(全件)")
    _fix_in_list(analysis.get("exit_alerts", []), "エグジット警告")


def _apply_sector_concentration_filter(analysis: dict, portfolio_result: dict) -> None:
    """
    保有銘柄のセクター集中を加味して analysis['recommendations'] を絞り込む。
    除外件数とセクター内訳を caution に追記し、ログに出力する。

    保有データは portfolio_tracker.check_portfolio() の戻り値（holdings 含む）を渡す。
    MAX_PER_SECTOR=1（既定）なら同一セクターは保有 + 推奨で 1 銘柄まで。
    """
    if not analysis.get("recommendations"):
        return

    try:
        watchlist = _load_watchlist()
    except Exception:
        watchlist = {}

    try:
        master = get_master()
    except Exception:
        master = {}

    held_counts = sector_filter.get_held_sector_counts(
        portfolio_result, watchlist, master
    )
    if held_counts:
        held_str = ", ".join(f"{s}×{n}" for s, n in held_counts.items())
        print(f"[report] 保有セクター: {held_str}")

    kept, removed = sector_filter.filter_by_sector_concentration(
        analysis["recommendations"],
        held_counts,
        max_per_sector=MAX_PER_SECTOR,
        watchlist=watchlist,
        master=master,
    )

    if removed:
        excluded_lines = [
            f"{r.get('code', '')[:4]}({r.get('_excluded_sector', '?')})"
            for r in removed
        ]
        notice = f"セクター集中で除外: {', '.join(excluded_lines)}"
        print(f"[report] {notice} (max_per_sector={MAX_PER_SECTOR})")

        existing = analysis.get("caution") or ""
        analysis["caution"] = (
            f"{existing} / {notice}".strip(" /") if existing else notice
        )

        # 除外された推奨も all_recommendations に保持（Stage1 詳細表示用）
        for r in removed:
            r["_invalid"] = True
            r["_invalid_reason"] = f"セクター{r.get('_excluded_sector', '')}集中で除外"

    analysis["recommendations"] = kept


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
        # Flex Message で送信してボタンを表示する
        empty_analysis = {
            "market_condition": macro_result.get("condition", "注意"),
            "market_comment": "本日はスクリーニング通過銘柄がありませんでした。",
            "recommendations": [],
            "exit_alerts": [],
            "caution": None,
        }
        line_notifier.send_daily_report(empty_analysis, {"holdings": []}, scan_info=scan_info)
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
        atr14 = stock.get("atr14")
        if current_price > 0:
            stock["price_candidates"] = price_calculator.calc_all_candidates(
                current_price, sma25, vix=vix, atr14=atr14
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

    # Step 5.1: 銘柄名を権威ソースで上書き（Claude の幻覚対策）
    _fix_recommendation_names(analysis, enriched_stocks)

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

    # Step 6.1: セクター集中フィルタ（保有銘柄を考慮して推奨を絞る）
    _apply_sector_concentration_filter(analysis, portfolio_result)

    # Step 7: LINE 送信
    print("[report] LINE 送信中...")
    line_notifier.send_daily_report(
        analysis, portfolio_result, scan_info=scan_info,
        stage1_stocks=enriched_stocks if FULL_SCAN_ENABLED else None,
    )
    print("[report] 送信完了")


def run_noon_report() -> None:
    """
    昼休み（11:30〜12:30 JST）中に実行する後場寄付向けレポート。

    朝レポートとの違い:
      - 朝: 前日終値ベースのスクリーニング → 9:00 寄付 IFDOCO 発注向け
      - 昼: 前場（9:00〜11:30）の値動きを織り込んだ再スクリーニング → 12:30 後場寄付 IFDOCO 発注向け

    処理フロー:
      1. 市場データ取得（朝と同じ）
      2. Stage1 スクリーニング（朝と同じ、日足ベース）
      3. noon_screener.apply_noon_filter() で前場データを付与・再スコア
      4. Claude 分析（朝と同じプロンプト、ただし current_price が前場引値に変わる）
      5. LINE 送信（「後場寄付向け」ヘッダー付き）
    """
    print("=== AI株式リサーチBot 起動（昼・後場寄付向け）===")
    print(f"[noon] モード: {'全銘柄スキャン' if FULL_SCAN_ENABLED else 'ウォッチリスト'}")

    # Step 1: 市場データ取得
    print("[noon] 市場データ取得中...")
    market_data = data_fetcher.fetch_market_data()

    # Step 1.5: マクロ前処理
    try:
        foreign = macro_preprocessor.get_foreign_investor_trend()
        market_data["foreign_flag"] = foreign["flag"]
    except Exception as e:
        print(f"[noon] 海外投資家動向取得失敗（続行）: {e}")
    macro_result = macro_preprocessor.preprocess_macro(market_data)
    print(f"[noon] マクロ判定: {macro_result['condition']} / {macro_result['flags_text']}")

    # Step 2: Stage1 スクリーニング（朝と同じ日足ベース）
    scan_info = None
    if FULL_SCAN_ENABLED:
        stage1 = _run_fullscan_mode(market_data)
        scan_info = f"J-Quants全銘柄スキャン+前場強化 → Stage1通過 {len(stage1)}件"
    else:
        stage1 = _run_watchlist_mode(market_data)

    if not stage1:
        print("[noon] Stage1 通過銘柄なし。")
        empty_analysis = {
            "market_condition": macro_result.get("condition", "注意"),
            "market_comment": "前場スクリーニング通過銘柄がありませんでした。",
            "recommendations": [],
            "exit_alerts": [],
            "caution": None,
        }
        line_notifier.send_daily_report(
            empty_analysis, {"holdings": []},
            scan_info=scan_info, session="noon",
        )
        return

    # Step 3: 前場メトリクスを付与して昼特化再スコア
    print("[noon] 前場メトリクス付与＆昼再スコア中...")
    noon_candidates = noon_screener.apply_noon_filter(stage1)

    if not noon_candidates:
        print("[noon] 昼フィルタ通過銘柄なし。")
        empty_analysis = {
            "market_condition": macro_result.get("condition", "注意"),
            "market_comment": "前場の値動きを踏まえた推奨候補がありませんでした。",
            "recommendations": [],
            "exit_alerts": [],
            "caution": None,
        }
        line_notifier.send_daily_report(
            empty_analysis, {"holdings": []},
            scan_info=scan_info, session="noon",
        )
        return

    # Step 4: 価格候補を再計算（前場引値ベース）
    vix = macro_result.get("vix", 20.0)
    for stock in noon_candidates:
        current_price = stock.get("close") or stock.get("price") or 0
        sma25 = stock.get("sma25")
        atr14 = stock.get("atr14")
        if current_price > 0:
            stock["price_candidates"] = price_calculator.calc_all_candidates(
                current_price, sma25, vix=vix, atr14=atr14
            )
        # stage1_signals に noon_signals を追記（LINE 表示用）
        existing = stock.get("stage1_signals", []) or []
        stock["stage1_signals"] = existing + stock.get("noon_signals", [])

    # Step 5: Claude 分析
    print("[noon] Claude 分析中...")
    market_news = []
    try:
        market_news = news_fetcher.fetch_market_news(hours=6, max_headlines=10)
    except Exception:
        pass
    analysis = claude_analyzer.analyze(noon_candidates, market_data, market_news, macro_result)
    analysis["nikkei_trend"] = market_data.get("nikkei_trend", "")

    # Step 5.1: 銘柄名を権威ソースで上書き
    _fix_recommendation_names(analysis, noon_candidates)

    # Step 6: ポートフォリオ確認
    portfolio_result = portfolio_tracker.check_portfolio()

    # Step 6.1: セクター集中フィルタ
    _apply_sector_concentration_filter(analysis, portfolio_result)

    # Step 7: LINE 送信
    line_notifier.send_daily_report(
        analysis, portfolio_result,
        scan_info=scan_info,
        stage1_stocks=noon_candidates if FULL_SCAN_ENABLED else None,
        session="noon",
    )
    print("[noon] 送信完了")


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

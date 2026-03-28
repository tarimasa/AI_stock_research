"""
test_dry_run.py
DRY_RUN=true で全モジュールが正常動作することを確認するテスト。
portfolio_store.py はローカルファイルフォールバックで動作確認。
"""

import json
import os
import sys
from pathlib import Path

import pytest

# DRY_RUN を強制設定
os.environ["DRY_RUN"] = "true"
# Azure 接続文字列を未設定にしてローカルフォールバックを使わせる
os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)

# src/ を import パスに追加
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import claude_analyzer
import data_fetcher
import line_notifier
import news_fetcher
import portfolio_store
import portfolio_tracker
import screener
from webhook_server import execute_command, parse_command


# ─────────────────────────────────────────
# portfolio_store
# ─────────────────────────────────────────

class TestPortfolioStore:
    def test_load_returns_dict(self):
        portfolio = portfolio_store.load_portfolio()
        assert isinstance(portfolio, dict)
        assert "holdings" in portfolio
        assert "default_alerts" in portfolio

    def test_save_and_load(self, tmp_path, monkeypatch):
        local_file = tmp_path / "portfolio_local.json"
        monkeypatch.setattr(portfolio_store, "_LOCAL_FALLBACK", local_file)
        test_data = {
            "default_alerts": {"profit_pct": 15, "loss_pct": -8, "rsi_overbought": 70, "rsi_oversold": 30},
            "holdings": [
                {"code": "7203.T", "name": "トヨタ自動車", "shares": 100, "buy_price": 2650,
                 "buy_date": "2026-01-15", "target_price": None, "stop_loss_pct": -8, "memo": ""}
            ],
        }
        portfolio_store.save_portfolio(test_data)
        loaded = portfolio_store.load_portfolio()
        assert loaded["holdings"][0]["code"] == "7203.T"


# ─────────────────────────────────────────
# data_fetcher
# ─────────────────────────────────────────

class TestDataFetcher:
    def test_fetch_stock_data_dry_run(self):
        result = data_fetcher.fetch_stock_data("7203.T")
        assert "price" in result
        assert "rsi_14" in result
        assert "ma25_diff_pct" in result

    def test_fetch_market_data_dry_run(self):
        market = data_fetcher.fetch_market_data()
        assert "nikkei" in market
        assert "usdjpy" in market


# ─────────────────────────────────────────
# screener
# ─────────────────────────────────────────

class TestScreener:
    STOCKS = [
        {"code": "7203.T", "name": "トヨタ自動車", "sector": "自動車"},
        {"code": "6758.T", "name": "ソニーグループ", "sector": "電機"},
        {"code": "9984.T", "name": "ソフトバンクグループ", "sector": "通信"},
    ]

    def test_screen_returns_list(self):
        result = screener.screen(self.STOCKS)
        assert isinstance(result, list)
        assert len(result) <= len(self.STOCKS)

    def test_screen_sorted_by_score(self):
        result = screener.screen(self.STOCKS)
        scores = [s["score"] for s in result]
        assert scores == sorted(scores, reverse=True)


# ─────────────────────────────────────────
# news_fetcher
# ─────────────────────────────────────────

class TestNewsFetcher:
    def test_fetch_news_dry_run(self):
        news = news_fetcher.fetch_news_for_stock("7203.T", "トヨタ自動車")
        assert isinstance(news, list)
        assert len(news) > 0
        assert "title" in news[0]
        assert "published" in news[0]


# ─────────────────────────────────────────
# claude_analyzer
# ─────────────────────────────────────────

class TestClaudeAnalyzer:
    STOCKS = [
        {"code": "7203.T", "name": "トヨタ自動車", "sector": "自動車", "score": 45},
        {"code": "6758.T", "name": "ソニーグループ", "sector": "電機", "score": 40},
    ]
    MARKET = {"nikkei": 38500, "nikkei_change": -0.5, "usdjpy": 148.5}

    def test_analyze_dry_run(self):
        result = claude_analyzer.analyze(self.STOCKS, self.MARKET)
        assert "market_condition" in result
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)

    def test_recommendations_have_required_fields(self):
        result = claude_analyzer.analyze(self.STOCKS, self.MARKET)
        for rec in result["recommendations"]:
            assert "code" in rec
            assert "name" in rec
            assert "action" in rec
            assert "current_price" in rec
            assert "target_price" in rec


# ─────────────────────────────────────────
# portfolio_tracker
# ─────────────────────────────────────────

class TestPortfolioTracker:
    def test_check_portfolio_empty(self, monkeypatch):
        monkeypatch.setattr(
            portfolio_store, "load_portfolio",
            lambda: {"default_alerts": {"profit_pct": 15, "loss_pct": -8, "rsi_overbought": 70, "rsi_oversold": 30}, "holdings": []}
        )
        result = portfolio_tracker.check_portfolio()
        assert result == {}

    def test_check_portfolio_with_holding(self, monkeypatch):
        monkeypatch.setattr(
            portfolio_store, "load_portfolio",
            lambda: {
                "default_alerts": {"profit_pct": 15, "loss_pct": -8, "rsi_overbought": 70, "rsi_oversold": 30},
                "holdings": [
                    {"code": "7203.T", "name": "トヨタ自動車", "shares": 100, "buy_price": 2650,
                     "buy_date": "2026-01-15", "target_price": 3100, "stop_loss_pct": -8, "memo": ""}
                ],
            }
        )
        result = portfolio_tracker.check_portfolio()
        assert "holdings" in result
        assert len(result["holdings"]) == 1
        assert "unrealized_pnl" in result["holdings"][0]
        assert "alert" in result["holdings"][0]


# ─────────────────────────────────────────
# line_notifier
# ─────────────────────────────────────────

class TestLineNotifier:
    ANALYSIS = {
        "market_condition": "注意",
        "market_comment": "日経平均は小幅下落。",
        "recommendations": [
            {
                "rank": 1, "code": "7203.T", "name": "トヨタ自動車",
                "action": "今すぐ買う", "current_price": 2850, "target_price": 3100,
                "upside_pct": 8.8, "reason": "テクニカルが好転。", "risk_level": 2,
                "risk_comment": "為替リスクあり",
            }
        ],
        "caution": None,
    }
    PORTFOLIO = {}

    def test_build_report_text(self):
        text = line_notifier.build_report_text(self.ANALYSIS, self.PORTFOLIO)
        assert "トヨタ自動車" in text
        assert "今すぐ買う" in text

    def test_send_daily_report_dry_run(self, capsys):
        line_notifier.send_daily_report(self.ANALYSIS, self.PORTFOLIO)
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out or "トヨタ" in captured.out


# ─────────────────────────────────────────
# webhook_server コマンドパース
# ─────────────────────────────────────────

class TestParseCommand:
    def test_add_command(self):
        cmd = parse_command("追加 7203 100 2650")
        assert cmd == {"action": "add", "code": "7203.T", "shares": 100, "price": 2650}

    def test_add_command_with_units(self):
        cmd = parse_command("追加 7203 100株 2650円")
        assert cmd == {"action": "add", "code": "7203.T", "shares": 100, "price": 2650}

    def test_remove_command(self):
        cmd = parse_command("削除 7203")
        assert cmd == {"action": "remove", "code": "7203.T"}

    def test_list_command(self):
        cmd = parse_command("一覧")
        assert cmd == {"action": "list"}

    def test_help_command(self):
        cmd = parse_command("ヘルプ")
        assert cmd == {"action": "help"}

    def test_unknown_command_returns_none(self):
        cmd = parse_command("こんにちは")
        assert cmd is None

    def test_fullwidth_space(self):
        cmd = parse_command("追加\u30007203\u3000100\u30002650")
        assert cmd is not None
        assert cmd["action"] == "add"


# ─────────────────────────────────────────
# メイン実行
# ─────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])

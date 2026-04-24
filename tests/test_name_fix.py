"""
test_name_fix.py
Claude 出力の銘柄名を権威ソース（master_manager + watchlist + enriched_stocks）で
上書きする処理の回帰テスト。
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["DRY_RUN"] = "true"
os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import report


class TestBuildNameLookup:
    def test_lookup_from_watchlist(self):
        """watchlist.json の銘柄名が取得できる。"""
        lookup = report._build_name_lookup([])
        assert "7203" in lookup
        assert "トヨタ" in lookup["7203"]

    def test_enriched_overrides(self):
        """enriched_stocks の名前が watchlist より優先される。"""
        enriched = [{"code": "7203.T", "name": "カスタム名"}]
        lookup = report._build_name_lookup(enriched)
        assert lookup["7203"] == "カスタム名"

    def test_4digit_and_dot_t_both_normalized(self):
        """'5599' と '5599.T' の両方が同じキーに正規化される。"""
        enriched = [
            {"code": "5599", "name": "ザ・グローバル社"},
        ]
        lookup = report._build_name_lookup(enriched)
        assert lookup["5599"] == "ザ・グローバル社"

    def test_empty_name_skipped(self):
        """enriched_stocks の空名はスキップされる（上書きされない）。"""
        enriched = [
            {"code": "7203.T", "name": ""},
        ]
        lookup = report._build_name_lookup(enriched)
        # watchlist の '7203' のトヨタが残る
        assert "7203" in lookup
        assert "トヨタ" in lookup["7203"]


class TestFixRecommendationNames:
    def test_claude_hallucinated_name_fixed(self):
        """Claude が誤った名前を返した場合、権威ソースで上書きされる。"""
        analysis = {
            "recommendations": [
                {"code": "7203.T", "name": "誤った名前", "action": "今すぐ買う"},
            ],
            "all_recommendations": [
                {"code": "7203.T", "name": "誤った名前"},
            ],
            "exit_alerts": [],
        }
        report._fix_recommendation_names(analysis, [])
        assert "トヨタ" in analysis["recommendations"][0]["name"]
        assert "トヨタ" in analysis["all_recommendations"][0]["name"]

    def test_non_watchlist_code_with_enriched(self):
        """watchlist 外のコードは enriched_stocks から名前を引ける。"""
        enriched = [{"code": "5599", "name": "ザ・グローバル社", "sector": "不動産"}]
        analysis = {
            "recommendations": [
                {"code": "5599.T", "name": "Claude幻覚名", "action": "今すぐ買う"},
            ],
            "exit_alerts": [],
        }
        report._fix_recommendation_names(analysis, enriched)
        assert analysis["recommendations"][0]["name"] == "ザ・グローバル社"

    def test_unknown_code_keeps_original(self):
        """どの権威ソースにもないコードは原文のまま残す。"""
        analysis = {
            "recommendations": [
                {"code": "9999.T", "name": "未知銘柄", "action": "今すぐ買う"},
            ],
            "exit_alerts": [],
        }
        report._fix_recommendation_names(analysis, [])
        assert analysis["recommendations"][0]["name"] == "未知銘柄"

    def test_exit_alerts_also_fixed(self):
        """exit_alerts の名前も上書きされる。"""
        analysis = {
            "recommendations": [],
            "exit_alerts": [
                {"code": "6758.T", "name": "間違ったソニー名",
                 "alert_type": "利確", "message": "", "suggested_action": ""},
            ],
        }
        report._fix_recommendation_names(analysis, [])
        assert "ソニー" in analysis["exit_alerts"][0]["name"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

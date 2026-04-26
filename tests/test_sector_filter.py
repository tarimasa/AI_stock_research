"""
test_sector_filter.py
保有銘柄のセクター集中で推奨銘柄を絞る sector_filter モジュールの単体テスト。
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import sector_filter


WATCHLIST = {
    "stocks": [
        {"code": "7203.T", "name": "トヨタ自動車", "sector": "自動車"},
        {"code": "7267.T", "name": "ホンダ", "sector": "自動車"},
        {"code": "8306.T", "name": "三菱UFJ", "sector": "金融"},
        {"code": "8316.T", "name": "三井住友FG", "sector": "金融"},
        {"code": "9201.T", "name": "JAL", "sector": "航空"},
    ]
}

# master の sector33（J-Quants 公式分類）優先
MASTER = {
    "7203": {"name": "トヨタ", "sector33": "輸送用機器"},
    "7267": {"name": "ホンダ", "sector33": "輸送用機器"},
    "8306": {"name": "三菱UFJ", "sector33": "銀行業"},
    "8316": {"name": "三井住友FG", "sector33": "銀行業"},
    "9201": {"name": "JAL",     "sector33": "空運業"},
    "9202": {"name": "ANA",     "sector33": "空運業"},
}


class TestGetSectorForCode:
    def test_master_takes_priority(self):
        # master の sector33 が watchlist より優先される
        sector = sector_filter.get_sector_for_code("7203.T", WATCHLIST, MASTER)
        assert sector == "輸送用機器"

    def test_falls_back_to_watchlist(self):
        # master に無いコードは watchlist にフォールバック
        master_partial = {}
        sector = sector_filter.get_sector_for_code("7203.T", WATCHLIST, master_partial)
        assert sector == "自動車"

    def test_unknown_code_returns_empty(self):
        sector = sector_filter.get_sector_for_code("9999.T", WATCHLIST, MASTER)
        assert sector == ""

    def test_4digit_and_dot_t_normalized(self):
        sector1 = sector_filter.get_sector_for_code("7203", WATCHLIST, MASTER)
        sector2 = sector_filter.get_sector_for_code("7203.T", WATCHLIST, MASTER)
        assert sector1 == sector2 == "輸送用機器"


class TestGetHeldSectorCounts:
    def test_counts_by_sector(self):
        portfolio = {
            "holdings": [
                {"code": "7203.T", "name": "トヨタ"},
                {"code": "7267.T", "name": "ホンダ"},
                {"code": "8306.T", "name": "三菱UFJ"},
            ]
        }
        counts = sector_filter.get_held_sector_counts(portfolio, WATCHLIST, MASTER)
        assert counts["輸送用機器"] == 2
        assert counts["銀行業"] == 1

    def test_empty_portfolio(self):
        counts = sector_filter.get_held_sector_counts({}, WATCHLIST, MASTER)
        assert counts == {}

    def test_unknown_codes_not_counted(self):
        portfolio = {"holdings": [{"code": "9999.T"}]}
        counts = sector_filter.get_held_sector_counts(portfolio, WATCHLIST, MASTER)
        assert counts == {}


class TestFilterBySectorConcentration:
    def test_remove_recommendation_when_sector_already_held(self):
        """銀行業を1銘柄保有中、別の銀行株が推奨されたら除外。"""
        held_counts = {"銀行業": 1}
        recs = [
            {"code": "8316.T", "name": "三井住友FG"},  # 銀行業 = 既保有 → 除外
            {"code": "7203.T", "name": "トヨタ"},      # 輸送用機器 = 未保有 → 通過
        ]
        kept, removed = sector_filter.filter_by_sector_concentration(
            recs, held_counts, max_per_sector=1,
            watchlist=WATCHLIST, master=MASTER,
        )
        assert len(kept) == 1
        assert kept[0]["code"] == "7203.T"
        assert len(removed) == 1
        assert removed[0]["code"] == "8316.T"
        assert removed[0]["_excluded_sector"] == "銀行業"
        assert removed[0]["_excluded_reason"] == "sector_concentration"

    def test_max_per_sector_2_allows_one_more(self):
        held_counts = {"銀行業": 1}
        recs = [
            {"code": "8316.T", "name": "三井住友FG"},  # +1 → 2/2 通過
            {"code": "8411.T", "name": "みずほFG"},    # +1 → 3/2 除外
        ]
        # みずほFG は master/watchlist に無いので "" sector → 通過扱い
        # まず watchlist に追加して銀行業セクターを引けるようにする
        wl = {
            "stocks": WATCHLIST["stocks"] + [
                {"code": "8411.T", "name": "みずほFG", "sector": "金融"},
            ]
        }
        master_with_mizuho = {
            **MASTER,
            "8411": {"name": "みずほFG", "sector33": "銀行業"},
        }
        kept, removed = sector_filter.filter_by_sector_concentration(
            recs, held_counts, max_per_sector=2,
            watchlist=wl, master=master_with_mizuho,
        )
        assert len(kept) == 1
        assert kept[0]["code"] == "8316.T"
        assert len(removed) == 1
        assert removed[0]["code"] == "8411.T"

    def test_no_holdings_keeps_all(self):
        recs = [
            {"code": "7203.T"},
            {"code": "7267.T"},
        ]
        kept, removed = sector_filter.filter_by_sector_concentration(
            recs, {}, max_per_sector=1,
            watchlist=WATCHLIST, master=MASTER,
        )
        # 7203/7267 はどちらも 輸送用機器 → 1件通過、もう1件除外
        assert len(kept) == 1
        assert len(removed) == 1

    def test_unknown_sector_passes_through(self):
        """セクター不明の推奨はフィルタ対象外（保守的に通過させる）。"""
        recs = [{"code": "9999.T", "name": "未知"}]
        kept, removed = sector_filter.filter_by_sector_concentration(
            recs, {"輸送用機器": 5}, max_per_sector=1,
            watchlist=WATCHLIST, master=MASTER,
        )
        assert len(kept) == 1
        assert len(removed) == 0

    def test_jal_blocking_ana_recommendation(self):
        """ユーザーシナリオ: JAL 保有中に ANA が推奨されたら除外。"""
        portfolio = {"holdings": [{"code": "9201.T", "name": "JAL"}]}
        held = sector_filter.get_held_sector_counts(portfolio, WATCHLIST, MASTER)
        assert held == {"空運業": 1}

        recs = [{"code": "9202.T", "name": "ANA"}]
        kept, removed = sector_filter.filter_by_sector_concentration(
            recs, held, max_per_sector=1,
            watchlist=WATCHLIST, master=MASTER,
        )
        assert kept == []
        assert len(removed) == 1
        assert removed[0]["_excluded_sector"] == "空運業"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

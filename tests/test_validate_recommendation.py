"""
tests/test_validate_recommendation.py
claude_analyzer.validate_recommendation() のユニットテスト。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from claude_analyzer import validate_recommendation


class TestValidateRecommendation:
    def _rec(self, buy=1000, tp=1050, sl=970, holding=3, **kwargs) -> dict:
        """テスト用の推奨データを返すヘルパー。"""
        base = {
            "buy_price": buy,
            "take_profit_price": tp,
            "stop_loss_price": sl,
            "holding_days": holding,
        }
        base.update(kwargs)
        return base

    # ── 正常ケース ────────────────────────────────────

    def test_valid_rr_passes(self):
        """RR比 1.5 以上は通過する。"""
        # reward = 1050 - 1000 = 50, risk = 1000 - 970 = 30 → RR ≈ 1.67
        rec = validate_recommendation(self._rec(buy=1000, tp=1050, sl=970))
        assert rec.get("_invalid") is False
        assert rec.get("rr_ratio") >= 1.5

    def test_rr_ratio_is_calculated_correctly(self):
        # reward = 100, risk = 50 → RR = 2.0
        rec = validate_recommendation(self._rec(buy=1000, tp=1100, sl=950))
        assert rec["rr_ratio"] == 2.0

    # ── 除外ケース ────────────────────────────────────

    def test_invalid_when_rr_below_1_5(self):
        """RR比 1.5 未満は除外。"""
        # reward = 20, risk = 30 → RR ≈ 0.67
        rec = validate_recommendation(self._rec(buy=1000, tp=1020, sl=970))
        assert rec.get("_invalid") is True
        assert "RR比" in rec.get("_invalid_reason", "")

    def test_invalid_when_buy_price_zero(self):
        rec = validate_recommendation(self._rec(buy=0, tp=1050, sl=970))
        assert rec.get("_invalid") is True

    def test_invalid_when_sl_zero(self):
        rec = validate_recommendation(self._rec(buy=1000, tp=1050, sl=0))
        assert rec.get("_invalid") is True

    def test_invalid_when_sl_above_buy(self):
        """損切りが買値より高い場合は除外。"""
        rec = validate_recommendation(self._rec(buy=1000, tp=1050, sl=1010))
        assert rec.get("_invalid") is True
        assert "損切りが買値以上" in rec.get("_invalid_reason", "")

    # ── 警告ケース ────────────────────────────────────

    def test_warning_when_short_term_sl_over_5pct(self):
        """短期（≤3日）で損切り幅 > 5% は警告。"""
        # buy=1000, sl=940 → sl_pct = 6%
        rec = validate_recommendation(self._rec(buy=1000, tp=1100, sl=940, holding=3))
        assert rec.get("_invalid") is False   # 除外はされない
        assert "_warning" in rec

    def test_no_warning_when_medium_term_sl_over_5pct(self):
        """中期（>3日）は損切り幅 > 5% でも警告なし。"""
        rec = validate_recommendation(self._rec(buy=1000, tp=1100, sl=940, holding=7))
        assert rec.get("_invalid") is False
        assert "_warning" not in rec

    def test_no_warning_when_short_term_sl_under_5pct(self):
        """短期で損切り幅 ≤ 5% は警告なし。"""
        rec = validate_recommendation(self._rec(buy=1000, tp=1100, sl=950, holding=3))
        assert rec.get("_invalid") is False
        assert "_warning" not in rec


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
tests/test_macro_preprocessor.py
macro_preprocessor モジュールのユニットテスト。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from macro_preprocessor import preprocess_macro


class TestPreprocessMacro:
    def _base(self, **kwargs) -> dict:
        """デフォルトの穏やかな市場データを返すヘルパー。"""
        base = {
            "vix": 18.0,
            "vix_trend": "横ばい",
            "sp500_change": 0.0,
            "gold_change": 0.0,
            "oil_change": 0.0,
            "us10y_trend": "横ばい",
            "dow_change": 0.0,
            "nikkei_trend": "上昇",
        }
        base.update(kwargs)
        return base

    # ── condition 判定 ────────────────────────────────

    def test_good_condition_low_vix(self):
        result = preprocess_macro(self._base(vix=15.0))
        assert result["condition"] == "良好"

    def test_caution_when_vix_between_20_and_30(self):
        result = preprocess_macro(self._base(vix=25.0))
        assert result["condition"] == "注意"

    def test_caution_when_sp500_drops_1pct(self):
        result = preprocess_macro(self._base(sp500_change=-1.5))
        assert result["condition"] == "注意"

    def test_deterioration_when_vix_high_and_nikkei_down(self):
        # VIX=25(0.5 negative) + nikkei_down(1.0) + sp500_drop(1.0) = 2.5 → 悪化
        result = preprocess_macro(self._base(vix=25.0, nikkei_trend="下落", sp500_change=-1.5))
        assert result["condition"] == "悪化"

    def test_caution_when_vix_25_and_nikkei_down(self):
        # VIX=25(0.5 negative) + nikkei_down(1.0) = 1.5 → 注意（2.0未満なので悪化にはならない）
        result = preprocess_macro(self._base(vix=25.0, nikkei_trend="下落"))
        assert result["condition"] == "注意"

    def test_deterioration_when_sp500_and_nikkei_both_down(self):
        result = preprocess_macro(self._base(sp500_change=-1.5, nikkei_trend="下落"))
        assert result["condition"] == "悪化"

    # ── risk_adjustment ───────────────────────────────

    def test_risk_adjustment_zero_when_vix_low(self):
        result = preprocess_macro(self._base(vix=18.0))
        assert result["risk_adjustment"] == 0

    def test_risk_adjustment_1_when_vix_ge_30(self):
        result = preprocess_macro(self._base(vix=32.0))
        assert result["risk_adjustment"] == 1

    # ── flags_text ────────────────────────────────────

    def test_flags_text_contains_vix_when_high(self):
        result = preprocess_macro(self._base(vix=35.0))
        assert "VIX" in result["flags_text"]

    def test_flags_text_contains_sp500_when_drops(self):
        result = preprocess_macro(self._base(sp500_change=-1.5))
        assert "S&P500" in result["flags_text"]

    def test_flags_text_no_flags_when_calm(self):
        result = preprocess_macro(self._base())
        assert result["flags_text"] == "特になし"

    def test_gold_risk_off_flag(self):
        result = preprocess_macro(self._base(gold_change=1.5))
        assert "金" in result["flags_text"]

    def test_oil_surge_flag(self):
        result = preprocess_macro(self._base(oil_change=3.5))
        assert "原油" in result["flags_text"]

    def test_nikkei_downtrend_flag(self):
        result = preprocess_macro(self._base(nikkei_trend="下落"))
        assert "日経" in result["flags_text"]

    # ── vix フィールド ────────────────────────────────

    def test_vix_returned_as_float(self):
        result = preprocess_macro(self._base(vix=22.5))
        assert result["vix"] == 22.5

    def test_vix_defaults_to_20_when_missing(self):
        result = preprocess_macro({})
        assert result["vix"] == 20.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

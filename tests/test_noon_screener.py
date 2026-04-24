"""
test_noon_screener.py
noon_screener の昼特化シグナル計算の単体テスト。
前場メトリクスの付与ロジックを DRY_RUN で確認する。
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import noon_screener


class TestCalcNoonSignals:
    def test_drunk_morning_data_passes(self, monkeypatch):
        """前場データが取れた場合、スコアと signals が返る。"""
        # DRY_RUN 時は get_morning_session_metrics が固定値を返す
        stock = {
            "code": "7203.T", "name": "トヨタ",
            "close": 2830, "sma25": 2820,
            "volume": 3_000_000, "stage1_score": 50,
        }
        result = noon_screener._calc_noon_signals(stock)
        assert result is not None
        assert "noon_score" in result
        assert "noon_signals" in result
        assert "morning" in result
        assert result["morning"]["open_9"] == 2840.0

    def test_error_morning_returns_none(self, monkeypatch):
        """前場データが取れない場合 None を返す。"""
        monkeypatch.setattr(
            "noon_screener.get_morning_session_metrics",
            lambda ticker: {"error": "no intraday data"},
        )
        stock = {"code": "5599.T", "name": "テスト銘柄", "close": 1000, "stage1_score": 30}
        result = noon_screener._calc_noon_signals(stock)
        assert result is None

    def test_healthy_gap_scores_positive(self, monkeypatch):
        """+2〜+5% の健全ギャップは noon_score を加点する。"""
        monkeypatch.setattr(
            "noon_screener.get_morning_session_metrics",
            lambda ticker: {
                "date": "2026-04-24",
                "prev_close": 2800.0,
                "open_9": 2880.0,              # +2.86% ギャップ
                "high": 2900.0,
                "low": 2870.0,
                "close_1130": 2895.0,
                "volume": 2_000_000,
                "gap_pct": 2.86,
                "morning_return_pct": 0.52,
                "range_pct": 1.04,
                "bars": 10,
            },
        )
        stock = {"code": "7203.T", "name": "トヨタ", "close": 2800,
                 "volume": 3_000_000, "stage1_score": 50}
        result = noon_screener._calc_noon_signals(stock)
        assert result["noon_score"] > 0
        assert any("健全ギャップ" in s for s in result["noon_signals"])

    def test_bearish_gap_penalty(self, monkeypatch):
        """-2% 超の弱気ギャップはペナルティが入る。"""
        monkeypatch.setattr(
            "noon_screener.get_morning_session_metrics",
            lambda ticker: {
                "date": "2026-04-24",
                "prev_close": 2800.0,
                "open_9": 2700.0,              # -3.57% ギャップダウン
                "high": 2720.0,
                "low": 2680.0,
                "close_1130": 2690.0,
                "volume": 2_000_000,
                "gap_pct": -3.57,
                "morning_return_pct": -0.37,
                "range_pct": 1.48,
                "bars": 10,
            },
        )
        stock = {"code": "7203.T", "name": "トヨタ", "close": 2800,
                 "volume": 3_000_000, "stage1_score": 50}
        result = noon_screener._calc_noon_signals(stock)
        # stage1_score * 0.25 = 12.5 を差し引いても -40 の penalty で負になる
        assert result["noon_score"] < 0
        assert any("弱気ギャップ" in s for s in result["noon_signals"])

    def test_price_updated_to_morning_close(self, monkeypatch):
        """前場引値が close/price フィールドに反映される。"""
        monkeypatch.setattr(
            "noon_screener.get_morning_session_metrics",
            lambda ticker: {
                "date": "2026-04-24",
                "prev_close": 1000.0, "open_9": 1010.0,
                "high": 1030.0, "low": 1008.0, "close_1130": 1025.0,
                "volume": 500_000, "gap_pct": 1.0,
                "morning_return_pct": 1.49, "range_pct": 2.18, "bars": 10,
            },
        )
        stock = {"code": "9999.T", "name": "テスト", "close": 1000,
                 "volume": 1_000_000, "stage1_score": 20}
        result = noon_screener._calc_noon_signals(stock)
        assert result["close"] == 1025.0
        assert result["price"] == 1025.0


class TestApplyNoonFilter:
    def test_empty_input_returns_empty(self):
        assert noon_screener.apply_noon_filter([]) == []

    def test_filters_out_negative_scores(self, monkeypatch):
        """noon_score <= 0 の候補は除外される。"""
        def _metrics(ticker):
            if "1111" in ticker:
                # 弱気ギャップ -5% → negative score
                return {
                    "date": "2026-04-24", "prev_close": 1000, "open_9": 950,
                    "high": 960, "low": 940, "close_1130": 945,
                    "volume": 100_000, "gap_pct": -5.0, "morning_return_pct": -0.5,
                    "range_pct": 2.1, "bars": 10,
                }
            else:
                # 健全ギャップ → positive
                return {
                    "date": "2026-04-24", "prev_close": 1000, "open_9": 1030,
                    "high": 1050, "low": 1025, "close_1130": 1045,
                    "volume": 1_000_000, "gap_pct": 3.0, "morning_return_pct": 1.46,
                    "range_pct": 2.43, "bars": 10,
                }

        monkeypatch.setattr(
            "noon_screener.get_morning_session_metrics", _metrics
        )
        candidates = [
            {"code": "1111", "name": "弱気", "close": 1000, "volume": 500_000, "stage1_score": 30},
            {"code": "2222", "name": "強気", "close": 1000, "volume": 500_000, "stage1_score": 30},
        ]
        result = noon_screener.apply_noon_filter(candidates)
        codes = [r["code"] for r in result]
        assert "2222" in codes
        assert "1111" not in codes


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

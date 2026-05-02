"""
test_stage1_threshold.py
STAGE1_MIN_SCORE 閾値と _calc_stage1_score の単体テスト。

PR #20 ユーザー報告: Stage1 通過が 572 件と多すぎ、20 件選定の意味が薄れる。
対策: STAGE1_MIN_SCORE で「明確な複合 or 強い単一シグナル」だけを通過させる。
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import screener


TUE = 1  # 火曜日 = 曜日ボーナス/ペナルティなし（テスト基準曜日）


class TestCalcStage1Score:
    def test_strong_composite_signal_high_score(self):
        """breakout + dvs正 + rsi5<=20 + 出来高急増 = 200点超."""
        stock = {
            "breakout_5d": True, "dvs": 25, "rsi5": 15, "rsi14": 30,
            "vol_ratio": 1.6, "close": 1000, "sma25": 980,
        }
        score, signals = screener._calc_stage1_score(stock, today_weekday=TUE)
        assert score >= 200
        assert any("breakout+dvs正+rsi5<20" in s for s in signals)
        assert any("複合最優秀" in s for s in signals)

    def test_single_weak_signal_low_score(self):
        """RSI14 が 38（≤40 のみ）= スコア 8（弱い単一シグナル）."""
        stock = {
            "breakout_5d": False, "dvs": 0, "rsi5": 50, "rsi14": 38,
            "vol_ratio": 1.0, "close": 1000, "sma25": 1000,
        }
        score, _ = screener._calc_stage1_score(stock, today_weekday=TUE)
        assert score == 8

    def test_dvs_negative_excluded(self):
        """DVS <= -10 は -200 ペナルティで実質除外."""
        stock = {
            "breakout_5d": True, "dvs": -15, "rsi5": 15, "rsi14": 25,
            "vol_ratio": 2.0, "close": 1000, "sma25": 1000,
        }
        score, signals = screener._calc_stage1_score(stock, today_weekday=TUE)
        assert score < 0
        assert any("dvs負" in s for s in signals)

    def test_zero_signal_zero_score(self):
        """全条件未達なら 0 点（火曜基準）."""
        stock = {
            "breakout_5d": False, "dvs": 0, "rsi5": 50, "rsi14": 50,
            "vol_ratio": 1.0, "close": 1000, "sma25": 1000,
        }
        score, signals = screener._calc_stage1_score(stock, today_weekday=TUE)
        assert score == 0
        assert signals == []

    def test_wednesday_rsi5_20_bonus(self):
        """水曜 + RSI5<20 → +50pt ボーナスが加算される."""
        stock = {
            "breakout_5d": False, "dvs": 15, "rsi5": 18, "rsi14": 40,
            "vol_ratio": 1.0, "close": 1000, "sma25": 1000,
        }
        score_tue, _ = screener._calc_stage1_score(stock, today_weekday=1)  # 火
        score_wed, signals = screener._calc_stage1_score(stock, today_weekday=2)  # 水
        assert score_wed == score_tue + 50
        assert any("水曜+RSI5<20" in s for s in signals)

    def test_monday_penalty(self):
        """月曜エントリーは -40pt ペナルティ."""
        stock = {
            "breakout_5d": False, "dvs": 15, "rsi5": 18, "rsi14": 30,
            "vol_ratio": 1.0, "close": 1000, "sma25": 1000,
        }
        score_tue, _ = screener._calc_stage1_score(stock, today_weekday=1)
        score_mon, signals = screener._calc_stage1_score(stock, today_weekday=0)
        assert score_mon == score_tue - 40
        assert any("月曜エントリー" in s for s in signals)


class TestApplyStage1FiltersThreshold:
    def test_threshold_60_blocks_weak_signals(self, monkeypatch):
        """既定 60 では単一弱シグナル銘柄は通過しない."""
        monkeypatch.setattr(screener, "STAGE1_MIN_SCORE", 60.0)
        stocks = [
            # score 8（RSI14<=40 のみ）→ 除外
            {"code": "1111", "breakout_5d": False, "dvs": 0, "rsi5": 50,
             "rsi14": 38, "vol_ratio": 1.0, "close": 1000, "sma25": 1000},
            # score 200+ → 通過
            {"code": "2222", "breakout_5d": True, "dvs": 25, "rsi5": 15,
             "rsi14": 25, "vol_ratio": 2.0, "close": 1000, "sma25": 980},
        ]
        result = screener._apply_stage1_filters(stocks, today_weekday=TUE)
        codes = [s["code"] for s in result]
        assert "1111" not in codes
        assert "2222" in codes

    def test_threshold_1_passes_weak_signals(self, monkeypatch):
        """閾値 1 では弱い単独シグナル（score=8）も通過."""
        monkeypatch.setattr(screener, "STAGE1_MIN_SCORE", 1.0)
        stocks = [
            {"code": "1111", "breakout_5d": False, "dvs": 0, "rsi5": 50,
             "rsi14": 38, "vol_ratio": 1.0, "close": 1000, "sma25": 1000},
        ]
        result = screener._apply_stage1_filters(stocks, today_weekday=TUE)
        assert len(result) == 1
        assert result[0]["stage1_score"] == 8

    def test_threshold_high_keeps_only_top_signals(self, monkeypatch):
        """閾値 100 では最強シグナル（breakout+dvs+rsi5<20 系）だけ通過."""
        monkeypatch.setattr(screener, "STAGE1_MIN_SCORE", 100.0)
        stocks = [
            # 中程度: breakout のみ = 20点 → 除外
            {"code": "1111", "breakout_5d": True, "dvs": -5, "rsi5": 50,
             "rsi14": 50, "vol_ratio": 1.0, "close": 1000, "sma25": 1000},
            # 強複合: 200+ → 通過
            {"code": "2222", "breakout_5d": True, "dvs": 25, "rsi5": 15,
             "rsi14": 25, "vol_ratio": 2.0, "close": 1000, "sma25": 980},
        ]
        result = screener._apply_stage1_filters(stocks, today_weekday=TUE)
        assert len(result) == 1
        assert result[0]["code"] == "2222"


class TestLogStage1Distribution:
    def test_distribution_buckets_correct(self, capsys):
        """ヒストグラムが正しく計算される."""
        scores = [
            200, 180, 160,        # ≥150: 3
            120, 100,             # 100-150: 2
            80, 70, 65,           # 60-100: 3
            50, 40, 35,           # 30-60: 3
            20, 10, 5,            # 1-30: 3
            0, -10,               # ≤0: 2
        ]
        screener._log_stage1_distribution(scores, threshold=60.0)
        out = capsys.readouterr().out
        assert "≥150=3" in out
        assert "100-150=2" in out
        assert "60-100=3" in out
        assert "30-60=3" in out
        assert "1-30=3" in out
        assert "≤0=2" in out
        # 閾値 60 以上 = 8件 (3+2+3)
        assert "閾値 60.0 以上: 8件" in out

    def test_empty_input_safe(self, capsys):
        screener._log_stage1_distribution([], threshold=60.0)
        out = capsys.readouterr().out
        assert "候補なし" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

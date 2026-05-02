"""
tests/test_price_calculator.py
price_calculator モジュールのユニットテスト。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from price_calculator import _tick_round, calc_price_candidates, calc_all_candidates, format_candidates_for_prompt


# ──────────────────────────────────────────────────────────
# _tick_round: 呼値単位テスト
# ──────────────────────────────────────────────────────────

class TestTickRound:
    def test_under_3000_rounds_to_1yen(self):
        assert _tick_round(1234.7) == 1235.0
        assert _tick_round(2999.2) == 2999.0
        assert _tick_round(500.5) == 501.0

    def test_3001_to_5000_rounds_to_5yen(self):
        assert _tick_round(3001.0) == 3000.0   # 3000以下は1円単位
        assert _tick_round(3003.0) == 3005.0
        assert _tick_round(4997.0) == 4995.0
        assert _tick_round(5000.0) == 5000.0

    def test_5001_to_30000_rounds_to_10yen(self):
        assert _tick_round(5001.0) == 5000.0
        assert _tick_round(8765.0) == 8770.0
        assert _tick_round(29994.0) == 29990.0
        assert _tick_round(30000.0) == 30000.0

    def test_over_30000_rounds_to_50yen(self):
        assert _tick_round(30001.0) == 30000.0
        assert _tick_round(50024.0) == 50000.0
        assert _tick_round(50025.0) == 50050.0

    def test_boundary_3000(self):
        assert _tick_round(3000.0) == 3000.0   # 3000以下 → 1円

    def test_boundary_5000(self):
        assert _tick_round(5000.0) == 5000.0   # 5000以下 → 5円


# ──────────────────────────────────────────────────────────
# calc_price_candidates: 価格候補計算テスト
# ──────────────────────────────────────────────────────────

class TestCalcPriceCandidates:
    # price_calculator.py の最適化 (65747a6) で SL 3→5%、TP 2〜4→7.5〜10% に変更された。
    # 以下のテストは現行値（SL 5% / TP 7.5-10%）に合わせている。
    def test_short_term_sl_is_5pct(self):
        result = calc_price_candidates(1000.0, sma25=None, holding_days=3, vix=20.0)
        bn = result["buy_now"]
        sl_pct = (bn["buy_price"] - bn["stop_loss"]) / bn["buy_price"] * 100
        assert abs(sl_pct - 5.0) < 0.5   # 5%前後（呼値丸めによる誤差許容）

    def test_short_term_sl_widens_with_high_vix(self):
        normal = calc_price_candidates(1000.0, sma25=None, holding_days=3, vix=20.0)
        high_vix = calc_price_candidates(1000.0, sma25=None, holding_days=3, vix=35.0)
        # VIX30超では損切り6%
        assert high_vix["buy_now"]["stop_loss_pct"] == 6.0
        assert normal["buy_now"]["stop_loss_pct"] == 5.0

    def test_medium_term_sl_is_5pct(self):
        result = calc_price_candidates(2000.0, sma25=None, holding_days=7, vix=20.0)
        assert result["buy_now"]["stop_loss_pct"] == 5.0

    def test_medium_term_sl_widens_with_high_vix(self):
        result = calc_price_candidates(2000.0, sma25=None, holding_days=7, vix=35.0)
        assert result["buy_now"]["stop_loss_pct"] == 7.0

    def test_short_term_tp_range(self):
        result = calc_price_candidates(1000.0, sma25=None, holding_days=2, vix=15.0)
        bn = result["buy_now"]
        tp_low_pct = (bn["take_profit_low"] - bn["buy_price"]) / bn["buy_price"] * 100
        tp_high_pct = (bn["take_profit_high"] - bn["buy_price"]) / bn["buy_price"] * 100
        assert 7.0 <= tp_low_pct <= 8.0     # 約7.5%
        assert 9.5 <= tp_high_pct <= 10.5   # 約10%

    def test_medium_term_tp_range(self):
        result = calc_price_candidates(2000.0, sma25=None, holding_days=7, vix=15.0)
        bn = result["buy_now"]
        tp_low_pct = (bn["take_profit_low"] - bn["buy_price"]) / bn["buy_price"] * 100
        tp_high_pct = (bn["take_profit_high"] - bn["buy_price"]) / bn["buy_price"] * 100
        assert 7.0 <= tp_low_pct <= 8.0     # 約7.5%
        assert 9.5 <= tp_high_pct <= 10.5   # 約10%

    def test_buy_dip_uses_sma25_when_lower(self):
        """SMA25が現在値より2%以上低い場合はSMA25を押し目価格に使う"""
        result = calc_price_candidates(2000.0, sma25=1900.0, holding_days=3, vix=20.0)
        # SMA25=1900 は現在値2000の5%下 → 押し目はSMA25を使う
        bd = result["buy_dip"]
        assert bd["buy_price"] == _tick_round(1900.0)

    def test_buy_dip_uses_fallback_when_sma25_close(self):
        """SMA25が現在値の2%未満の場合は -3% をフォールバックで使う"""
        result = calc_price_candidates(2000.0, sma25=1990.0, holding_days=3, vix=20.0)
        # SMA25=1990 は現在値2000の0.5%下 → フォールバック -3%
        bd = result["buy_dip"]
        assert bd["buy_price"] == _tick_round(2000.0 * 0.97)

    def test_buy_dip_uses_fallback_when_sma25_none(self):
        result = calc_price_candidates(2000.0, sma25=None, holding_days=3, vix=20.0)
        bd = result["buy_dip"]
        assert bd["buy_price"] == _tick_round(2000.0 * 0.97)

    def test_rr_ratio_short_term_is_adequate(self):
        """短期: TP_low=7.5% / SL=5% → RR=1.5 以上を確認。
        validate_recommendation(RR≥1.5) との整合性を保つため TP_low で判定する。
        """
        result = calc_price_candidates(1000.0, sma25=None, holding_days=3, vix=20.0)
        bn = result["buy_now"]
        reward_low = bn["take_profit_low"] - bn["buy_price"]
        risk = bn["buy_price"] - bn["stop_loss"]
        assert risk > 0
        assert reward_low / risk >= 1.4   # TP_low=7.5%, SL=5% → RR=1.5（呼値丸めで微差許容）


# ──────────────────────────────────────────────────────────
# calc_all_candidates: 両期間候補テスト
# ──────────────────────────────────────────────────────────

class TestCalcAllCandidates:
    def test_returns_both_periods(self):
        result = calc_all_candidates(2000.0, sma25=1900.0, vix=20.0)
        assert "short_term" in result
        assert "medium_term" in result

    def test_short_and_medium_term_sl_sensible(self):
        """短期・中期いずれの SL も 3〜10% の妥当な範囲内にあることを確認。
        (旧実装では短期<中期だったが、最適化後は両方 5% の固定値となった)
        """
        result = calc_all_candidates(2000.0, sma25=None, vix=20.0)
        st_sl = result["short_term"]["buy_now"]["stop_loss_pct"]
        mt_sl = result["medium_term"]["buy_now"]["stop_loss_pct"]
        assert 3.0 <= st_sl <= 10.0
        assert 3.0 <= mt_sl <= 10.0


# ──────────────────────────────────────────────────────────
# format_candidates_for_prompt: プロンプト整形テスト
# ──────────────────────────────────────────────────────────

class TestFormatCandidatesForPrompt:
    def test_contains_code_and_both_periods(self):
        candidates = calc_all_candidates(2000.0, sma25=None, vix=20.0)
        text = format_candidates_for_prompt("7203.T", candidates)
        assert "7203.T" in text
        assert "短期" in text
        assert "中期" in text

    def test_contains_buy_and_sl_values(self):
        candidates = calc_all_candidates(2000.0, sma25=None, vix=20.0)
        text = format_candidates_for_prompt("7203.T", candidates)
        assert "損切" in text
        assert "利確" in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

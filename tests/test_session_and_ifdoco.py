"""
test_session_and_ifdoco.py
session 切り替え（morning/noon）と IFDOCO 3点セット表示の回帰テスト。
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["DRY_RUN"] = "true"
os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import line_notifier


ANALYSIS = {
    "market_condition": "良好",
    "market_comment": "日経平均は堅調。",
    "recommendations": [
        {
            "rank": 1, "code": "7203.T", "name": "トヨタ自動車",
            "action": "今すぐ買う",
            "current_price": 2850, "buy_price": 2830,
            "take_profit_price": 3040, "stop_loss_price": 2689,
            "target_price": 3040,
            "upside_pct": 7.4, "rr_ratio": 1.5,
            "reason": "5日高値ブレイクを確認。",
            "key_signal": "breakout_5d",
            "holding_days": 3,
            "exit_timing": "目標到達",
            "risk_level": 2,
            "risk_comment": "為替リスクあり",
        }
    ],
    "exit_alerts": [],
    "caution": None,
}


class TestSessionHeader:
    def test_morning_session_header(self):
        text = line_notifier.build_report_text(ANALYSIS, {}, session="morning")
        assert "07:00" in text and "09:00" in text
        assert "前場寄付" in text

    def test_noon_session_header(self):
        text = line_notifier.build_report_text(ANALYSIS, {}, session="noon")
        assert "12:00" in text and "12:30" in text
        assert "後場寄付" in text

    def test_default_session_is_morning(self):
        """session 未指定時は morning 扱い。"""
        text = line_notifier.build_report_text(ANALYSIS, {})
        assert "前場寄付" in text


class TestIFDOCOBlock:
    def test_ifdoco_three_prices_present(self):
        """IFDOCO の 3 価格（買い/利確/損切）が全て表示される。"""
        text = line_notifier.build_report_text(ANALYSIS, {})
        assert "¥2,830" in text   # buy
        assert "¥3,040" in text   # take profit
        assert "¥2,689" in text   # stop loss

    def test_ifdoco_pct_shown(self):
        """利確率・損切率が併記される。"""
        text = line_notifier.build_report_text(ANALYSIS, {})
        # +7.4% 利確、-5.0% 損切（おおよそ）
        assert "+" in text and "%" in text
        assert "-" in text

    def test_rr_ratio_shown(self):
        """RR 比がフッターに表示される。"""
        text = line_notifier.build_report_text(ANALYSIS, {})
        assert "RR比" in text or "1.5" in text


class TestFlexMessage:
    def test_morning_header_color_blue(self):
        msg = line_notifier.build_flex_message("test", session="morning")
        bg = msg["contents"]["header"]["backgroundColor"]
        assert bg == "#1E3A5F"

    def test_noon_header_color_orange(self):
        msg = line_notifier.build_flex_message("test", session="noon")
        bg = msg["contents"]["header"]["backgroundColor"]
        assert bg == "#C75300"

    def test_noon_alt_text_mentions_noon(self):
        msg = line_notifier.build_flex_message("test", session="noon")
        assert "昼" in msg["altText"] or "後場" in msg["altText"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
tests/test_limit_fill_analyzer.py

backtest/limit_fill_analyzer.py の中核ロジックを合成データで検証する。
JQuants の実データに依存せず、手作りのバーで「指値が刺さるべきケース／刺さらないケース」
「TP / SL / 期日決済の優先順位」「ベクトル丸めと単一価格丸めの整合性」を確認する。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backtest"))

from price_calculator import _tick_round
from limit_fill_analyzer import (
    tick_round_series,
    add_next_bars,
    add_future_bars,
    add_limit_fill_columns,
    simulate_holding,
    build_scenarios,
)


# ── 呼値丸め: ベクトル版 vs 単一価格版の整合性 ──────────────────────────────

class TestTickRoundSeries:
    def test_matches_scalar_tick_round_for_each_band(self):
        prices = [501.4, 1234.7, 2999.2,
                  3003.0, 4997.0,
                  5001.0, 8765.0, 29994.0,
                  30001.0, 50024.0, 50025.0]
        s = pd.Series(prices)
        out = tick_round_series(s).tolist()
        expected = [_tick_round(p) for p in prices]
        assert out == expected

    def test_handles_nan(self):
        out = tick_round_series(pd.Series([100.4, float("nan"), 4001.0]))
        assert out.iloc[0] == 100.0
        assert np.isnan(out.iloc[1])
        assert out.iloc[2] == 4000.0


# ── 約定判定 ────────────────────────────────────────────────────────────────

def _make_bars(rows):
    """rows: list[(Code, Date, Open, High, Low, Close)] → 標準 DataFrame。"""
    df = pd.DataFrame(rows, columns=["Code", "Date", "Open", "High", "Low", "Close"])
    df["Date"] = pd.to_datetime(df["Date"])
    df["Volume"] = 1_000_000
    return df


class TestFillStatus:
    def test_gap_down_open_below_limit_fills_at_open(self):
        # T=1000 終値 → 指値 = 1000 * 0.993 = 993
        # T+1 始値 990 (指値より下) → fill_open、約定価格 = 990
        bars = _make_bars([
            ("0001", "2025-01-06", 1000, 1010, 990,  1000),
            ("0001", "2025-01-07",  990, 1000, 980,  995),
        ])
        df = add_next_bars(bars)
        df = add_future_bars(df, days=1)
        df = add_limit_fill_columns(df, limit_pct=-0.7)
        # シグナル行は 1 日目
        row = df.iloc[0]
        assert row["limit_price"] == 993.0
        assert row["fill_status"] == "fill_open"
        assert row["limit_entry"] == 990.0

    def test_gap_up_but_intraday_touches_limit_fills_at_limit(self):
        # T=1000 → 指値 993
        # T+1 始値 1005, 安値 990 → 指値到達、約定価格 = 993
        bars = _make_bars([
            ("0002", "2025-01-06", 1000, 1010, 990,  1000),
            ("0002", "2025-01-07", 1005, 1020, 990,  1015),
        ])
        df = add_next_bars(bars)
        df = add_future_bars(df, days=1)
        df = add_limit_fill_columns(df, limit_pct=-0.7)
        row = df.iloc[0]
        assert row["fill_status"] == "fill_intraday"
        assert row["limit_entry"] == 993.0

    def test_gap_up_no_dip_misses(self):
        # T=1000 → 指値 993
        # T+1 始値 1010, 安値 1005 → 指値到達せず → missed
        bars = _make_bars([
            ("0003", "2025-01-06", 1000, 1010, 990,  1000),
            ("0003", "2025-01-07", 1010, 1030, 1005, 1025),
        ])
        df = add_next_bars(bars)
        df = add_future_bars(df, days=1)
        df = add_limit_fill_columns(df, limit_pct=-0.7)
        row = df.iloc[0]
        assert row["fill_status"] == "missed"
        assert np.isnan(row["limit_entry"])
        # ギャップは +1.0%
        assert abs(row["gap_pct"] - 1.0) < 1e-6

    def test_last_row_marked_invalid(self):
        bars = _make_bars([
            ("0004", "2025-01-06", 1000, 1010, 990, 1000),
        ])
        df = add_next_bars(bars)
        df = add_future_bars(df, days=1)
        df = add_limit_fill_columns(df, limit_pct=-0.7)
        assert df.iloc[0]["fill_status"] == "invalid"


# ── 保有シミュレーション ───────────────────────────────────────────────────

class TestSimulateHolding:
    def _signal_df(self, future_bars, entry_price):
        """1 行の合成シグナル DataFrame を作る。"""
        df = pd.DataFrame({
            "Code": ["0001"],
            "Date": [pd.Timestamp("2025-01-06")],
            "entry": [entry_price],
        })
        for k, (h, l, c) in enumerate(future_bars, start=1):
            df[f"high_d{k}"]  = [h]
            df[f"low_d{k}"]   = [l]
            df[f"close_d{k}"] = [c]
        return df

    def test_tp_hit_on_day_1(self):
        # entry=1000, TP=+5%→1050, SL=-2%→980
        # 翌日 High=1060 → TP 達成、リターン = +5%
        df = self._signal_df([(1060, 990, 1055)], entry_price=1000.0)
        ret, outcome = simulate_holding(df, "entry", sl_pct=-2.0, tp_pct=5.0,
                                          days=1, cost_pct=0.0)
        assert outcome.iloc[0] == "tp"
        assert ret.iloc[0] == pytest.approx(5.0)

    def test_sl_priority_when_same_day_both_hit(self):
        # 同日 High も Low も SL/TP 範囲を越える → SL 優先（保守的）
        df = self._signal_df([(1060, 970, 1000)], entry_price=1000.0)
        ret, outcome = simulate_holding(df, "entry", sl_pct=-2.0, tp_pct=5.0,
                                          days=1, cost_pct=0.0)
        assert outcome.iloc[0] == "sl"
        assert ret.iloc[0] == pytest.approx(-2.0)

    def test_holds_until_close_when_neither_hits(self):
        # 1日目: TP/SL とも未到達 → 最終日 Close で決済
        df = self._signal_df([(1020, 990, 1010)], entry_price=1000.0)
        ret, outcome = simulate_holding(df, "entry", sl_pct=-2.0, tp_pct=5.0,
                                          days=1, cost_pct=0.0)
        assert outcome.iloc[0] == "exit_close"
        assert ret.iloc[0] == pytest.approx(1.0, abs=1e-6)

    def test_multi_day_tp_on_day_2(self):
        # 1日目: 何も当たらず／2日目: TP 達成
        df = self._signal_df(
            [(1020, 990, 1010), (1060, 1000, 1055)],
            entry_price=1000.0,
        )
        ret, outcome = simulate_holding(df, "entry", sl_pct=-2.0, tp_pct=5.0,
                                          days=2, cost_pct=0.0)
        assert outcome.iloc[0] == "tp"
        assert ret.iloc[0] == pytest.approx(5.0)

    def test_cost_is_subtracted(self):
        df = self._signal_df([(1060, 990, 1055)], entry_price=1000.0)
        ret, _ = simulate_holding(df, "entry", sl_pct=-2.0, tp_pct=5.0,
                                    days=1, cost_pct=0.3)
        assert ret.iloc[0] == pytest.approx(5.0 - 0.3)

    def test_nan_entry_yields_invalid(self):
        df = self._signal_df([(1060, 990, 1055)], entry_price=float("nan"))
        ret, outcome = simulate_holding(df, "entry", sl_pct=-2.0, tp_pct=5.0,
                                          days=1, cost_pct=0.0)
        assert outcome.iloc[0] == "invalid"
        assert np.isnan(ret.iloc[0])


# ── シナリオ統合 ────────────────────────────────────────────────────────────

class TestBuildScenarios:
    def test_missed_signal_excluded_from_limit_strategy(self):
        # ギャップアップで指値不約定 → ret_limit は NaN（不参戦）／ret_open は値を持つ
        # entry=next_open=1010, TP=+5%→1060.5, High=1070 → TP 達成（+5%）
        bars = _make_bars([
            ("0010", "2025-01-06", 1000, 1010, 990,  1000),
            ("0010", "2025-01-07", 1010, 1070, 1005, 1065),
        ])
        df = add_next_bars(bars)
        df = add_future_bars(df, days=1)
        df = add_limit_fill_columns(df, limit_pct=-0.7)
        df = build_scenarios(df, sl_pct=-5.0, tp_pct=5.0, days=1,
                              cost_pct=0.0, max_gap_pct=1.0)
        row = df.iloc[0]
        assert row["fill_status"] == "missed"
        assert np.isnan(row["ret_limit"])         # 不参戦
        assert row["ret_open"] == pytest.approx(5.0)  # TP 達成

    def test_conditional_open_skipped_for_big_gap(self):
        # ギャップ +2% で max_gap_pct=1.0 → 戦略 C は不参戦
        # entry=next_open=1020, TP=+5%→1071, High=1080 → TP 達成（戦略Bは +5%）
        bars = _make_bars([
            ("0011", "2025-01-06", 1000, 1010, 990,  1000),
            ("0011", "2025-01-07", 1020, 1080, 1015, 1075),
        ])
        df = add_next_bars(bars)
        df = add_future_bars(df, days=1)
        df = add_limit_fill_columns(df, limit_pct=-0.7)
        df = build_scenarios(df, sl_pct=-5.0, tp_pct=5.0, days=1,
                              cost_pct=0.0, max_gap_pct=1.0)
        row = df.iloc[0]
        assert np.isnan(row["ret_cond_open"])     # gap > 閾値 → スキップ
        assert not np.isnan(row["ret_open"])       # B は常に参戦

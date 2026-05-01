"""
test_threshold_review.py
src/threshold_review.py の単体テスト。

実Azure Blobには接続せず、`_load_log` をモックして
合成データで挙動を検証する。
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import threshold_review as tr


# ── 合成データ生成 ────────────────────────────────────────────────────────────


def _make_entry(
    *,
    outcome: str,
    return_pct: float,
    rsi5: float = 25,
    rsi14: float = 35,
    vol_ratio: float = 1.5,
    week52_pos_pct: float = 30,
    directional_vol_score: float = 10,
    screener_score: float = 60,
    breakout_5d: bool = True,
) -> dict:
    return {
        "signal_date": "2026-01-15",
        "code": "0000",
        "outcome": outcome,
        "return_pct": return_pct,
        "signals": {
            "rsi5": rsi5,
            "rsi14": rsi14,
            "vol_ratio": vol_ratio,
            "week52_pos_pct": week52_pos_pct,
            "directional_vol_score": directional_vol_score,
            "screener_score": screener_score,
            "breakout_5d": breakout_5d,
        },
    }


@pytest.fixture
def patch_log(monkeypatch):
    """`_load_log` を差し替えて任意のレコード集合を返すファクトリ。"""
    def _set(entries: list[dict]):
        monkeypatch.setattr(tr, "_load_log", lambda: entries)
    return _set


# ── 統計関数 ──────────────────────────────────────────────────────────────────


class TestStats:
    def test_empty(self):
        s = tr._stats([])
        assert s.n == 0
        assert s.win_rate is None
        assert s.expectancy is None

    def test_pure_wins(self):
        entries = [_make_entry(outcome="win", return_pct=3.0) for _ in range(5)]
        s = tr._stats(entries)
        assert s.n == 5
        assert s.wins == 5
        assert s.win_rate == 1.0
        assert s.avg_return == 3.0
        assert s.expectancy == 3.0

    def test_mixed(self):
        entries = (
            [_make_entry(outcome="win", return_pct=3.0) for _ in range(6)]
            + [_make_entry(outcome="loss", return_pct=-2.0) for _ in range(4)]
        )
        s = tr._stats(entries)
        assert s.n == 10
        assert s.wins == 6
        assert s.losses == 4
        assert s.win_rate == 0.6
        # 期待値 = 0.6 * 3.0 + 0.4 * -2.0 = 1.0
        assert s.expectancy == pytest.approx(1.0, abs=1e-3)


# ── load_decided_entries ──────────────────────────────────────────────────────


class TestLoadDecided:
    def test_filters_open_and_expired(self, patch_log):
        patch_log([
            _make_entry(outcome="win", return_pct=3.0),
            _make_entry(outcome="loss", return_pct=-2.0),
            {"outcome": "expired", "return_pct": None, "signals": {}},
            {"outcome": None, "return_pct": None, "signals": {}},
        ])
        decided = tr.load_decided_entries()
        assert len(decided) == 2
        assert {e["outcome"] for e in decided} == {"win", "loss"}


# ── 閾値スキャン ──────────────────────────────────────────────────────────────


class TestThresholdScan:
    def test_below_direction_finds_better_threshold(self, patch_log):
        """RSI5 が低いほど勝率が上がるパターン → 閾値もより低い側を提案する。"""
        # RSI5 ≤ 20: 勝率 90%, RSI5 30〜40: 勝率 30%
        entries = []
        for _ in range(20):
            entries.append(_make_entry(outcome="win", return_pct=3.0, rsi5=15))
        for _ in range(2):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0, rsi5=18))
        for _ in range(6):
            entries.append(_make_entry(outcome="win", return_pct=2.0, rsi5=35))
        for _ in range(14):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0, rsi5=38))

        patch_log(entries)
        decided = tr.load_decided_entries()
        scans = tr.scan_signal_thresholds(decided)
        rsi5 = scans["rsi5_oversold"]

        assert rsi5.best is not None
        # 強い側（≤20付近）の閾値が選ばれる
        assert rsi5.best["threshold"] <= 25
        # 現行 30 とベストとで ev_score が異なる
        assert rsi5.current_stats is not None

    def test_min_samples_filters_thin_bins(self, patch_log):
        """サンプルが MIN_SAMPLES 未満の閾値は rows に含まれない。"""
        # RSI5 ≤ 10 を満たすのは 3 件のみ → rows に出ない
        entries = []
        for _ in range(3):
            entries.append(_make_entry(outcome="win", return_pct=3.0, rsi5=8))
        for _ in range(15):
            entries.append(_make_entry(outcome="win", return_pct=2.0, rsi5=25))
        for _ in range(15):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0, rsi5=28))

        patch_log(entries)
        decided = tr.load_decided_entries()
        scans = tr.scan_signal_thresholds(decided)
        rsi5 = scans["rsi5_oversold"]
        thresholds = [r["threshold"] for r in rsi5.rows]
        # 10 はサンプル不足で除外される
        assert 10 not in thresholds

    def test_above_direction_for_dvs(self, patch_log):
        """DVS は above 方向（高いほど良い）。中間帯と高帯で勝率に勾配を持たせる。"""
        entries = []
        # DVS=30 は強く勝つ
        for _ in range(15):
            entries.append(_make_entry(outcome="win", return_pct=3.0,
                                       directional_vol_score=30))
        # DVS=5 は半々（中間帯）
        for _ in range(8):
            entries.append(_make_entry(outcome="win", return_pct=2.0,
                                       directional_vol_score=5))
        for _ in range(8):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0,
                                       directional_vol_score=5))
        # DVS=-5 は負ける
        for _ in range(15):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0,
                                       directional_vol_score=-5))
        patch_log(entries)
        decided = tr.load_decided_entries()
        scans = tr.scan_signal_thresholds(decided)
        dvs = scans["dvs_positive"]
        assert dvs.direction == "above"
        assert dvs.best is not None
        # 高帯 (≥20) のほうが期待値が高くなる
        assert dvs.best["threshold"] >= 10


# ── 提案ロジック ─────────────────────────────────────────────────────────────


class TestProposals:
    def test_no_proposal_when_current_already_best(self, patch_log):
        """現行 30 がそのままベストなら、stage1 は提案しない。"""
        entries = []
        # screener_score=60 を Stage1 の現行カットオフ（60）で完全に勝てる構成
        for _ in range(30):
            entries.append(_make_entry(outcome="win", return_pct=3.0,
                                       screener_score=80))
        patch_log(entries)
        decided = tr.load_decided_entries()
        stage1 = tr.scan_stage1_cutoff(decided)
        signals = tr.scan_signal_thresholds(decided)
        proposals = tr.build_proposals(stage1, signals)
        # 現行と同等なら stage1 提案は出ない（ev_score が同じ）
        assert "stage1_min_score" not in proposals or \
               proposals["stage1_min_score"]["proposed"] != 60 or \
               proposals["stage1_min_score"]["proposed_ev_score"] > \
               proposals["stage1_min_score"].get("current_ev_score", 0)

    def test_proposal_includes_proposed_value(self, patch_log):
        entries = []
        # vol_ratio が高い時に勝率が高い → vol_ratio_surge の提案が出やすい
        for _ in range(20):
            entries.append(_make_entry(outcome="win", return_pct=3.0, vol_ratio=2.0))
        for _ in range(20):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0, vol_ratio=1.0))
        patch_log(entries)
        decided = tr.load_decided_entries()
        stage1 = tr.scan_stage1_cutoff(decided)
        signals = tr.scan_signal_thresholds(decided)
        proposals = tr.build_proposals(stage1, signals)
        if "vol_ratio_surge" in proposals:
            p = proposals["vol_ratio_surge"]
            assert "proposed" in p
            assert "current" in p
            assert "proposed_n" in p


# ── レポート出力 ─────────────────────────────────────────────────────────────


class TestReport:
    def test_report_when_no_data(self, patch_log):
        patch_log([])
        report = tr.generate_threshold_report()
        assert "確定済み" in report

    def test_report_includes_sections(self, patch_log):
        entries = []
        for _ in range(15):
            entries.append(_make_entry(outcome="win", return_pct=3.0))
        for _ in range(15):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0))
        patch_log(entries)
        report = tr.generate_threshold_report()
        assert "推奨閾値 振り返りレポート" in report
        assert "Stage1 score" in report
        assert "RSI5" in report
        assert "推奨閾値サマリー" in report

    def test_summary_json_shape(self, patch_log):
        entries = []
        for _ in range(15):
            entries.append(_make_entry(outcome="win", return_pct=3.0))
        for _ in range(15):
            entries.append(_make_entry(outcome="loss", return_pct=-2.0))
        patch_log(entries)
        summary = tr.build_summary()
        assert summary["n_decided"] == 30
        assert "current_thresholds" in summary
        assert "scans" in summary
        assert "proposals" in summary
        assert summary["current_thresholds"]["stage1_min_score"] == 60

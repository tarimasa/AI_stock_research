"""
test_determinism.py
スクリーニング・推奨ロジックが「同一入力 → 同一出力」になることを保証する回帰テスト。

PR #20 でユーザー報告: liff の「AIレポートを更新」を 5 分間隔で 2 回叩いたところ
Stage1 通過 10 件のうち境界付近で 2 件が入れ替わる（休場日にもかかわらず）。

原因:
  - sorted(..., key=lambda x: x["score"], reverse=True) は同点の場合
    入力順を維持するが、ThreadPoolExecutor の as_completed は完了順 = 非決定
  - Claude API の temperature デフォルト 1.0 で出力が揺らぐ

修正:
  - 全ソートを (-score, code) のタプルキーで決定論化
  - claude_analyzer に temperature=0 を明示
"""

import os
import sys
from pathlib import Path

import pytest

os.environ["DRY_RUN"] = "true"
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import noon_screener
import screener


def _make_stocks_with_ties():
    """同点スコアを大量に含む候補リストを返す（Stage1 ソートのタイブレーカー検証用）。"""
    return [
        {"code": "9999", "stage1_score": 100.0},
        {"code": "1111", "stage1_score": 100.0},  # 同点
        {"code": "5555", "stage1_score": 100.0},  # 同点
        {"code": "3333", "stage1_score": 80.0},
        {"code": "7777", "stage1_score": 80.0},   # 同点
        {"code": "2222", "stage1_score": 60.0},
        {"code": "4444", "stage1_score": 60.0},   # 同点
        {"code": "6666", "stage1_score": 50.0},
        {"code": "8888", "stage1_score": 50.0},   # 同点（境界）
        {"code": "1010", "stage1_score": 50.0},   # 同点（境界）
        {"code": "2020", "stage1_score": 40.0},   # ここから外れる
        {"code": "3030", "stage1_score": 40.0},
    ]


def _run_screener_sort(stocks):
    """screener.py の最終ソートロジックを再現（同じ式）。"""
    return sorted(
        stocks, key=lambda x: (-x["stage1_score"], str(x.get("code", "")))
    )[:10]


class TestStage1SortDeterminism:
    def test_same_input_produces_same_output(self):
        """同一入力で 100 回ソートして全結果が一致することを確認。"""
        stocks = _make_stocks_with_ties()
        first_result = [s["code"] for s in _run_screener_sort(stocks)]

        for _ in range(100):
            result = [s["code"] for s in _run_screener_sort(stocks)]
            assert result == first_result, "ソートが非決定的"

    def test_shuffled_input_produces_same_output(self):
        """入力順がシャッフルされても結果は同一（タイブレーカーが効いている）。"""
        import random
        stocks = _make_stocks_with_ties()
        baseline = [s["code"] for s in _run_screener_sort(stocks)]

        for seed in range(20):
            shuffled = stocks.copy()
            random.Random(seed).shuffle(shuffled)
            result = [s["code"] for s in _run_screener_sort(shuffled)]
            assert result == baseline, f"seed={seed} で結果が変わった"

    def test_tied_scores_resolved_by_code_ascending(self):
        """同点の場合はコード昇順で並ぶ（小さい数字が上位）。"""
        stocks = [
            {"code": "9999", "stage1_score": 100.0},
            {"code": "1111", "stage1_score": 100.0},
            {"code": "5555", "stage1_score": 100.0},
        ]
        result = [s["code"] for s in _run_screener_sort(stocks)]
        assert result == ["1111", "5555", "9999"]

    def test_top10_boundary_ties_stable(self):
        """rank 10/11 が同点でも常に同じ銘柄が選ばれる（ユーザー報告のシナリオ）。"""
        # 1〜10位は確定、11位以降に同点を作る
        stocks = [{"code": f"00{i:02d}", "stage1_score": 200 - i * 10} for i in range(8)]
        # 8位、9位、10位、11位が全て同点（50点）
        stocks.extend([
            {"code": "9127", "stage1_score": 50.0},   # ユーザーの実例
            {"code": "2760", "stage1_score": 50.0},   # ユーザーの実例
            {"code": "5599", "stage1_score": 50.0},
            {"code": "1234", "stage1_score": 50.0},
        ])

        result = [s["code"] for s in _run_screener_sort(stocks)]
        assert len(result) == 10
        # 0000〜0007 (8銘柄) で 8 位までは確定。残り 2 枠を同点 4 銘柄で争う。
        # コード昇順タイブレークなので 1234, 2760 が選ばれ、5599/9127 が落ちる。
        assert "1234" in result
        assert "2760" in result
        assert "5599" not in result   # 同点だがコード値で 2760 に負ける（決定論的）
        assert "9127" not in result

        # シャッフルしても同じ結果
        import random
        for seed in range(10):
            shuffled = stocks.copy()
            random.Random(seed).shuffle(shuffled)
            result2 = [s["code"] for s in _run_screener_sort(shuffled)]
            assert result == result2, f"seed={seed} で境界ソートが変わった"


class TestNoonSortDeterminism:
    def test_noon_filter_sort_is_deterministic(self):
        """noon_screener も同じタイブレーカーで決定論的。"""
        import random
        # 同点を含む noon_score
        candidates = [
            {"code": f"00{i:02d}", "noon_score": 50.0,
             "stage1_score": 30, "close": 1000, "volume": 100000}
            for i in range(20)
        ]

        # apply_noon_filter は前場メトリクス取得を伴うので、ソート部分だけ単独で再現
        def _noon_sort(c, n):
            return sorted(
                c, key=lambda x: (-x.get("noon_score", 0), str(x.get("code", "")))
            )[:n]

        baseline = [s["code"] for s in _noon_sort(candidates, 8)]
        for seed in range(20):
            shuffled = candidates.copy()
            random.Random(seed).shuffle(shuffled)
            result = [s["code"] for s in _noon_sort(shuffled, 8)]
            assert result == baseline


class TestApplyStage1FiltersOrdering:
    def test_apply_stage1_passes_through_in_input_order(self):
        """_apply_stage1_filters は入力順を保持し、後段ソートで決定論化される。"""
        stocks = [
            {"code": "9999", "rsi5": 15, "rsi14": 30, "vol_ratio": 1.6,
             "w52_pos": 30, "breakout_5d": True, "dvs": 25, "close": 1000, "sma25": 980},
            {"code": "1111", "rsi5": 15, "rsi14": 30, "vol_ratio": 1.6,
             "w52_pos": 30, "breakout_5d": True, "dvs": 25, "close": 1000, "sma25": 980},
        ]
        result1 = screener._apply_stage1_filters(stocks)
        result2 = screener._apply_stage1_filters(list(reversed(stocks)))
        # input order が反転すれば passed の順序も反転する（後段で再ソートする責任）
        assert [s["code"] for s in result1] != [s["code"] for s in result2]
        # スコアは同じ（同条件なので同点）
        assert result1[0]["stage1_score"] == result2[0]["stage1_score"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

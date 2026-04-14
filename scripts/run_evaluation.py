#!/usr/bin/env python3
"""
scripts/run_evaluation.py
バックテスト評価レポートをコンソールに出力する。
GitHub Actions の evaluate.yml から呼ばれる。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backtest_evaluator import generate_evaluation_report
from backtest_logger import update_outcomes
from signal_tracker import update_signal_outcomes


def main() -> None:
    print("=== バックテスト評価 開始 ===")

    # signal_tracker のクローズ済みシグナルをバックテストログに反映
    print("[eval] シグナル結果を更新中...")
    closed = update_signal_outcomes()
    update_outcomes(closed)

    # 評価レポートを生成・出力
    print("[eval] 評価レポート生成中...")
    report = generate_evaluation_report()
    print("\n" + report)
    print("\n=== バックテスト評価 完了 ===")


if __name__ == "__main__":
    main()

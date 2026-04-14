#!/usr/bin/env python3
"""
scripts/send_eval_summary.py
バックテスト評価サマリーをLINEに送信する。
週次で GitHub Actions から呼ばれることを想定。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from backtest_evaluator import generate_evaluation_report
from backtest_logger import update_outcomes
from line_notifier import push_message
from signal_tracker import update_signal_outcomes


def main() -> None:
    print("=== 週次バックテスト評価サマリー送信 ===")

    # 最新のシグナル結果を反映してからレポート生成
    closed = update_signal_outcomes()
    update_outcomes(closed)

    report_text = generate_evaluation_report()
    print(report_text)

    # LINE に送信
    push_message([{
        "type": "text",
        "text": report_text,
    }])
    print("[send_eval] LINE送信完了")


if __name__ == "__main__":
    main()

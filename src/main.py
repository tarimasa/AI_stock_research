"""
main.py
オーケストレーター。GitHub Actions から実行される。
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

import line_notifier
import report


def main() -> None:
    try:
        report.run_report()
    except Exception as e:
        print(f"[main] エラー発生: {e}", file=sys.stderr)
        try:
            line_notifier.send_error_notification(str(e))
        except Exception as notify_err:
            print(f"[main] エラー通知の送信にも失敗: {notify_err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

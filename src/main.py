"""
main.py
オーケストレーター。GitHub Actions から実行される。

セッション切り替え（環境変数 or コマンドライン引数）:
  - SESSION=morning or 引数 "morning" (既定): 朝レポート、9:00 前場寄付向け
  - SESSION=noon    or 引数 "noon":           昼レポート、12:30 後場寄付向け
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

import line_notifier
import report


def _get_session() -> str:
    """コマンドライン引数 → 環境変数 SESSION → 既定 "morning" の順に解決する。"""
    if len(sys.argv) >= 2 and sys.argv[1] in ("morning", "noon"):
        return sys.argv[1]
    session = os.environ.get("SESSION", "morning").lower()
    return session if session in ("morning", "noon") else "morning"


def main() -> None:
    session = _get_session()
    try:
        if session == "noon":
            report.run_noon_report()
        else:
            report.run_report()
    except Exception as e:
        print(f"[main] エラー発生 ({session}): {e}", file=sys.stderr)
        try:
            line_notifier.send_error_notification(str(e))
        except Exception as notify_err:
            print(f"[main] エラー通知の送信にも失敗: {notify_err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

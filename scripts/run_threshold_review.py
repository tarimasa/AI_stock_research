#!/usr/bin/env python3
"""
scripts/run_threshold_review.py

クラウド (Azure Blob) に蓄積された推奨ログから現行閾値の振り返りを行い、
推奨閾値の提案をテキスト & JSON で出力する。GitHub Actions の
.github/workflows/threshold_review.yml から呼ばれる。

ローカルで動かす場合:
  cd ~/AI_stock_research
  source venv/bin/activate
  AZURE_STORAGE_CONNECTION_STRING=... python scripts/run_threshold_review.py

  # オフラインで config/backtest_log_local.json から読みたい場合:
  unset AZURE_STORAGE_CONNECTION_STRING
  python scripts/run_threshold_review.py

オプション:
  --notify-line   レポートの提案サマリーを LINE に push する
  --output-dir P  テキスト / JSON の出力先（既定: data/threshold_review）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from backtest_logger import update_outcomes  # noqa: E402
from signal_tracker import update_signal_outcomes  # noqa: E402
from threshold_review import (  # noqa: E402
    CURRENT_THRESHOLDS,
    build_summary,
    generate_threshold_report,
    write_summary,
)

JST = ZoneInfo("Asia/Tokyo")


def _build_line_summary(summary: dict) -> str:
    """LINE 通知用のショート版（≦ 1900 文字目安）を生成する。"""
    n = summary.get("n_decided", 0)
    overall = summary.get("overall") or {}
    proposals = summary.get("proposals") or {}

    if n == 0:
        return "📊 推奨閾値レビュー: 確定済み推奨がまだありません。"

    win_rate = (overall.get("win_rate") or 0) * 100
    avg_ret = overall.get("avg_return")
    avg_ret_str = f"{avg_ret:+.2f}%" if avg_ret is not None else "-"
    ev = overall.get("expectancy")
    ev_str = f"{ev:+.2f}%" if ev is not None else "-"

    lines = [
        "📊 推奨閾値レビュー",
        f"確定推奨 {n}件 / 勝率 {win_rate:.1f}% / 平均R {avg_ret_str} / 期待値 {ev_str}",
        "",
    ]
    if proposals:
        lines.append("【推奨閾値（現行→提案）】")
        for key, p in proposals.items():
            wr = (p.get("proposed_win_rate") or 0) * 100
            lines.append(
                f"・{key}: {p['current']} → {p['proposed']} "
                f"(n={p['proposed_n']}, 勝率{wr:.1f}%)"
            )
    else:
        lines.append("提案: 現行閾値が概ね最適、または有意差なし。")
    lines.append("")
    lines.append("詳細レポートは GitHub Actions のアーティファクトを参照。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Threshold review for stock recommendations")
    parser.add_argument(
        "--notify-line",
        action="store_true",
        help="LINEに推奨閾値サマリーをpushする（LINE_CHANNEL_ACCESS_TOKEN等が必要）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "threshold_review",
        help="レポート/JSONの出力ディレクトリ",
    )
    parser.add_argument(
        "--skip-update",
        action="store_true",
        help="シグナル結果の更新をスキップ（評価ロジックの単体実行用）",
    )
    args = parser.parse_args()

    print("=== 推奨閾値 振り返り 開始 ===")
    current_pairs = ", ".join(f"{k}={v['value']}" for k, v in CURRENT_THRESHOLDS.items())
    print(f"現行閾値: {{ {current_pairs} }}")

    # 1. クラウド上のオープンシグナルを最新化（win/loss を確定させる）
    if not args.skip_update:
        print("[review] シグナル結果を更新中...")
        try:
            closed = update_signal_outcomes()
            update_outcomes(closed)
        except Exception as e:
            # 更新が失敗しても既存ログから振り返りは可能なので警告のみ
            print(f"[review] WARN: シグナル更新に失敗しました ({type(e).__name__}: {e})")
    else:
        print("[review] --skip-update 指定のため結果更新をスキップ")

    # 2. レポート生成
    print("[review] 振り返りレポート生成中...")
    report_text = generate_threshold_report()
    print("\n" + report_text + "\n")

    # 3. ファイル出力
    args.output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(JST).strftime("%Y-%m-%d")
    txt_path = args.output_dir / f"threshold_review_{today}.txt"
    json_path = args.output_dir / f"threshold_review_{today}.json"

    txt_path.write_text(report_text, encoding="utf-8")
    write_summary(json_path)
    # 最新版へのシンボリック名（CI が固定パスでアップロードしやすいよう）
    (args.output_dir / "latest.txt").write_text(report_text, encoding="utf-8")
    write_summary(args.output_dir / "latest.json")

    print(f"[review] テキスト保存: {txt_path}")
    print(f"[review] JSON保存:    {json_path}")

    # GitHub Actions 用: ステップサマリーへの貼り付け
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("## 推奨閾値 振り返りレポート\n\n```\n")
            f.write(report_text)
            f.write("\n```\n")
        print("[review] GITHUB_STEP_SUMMARY に書き込み済み")

    # 4. LINE 通知（任意）
    if args.notify_line:
        try:
            from line_notifier import push_message
            summary = build_summary()
            line_text = _build_line_summary(summary)
            push_message([{"type": "text", "text": line_text}])
            print("[review] LINE 通知送信完了")
        except Exception as e:
            print(f"[review] LINE 通知失敗: {type(e).__name__}: {e}")
            return 1

    print("=== 推奨閾値 振り返り 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

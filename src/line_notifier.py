"""
line_notifier.py
推奨レポートと保有株アラートを Flex Message で LINE に送信する。
DRY_RUN=true の場合は print 出力のみ。
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
LIFF_URL = os.environ.get("LIFF_URL", "https://liff.line.me/YOUR_LIFF_ID")

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

ACTION_EMOJI = {"今すぐ買う": "🟢", "押し目待ち": "🟡", "見送り": "🔴"}
RISK_STARS = {1: "★☆☆", 2: "★★☆", 3: "★★★"}


def build_report_text(analysis: dict, portfolio_result: dict) -> str:
    """推奨レポート＋保有株アラートのテキストを生成。"""
    lines = []

    if analysis.get("market_condition") == "悪化":
        lines.append("⛔ 本日は全銘柄買い見送り推奨")
        lines.append(f"理由: {analysis.get('market_comment', '')}")
    else:
        lines.append("━━ 本日の推奨銘柄 ━━")
        for r in analysis.get("recommendations", []):
            emoji = ACTION_EMOJI.get(r.get("action", ""), "⚪")
            risk_star = RISK_STARS.get(r.get("risk_level", 2), "★★☆")
            lines.append(
                f"{emoji} {r.get('action', '')} {r.get('code', '').replace('.T', '')} {r.get('name', '')}\n"
                f"   ¥{r.get('current_price', 0):,} → 目標¥{r.get('target_price', 0):,}（+{r.get('upside_pct', 0)}%）\n"
                f"   {r.get('reason', '')}\n"
                f"   リスク: {risk_star} {r.get('risk_comment', '')}"
            )

    if analysis.get("caution"):
        lines.append(f"\n⚠️ {analysis['caution']}")

    # 保有株アラート（holdings がある場合のみ）
    if portfolio_result.get("holdings"):
        lines.append("\n━━ 保有株アラート ━━")
        for h in portfolio_result["holdings"]:
            if h.get("alert"):
                alert_emoji = "🔴" if "損切り" in h["alert"] else "🟡"
                lines.append(
                    f"{alert_emoji} 【{h['alert']}】{h['name']}\n"
                    f"   {h['unrealized_pnl_pct']:+.1f}%（{h['unrealized_pnl']:+,}円）"
                )
            else:
                lines.append(
                    f"✅ 【様子見】{h['name']}\n"
                    f"   {h['unrealized_pnl_pct']:+.1f}%（{h['unrealized_pnl']:+,}円）"
                )
        pnl = portfolio_result["total_unrealized_pnl"]
        pnl_pct = portfolio_result["total_unrealized_pnl_pct"]
        lines.append(f"\n💰 合計評価損益: {pnl:+,}円（{pnl_pct:+.1f}%）")

    lines.append("\n❗ 本情報は投資判断の参考です。最終決定は自己責任でお願いします。")
    return "\n".join(lines)


def build_flex_message(report_text: str) -> dict:
    """レポートテキスト＋LIFF ボタンを Flex Message に組み立てる。"""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d")
    return {
        "type": "flex",
        "altText": f"AI株式リサーチ {today}",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#1E3A5F",
                "contents": [{
                    "type": "text",
                    "text": f"🤖 AI株式リサーチ {today}",
                    "color": "#FFFFFF",
                    "weight": "bold",
                    "size": "md",
                }],
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [{
                    "type": "text",
                    "text": report_text,
                    "wrap": True,
                    "size": "sm",
                }],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [{
                    "type": "button",
                    "action": {
                        "type": "uri",
                        "label": "📝 銘柄を更新する",
                        "uri": LIFF_URL,
                    },
                    "style": "primary",
                    "color": "#1E3A5F",
                }],
            },
        },
    }


def _line_headers() -> dict:
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def push_message(messages: list) -> None:
    """LINE Push Message API でメッセージを送信する。"""
    payload = {"to": LINE_USER_ID, "messages": messages}
    if DRY_RUN:
        print("[DRY_RUN] LINE Push Message:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    resp = requests.post(LINE_PUSH_URL, headers=_line_headers(), json=payload, timeout=10)
    resp.raise_for_status()


def reply_message(reply_token: str, messages: list) -> None:
    """LINE Reply Message API でメッセージを返信する。"""
    payload = {"replyToken": reply_token, "messages": messages}
    if DRY_RUN:
        print("[DRY_RUN] LINE Reply Message:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    resp = requests.post(LINE_REPLY_URL, headers=_line_headers(), json=payload, timeout=10)
    resp.raise_for_status()


def send_daily_report(analysis: dict, portfolio_result: dict) -> None:
    """毎日の推奨レポートを LINE に送信する。"""
    report_text = build_report_text(analysis, portfolio_result)
    if DRY_RUN:
        print("===== DRY RUN: 送信するレポート =====")
        print(report_text)
        print("====================================")

    flex_msg = build_flex_message(report_text)
    push_message([flex_msg])


def send_error_notification(error_message: str) -> None:
    """エラー発生時に LINE にエラー通知を送る。"""
    msg = {
        "type": "text",
        "text": f"⚠️ AI株式リサーチ エラー\n\n{error_message}\n\n管理者にお問い合わせください。",
    }
    push_message([msg])

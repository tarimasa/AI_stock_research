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


def _format_stage1_signals(rec: dict) -> str:
    """stage1_signals / key_signal から J-Quants スキャン根拠を1行で返す。"""
    sigs = rec.get("stage1_signals") or []
    if sigs:
        return "📡 " + " / ".join(sigs)
    key = rec.get("key_signal", "")
    return f"📡 {key}" if key else ""


def build_report_text(analysis: dict, portfolio_result: dict) -> str:
    """推奨レポート＋保有株アラートのテキストを生成。"""
    lines = []

    if analysis.get("market_condition") == "悪化":
        lines.append("⛔ 本日は全銘柄買い見送り推奨")
        lines.append(f"理由: {analysis.get('market_comment', '')}")
    else:
        lines.append("━━ 本日の推奨銘柄（短期1〜3日）━━")
        if not analysis.get("recommendations"):
            lines.append(f"📊 {analysis.get('market_comment', '本日は推奨銘柄がありませんでした。')}")
        for r in analysis.get("recommendations", []):
            emoji = ACTION_EMOJI.get(r.get("action", ""), "⚪")
            risk_star = RISK_STARS.get(r.get("risk_level", 2), "★★☆")
            buy_p     = r.get("buy_price") or r.get("current_price") or 0
            tp_p      = r.get("take_profit_price") or r.get("target_price") or 0
            sl_p      = r.get("stop_loss_price") or 0
            upside    = r.get("upside_pct") or 0
            current_p = r.get("current_price") or 0
            hold_days = r.get("holding_days", 3)
            sig_line  = _format_stage1_signals(r)

            block = (
                f"{emoji} {r.get('action', '')}  "
                f"{r.get('code', '').replace('.T', '')} {r.get('name', '')}\n"
                f"   現在値 ¥{current_p:,} → 目標 ¥{tp_p:,}（+{upside}%）\n"
                f"   {r.get('reason', '')}\n"
            )
            if sig_line:
                block += f"   {sig_line}\n"
            block += (
                f"   保有目安: {hold_days}営業日  "
                f"リスク: {risk_star} {r.get('risk_comment', '')}\n"
                f"   ┌─ IFDOCO注文\n"
                f"   │①買い指値   ¥{buy_p:,}\n"
                f"   │②利確指値   ¥{tp_p:,}\n"
                f"   └③損切逆指値 ¥{sl_p:,}"
            )
            lines.append(block)

    if analysis.get("caution"):
        lines.append(f"\n⚠️ {analysis['caution']}")

    # エグジットアラート（Claude が検出した保有中銘柄の撤退シグナル）
    exit_alerts = analysis.get("exit_alerts", [])
    if exit_alerts:
        lines.append("\n━━ エグジットアラート ━━")
        for alert in exit_alerts:
            code = alert.get("code", "").replace(".T", "")
            name = alert.get("name", "")
            alert_type = alert.get("alert_type", "")
            message = alert.get("message", "")
            suggested = alert.get("suggested_action", "")
            lines.append(
                f"🚨 【{alert_type}】{code} {name}\n"
                f"   {message}\n"
                f"   → {suggested}"
            )

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
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "uri",
                            "label": "🔄 AIレポートを更新",
                            "uri": LIFF_URL + "?mode=refresh",
                        },
                        "style": "primary",
                        "color": "#1E3A5F",
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "uri",
                            "label": "📊 バックテスト評価",
                            "uri": LIFF_URL + "?mode=backtest",
                        },
                        "style": "secondary",
                        "color": "#2E7D32",
                    },
                    {
                        "type": "button",
                        "action": {
                            "type": "uri",
                            "label": "📝 銘柄を更新する",
                            "uri": LIFF_URL,
                        },
                        "style": "secondary",
                    },
                ],
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


def send_daily_report(
    analysis: dict,
    portfolio_result: dict,
    scan_info: str | None = None,
) -> None:
    """毎日の推奨レポートを LINE に送信する。
    scan_info: フッターに追加するスキャン情報（例: "J-Quants全銘柄スキャン: 3,921銘柄→Stage1通過8件"）
    """
    report_text = build_report_text(analysis, portfolio_result)
    if scan_info:
        report_text += f"\n\n🔍 {scan_info}"
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

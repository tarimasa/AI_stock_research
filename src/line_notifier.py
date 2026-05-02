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


def build_report_text(analysis: dict, portfolio_result: dict, session: str = "morning") -> str:
    """推奨レポート＋保有株アラートのテキストを生成。

    session: "morning" = 7:00〜9:00 発注窓 / 9:00 寄付向け
             "noon"    = 12:00〜13:00 発注窓 / 12:30 後場寄付向け
    """
    lines = []

    # 発注時刻ヘッダー
    if session == "noon":
        lines.append("📣 12:00〜12:30 発注 → 12:30 後場寄付")
    else:
        lines.append("📣 07:00〜09:00 発注 → 09:00 前場寄付")
    lines.append("")

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
            # IFDOCO 3点セット: 証券会社の注文画面にコピペしやすい形式で出力。
            # 損失率・利益率も併記して発注ミスを減らす。
            sl_pct = ((buy_p - sl_p) / buy_p * 100) if buy_p > 0 else 0
            tp_pct = ((tp_p - buy_p) / buy_p * 100) if buy_p > 0 else 0
            block += (
                f"   保有目安: {hold_days}営業日  "
                f"リスク: {risk_star} {r.get('risk_comment', '')}\n"
                f"   ┌─ IFDOCO 3点セット ─┐\n"
                f"   │①買い指値   ¥{buy_p:,}\n"
                f"   │②利確指値   ¥{tp_p:,}  (+{tp_pct:.1f}%)\n"
                f"   │③損切逆指値 ¥{sl_p:,}  (-{sl_pct:.1f}%)\n"
                f"   └─ RR比 {r.get('rr_ratio', 0):.2f} ─┘"
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


def build_flex_message(report_text: str, session: str = "morning") -> dict:
    """レポートテキスト＋LIFF ボタンを Flex Message に組み立てる。

    session: "morning" = 朝レポート（前場寄付向け）
             "noon"    = 昼レポート（後場寄付向け）
    """
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d")
    session_label = "昼・後場寄付向け" if session == "noon" else "朝・前場寄付向け"
    header_color = "#C75300" if session == "noon" else "#1E3A5F"
    return {
        "type": "flex",
        "altText": f"AI株式リサーチ {today}（{session_label}）",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": header_color,
                "contents": [{
                    "type": "text",
                    "text": f"🤖 AI株式リサーチ {today} ({session_label})",
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
                        "style": "primary",
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


def build_stage1_detail_text(stage1_stocks: list, analysis: dict) -> str:
    """Stage1通過銘柄とClaudeの判断を一覧表示するテキストを生成する。"""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d")
    n = len(stage1_stocks)
    lines = [f"📋 Stage1分析詳細 {today}（{n}件）", "─" * 20]

    # Claudeの推奨をコードでインデックス（4桁/6桁両対応）
    all_recs = analysis.get("all_recommendations", [])
    recs_by_code: dict = {}
    for r in all_recs:
        code = r.get("code", "").replace(".T", "")
        recs_by_code[code] = r
        recs_by_code[code[:4]] = r

    for stock in stage1_stocks:
        raw_code = stock.get("code", "")
        code4 = raw_code.replace(".T", "")[:4]
        # 名前フォールバック: 空文字なら "(銘柄名不明)" を表示（raw_codeの2度表示を避ける）
        name = stock.get("name") or "(銘柄名不明)"

        rsi5 = stock.get("rsi5")
        vol = stock.get("vol_ratio")
        dvs = stock.get("dvs") if stock.get("dvs") is not None else stock.get("directional_vol_score")

        rsi_mark = (f"RSI5={rsi5:.0f}✓" if rsi5 is not None and rsi5 <= 20
                    else (f"RSI5={rsi5:.0f}" if rsi5 is not None else ""))
        vol_mark = (f"出来高{vol:.1f}x✓" if vol is not None and vol >= 1.5
                    else (f"出来高{vol:.1f}x" if vol is not None else ""))
        dvs_str = f" DVS{dvs:+.0f}" if dvs is not None else ""

        rec = recs_by_code.get(code4) or recs_by_code.get(raw_code.replace(".T", ""))

        # 銘柄名: screenerのmaster→Claudeの推奨→コードの順で取得
        stock_name = stock.get("name", "")
        rec_name = rec.get("name", "") if rec else ""
        name = (stock_name if (stock_name and stock_name != code4)
                else (rec_name if rec_name else code4))

        if rec is None:
            emoji = "🔘"
            detail = "分析対象外"
        elif rec.get("_invalid"):
            emoji = "❌"
            detail = f"除外: {rec.get('_invalid_reason', '')}"
        elif rec.get("action") == "見送り":
            emoji = "🔘"
            reason = rec.get("reason", "")
            detail = f"見送り: {reason[:40]}" if reason else "見送り"
        else:
            action = rec.get("action", "")
            emoji = ACTION_EMOJI.get(action, "⚪")
            reason = rec.get("reason", "")
            tp = rec.get("take_profit_price", 0)
            detail = f"{action}: {reason[:40]}" if reason else action
            if tp:
                detail += f" 目標¥{tp:,}"

        sig_parts = [p for p in [rsi_mark, vol_mark] if p]
        sig_line = " ".join(sig_parts) + dvs_str if (sig_parts or dvs_str) else ""

        lines.append(f"{emoji} {code4} {name}")
        if sig_line:
            lines.append(f"   {sig_line}")
        lines.append(f"   {detail}")

    return "\n".join(lines)


def send_daily_report(
    analysis: dict,
    portfolio_result: dict,
    scan_info: str | None = None,
    stage1_stocks: list | None = None,
    session: str = "morning",
) -> None:
    """毎日の推奨レポートを LINE に送信する。
    scan_info: フッターに追加するスキャン情報
    stage1_stocks: Stage1通過銘柄リスト（詳細を第2メッセージで送信する）
    session: "morning"=朝の寄付向け, "noon"=昼の後場寄付向け
    """
    report_text = build_report_text(analysis, portfolio_result, session=session)
    if scan_info:
        report_text += f"\n\n🔍 {scan_info}"
    if DRY_RUN:
        print("===== DRY RUN: 送信するレポート =====")
        print(report_text)
        print("====================================")

    flex_msg = build_flex_message(report_text, session=session)
    push_message([flex_msg])

    # Stage1通過銘柄の詳細を第2メッセージとして送信
    if stage1_stocks and "all_recommendations" in analysis:
        detail_text = build_stage1_detail_text(stage1_stocks, analysis)
        if DRY_RUN:
            print("===== DRY RUN: Stage1詳細 =====")
            print(detail_text)
            print("==============================")
        push_message([{"type": "text", "text": detail_text}])


def send_error_notification(error_message: str) -> None:
    """エラー発生時に LINE にエラー通知を送る。"""
    msg = {
        "type": "text",
        "text": f"⚠️ AI株式リサーチ エラー\n\n{error_message}\n\n管理者にお問い合わせください。",
    }
    push_message([msg])

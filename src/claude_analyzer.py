"""
claude_analyzer.py
スクリーニング通過銘柄を Claude API で分析し推奨レポートを生成する。

v2 変更点:
- システムプロンプトを 800 トークン以下に圧縮（タイムアウト対策）
- max_tokens 2048 → 4096、タイムアウト 60 → 120 秒
- 価格計算を price_calculator.py に移行。LLM は action/holding_days 判断のみ
- validate_recommendation() でRR比 < 1.5 の推奨を除外
- format_screener_for_prompt() でユーザープロンプトをCSV形式に圧縮
"""

import json
import os
import time
from datetime import datetime

import anthropic
import httpx
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MODEL = "claude-haiku-4-5-20251001"
MAX_RETRIES = 3

# ── システムプロンプト（圧縮版 ≤800 トークン） ────────────────────────────
SYSTEM_PROMPT = """\
あなたは日本株短期トレードの分析AIです。スクリーナーデータとマクロ指標を受け取り、JSON形式で推奨を返します。

# ルール

## 選定
- 推奨1〜3銘柄。短期(1-3日)はシグナル強度順。中期(5-10日)は異なるセクター必須
- 短期は同一セクター2銘柄まで可。3銘柄全て同一は不可
- 同一銘柄の連続推奨禁止

## シグナル優先度
1. breakout_5d=true + dvs正 + rsi5≤30 → 短期最優先
2. vol_ratio≥1.5 + w52_pos≤25 + 権利確定接近 → 次点
3. dvs≤-10 → 短期推奨不可

## マクロ判定
- VIX≥30: リスク+1段上げ
- VIX 20-30: 慎重
- S&P500 -1%超: 波及リスク注記
- 金+1%超: リスクオフ注記
- 原油±3%超: 該当セクター注記

## 価格（重要）
事前計算済み価格をそのまま使え。自分で計算するな。
- action=今すぐ買う → buy_nowの値セット
- action=押し目待ち → buy_dipの値セット
- 短期(holding_days≤3)→短期価格セット、中期→中期価格セット
- take_profit_priceはtp_low〜tp_highの範囲で選べ

## エグジット警告
保有中銘柄がある場合、以下でexit_alertsに出力:
- dvs≤-10 or rsi5≥80 or 目標価格80%到達済

## 出力
JSONのみ返せ。コードブロック・説明文不要。
{"market_condition":"良好|注意|悪化","market_comment":"1文","recommendations":[{"rank":1,"code":"7203.T","name":"トヨタ","sector":"自動車","action":"今すぐ買う|押し目待ち|見送り","current_price":0,"buy_price":0,"target_price":0,"take_profit_price":0,"stop_loss_price":0,"upside_pct":0,"rr_ratio":0,"reason":"2文以内","key_signal":"主要シグナル","holding_days":3,"exit_timing":"目標到達or3営業日後","risk_level":2,"risk_comment":"1文"}],"exit_alerts":[{"code":"","name":"","alert_type":"","message":"","suggested_action":""}],"caution":"注意事項orNull"}
"""


# ── validate_recommendation（修正E） ───────────────────────────────────────

def validate_recommendation(rec: dict) -> dict:
    """
    推奨のリスクリワード比を検証し、不合格なら _invalid=True を立てる。
    RR比 < 1.5 は除外。短期で損切り幅 > 5% は警告のみ。
    """
    buy = rec.get("buy_price", 0)
    tp = rec.get("take_profit_price", 0)
    sl = rec.get("stop_loss_price", 0)

    if not buy or buy <= 0 or not sl or sl <= 0:
        rec["_invalid"] = True
        rec["_invalid_reason"] = "価格が0以下"
        return rec

    reward = tp - buy
    risk = buy - sl

    if risk <= 0:
        rec["_invalid"] = True
        rec["_invalid_reason"] = "損切りが買値以上"
        return rec

    rr_ratio = reward / risk
    rec["rr_ratio"] = round(rr_ratio, 2)

    if rr_ratio < 1.5:
        rec["_invalid"] = True
        rec["_invalid_reason"] = f"RR比 {rr_ratio:.2f} < 1.5"
        return rec

    # 短期で損切り幅が 5% 超は警告
    holding = rec.get("holding_days", 10)
    sl_pct = (buy - sl) / buy * 100
    if holding <= 3 and sl_pct > 5.0:
        rec["_warning"] = f"短期なのに損切り幅 {sl_pct:.1f}% は広い"

    rec["_invalid"] = False
    return rec


# ── format_screener_for_prompt（修正F） ────────────────────────────────────

def format_screener_for_prompt(stocks: list, macro_result: dict) -> str:
    """
    スクリーナー結果をLLM用の圧縮テキスト（ヘッダー付きCSV + 事前計算価格）に変換。
    ユーザープロンプトのトークン数を削減する。
    """
    lines = []
    lines.append(f"## マクロ: {macro_result['condition']} — {macro_result['flags_text']}")
    if macro_result.get("risk_adjustment"):
        lines.append(f"リスク調整: +{macro_result['risk_adjustment']}")
    lines.append("")

    # ヘッダー付きCSV（1行ヘッダー + 銘柄ごと1行）
    lines.append("## 候補銘柄データ")
    lines.append("code|name|sector|close|bo5d|dvs|rsi5|rsi14|vol|w52|ptn|sma25|ex_div|earn")

    for s in stocks:
        close = s.get("price") or s.get("close") or s.get("current_price") or 0
        dvs = s.get("directional_vol_score", 0) or 0
        rsi5 = s.get("rsi5") or 0
        rsi14 = s.get("rsi_14") or s.get("rsi14") or 0
        vol = s.get("vol_ratio") or 0
        w52 = s.get("week52_pos_pct") or 0
        ptn = s.get("candle_pattern") or "none"
        sma25 = s.get("sma25") or 0
        bo5d = "T" if s.get("breakout_5d") else "F"
        ex_div = s.get("days_to_ex_dividend")
        earn = s.get("days_to_earnings")
        lines.append(
            f"{s['code']}|{s['name']}|{s.get('sector', '')}|{close}"
            f"|{bo5d}|{dvs:.0f}|{rsi5:.0f}|{rsi14:.0f}"
            f"|{vol:.2f}|{w52:.0f}|{ptn}|{sma25:.0f}"
            f"|{ex_div if ex_div is not None else '-'}"
            f"|{earn if earn is not None else '-'}"
        )

    lines.append("")
    lines.append("## 事前計算済み価格（この値をそのまま使え・自分で計算するな）")
    for s in stocks:
        pc = s.get("price_candidates")
        if not pc:
            continue
        for period_key, label in [("short_term", "短期"), ("medium_term", "中期")]:
            p = pc.get(period_key, {})
            bn = p.get("buy_now", {})
            bd = p.get("buy_dip", {})
            if not bn:
                continue
            lines.append(
                f"{s['code']}[{label}]:"
                f" 今すぐ→buy={bn.get('buy_price')}"
                f",tp={bn.get('take_profit_low')}-{bn.get('take_profit_high')}"
                f",sl={bn.get('stop_loss')}"
                f" / 押し目→buy={bd.get('buy_price')}"
                f",tp={bd.get('take_profit_low')}-{bd.get('take_profit_high')}"
                f",sl={bd.get('stop_loss')}"
            )

    return "\n".join(lines)


# ── build_user_prompt ──────────────────────────────────────────────────────

def build_user_prompt(
    screened_stocks: list,
    market_data: dict,
    market_news: list | None = None,
    macro_result: dict | None = None,
) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    nikkei = market_data.get("nikkei", "N/A")
    nikkei_change = market_data.get("nikkei_change", "N/A")
    usdjpy = market_data.get("usdjpy", "N/A")

    if macro_result is None:
        macro_result = {"condition": "不明", "flags_text": "データなし", "risk_adjustment": 0}

    downtrend_note = ""
    if market_data.get("nikkei_trend") == "下落":
        downtrend_note = "\n⚠️ 日経25日線割れ。買い推奨は最小限に。"

    # ニュースは最大 10 件・タイトル 60 文字に制限してトークン節約
    news_lines = []
    for n in (market_news or [])[:10]:
        pub = n.get("published", "")[:10]
        title = n.get("title", "")[:60]
        news_lines.append(f"・{pub} {title}")
    news_text = "\n".join(news_lines) if news_lines else "なし"

    screener_text = format_screener_for_prompt(screened_stocks, macro_result)

    return (
        f"分析日: {today}\n"
        f"日経: {nikkei}円({nikkei_change}%) USD/JPY: {usdjpy}円{downtrend_note}\n"
        f"\n## 最新ニュース\n{news_text}\n"
        f"\n{screener_text}\n"
    )


# ── analyze ───────────────────────────────────────────────────────────────

def analyze(
    screened_stocks: list,
    market_data: dict,
    market_news: list | None = None,
    macro_result: dict | None = None,
) -> dict:
    """Claude API で分析を実行し、推奨 dict を返す。"""
    if DRY_RUN:
        return _dummy_analysis(screened_stocks)

    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        timeout=httpx.Timeout(120.0, connect=10.0),
    )
    user_prompt = build_user_prompt(screened_stocks, market_data, market_news, macro_result)

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = message.content[0].text.strip()
            # JSON ブロックを抽出（```json ... ``` の場合に対応）
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)

            # RR比バリデーション（修正E）
            validated = [validate_recommendation(r) for r in result.get("recommendations", [])]
            valid = [r for r in validated if not r.get("_invalid", False)]
            if len(valid) < len(validated):
                print(f"[claude_analyzer] RR比不足で {len(validated) - len(valid)} 件の推奨を除外")
            result["recommendations"] = valid
            if "exit_alerts" not in result:
                result["exit_alerts"] = []
            return result

        except Exception as e:
            wait = 2 ** attempt
            print(f"[claude_analyzer] 試行 {attempt + 1}/{MAX_RETRIES} 失敗: {e}。{wait}秒後にリトライ")
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)

    raise RuntimeError("Claude API の呼び出しが全リトライで失敗しました")


# ── DRY_RUN 用ダミー ────────────────────────────────────────────────────────

def _dummy_analysis(screened_stocks: list) -> dict:
    """DRY_RUN 用のダミー分析結果。"""
    recommendations = []
    for i, stock in enumerate(screened_stocks[:3]):
        actions = ["今すぐ買う", "押し目待ち", "見送り"]
        recommendations.append({
            "rank": i + 1,
            "code": stock["code"],
            "name": stock["name"],
            "action": actions[i],
            "current_price": 2850,
            "buy_price": 2830,
            "target_price": 3000,
            "take_profit_price": 3000,
            "stop_loss_price": 2745,
            "upside_pct": 6.0,
            "rr_ratio": 2.0,
            "reason": "5日高値をブレイク中。方向性出来高も正でモメンタム確認。",
            "key_signal": "breakout_5d×dvs+80×rsi5=28",
            "holding_days": 3,
            "exit_timing": "目標到達または3営業日後",
            "risk_level": 2,
            "risk_comment": "為替リスクあり",
            "_invalid": False,
        })
    return {
        "market_condition": "注意",
        "market_comment": "日経平均は小幅下落。様子見が続く。",
        "recommendations": recommendations,
        "exit_alerts": [],
        "caution": "米国市場の動向に注意。",
    }

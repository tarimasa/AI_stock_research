"""
claude_analyzer.py
スクリーニング通過銘柄を Claude API で分析し推奨レポートを生成する。

v2 変更点:
- システムプロンプトを 800 トークン以下に圧縮（タイムアウト対策）
- max_tokens 2048 → 4096、タイムアウト 60 → 120 秒
- 価格計算を price_calculator.py に移行。LLM は action/holding_days 判断のみ
- validate_recommendation() でRR比 < 1.5 の推奨を除外
- format_screener_for_prompt() でユーザープロンプトをCSV形式に圧縮

J-Quants 統合による追加変更:
- タイムアウトを 120 → 180 秒に延長（全銘柄スキャン対応）
- analyze_with_claude_safe() でトークン数チェック後に自動候補削減
- analyze_with_claude_cached() でプロンプトキャッシュ有効化（応答時間短縮）
- Stage 2 入力候補を最大10件に制限（タイムアウト対策）
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
# Stage1 通過候補の Stage2 投入上限。screener.MAX_STOCKS と同期させる。
# 機会損失抑制のため 10 → 20 に拡張（PR #20 設計判断、20 件運用で品質と速度の両立）。
_MAX_CANDIDATES = int(os.environ.get("MAX_STOCKS_TO_ANALYZE", 20))
# タイムアウトを 240 秒に延長（候補 20 件で平均 15-30 秒、外れ値考慮）
_TIMEOUT = httpx.Timeout(240.0, connect=10.0)
# 短期投資の運用判断には再現性が重要なので temperature=0 で確定的応答にする。
# 同一プロンプトに対して毎回同じ推奨が返るようになる（ユーザー報告 #20 対応）。
# 環境変数 CLAUDE_TEMPERATURE で上書き可能（探索用に 0.3 などにも設定可能）。
_TEMPERATURE = float(os.environ.get("CLAUDE_TEMPERATURE", 0.0))

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
    lines.append("code|name|sector|close|bo5d|dvs|rsi5|rsi14|vol|w52|ptn|sma25|ex_div|earn|sig")

    for s in stocks:
        close = s.get("price") or s.get("close") or s.get("current_price") or 0
        # フルスキャン(dvs/w52_pos)とウォッチリスト(directional_vol_score/week52_pos_pct)の両フィールド名に対応
        dvs = s.get("directional_vol_score") if s.get("directional_vol_score") is not None else (s.get("dvs") or 0)
        rsi5 = s.get("rsi5") or 0
        rsi14 = s.get("rsi_14") or s.get("rsi14") or 0
        vol = s.get("vol_ratio") or 0
        w52 = s.get("week52_pos_pct") if s.get("week52_pos_pct") is not None else (s.get("w52_pos") or 0)
        ptn = s.get("candle_pattern") or "none"
        sma25 = s.get("sma25") or 0
        bo5d = "T" if s.get("breakout_5d") else "F"
        ex_div = s.get("days_to_ex_dividend")
        earn = s.get("days_to_earnings")
        # フルスキャン由来の stage1_signals をシグナル列に追加
        sig = "|".join(s.get("stage1_signals", [])) or "-"
        lines.append(
            f"{s['code']}|{s['name']}|{s.get('sector', '')}|{close}"
            f"|{bo5d}|{dvs:.0f}|{rsi5:.0f}|{rsi14:.0f}"
            f"|{vol:.2f}|{w52:.0f}|{ptn}|{sma25:.0f}"
            f"|{ex_div if ex_div is not None else '-'}"
            f"|{earn if earn is not None else '-'}"
            f"|{sig}"
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

def _parse_claude_response(message) -> dict:
    """Claude API レスポンスをパースして dict を返す共通処理。"""
    raw = message.content[0].text.strip()
    # JSON ブロックを抽出（```json ... ``` の場合に対応）
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    result = json.loads(raw)

    # RR比バリデーション
    validated = [validate_recommendation(r) for r in result.get("recommendations", [])]
    valid = [r for r in validated if not r.get("_invalid", False)]
    if len(valid) < len(validated):
        print(f"[claude_analyzer] RR比不足で {len(validated) - len(valid)} 件の推奨を除外")
    result["recommendations"] = valid
    result["all_recommendations"] = validated  # 除外分も含む全推奨を保持（LINE詳細表示用）
    if "exit_alerts" not in result:
        result["exit_alerts"] = []
    return result


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
        timeout=_TIMEOUT,
    )
    user_prompt = build_user_prompt(screened_stocks, market_data, market_news, macro_result)

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                temperature=_TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return _parse_claude_response(message)

        except Exception as e:
            wait = 2 ** attempt
            print(f"[claude_analyzer] 試行 {attempt + 1}/{MAX_RETRIES} 失敗: {e}。{wait}秒後にリトライ")
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)

    raise RuntimeError("Claude API の呼び出しが全リトライで失敗しました")


# ── analyze_with_claude_safe（4-3a: トークン計測付き安全版）────────────────

# Stage 2 入力トークン上限（超えたら候補を自動削減）
# 候補 20 件想定で 5000 トークンまで許容（システム+ユーザー合計）
_MAX_INPUT_TOKENS = 5000
_MIN_CANDIDATES = 5


def _count_tokens(client: anthropic.Anthropic, system: str, user: str) -> int:
    """入力トークン数を計測する。"""
    try:
        resp = client.messages.count_tokens(
            model=MODEL,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return resp.input_tokens
    except Exception:
        # count_tokens 失敗時は文字数の 1/4 で近似
        return (len(system) + len(user)) // 4


def _build_fullscan_prompt(
    candidates: list[dict],
    macro_result: dict,
    master: dict,
    upcoming_earnings: dict,
) -> str:
    """
    全銘柄スキャン候補用のコンパクトプロンプトを生成する（4-3 対策3）。
    record-delimiter 形式でトークン効率を最大化。
    """
    lines: list[str] = []

    lines.append(
        f"M:{macro_result.get('condition','不明')}|"
        f"{macro_result.get('flags_text','なし')}|"
        f"risk+{macro_result.get('risk_adjustment', 0)}"
    )
    lines.append("")
    lines.append("#c|n|s|p|b|d|r5|r14|v|w|sm|e|sg")

    for s in candidates:
        code = s.get("code", "")
        info_m = master.get(code, {})
        # 名前は切り詰めずフルで渡す（[:6]切り詰めはClaudeの幻覚の原因となるため撤廃）。
        # Claudeが返した name は後段で master を使って上書き（report._fix_recommendation_names）。
        name = info_m.get("name", s.get("name", ""))
        sector = info_m.get("sector33", s.get("sector", ""))[:4]
        earn = upcoming_earnings.get(code, {})
        earn_str = str(earn.get("days_until", "-")) if earn else "-"
        sig = "+".join(s.get("stage1_signals", []))[:20]
        bo = "T" if s.get("breakout_5d") else "F"

        lines.append(
            f"{code}|{name}|{sector}|{s.get('close', 0)}"
            f"|{bo}|{s.get('dvs', 0)}|{s.get('rsi5', 50)}|{s.get('rsi14', 50)}"
            f"|{s.get('vol_ratio', 1)}|{s.get('w52_pos', 50)}|{int(s.get('sma25', 0))}"
            f"|{earn_str}|{sig}"
        )

    lines.append("")
    lines.append("#PRICE(code:now=buy/tpL-tpH/sl|dip=buy/tpL-tpH/sl)")
    for s in candidates:
        pc = s.get("price_candidates")
        if not pc:
            continue
        bn = pc.get("buy_now", {})
        bd = pc.get("buy_dip", {})
        if not bn:
            continue
        lines.append(
            f"{s['code']}:"
            f"now={bn.get('buy_price')}/{bn.get('take_profit_low')}-{bn.get('take_profit_high')}/{bn.get('stop_loss')}|"
            f"dip={bd.get('buy_price')}/{bd.get('take_profit_low')}-{bd.get('take_profit_high')}/{bd.get('stop_loss')}"
        )

    return "\n".join(lines)


def analyze_with_claude_safe(
    candidates: list[dict],
    macro_result: dict,
    master: dict,
    upcoming_earnings: dict,
) -> dict:
    """
    全銘柄スキャン版の Claude 分析（4-3a: トークン数チェック付き）。
    入力トークンが上限を超えたら候補を自動削減してリトライする。

    Args:
        candidates: run_full_scan() が返した Stage 1 通過候補（最大10件）
        macro_result: preprocess_macro() の結果
        master: get_master() の結果
        upcoming_earnings: get_upcoming_earnings() の結果

    Returns:
        Claude が選定した推奨 dict
    """
    if DRY_RUN:
        return _dummy_analysis(candidates)

    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        timeout=_TIMEOUT,
    )

    current_candidates = list(candidates[:_MAX_CANDIDATES])

    # トークン数が収まるまで候補を2件ずつ削減
    while len(current_candidates) >= _MIN_CANDIDATES:
        user = _build_fullscan_prompt(
            current_candidates, macro_result, master, upcoming_earnings
        )
        tokens = _count_tokens(client, SYSTEM_PROMPT, user)
        print(f"[claude_analyzer] candidates={len(current_candidates)}, input_tokens={tokens}")

        if tokens <= _MAX_INPUT_TOKENS:
            break
        current_candidates = current_candidates[:-2]

    if len(current_candidates) < _MIN_CANDIDATES:
        current_candidates = list(candidates[:_MIN_CANDIDATES])
        user = _build_fullscan_prompt(
            current_candidates, macro_result, master, upcoming_earnings
        )

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                temperature=_TEMPERATURE,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
            return _parse_claude_response(message)

        except anthropic.APITimeoutError:
            wait = 2 ** attempt
            print(f"[claude_analyzer] タイムアウト。候補を削減してリトライ ({wait}秒後)")
            if attempt < MAX_RETRIES - 1:
                # タイムアウト時は候補をさらに削減
                if len(current_candidates) > _MIN_CANDIDATES:
                    current_candidates = current_candidates[:-2]
                    user = _build_fullscan_prompt(
                        current_candidates, macro_result, master, upcoming_earnings
                    )
                time.sleep(wait)
        except Exception as e:
            wait = 2 ** attempt
            print(f"[claude_analyzer] 試行 {attempt + 1}/{MAX_RETRIES} 失敗: {e}。{wait}秒後にリトライ")
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)

    raise RuntimeError("Claude API の呼び出しが全リトライで失敗しました")


def analyze_with_claude_cached(
    candidates: list[dict],
    macro_result: dict,
    master: dict,
    upcoming_earnings: dict,
) -> dict:
    """
    プロンプトキャッシュを有効化した Claude 分析（Layer 5 / 5-3 最適化）。
    システムプロンプトをキャッシュすることで、2回目以降の応答時間を短縮し
    入力トークン課金を 90% 削減する。
    """
    if DRY_RUN:
        return _dummy_analysis(candidates)

    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        timeout=_TIMEOUT,
    )

    user = _build_fullscan_prompt(
        candidates[:_MAX_CANDIDATES], macro_result, master, upcoming_earnings
    )

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                temperature=_TEMPERATURE,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},  # プロンプトキャッシュ
                    }
                ],
                messages=[{"role": "user", "content": user}],
            )
            usage = message.usage
            cache_read = getattr(usage, "cache_read_input_tokens", 0)
            cache_create = getattr(usage, "cache_creation_input_tokens", 0)
            print(f"[claude_analyzer] cache_read={cache_read}, cache_created={cache_create}")
            return _parse_claude_response(message)

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

"""
claude_analyzer.py
スクリーニング通過銘柄を Claude API で分析し推奨レポートを生成する。
"""

import json
import os
import time
from datetime import datetime

import anthropic
from dotenv import load_dotenv

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MODEL = "claude-haiku-4-5-20251001"
MAX_RETRIES = 3

SYSTEM_PROMPT = """
あなたは日本株の個人投資家向けアドバイザーです。
ユーザーはSBI証券でIFDOCO注文（指値買い→利確指値＋損切り逆指値の同時発注）を使います。
以下のルールを必ず守って分析・推奨を行ってください。

【大前提: 目的は短期売買による利益最大化】
- ユーザーは配当受取を目的としない。数日〜数週間での売却を前提とする。
- IFDOCO注文（指値買い→利確指値＋損切り逆指値）を使った短期取引を支援する。
- 配当利回りの高さは保有の根拠にしないこと。

【選定ルール】
1. 推奨銘柄を1〜3本に必ず絞ること（多すぎると初心者が迷うため）
2. 必ず**異なるセクター**から選ぶこと（同セクターの銘柄を複数推奨しない）
3. 以下の追加指標を積極的に活用し、テクニカルだけでなく多面的に判断すること：
   - vol_ratio（出来高比率）: 1.5倍以上は資金流入シグナル、安値圏での急増は特に強い
   - week52_pos_pct（52週レンジ位置）: 30%以下は年間安値圏で反発期待大
   - days_to_ex_dividend（権利落ち日まで日数）:
     【重要・短期売買に直結】権利付最終日（≒権利落ち日-1日）の1〜2週前は
     「配当取り目的の機関投資家・個人の買い需要」で統計的に株価上昇バイアスあり。
     売却タイミングは権利付最終日当日〜直前推奨（権利落ち後は価格調整が入る）。
     ・3〜10日前: 買い需要ピーク、最も強い上昇バイアス
     ・11〜20日前: 機関投資家の仕込み本格化
     ・21〜35日前: 先行買い開始期
   - ma25_diff_pct（25日線乖離率）: -4%以下の調整は押し目買いチャンス
4. スコアだけでなく**出来高急増×安値圏×権利確定日接近**の複合シグナルを最重視すること
5. 同じ銘柄（特にトヨタ・ソフトバンク・NTT・ソニー）を毎回選ばないこと。
   スコア上位でも前回と異なる視点・銘柄で提案を試みること。
6. 配当利回り（div_yield_pct）はスコア対象外・参考情報のみ。
   高配当でも「権利落ち日接近」でない限り、それを推奨理由にしないこと。

【分類ルール】
6. 各銘柄について「今すぐ買う」「押し目待ち」「見送り」の3択で明確に分類すること
7. 推奨理由は平易な言葉で2〜3文に収め、どの指標が決め手かを必ず言及すること
8. リスクを★1〜3で示すこと（★1:低リスク、★3:高リスク）
9. 目標株価を必ず提示すること（根拠：過去レジスタンス、52週高値、PER適正水準等）
10. 相場全体が悪い場合は「本日は買い見送り推奨」と明示すること

【IFDOCO価格算出ルール】
11. IFDOCO注文用に以下の3価格を必ず算出すること（10円単位で丸める）
   - buy_price: 指値買い価格
     「今すぐ買う」→ 現在値の-0.5〜-1.0%
     「押し目待ち」→ SMA25 または現在値の-2〜-4%
   - take_profit_price: 利確売り指値（= target_price と同じ）
   - stop_loss_price: 損切り逆指値
     buy_price × (1 - stop_loss_pct/100) で算出。
     デフォルト stop_loss_pct=8。リスク★3なら10、★1なら5。

【出力フォーマット】
JSON形式のみで出力すること（コードブロック不要）。
{
  "market_condition": "良好|注意|悪化",
  "market_comment": "相場全体の一言コメント",
  "recommendations": [
    {
      "rank": 1,
      "code": "7203.T",
      "name": "トヨタ自動車",
      "sector": "自動車",
      "action": "今すぐ買う|押し目待ち|見送り",
      "current_price": 2850,
      "buy_price": 2830,
      "target_price": 3100,
      "take_profit_price": 3100,
      "stop_loss_price": 2610,
      "upside_pct": 8.8,
      "reason": "推奨理由（2〜3文、決め手となった指標を必ず言及）",
      "key_signal": "出来高1.8倍×52週安値圏25%×権利落ち12日前",
      "exit_timing": "権利付最終日当日に売却推奨 / またはtarget_price到達時",
      "risk_level": 2,
      "risk_comment": "リスクの内容"
    }
  ],
  "caution": "本日の注意事項（なければnull）"
}
"""


def build_user_prompt(screened_stocks: list, market_data: dict) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    nikkei_trend = market_data.get("nikkei_trend", "不明")
    nikkei_vs_sma25 = market_data.get("nikkei_vs_sma25_pct", 0)
    trend_note = f"／ 25日線比 {nikkei_vs_sma25:+.1f}% ／ トレンド: {nikkei_trend}"
    downtrend_warning = (
        "\n⚠️ 日経平均が25日移動平均を下回っています。全体相場が弱い環境です。"
        "market_conditionは原則「悪化」または「注意」とし、「今すぐ買う」推奨は最小限にしてください。"
        if nikkei_trend == "下落" else ""
    )
    # 各銘柄の追加シグナルを読みやすく整形
    signals_summary = []
    for s in screened_stocks:
        sig_parts = []
        vol = s.get("vol_ratio")
        if vol is not None:
            sig_parts.append(f"出来高比率:{vol}倍")
        pos = s.get("week52_pos_pct")
        if pos is not None:
            sig_parts.append(f"52週レンジ位置:{pos}%")
        days_ex = s.get("days_to_ex_dividend")
        if days_ex is not None:
            if days_ex > 0:
                sig_parts.append(f"権利落ち日まで:{days_ex}日（権利付最終日まで約{days_ex-1}日）")
            elif days_ex == 0:
                sig_parts.append("本日権利落ち")
            else:
                sig_parts.append(f"権利落ち後:{abs(days_ex)}日経過")
        div = s.get("div_yield_pct")
        if div is not None and div > 0:
            sig_parts.append(f"配当利回り:{div}%（参考値・スコア対象外）")
        ma = s.get("ma25_diff_pct")
        if ma is not None:
            sig_parts.append(f"25日線乖離:{ma:+.1f}%")
        signals_summary.append(f"  {s['code']} {s['name']}（{s.get('sector','')}）スコア{s.get('score',0)}: {', '.join(sig_parts)}")

    signals_text = "\n".join(signals_summary) if signals_summary else "  （追加シグナルなし）"

    return f"""
## 本日の市場状況
- 日経平均: {market_data.get('nikkei', 'N/A')}円（前日比{market_data.get('nikkei_change', 'N/A')}%{trend_note}）
- ドル円: {market_data.get('usdjpy', 'N/A')}円
- 分析日: {today}{downtrend_warning}

## スクリーニング通過銘柄（上位{len(screened_stocks)}本）の追加シグナル要約
{signals_text}

## スクリーニング通過銘柄の詳細データ
{json.dumps(screened_stocks, ensure_ascii=False, indent=2)}

上記データをもとに、セクターが重複しないよう注意しながら、
初心者投資家向けの推奨レポートをJSON形式で生成してください。
特に出来高急増・52週安値圏・高配当などの複合シグナルを重視してください。
"""


def analyze(screened_stocks: list, market_data: dict) -> dict:
    """Claude API で分析を実行し、推奨 dict を返す。"""
    if DRY_RUN:
        return _dummy_analysis(screened_stocks)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    user_prompt = build_user_prompt(screened_stocks, market_data)

    for attempt in range(MAX_RETRIES):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = message.content[0].text.strip()
            # JSON ブロックを抽出（```json ... ``` の場合に対応）
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except Exception as e:
            wait = 2 ** attempt
            print(f"[claude_analyzer] 試行 {attempt + 1}/{MAX_RETRIES} 失敗: {e}。{wait}秒後にリトライ")
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)

    raise RuntimeError("Claude API の呼び出しが全リトライで失敗しました")


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
            "target_price": 3100,
            "take_profit_price": 3100,
            "stop_loss_price": 2600,
            "upside_pct": 8.8,
            "reason": "テクニカル指標が割安圏。RSIが売られすぎ水準で反発期待。業績も好調。",
            "risk_level": 2,
            "risk_comment": "為替リスクあり（★★☆）",
        })
    return {
        "market_condition": "注意",
        "market_comment": "日経平均は小幅下落。全体的に様子見が続く。",
        "recommendations": recommendations,
        "caution": "米国市場の動向に注意。",
    }

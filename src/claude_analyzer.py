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
以下のルールを必ず守って分析・推奨を行ってください。

【出力ルール】
1. 推奨銘柄を1〜3本に必ず絞ること（多すぎると初心者が迷うため）
2. 各銘柄について「今すぐ買う」「押し目待ち」「見送り」の3択で明確に分類すること
3. 推奨理由は小学生でもわかる平易な言葉で2〜3文に収めること
4. リスクを★1〜3で示すこと（★1:低リスク、★3:高リスク）
5. 目標株価を必ず提示すること（根拠も1文で）
6. 相場全体が悪い場合は「本日は買い見送り推奨」と明示すること

【出力フォーマット】
JSON形式で出力すること。
{
  "market_condition": "良好|注意|悪化",
  "market_comment": "相場全体の一言コメント",
  "recommendations": [
    {
      "rank": 1,
      "code": "7203.T",
      "name": "トヨタ自動車",
      "action": "今すぐ買う|押し目待ち|見送り",
      "current_price": 2850,
      "target_price": 3100,
      "upside_pct": 8.8,
      "reason": "推奨理由（2〜3文）",
      "risk_level": 2,
      "risk_comment": "リスクの内容"
    }
  ],
  "caution": "本日の注意事項（なければnull）"
}
"""


def build_user_prompt(screened_stocks: list, market_data: dict) -> str:
    today = datetime.today().strftime("%Y-%m-%d")
    return f"""
## 本日の市場状況
- 日経平均: {market_data.get('nikkei', 'N/A')}円（前日比{market_data.get('nikkei_change', 'N/A')}%）
- ドル円: {market_data.get('usdjpy', 'N/A')}円
- 分析日: {today}

## スクリーニング通過銘柄（上位{len(screened_stocks)}本）
{json.dumps(screened_stocks, ensure_ascii=False, indent=2)}

上記データをもとに、初心者投資家向けの推奨レポートをJSON形式で生成してください。
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
            "target_price": 3100,
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

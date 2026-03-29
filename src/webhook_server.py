"""
webhook_server.py
LINE Messaging API の Webhook と LIFF フォームからのリクエストを受信する FastAPI サーバー。
Azure Container Apps にデプロイし常時稼働させる。
"""

import os
import re
import sys
from pathlib import Path
from typing import Literal

import requests as _requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

import data_fetcher
import line_notifier
import portfolio_store

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
# LIFF からのリクエストを許可するオリジン（デプロイ先URLに変更）
LIFF_ORIGIN = os.environ.get("LIFF_ORIGIN", "https://liff.line.me")

# 起動時に必須環境変数を検証
if not LINE_CHANNEL_SECRET:
    raise RuntimeError("LINE_CHANNEL_SECRET が設定されていません")
if not LINE_CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN が設定されていません")

app = FastAPI(title="AI株式リサーチBot Webhook")

# CORS: LIFF オリジンのみ許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=[LIFF_ORIGIN],
    allow_methods=["POST"],
    allow_headers=["Content-Type", "Authorization"],
)

handler = WebhookHandler(LINE_CHANNEL_SECRET)

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)


# ─────────────────────────────────────────
# コマンドパース
# ─────────────────────────────────────────

def parse_command(text: str) -> dict | None:
    """
    「追加 7203 100 2650」→ {"action": "add", "code": "7203.T", "shares": 100, "price": 2650}
    「削除 7203」         → {"action": "remove", "code": "7203.T"}
    「一覧」              → {"action": "list"}
    「ヘルプ」            → {"action": "help"}
    解析失敗              → None
    """
    text = text.strip().replace("\u3000", " ")  # 全角スペース対応
    parts = text.split()
    if not parts:
        return None

    cmd = parts[0]

    if cmd in ["追加", "add"]:
        if len(parts) < 4:
            return None
        code = parts[1] + ".T" if not parts[1].endswith(".T") else parts[1]
        try:
            shares = int(re.sub(r"[^\d]", "", parts[2]))
            price = int(re.sub(r"[^\d]", "", parts[3]))
        except ValueError:
            return None
        return {"action": "add", "code": code, "shares": shares, "price": price}

    elif cmd in ["削除", "remove", "売却"]:
        if len(parts) < 2:
            return None
        code = parts[1] + ".T" if not parts[1].endswith(".T") else parts[1]
        return {"action": "remove", "code": code}

    elif cmd in ["一覧", "list", "ポートフォリオ"]:
        return {"action": "list"}

    elif cmd in ["ヘルプ", "help"]:
        return {"action": "help"}

    return None


# ─────────────────────────────────────────
# コマンド実行
# ─────────────────────────────────────────

def execute_command(cmd: dict) -> str:
    action = cmd["action"]

    if action == "help":
        return (
            "📋 コマンド一覧\n\n"
            "追加 [コード] [株数] [単価]\n"
            "  例: 追加 7203 100 2650\n\n"
            "削除 [コード]\n"
            "  例: 削除 7203\n\n"
            "一覧  → 保有株一覧を表示\n"
            "ヘルプ → このメッセージ"
        )

    portfolio = portfolio_store.load_portfolio()
    holdings = portfolio.get("holdings", [])

    if action == "list":
        if not holdings:
            return "📦 保有株はありません。\n「追加 7203 100 2650」の形式で追加できます。"
        lines = [f"📦 保有株一覧（{len(holdings)}銘柄）\n"]
        total_pnl = 0
        for h in holdings:
            try:
                stock_data = data_fetcher.fetch_stock_data(h["code"])
                current = stock_data.get("price", h["buy_price"])
            except Exception:
                current = h["buy_price"]
            pnl = (current - h["buy_price"]) * h["shares"]
            pnl_pct = (current - h["buy_price"]) / h["buy_price"] * 100
            total_pnl += pnl
            emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            lines.append(
                f"{h['code'].replace('.T', '')} {h['name']}  "
                f"{h['shares']}株 ¥{current:,.0f} {pnl_pct:+.1f}% {emoji}"
            )
        lines.append(f"\n━━━━━━━━━━\n合計評価損益: {total_pnl:+,.0f}円")
        return "\n".join(lines)

    if action == "add":
        code = cmd["code"]
        shares = cmd["shares"]
        price = cmd["price"]
        # yfinance で銘柄名を取得（失敗したらコードをそのまま使う）
        try:
            info = data_fetcher.fetch_info(code)
            name = info.get("longName") or info.get("shortName") or code
        except Exception:
            name = code

        # 既存銘柄は上書き（同一コードで更新）
        holdings = [h for h in holdings if h["code"] != code]
        holdings.append({
            "code": code,
            "name": name,
            "shares": shares,
            "buy_price": price,
            "buy_date": __import__("datetime").date.today().isoformat(),
            "target_price": None,
            "stop_loss_pct": portfolio.get("default_alerts", {}).get("loss_pct", -8),
            "memo": "",
        })
        portfolio["holdings"] = holdings
        portfolio_store.save_portfolio(portfolio)
        total = shares * price
        return (
            f"✅ {name} を追加しました\n"
            f"{shares}株 × ¥{price:,} = 取得額 ¥{total:,}\n"
            f"目標株価未設定（デフォルト+15%で利確アラート）"
        )

    if action == "remove":
        code = cmd["code"]
        target = next((h for h in holdings if h["code"] == code), None)
        if not target:
            return f"⚠️ {code.replace('.T', '')} は保有リストに見つかりません。"
        try:
            stock_data = data_fetcher.fetch_stock_data(code)
            current = stock_data.get("price", target["buy_price"])
        except Exception:
            current = target["buy_price"]
        pnl = (current - target["buy_price"]) * target["shares"]
        pnl_pct = (current - target["buy_price"]) / target["buy_price"] * 100

        # 保有日数
        try:
            from datetime import date
            buy_date = date.fromisoformat(target.get("buy_date", date.today().isoformat()))
            days = (date.today() - buy_date).days
        except Exception:
            days = 0

        holdings = [h for h in holdings if h["code"] != code]
        portfolio["holdings"] = holdings
        portfolio_store.save_portfolio(portfolio)
        return (
            f"🗑️ {target['name']} を削除しました\n"
            f"売却損益: {pnl:+,.0f}円（{pnl_pct:+.1f}%）\n"
            f"保有期間: {days}日間"
        )

    return "⚠️ 不明なコマンドです。「ヘルプ」と送ってください。"


# ─────────────────────────────────────────
# LINE Webhook エンドポイント
# ─────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return {"status": "ok"}


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    text = event.message.text
    reply_token = event.reply_token

    cmd = parse_command(text)
    if cmd is None:
        reply_text = (
            "⚠️ コマンドを認識できませんでした。\n"
            "「ヘルプ」と送るとコマンド一覧を確認できます。"
        )
    else:
        try:
            reply_text = execute_command(cmd)
        except Exception as e:
            reply_text = f"⚠️ 処理中にエラーが発生しました: {e}"

    with ApiClient(line_config) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


# ─────────────────────────────────────────
# LIFF フォーム用エンドポイント
# ─────────────────────────────────────────

class PortfolioRequest(BaseModel):
    action: Literal["add", "remove", "list"]   # 列挙型で不正アクションを拒否
    code: str | None = Field(default=None, pattern=r"^\d{4}$")  # 4桁数字のみ
    shares: int | None = Field(default=None, ge=1, le=100_000)  # 上限10万株
    price: int | None = Field(default=None, ge=1, le=100_000_000)  # 上限1億円


def _verify_liff_token(access_token: str) -> str:
    """
    LIFF アクセストークンを LINE API で検証し、ユーザー ID を返す。
    検証失敗時は HTTPException(401) を送出する。
    """
    resp = _requests.get(
        "https://api.line.me/oauth2/v2.1/verify",
        params={"access_token": access_token},
        timeout=5,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid LIFF access token")
    data = resp.json()
    # client_id がチャンネル ID と一致するか確認（オプショナルだが推奨）
    user_id = data.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    return user_id


@app.post("/portfolio")
async def portfolio_endpoint(
    payload: PortfolioRequest,
    authorization: str | None = Header(default=None),
):
    # LIFF アクセストークンをサーバー側で検証してユーザー ID を取得
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header required")
    liff_token = authorization.removeprefix("Bearer ")
    verified_user_id = _verify_liff_token(liff_token)

    cmd: dict = {"action": payload.action}
    if payload.code:
        code = payload.code.strip()
        cmd["code"] = code + ".T" if not code.endswith(".T") else code
    if payload.shares:
        cmd["shares"] = payload.shares
    if payload.price:
        cmd["price"] = payload.price

    try:
        reply_text = execute_command(cmd)
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")

    # LINE Push で結果を通知
    try:
        line_notifier.push_message([{"type": "text", "text": reply_text}])
    except Exception:
        pass

    return {"status": "ok", "message": reply_text}


# ─────────────────────────────────────────
# ヘルスチェック
# ─────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8000, reload=False)

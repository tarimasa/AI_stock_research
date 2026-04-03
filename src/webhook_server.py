"""
webhook_server.py
LINE Messaging API の Webhook と LIFF フォームからのリクエストを受信する FastAPI サーバー。
Azure Container Apps にデプロイし常時稼働させる。
"""

import asyncio
import os
import re
import sys
import threading
import uuid
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
import report as report_module

# レポート再生成の多重実行を防ぐロック
_report_lock = threading.Lock()

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
# LIFF からのリクエストを許可するオリジン（デプロイ先URLに変更）
LIFF_ORIGIN = os.environ.get("LIFF_ORIGIN", "https://liff.line.me")


from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時に必須環境変数を検証（テスト時の import では実行されない）
    if not LINE_CHANNEL_SECRET:
        raise RuntimeError("LINE_CHANNEL_SECRET が設定されていません")
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN が設定されていません")
    yield


app = FastAPI(title="AI株式リサーチBot Webhook", lifespan=lifespan)

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

    elif cmd in ["更新", "refresh", "再取得"]:
        return {"action": "refresh"}

    return None


# ─────────────────────────────────────────
# コマンド実行
# ─────────────────────────────────────────

def execute_command(cmd: dict) -> str:
    action = cmd["action"]

    if action == "refresh":
        if not _report_lock.acquire(blocking=False):
            return "⏳ 現在更新中です。完了後にレポートをお送りします。"

        def _run():
            try:
                report_module.run_report()
            except Exception as e:
                print(f"[webhook] オンデマンドレポート失敗: {e}")
                try:
                    line_notifier.push_message([{
                        "type": "text",
                        "text": f"⚠️ レポート更新に失敗しました\n{e}",
                    }])
                except Exception:
                    pass
            finally:
                _report_lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return "🔄 AI株式リサーチを更新しています...\n完了後にレポートをお送りします（3〜5分程度）。"

    if action == "help":
        return (
            "📋 コマンド一覧\n\n"
            "追加 [コード] [株数] [単価]\n"
            "  例: 追加 7203 100 2650\n\n"
            "削除 [コード]\n"
            "  例: 削除 7203\n\n"
            "一覧  → 保有株一覧を表示\n"
            "更新  → AI株式リサーチを再実行\n"
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

        holdings.append({
            "id": uuid.uuid4().hex[:8],
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
        holding_id = cmd.get("holding_id")
        code = cmd.get("code", "")

        # holding_id 優先、なければコード（後方互換）
        if holding_id:
            target = next((h for h in holdings if h.get("id") == holding_id), None)
            # IDなし旧データ向けフォールバック: "code:price" 形式
            if not target:
                for h in holdings:
                    if f"{h['code']}:{h['buy_price']}" == holding_id:
                        target = h
                        break
        elif code:
            target = next((h for h in holdings if h["code"] == code), None)
        else:
            return "⚠️ 削除対象が指定されていません。"

        if not target:
            return "⚠️ 指定された銘柄が見つかりません。"

        try:
            stock_data = data_fetcher.fetch_stock_data(target["code"])
            current = stock_data.get("price", target["buy_price"])
        except Exception:
            current = target["buy_price"]
        pnl = (current - target["buy_price"]) * target["shares"]
        pnl_pct = (current - target["buy_price"]) / target["buy_price"] * 100

        try:
            from datetime import date
            buy_date = date.fromisoformat(target.get("buy_date", date.today().isoformat()))
            days = (date.today() - buy_date).days
        except Exception:
            days = 0

        # target と同一オブジェクトのみ除外（同銘柄複数対応）
        new_holdings = []
        removed = False
        for h in holdings:
            if not removed and h is target:
                removed = True
            else:
                new_holdings.append(h)
        portfolio["holdings"] = new_holdings
        portfolio_store.save_portfolio(portfolio)
        return (
            f"🗑️ {target['name']}（¥{target['buy_price']:,}）を削除しました\n"
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
    action: Literal["add", "remove", "list", "holdings"]  # 列挙型で不正アクションを拒否
    code: str | None = Field(default=None, pattern=r"^\d{4}$")  # 4桁数字のみ
    shares: int | None = Field(default=None, ge=1, le=100_000)  # 上限10万株
    price: int | None = Field(default=None, ge=1, le=100_000_000)  # 上限1億円
    holding_id: str | None = Field(default=None, max_length=64)  # 個別削除用ID


def _verify_liff_token(access_token: str) -> str:
    """
    LIFF アクセストークンを LINE Profile API で検証し、ユーザー ID を返す。
    検証失敗時は HTTPException(401) を送出する。
    """
    resp = _requests.get(
        "https://api.line.me/v2/profile",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=5,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid LIFF access token")
    user_id = resp.json().get("userId")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    return user_id


def _generate_insight(pnl_pct: float, target_price: float | None,
                      current_price: float, stop_loss_pct: float,
                      rsi: float = 50.0, ma25_diff_pct: float = 0.0,
                      macd: float = 0.0, macd_signal: float = 0.0) -> str:
    """
    RSI・MA乖離・MACDを組み合わせた考察を生成する。
    screener.py と同じテクニカル指標を保有株の出口判断に応用する。
    """
    # ── 最優先: 損切りライン到達 ──
    if pnl_pct <= stop_loss_pct:
        return f"⚠️ 損切りライン（{stop_loss_pct:+.0f}%）到達。早急な売り注文を。"

    # ── 目標価格チェック ──
    if target_price and current_price >= target_price:
        extra = f" RSI{rsi:.0f}（過熱）も重なる。" if rsi >= 65 else ""
        return f"🎯 目標価格到達！{extra}利確を強く検討。"
    if target_price:
        remaining = (target_price - current_price) / current_price * 100
        if remaining <= 3:
            rsi_note = f" RSI{rsi:.0f}（過熱圏）。" if rsi >= 65 else ""
            return f"🎯 目標まであと{remaining:.1f}%。{rsi_note}利確タイミングに注意。"

    # ── テクニカル複合シグナル（screenerロジック応用）──
    # RSI 過熱 × 含み益: 利確検討
    if rsi >= 70 and pnl_pct > 0:
        return f"📈 RSI{rsi:.0f}（過熱圏）& 含み益{pnl_pct:+.1f}%。指値売り注文を検討。"
    # RSI 過熱 × MA乖離大: 反落リスク高
    if rsi >= 65 and ma25_diff_pct >= 8:
        return f"⚡ RSI{rsi:.0f} & 25日線乖離+{ma25_diff_pct:.1f}%（過熱）。利確タイミング。"
    # MACDデッドクロス × 含み益あり: 売り転換シグナル
    if macd < macd_signal and macd_signal > 0 and pnl_pct > 0:
        return f"🔻 MACDデッドクロス（売り転換）。含み益{pnl_pct:+.1f}%のうちに利確検討。"
    # 25日線割れ × 含み損: 損切り接近
    if ma25_diff_pct < -5 and pnl_pct < 0:
        return f"🔴 25日線割れ（{ma25_diff_pct:.1f}%）& 含み損。損切りライン（{stop_loss_pct:+.0f}%）に注意。"
    # RSI 売られすぎ: 底値圏、損切り判断を慎重に
    if rsi <= 30 and pnl_pct < 0:
        return f"🔵 RSI{rsi:.0f}（売られすぎ圏）。反発待ちか損切りか要判断。"

    # ── 基本判断 ──
    if pnl_pct >= 15:
        return "📈 好調な含み益。目標価格まで継続保有を検討。"
    if pnl_pct >= 5:
        return "✅ 含み益あり。引き続き保有を継続。"
    if pnl_pct >= 0:
        return "⚪ 小幅な含み益。様子見で問題なし。"
    if pnl_pct >= -5:
        return "🔶 小幅な含み損。損切りラインまで余裕あり。"
    return f"🔴 含み損が拡大中。損切りライン（{stop_loss_pct:+.0f}%）に注意。"


async def _build_list_data(portfolio: dict) -> dict:
    """一覧用の構造化データを生成する（LIFF カード表示用）。全銘柄を並列取得する。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    holdings = portfolio.get("holdings", [])
    default_alerts = portfolio.get("default_alerts", {})
    fetched_at = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d %H:%M JST")

    loop = asyncio.get_event_loop()

    async def _process_one(h: dict) -> tuple[dict, float]:
        """1銘柄分のデータ取得・計算をスレッドプールで実行する。"""
        try:
            # fetch_stock_data_with_df で OHLCV を1回だけ取得し、指標と df を受け取る
            stock_data, df = await loop.run_in_executor(
                None, data_fetcher.fetch_stock_data_with_df, h["code"]
            )
            current = float(stock_data.get("price", h["buy_price"]))
            rsi = float(stock_data.get("rsi_14", 50.0))
            ma25_diff_pct = float(stock_data.get("ma25_diff_pct", 0.0))
        except Exception:
            df = None
            current = float(h["buy_price"])
            rsi = 50.0
            ma25_diff_pct = 0.0

        # 同じ df から MACD を計算（追加の yfinance 呼び出しなし）
        macd, macd_signal = 0.0, 0.0
        try:
            import pandas_ta as ta
            if df is not None and not df.empty:
                df = df.copy()  # in-place 変更を避けるためコピー
                df.ta.macd(append=True)
                latest = df.iloc[-1]
                macd = float(latest.get("MACD_12_26_9", 0) or 0)
                macd_signal = float(latest.get("MACDs_12_26_9", 0) or 0)
        except Exception:
            pass

        pnl = (current - h["buy_price"]) * h["shares"]
        pnl_pct = (current - h["buy_price"]) / h["buy_price"] * 100
        stop_loss_pct = h.get("stop_loss_pct", default_alerts.get("loss_pct", -8))
        target_price = h.get("target_price")

        target_remaining_pct = None
        if target_price:
            target_remaining_pct = round((target_price - current) / current * 100, 1)

        stop_loss_price = round(h["buy_price"] * (1 + stop_loss_pct / 100))
        take_profit_price = target_price if target_price else round(
            h["buy_price"] * (1 + default_alerts.get("profit_pct", 15) / 100)
        )

        item = {
            "code": h["code"].replace(".T", ""),
            "name": h["name"],
            "shares": h["shares"],
            "buy_price": h["buy_price"],
            "current_price": round(current),
            "pnl": round(pnl),
            "pnl_pct": round(pnl_pct, 1),
            "target_price": take_profit_price,
            "target_remaining_pct": target_remaining_pct,
            "stop_loss_price": stop_loss_price,
            "stop_loss_pct": stop_loss_pct,
            "rsi": round(rsi, 1),
            "ma25_diff_pct": round(ma25_diff_pct, 1),
            "insight": _generate_insight(
                pnl_pct, target_price, current, stop_loss_pct,
                rsi, ma25_diff_pct, macd, macd_signal
            ),
        }
        return item, pnl

    # 全銘柄を並列取得（return_exceptions=True で個別失敗が全体に波及しない）
    results = await asyncio.gather(
        *[_process_one(h) for h in holdings], return_exceptions=True
    )

    items = []
    total_pnl = 0.0
    for r in results:
        if isinstance(r, Exception):
            continue  # 個別銘柄の取得失敗はスキップ（全体は返す）
        item, pnl = r
        items.append(item)
        total_pnl += pnl

    total_buy = sum(h["buy_price"] * h["shares"] for h in holdings) or 1
    return {
        "fetched_at": fetched_at,
        "count": len(items),
        "total_pnl": round(total_pnl),
        "total_pnl_pct": round(total_pnl / total_buy * 100, 1),
        "holdings": items,
    }


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

    response: dict = {"status": "ok"}

    if payload.action == "list":
        # 一覧は _build_list_data で並列取得し、その結果から LINE テキストも生成
        # （execute_command("list") と _build_list_data の二重取得を排除）
        try:
            portfolio = portfolio_store.load_portfolio()
            holdings_data = await _build_list_data(portfolio)
            response["holdings_data"] = holdings_data

            if holdings_data["count"] == 0:
                reply_text = "📦 保有株はありません。\n「追加 7203 100 2650」の形式で追加できます。"
            else:
                lines = [f"📦 保有株一覧（{holdings_data['count']}銘柄）\n"]
                for item in holdings_data["holdings"]:
                    emoji = "🟢" if item["pnl"] > 0 else ("🔴" if item["pnl"] < 0 else "⚪")
                    lines.append(
                        f"{item['code']} {item['name']}  "
                        f"{item['shares']}株 ¥{item['current_price']:,} {item['pnl_pct']:+.1f}% {emoji}"
                    )
                lines.append(f"\n━━━━━━━━━━\n合計評価損益: {holdings_data['total_pnl']:+,.0f}円")
                reply_text = "\n".join(lines)
        except Exception as e:
            import traceback
            print(f"[ERROR] _build_list_data failed: {e}\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

    elif payload.action == "holdings":
        # 削除選択UI用: ストレージから保有株一覧を軽量取得（リアルタイム価格なし）
        portfolio = portfolio_store.load_portfolio()
        holdings = portfolio.get("holdings", [])
        # IDなし旧データには "code:price" 形式の合成IDを付与
        result = []
        for h in holdings:
            entry = dict(h)
            if "id" not in entry:
                entry["id"] = f"{h['code']}:{h['buy_price']}"
            result.append(entry)
        return {"status": "ok", "holdings": result}

    else:
        cmd: dict = {"action": payload.action}
        if payload.code:
            code = payload.code.strip()
            cmd["code"] = code + ".T" if not code.endswith(".T") else code
        if payload.shares:
            cmd["shares"] = payload.shares
        if payload.price:
            cmd["price"] = payload.price
        if payload.holding_id:
            cmd["holding_id"] = payload.holding_id

        try:
            reply_text = execute_command(cmd)
        except Exception as e:
            import traceback
            print(f"[ERROR] execute_command failed: {e}\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"Internal server error: {e}")

    # LINE Push で結果を通知
    try:
        line_notifier.push_message([{"type": "text", "text": reply_text}])
    except Exception:
        pass

    response["message"] = reply_text
    return response


# ─────────────────────────────────────────
# ヘルスチェック
# ─────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8000, reload=False)

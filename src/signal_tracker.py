"""
signal_tracker.py
AIシグナルを Azure Blob Storage に記録し、実際の価格推移で勝敗を判定する。
バックテスト結果（勝率）を算出して投資ロジックの改善に役立てる。

シグナルのライフサイクル:
  open → win  : 現在値 >= target_price
  open → loss : 現在値 <= stop_loss_price
  open → expired : 記録から30日経過
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BLOB_CONTAINER = "stock-bot"
BLOB_NAME = "signals.json"
_LOCAL_FALLBACK = Path(__file__).parent.parent / "config" / "signals_local.json"
JST = ZoneInfo("Asia/Tokyo")
EXPIRY_DAYS = 30
SHORT_TERM_EXPIRY_DAYS = 5   # 短期シグナル（holding_days≤3）は5営業日で期限切れ


def _use_blob() -> bool:
    return bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))


def _load_signals() -> list:
    if _use_blob():
        from azure.storage.blob import BlobServiceClient
        conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        client = BlobServiceClient.from_connection_string(conn_str)
        container = client.get_container_client(BLOB_CONTAINER)
        try:
            blob = container.get_blob_client(BLOB_NAME)
            data = blob.download_blob().readall()
            return json.loads(data)
        except Exception:
            return []
    else:
        if _LOCAL_FALLBACK.exists():
            return json.loads(_LOCAL_FALLBACK.read_text(encoding="utf-8"))
        return []


def _save_signals(signals: list) -> None:
    payload = json.dumps(signals, ensure_ascii=False, indent=2)
    if _use_blob():
        from azure.storage.blob import BlobServiceClient
        conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        client = BlobServiceClient.from_connection_string(conn_str)
        container = client.get_container_client(BLOB_CONTAINER)
        try:
            container.create_container()
        except Exception:
            pass
        blob = container.get_blob_client(BLOB_NAME)
        blob.upload_blob(payload.encode("utf-8"), overwrite=True)
    else:
        _LOCAL_FALLBACK.write_text(payload, encoding="utf-8")


def record_signals(analysis: dict) -> int:
    """
    Claude分析の推奨銘柄をシグナルとして記録する。
    「今すぐ買う」「押し目待ち」のみ対象（「見送り」は除外）。
    返り値: 新規記録件数
    """
    today = datetime.now(JST).strftime("%Y-%m-%d")
    signals = _load_signals()

    # 本日すでに記録済みのコードは追加しない（同日の重複防止）
    existing_today = {s["code"] for s in signals if s["signal_date"] == today}

    new_signals = []
    for r in analysis.get("recommendations", []):
        if r.get("action") == "見送り":
            continue
        if r.get("code") in existing_today:
            continue
        holding_days = r.get("holding_days") or 10  # デフォルトは中期扱い
        new_signals.append({
            "signal_date": today,
            "code": r["code"],
            "name": r.get("name", ""),
            "action": r.get("action", ""),
            "entry_price": r.get("buy_price") or r.get("current_price") or 0,
            "target_price": r.get("take_profit_price") or r.get("target_price") or 0,
            "stop_loss_price": r.get("stop_loss_price") or 0,
            "market_condition": analysis.get("market_condition", ""),
            "nikkei_trend": analysis.get("nikkei_trend", ""),
            "holding_days": holding_days,
            "status": "open",
            "outcome_price": None,
            "outcome_date": None,
        })

    if new_signals:
        signals.extend(new_signals)
        _save_signals(signals)
        print(f"[signal_tracker] {len(new_signals)}件のシグナルを記録")

    return len(new_signals)


def update_signal_outcomes() -> list:
    """
    open状態のシグナルを現在の株価で評価し勝敗を確定する。
    返り値: 新たにクローズされたシグナルのリスト
    """
    from data_fetcher import fetch_current_price

    signals = _load_signals()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    expiry_cutoff = (datetime.now(JST) - timedelta(days=EXPIRY_DAYS)).strftime("%Y-%m-%d")

    closed = []
    changed = False

    for s in signals:
        if s["status"] != "open":
            continue

        # 期限切れ判定: 短期シグナル（holding_days≤3）は5日、それ以外は30日
        is_short_term = s.get("holding_days", 10) <= 3
        if is_short_term:
            cutoff = (datetime.now(JST) - timedelta(days=SHORT_TERM_EXPIRY_DAYS)).strftime("%Y-%m-%d")
        else:
            cutoff = expiry_cutoff
        if s["signal_date"] < cutoff:
            s["status"] = "expired"
            s["outcome_date"] = today
            closed.append(s)
            changed = True
            continue

        # 当日シグナルは翌日以降に評価（発注日当日は価格が動いていない）
        if s["signal_date"] == today:
            continue

        try:
            current_price = fetch_current_price(s["code"])
        except Exception as e:
            print(f"[signal_tracker] {s['code']} 価格取得失敗: {e}")
            continue

        if s["target_price"] and current_price >= s["target_price"]:
            s["status"] = "win"
            s["outcome_price"] = round(current_price, 2)
            s["outcome_date"] = today
            closed.append(s)
            changed = True
        elif s["stop_loss_price"] and current_price <= s["stop_loss_price"]:
            s["status"] = "loss"
            s["outcome_price"] = round(current_price, 2)
            s["outcome_date"] = today
            closed.append(s)
            changed = True

    if changed:
        _save_signals(signals)
        print(f"[signal_tracker] {len(closed)}件のシグナルをクローズ")

    return closed


def get_win_rate_summary() -> dict:
    """記録済みシグナル全体のバックテスト集計を返す。短期/中期別の勝率も算出する。"""
    signals = _load_signals()

    wins = [s for s in signals if s["status"] == "win"]
    losses = [s for s in signals if s["status"] == "loss"]
    expired = [s for s in signals if s["status"] == "expired"]
    open_sigs = [s for s in signals if s["status"] == "open"]

    decided = len(wins) + len(losses)
    win_rate = round(len(wins) / decided * 100, 1) if decided > 0 else None

    # 短期（holding_days≤3）と中期（holding_days>3）に分けて勝率を算出
    def _category_stats(category_signals: list) -> dict:
        w = [s for s in category_signals if s["status"] == "win"]
        l = [s for s in category_signals if s["status"] == "loss"]
        d = len(w) + len(l)
        return {
            "wins": len(w),
            "losses": len(l),
            "win_rate": round(len(w) / d * 100, 1) if d > 0 else None,
        }

    short_term = [s for s in signals if s.get("holding_days", 10) <= 3]
    medium_term = [s for s in signals if s.get("holding_days", 1) > 3]

    return {
        "total": len(signals),
        "wins": len(wins),
        "losses": len(losses),
        "expired": len(expired),
        "open": len(open_sigs),
        "win_rate": win_rate,
        "short_term": _category_stats(short_term),   # holding_days≤3
        "medium_term": _category_stats(medium_term), # holding_days>3
    }

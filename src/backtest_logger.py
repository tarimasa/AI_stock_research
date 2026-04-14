"""
backtest_logger.py
推奨シグナルの詳細データ（どのシグナルが発火したか）を記録し、
後日 backtest_evaluator.py でシグナル別勝率を分析できるようにする。

signal_tracker.py との違い:
  - signal_tracker.py: 推奨結果（win/loss/expired）を追跡
  - backtest_logger.py: どのシグナルが発火したかを詳細記録（シグナル品質の分析用）
"""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BLOB_CONTAINER = "stock-bot"
BLOB_NAME = "backtest_log.json"
_LOCAL_FALLBACK = Path(__file__).parent.parent / "config" / "backtest_log_local.json"
JST = ZoneInfo("Asia/Tokyo")


def _use_blob() -> bool:
    return bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))


def _load_log() -> list:
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


def _save_log(log: list) -> None:
    payload = json.dumps(log, ensure_ascii=False, indent=2)
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
        _LOCAL_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_FALLBACK.write_text(payload, encoding="utf-8")


def log_recommendations(
    analysis: dict,
    screened_stocks: list,
    macro_result: dict | None = None,
) -> int:
    """
    Claude の推奨とその時点のシグナル詳細を記録する。
    「見送り」は記録しない。返り値: 新規記録件数。

    記録内容:
      - 日時・銘柄コード・推奨アクション・保有期間
      - 発火シグナル: breakout_5d, dvs, rsi5, vol_ratio, candle_pattern 等
      - マクロ状況: market_condition, vix
      - 価格: entry_price, take_profit_price, stop_loss_price, rr_ratio
      - 結果フィールド（後で backtest_evaluator が書き込む）: outcome, outcome_date, outcome_price
    """
    today = datetime.now(JST).strftime("%Y-%m-%d")
    log = _load_log()

    # 本日すでに記録済みのコードはスキップ
    existing_today = {e["code"] for e in log if e["signal_date"] == today}

    # スクリーナー結果をコード→データのマップに変換
    stock_map = {s["code"]: s for s in screened_stocks}

    new_entries = []
    for r in analysis.get("recommendations", []):
        if r.get("action") == "見送り":
            continue
        code = r.get("code", "")
        if code in existing_today:
            continue

        # シグナル詳細をスクリーナーデータから取得
        s = stock_map.get(code, {})
        signals = {
            "breakout_5d": s.get("breakout_5d", False),
            "directional_vol_score": s.get("directional_vol_score", 0),
            "rsi5": s.get("rsi5"),
            "rsi14": s.get("rsi_14") or s.get("rsi14"),
            "vol_ratio": s.get("vol_ratio"),
            "week52_pos_pct": s.get("week52_pos_pct"),
            "candle_pattern": s.get("candle_pattern", "none"),
            "ma25_diff_pct": s.get("ma25_diff_pct"),
            "days_to_ex_dividend": s.get("days_to_ex_dividend"),
            "days_to_earnings": s.get("days_to_earnings"),
            "screener_score": s.get("score"),
        }

        new_entries.append({
            "signal_date": today,
            "code": code,
            "name": r.get("name", ""),
            "sector": r.get("sector", ""),
            "action": r.get("action", ""),
            "holding_days": r.get("holding_days", 10),
            "entry_price": r.get("buy_price") or r.get("current_price") or 0,
            "take_profit_price": r.get("take_profit_price") or 0,
            "stop_loss_price": r.get("stop_loss_price") or 0,
            "rr_ratio": r.get("rr_ratio"),
            "risk_level": r.get("risk_level"),
            "market_condition": analysis.get("market_condition", ""),
            "vix": (macro_result or {}).get("vix"),
            "macro_condition": (macro_result or {}).get("condition", ""),
            "signals": signals,
            # 後で evaluator が埋める結果フィールド
            "outcome": None,        # "win" | "loss" | "expired"
            "outcome_date": None,
            "outcome_price": None,
            "return_pct": None,
        })

    if new_entries:
        log.extend(new_entries)
        _save_log(log)
        print(f"[backtest_logger] {len(new_entries)}件のシグナル詳細を記録")

    return len(new_entries)


def update_outcomes(closed_signals: list) -> None:
    """
    signal_tracker.update_signal_outcomes() の結果をバックテストログに反映する。
    closed_signals: signal_tracker が返すクローズ済みシグナルのリスト。
    """
    if not closed_signals:
        return

    log = _load_log()
    closed_map = {s["code"]: s for s in closed_signals}

    changed = False
    for entry in log:
        if entry.get("outcome") is not None:
            continue  # すでに確定済み
        code = entry.get("code")
        signal_date = entry.get("signal_date")
        closed = closed_map.get(code)
        if not closed:
            continue
        if closed.get("signal_date") != signal_date:
            continue  # 同コードでも別日のシグナル

        entry["outcome"] = closed.get("status")  # "win" / "loss" / "expired"
        entry["outcome_date"] = closed.get("outcome_date")
        entry["outcome_price"] = closed.get("outcome_price")

        # リターン率を計算
        entry_price = entry.get("entry_price", 0)
        outcome_price = entry.get("outcome_price")
        if entry_price and outcome_price:
            entry["return_pct"] = round((outcome_price - entry_price) / entry_price * 100, 2)

        changed = True

    if changed:
        _save_log(log)
        print(f"[backtest_logger] アウトカム更新: {len(closed_signals)}件")


def get_signal_stats() -> dict:
    """
    シグナル別の勝率統計を返す。
    バックテスト評価レポート生成に使用する。
    """
    log = _load_log()
    decided = [e for e in log if e.get("outcome") in ("win", "loss")]

    if not decided:
        return {"total": 0, "by_signal": {}}

    total = len(decided)
    wins = sum(1 for e in decided if e["outcome"] == "win")

    def _signal_win_rate(key: str, check_fn) -> dict:
        matched = [e for e in decided if check_fn(e)]
        if not matched:
            return None
        w = sum(1 for e in matched if e["outcome"] == "win")
        return {
            "count": len(matched),
            "wins": w,
            "win_rate": round(w / len(matched) * 100, 1),
        }

    by_signal = {}

    # ブレイクアウト有無
    stat = _signal_win_rate("breakout_5d", lambda e: e.get("signals", {}).get("breakout_5d") is True)
    if stat:
        by_signal["breakout_5d=true"] = stat

    # 方向性出来高スコア正
    stat = _signal_win_rate("dvs_positive", lambda e: (e.get("signals", {}).get("directional_vol_score") or 0) > 0)
    if stat:
        by_signal["dvs_positive"] = stat

    # RSI5 ≤ 30
    stat = _signal_win_rate("rsi5_oversold", lambda e: (e.get("signals", {}).get("rsi5") or 999) <= 30)
    if stat:
        by_signal["rsi5_le_30"] = stat

    # 出来高急増（1.5倍以上）
    stat = _signal_win_rate("vol_surge", lambda e: (e.get("signals", {}).get("vol_ratio") or 0) >= 1.5)
    if stat:
        by_signal["vol_ratio_ge_1.5"] = stat

    # 足型シグナル
    stat = _signal_win_rate("bullish_engulfing", lambda e: e.get("signals", {}).get("candle_pattern") == "bullish_engulfing")
    if stat:
        by_signal["bullish_engulfing"] = stat

    stat = _signal_win_rate("hammer", lambda e: e.get("signals", {}).get("candle_pattern") == "hammer")
    if stat:
        by_signal["hammer"] = stat

    # 短期（holding_days≤3）vs 中期
    stat = _signal_win_rate("short_term", lambda e: e.get("holding_days", 10) <= 3)
    if stat:
        by_signal["short_term_holding"] = stat

    stat = _signal_win_rate("medium_term", lambda e: e.get("holding_days", 1) > 3)
    if stat:
        by_signal["medium_term_holding"] = stat

    return {
        "total": total,
        "overall_win_rate": round(wins / total * 100, 1) if total > 0 else None,
        "by_signal": by_signal,
    }

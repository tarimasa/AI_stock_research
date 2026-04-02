"""
portfolio_tracker.py
portfolio_store から保有株を読み込み、現在損益とアラートを計算する。
"""

import os

from dotenv import load_dotenv

import data_fetcher
import portfolio_store

load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


def judge_alert(holding: dict, current_price: float, rsi: float, default_alerts: dict) -> tuple[str | None, str]:
    buy_price = holding["buy_price"]
    pnl_pct = (current_price - buy_price) / buy_price * 100

    # 損切りチェック（最優先）
    stop_loss = holding.get("stop_loss_pct", default_alerts["loss_pct"])
    if pnl_pct <= stop_loss:
        return "損切り推奨", f"含み損{pnl_pct:.1f}%が損切りライン{stop_loss}%を超過"

    # 目標株価チェック
    target = holding.get("target_price")
    if target and current_price >= target:
        return "利確推奨", f"目標株価¥{target}に到達（含み益+{pnl_pct:.1f}%）"

    # 含み益 % チェック（target_price 未設定時のフォールバック）
    if not target and pnl_pct >= default_alerts["profit_pct"]:
        return "利確推奨", f"含み益+{pnl_pct:.1f}%がデフォルト利確ライン到達"

    # RSI 過熱チェック
    if rsi >= default_alerts["rsi_overbought"]:
        return "RSI過熱", f"RSI{rsi:.0f}（買われすぎ圏、利確タイミング候補）"
    if rsi <= default_alerts["rsi_oversold"]:
        return "RSI底値", f"RSI{rsi:.0f}（売られすぎ圏、追加購入候補）"

    return None, ""


def check_portfolio() -> dict:
    """
    portfolio.json を読み込み、全保有株の損益とアラートを返す。
    holdings が空の場合は空の dict を返す。
    """
    portfolio = portfolio_store.load_portfolio()
    holdings = portfolio.get("holdings", [])

    if not holdings:
        return {}

    default_alerts = portfolio.get("default_alerts", {
        "profit_pct": 15,
        "loss_pct": -8,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
    })

    result_holdings = []
    total_cost = 0.0
    total_value = 0.0

    for h in holdings:
        try:
            if DRY_RUN:
                stock_data = data_fetcher._dummy_stock_data(h["code"])
            else:
                stock_data = data_fetcher.fetch_stock_data(h["code"])

            current_price = stock_data.get("price", h["buy_price"])
            rsi = stock_data.get("rsi_14", 50.0)
            shares = h["shares"]
            buy_price = h["buy_price"]

            unrealized_pnl = (current_price - buy_price) * shares
            unrealized_pnl_pct = (current_price - buy_price) / buy_price * 100

            alert, alert_reason = judge_alert(h, current_price, rsi, default_alerts)

            result_holdings.append({
                "code": h["code"],
                "name": h["name"],
                "shares": shares,
                "buy_price": buy_price,
                "current_price": current_price,
                "unrealized_pnl": round(unrealized_pnl),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 1),
                "rsi_14": rsi,
                "alert": alert,
                "alert_reason": alert_reason,
            })

            total_cost += buy_price * shares
            total_value += current_price * shares

        except Exception as e:
            print(f"[portfolio_tracker] {h['code']} スキップ: {e}")

    total_unrealized_pnl = round(total_value - total_cost)
    total_unrealized_pnl_pct = (
        round((total_value - total_cost) / total_cost * 100, 1)
        if total_cost > 0
        else 0.0
    )

    return {
        "total_unrealized_pnl": total_unrealized_pnl,
        "total_unrealized_pnl_pct": total_unrealized_pnl_pct,
        "holdings": result_holdings,
    }

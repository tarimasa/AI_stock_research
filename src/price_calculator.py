"""
price_calculator.py
LLMに渡す前に、各銘柄の候補価格（買い指値・利確・損切り）を事前計算する。
LLMは action（今すぐ買う/押し目待ち/見送り）と holding_days の判断のみ行い、
価格計算そのものはこのモジュールが担う。これによりLLMの計算ミス・幻覚を排除する。
"""


def calc_price_candidates(
    current_price: float,
    sma25: float | None,
    holding_days: int = 3,
    vix: float = 20.0,
) -> dict:
    """
    銘柄の現在価格・SMA25・保有期間・VIXから候補価格セットを返す。

    Returns:
        {
            "current_price": float,
            "holding_days": int,
            "buy_now": {
                "buy_price": float,
                "take_profit_low": float,   # 利確下限
                "take_profit_high": float,  # 利確上限
                "stop_loss": float,
                "stop_loss_pct": float,
                "tp_pct_low": float,
                "tp_pct_high": float,
            },
            "buy_dip": { ... },  # 押し目待ち用（同じ構造）
        }
    """
    # ── 損切り幅・利益目標（バックテスト最適値: SL-5%/TP+7.5〜10%/5日保有 EV+0.708%）
    # RR比 ≥ 1.5 を確保するため TP下限 = SL × 1.5 = 7.5%
    if holding_days <= 3:
        sl_pct = 6.0 if vix >= 30 else 5.0
        tp_pct_low, tp_pct_high = 7.5, 10.0
    else:
        sl_pct = 7.0 if vix >= 30 else 5.0
        tp_pct_low, tp_pct_high = 7.5, 10.0

    # ── 買い価格候補 ──────────────────────────────────────────────────────
    buy_now = _tick_round(current_price * 0.993)   # 今すぐ買う: 現在値の-0.7%

    # 押し目待ち: SMA25が現在値より2%以上低い場合はSMA25を使用、それ以外は-3%
    if sma25 and sma25 < current_price * 0.98:
        buy_dip = _tick_round(sma25)
    else:
        buy_dip = _tick_round(current_price * 0.97)

    def _calc_targets(buy_p: float) -> dict:
        return {
            "buy_price": buy_p,
            "take_profit_low": _tick_round(buy_p * (1 + tp_pct_low / 100)),
            "take_profit_high": _tick_round(buy_p * (1 + tp_pct_high / 100)),
            "stop_loss": _tick_round(buy_p * (1 - sl_pct / 100)),
            "stop_loss_pct": sl_pct,
            "tp_pct_low": tp_pct_low,
            "tp_pct_high": tp_pct_high,
        }

    return {
        "current_price": current_price,
        "holding_days": holding_days,
        "buy_now": _calc_targets(buy_now),
        "buy_dip": _calc_targets(buy_dip),
    }


def calc_all_candidates(
    current_price: float,
    sma25: float | None,
    vix: float = 20.0,
) -> dict:
    """
    短期（1〜3日）と中期（5〜10日）の両方の価格セットを返す。
    プロンプトに両方を埋め込み、Claudeが holding_days を決定した後に
    適切なセットを選択できるようにする。
    """
    return {
        "short_term": calc_price_candidates(
            current_price, sma25, holding_days=3, vix=vix
        ),
        "medium_term": calc_price_candidates(
            current_price, sma25, holding_days=7, vix=vix
        ),
    }


def format_candidates_for_prompt(code: str, candidates: dict) -> str:
    """
    価格候補セットをLLMプロンプト用のテキストに整形する。
    """
    cp = candidates["short_term"]["current_price"]
    st = candidates["short_term"]
    mt = candidates["medium_term"]

    def _fmt(c: dict, label: str) -> str:
        bn = c["buy_now"]
        bd = c["buy_dip"]
        return (
            f"  【{label}】"
            f"今すぐ: 買={bn['buy_price']}, 利確={bn['take_profit_low']}〜{bn['take_profit_high']}, "
            f"損切={bn['stop_loss']}({bn['stop_loss_pct']}%) ／ "
            f"押し目: 買={bd['buy_price']}, 利確={bd['take_profit_low']}〜{bd['take_profit_high']}, "
            f"損切={bd['stop_loss']}({bd['stop_loss_pct']}%)"
        )

    return (
        f"{code} 現在値={cp}\n"
        + _fmt(st, "短期1〜3日") + "\n"
        + _fmt(mt, "中期5〜10日")
    )


def _tick_round(price: float) -> float:
    """
    東証の呼値単位に丸める（簡易版）。切り捨て-0.5は切り上げ（round-half-up）。
    ・〜3,000円  : 1円単位
    ・3,001〜5,000円: 5円単位
    ・5,001〜30,000円: 10円単位
    ・30,001円〜 : 50円単位
    """
    import math

    def _rhu(x: float, unit: float) -> float:
        """Round half up to the given unit."""
        return math.floor(x / unit + 0.5) * unit

    if price <= 3000:
        return _rhu(price, 1)
    elif price <= 5000:
        return _rhu(price, 5)
    elif price <= 30000:
        return _rhu(price, 10)
    else:
        return _rhu(price, 50)

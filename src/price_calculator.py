"""
price_calculator.py
LLMに渡す前に、各銘柄の候補価格（買い指値・利確・損切り）を事前計算する。
LLMは action（今すぐ買う/押し目待ち/見送り）と holding_days の判断のみ行い、
価格計算そのものはこのモジュールが担う。これによりLLMの計算ミス・幻覚を排除する。

「今すぐ買う」の指値:
  ATR14 が利用可能なら ATR 連動 (Close - ATR_MULT_BUY_NOW × ATR14)、
  使えない場合は固定オフセット FALLBACK_BUY_NOW_PCT(%) にフォールバック。
  バックテスト (TRAIN 2021-05〜2025-09 / VAL 2025-10〜2026-04, Stage1>=60) では
  ATR×1.5 連動が VAL 純EV +0.036%/シグナルで、固定 -0.7% (-0.136%) を大きく上回る。
"""

# ── 「今すぐ買う」指値の基本パラメータ ────────────────────────────────────────
# ATR14 × この倍率を Close から引いた値が指値の原資 (バックテストの最適 k=1.5)
ATR_MULT_BUY_NOW = 1.5
# ATR14 が取れない銘柄のフォールバック用オフセット (%、負値で「下に」の意)
FALLBACK_BUY_NOW_PCT = -3.5


def _calc_buy_now_price(current_price: float, atr14: float | None) -> float:
    """
    ATR が有効なら Close - ATR_MULT_BUY_NOW × ATR14、無理なら FALLBACK_BUY_NOW_PCT。
    呼値丸めまで実施した最終指値を返す。
    """
    if atr14 is not None and atr14 > 0:
        raw = current_price - ATR_MULT_BUY_NOW * atr14
    else:
        raw = current_price * (1.0 + FALLBACK_BUY_NOW_PCT / 100.0)
    return _tick_round(raw)


def calc_price_candidates(
    current_price: float,
    sma25: float | None,
    holding_days: int = 3,
    vix: float = 20.0,
    atr14: float | None = None,
) -> dict:
    """
    銘柄の現在価格・SMA25・ATR14・保有期間・VIXから候補価格セットを返す。

    Args:
        current_price: 当日終値 (= 翌日の指値判断の基準)
        sma25:         25日移動平均 (押し目買い用)
        holding_days:  想定保有日数 (TP/SL 幅の選択に使用)
        vix:           VIX 指数 (>=30 で SL をやや広げる)
        atr14:         14日ATR (円スケール)。未提供時は固定 -3.5% にフォールバック

    Returns:
        {
            "current_price": float,
            "holding_days": int,
            "atr14": float | None,
            "buy_now": {...},  # ATR連動 (or -3.5% fallback)
            "buy_dip": {...},  # 押し目待ち（同じ構造）
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
    # 今すぐ買う: ATR14 連動 (バックテスト採用) or 固定 -3.5% フォールバック
    buy_now = _calc_buy_now_price(current_price, atr14)

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
        "atr14": atr14,
        "buy_now": _calc_targets(buy_now),
        "buy_dip": _calc_targets(buy_dip),
    }


def calc_all_candidates(
    current_price: float,
    sma25: float | None,
    vix: float = 20.0,
    atr14: float | None = None,
) -> dict:
    """
    短期（1〜3日）と中期（5〜10日）の両方の価格セットを返す。
    プロンプトに両方を埋め込み、Claudeが holding_days を決定した後に
    適切なセットを選択できるようにする。

    atr14 を渡すと「今すぐ買う」指値が ATR×1.5 連動になる。
    未指定の場合は固定 -3.5% にフォールバック。
    """
    return {
        "short_term": calc_price_candidates(
            current_price, sma25, holding_days=3, vix=vix, atr14=atr14
        ),
        "medium_term": calc_price_candidates(
            current_price, sma25, holding_days=7, vix=vix, atr14=atr14
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

"""
macro_preprocessor.py
マクロ指標を取得し、LLMに渡すフラグ・コメントに変換する。
fetch_market_data() の出力フィールド名に合わせて実装。
"""


def preprocess_macro(macro_data: dict) -> dict:
    """
    fetch_market_data() の出力を受け取り、フラグとサマリーコメントを生成する。

    Returns:
        {
            "condition": "良好|注意|悪化",
            "flags": [str],
            "flags_text": str,
            "risk_adjustment": int,   # リスク段数の追加補正
            "vix": float,
        }
    """
    vix = float(macro_data.get("vix", 20.0) or 20.0)
    vix_trend = macro_data.get("vix_trend", "不明")
    sp500_change = float(macro_data.get("sp500_change", 0.0) or 0.0)
    gold_change = float(macro_data.get("gold_change", 0.0) or 0.0)
    oil_change = float(macro_data.get("oil_change", 0.0) or 0.0)
    us10y_trend = macro_data.get("us10y_trend", "横ばい")  # "上昇"|"低下"|"横ばい"|"不明"
    dow_change = float(macro_data.get("dow_change", 0.0) or 0.0)
    nikkei_trend = macro_data.get("nikkei_trend", "不明")

    flags = []
    risk_adjustment = 0
    negative_signals = 0

    # ── VIX ──────────────────────────────────────────────
    if vix >= 30:
        flags.append(f"VIX{vix:.0f}高恐怖→リスク+1段")
        risk_adjustment = 1
        negative_signals += 1
    elif vix >= 20:
        flags.append(f"VIX{vix:.0f}警戒水準")
        negative_signals += 0.5

    if vix_trend == "上昇":
        flags.append("VIX上昇中→利確は近めに")

    # ── 米株 ─────────────────────────────────────────────
    if sp500_change <= -1.0:
        flags.append(f"S&P500 {sp500_change:+.1f}%→波及リスク")
        negative_signals += 1
    if dow_change <= -1.0:
        flags.append(f"ダウ {dow_change:+.1f}%→波及リスク")
        negative_signals += 0.5

    # ── 金 ───────────────────────────────────────────────
    if gold_change >= 1.0:
        flags.append(f"金 {gold_change:+.1f}%→リスクオフ")
        negative_signals += 0.5
    elif gold_change <= -1.0:
        flags.append(f"金 {gold_change:+.1f}%→リスクオン追い風")

    # ── 原油 ─────────────────────────────────────────────
    if oil_change >= 3.0:
        flags.append(f"原油 {oil_change:+.1f}%急騰→エネルギー株追い風")
    elif oil_change <= -3.0:
        flags.append(f"原油 {oil_change:+.1f}%急落→航空・海運追い風")

    # ── 米金利 ────────────────────────────────────────────
    if us10y_trend == "上昇":
        flags.append("米金利上昇→円安バイアス/輸出有利")
    elif us10y_trend == "低下":
        flags.append("米金利低下→円高リスク/輸出注意")

    # ── 日経トレンド ──────────────────────────────────────
    if nikkei_trend == "下落":
        flags.append("日経25日線割れ→全体弱い")
        negative_signals += 1

    # ── 総合判定 ──────────────────────────────────────────
    if negative_signals >= 2:
        condition = "悪化"
    elif negative_signals >= 1 or vix >= 20:
        condition = "注意"
    else:
        condition = "良好"

    return {
        "condition": condition,
        "flags": flags,
        "flags_text": " / ".join(flags) if flags else "特になし",
        "risk_adjustment": risk_adjustment,
        "vix": vix,
    }

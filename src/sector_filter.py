"""
sector_filter.py
保有銘柄のセクター集中を加味して Claude の推奨銘柄を post-filter する。

ルール:
  - 各推奨のセクターを master(33業種) → watchlist 順にルックアップ
  - 既保有銘柄のセクターをカウント
  - 同一セクターの保有数が max_per_sector に達していれば、その推奨は除外

セクター情報の引き元優先順位:
  1. master_manager.get_master() の sector33（J-Quants 公式 33 業種）
  2. config/watchlist.json の sector フィールド（ユーザー定義）
  3. 不明 → セクター "" として扱う（フィルタ対象外）
"""


def get_sector_for_code(code: str, watchlist: dict, master: dict) -> str:
    """
    銘柄コードのセクター名（33 業種）を返す。不明時は空文字。
    """
    code4 = str(code).replace(".T", "")[:4]
    if not code4 or not code4.isdigit():
        return ""

    info = master.get(code4) if master else None
    if info and info.get("sector33"):
        return info["sector33"]

    for stock in (watchlist or {}).get("stocks", []):
        if stock.get("code", "").replace(".T", "")[:4] == code4:
            return stock.get("sector", "")

    return ""


def get_held_sector_counts(
    portfolio_result: dict,
    watchlist: dict,
    master: dict,
) -> dict:
    """
    保有銘柄のセクター別カウントを返す。
    portfolio_tracker.check_portfolio() の戻り値（holdings リスト）を入力する。

    Returns: {sector_name: count, ...}
    """
    counts: dict = {}
    holdings = (portfolio_result or {}).get("holdings", [])
    for h in holdings:
        sector = get_sector_for_code(h.get("code", ""), watchlist, master)
        if sector:
            counts[sector] = counts.get(sector, 0) + 1
    return counts


def filter_by_sector_concentration(
    recommendations: list,
    held_sector_counts: dict,
    max_per_sector: int = 1,
    watchlist: dict | None = None,
    master: dict | None = None,
) -> tuple[list, list]:
    """
    推奨銘柄リストをセクター集中ルールで選別する。

    Args:
        recommendations: Claude の推奨銘柄リスト（dict のリスト）
        held_sector_counts: get_held_sector_counts() の戻り値
        max_per_sector: 1 セクターあたりの保有上限（推奨後）
        watchlist, master: セクター引き元

    Returns:
        (kept, removed): 通過した推奨と除外された推奨
        除外された推奨には _excluded_reason / _excluded_sector が付く
    """
    kept: list = []
    removed: list = []
    sector_counter = dict(held_sector_counts)  # 保有数を初期値とした mutable counter

    for rec in recommendations or []:
        code = rec.get("code", "")
        sector = get_sector_for_code(code, watchlist or {}, master or {})

        if not sector:
            # セクター不明はフィルタ対象外（通過させる）
            kept.append(rec)
            continue

        current = sector_counter.get(sector, 0)
        if current >= max_per_sector:
            removed.append({
                **rec,
                "_excluded_reason": "sector_concentration",
                "_excluded_sector": sector,
                "_excluded_held_count": current,
            })
            continue

        kept.append(rec)
        sector_counter[sector] = current + 1

    return kept, removed

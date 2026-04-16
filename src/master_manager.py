"""
master_manager.py
J-Quants 上場銘柄マスタを日次キャッシュで管理する。
公式の 33 業種・17 業種コードをスクリーナーやプロンプト生成に提供する。
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_fetcher import fetch_master

JST = timezone(timedelta(hours=9))
CACHE_PATH = Path(__file__).parent.parent / "data" / "master_cache.json"


def get_master(force_refresh: bool = False) -> dict:
    """
    上場銘柄マスタを辞書形式で返す。
    キー: 4 桁コード（例: "7203"）
    値: {name, sector33, sector33_code, sector17, sector17_code, market}

    1 日 1 回だけ API 取得し、以降はキャッシュを使う。
    API 取得失敗時はキャッシュがあればそれを使う。

    Args:
        force_refresh: True の場合はキャッシュを無視して強制取得。
    """
    if not force_refresh and _is_cache_fresh():
        cached = _load_cache()
        if cached:
            return cached

    df = fetch_master()
    if df is None or df.empty:
        # API 取得失敗: キャッシュがあれば使う
        cached = _load_cache()
        return cached if cached else {}

    master = {}
    for _, row in df.iterrows():
        # Code 列: "7203" や "72030" 形式の場合に統一
        raw_code = str(row.get("Code", "")).strip()
        if len(raw_code) == 5 and raw_code.endswith("0") and raw_code.isdigit():
            code4 = raw_code[:4]
        else:
            code4 = raw_code[:4].zfill(4) if raw_code.isdigit() else raw_code[:4]

        if not code4 or not code4.isdigit():
            continue

        master[code4] = {
            "name": str(row.get("CompanyName", row.get("Name", ""))),
            "sector33": str(row.get("Sector33CodeName", "")),
            "sector33_code": str(row.get("Sector33Code", "")),
            "sector17": str(row.get("Sector17CodeName", "")),
            "sector17_code": str(row.get("Sector17Code", "")),
            "market": str(row.get("MarketCodeName", row.get("MarketCode", ""))),
        }

    if master:
        _save_cache(master)
    return master


def get_sector_info(code: str) -> dict:
    """
    銘柄コードのセクター情報を返す便利関数。
    get_master() のキャッシュ結果から引く。

    Returns:
        {"name": str, "sector33": str, "sector17": str, "market": str}
        不明時は全フィールドが空文字のdictを返す。
    """
    code4 = code.replace(".T", "")[:4]
    master = get_master()
    return master.get(code4, {
        "name": "",
        "sector33": "",
        "sector33_code": "",
        "sector17": "",
        "sector17_code": "",
        "market": "",
    })


def _is_cache_fresh() -> bool:
    """当日中にキャッシュが生成されていれば True。"""
    if not CACHE_PATH.exists():
        return False
    mtime = datetime.fromtimestamp(CACHE_PATH.stat().st_mtime, tz=JST)
    now = datetime.now(JST)
    return mtime.date() == now.date()


def _load_cache() -> dict:
    """キャッシュファイルを読み込む。存在しない場合は空辞書。"""
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    """キャッシュファイルに書き込む。"""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[master_manager] キャッシュ保存失敗: {e}")

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


def _detect_column(df, candidates: list[str], pattern_keywords: list[str] | None = None) -> str | None:
    """
    候補名から最初に一致した列名を返す。なければ pattern_keywords を含む列を探す。
    J-Quants の API バージョン・プランによる列名差異（CompanyName / Name / 短縮形）を吸収する。
    """
    for c in candidates:
        if c in df.columns:
            return c
    if pattern_keywords:
        for c in df.columns:
            cl = c.lower()
            if any(k in cl for k in pattern_keywords):
                return c
    return None


def get_master(force_refresh: bool = False) -> dict:
    """
    上場銘柄マスタを辞書形式で返す。
    キー: 4 桁コード（例: "7203"）
    値: {name, sector33, sector33_code, sector17, sector17_code, market}

    1 日 1 回だけ API 取得し、以降はキャッシュを使う。
    API 取得失敗時はキャッシュがあればそれを使う。

    取得後に name が空の銘柄が 50% 超ある場合は警告ログを出力する
    （J-Quants の列名変更や Light プラン特有のフィールド省略を検知するため）。

    Args:
        force_refresh: True の場合はキャッシュを無視して強制取得。
    """
    if not force_refresh and _is_cache_fresh():
        cached = _load_cache()
        if cached:
            return cached

    df = fetch_master()
    if df is None or df.empty:
        cached = _load_cache()
        return cached if cached else {}

    # 列名差異を吸収（CompanyName / Name / 略称、Sector33CodeName 系も同様）
    name_col = _detect_column(
        df,
        ["CompanyName", "Name", "CompanyNameJapanese", "CompanyJpName"],
        pattern_keywords=["companyname", "name"],
    )
    sector33_name_col = _detect_column(
        df, ["Sector33CodeName"], pattern_keywords=["sector33codename", "sector33name"]
    )
    sector33_code_col = _detect_column(df, ["Sector33Code"], pattern_keywords=["sector33code"])
    sector17_name_col = _detect_column(
        df, ["Sector17CodeName"], pattern_keywords=["sector17codename", "sector17name"]
    )
    sector17_code_col = _detect_column(df, ["Sector17Code"], pattern_keywords=["sector17code"])
    market_col = _detect_column(
        df, ["MarketCodeName", "MarketCode"], pattern_keywords=["marketname", "marketcode"]
    )

    if name_col is None:
        print(
            f"[master_manager] WARNING: 銘柄名の列が検出できませんでした。"
            f"利用可能列={list(df.columns)[:20]}"
        )

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
            "name": str(row.get(name_col, "")) if name_col else "",
            "sector33": str(row.get(sector33_name_col, "")) if sector33_name_col else "",
            "sector33_code": str(row.get(sector33_code_col, "")) if sector33_code_col else "",
            "sector17": str(row.get(sector17_name_col, "")) if sector17_name_col else "",
            "sector17_code": str(row.get(sector17_code_col, "")) if sector17_code_col else "",
            "market": str(row.get(market_col, "")) if market_col else "",
        }

    # 名前充填率の検証（>50% empty なら警告）
    if master:
        empty_count = sum(1 for v in master.values() if not v.get("name"))
        empty_ratio = empty_count / len(master)
        if empty_ratio > 0.5:
            print(
                f"[master_manager] WARNING: master {len(master)}件中 {empty_count}件で name が空 "
                f"({empty_ratio*100:.0f}%). name_col='{name_col}' の検出が失敗している可能性があります。"
                f"列名サンプル={list(df.columns)[:15]}"
            )
        else:
            print(f"[master_manager] master 取得完了: {len(master)}件 (name 充填率 {(1-empty_ratio)*100:.1f}%)")

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

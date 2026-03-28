"""
portfolio_store.py
Azure Blob Storage で portfolio.json を読み書きする。
AZURE_STORAGE_CONNECTION_STRING が未設定の場合は
config/portfolio_local.json にフォールバック（開発用）。
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BLOB_CONTAINER = "stock-bot"
BLOB_NAME = "portfolio.json"

_LOCAL_FALLBACK = Path(__file__).parent.parent / "config" / "portfolio_local.json"

_DEFAULT_PORTFOLIO: dict = {
    "default_alerts": {
        "profit_pct": 15,
        "loss_pct": -8,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
    },
    "holdings": [],
}


def _use_blob() -> bool:
    return bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))


def load_portfolio() -> dict:
    """Blob から JSON を読み込む。存在しない場合は空のデフォルトを返す。"""
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
            return dict(_DEFAULT_PORTFOLIO)
    else:
        if _LOCAL_FALLBACK.exists():
            return json.loads(_LOCAL_FALLBACK.read_text(encoding="utf-8"))
        return dict(_DEFAULT_PORTFOLIO)


def save_portfolio(portfolio: dict) -> None:
    """dict を JSON にシリアライズして Blob に保存。"""
    payload = json.dumps(portfolio, ensure_ascii=False, indent=2)
    if _use_blob():
        from azure.storage.blob import BlobServiceClient

        conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        client = BlobServiceClient.from_connection_string(conn_str)
        container = client.get_container_client(BLOB_CONTAINER)
        # コンテナがなければ作成
        try:
            container.create_container()
        except Exception:
            pass
        blob = container.get_blob_client(BLOB_NAME)
        blob.upload_blob(payload.encode("utf-8"), overwrite=True)
    else:
        _LOCAL_FALLBACK.write_text(payload, encoding="utf-8")

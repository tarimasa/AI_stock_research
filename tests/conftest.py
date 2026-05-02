"""
conftest.py
pytest の共通セットアップ。オプショナル依存（feedparser, pandas_ta など）が
欠けている環境でもテストが走るようダミーモジュールをスタブする。
本番環境には影響しない。
"""

import os
import sys
import types

# DRY_RUN を強制（外部 API 呼び出しを抑止）
os.environ.setdefault("DRY_RUN", "true")

# オプショナル依存のスタブ
_OPTIONAL_MODULES = {
    "feedparser": {"parse": lambda url: types.SimpleNamespace(entries=[])},
    "pandas_ta": {},
    "yfinance": {},
    "jquantsapi": {},
    "azure.storage.blob": {},
    "azure": {},
    "linebot": {},
    "linebot.v3": {},
    "linebot.v3.messaging": {},
}

for mod_name, attrs in _OPTIONAL_MODULES.items():
    if mod_name not in sys.modules:
        try:
            __import__(mod_name)
        except ImportError:
            stub = types.ModuleType(mod_name)
            for attr, value in attrs.items():
                setattr(stub, attr, value)
            sys.modules[mod_name] = stub

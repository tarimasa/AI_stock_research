"""J-Quants 前場四本値の取得・ポーリング・昼スクリーナー連携のテスト（API はモック）。"""

from datetime import timedelta

import pandas as pd
import pytest

import data_fetcher
import jquants_fetcher
import noon_screener


def _am_df(date):
    return pd.DataFrame([
        {"Date": pd.Timestamp(date), "Code": "72030", "MO": 3000, "MH": 3060, "ML": 2990, "MC": 3045, "MVo": 1_000_000, "MVa": 3e9},
        {"Date": pd.Timestamp(date), "Code": "99990", "MO": None, "MH": None, "ML": None, "MC": None, "MVo": 0, "MVa": 0},
    ])


class _FakeClient:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def get_eq_bars_daily_am(self, code=""):
        self.calls += 1
        f = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if isinstance(f, Exception):
            raise f
        return f


@pytest.fixture
def today():
    return jquants_fetcher.now_jst().date()


def test_fetch_returns_none_until_today_reflected(monkeypatch, today):
    client = _FakeClient([_am_df(today - timedelta(days=1))])
    monkeypatch.setattr(jquants_fetcher, "_get_client", lambda: client)
    assert jquants_fetcher.fetch_morning_bars_today() is None


def test_fetch_parses_today_and_skips_no_trade(monkeypatch, today):
    client = _FakeClient([_am_df(today)])
    monkeypatch.setattr(jquants_fetcher, "_get_client", lambda: client)
    bars = jquants_fetcher.fetch_morning_bars_today()
    assert set(bars) == {"7203"}
    assert bars["7203"]["open"] == 3000 and bars["7203"]["close"] == 3045


def test_wait_polls_until_reflected(monkeypatch, today):
    client = _FakeClient([_am_df(today - timedelta(days=1)), _am_df(today - timedelta(days=1)), _am_df(today)])
    monkeypatch.setattr(jquants_fetcher, "_get_client", lambda: client)
    monkeypatch.setattr(jquants_fetcher.time, "sleep", lambda s: None)
    deadline = jquants_fetcher.now_jst() + timedelta(minutes=30)
    bars = jquants_fetcher.wait_for_morning_bars(deadline, interval_sec=60)
    assert bars and client.calls == 3


def test_wait_gives_up_after_deadline(monkeypatch, today):
    client = _FakeClient([_am_df(today - timedelta(days=1))])
    monkeypatch.setattr(jquants_fetcher, "_get_client", lambda: client)
    monkeypatch.setattr(jquants_fetcher.time, "sleep", lambda s: None)
    deadline = jquants_fetcher.now_jst() + timedelta(seconds=10)
    assert jquants_fetcher.wait_for_morning_bars(deadline, interval_sec=60) is None
    assert client.calls == 1


def test_wait_stops_immediately_on_plan_error(monkeypatch):
    client = _FakeClient([Exception("403 Client Error: Forbidden")])
    monkeypatch.setattr(jquants_fetcher, "_get_client", lambda: client)
    monkeypatch.setattr(jquants_fetcher.time, "sleep", lambda s: pytest.fail("should not sleep"))
    deadline = jquants_fetcher.now_jst() + timedelta(minutes=30)
    assert jquants_fetcher.wait_for_morning_bars(deadline) is None


def test_noon_screener_prefers_am_bars(monkeypatch):
    monkeypatch.setattr(data_fetcher, "_fetch_prev_close", lambda t, d: 2950.0)
    called = []
    monkeypatch.setattr(noon_screener, "get_morning_session_metrics", lambda t: called.append(t) or {"error": "x"})
    am = {"7203": {"date": "2026-11-27", "open": 3000.0, "high": 3060.0, "low": 2990.0, "close": 3045.0, "volume": 1e6, "value": 3e9}}
    out = noon_screener._calc_noon_signals({"code": "7203", "close": 2950.0}, am_bars=am)
    assert not called
    assert out["morning"]["source"] == "jquants_am"
    assert out["close"] == 3045.0
    assert out["morning"]["gap_pct"] == pytest.approx(1.69, abs=0.01)

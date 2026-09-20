"""探测模块测试：保证它在任何情况下都不阻断机器人启动。"""

from __future__ import annotations

import logging
import sys

import pytest


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    for mod in ("app.probe", "app.config"):
        sys.modules.pop(mod, None)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'probe.db'}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("FOOTBALL_API_KEY", "fake")
    monkeypatch.setenv("ENABLED_LEAGUES", "39")
    yield


def test_run_probe_returns_all_keys():
    from datetime import date

    import app.probe as probe

    rep = probe.run_probe(date(2026, 9, 21))
    for key in ("fixtures", "odds_by_date", "odds_by_fixture",
                "standings_current", "bookmakers"):
        assert key in rep
        assert "ok" in rep[key]


def test_probe_never_raises_on_network_failure(monkeypatch):
    """网络异常也必须返回结构化的失败结果，绝不能抛异常。"""
    import app.probe as probe

    def boom(path, params):
        raise RuntimeError("network down")

    monkeypatch.setattr(probe, "_get", boom)
    from datetime import date
    rep = probe.run_probe(date(2026, 9, 21))
    assert rep["fixtures"]["ok"] is False


def test_missing_api_key_reports_clearly(monkeypatch):
    from datetime import date

    import app.probe as probe

    monkeypatch.setenv("FOOTBALL_API_KEY", "")
    rep = probe.run_probe(date(2026, 9, 21))
    assert rep["fixtures"]["ok"] is False
    assert "FOOTBALL_API_KEY" in rep["fixtures"]["msg"]


def test_probe_once_on_start_is_guarded(monkeypatch, tmp_path):
    """同一部署内只探测一次；PROBE_ON_START=0 时完全跳过。"""
    import app.probe as probe

    marker = tmp_path / "done"
    monkeypatch.setattr(probe, "PROBE_DONE_FILE", str(marker))

    calls = []
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: calls.append(1))

    probe.probe_once_on_start()
    assert len(calls) == 1
    probe.probe_once_on_start()
    assert len(calls) == 1, "已探测过则不应重复消耗配额"


def test_probe_can_be_disabled(monkeypatch, tmp_path):
    import app.probe as probe

    monkeypatch.setattr(probe, "PROBE_DONE_FILE", str(tmp_path / "d2"))
    monkeypatch.setenv("PROBE_ON_START", "0")
    calls = []
    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: calls.append(1))
    probe.probe_once_on_start()
    assert calls == []


def test_probe_failure_does_not_break_startup(monkeypatch, tmp_path):
    """启动探测本身异常时必须被吞掉，不能让机器人起不来。"""
    import app.probe as probe

    monkeypatch.setattr(probe, "PROBE_DONE_FILE", str(tmp_path / "d3"))

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(probe, "run_probe", boom)
    probe.probe_once_on_start()   # 不应抛出


def test_summarize_odds_reads_bookmakers_and_markets():
    import app.probe as probe

    data = {"response": [
        {"bookmakers": [
            {"name": "Bet365", "bets": [
                {"name": "Match Winner", "values": []},
                {"name": "Over/Under", "values": []},
            ]},
        ]},
    ]}
    text = probe._summarize_odds(data)
    assert "Bet365" in text
    assert "1 场" in text


def test_log_report_emits_marker(caplog):
    import app.probe as probe

    rep = {
        "date": "2026-09-21", "league": 39, "season": 2026,
        "fixtures": {"ok": True},
        "odds_by_date": {"ok": True},
        "odds_by_fixture": {"ok": False, "msg": "x"},
        "standings_current": {"ok": False, "msg": "season blocked"},
        "bookmakers": {"ok": True, "count": 30},
    }
    with caplog.at_level(logging.INFO):
        probe._log_report(rep)
    joined = caplog.text
    assert probe.MARKER in joined
    assert "赔率可用" in joined

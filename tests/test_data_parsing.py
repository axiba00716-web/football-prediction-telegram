"""data.py 测试：时间解析、球队 ID 解析、sync_date 窗口、sync_team_history。

全部通过 patch ``FootballAPI._request`` 完成，不访问真实网络。"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for mod in ("app.data", "app.config", "app.db"):
        sys.modules.pop(mod, None)
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data.db'}")
    monkeypatch.setenv("FOOTBALL_API_KEY", "test-key")
    monkeypatch.setenv("ENABLED_LEAGUES", "39,140")
    yield
    db_module.reset_db_state()


def _item(fid, dt, home_id, away_id, league_id=39, status="NS", hg=None, ag=None):
    return {
        "fixture": {"id": fid, "date": dt, "status": {"short": status}},
        "league": {"id": league_id, "name": "Premier League"},
        "teams": {"home": {"id": home_id, "name": f"H{fid}"},
                  "away": {"id": away_id, "name": f"A{fid}"}},
        "goals": {"home": hg, "away": ag},
    }


# ---------- 14. 日期时间格式解析 ----------

class TestParseTime:
    def test_iso_with_offset(self):
        dt = _parse("2026-09-20T15:00:00+00:00")
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 9, 20, 15, 0)

    def test_iso_with_z(self):
        dt = _parse("2026-09-20T15:00:00Z")
        assert (dt.year, dt.hour) == (2026, 15)

    def test_space_separator(self):
        assert _parse("2026-09-20 15:00:00") == datetime(2026, 9, 20, 15, 0, 0)

    def test_date_only(self):
        assert _parse("2026-09-20") == datetime(2026, 9, 20)

    def test_invalid_returns_none(self):
        assert _parse(None) is None
        assert _parse("") is None
        assert _parse("not-a-date") is None

    def test_start_time_never_none_for_api_formats(self):
        """回归：+00:00 曾导致 start_time=None，使比赛无法按日期筛选。"""
        for s in ("2026-09-20T15:00:00+00:00", "2026-09-20T15:00:00Z",
                  "2026-09-20 15:00:00"):
            assert _parse(s) is not None, f"{s} 不应解析失败"


def _parse(value):
    from app.data import _parse_time
    return _parse_time(value)


# ---------- 13. API 数据正确转换球队 ID ----------

class TestParseFixture:
    def test_team_and_league_ids_preserved(self):
        from app.data import _parse_fixture
        out = _parse_fixture(_item(999, "2026-09-20T15:00:00+00:00", 101, 202))
        assert out["external_id"] == 999
        assert out["home_team_id"] == 101
        assert out["away_team_id"] == 202
        assert out["league_id"] == 39
        assert out["start_time"] is not None
        assert out["home"] == "H999" and out["away"] == "A999"

    def test_disallowed_league_dropped(self):
        from app.data import _parse_fixture
        assert _parse_fixture(_item(1, "2026-09-20T15:00:00+00:00", 1, 2, league_id=999),
                              allowed_leagues={39}) is None

    def test_finished_status_helper(self):
        from app.data import _is_finished_status
        assert _is_finished_status("FT")
        assert _is_finished_status("AET")
        assert not _is_finished_status("NS")
        assert not _is_finished_status("TBD")


# ---------- sync_date ----------

class TestSyncDate:
    def test_sync_date_writes_and_returns_day_window(self, monkeypatch):
        from app.data import FootballAPI, sync_date

        calls = []

        def fake_request(self, path, params=None):
            calls.append((path, params))
            return {"response": [
                _item(1, "2026-09-20T01:00:00+00:00", 10, 20),
                _item(2, "2026-09-20T23:30:00+00:00", 30, 40),
            ]}

        monkeypatch.setattr(FootballAPI, "_request", fake_request)

        written, fixtures = asyncio.run(sync_date(date(2026, 9, 20)))

        assert calls and calls[0][0] == "/fixtures"
        assert calls[0][1]["date"] == "2026-09-20"
        assert written == 2
        assert len(fixtures) == 2, "当天 01:00 与 23:30 都应落在窗口内"
        for f in fixtures:
            assert f["start_time"] is not None
            assert datetime(2026, 9, 20) <= f["start_time"] < datetime(2026, 9, 21)

    def test_sync_date_filters_disabled_leagues(self, monkeypatch):
        from app.data import FootballAPI, sync_date

        monkeypatch.setattr(FootballAPI, "_request", lambda self, path, params=None: {
            "response": [
                _item(1, "2026-09-20T15:00:00+00:00", 10, 20, league_id=39),
                _item(2, "2026-09-20T15:00:00+00:00", 30, 40, league_id=888),
            ]
        })
        written, fixtures = asyncio.run(sync_date(date(2026, 9, 20)))
        assert written == 1
        assert [f["external_id"] for f in fixtures] == [1]

    def test_upsert_is_idempotent(self):
        from app.data import _parse_fixture, upsert_fixtures
        rows = [_parse_fixture(_item(7, "2026-09-20T15:00:00+00:00", 10, 20))]
        assert upsert_fixtures(rows) == 1
        assert upsert_fixtures(rows) == 1  # 再次写入为更新，不新增
        from app.db import Fixture, get_session
        s = get_session()
        try:
            assert s.query(Fixture).count() == 1
        finally:
            s.close()


# ---------- sync_team_history ----------

class TestSyncTeamHistory:
    def test_history_keeps_other_leagues(self, monkeypatch):
        """球队历史可能来自其他联赛，必须保留以供预测器使用。"""
        from app.data import FootballAPI, sync_team_history

        seen = {}

        def fake_request(self, path, params=None):
            seen.update(params or {})
            return {"response": [
                _item(11, "2026-08-01T15:00:00+00:00", 33, 34, league_id=999,
                      status="FT", hg=2, ag=1),
                _item(12, "2026-08-05T15:00:00+00:00", 33, 35, league_id=39,
                      status="FT", hg=1, ag=1),
            ]}

        monkeypatch.setattr(FootballAPI, "_request", fake_request)
        written = asyncio.run(sync_team_history(33, last=20))

        assert seen.get("team") == 33
        # 免费套餐不支持 last 参数，改用 season 拉取整季后本地截取
        assert "last" not in seen, "API-Football 免费套餐不支持 last 参数"
        assert seen.get("season") is not None
        assert written == 2, "跨联赛历史不应被 ENABLED_LEAGUES 过滤掉"

    def test_zero_team_id_is_noop(self):
        from app.data import sync_team_history
        assert asyncio.run(sync_team_history(0)) == 0


# ---------- FootballAPI 基础行为 ----------

class TestFootballAPI:
    def test_missing_api_key_raises(self, monkeypatch):
        import app.data as data_mod
        monkeypatch.setenv("FOOTBALL_API_KEY", "")
        data_mod.get_settings.cache_clear()
        try:
            with pytest.raises(data_mod.FootballAPIError):
                data_mod.FootballAPI()._request("/fixtures", {})
        finally:
            data_mod.get_settings.cache_clear()

    def test_settings_are_read_from_config(self):
        from app.data import FootballAPI
        api = FootballAPI()
        assert api.api_key == "test-key"
        assert api.base_url.startswith("http")
        assert api.timeout > 0

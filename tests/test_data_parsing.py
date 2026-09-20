"""data.py 时间解析与日期窗口专项测试：覆盖 API-Football 实际返回的 ISO 格式。"""

import os
import sys
import pytest
from datetime import date, datetime

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_data_parse.db")
os.environ.setdefault("FOOTBALL_API_KEY", "test-key")

from app.data import _parse_time, _parse_fixture  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_modules():
    for mod in ("app.data", "app.config", "app.db"):
        sys.modules.pop(mod, None)
    yield


class TestParseTime:
    def test_iso_with_positive_offset(self):
        # API-Football 常见格式
        dt = _parse_time("2026-09-20T15:00:00+00:00")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 9 and dt.day == 20
        assert dt.hour == 15 and dt.minute == 0

    def test_iso_with_z(self):
        dt = _parse_time("2026-09-20T15:00:00Z")
        assert dt is not None
        assert dt.year == 2026 and dt.hour == 15

    def test_iso_with_negative_offset(self):
        dt = _parse_time("2026-09-20T20:00:00+05:30")
        assert dt is not None
        assert dt.utcoffset() is not None

    def test_space_separator(self):
        dt = _parse_time("2026-09-20 15:00:00")
        assert dt == datetime(2026, 9, 20, 15, 0, 0)

    def test_date_only(self):
        dt = _parse_time("2026-09-20")
        assert dt == datetime(2026, 9, 20, 0, 0, 0)

    def test_none_returns_none(self):
        assert _parse_time(None) is None

    def test_datetime_passthrough(self):
        src = datetime(2026, 9, 20, 15, 0, 0)
        assert _parse_time(src) is src

    def test_garbage_returns_none(self):
        assert _parse_time("not-a-date") is None
        assert _parse_time("2026/99/99 99:99:99") is None

    def test_empty_string_returns_none(self):
        assert _parse_time("") is None
        assert _parse_time("   ") is None

    def test_start_time_never_none_for_valid_iso(self):
        """回归：+00:00 格式曾导致 start_time=None，使比赛无法按日期筛选。"""
        for s in ("2026-09-20T15:00:00+00:00",
                  "2026-09-20T15:00:00Z",
                  "2026-09-20 15:00:00"):
            assert _parse_time(s) is not None, f"格式 {s} 不应解析失败"


class TestSyncDateWindow:
    def test_sync_date_uses_day_open_closed_interval(self):
        """sync_date 应以 [当天00:00, 次日00:00) 为窗口，覆盖 23:xx 开赛的比赛。"""
        import app.data as data_mod
        from app.data import _parse_fixture

        captured = {}

        def fake_request(path, params=None):
            # 记录实际请求参数
            captured["path"] = path
            captured["params"] = params
            # 返回当天边界两场比赛：凌晨 01:00 与深夜 23:30
            return {
                "response": [
                    {"fixture": {"id": 1, "date": "2026-09-20T01:00:00+00:00",
                                 "status": {"short": "NS"}},
                     "league": {"id": 39, "name": "Premier League"},
                     "teams": {"home": {"id": 10, "name": "A"}, "away": {"id": 20, "name": "B"}},
                     "goals": {"home": None, "away": None}},
                    {"fixture": {"id": 2, "date": "2026-09-20T23:30:00+00:00",
                                 "status": {"short": "NS"}},
                     "league": {"id": 39, "name": "Premier League"},
                     "teams": {"home": {"id": 30, "name": "C"}, "away": {"id": 40, "name": "D"}},
                     "goals": {"home": None, "away": None}},
                ]
            }

        monkeypatch_request = None
        # 通过 patch _request 模拟
        import asyncio
        orig = data_mod._request

        calls = []
        def patched(path, params=None):
            calls.append((path, params))
            return fake_request(path, params)

        data_mod._request = patched
        try:
            import asyncio
            target = date(2026, 9, 20)
            written, fixtures = asyncio.run(data_mod.sync_date(target))
            # 请求参数含正确日期
            assert calls, "应发起一次 /fixtures 请求"
            path, params = calls[0]
            assert "/fixtures" in path
            assert params.get("date") == "2026-09-20"
            # 两条比赛都写入（时间解析成功，start_time 非 None）
            assert written == 2
            starts = [f["start_time"] for f in fixtures]
            assert all(s is not None for s in starts), "start_time 不应有 None"
            # 两条都在当天窗口内（用 naive 比较：API 返回 UTC，存库时已是 datetime）
            for s in starts:
                assert s >= datetime(2026, 9, 20, 0, 0, 0)
                assert s < datetime(2026, 9, 21, 0, 0, 0)
        finally:
            data_mod._request = orig


class TestParseFixturePreservesTeamIds:
    def test_team_ids_and_league_id_present(self):
        item = {
            "fixture": {"id": 999, "date": "2026-09-20T15:00:00+00:00",
                        "status": {"short": "NS"}},
            "league": {"id": 39, "name": "Premier League"},
            "teams": {"home": {"id": 101, "name": "Home FC"},
                      "away": {"id": 202, "name": "Away FC"}},
            "goals": {"home": None, "away": None},
        }
        out = _parse_fixture(item, allowed_leagues={39})
        assert out is not None
        assert out["home_team_id"] == 101
        assert out["away_team_id"] == 202
        assert out["league_id"] == 39
        assert out["start_time"] is not None
        assert out["external_id"] == 999

    def test_league_not_allowed_returns_none(self):
        item = {
            "fixture": {"id": 1, "date": "2026-09-20T15:00:00+00:00",
                        "status": {"short": "NS"}},
            "league": {"id": 999, "name": "Other"},
            "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
            "goals": {"home": None, "away": None},
        }
        assert _parse_fixture(item, allowed_leagues={39}) is None

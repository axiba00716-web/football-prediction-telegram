"""时区口径测试：`/today`、`/tomorrow`、`/predict` 必须按用户时区的比赛当天计算。

背景：数据库存 UTC，Railway 容器本机也是 UTC，但用户说的"今天"是
``TIMEZONE``（默认 Asia/Shanghai）的那一天。若直接用服务器本地时间，
北京时间 00:00–08:00 会拿到前一天的比赛。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    for mod in ("app.bot", "app.data", "app.config", "app.db", "app.main"):
        sys.modules.pop(mod, None)
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tz.db'}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FOOTBALL_API_KEY", "test-key")
    monkeypatch.setenv("TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("ENABLED_LEAGUES", "39")
    yield
    db_module.reset_db_state()


CST = timezone(timedelta(hours=8))


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi)


# ---------- 窗口换算 ----------

def test_local_day_window_maps_to_utc():
    from app.data import local_day_window

    start, end = local_day_window(date(2026, 9, 20), CST)
    assert start == _utc(2026, 9, 19, 16, 0), "东八区 9/20 00:00 = UTC 9/19 16:00"
    assert end == _utc(2026, 9, 20, 16, 0), "东八区 9/21 00:00 = UTC 9/20 16:00"


def test_local_day_window_utc_timezone_is_identity():
    from app.data import local_day_window

    start, end = local_day_window(date(2026, 9, 20), timezone.utc)
    assert start == _utc(2026, 9, 20, 0, 0)
    assert end == _utc(2026, 9, 21, 0, 0)


def test_api_request_includes_timezone_param(monkeypatch):
    """API 请求必须带 timezone，否则 date= 会按 UTC 切分。"""
    import asyncio

    import app.data as data_mod
    from app.data import fixtures_by_date

    captured = []

    def fake_http_get(path, params, api_key, base_url, timeout=None):
        captured.append(dict(params or {}))
        return {"response": []}

    monkeypatch.setattr(data_mod, "_http_get", fake_http_get)
    asyncio.run(fixtures_by_date(date(2026, 9, 20)))

    assert captured, "应发起一次请求"
    assert captured[0]["date"] == "2026-09-20"
    assert captured[0]["timezone"] == "Asia/Shanghai", \
        "缺少 timezone 参数，东八区凌晨场会被算到前一天"


def test_team_fixtures_also_sends_timezone(monkeypatch):
    """历史请求同样带 timezone，保证开赛时间口径一致。"""
    import asyncio

    import app.data as data_mod
    from app.data import team_fixtures

    captured = []
    monkeypatch.setattr(data_mod, "_http_get",
                        lambda path, params, api_key, base_url, timeout=None: (
                            captured.append(dict(params or {})), {"response": []})[1])
    asyncio.run(team_fixtures(33, last=20))
    assert captured[0]["timezone"] == "Asia/Shanghai"


def test_with_timezone_does_not_override_explicit_value():
    from app.data import FootballAPI
    api = FootballAPI(api_key="k", timezone="Asia/Shanghai")
    assert api._with_timezone({"date": "2026-09-20"}) == \
        {"date": "2026-09-20", "timezone": "Asia/Shanghai"}
    assert api._with_timezone({"timezone": "UTC"})["timezone"] == "UTC"


# ---------- 核心回归：北京时间凌晨的比赛属于「今天」 ----------

def test_sync_local_date_includes_late_night_matches(monkeypatch):
    """北京时间 9/20 00:30 与 23:00 的比赛都要算 9/20，且不能混入 9/21。"""
    import app.data as data_mod
    from app.data import sync_local_date

    # 三场比赛（UTC）：
    #   A 9/19 16:30 = 北京 9/20 00:30  → 属于今天
    #   B 9/20 15:00 = 北京 9/20 23:00  → 属于今天
    #   C 9/20 16:30 = 北京 9/21 00:30  → 不属于今天
    payload = {
        "2026-09-19": [{"fixture": {"id": 1, "date": "2026-09-19T16:30:00+00:00",
                                    "status": {"short": "NS"}},
                        "league": {"id": 39, "name": "EPL"},
                        "teams": {"home": {"id": 10, "name": "A"},
                                  "away": {"id": 11, "name": "B"}},
                        "goals": {"home": None, "away": None}}],
        "2026-09-20": [{"fixture": {"id": 2, "date": "2026-09-20T15:00:00+00:00",
                                    "status": {"short": "NS"}},
                        "league": {"id": 39, "name": "EPL"},
                        "teams": {"home": {"id": 12, "name": "C"},
                                  "away": {"id": 13, "name": "D"}},
                        "goals": {"home": None, "away": None}},
                       {"fixture": {"id": 3, "date": "2026-09-20T16:30:00+00:00",
                                    "status": {"short": "NS"}},
                        "league": {"id": 39, "name": "EPL"},
                        "teams": {"home": {"id": 14, "name": "E"},
                                  "away": {"id": 15, "name": "F"}},
                        "goals": {"home": None, "away": None}}],
    }
    queried = []

    def fake_http_get(path, params, api_key, base_url, timeout=None):
        queried.append(dict(params or {}))
        # 模拟 API 尊重 timezone：date=2026-09-20 + Asia/Shanghai
        # → 返回上海 9/20 全天（UTC 9/19 16:00 ~ 9/20 16:00）的比赛
        if params.get("timezone") == "Asia/Shanghai" and params.get("date") == "2026-09-20":
            return {"response": payload["2026-09-19"] + payload["2026-09-20"]}
        return {"response": []}

    monkeypatch.setattr(data_mod, "_http_get", fake_http_get)

    written, rows = asyncio.run(sync_local_date(date(2026, 9, 20)))

    assert len(queried) == 1, "timezone 生效后只需一次请求（省 API 配额）"
    assert queried[0] == {"date": "2026-09-20", "timezone": "Asia/Shanghai"}
    assert written == 3
    assert [r["external_id"] for r in rows] == [1, 2], \
        "北京 9/20 00:30 与 23:00 应在今天；北京 9/21 00:30 不应出现"


def test_sync_local_date_excludes_previous_local_day(monkeypatch):
    """北京 9/19 23:00（UTC 9/19 15:00）不能出现在北京 9/20 的清单里。"""
    import app.data as data_mod
    from app.data import sync_local_date
    from app.db import init_db

    init_db()
    monkeypatch.setattr(data_mod, "_http_get",
                        lambda path, params, api_key, base_url, timeout=None: {
        "response": [
            {"fixture": {"id": 9, "date": "2026-09-19T15:00:00+00:00",
                         "status": {"short": "FT"}},
             "league": {"id": 39, "name": "EPL"},
             "teams": {"home": {"id": 1, "name": "X"}, "away": {"id": 2, "name": "Y"}},
             "goals": {"home": 1, "away": 1}},
        ]
    })
    written, rows = asyncio.run(sync_local_date(date(2026, 9, 20)))
    assert rows == [], "UTC 9/19 15:00 = 北京 9/19 23:00，属于前一天"


# ---------- 数据库时间统一规则 ----------

def test_to_utc_naive_normalizes_offsets():
    from app.data import _parse_time, to_utc_naive

    # aware 带偏移 → 折算成 UTC naive
    assert to_utc_naive("2026-09-20T15:00:00+00:00") == _utc(2026, 9, 20, 15, 0)
    assert to_utc_naive("2026-09-20T15:00:00Z") == _utc(2026, 9, 20, 15, 0)
    # 东八区 23:00 → UTC 15:00
    assert to_utc_naive("2026-09-20T23:00:00+08:00") == _utc(2026, 9, 20, 15, 0)
    # 负偏移
    assert to_utc_naive("2026-09-20T10:00:00-05:00") == _utc(2026, 9, 20, 15, 0)
    # naive 视为已 UTC，原样保留
    assert to_utc_naive(_utc(2026, 9, 20, 15, 0)) == _utc(2026, 9, 20, 15, 0)
    assert to_utc_naive(None) is None
    # 解析结果一定不带 tzinfo，杜绝 naive/aware 混用
    assert to_utc_naive("2026-09-20T23:00:00+08:00").tzinfo is None


def test_parse_time_keeps_offset_info():
    """解析阶段保留 aware（偏移信息不丢），归一化只在入库时发生。"""
    from app.data import _parse_time

    dt = _parse_time("2026-09-20T23:00:00+08:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=8)


def test_stored_start_time_is_utc_naive(monkeypatch):
    """入库后的 start_time 必须是 UTC naive，才能与窗口边界直接比较。"""
    import asyncio

    import app.data as data_mod
    from app.data import sync_local_date, upsert_fixtures
    from app.db import Fixture, get_session, init_db

    init_db()
    monkeypatch.setattr(data_mod, "_http_get",
                        lambda path, params, api_key, base_url, timeout=None: {"response": [
                            {"fixture": {"id": 77, "date": "2026-09-20T23:00:00+08:00",
                                         "status": {"short": "NS"}},
                             "league": {"id": 39, "name": "EPL"},
                             "teams": {"home": {"id": 1, "name": "H"},
                                       "away": {"id": 2, "name": "A"}},
                             "goals": {"home": None, "away": None}},
                        ]})

    asyncio.run(sync_local_date(date(2026, 9, 20)))
    session = get_session()
    try:
        f = session.query(Fixture).filter(Fixture.external_id == 77).one()
        assert f.start_time.tzinfo is None
        assert f.start_time == _utc(2026, 9, 20, 15, 0), "东八区 23:00 应存为 UTC 15:00"
    finally:
        session.close()


# ---------- today_local / 显示时间 ----------

def test_today_local_follows_configured_timezone(monkeypatch):
    import app.bot as bot_mod
    from datetime import datetime as dt

    expected = dt.now(CST).date()
    assert bot_mod.today_local() == expected


def test_display_time_converted_to_local_timezone(monkeypatch):
    import app.bot as bot_mod

    # UTC 15:00 → 东八区 23:00
    assert bot_mod._local_time_text(_utc(2026, 9, 20, 15, 0)) == "2026-09-20 23:00"
    # UTC 16:30 → 东八区 次日 00:30
    assert bot_mod._local_time_text(_utc(2026, 9, 19, 16, 30)) == "2026-09-20 00:30"
    assert bot_mod._local_time_text(None) == "时间待定"


def test_fixtures_on_uses_local_day_window():
    import app.bot as bot_mod
    import app.db as db_mod
    from app.db import Fixture

    db_mod.init_db()
    session = db_mod.get_session()
    try:
        for fid, st in ((1, _utc(2026, 9, 19, 16, 30)),   # 北京 9/20 00:30
                        (2, _utc(2026, 9, 20, 15, 0)),    # 北京 9/20 23:00
                        (3, _utc(2026, 9, 20, 16, 30))):  # 北京 9/21 00:30
            session.add(Fixture(external_id=fid, league="EPL", league_id=39,
                                start_time=st, home_team_id=10, away_team_id=11,
                                home="H", away="A", status="NS"))
        session.commit()
    finally:
        session.close()

    rows = bot_mod._fixtures_on(date(2026, 9, 20))
    assert [r.external_id for r in rows] == [1, 2]


def test_timezone_fallback_without_tzdata(monkeypatch):
    """tzdata 缺失时按内置偏移表回退，绝不抛异常。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "zoneinfo":
            raise ImportError("no tzdata")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from app.config import get_timezone
    tz = get_timezone()
    assert datetime(2026, 9, 20, 12, 0, tzinfo=tz).utcoffset() == timedelta(hours=8)

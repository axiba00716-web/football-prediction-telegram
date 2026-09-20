"""/predict 命令端到端测试（无网络）：验证使用真实球队 ID，而非 fix.id。"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import pytest


class _Message:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)


class _Update:
    def __init__(self):
        self.message = _Message()


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    for mod in ("app.bot", "app.data", "app.config", "app.db", "app.main"):
        sys.modules.pop(mod, None)
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bot.db'}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FOOTBALL_API_KEY", "test-key")
    monkeypatch.setenv("MIN_HISTORY_MATCHES", "5")
    yield
    db_module.reset_db_state()


def _seed_today_fixture(session, fixture_pk=1, home_id=101, away_id=202):
    from app.db import Fixture
    f = Fixture(
        external_id=9000 + fixture_pk,
        league="EPL", league_id=39,
        start_time=datetime(2026, 9, 20, 15, 0, 0),
        home_team_id=home_id, away_team_id=away_id,
        home="Home FC", away="Away FC", status="NS",
    )
    session.add(f)
    session.commit()
    return f


def _seed_history(session, home_id, away_id, n=6):
    """为两队各造 n 场已结束比赛（主队主场 / 客队客场各 n 场）。"""
    from app.db import Fixture
    for i in range(n):
        session.add(Fixture(
            external_id=5000 + i, league="EPL", league_id=39,
            start_time=datetime(2026, 8, 1 + i, 15, 0, 0),
            home_team_id=home_id, away_team_id=999 + i,
            home="Home FC", away="Other", status="FT",
            home_score=2, away_score=1,
        ))
        session.add(Fixture(
            external_id=6000 + i, league="EPL", league_id=39,
            start_time=datetime(2026, 8, 10 + i, 15, 0, 0),
            home_team_id=888 + i, away_team_id=away_id,
            home="Other", away="Away FC", status="FT",
            home_score=0, away_score=2,
        ))
    session.commit()


def test_predict_uses_real_team_ids(monkeypatch):
    import app.bot as bot_mod
    import app.db as db_mod

    db_mod.init_db()
    session = db_mod.get_session()
    try:
        fix = _seed_today_fixture(session)
        _seed_history(session, home_id=101, away_id=202)
    finally:
        session.close()

    seen_ids = []

    async def fake_sync_history(team_id, last=20):
        seen_ids.append(team_id)
        return 0

    async def fake_sync_date(target):
        return 0, []

    monkeypatch.setattr(bot_mod, "sync_team_history", fake_sync_history)
    monkeypatch.setattr(bot_mod, "sync_date", fake_sync_date)

    update = _Update()
    asyncio.run(bot_mod.predict(update, None))

    # 关键：传给 sync_team_history 的是真实球队 ID，绝不能是 Fixture.id
    assert seen_ids == [101, 202]
    assert fix.id not in seen_ids

    body = "\n".join(update.message.texts)
    assert "101" in body and "202" in body, "预测依据应体现真实球队 ID"
    assert "仅供数据分析参考，不构成投注建议。" in body


def test_predict_missing_team_id_reports_unavailable(monkeypatch):
    import app.bot as bot_mod
    import app.db as db_mod

    db_mod.init_db()
    session = db_mod.get_session()
    try:
        _seed_today_fixture(session, fixture_pk=2, home_id=0, away_id=0)
    finally:
        session.close()

    called = []

    async def fake_sync_history(team_id, last=20):
        called.append(team_id)
        return 0

    monkeypatch.setattr(bot_mod, "sync_team_history", fake_sync_history)

    update = _Update()
    asyncio.run(bot_mod.predict(update, None))

    assert called == [], "球队 ID 缺失时不应发起历史同步"
    body = "\n".join(update.message.texts)
    assert "缺少真实球队 ID" in body


def test_predict_insufficient_history_message(monkeypatch):
    import app.bot as bot_mod
    import app.db as db_mod

    db_mod.init_db()
    session = db_mod.get_session()
    try:
        _seed_today_fixture(session, fixture_pk=3, home_id=101, away_id=202)
    finally:
        session.close()

    monkeypatch.setattr(bot_mod, "sync_team_history", lambda team_id, last=20: _noop())
    update = _Update()
    asyncio.run(bot_mod.predict(update, None))
    body = "\n".join(update.message.texts)
    assert "历史样本不足，暂不提供可靠预测" in body


async def _noop():
    return 0


def test_status_reports_db_counts():
    import app.bot as bot_mod
    import app.db as db_mod

    db_mod.init_db()
    session = db_mod.get_session()
    try:
        _seed_today_fixture(session, fixture_pk=4)
    finally:
        session.close()

    update = _Update()
    asyncio.run(bot_mod.status(update, None))
    body = "\n".join(update.message.texts)
    assert "机器人运行中" in body
    assert "比赛(Fixture)数量: 1" in body


def test_today_local_uses_configured_timezone(monkeypatch):
    import app.bot as bot_mod
    monkeypatch.setenv("TIMEZONE", "Asia/Shanghai")
    from datetime import date
    assert isinstance(bot_mod.today_local(), date)

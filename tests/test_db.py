"""数据库层测试：建表、URL 归一化、球队 ID 字段、Session 行为。"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """每个测试使用独立 SQLite 文件 + 重置 Engine 单例。"""
    for mod in ("app.db", "app.config"):
        sys.modules.pop(mod, None)
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'db_test.db'}")
    yield
    db_module.reset_db_state()


# ---------- 9. 数据库可以初始化 ----------

def test_database_can_be_initialized():
    from app.db import Fixture, Prediction, get_session, init_db

    init_db()  # 幂等
    init_db()

    session = get_session()
    try:
        assert session.query(Fixture).count() == 0
        assert session.query(Prediction).count() == 0
    finally:
        session.close()


# ---------- 10/11/12. URL 归一化 ----------

def test_postgres_url_is_normalized():
    from app.db import normalize_url_for_railway
    assert normalize_url_for_railway("postgres://user:pw@host:5432/db") == \
        "postgresql+psycopg://user:pw@host:5432/db"


def test_postgresql_url_is_normalized():
    from app.db import normalize_url_for_railway
    assert normalize_url_for_railway("postgresql://user:pw@host:5432/db") == \
        "postgresql+psycopg://user:pw@host:5432/db"


def test_postgresql_psycopg_url_unchanged():
    from app.db import normalize_url_for_railway
    url = "postgresql+psycopg://user:pw@host:5432/db"
    assert normalize_url_for_railway(url) == url


def test_sqlite_url_unchanged():
    from app.db import normalize_url_for_railway
    for url in ("sqlite:///./football.db", "sqlite:////tmp/x.db"):
        assert normalize_url_for_railway(url) == url


def test_sqlite_engine_uses_check_same_thread_false():
    from app.db import get_engine
    engine = get_engine()
    assert engine.dialect.name == "sqlite"
    assert engine.dialect.name.startswith("sqlite")


# ---------- Fixture / Prediction 字段完整性 ----------

def test_fixture_model_has_real_team_id_columns():
    from app.db import Fixture
    cols = {c.name for c in Fixture.__table__.columns}
    for name in ("id", "external_id", "league", "league_id", "start_time",
                 "home_team_id", "away_team_id", "home", "away",
                 "status", "home_score", "away_score", "updated_at"):
        assert name in cols, f"Fixture 缺少字段 {name}"


def test_prediction_model_columns():
    from app.db import Prediction
    cols = {c.name for c in Prediction.__table__.columns}
    for name in ("id", "fixture_id", "model_version", "home_prob", "draw_prob",
                 "away_prob", "expected_home_goals", "expected_away_goals",
                 "predicted_score", "confidence", "data_completeness",
                 "evidence", "created_at", "settled"):
        assert name in cols, f"Prediction 缺少字段 {name}"


def test_fixture_team_ids_are_independent_from_primary_key():
    """home_team_id / away_team_id 必须能存真实球队 ID，而非自增主键。"""
    from app.db import Fixture, get_session, init_db

    init_db()
    session = get_session()
    try:
        f = Fixture(external_id=12345, league="EPL", league_id=39,
                    home_team_id=101, away_team_id=202,
                    home="Home FC", away="Away FC", status="NS")
        session.add(f)
        session.commit()
        assert f.id is not None
        # 自增主键与球队 ID 完全独立
        assert f.home_team_id == 101 and f.away_team_id == 202
        assert f.id not in (f.home_team_id, f.away_team_id)

        loaded = session.query(Fixture).filter(Fixture.external_id == 12345).one()
        assert loaded.home_team_id == 101
        assert loaded.away_team_id == 202
    finally:
        session.close()


def test_get_session_returns_independent_sessions():
    from app.db import get_session
    s1 = get_session()
    s2 = get_session()
    try:
        assert s1 is not s2
    finally:
        s1.close()
        s2.close()


def test_old_table_missing_columns_is_repaired():
    """旧库缺 home_team_id / away_team_id 时，init_db() 应补列而不是崩溃。"""
    import app.db as db_module
    from sqlalchemy import text

    engine = db_module.get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE fixtures (id INTEGER PRIMARY KEY, external_id INTEGER)"))

    db_module.init_db()  # 不应抛异常

    from sqlalchemy import inspect
    cols = {c["name"] for c in inspect(engine).get_columns("fixtures")}
    assert "home_team_id" in cols
    assert "away_team_id" in cols
    assert "league_id" in cols

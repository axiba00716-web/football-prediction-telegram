"""SQLAlchemy 数据库层。

约定
----
* ``Fixture.external_id`` = API-Football 的 fixture id（唯一）；
  ``Fixture.id`` 是自增主键，**仅用于数据库内部引用**（如 Prediction.fixture_id）。
* ``home_team_id`` / ``away_team_id`` 使用 **API-Football 的真实球队 ID**，
  与预测模块保持一致，**绝不与 Fixture.id 混用**。
* ``home`` / ``away`` 只保存球队名称，仅用于展示。
* URL 归一化：``postgres://`` / ``postgresql://`` → ``postgresql+psycopg://``，
  SQLite 保持原样并加 ``check_same_thread=False`` 兼容多线程。

兼容旧表
--------
``init_db()`` 先 ``create_all``，再对**已存在但缺列**的表做一次 best-effort
``ALTER TABLE ADD COLUMN``（SQLite 与 PostgreSQL 均支持），避免旧库缺新字段
（home_team_id / away_team_id / league_id …）导致启动即崩。MVP 阶段仍建议
Railway 首次部署直接使用全新数据库（见 README）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text, UniqueConstraint, inspect, text,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

Base = declarative_base()


class Fixture(Base):
    """一场比赛（对应 API-Football 的一个 fixture）。"""

    __tablename__ = "fixtures"
    __table_args__ = (UniqueConstraint("external_id", name="uq_fixture_external"),)

    id = Column(Integer, primary_key=True)                                   # 仅数据库内部使用
    external_id = Column(Integer, nullable=False, unique=True, index=True)   # API-Football fixture id
    league = Column(String(120), default="")
    league_id = Column(Integer, default=0, index=True)
    start_time = Column(DateTime, nullable=True, index=True)
    home_team_id = Column(Integer, nullable=False, default=0, index=True)    # API-Football 球队 ID
    away_team_id = Column(Integer, nullable=False, default=0, index=True)    # API-Football 球队 ID
    home = Column(String(120), default="")                                   # 球队名称（展示用）
    away = Column(String(120), default="")
    status = Column(String(20), default="NS")
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Prediction(Base):
    """一条预测记录。同一 (fixture_id, model_version) 只保留一条。"""

    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint(
            "fixture_id", "model_version", "prediction_type", "prediction_market",
            name="uq_prediction_scope",
        ),
    )

    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, nullable=False, index=True)
    model_version = Column(String(40), default="")

    # ---- 口径分离（关键）----
    # full_1x2    全量胜平负（覆盖率 100%）
    # selected_1x2 精选胜平负（高置信子集）
    # binary      二分类市场
    prediction_type = Column(String(20), default="full_1x2", index=True)
    # 胜平负：home_win / draw / away_win
    # 二分类：home_double_chance / away_double_chance / over_1_5 / under_4_5 / btts
    prediction_market = Column(String(24), default="", index=True)
    prediction_probability = Column(Float, default=0.0)
    tier = Column(String(4), default="", index=True)        # A / B / C / ""
    consistency = Column(String(8), default="")             # 如 "3/3"

    home_prob = Column(Float, default=0.0)
    draw_prob = Column(Float, default=0.0)
    away_prob = Column(Float, default=0.0)
    expected_home_goals = Column(Float, default=0.0)
    expected_away_goals = Column(Float, default=0.0)
    predicted_score = Column(String(10), default="")
    confidence = Column(String(10), default="低")
    data_completeness = Column(Float, default=0.0)
    evidence = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    feature_cutoff_at = Column(DateTime, nullable=True)     # 特征截止时间（防未来函数）

    # ---- 赛后结算 ----
    settled = Column(Boolean, default=False, index=True)
    prediction_result = Column(String(20), default="")      # 实际结果
    is_correct = Column(Boolean, nullable=True)
    settled_at = Column(DateTime, nullable=True)


# --------------------------------------------------------------------------- #
# URL 归一化
# --------------------------------------------------------------------------- #

def normalize_url_for_railway(url: str) -> str:
    """把各类 PostgreSQL URL 统一成 SQLAlchemy 可用的 ``postgresql+psycopg://``。

    ``postgres://``    -> ``postgresql+psycopg://``
    ``postgresql://``  -> ``postgresql+psycopg://``
    ``postgresql+psycopg://`` 保持不变
    ``sqlite:///...``         保持不变
    """
    if not url:
        return url
    s = url.strip()
    if s.startswith("postgres://"):
        s = "postgresql+psycopg://" + s[len("postgres://"):]
    elif s.startswith("postgresql://"):
        s = "postgresql+psycopg://" + s[len("postgresql://"):]
    return s


# --------------------------------------------------------------------------- #
# Engine / Session 管理（懒初始化，单进程单例）
# --------------------------------------------------------------------------- #

_engine = None
_SessionLocal: Optional[sessionmaker] = None

DEFAULT_TIMEOUT_SECONDS = 20


def get_engine():
    """返回单例 Engine；按 DATABASE_URL 自动选择驱动与连接参数。"""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    settings = get_settings()
    raw = settings.DATABASE_URL or "sqlite:///./football.db"
    url = normalize_url_for_railway(raw)
    kwargs = {"future": True}

    if url.startswith("sqlite"):
        # SQLite + 多线程（Telegram polling 会在线程池里跑 handler）
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True   # Railway Postgres 空闲断连后自动重连

    from sqlalchemy import create_engine
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


# --------------------------------------------------------------------------- #
# 建表 + 旧表补列
# --------------------------------------------------------------------------- #

def _ddl_type(engine, column) -> str:
    """为缺失列生成一个尽量保守的 DDL 类型字符串（SQLite / PostgreSQL 通用）。"""
    is_pg = not engine.dialect.name.startswith("sqlite")
    py = type(column.type)
    try:
        from sqlalchemy import Boolean as SaBoolean, DateTime as SaDateTime, Float as SaFloat
        from sqlalchemy import Integer as SaInteger, Text as SaText
        if py is SaInteger:
            return "INTEGER"
        if py is SaFloat:
            return "DOUBLE PRECISION" if is_pg else "FLOAT"
        if py is SaBoolean:
            return "BOOLEAN" if is_pg else "SMALLINT"
        if py is SaDateTime:
            return "TIMESTAMP" if is_pg else "DATETIME"
        if py is SaText:
            return "TEXT"
        if py is type(String()) or isinstance(column.type, String):
            return f"VARCHAR({column.type.length or 255})"
    except Exception:  # pragma: no cover - 类型推断失败时走通用兜底
        pass
    return "VARCHAR(255)" if is_pg else "TEXT"


def _ensure_missing_columns(engine) -> None:
    """best-effort：为已存在但缺失新字段的表补列，失败只告警不影响启动。"""
    try:
        insp = inspect(engine)
    except Exception as e:  # pragma: no cover
        logger.warning("无法检查表结构（跳过补列）：%s", e)
        return

    for model in (Fixture, Prediction):
        table = model.__tablename__
        try:
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
        except Exception as e:  # pragma: no cover
            logger.warning("读取表 %s 结构失败：%s", table, e)
            continue

        for col in model.__table__.columns:
            if col.name in existing:
                continue
            try:
                ddl = f'ALTER TABLE {table} ADD COLUMN {col.name} {_ddl_type(engine, col)}'
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                logger.info("旧表 %s 已补列 %s", table, col.name)
            except Exception as e:
                # 不允许因补列失败影响启动
                logger.warning("表 %s 补列 %s 失败：%s", table, col.name, e)


def init_db() -> None:
    """建表（幂等），并对旧表补齐缺失列。"""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    _ensure_missing_columns(engine)


def get_session() -> Session:
    """返回一个**新的** Session；调用方必须显式 ``session.close()``。"""
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def reset_db_state() -> None:
    """测试辅助：重置 Engine 单例（切换 DATABASE_URL 前调用）。"""
    global _engine, _SessionLocal
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:  # pragma: no cover
            pass
    _engine = None
    _SessionLocal = None


__all__ = [
    "Base", "Fixture", "Prediction",
    "get_engine", "init_db", "get_session", "normalize_url_for_railway",
    "reset_db_state",
]

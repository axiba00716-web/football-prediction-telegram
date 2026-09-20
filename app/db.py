"""SQLAlchemy 数据库层。

约定
----
* ``Fixture.external_id`` = API-Football 的 fixture id (唯一)；
  ``Fixture.id`` 是自增主键，**仅用于数据库内部引用**。
* ``home_team_id`` / ``away_team_id`` 使用 **API-Football 的真实球队 ID**，
  与预测模块保持一致，绝不与 Fixture.id 混用。
* ``home`` / ``away`` 保存球队名称（用于展示）。
* URL 归一化：``postgres://`` / ``postgresql://`` → ``postgresql+psycopg://``，
  SQLite 保持原样，并加 ``check_same_thread=False`` 兼容多线程。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import get_settings

Base = declarative_base()


class Fixture(Base):
    """一场比赛。"""

    __tablename__ = "fixtures"
    __table_args__ = (UniqueConstraint("external_id", name="uq_fixture_external"),)

    id = Column(Integer, primary_key=True)
    external_id = Column(Integer, nullable=False, unique=True, index=True)  # API-Football fixture id
    league = Column(String(120), default="")
    league_id = Column(Integer, default=0, index=True)
    start_time = Column(DateTime, nullable=True, index=True)
    home_team_id = Column(Integer, nullable=False, default=0, index=True)   # API-Football 球队 ID
    away_team_id = Column(Integer, nullable=False, default=0, index=True)   # API-Football 球队 ID
    home = Column(String(120), default="")                                  # 球队名称
    away = Column(String(120), default="")
    status = Column(String(20), default="NS")
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Prediction(Base):
    """一条预测记录。"""

    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("fixture_id", "model_version", name="uq_prediction_fixture_model"),
    )

    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, nullable=False, index=True)
    model_version = Column(String(40), default="")
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
    settled = Column(Boolean, default=False)


# --------------------------------------------------------------------------- #
# Engine / Session 管理（懒初始化，线程安全）
# --------------------------------------------------------------------------- #

_engine = None
_SessionLocal: Optional[sessionmaker] = None


def normalize_url_for_railway(url: str) -> str:
    """将各类 PostgreSQL URL 统一为 SQLAlchemy 可用的 ``postgresql+psycopg://`` 形式。

    转换规则:
        postgres://...    -> postgresql+psycopg://...
        postgresql://...  -> postgresql+psycopg://...
        postgresql+psycopg://... (保持不变)
        sqlite:///...     (保持不变)
    """
    if not url:
        return url
    s = url.strip()
    if s.startswith("postgres://"):
        s = "postgresql+psycopg://" + s[len("postgres://"):]
    elif s.startswith("postgresql://"):
        s = "postgresql+psycopg://" + s[len("postgresql://"):]
    return s


def get_engine():
    """返回单例 Engine，按 DATABASE_URL 自动选择驱动与参数。"""
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    settings = get_settings()
    raw = settings.DATABASE_URL or "sqlite:///./football.db"
    url = normalize_url_for_railway(raw)
    kwargs = {"future": True}

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}

    from sqlalchemy import create_engine
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    """创建全部表（幂等，可重复调用）。"""
    Base.metadata.create_all(bind=get_engine())


def get_session() -> Session:
    """获取一个新的 ORM Session。调用方负责 ``session.close()``。

    推荐用法::

        with get_session() as s:
            ...
    """
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def reset_db_state() -> None:
    """测试辅助：重置引擎单例（切换 DATABASE_URL 前调用）。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


# 兼容旧调用：部分历史代码直接 import init_db / get_session
__all__ = [
    "Base", "Fixture", "Prediction",
    "get_engine", "init_db", "get_session", "get_session", "normalize_url_for_railway",
    "reset_db_state",
]

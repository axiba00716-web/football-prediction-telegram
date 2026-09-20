from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import get_settings

Base = declarative_base()


def _normalize_db_url(url: str) -> str:
    """Railway 提供的 postgres:// 需转换为 SQLAlchemy 驱动格式。"""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Fixture(Base):
    __tablename__ = "fixtures"

    id = Column(Integer, primary_key=True)
    external_id = Column(Integer, unique=True, nullable=False)
    league = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    home = Column(String, nullable=False)
    away = Column(String, nullable=False)
    status = Column(String, default="scheduled")
    home_score = Column(Integer)
    away_score = Column(Integer)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, ForeignKey("fixtures.id"), nullable=False)
    model_version = Column(String, nullable=False)
    home_prob = Column(Float, nullable=False)
    draw_prob = Column(Float, nullable=False)
    away_prob = Column(Float, nullable=False)
    expected_home_goals = Column(Float, nullable=False)
    expected_away_goals = Column(Float, nullable=False)
    predicted_score = Column(String, nullable=False)
    confidence = Column(String, nullable=False)
    data_completeness = Column(Float, nullable=False)
    evidence = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    settled = Column(Boolean, default=False)


def get_engine():
    settings = get_settings()
    url = _normalize_db_url(settings.DATABASE_URL)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    return engine


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session() -> Session:
    engine = get_engine()
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    return SessionLocal()


def normalize_url_for_railway(url: str) -> str:
    """公开给测试使用的 URL 转换函数。"""
    return _normalize_db_url(url)

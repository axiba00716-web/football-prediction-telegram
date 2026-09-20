import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 测试使用独立 SQLite，避免污染
os.environ["DATABASE_URL"] = "sqlite:///./test_football.db"
os.environ["FOOTBALL_API_KEY"] = "test-key"
os.environ["TELEGRAM_BOT_TOKEN"] = ""

from app.db import Base, init_db, normalize_url_for_railway, Fixture, Prediction
from app.predictor import predict_match, MODEL_VERSION


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///./test_football.db")
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    if os.path.exists("test_football.db"):
        os.remove("test_football.db")


def _history(n=8):
    """构造主队/客队各有 n 场已结束比赛的对称历史。"""
    home_id, away_id = 1, 2
    rows = []
    for i in range(n):
        rows.append({"home_id": home_id, "away_id": 3 + i, "home_goals": 2, "away_goals": 1, "status": "FT"})
        rows.append({"home_id": 4 + i, "away_id": away_id, "home_goals": 1, "away_goals": 2, "status": "FT"})
    return home_id, away_id, rows


def test_poisson_probabilities_valid():
    home_id, away_id, history = _history()
    result = predict_match(home_id, away_id, history)
    assert result is not None
    for p in (result.home_prob, result.draw_prob, result.away_prob):
        assert 0.0 <= p <= 1.0, "概率必须在 [0,1]"


def test_probabilities_sum_to_one():
    home_id, away_id, history = _history()
    result = predict_match(home_id, away_id, history)
    total = result.home_prob + result.draw_prob + result.away_prob
    assert abs(total - 1.0) < 1e-6, f"概率和应接近 1，实际 {total}"


def test_no_negative_probability():
    home_id, away_id, history = _history()
    result = predict_match(home_id, away_id, history)
    assert result.home_prob >= 0 and result.draw_prob >= 0 and result.away_prob >= 0


def test_insufficient_history_no_prediction():
    # 仅 2 场，远低于 MIN_HISTORY_MATCHES=5
    home_id, away_id = 1, 2
    history = [
        {"home_id": home_id, "away_id": 3, "home_goals": 1, "away_goals": 0, "status": "FT"},
        {"home_id": 4, "away_id": away_id, "home_goals": 0, "away_goals": 1, "status": "FT"},
    ]
    assert predict_match(home_id, away_id, history) is None


def test_future_results_excluded():
    """未结束比赛（含未来结果）不参与建模，应回退为数据不足。"""
    home_id, away_id = 1, 2
    history = [
        {"home_id": home_id, "away_id": 3, "home_goals": 2, "away_goals": 1, "status": "NS"},  # 未开赛
    ]
    assert predict_match(home_id, away_id, history, min_history=1) is None


def test_database_initializable(engine):
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    fix = Fixture(external_id=999, league="Test", start_time=__import__("datetime").datetime.utcnow(),
                  home="A", away="B", status="FT", home_score=1, away_score=0)
    session.add(fix)
    session.commit()
    pred = Prediction(fixture_id=fix.id, model_version=MODEL_VERSION,
                      home_prob=0.5, draw_prob=0.25, away_prob=0.25,
                      expected_home_goals=1.5, expected_away_goals=1.0,
                      predicted_score="1-0", confidence="中", data_completeness=1.0, evidence="test")
    session.add(pred)
    session.commit()
    assert session.query(Fixture).count() == 1
    assert session.query(Prediction).count() == 1
    session.close()


def test_init_db_runs():
    init_db()  # 不应抛异常


@pytest.mark.parametrize("url,expected", [
    ("postgres://user:pass@host/db", "postgresql+psycopg://user:pass@host/db"),
    ("postgresql://user:pass@host/db", "postgresql+psycopg://user:pass@host/db"),
    ("sqlite:///./football.db", "sqlite:///./football.db"),
])
def test_postgres_url_normalization(url, expected):
    assert normalize_url_for_railway(url) == expected

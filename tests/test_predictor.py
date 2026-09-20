"""predictor 单元测试：不依赖真实 Token / API Key / 网络。"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.predictor import (
    MODEL_VERSION, PredictionResult, predict_match, format_prediction,
    _is_finished,
)


def _finished(home_id, away_id, hg, ag, status="FT"):
    return {"home_id": home_id, "away_id": away_id, "home_goals": hg, "away_goals": ag, "status": status}


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    """隔离 DB 单例 + DATABASE_URL，避免测试间串扰。"""
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("MIN_HISTORY_MATCHES", "5")
    yield


# ---------- 1. 概率都在 [0, 1] ----------

def test_probabilities_between_0_and_1():
    result = predict_match(1, 2, [_finished(1, 2, 2, 1)] * 10 + [_finished(2, 1, 1, 3)] * 10)
    assert result is not None
    for key in ("home_prob", "draw_prob", "away_prob"):
        assert 0.0 <= getattr(result, key) <= 1.0, f"{key} 越界"


# ---------- 2. 概率和 ≈ 1 ----------

def test_probabilities_sum_to_one():
    result = predict_match(1, 2, [_finished(1, 2, 2, 1)] * 10 + [_finished(2, 1, 1, 3)] * 10)
    assert result is not None
    total = result.home_prob + result.draw_prob + result.away_prob
    assert abs(total - 1.0) < 1e-6


# ---------- 3. 无负数 ----------

def test_probabilities_non_negative():
    result = predict_match(1, 2, [_finished(1, 2, 2, 1)] * 10 + [_finished(2, 1, 1, 3)] * 10)
    assert result is not None
    assert result.home_prob >= 0 and result.draw_prob >= 0 and result.away_prob >= 0
    assert result.expected_home_goals >= 0 and result.expected_away_goals >= 0


# ---------- 4. 样本不足 → None ----------

def test_insufficient_history_returns_none():
    # 仅 3 场，低于 min_history=5
    assert predict_match(1, 2, [_finished(1, 2, 1, 0)] * 3) is None
    # 空历史
    assert predict_match(1, 2, []) is None
    # team id 为 0（缺失）
    assert predict_match(0, 2, [_finished(1, 2, 1, 0)] * 10) is None
    assert predict_match(1, 0, [_finished(1, 2, 1, 0)] * 10) is None


# ---------- 5. 未结束比赛不参与 ----------

def test_unfinished_matches_excluded():
    # 全部 NS（未开赛）→ 无有效已结束样本 → None
    ns = {"home_id": 1, "away_id": 2, "home_goals": 0, "away_goals": 0, "status": "NS"}
    history = [ns] * 20
    assert predict_match(1, 2, history) is None

    # 混有 finished + NS：只有 FT 计入
    history = [_finished(1, 2, 2, 1)] * 10 + [dict(ns)] * 10
    result = predict_match(1, 2, history, min_history=5)
    assert result is not None  # 10 场 FT 足够


# ---------- 6. 主客队 ID 不混淆（核心回归）----------

def test_team_ids_not_confused():
    # 主队(10)主场进球多 → 主队预期进球应高于「把主客颠倒」时
    home_strong = [_finished(10, 20, 3, 0)] * 10   # 10 主场 3 球
    away_weak   = [_finished(20, 10, 0, 1)] * 10    # 20 客场 0 球, 10 客场 1 球
    history = home_strong + away_weak

    r = predict_match(10, 20, history, min_history=5)
    assert r is not None
    # 主队(10)进攻极强、客队(20)进攻极弱 → 主胜概率应显著偏高
    assert r.home_prob > 0.85
    assert r.home_team_id == 10 and r.away_team_id == 20


def test_fixture_id_must_not_be_used_as_team_id():
    """旧 bug 复现保护：若调用方误把 fixture.id 当球队 id，必因查无此队历史而返回 None。

    Fixture.id (999) ≠ 真实球队 id (10/20)，历史按球队 id 索引。
    """
    from types import SimpleNamespace
    fix = SimpleNamespace(id=999, home_team_id=10, away_team_id=20)
    history = [_finished(10, 20, 2, 1)] * 10 + [_finished(20, 10, 1, 3)] * 10

    # ✅ 正确：用真实球队 ID
    ok = predict_match(fix.home_team_id, fix.away_team_id, history, min_history=5)
    assert ok is not None

    # ❌ 旧 bug：predict_match(fix.id, fix.id, ...)
    bug = predict_match(fix.id, fix.id, history, min_history=5)
    assert bug is None, "用 fixture.id 当两队 ID 必须查无历史 → 返回 None"


# ---------- 7. DB 可初始化（接口一致性）----------

def test_db_can_initialize(tmp_path, monkeypatch):
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'init.db'}")
    from app.db import init_db, Fixture, Prediction, get_session
    init_db()
    init_db()  # 幂等
    s = get_session()
    try:
        s.add(Fixture(external_id=1, home="H", away="A", home_team_id=1, away_team_id=2, status="NS"))
        s.commit()
        s.add(Prediction(fixture_id=1, model_version=MODEL_VERSION, home_prob=0.5,
                         draw_prob=0.25, away_prob=0.25, expected_home_goals=1.5,
                         expected_away_goals=1.2, predicted_score="1-1", confidence="中",
                         data_completeness=1.0, evidence="t"))
        s.commit()
        assert s.query(Fixture).count() == 1
        assert s.query(Prediction).count() == 1
    finally:
        s.close()


# ---------- 8 & 9. URL 转换 ----------

def test_normalize_postgres_url():
    from app.db import normalize_url_for_railway as n
    assert n("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"


def test_normalize_postgresql_url():
    from app.db import normalize_url_for_railway as n
    assert n("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert n("postgresql+psycopg://u:p@h/d") == "postgresql+psycopg://u:p@h/d"


def test_normalize_sqlite_unchanged():
    from app.db import normalize_url_for_railway as n
    assert n("sqlite:///./football.db") == "sqlite:///./football.db"
    assert n("") == ""


# ---------- 额外：format_prediction ----------

def test_format_prediction_readable():
    result = predict_match(1, 2, [_finished(1, 2, 2, 1)] * 10 + [_finished(2, 1, 1, 3)] * 10)
    assert result is not None
    text = format_prediction(result)
    assert "主胜" in text and "预期进球" in text
    assert result.evidence in text


# ---------- 额外：finished 状态判定 ----------

def test_is_finished_recognizes_all_valid():
    for s in ("FT", "AET", "PEN", "finished", "Match Finished"):
        assert _is_finished(s), s
    assert not _is_finished("NS")
    assert not _is_finished(None)
    assert not _is_finished("LIVE")

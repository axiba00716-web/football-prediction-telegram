"""predictor 单元测试：纯函数，不访问网络、不依赖真实 Token / API Key。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.predictor import (  # noqa: E402
    MODEL_VERSION, PredictionResult, _is_finished, format_prediction, predict_match,
)

MIN_HISTORY = 5


def _rec(home_id, away_id, hg, ag, status="FT"):
    """构造一条 history 记录。"""
    return {
        "home_id": home_id,
        "away_id": away_id,
        "home_goals": hg,
        "away_goals": ag,
        "status": status,
    }


def _both_sides(n=MIN_HISTORY + 3):
    """主队 1 主场 n 场 + 客队 2 客场 n 场，均满足最少样本要求。"""
    home = [_rec(1, 2, 2, 1) for _ in range(n)]      # 队 1 主场
    away = [_rec(1, 2, 1, 2) for _ in range(n)]      # 队 2 客场
    return home + away


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    """隔离 DB 单例 + 环境变量，避免测试间串扰。"""
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("MIN_HISTORY_MATCHES", str(MIN_HISTORY))
    yield


# ---------- 1. 正常样本可以生成预测 ----------

def test_normal_history_produces_prediction():
    result = predict_match(1, 2, _both_sides())
    assert isinstance(result, PredictionResult)
    assert result.model_version == MODEL_VERSION
    assert result.predicted_score.count("-") == 1


# ---------- 2. 三项概率都在 0 到 1 ----------

def test_probabilities_between_0_and_1():
    result = predict_match(1, 2, _both_sides())
    for key in ("home_prob", "draw_prob", "away_prob"):
        assert 0.0 <= getattr(result, key) <= 1.0, f"{key} 越界"


# ---------- 3. 概率总和接近 1 ----------

def test_probabilities_sum_to_one():
    result = predict_match(1, 2, _both_sides())
    total = result.home_prob + result.draw_prob + result.away_prob
    assert abs(total - 1.0) < 1e-9


# ---------- 4. 没有负数概率 ----------

def test_probabilities_non_negative():
    result = predict_match(1, 2, _both_sides())
    assert result.home_prob >= 0
    assert result.draw_prob >= 0
    assert result.away_prob >= 0
    assert result.expected_home_goals >= 0
    assert result.expected_away_goals >= 0


# ---------- 5. 主队历史不足 → None ----------

def test_insufficient_home_history_returns_none():
    # 队 1 只有 2 个主场样本，队 2 样本充足
    history = [_rec(1, 2, 2, 1) for _ in range(2)] + [_rec(3, 2, 1, 1) for _ in range(10)]
    assert predict_match(1, 2, history) is None


# ---------- 6. 客队历史不足 → None ----------

def test_insufficient_away_history_returns_none():
    # 队 1 主场样本充足，但队 2 作为客场只有 1 场
    history = [_rec(1, 3, 2, 0) for _ in range(10)] + [_rec(5, 2, 1, 1)]
    assert predict_match(1, 2, history) is None


# ---------- 7. NS / TBD 等未结束比赛不参与预测 ----------

def test_unfinished_matches_are_excluded():
    unfinished = (
        [_rec(1, 2, 9, 9, status="NS") for _ in range(20)]
        + [_rec(1, 2, 9, 9, status="TBD") for _ in range(20)]
    )
    assert predict_match(1, 2, unfinished) is None

    # 混入未结束比赛时，统计结果应与只有已结束比赛时一致
    finished_only = _both_sides()
    with_unfinished = finished_only + [_rec(1, 2, 9, 9, status="NS")]
    a = predict_match(1, 2, finished_only)
    b = predict_match(1, 2, with_unfinished)
    assert a is not None and b is not None
    assert a.home_prob == b.home_prob
    assert a.predicted_score == b.predicted_score


def test_is_finished_status_rules():
    for s in ("FT", "AET", "PEN", "finished", "Match Finished"):
        assert _is_finished(s), f"{s} 应视为已结束"
    for s in ("NS", "TBD", "CANC", "PST", "1H", "HT", "LIVE", None, ""):
        assert not _is_finished(s), f"{s} 不应视为已结束"


# ---------- 8. 主客队 ID 不会混淆 ----------

def test_home_away_ids_not_interchangeable():
    """队 1 主场极强 / 客场平庸，队 2 客场极弱 / 主场平庸。"""
    history = (
        [_rec(1, 2, 4, 0) for _ in range(MIN_HISTORY + 1)]   # 队 1 主场
        + [_rec(3, 2, 4, 0) for _ in range(MIN_HISTORY + 1)]  # 队 2 客场（大比分落败）
        + [_rec(3, 1, 1, 1) for _ in range(MIN_HISTORY + 1)]  # 队 1 客场（平庸）
        + [_rec(2, 4, 1, 1) for _ in range(MIN_HISTORY + 1)]  # 队 2 主场（平庸）
    )
    r1 = predict_match(1, 2, history)
    r2 = predict_match(2, 1, history)
    assert r1 is not None and r2 is not None

    # 1 主 2 客：队 1 主场碾压 + 队 2 客场极弱 → 明显偏向主胜
    assert r1.home_prob > r1.away_prob + 0.2

    # 2 主 1 客：队 1 整体 Elo 更高（源于其强势主场），故即便客场仍被看好。
    # 这正是「主客队 ID 未被混淆」的体现——同一批历史换个站位，结论随之改变。
    assert r2.away_prob > r2.home_prob, "队 1 实力更强，客场比赛仍应占优"
    assert r1.home_team_id == 1 and r1.away_team_id == 2
    assert r2.home_team_id == 2 and r2.away_team_id == 1
    # 两种站位下概率分布必须不同（否则说明模型没区分主客）
    assert abs(r1.home_prob - r2.home_prob) > 0.05


def test_stats_attribution_by_role():
    """主队样本只取 home_id==team，客队样本只取 away_id==team。"""
    from app.predictor import _team_away_stats, _team_home_stats

    h = [_rec(1, 2, 3, 1), _rec(2, 1, 0, 2)]
    assert _team_home_stats(h, 1) == (3.0, 1.0, 1)   # 队 1 主场：进 3 失 1
    assert _team_away_stats(h, 2) == (1.0, 3.0, 1)   # 队 2 客场：进 1 失 3
    assert _team_home_stats(h, 2) == (0.0, 2.0, 1)   # 队 2 主场：进 0 失 2
    assert _team_away_stats(h, 1) == (2.0, 0.0, 1)   # 队 1 客场：进 2 失 0


def test_history_filtered_by_role_not_by_presence():
    """主队只统计其主场记录，客队只统计其客场记录。"""
    # 队 1、队 2 都只有客场记录（home_id != 1/2），不应被当作主队样本
    history = [_rec(9, 1, 0, 2) for _ in range(10)] + [_rec(9, 2, 1, 1) for _ in range(10)]
    assert predict_match(1, 2, history) is None


# ---------- 其他：无球队 ID / 格式化 ----------

def test_missing_team_id_returns_none():
    assert predict_match(0, 2, _both_sides()) is None
    assert predict_match(1, 0, _both_sides()) is None
    assert predict_match(None, 2, _both_sides()) is None


def test_format_prediction_contains_key_fields():
    result = predict_match(1, 2, _both_sides())
    text = format_prediction(result)
    for kw in ("主胜", "平", "客胜", "预期进球", "最可能比分", "置信度", "数据完整度", MODEL_VERSION):
        assert kw in text

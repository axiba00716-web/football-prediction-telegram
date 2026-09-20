"""三口径体系测试：二分类市场、模型一致性、精选分级、分开结算与统计。

核心断言：**不同口径绝不混算**。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _prep(monkeypatch, tmp_path):
    for mod in ("app.bot", "app.data", "app.config", "app.db", "app.tracking", "app.markets"):
        sys.modules.pop(mod, None)
    import app.db as db_module
    db_module.reset_db_state()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mkt.db'}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("FOOTBALL_API_KEY", "test-key")
    monkeypatch.setenv("TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("MIN_HISTORY_MATCHES", "5")
    import app.db as _db
    _db.init_db()
    yield
    db_module.reset_db_state()


def _rec(home, away, hg, ag, days_ago=0, status="FT"):
    return {"home_id": home, "away_id": away, "home_goals": hg, "away_goals": ag,
            "status": status, "start_time": datetime(2024, 1, 1) + timedelta(days=days_ago)}


def _strong_history(home=101, away=202):
    """主队强势、客队弱势的历史（Ascending）。"""
    rows = [_rec(home, away, 3, 0, i) for i in range(8)]
    rows += [_rec(home, 999, 2, 1, 100 + i) for i in range(6)]
    rows += [_rec(888, away, 3, 0, 200 + i) for i in range(6)]
    return sorted(rows, key=lambda r: r["start_time"])


# ---------- 二分类市场 ----------

def test_binary_markets_from_score_matrix():
    """二分类必须由比分矩阵求和得出，不是独立猜测。"""
    from app.markets import compute_binary_markets
    from app.predictor import predict_match

    result = predict_match(101, 202, _strong_history())
    assert result is not None
    mk = compute_binary_markets(result)
    assert mk is not None

    # 主队不败 = 主胜 + 平局
    assert abs(mk.home_double_chance - (result.home_prob + result.draw_prob)) < 1e-6
    # 主队不败 + 客队不败 ≥ 1（平局被两边共享）
    assert mk.home_double_chance + mk.away_double_chance >= 1.0 - 1e-6
    assert 0.0 <= mk.over_1_5 <= 1.0
    assert 0.0 <= mk.under_4_5 <= 1.0
    assert 0.0 <= mk.btts <= 1.0
    # 小于4.5 球应比 大于1.5 球更常见（足球比分集中在 1-3 球）
    assert mk.under_4_5 > 0.5


def test_binary_markets_none_without_matrix():
    from app.markets import compute_binary_markets, MarketSet
    from app.predictor import PredictionResult

    r = PredictionResult(home_prob=.5, draw_prob=.25, away_prob=.25,
                         expected_home_goals=1, expected_away_goals=1,
                         predicted_score="1-1", confidence="中",
                         data_completeness=1.0, evidence="")
    assert compute_binary_markets(r) is None


def test_market_set_best():
    from app.markets import MarketSet
    mk = MarketSet(home_double_chance=0.8, away_double_chance=0.4,
                   over_1_5=0.7, under_4_5=0.9, btts=0.5)
    assert mk.best() == ("under_4_5", 0.9)


# ---------- 结算规则 ----------

def test_settle_1x2():
    from app.markets import settle_1x2
    assert settle_1x2(2, 1) == "home_win"
    assert settle_1x2(1, 2) == "away_win"
    assert settle_1x2(1, 1) == "draw"


def test_settle_market_rules():
    from app.markets import settle_market

    assert settle_market("home_double_chance", 1, 1) is True    # 平局算不败
    assert settle_market("home_double_chance", 2, 1) is True
    assert settle_market("home_double_chance", 0, 1) is False
    assert settle_market("away_double_chance", 1, 1) is True
    assert settle_market("over_1_5", 1, 1) is True              # 2 球
    assert settle_market("over_1_5", 1, 0) is False             # 1 球
    assert settle_market("under_4_5", 2, 2) is True             # 4 球
    assert settle_market("under_4_5", 3, 2) is False            # 5 球
    assert settle_market("btts", 1, 1) is True
    assert settle_market("btts", 2, 0) is False


def test_double_chance_hit_is_not_home_win_hit():
    """铁律：主队不败命中 ≠ 主胜命中，二者分开结算。"""
    from app.markets import settle_prediction_row

    # 比分 1-1：主胜 prediction 未命中；主队不败 binary 命中
    assert settle_prediction_row("full_1x2", "home_win", 1, 1) is False
    assert settle_prediction_row("binary", "home_double_chance", 1, 1) is True
    # 比分 2-1：两者都命中
    assert settle_prediction_row("full_1x2", "home_win", 2, 1) is True
    assert settle_prediction_row("binary", "home_double_chance", 2, 1) is True


def test_unknown_market_raises():
    from app.markets import settle_market
    with pytest.raises(ValueError):
        settle_market("no_such_market", 1, 1)


# ---------- 模型一致性 ----------

def test_consistency_counts_three_signals():
    from app.markets import evaluate_consistency
    from app.predictor import predict_match

    history = _strong_history()
    result = predict_match(101, 202, history)
    c = evaluate_consistency(result, history)

    assert len(c.signals) == 3, "必须是三路不同方法"
    assert set(c.signals) == {"Elo", "Poisson", "攻防强度"}
    assert c.agree >= 1
    assert c.ratio_text.endswith("/3")


def test_selection_tier_requires_enough_samples():
    from app.markets import evaluate_consistency, selection_tier
    from app.predictor import predict_match

    # 样本很少（每队仅 2 场 → 合计 4 < 6）→ 应判为数据不足（tier 为空）
    tiny = [_rec(101, 202, 3, 0, i) for i in range(2)]
    r = predict_match(101, 202, tiny, min_history=1)
    assert r is not None
    tier, reasons = selection_tier(r, evaluate_consistency(r, tiny))
    assert tier == "", "样本不足必须归类为数据不足，不能进精选"
    assert reasons


# ---------- 分开统计 ----------

def _seed_settled(session, n_selected_correct=8, n_selected_wrong=2,
                  n_full_correct=5, n_full_wrong=5):
    from app.db import Fixture, Prediction
    from app.tracking import save_prediction, settle_finished

    def mk(i, hg, ag):
        f = Fixture(external_id=1000 + i, league="EPL", league_id=39,
                    start_time=datetime(2024, 1, 1) + timedelta(days=i),
                    home_team_id=101 + i, away_team_id=202 + i,
                    home="H", away="A", status="FT",
                    home_score=hg, away_score=ag)
        session.add(f)
        session.flush()
        return f

    # 精选：8 中 2 错 → 80%
    for i in range(n_selected_correct):
        f = mk(i, 2, 1)
        save_prediction(session, fixture_id=f.id, prediction_type="selected_1x2",
                        prediction_market="home_win", probability=0.7,
                        model_version="v2", tier="A")
    for i in range(n_selected_wrong):
        f = mk(100 + i, 0, 1)
        save_prediction(session, fixture_id=f.id, prediction_type="selected_1x2",
                        prediction_market="home_win", probability=0.7,
                        model_version="v2", tier="A")
    # 全量：5 中 5 错 → 50%
    for i in range(n_full_correct):
        f = mk(200 + i, 2, 1)
        save_prediction(session, fixture_id=f.id, prediction_type="full_1x2",
                        prediction_market="home_win", probability=0.55,
                        model_version="v2")
    for i in range(n_full_wrong):
        f = mk(300 + i, 0, 1)
        save_prediction(session, fixture_id=f.id, prediction_type="full_1x2",
                        prediction_market="home_win", probability=0.55,
                        model_version="v2")
    session.commit()
    settle_finished(session)


def test_selected_and_full_are_counted_separately():
    """核心：精选 80% 与全量 50% 必须分别统计，绝不互相污染。"""
    from app.db import get_session, init_db
    from app.tracking import stats_by_type
    init_db()

    session = get_session()
    try:
        _seed_settled(session)
        sel = stats_by_type("selected_1x2", session=session)
        full = stats_by_type("full_1x2", session=session)

        assert sel["settled"] == 10
        assert sel["hits"] == 8
        assert sel["accuracy"] == 80.0

        assert full["settled"] == 10
        assert full["hits"] == 5
        assert full["accuracy"] == 50.0

        # 两个口径的准确率必须不同 —— 证明没有被混算
        assert sel["accuracy"] != full["accuracy"]
    finally:
        session.close()


def test_coverage_is_reported_alongside_accuracy():
    """只看准确率会误判，覆盖率必须同时展示。"""
    from app.db import get_session, init_db
    from app.tracking import stats_by_type
    init_db()

    session = get_session()
    try:
        _seed_settled(session)
        sel = stats_by_type("selected_1x2", session=session)
        assert "coverage" in sel
        assert 0.0 <= sel["coverage"] <= 100.0
    finally:
        session.close()


def test_binary_stats_per_market():
    """每个二分类市场单独统计，不合并。"""
    from app.db import Fixture, get_session, init_db
    from app.tracking import save_prediction, settle_finished, stats_by_market
    init_db()

    session = get_session()
    try:
        for i, (hg, ag, market) in enumerate([
            (2, 0, "btts"),        # 无双方进球 → 错
            (1, 1, "btts"),        # 双方进球 → 中
            (3, 1, "over_1_5"),    # 中
        ]):
            f = Fixture(external_id=2000 + i, league="EPL", league_id=39,
                        start_time=datetime(2024, 2, 1 + i),
                        home_team_id=1 + i, away_team_id=2 + i,
                        home="H", away="A", status="FT",
                        home_score=hg, away_score=ag)
            session.add(f)
            session.flush()
            save_prediction(session, fixture_id=f.id, prediction_type="binary",
                            prediction_market=market, probability=0.75,
                            model_version="v2")
        session.commit()
        settle_finished(session)

        rows = {r["market"]: r for r in stats_by_market(session=session)}
        assert rows["btts"]["settled"] == 2
        assert rows["btts"]["hits"] == 1
        assert rows["btts"]["accuracy"] == 50.0
        assert rows["over_1_5"]["settled"] == 1
        assert rows["over_1_5"]["accuracy"] == 100.0
        # 其他市场没有样本，应为 0 而不是继承别人的数据
        assert rows["under_4_5"]["settled"] == 0
    finally:
        session.close()


def test_settle_marks_result_and_correctness():
    from app.db import Fixture, Prediction, get_session, init_db
    from app.tracking import save_prediction, settle_finished
    init_db()

    session = get_session()
    try:
        f = Fixture(external_id=3000, league="EPL", league_id=39,
                    start_time=datetime(2024, 3, 1), home_team_id=1, away_team_id=2,
                    home="H", away="A", status="NS", home_score=None, away_score=None)
        session.add(f)
        session.flush()
        save_prediction(session, fixture_id=f.id, prediction_type="full_1x2",
                        prediction_market="home_win", probability=0.6, model_version="v2")
        session.commit()

        # 未结束 → 不结算
        assert settle_finished(session) == 0

        f.status = "FT"
        f.home_score, f.away_score = 2, 1
        session.commit()
        assert settle_finished(session) == 1

        p = session.query(Prediction).filter(Prediction.fixture_id == f.id).one()
        assert p.settled is True
        assert p.prediction_result == "home_win"
        assert p.is_correct is True
    finally:
        session.close()


def test_streak_tracking():
    from app.db import get_session, init_db
    from app.tracking import stats_by_type
    init_db()

    session = get_session()
    try:
        _seed_settled(session)
        sel = stats_by_type("selected_1x2", session=session)
        assert isinstance(sel["streak"], int)
    finally:
        session.close()


def test_overall_summary_contains_all_types():
    from app.db import get_session, init_db
    from app.tracking import format_summary, overall_summary
    init_db()

    session = get_session()
    try:
        _seed_settled(session)
        data = overall_summary(session=session)
        assert set(data) == {"full", "selected", "binary_markets"}
        text = format_summary(data)
        assert "全量胜平负" in text
        assert "精选胜平负" in text
        assert "覆盖率" in text, "必须同时展示覆盖率"
    finally:
        session.close()

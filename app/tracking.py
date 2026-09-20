"""按口径分离的统计与结算。

三条铁律
--------
1. **不同 prediction_type 绝不混算**——精选的命中率不能代表全量能力。
2. **覆盖率必须同时展示**——只看准确率会让人误判（精选 68% 但覆盖 22%）。
3. **二分类独立于胜平负**——「主队不败命中」≠「主胜命中」，分开记录。

结算时机：比赛状态变为已结束（FT/AET/PEN）且已有比分时。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import Fixture, Prediction, get_session
from app.markets import (
    MARKETS, OUTCOME_LABELS, settle_1x2, settle_prediction_row,
)

logger = logging.getLogger(__name__)

TYPE_LABELS = {
    "full_1x2": "全量胜平负",
    "selected_1x2": "精选胜平负",
    "binary": "二分类市场",
}

_FINISHED = {"FT", "AET", "PEN"}


def _is_finished(status) -> bool:
    return str(status or "").strip().upper() in _FINISHED


# --------------------------------------------------------------------------- #
# 保存预测
# --------------------------------------------------------------------------- #

def save_prediction(session: Session, *, fixture_id: int, prediction_type: str,
                    prediction_market: str, probability: float, model_version: str,
                    tier: str = "", consistency: str = "",
                    home_prob: float = 0.0, draw_prob: float = 0.0,
                    away_prob: float = 0.0, exp_home: float = 0.0,
                    exp_away: float = 0.0, score: str = "", confidence: str = "",
                    completeness: float = 0.0, evidence: str = "",
                    cutoff: Optional[datetime] = None) -> Optional[Prediction]:
    """写入一条预测；同 (fixture, model, type, market) 已存在则更新。"""
    row = (
        session.query(Prediction)
        .filter(Prediction.fixture_id == fixture_id,
                Prediction.model_version == model_version,
                Prediction.prediction_type == prediction_type,
                Prediction.prediction_market == prediction_market)
        .one_or_none()
    )
    if row is None:
        row = Prediction(fixture_id=fixture_id, model_version=model_version,
                         prediction_type=prediction_type,
                         prediction_market=prediction_market)
        session.add(row)

    row.prediction_probability = float(probability)
    row.tier = tier or ""
    row.consistency = consistency or ""
    row.home_prob = float(home_prob)
    row.draw_prob = float(draw_prob)
    row.away_prob = float(away_prob)
    row.expected_home_goals = float(exp_home)
    row.expected_away_goals = float(exp_away)
    row.predicted_score = score or ""
    row.confidence = confidence or ""
    row.data_completeness = float(completeness)
    row.evidence = evidence or ""
    row.feature_cutoff_at = cutoff
    # 重新预测 → 视为未结算，等待新结果
    row.settled = False
    row.prediction_result = ""
    row.is_correct = None
    return row


# --------------------------------------------------------------------------- #
# 赛后结算
# --------------------------------------------------------------------------- #

def settle_finished(session: Session, limit: int = 500) -> int:
    """扫描已结束且有比分的比赛，结算其未结算预测。返回结算条数。"""
    rows = (
        session.query(Fixture)
        .filter(Fixture.home_score.isnot(None), Fixture.away_score.isnot(None))
        .order_by(Fixture.start_time.desc())
        .limit(limit)
        .all()
    )
    done = 0
    for f in rows:
        if not _is_finished(f.status):
            continue
        preds = (
            session.query(Prediction)
            .filter(Prediction.fixture_id == f.id, Prediction.settled.is_(False))
            .all()
        )
        for p in preds:
            try:
                correct = settle_prediction_row(
                    p.prediction_type, p.prediction_market,
                    f.home_score, f.away_score,
                )
            except ValueError:
                logger.warning("未知市场 %s，跳过结算", p.prediction_market)
                continue
            p.prediction_result = settle_1x2(f.home_score, f.away_score)
            p.is_correct = bool(correct)
            p.settled = True
            p.settled_at = datetime.utcnow()
            done += 1
    if done:
        session.commit()
    return done


def settle_all() -> int:
    """独立 Session 的便捷入口（供定时任务 / 命令调用）。"""
    session = get_session()
    try:
        return settle_finished(session)
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #

def _rate(hits: int, total: int) -> float:
    return (hits / total * 100.0) if total else 0.0


def stats_by_type(prediction_type: str, session: Optional[Session] = None,
                  market: Optional[str] = None) -> dict:
    """某个口径的汇总统计：命中 / 总数 / 准确率 / 覆盖率。"""
    own = session is None
    if own:
        session = get_session()
    try:
        q = session.query(Prediction).filter(Prediction.prediction_type == prediction_type)
        if market:
            q = q.filter(Prediction.prediction_market == market)
        total = q.count()
        settled_q = q.filter(Prediction.settled.is_(True))
        settled = settled_q.count()
        hits = settled_q.filter(Prediction.is_correct.is_(True)).count()

        # 覆盖率 = 该口径预测场次数 / 已结束比赛场次数
        finished = session.query(func.count(Fixture.id)).filter(
            Fixture.home_score.isnot(None), Fixture.away_score.isnot(None),
        ).scalar() or 0
        # 注意：精选口径的分母应为「被评估的场次」，用已结束场次近似
        coverage = (settled / finished * 100.0) if finished else 0.0

        # 连续命中 / 连续错误（按结算时间倒序）
        recent = (
            settled_q.filter(Prediction.is_correct.isnot(None))
            .order_by(Prediction.settled_at.desc()).limit(50).all()
        )
        streak = 0
        for r in recent:
            if r.is_correct:
                if streak < 0:
                    break
                streak += 1
            else:
                if streak > 0:
                    break
                streak -= 1

        return {
            "type": prediction_type,
            "label": TYPE_LABELS.get(prediction_type, prediction_type),
            "market": market or "",
            "market_label": MARKETS.get(market, "") if market else "",
            "total": total,
            "settled": settled,
            "hits": hits,
            "accuracy": round(_rate(hits, settled), 2),
            "coverage": round(coverage, 2),
            "streak": streak,
        }
    finally:
        if own:
            session.close()


def stats_by_market(session: Optional[Session] = None) -> list[dict]:
    """各二分类市场分别统计（不合并）。"""
    out = []
    for key, label in MARKETS.items():
        s = stats_by_type("binary", session=session, market=key)
        s["market"] = key
        s["market_label"] = label
        out.append(s)
    return out


def stats_by_league(prediction_type: str = "selected_1x2",
                    session: Optional[Session] = None) -> list[dict]:
    """按联赛统计某口径的准确率。"""
    own = session is None
    if own:
        session = get_session()
    try:
        rows = (
            session.query(Fixture.league, Prediction.is_correct)
            .join(Prediction, Prediction.fixture_id == Fixture.id)
            .filter(Prediction.prediction_type == prediction_type,
                    Prediction.settled.is_(True))
            .all()
        )
        agg: dict[str, list] = defaultdict(list)
        for league, correct in rows:
            agg[league or "未知"].append(bool(correct))
        out = [
            {"league": lg, "settled": len(v), "hits": sum(v),
             "accuracy": round(_rate(sum(v), len(v)), 2)}
            for lg, v in agg.items()
        ]
        return sorted(out, key=lambda x: -x["settled"])
    finally:
        if own:
            session.close()


def overall_summary(session: Optional[Session] = None) -> dict:
    """一次性给出全部口径的概览。"""
    return {
        "full": stats_by_type("full_1x2", session=session),
        "selected": stats_by_type("selected_1x2", session=session),
        "binary_markets": stats_by_market(session=session),
    }


def format_summary(data: dict) -> str:
    """把概览格式化为可读文本（同时展示准确率与覆盖率）。"""

    def _line(s: dict, prefix: str = "") -> str:
        if not s.get("settled"):
            return f"{prefix}{s['label']}：暂无已结算样本"
        return (
            f"{prefix}{s['label']}：命中 {s['hits']}/{s['settled']} "
            f"准确率 {s['accuracy']}% 覆盖率 {s['coverage']}%"
        )

    lines = [_line(data["full"]), _line(data["selected"]), "", "二分类（分别统计）："]
    for m in data["binary_markets"]:
        if m.get("settled"):
            lines.append(
                f"· {m['market_label']}：{m['hits']}/{m['settled']} "
                f"准确率 {m['accuracy']}%"
            )
        else:
            lines.append(f"· {m['market_label']}：暂无已结算样本")
    return "\n".join(lines)


__all__ = [
    "save_prediction", "settle_finished", "settle_all",
    "stats_by_type", "stats_by_market", "stats_by_league",
    "overall_summary", "format_summary", "TYPE_LABELS", "OUTCOME_LABELS",
]

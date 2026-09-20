"""Poisson 基线预测模型（纯函数，无 DB / 无网络 / 无 Telegram 依赖）。

核心约定
--------
* 所有「球队身份」统一使用 **API-Football 的真实球队 ID**（整数），
  绝不混用数据库主键 (``Fixture.id``) 或球队名称字符串。
* ``history`` 每条记录统一为::

      {"home_id": int, "away_id": int, "home_goals": int, "away_goals": int, "status": str}

* 只统计「已结束」的比赛。NS / TBD / CANC / PST / 进行中一律排除，
  杜绝把未来结果当成历史。
* 主队只统计其**作为主队**的历史，客队只统计其**作为客队**的历史。
* 任何一队有效样本 < ``min_history``（默认 5）直接返回 ``None``，
  **绝不**用虚构默认值绕过样本不足。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

MODEL_VERSION = "baseline-poisson-v1"
MAX_GOALS = 6  # 比分概率矩阵覆盖范围 0..6

# 联赛平均每队每场进球（弱先验；样本足够时被数据主导）
_LEAGUE_AVG = 1.4

# 视为「已结束」的状态（不区分大小写匹配）
_FINISHED_STATUSES = {"FT", "AET", "PEN", "FINISHED", "MATCH FINISHED"}


def _is_finished(status) -> bool:
    """判断一场比赛是否已结束。None / 空串 / NS / 进行中均视为未结束。"""
    if status is None:
        return False
    s = str(status).strip().upper()
    return s in {x.upper() for x in _FINISHED_STATUSES}


@dataclass
class PredictionResult:
    """单场比赛预测结果。概率均在 [0, 1]，三者之和 ≈ 1。"""

    home_prob: float
    draw_prob: float
    away_prob: float
    expected_home_goals: float
    expected_away_goals: float
    predicted_score: str
    confidence: str
    data_completeness: float
    evidence: str
    model_version: str = MODEL_VERSION
    home_team_id: Optional[int] = None
    away_team_id: Optional[int] = None


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #

def _poisson(lam: float, k: int) -> float:
    """Poisson 概率质量函数 P(X=k)。"""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _validate_record(rec: dict) -> Optional[tuple[int, int, int, int]]:
    """提取 (home_id, away_id, home_goals, away_goals)；进球数非法返回 None。"""
    try:
        home_id = int(rec.get("home_id"))
        away_id = int(rec.get("away_id"))
        hg = int(rec.get("home_goals"))
        ag = int(rec.get("away_goals"))
    except (TypeError, ValueError):
        return None
    if hg < 0 or ag < 0:
        return None
    return home_id, away_id, hg, ag


def _team_home_stats(history: list[dict], team_id: int) -> tuple[float, float, int]:
    """某队「作为主队」时的 (进球和, 失球和, 场次数)。"""
    gf = ga = n = 0
    for rec in history or []:
        v = _validate_record(rec)
        if v is None:
            continue
        home_id, _away_id, hg, ag = v
        if home_id != team_id:
            continue
        if not _is_finished(rec.get("status")):
            continue
        gf += hg
        ga += ag
        n += 1
    return float(gf), float(ga), n


def _team_away_stats(history: list[dict], team_id: int) -> tuple[float, float, int]:
    """某队「作为客队」时的 (进球和, 失球和, 场次数)。"""
    gf = ga = n = 0
    for rec in history or []:
        v = _validate_record(rec)
        if v is None:
            continue
        _home_id, away_id, hg, ag = v
        if away_id != team_id:
            continue
        if not _is_finished(rec.get("status")):
            continue
        gf += ag   # 客队视角进球 = away_goals
        ga += hg   # 客队视角失球 = home_goals
        n += 1
    return float(gf), float(ga), n


def _confidence(home_prob: float, draw_prob: float, away_prob: float,
                total_games: int) -> str:
    """由「最大概率 − 次大概率」的差值 + 样本量决定 低/中/高。"""
    ordered = sorted([home_prob, draw_prob, away_prob], reverse=True)
    gap = ordered[0] - ordered[1]
    if gap >= 0.15 and total_games >= 15:
        return "高"
    if gap >= 0.08 and total_games >= 8:
        return "中"
    return "低"


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def predict_match(
    home_team_id: int,
    away_team_id: int,
    history: list[dict],
    min_history: Optional[int] = None,
) -> Optional[PredictionResult]:
    """为 home_team_id(主) vs away_team_id(客) 生成 Poisson 预测。

    Parameters
    ----------
    home_team_id, away_team_id
        API-Football 真实球队 ID。**绝不可传入 Fixture.id**。
    history
        比赛记录列表（含未结束场次，函数内部自行过滤）。
    min_history
        每队所需的最少有效历史场次，缺省取 ``MIN_HISTORY_MATCHES``（默认 5）。
    """
    if not isinstance(history, list):
        return None
    if not home_team_id or not away_team_id:
        return None  # 球队 ID 缺失 → 拒绝预测，绝不用 fixture.id 顶替

    if min_history is None:
        try:
            from app.config import get_settings
            min_history = int(get_settings().MIN_HISTORY_MATCHES)
        except Exception:
            min_history = 5
    min_history = max(1, int(min_history))

    # 主队只取主场历史，客队只取客场历史
    h_gf, h_ga, h_n = _team_home_stats(history, home_team_id)
    a_gf, a_ga, a_n = _team_away_stats(history, away_team_id)

    # 样本不足 → 拒绝预测（不使用任何虚构默认数据）
    if h_n < min_history or a_n < min_history:
        return None

    avg = _LEAGUE_AVG

    home_attack = (h_gf / h_n) / avg     # 主队主场进攻
    away_defense = (a_ga / a_n) / avg    # 客队客场防守（失球越多越弱）
    away_attack = (a_gf / a_n) / avg     # 客队客场进攻
    home_defense = (h_ga / h_n) / avg    # 主队主场防守

    lambda_home = max(0.2, home_attack * away_defense * avg)
    lambda_away = max(0.2, away_attack * home_defense * avg)

    # 比分概率矩阵 P(home=i, away=j)，i/j ∈ [0, MAX_GOALS]
    matrix = [[_poisson(lambda_home, i) * _poisson(lambda_away, j)
               for j in range(MAX_GOALS + 1)]
              for i in range(MAX_GOALS + 1)]

    home_prob = sum(matrix[i][j] for i in range(MAX_GOALS + 1)
                    for j in range(MAX_GOALS + 1) if i > j)
    away_prob = sum(matrix[i][j] for i in range(MAX_GOALS + 1)
                    for j in range(MAX_GOALS + 1) if i < j)
    draw_prob = sum(matrix[i][j] for i in range(MAX_GOALS + 1)
                    for j in range(MAX_GOALS + 1) if i == j)

    # 归一化，消除 0..6 截断造成的尾差，保证三者之和严格 = 1
    total = home_prob + draw_prob + away_prob
    if total <= 0:
        return None
    home_prob /= total
    draw_prob /= total
    away_prob /= total

    # 最可能比分
    best_i = best_j = 0
    best_p = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            if matrix[i][j] > best_p:
                best_p, best_i, best_j = matrix[i][j], i, j

    total_games = h_n + a_n
    completeness = min(1.0, total_games / max(1, min_history * 2))
    confidence = _confidence(home_prob, draw_prob, away_prob, total_games)

    evidence = (
        f"主队近 {h_n} 个主场 进{h_gf:.0f} 失{h_ga:.0f}；"
        f"客队近 {a_n} 个客场 进{a_gf:.0f} 失{a_ga:.0f}；"
        f"期望进球 {lambda_home:.2f} - {lambda_away:.2f}。"
    )

    return PredictionResult(
        home_prob=home_prob,
        draw_prob=draw_prob,
        away_prob=away_prob,
        expected_home_goals=round(lambda_home, 2),
        expected_away_goals=round(lambda_away, 2),
        predicted_score=f"{best_i}-{best_j}",
        confidence=confidence,
        data_completeness=round(completeness, 2),
        evidence=evidence,
        model_version=MODEL_VERSION,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
    )


def format_prediction(result: PredictionResult) -> str:
    """把预测结果格式化为面向用户的多行文本。"""
    if not isinstance(result, PredictionResult):
        return "暂无可用预测。"
    return (
        f"主胜 {result.home_prob * 100:.1f}% / 平 {result.draw_prob * 100:.1f}% / "
        f"客胜 {result.away_prob * 100:.1f}%\n"
        f"预期进球 {result.expected_home_goals:.2f} - {result.expected_away_goals:.2f}\n"
        f"最可能比分 {result.predicted_score} | 置信度 {result.confidence}\n"
        f"数据完整度 {int(result.data_completeness * 100)}%\n"
        f"模型版本 {result.model_version}\n"
        f"预测依据：{result.evidence}"
    )


__all__ = [
    "MODEL_VERSION", "PredictionResult", "predict_match", "format_prediction",
    "_is_finished",
]

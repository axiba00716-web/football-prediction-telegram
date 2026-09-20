from dataclasses import dataclass
from typing import Optional
import math

from app.config import get_settings

MODEL_VERSION = "baseline-poisson-v1"

# 联赛平均进球基线（数据不足时的回退值）
LEAGUE_AVG_GOALS = 1.4


@dataclass
class PredictionResult:
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


def _poisson(l: float, k: int) -> float:
    if l <= 0:
        return 0.0
    return math.exp(-l) * (l ** k) / math.factorial(k)


def _max_goals() -> int:
    return 6  # 计算 0..6 球范围


def predict_match(
    home_team_id: int,
    away_team_id: int,
    history: list[dict],
    min_history: Optional[int] = None,
) -> Optional[PredictionResult]:
    """
    基于历史已结束比赛做 Poisson 预测。
    history: [{"home_id": int, "away_id": int, "home_goals": int, "away_goals": int, "venue": "home"/"away"}, ...]
    仅使用已结束比赛；数据不足返回 None（拒绝预测）。
    """
    if min_history is None:
        min_history = get_settings().MIN_HISTORY_MATCHES

    home_attack, home_defense, away_attack, away_defense = [], [], [], []
    for m in history:
        if m.get("status") not in ("FT", "finished", "Match Finished"):
            continue  # 防止数据泄漏：只用已结束比赛
        if m["home_id"] == home_team_id:
            home_attack.append(m["home_goals"])
            away_defense.append(m["home_goals"])  # 客队防守 = 对手在主场进球
        elif m["away_id"] == home_team_id:
            home_attack.append(m["away_goals"])
            away_defense.append(m["away_goals"])
        if m["away_id"] == away_team_id:
            away_attack.append(m["away_goals"])
            home_defense.append(m["away_goals"])
        elif m["home_id"] == away_team_id:
            away_attack.append(m["home_goals"])
            home_defense.append(m["home_goals"])

    n_home = len(home_attack)
    n_away = len(away_attack)

    # 数据完整度：以所需样本为基准，封顶 1.0
    data_completeness = min(1.0, (n_home + n_away) / (2 * min_history))

    if n_home < min_history or n_away < min_history:
        return None  # 历史样本不足，拒绝预测

    avg_home_attack = sum(home_attack) / n_home
    avg_away_attack = sum(away_attack) / n_away
    avg_home_defense = sum(home_defense) / max(1, len(home_defense))
    avg_away_defense = sum(away_defense) / max(1, len(away_defense))

    # 攻击力/防守力相对联赛均值
    home_attack_strength = avg_home_attack / LEAGUE_AVG_GOALS
    away_attack_strength = avg_away_attack / LEAGUE_AVG_GOALS
    home_defense_strength = avg_home_defense / LEAGUE_AVG_GOALS
    away_defense_strength = avg_away_defense / LEAGUE_AVG_GOALS

    expected_home = home_attack_strength * away_defense_strength * LEAGUE_AVG_GOALS
    expected_away = away_attack_strength * home_defense_strength * LEAGUE_AVG_GOALS

    # 比分矩阵概率
    home_win, draw, away_win = 0.0, 0.0, 0.0
    best_score, best_prob = "", 0.0
    g = _max_goals()
    for i in range(g + 1):
        for j in range(g + 1):
            p = _poisson(expected_home, i) * _poisson(expected_away, j)
            if p <= 0:
                continue
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p
            if p > best_prob:
                best_prob, best_score = p, f"{i}-{j}"

    # 归一化（截断到 6 球会损失少量概率，重新归一保证和为 1）
    total = home_win + draw + away_win
    if total <= 0:
        return None
    home_win /= total
    draw /= total
    away_win /= total

    # 置信度：由最大概率方优势决定
    probs = sorted([home_win, draw, away_win], reverse=True)
    spread = probs[0] - probs[1]
    if spread > 0.15:
        confidence = "高"
    elif spread > 0.07:
        confidence = "中"
    else:
        confidence = "低"

    evidence = (
        f"模型 {MODEL_VERSION}：主队近 {n_home} 场场均进球 {avg_home_attack:.2f}，"
        f"客队近 {n_away} 场场均进球 {avg_away_attack:.2f}；"
        f"预期进球 {expected_home:.2f} vs {expected_away:.2f}，最可能比分 {best_score}。"
    )

    return PredictionResult(
        home_prob=round(home_win, 4),
        draw_prob=round(draw, 4),
        away_prob=round(away_win, 4),
        expected_home_goals=round(expected_home, 2),
        expected_away_goals=round(expected_away, 2),
        predicted_score=best_score,
        confidence=confidence,
        data_completeness=round(data_completeness, 2),
        evidence=evidence,
    )


def format_prediction(p: PredictionResult) -> str:
    return (
        f"⚽ 预测（{p.model_version}）\n"
        f"主胜 {p.home_prob*100:.1f}% / 平 {p.draw_prob*100:.1f}% / 客胜 {p.away_prob*100:.1f}%\n"
        f"预期进球 {p.expected_home_goals:.2f} - {p.expected_away_goals:.2f}\n"
        f"最可能比分 {p.predicted_score}｜置信度 {p.confidence}｜数据完整度 {p.data_completeness*100:.0f}%"
    )

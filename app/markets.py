"""市场与精选层：二分类市场、模型一致性投票、分级筛选。

设计原则
--------
**三种口径必须分开统计**，绝不把「精选命中」算成「全量命中」：

* ``full_1x2``      —— 全量胜平负（每场都预测，覆盖率 100%）
* ``selected_1x2``  —— 精选胜平负（只推送高置信场次，覆盖率通常 < 30%）
* ``binary``        —— 二分类市场（主队不败 / 大于1.5球 / 双方进球 …）

二分类概率**由比分矩阵精确求和**得到，不是独立猜测：

* 主队不败 = 1 − 客胜概率
* 大于 1.5 球 = 1 − P(0 球) − P(1 球)
* 小于 4.5 球 = P(总进球 ≤ 4)
* 双方进球 = P(主队 > 0 且 客队 > 0)

「模型一致性」是**三种不同方法**的投票，不是同一个模型算三遍：

1. Elo 分差（logistic）
2. Dixon-Coles 泊松矩阵（主模型）
3. 攻防强度泊松（进攻强度 × 防守强度）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.predictor import MAX_GOALS, PredictionResult, attack_defense_probs, elo_signal_probs

# ---- 二分类市场定义 ----
MARKETS: dict[str, str] = {
    "home_double_chance": "主队不败",
    "away_double_chance": "客队不败",
    "over_1_5": "大于 1.5 球",
    "under_4_5": "小于 4.5 球",
    "btts": "双方进球",
}

# ---- 精选阈值 ----
TIER_A_PROB = 0.60           # A 级：最高概率门槛
TIER_A_GAP = 0.18            # A 级：与次大概率的最小差值
TIER_A_CONSISTENCY = 3       # A 级：三路信号必须全票一致
TIER_A_MIN_GAMES = 12        # A 级：最小历史样本

TIER_B_PROB = 0.50
TIER_B_GAP = 0.10
TIER_B_CONSISTENCY = 2
TIER_B_MIN_GAMES = 6

# 二分类精选门槛（比胜平负宽松，因为二选一本身概率更高）
BINARY_RECOMMEND_PROB = 0.70
BINARY_MIN_GAMES = 6

OUTCOME_LABELS = {"home_win": "主胜", "draw": "平局", "away_win": "客胜"}
OUTCOME_KEYS = ("home_win", "draw", "away_win")


@dataclass
class MarketSet:
    """一场比赛的二分类市场概率。"""

    home_double_chance: float = 0.0
    away_double_chance: float = 0.0
    over_1_5: float = 0.0
    under_4_5: float = 0.0
    btts: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "home_double_chance": self.home_double_chance,
            "away_double_chance": self.away_double_chance,
            "over_1_5": self.over_1_5,
            "under_4_5": self.under_4_5,
            "btts": self.btts,
        }

    def best(self) -> tuple[str, float]:
        d = self.as_dict()
        key = max(d, key=d.get)
        return key, d[key]


def compute_binary_markets(result: PredictionResult) -> Optional[MarketSet]:
    """从比分矩阵精确计算二分类市场概率。矩阵缺失时返回 None。"""
    matrix = getattr(result, "score_matrix", None)
    if not matrix:
        return None

    total_goals_prob: dict[int, float] = {}
    home_scores = 0.0
    away_scores = 0.0
    both_score = 0.0

    for i in range(min(len(matrix), MAX_GOALS + 1)):
        row = matrix[i]
        for j in range(min(len(row), MAX_GOALS + 1)):
            p = row[j]
            if p <= 0:
                continue
            total_goals_prob[i + j] = total_goals_prob.get(i + j, 0.0) + p
            if i > 0:
                home_scores += p
            if j > 0:
                away_scores += p
            if i > 0 and j > 0:
                both_score += p

    # 归一化（消除 0..6 截断的尾差）
    norm = sum(total_goals_prob.values())
    if norm <= 0:
        return None
    for k in total_goals_prob:
        total_goals_prob[k] /= norm
    home_scores /= norm
    away_scores /= norm
    both_score /= norm

    p0 = total_goals_prob.get(0, 0.0)
    p1 = total_goals_prob.get(1, 0.0)
    under_4_5 = sum(v for k, v in total_goals_prob.items() if k <= 4)

    return MarketSet(
        # 平局也算「主队不败」
        home_double_chance=min(1.0, result.home_prob + result.draw_prob),
        away_double_chance=min(1.0, result.away_prob + result.draw_prob),
        over_1_5=max(0.0, min(1.0, 1.0 - p0 - p1)),
        under_4_5=max(0.0, min(1.0, under_4_5)),
        btts=max(0.0, min(1.0, both_score)),
    )


@dataclass
class Consistency:
    """三路信号的投票结果。"""

    votes: dict[str, int] = field(default_factory=dict)   # outcome -> 得票数
    signals: dict[str, str] = field(default_factory=dict)  # 信号名 -> 它选中的结果
    agree: int = 1                                        # 领先结果获得的最高票数
    top: str = "draw"                                     # 领先结果

    @property
    def ratio_text(self) -> str:
        return f"{self.agree}/{len(self.signals)}"


def evaluate_consistency(result: PredictionResult, history: list[dict]) -> Consistency:
    """用三路不同方法投票，返回一致性。样本不足时该信号不参与投票。"""
    signals: dict[str, str] = {}

    def _pick(probs) -> str:
        return OUTCOME_KEYS[max(range(3), key=lambda i: probs[i])]

    # 1) Elo
    try:
        eh, ed, ea = elo_signal_probs(result.elo_diff)
        signals["Elo"] = _pick((eh, ed, ea))
    except Exception:
        pass

    # 2) 主模型（Dixon-Coles）
    signals["Poisson"] = _pick((result.home_prob, result.draw_prob, result.away_prob))

    # 3) 攻防强度
    try:
        ad = attack_defense_probs(history, result.home_team_id, result.away_team_id)
        if ad:
            signals["攻防强度"] = _pick(ad)
    except Exception:
        pass

    votes = {k: 0 for k in OUTCOME_KEYS}
    for pick in signals.values():
        votes[pick] += 1

    top = max(votes, key=votes.get)
    return Consistency(votes=votes, signals=signals, agree=votes[top], top=top)


def selection_tier(result: PredictionResult, consistency: Consistency) -> tuple[str, list[str]]:
    """判定精选等级：``A`` / ``B`` / ````（空串 = 数据不足，不推送）。"""
    probs = {"home_win": result.home_prob, "draw": result.draw_prob,
             "away_win": result.away_prob}
    ordered = sorted(probs.values(), reverse=True)
    top_prob, gap = ordered[0], ordered[0] - ordered[1]
    games = getattr(result, "sample_games", 0) or 0

    # 硬性前置：样本不足直接归类为「数据不足」
    if games < TIER_B_MIN_GAMES:
        return "", [f"历史样本仅 {games} 场（需 ≥ {TIER_B_MIN_GAMES}）"]

    reasons: list[str] = []
    if top_prob < TIER_B_PROB:
        reasons.append(f"最高概率 {top_prob * 100:.0f}% < {TIER_B_PROB * 100:.0f}%")
    if gap < TIER_B_GAP:
        reasons.append(f"概率差 {gap * 100:.0f}% < {TIER_B_GAP * 100:.0f}%")
    if consistency.agree < TIER_B_CONSISTENCY:
        reasons.append(f"模型一致性 {consistency.ratio_text} < {TIER_B_CONSISTENCY}/3")
    if reasons:
        return "C", reasons  # C = 普通（有预测但不建议）

    if (top_prob >= TIER_A_PROB and gap >= TIER_A_GAP
            and consistency.agree >= TIER_A_CONSISTENCY
            and games >= TIER_A_MIN_GAMES):
        return "A", []

    return "B", []


def recommended_binary(markets: MarketSet, sample_games: int) -> Optional[tuple[str, float]]:
    """二分类精选：概率达门槛且样本充足才推荐。"""
    if sample_games < BINARY_MIN_GAMES:
        return None
    key, prob = markets.best()
    if prob < BINARY_RECOMMEND_PROB:
        return None
    return key, prob


# --------------------------------------------------------------------------- #
# 赛后结算：把真实比分翻译成各市场是否命中
# --------------------------------------------------------------------------- #

def settle_1x2(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home_win"
    if home_goals < away_goals:
        return "away_win"
    return "draw"


def settle_market(market: str, home_goals: int, away_goals: int) -> bool:
    """判断某个二分类市场是否命中。"""
    hg, ag = int(home_goals), int(away_goals)
    total = hg + ag
    if market == "home_double_chance":
        return hg >= ag            # 主队不败（含平局）
    if market == "away_double_chance":
        return ag >= hg            # 客队不败（含平局）
    if market == "over_1_5":
        return total >= 2
    if market == "under_4_5":
        return total <= 4
    if market == "btts":
        return hg > 0 and ag > 0
    raise ValueError(f"未知市场: {market}")


def settle_prediction_row(prediction_type: str, market: str,
                          home_goals: int, away_goals: int) -> bool:
    """统一结算入口：按预测类型与实际比分判断是否命中。"""
    if prediction_type == "binary":
        return settle_market(market, home_goals, away_goals)
    # 胜平负（full_1x2 / selected_1x2）
    actual = settle_1x2(home_goals, away_goals)
    return actual == market


__all__ = [
    "MARKETS", "MarketSet", "Consistency",
    "compute_binary_markets", "evaluate_consistency", "selection_tier",
    "recommended_binary", "settle_1x2", "settle_market",
    "settle_prediction_row", "OUTCOME_LABELS",
]

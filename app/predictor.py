"""Elo + Dixon-Coles 预测模型（纯函数，无 DB / 无网络 / 无 Telegram 依赖）。

相对旧版 ``baseline-poisson-v1`` 的三处升级
------------------------------------------
1. **Elo 评分**替代「近 N 场平均进球」：逐场递推、对状态变化敏感，
   在样本很少时（免费 API 每天 100 次请求）比滑动平均稳定得多。
2. **Dixon-Coles 修正**替代纯 Poisson：修正 0-0、1-0、1-1 等低比分
   被系统性低估的经典问题。
3. **选择性出手**：把握不足的场次**仍然给出预测结果**，但明确标注
   「不建议参考」，而不是硬给一个看似确定的结论。

核心约定
--------
* 所有「球队身份」统一使用 **API-Football 的真实球队 ID**（整数），
  绝不混用数据库主键 (``Fixture.id``) 或球队名称字符串。
* ``history`` 每条记录统一为::
      {"home_id": int, "away_id": int, "home_goals": int,
       "away_goals": int, "status": str}
  且**按开赛时间升序**传入，Elo 依赖顺序递推。
* 只统计「已结束」的比赛（FT / AET / PEN），未来场次一律排除。
* 任何一队有效样本 < ``min_history`` 直接返回 ``None``，不使用虚构默认数据。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

MODEL_VERSION = "elo-dixoncoles-v2"
MAX_GOALS = 6  # 比分概率矩阵覆盖范围 0..6

# ---- Elo 参数（World Football Elo Ratings 常用取值）----
ELO_INITIAL = 1500.0
ELO_K = 20.0
ELO_HOME_ADVANTAGE = 65.0      # 主场优势折算的 Elo 分差
ELO_SCALE = 400.0              # Elo 分差 → 胜率的尺度

# ---- 进球强度参数 ----
BASE_GOALS = 1.30              # 势均力敌时双方各自的期望进球
GOAL_ELO_SCALE = 800.0         # Elo 分差 → 进球率的尺度

# ---- Dixon-Coles 低比分修正 ----
DC_RHO = -0.08                 # 负值：抬高 0-0 / 1-1，压低 0-1 / 1-0

# ---- 选择性出手门槛 ----
RECOMMEND_GAP = 0.15           # 最大概率与次大概率的最小差值
RECOMMEND_ELO_GAP = 100.0      # 双方 Elo 的最小分差
RECOMMEND_MIN_GAMES = 10       # 建议出手所需的最小总样本

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
    score_matrix: list = field(default_factory=list)  # [i][j] = 比分 i-j 的概率
    # 选择性出手：False 表示「有预测结果，但不建议参考」
    recommended: bool = True
    advice: str = ""
    elo_home: float = ELO_INITIAL
    elo_away: float = ELO_INITIAL
    elo_diff: float = 0.0
    top_two_gap: float = 0.0
    sample_games: int = 0
    reasons: list[str] = field(default_factory=list)


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
    """提取 (home_id, away_id, home_goals, away_goals)；非法返回 None。"""
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


def _goal_diff_multiplier(diff: int) -> float:
    """进球差放大系数（World Football Elo 标准）：赢球越多，Elo 变动越大。"""
    if diff <= 1:
        return 1.0
    if diff == 2:
        return 1.5
    return 1.75 + (diff - 3) / 8.0


def compute_elos(history: list[dict], initial: float = ELO_INITIAL,
                 k: float = ELO_K, home_advantage: float = ELO_HOME_ADVANTAGE) -> dict[int, float]:
    """按比赛顺序递推各队 Elo 评分。

    ``history`` 必须**按开赛时间升序**传入，否则递推方向错误。
    只统计已结束场次；未结束的记录直接跳过。
    """
    elos: dict[int, float] = {}
    for rec in history or []:
        v = _validate_record(rec)
        if v is None:
            continue
        home_id, away_id, hg, ag = v
        if not _is_finished(rec.get("status")):
            continue

        eh = elos.get(home_id, initial)
        ea = elos.get(away_id, initial)

        # 主队期望得分（含主场优势）
        exp_home = 1.0 / (1.0 + 10.0 ** (-(eh + home_advantage - ea) / ELO_SCALE))
        exp_away = 1.0 - exp_home

        if hg > ag:
            act_home, act_away, diff = 1.0, 0.0, hg - ag
        elif hg < ag:
            act_home, act_away, diff = 0.0, 1.0, ag - hg
        else:
            act_home, act_away, diff = 0.5, 0.5, 1

        mult = _goal_diff_multiplier(diff)
        elos[home_id] = eh + k * mult * (act_home - exp_home)
        elos[away_id] = ea + k * mult * (act_away - exp_away)

    return elos


def _team_home_stats(history: list[dict], team_id: int) -> tuple[float, float, int]:
    """某队「作为主队」时的 (进球和, 失球和, 场次数)。"""
    gf = ga = n = 0
    for rec in history or []:
        v = _validate_record(rec)
        if v is None:
            continue
        home_id, _away_id, hg, ag = v
        if home_id != team_id or not _is_finished(rec.get("status")):
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
        if away_id != team_id or not _is_finished(rec.get("status")):
            continue
        gf += ag
        ga += hg
        n += 1
    return float(gf), float(ga), n


def _dixon_coles_tau(x: int, y: int, lam_home: float, lam_away: float,
                     rho: float = DC_RHO) -> float:
    """Dixon-Coles 低比分修正因子 τ。

    rho 为负时会抬高 0-0 / 1-1 的概率、压低 0-1 / 1-0，
    修正纯 Poisson 对低比分赛事的系统性低估。
    """
    if x == 0 and y == 0:
        return 1.0 - lam_home * lam_away * rho
    if x == 0 and y == 1:
        return 1.0 + lam_home * rho
    if x == 1 and y == 0:
        return 1.0 + lam_away * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def predict_match(
    home_team_id: int,
    away_team_id: int,
    history: list[dict],
    min_history: Optional[int] = None,
) -> Optional[PredictionResult]:
    """为 home_team_id(主) vs away_team_id(客) 生成 Elo + Dixon-Coles 预测。

    Parameters
    ----------
    home_team_id, away_team_id
        API-Football 真实球队 ID。**绝不可传入 Fixture.id**。
    history
        比赛记录列表，**按开赛时间升序**；函数内部自行过滤未结束场次。
    min_history
        每队所需的最少有效历史场次，缺省取 ``MIN_HISTORY_MATCHES``（默认 5）。

    Returns
    -------
    样本不足返回 ``None``；否则返回 ``PredictionResult``，
    其中 ``recommended=False`` 表示「有结果但不建议参考」。
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

    # ---- Elo ----
    elos = compute_elos(history)
    elo_home = elos.get(home_team_id, ELO_INITIAL)
    elo_away = elos.get(away_team_id, ELO_INITIAL)
    elo_diff = elo_home + ELO_HOME_ADVANTAGE - elo_away

    # ---- 期望进球（由 Elo 分差驱动的对数线性模型）----
    lam_home = BASE_GOALS * math.exp(elo_diff / GOAL_ELO_SCALE)
    lam_away = BASE_GOALS * math.exp(-elo_diff / GOAL_ELO_SCALE)
    lam_home = max(0.2, lam_home)
    lam_away = max(0.2, lam_away)

    # ---- 比分概率矩阵（含 Dixon-Coles 修正）----
    matrix = [[0.0] * (MAX_GOALS + 1) for _ in range(MAX_GOALS + 1)]
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            base = _poisson(lam_home, i) * _poisson(lam_away, j)
            matrix[i][j] = base * _dixon_coles_tau(i, j, lam_home, lam_away)

    home_prob = sum(matrix[i][j] for i in range(MAX_GOALS + 1)
                    for j in range(MAX_GOALS + 1) if i > j)
    away_prob = sum(matrix[i][j] for i in range(MAX_GOALS + 1)
                    for j in range(MAX_GOALS + 1) if i < j)
    draw_prob = sum(matrix[i][j] for i in range(MAX_GOALS + 1)
                    for j in range(MAX_GOALS + 1) if i == j)

    # 归一化：消除 0..6 截断与 DC 修正造成的尾差，保证三者之和严格 = 1
    total = home_prob + draw_prob + away_prob
    if total <= 0:
        return None
    home_prob /= total
    draw_prob /= total
    away_prob /= total

    # 概率非负钳制（浮点误差兜底）
    home_prob = min(1.0, max(0.0, home_prob))
    draw_prob = min(1.0, max(0.0, draw_prob))
    away_prob = min(1.0, max(0.0, away_prob))

    # ---- 最可能比分 ----
    best_i = best_j = 0
    best_p = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            if matrix[i][j] > best_p:
                best_p, best_i, best_j = matrix[i][j], i, j

    # ---- 置信度与「是否建议参考」----
    ordered = sorted([home_prob, draw_prob, away_prob], reverse=True)
    gap = ordered[0] - ordered[1]
    total_games = h_n + a_n

    reasons: list[str] = []
    if gap < RECOMMEND_GAP:
        reasons.append(f"胜负概率差距仅 {gap * 100:.0f}%（需 ≥ {RECOMMEND_GAP * 100:.0f}%）")
    if abs(elo_diff) < RECOMMEND_ELO_GAP:
        reasons.append(f"双方实力差仅 {abs(elo_diff):.0f} 分（需 ≥ {RECOMMEND_ELO_GAP:.0f}）")
    if total_games < RECOMMEND_MIN_GAMES:
        reasons.append(f"历史样本仅 {total_games} 场（需 ≥ {RECOMMEND_MIN_GAMES}）")

    recommended = not reasons
    if recommended:
        confidence = "高" if gap >= 0.22 else "中"
    else:
        confidence = "低"

    completeness = min(1.0, total_games / max(1, min_history * 2))

    evidence = (
        f"主队近 {h_n} 个主场 进{h_gf:.0f} 失{h_ga:.0f}；"
        f"客队近 {a_n} 个客场 进{a_gf:.0f} 失{a_ga:.0f}；"
        f"Elo {elo_home:.0f} vs {elo_away:.0f}（差 {elo_diff:+.0f}）；"
        f"期望进球 {lam_home:.2f} - {lam_away:.2f}。"
    )
    advice = (
        "建议参考" if recommended
        else "不建议参考：" + "；".join(reasons)
    )

    return PredictionResult(
        home_prob=home_prob,
        draw_prob=draw_prob,
        away_prob=away_prob,
        expected_home_goals=round(lam_home, 2),
        expected_away_goals=round(lam_away, 2),
        predicted_score=f"{best_i}-{best_j}",
        confidence=confidence,
        data_completeness=round(completeness, 2),
        evidence=evidence,
        model_version=MODEL_VERSION,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        score_matrix=matrix,
        recommended=recommended,
        advice=advice,
        elo_home=elo_home,
        elo_away=elo_away,
        elo_diff=elo_diff,
        top_two_gap=round(gap, 4),
        sample_games=total_games,
        reasons=reasons,
    )


def attack_defense_probs(history: list[dict], home_team_id: int, away_team_id: int,
                         league_avg: float = 1.35) -> Optional[tuple[float, float, float]]:
    """第三路信号：经典「进攻强度 × 防守强度」Poisson，与主模型方法不同。

    与 Elo / Dixon-Coles 并列，用于模型一致性投票（3/3 表示三路方向一致）。
    任一队样本为 0 时返回 None（不参与投票，避免用默认值伪造一致性）。
    """
    h_gf, h_ga, h_n = _team_home_stats(history, home_team_id)
    a_gf, a_ga, a_n = _team_away_stats(history, away_team_id)
    if h_n == 0 or a_n == 0:
        return None

    # 主队进攻强度 vs 客队客场防守；客队进攻 vs 主队主场防守
    home_attack = (h_gf / h_n) / league_avg
    away_defense = (a_ga / a_n) / league_avg
    away_attack = (a_gf / a_n) / league_avg
    home_defense = (h_ga / h_n) / league_avg

    lam_h = max(0.15, league_avg * home_attack * away_defense)
    lam_a = max(0.15, league_avg * away_attack * home_defense)

    hp = dp = ap = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            pr = _poisson(lam_h, i) * _poisson(lam_a, j)
            if i > j:
                hp += pr
            elif i < j:
                ap += pr
            else:
                dp += pr
    total = hp + dp + ap
    if total <= 0:
        return None
    return hp / total, dp / total, ap / total


def elo_signal_probs(elo_diff: float) -> tuple[float, float, float]:
    """第一路信号：纯 Elo 分差 → 胜平负（logistic + 平局经验分布）。"""
    home_win = 1.0 / (1.0 + 10.0 ** (-elo_diff / ELO_SCALE))
    # 平局比例随双方接近而升高（经验近似，非校准值）
    draw = 0.30 * (1.0 - abs(home_win - 0.5) * 2.0) + 0.08
    draw = min(0.34, max(0.06, draw))
    remain = 1.0 - draw
    return home_win * remain, draw, (1.0 - home_win) * remain


def format_prediction(result: PredictionResult) -> str:
    """把预测结果格式化为面向用户的多行文本。"""
    if not isinstance(result, PredictionResult):
        return "暂无可用预测。"
    lines = [
        f"主胜 {result.home_prob * 100:.1f}% / 平 {result.draw_prob * 100:.1f}% / "
        f"客胜 {result.away_prob * 100:.1f}%",
        f"预期进球 {result.expected_home_goals:.2f} - {result.expected_away_goals:.2f}",
        f"最可能比分 {result.predicted_score} | 置信度 {result.confidence}",
        f"数据完整度 {int(result.data_completeness * 100)}%",
        f"模型版本 {result.model_version}",
        f"预测依据：{result.evidence}",
        f"【{result.advice}】",
    ]
    return "\n".join(lines)


__all__ = [
    "MODEL_VERSION", "PredictionResult", "predict_match", "format_prediction",
    "compute_elos", "_is_finished", "_dixon_coles_tau",
    "attack_defense_probs", "elo_signal_probs",
]

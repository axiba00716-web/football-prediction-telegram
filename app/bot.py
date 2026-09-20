"""Telegram Bot：命令处理 + 预测编排。

关键不变量
----------
* 预测使用的身份 = ``Fixture.home_team_id`` / ``Fixture.away_team_id``
  （API-Football 真实球队 ID）。
* **绝不**把 ``Fixture.id``（数据库主键）当作球队 ID 传给 ``predict_match``。
* ``/predict`` 流程：当日比赛 →（无则 sync_date）→ 逐场 ``sync_team_history``
  → 从 DB 读两队已结束历史 → ``predict_match`` → 保存 Prediction（同
  fixture_id + model_version 去重）。
* 模块导入时不执行任何网络请求 / 数据库查询。
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import date, datetime, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings, get_timezone
from app.data import (
    FootballAPIError, from_utc_naive, local_day_window,
    sync_local_date, sync_team_history,
)
from app.markets import (
    MARKETS, OUTCOME_LABELS, compute_binary_markets, evaluate_consistency,
    recommended_binary, selection_tier,
)
from app.tracking import (
    format_summary, overall_summary, save_prediction, settle_all, settle_finished,
    stats_by_league,
)
from app.db import Fixture, Prediction, get_session, init_db
from app.predictor import (
    MODEL_VERSION, PredictionResult, format_prediction, predict_match,
)

logger = logging.getLogger(__name__)

DISCLAIMER = "仅供数据分析参考，不构成投注建议。"
TELEGRAM_MSG_LIMIT = 3500  # Telegram 上限 4096，留余量
# 赛程/预测表格每批最多几行：控制单条消息长度，避免代码块被截断
TABLE_BATCH = 20


def _display_width(text: str) -> int:
    """按等宽字体显示宽度计算（中日韩字符占 2 列，保证对齐）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(text))


def _pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补齐到指定列宽。"""
    text = str(text)
    gap = width - _display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def _ellipsis(text: str, max_width: int) -> str:
    """按显示宽度截断，超长加省略号。"""
    text = str(text)
    if _display_width(text) <= max_width:
        return text
    out, cur = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if cur + w > max_width - 1:
            break
        out += ch
        cur += w
    return out + "…"


def render_table(headers: list[str], rows: list[list], aligns: list[str] | None = None) -> str:
    """生成等宽文本表格（配合 Telegram Markdown 代码块显示为表格）。"""
    if not headers or not rows:
        return ""
    n = len(headers)
    aligns = aligns or ["left"] * n
    cells = [[str(c) for c in list(r)[:n]] + [""] * max(0, n - len(r)) for r in rows]
    widths = [
        max(_display_width(headers[i]),
            max((_display_width(c[i]) for c in cells), default=0))
        for i in range(n)
    ]
    line_sep = "  ".join("-" * w for w in widths)
    lines = ["  ".join(_pad(headers[i], widths[i], "center") for i in range(n)), line_sep]
    for c in cells:
        lines.append("  ".join(_pad(c[i], widths[i], aligns[i]) for i in range(n)))
    return "\n".join(lines)


def render_fixture_cards(rows: list[list]) -> str:
    """赛程卡片：两行一场，**不依赖列对齐**。

    中文在 Telegram 等宽字体下宽度不 guaranteed 为 2 列，
    用空格 padding 做表格必然错位；改为「时间·联赛 / 对阵」两行结构，
    任何字体下都整齐。
    """
    if not rows:
        return ""
    blocks = []
    for r in rows:
        when, league, matchup = (list(r) + ["", "", ""])[:3]
        line1 = " · ".join(x for x in (when, league) if x)
        blocks.append(f"{line1}\n{matchup}" if line1 else matchup)
    return "\n\n".join(blocks)


def render_prediction_cards(rows: list[list], details: list[str],
                            unavailable: list[str]) -> str:
    """预测卡片：一场三行，同样不依赖列对齐。"""
    parts = []
    for r in rows:
        matchup, hp, dp, ap, score, conf = (list(r) + [""] * 6)[:6]
        parts.append(
            f"{matchup}\n"
            f"主 {hp} · 平 {dp} · 客 {ap}\n"
            f"比分 {score} · 置信 {conf}"
        )
    if details:
        parts.append("预测依据\n" + "\n".join(details))
    if unavailable:
        parts.append("未能预测\n" + "\n".join(f"· {u}" for u in unavailable))
    return "\n\n".join(parts)


def _code_block(text: str) -> str:
    """包裹为 Telegram 等宽代码块（内容转义，``` 标记保持原样）。"""
    return "```\n" + _escape_md(text) + "\n```"


def _escape_md(text: str) -> str:
    """Markdown 模式下转义易破坏解析的字符。"""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text

# API-Football 免费套餐 100 次/天，每支球队的历史需 1 次请求，
# 限制单轮预测场次，避免一次 /predict 就把当天配额打光。
MAX_PREDICT_FIXTURES = 8

try:
    import telegram as _tg_probe
    _HAS_TELEGRAM = True
except Exception:  # pragma: no cover - 测试环境可能无 telegram
    _HAS_TELEGRAM = False

# 已结束状态（与 predictor / data 保持一致）
FINISHED_STATUSES = ["FT", "AET", "PEN", "finished", "Match Finished"]


# --------------------------------------------------------------------------- #
# 时区工具
# --------------------------------------------------------------------------- #

def today_local() -> date:
    """按 ``TIMEZONE``（默认 Asia/Shanghai）取用户口中的「今天」。

    这是「比赛当天」的唯一口径：用户说今天，指的是北京时间今天，
    而不是容器本机（Railway 为 UTC）的今天。
    """
    return datetime.now(get_timezone()).date()


# --------------------------------------------------------------------------- #
# 消息工具
# --------------------------------------------------------------------------- #

async def _reply(update: Update, text: str, markdown: bool = False,
                 keyboard=None) -> None:
    """超长消息自动分段发送；Markdown 解析失败时自动回退纯文本。"""
    if not text:
        return
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_MSG_LIMIT:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, TELEGRAM_MSG_LIMIT)
        if cut <= 0:
            cut = TELEGRAM_MSG_LIMIT
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    for p in parts:
        if markdown:
            try:
                await update.message.reply_text(p, parse_mode="Markdown")
                continue
            except Exception:  # noqa: BLE001 - Markdown 解析失败则降级
                logger.warning("Markdown 发送失败，回退纯文本")
        await update.message.reply_text(p, reply_markup=keyboard)


def _local_time_text(start_time) -> str:
    """库里存的是 UTC naive，展示前换算到 ``TIMEZONE``。"""
    local = from_utc_naive(start_time)
    if local is None:
        return "时间待定"
    return local.strftime("%Y-%m-%d %H:%M")


def _fmt_fixture(f) -> str:
    return f"[{f.league}] {f.home} vs {f.away} @ {_local_time_text(getattr(f, 'start_time', None))}"


def _fixtures_on(target: date) -> list[Fixture]:
    """查询用户时区某一天的比赛（内部换算为 UTC 窗口），Session 显式关闭。"""
    utc_start, utc_end = local_day_window(target)
    session = get_session()
    try:
        return session.query(Fixture).filter(
            Fixture.start_time >= utc_start,
            Fixture.start_time < utc_end,
        ).order_by(Fixture.start_time).all()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# /start /help
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, (
        "⚽ 足球赛程与预测机器人\n\n"
        "三种口径独立统计，绝不混算：\n"
        "🎯 精选胜平负 —— 只推送 A/B 级高置信场次\n"
        "📊 二分类预测 —— 主队不败 / 大于1.5球 / 双方进球\n"
        "📈 完整数据报告 —— 全部比赛与模型细节\n\n"
        "命令：/select /binary /report /stats /today /predict\n\n"
        + DISCLAIMER
    ), markdown=False, keyboard=_main_keyboard() if _HAS_TELEGRAM else None)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, (
        "📖 使用说明\n"
        "/select - 精选胜平负（A/B 级，高置信）\n"
        "/binary - 二分类市场（主队不败 / 大于1.5球 / 双方进球）\n"
        "/report - 完整数据报告（全部比赛）\n"
        "/stats - 历史表现（各口径分开统计）\n"
        "/today、/tomorrow - 赛程\n"
        "/predict - 全量胜平负\n\n"
        "说明：\n"
        "- 历史样本不足时不会强行预测。\n"
        "- 所有预测仅供数据分析参考，不构成投注建议。\n"
        "- 不保证命中率、准确率或盈利。"
    ))


# --------------------------------------------------------------------------- #
# /today /tomorrow
# --------------------------------------------------------------------------- #

async def _sync_and_show(update: Update, target: date) -> None:
    try:
        await sync_local_date(target)
    except FootballAPIError as e:
        await _reply(update, f"⚠️ 数据同步失败：{e}")
        return
    except Exception as e:  # noqa: BLE001
        logger.exception("sync_date 异常")
        await _reply(update, f"⚠️ 同步异常：{e}")
        return

    rows = _fixtures_on(target)
    if not rows:
        await _reply(update, f"{target.isoformat()} 暂无配置联赛的比赛。")
        return

    tzname = get_settings().TIMEZONE
    card_rows = [
        [_local_time_text(getattr(f, "start_time", None)),
         _ellipsis(f.league or "-", 12),
         f"{_ellipsis(f.home or '?', 14)} vs {_ellipsis(f.away or '?', 14)}"]
        for f in rows
    ]
    header = f"📅 {target.isoformat()} · {len(rows)} 场（{tzname}）"

    # 按批发送，避免单条消息超长
    for i in range(0, len(card_rows), TABLE_BATCH):
        batch = card_rows[i:i + TABLE_BATCH]
        body = render_fixture_cards(batch)
        prefix = header if i == 0 else None
        text = (_escape_md(prefix) + "\n\n" if prefix else "") + _code_block(body)
        await _reply(update, text, markdown=True)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _sync_and_show(update, today_local())


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _sync_and_show(update, today_local() + timedelta(days=1))


# --------------------------------------------------------------------------- #
# /predict
# --------------------------------------------------------------------------- #

def _history_from_db(home_team_id: int, away_team_id: int) -> list[dict]:
    """读取两队相关比赛并转成 predictor 的 history schema。

    只保留**已结束**且比分完整的场次；主客位置由数据库字段决定，不做任何互换。
    """
    session = get_session()
    try:
        rows = session.query(Fixture).filter(
            ((Fixture.home_team_id == home_team_id) | (Fixture.away_team_id == home_team_id))
            | ((Fixture.home_team_id == away_team_id) | (Fixture.away_team_id == away_team_id)),
        ).all()
        history: list[dict] = []
        for f in rows:
            if f.home_score is None or f.away_score is None:
                continue
            history.append({
                "home_id": f.home_team_id,
                "away_id": f.away_team_id,
                "home_goals": f.home_score,
                "away_goals": f.away_score,
                "status": f.status,
                "start_time": f.start_time,
            })
        # Elo 必须按开赛时间升序递推，顺序错误会得出反的实力差
        history.sort(key=lambda r: r.get("start_time") or datetime.min)
        return history
    finally:
        session.close()


def _save_prediction(fix: Fixture, result: PredictionResult) -> None:
    """按**三种口径分别**保存预测，便于事后分开统计准确率。

    * ``full_1x2``     —— 全量胜平负（每场都记，覆盖率 100%）
    * ``selected_1x2`` —— 仅 A/B 级精选
    * ``binary``       —— 二分类市场（仅达推荐门槛的）
    """
    session = get_session()
    try:
        consistency = evaluate_consistency(result, _history_from_db(
            result.home_team_id, result.away_team_id))
        tier, _reasons = selection_tier(result, consistency)

        probs = {"home_win": result.home_prob, "draw": result.draw_prob,
                 "away_win": result.away_prob}
        top_outcome = max(probs, key=probs.get)

        # 1) 全量胜平负
        save_prediction(
            session, fixture_id=fix.id, prediction_type="full_1x2",
            prediction_market=top_outcome, probability=probs[top_outcome],
            model_version=result.model_version, tier=tier,
            consistency=consistency.ratio_text,
            home_prob=result.home_prob, draw_prob=result.draw_prob,
            away_prob=result.away_prob,
            exp_home=result.expected_home_goals, exp_away=result.expected_away_goals,
            score=result.predicted_score, confidence=result.confidence,
            completeness=result.data_completeness, evidence=result.evidence,
            cutoff=datetime.utcnow(),
        )

        # 2) 精选胜平负（仅 A / B 级）
        if tier in ("A", "B"):
            save_prediction(
                session, fixture_id=fix.id, prediction_type="selected_1x2",
                prediction_market=top_outcome, probability=probs[top_outcome],
                model_version=result.model_version, tier=tier,
                consistency=consistency.ratio_text,
                home_prob=result.home_prob, draw_prob=result.draw_prob,
                away_prob=result.away_prob,
                exp_home=result.expected_home_goals, exp_away=result.expected_away_goals,
                score=result.predicted_score, confidence=result.confidence,
                completeness=result.data_completeness, evidence=result.evidence,
                cutoff=datetime.utcnow(),
            )

        # 3) 二分类市场（仅推荐项）
        markets = compute_binary_markets(result)
        if markets:
            rec = recommended_binary(markets, getattr(result, "sample_games", 0))
            if rec:
                key, prob = rec
                save_prediction(
                    session, fixture_id=fix.id, prediction_type="binary",
                    prediction_market=key, probability=prob,
                    model_version=result.model_version, tier=tier,
                    consistency=consistency.ratio_text,
                    home_prob=result.home_prob, draw_prob=result.draw_prob,
                    away_prob=result.away_prob,
                    exp_home=result.expected_home_goals, exp_away=result.expected_away_goals,
                    score=result.predicted_score, confidence=result.confidence,
                    completeness=result.data_completeness,
                    evidence=f"{MARKETS.get(key, key)} {prob * 100:.0f}%",
                    cutoff=datetime.utcnow(),
                )
        session.commit()
    finally:
        session.close()


async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not get_settings().PREDICTION_ENABLED:
        await _reply(update, "预测功能当前已关闭（PREDICTION_ENABLED=false）。")
        return

    target = today_local()
    fixtures = _fixtures_on(target)

    # 当日无比赛 → 先同步一次
    if not fixtures:
        try:
            await sync_local_date(target)
        except FootballAPIError as e:
            await _reply(update, f"⚠️ 同步赛程失败：{e}")
            return
        except Exception as e:  # noqa: BLE001
            logger.exception("sync_date 异常")
            await _reply(update, f"⚠️ 同步赛程异常：{e}")
            return
        fixtures = _fixtures_on(target)

    if not fixtures:
        await _reply(update, "今日无配置联赛的比赛，暂无可预测项。")
        return

    total_fixtures = len(fixtures)
    if total_fixtures > MAX_PREDICT_FIXTURES:
        fixtures = fixtures[:MAX_PREDICT_FIXTURES]
        notice = (
            f"当日共 {total_fixtures} 场比赛，"
            f"受 API 免费额度限制，本次只预测前 {MAX_PREDICT_FIXTURES} 场。\n\n"
        )
    else:
        notice = ""

    # 先把当天涉及的球队去重批量同步历史（内部有缓存，每队只请求一次）
    team_ids: list[int] = []
    for fix in fixtures:
        for tid in (fix.home_team_id, fix.away_team_id):
            if tid and tid not in team_ids:
                team_ids.append(tid)

    sync_errors: dict[int, str] = {}
    for tid in team_ids:
        try:
            await sync_team_history(tid, last=20)
        except FootballAPIError as e:
            sync_errors[tid] = str(e)
        except Exception as e:  # noqa: BLE001
            logger.exception("sync_team_history 异常")
            sync_errors[tid] = str(e)

    table_rows: list[list] = []
    details: list[str] = []
    unavailable: list[str] = []

    for fix in fixtures:
        home_id = fix.home_team_id
        away_id = fix.away_team_id
        matchup = f"{_ellipsis(fix.home or '?', 9)} vs {_ellipsis(fix.away or '?', 9)}"

        # 球队 ID 缺失 → 明确告知无法预测，绝不用 fix.id 顶替
        if not home_id or not away_id:
            unavailable.append(f"{matchup}：缺少真实球队 ID，无法可靠预测")
            continue

        failed = sync_errors.get(home_id) or sync_errors.get(away_id)
        if failed:
            unavailable.append(f"{matchup}：历史同步失败 - {failed}")
            continue

        history = _history_from_db(home_id, away_id)
        result = predict_match(home_id, away_id, history)

        if result is None:
            unavailable.append(f"{matchup}：历史样本不足，暂不提供可靠预测")
            continue

        _save_prediction(fix, result)
        table_rows.append([
            matchup,
            f"{round(result.home_prob * 100)}%",
            f"{round(result.draw_prob * 100)}%",
            f"{round(result.away_prob * 100)}%",
            result.predicted_score,
            result.confidence,
        ])
        details.append(f"• {matchup}：{result.evidence}")

    if table_rows:
        header = f"📊 {target.isoformat()} 预测 · {len(table_rows)} 场"
        body = render_prediction_cards(table_rows, details, unavailable)
        text = _escape_md(header) + "\n\n" + _code_block(body)
        text += "\n\n" + DISCLAIMER
        await _reply(update, text, markdown=True)

    if not table_rows and unavailable:
        await _reply(update, "⚠️ 未能预测：\n" + "\n".join(f"• {u}" for u in unavailable))

    if not table_rows and not unavailable:
        await _reply(update, notice.strip() or "今日无可预测项。")


# --------------------------------------------------------------------------- #
# 三口径命令：/select  /binary  /report  /stats
# --------------------------------------------------------------------------- #

async def _ensure_fixtures(update: Update, target: date) -> list:
    rows = _fixtures_on(target)
    if rows:
        return rows
    try:
        await sync_local_date(target)
    except FootballAPIError as e:
        await _reply(update, f"⚠️ 同步赛程失败：{e}")
        return []
    except Exception as e:  # noqa: BLE001
        logger.exception("sync 异常")
        await _reply(update, f"⚠️ 同步异常：{e}")
        return []
    return _fixtures_on(target)


def _analyse(fix, history: list[dict]):
    """对一场比赛跑完整分析，返回 (result, consistency, tier, markets, reasons)。"""
    result = predict_match(fix.home_team_id, fix.away_team_id, history)
    if result is None:
        return None, None, "", None, ["历史样本不足"]
    consistency = evaluate_consistency(result, history)
    tier, reasons = selection_tier(result, consistency)
    markets = compute_binary_markets(result)
    return result, consistency, tier, markets, reasons


async def select_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🎯 精选胜平负：只推送 A/B 级高置信场次。"""
    if not get_settings().PREDICTION_ENABLED:
        await _reply(update, "预测功能当前已关闭。")
        return

    target = today_local()
    fixtures = await _ensure_fixtures(update, target)
    if not fixtures:
        await _reply(update, "今日暂无比赛。")
        return

    try:
        settle_all()   # 先结算已结束比赛，保证统计新鲜
    except Exception:  # noqa: BLE001
        logger.warning("结算失败（不影响预测）", exc_info=True)

    picked = []
    for fix in fixtures[:MAX_PREDICT_FIXTURES]:
        if not (fix.home_team_id and fix.away_team_id):
            continue
        try:
            await sync_team_history(fix.home_team_id, last=20)
            await sync_team_history(fix.away_team_id, last=20)
        except Exception as e:  # noqa: BLE001
            logger.warning("历史同步失败 %s: %s", fix.external_id, e)
            continue
        history = _history_from_db(fix.home_team_id, fix.away_team_id)
        result, consistency, tier, _m, reasons = _analyse(fix, history)
        if result is None or tier not in ("A", "B"):
            continue
        _save_prediction(fix, result)
        probs = {"home_win": result.home_prob, "draw": result.draw_prob,
                 "away_win": result.away_prob}
        outcome = max(probs, key=probs.get)
        picked.append((fix, result, outcome, probs[outcome], tier, consistency))

    if not picked:
        await _reply(update, "当前没有达到精选标准的比赛。")
        return

    picked.sort(key=lambda x: (x[4] != "A", -x[3]))
    blocks = []
    for fix, result, outcome, prob, tier, consistency in picked:
        blocks.append(
            f"{'🅰️' if tier == 'A' else '🅱️'} {fix.home} vs {fix.away}\n"
            f"主胜 {result.home_prob * 100:.0f}% · 平 {result.draw_prob * 100:.0f}% "
            f"· 客胜 {result.away_prob * 100:.0f}%\n"
            f"预测：{OUTCOME_LABELS[outcome]}\n"
            f"等级：{tier}级精选 · 一致性 {consistency.ratio_text}\n"
            f"数据完整度 {int(result.data_completeness * 100)}%"
        )
    text = "🎯 精选胜平负\n\n" + "\n\n".join(blocks) + "\n\n" + DISCLAIMER
    await _reply(update, _code_block(text))


async def binary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📊 二分类预测：主队不败 / 客队不败 / 大于1.5球 / 小于4.5球 / 双方进球。"""
    if not get_settings().PREDICTION_ENABLED:
        await _reply(update, "预测功能当前已关闭。")
        return

    target = today_local()
    fixtures = await _ensure_fixtures(update, target)
    if not fixtures:
        await _reply(update, "今日暂无比赛。")
        return

    blocks = []
    for fix in fixtures[:MAX_PREDICT_FIXTURES]:
        if not (fix.home_team_id and fix.away_team_id):
            continue
        try:
            await sync_team_history(fix.home_team_id, last=20)
            await sync_team_history(fix.away_team_id, last=20)
        except Exception as e:  # noqa: BLE001
            logger.warning("历史同步失败 %s: %s", fix.external_id, e)
            continue
        history = _history_from_db(fix.home_team_id, fix.away_team_id)
        result, _c, _t, markets, reasons = _analyse(fix, history)
        if result is None or markets is None:
            continue

        d = markets.as_dict()
        lines = [
            f"{fix.home} vs {fix.away}",
            f"主队不败 {d['home_double_chance'] * 100:.0f}% · "
            f"客队不败 {d['away_double_chance'] * 100:.0f}%",
            f"大于1.5球 {d['over_1_5'] * 100:.0f}% · "
            f"小于4.5球 {d['under_4_5'] * 100:.0f}% · "
            f"双方进球 {d['btts'] * 100:.0f}%",
        ]
        rec = recommended_binary(markets, getattr(result, "sample_games", 0))
        if rec:
            key, prob = rec
            _save_prediction(fix, result)
            lines.append(f"优先预测：{MARKETS[key]}（{prob * 100:.0f}%）")
        else:
            lines.append("无达门槛的二分类项")
        blocks.append("\n".join(lines))

    if not blocks:
        await _reply(update, "暂无可计算二分类市场的比赛。")
        return

    text = "📊 二分类预测\n\n" + "\n\n".join(blocks) + "\n\n" + DISCLAIMER
    await _reply(update, _code_block(text))


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📈 完整数据报告：全部比赛 + 模型细节 + 分档统计。"""
    if not get_settings().PREDICTION_ENABLED:
        await _reply(update, "预测功能当前已关闭。")
        return

    target = today_local()
    fixtures = await _ensure_fixtures(update, target)
    if not fixtures:
        await _reply(update, "今日暂无比赛。")
        return

    tier_count = {"A": 0, "B": 0, "C": 0, "insufficient": 0}
    analysed = []
    best_home = best_draw = best_binary = None

    for fix in fixtures[:MAX_PREDICT_FIXTURES]:
        if not (fix.home_team_id and fix.away_team_id):
            tier_count["insufficient"] += 1
            continue
        try:
            await sync_team_history(fix.home_team_id, last=20)
            await sync_team_history(fix.away_team_id, last=20)
        except Exception:  # noqa: BLE001
            tier_count["insufficient"] += 1
            continue
        history = _history_from_db(fix.home_team_id, fix.away_team_id)
        result, consistency, tier, markets, reasons = _analyse(fix, history)
        if result is None:
            tier_count["insufficient"] += 1
            continue
        tier_count[tier or "insufficient"] = tier_count.get(tier or "insufficient", 0) + 1
        analysed.append((fix, result, consistency, tier, markets))

        if best_home is None or result.home_prob > best_home[1].home_prob:
            best_home = (fix, result)
        if best_draw is None or result.draw_prob > best_draw[1].draw_prob:
            best_draw = (fix, result)
        if markets:
            key, prob = markets.best()
            if best_binary is None or prob > best_binary[2]:
                best_binary = (fix, key, prob)

    lines = [f"📈 今日 {len(fixtures)} 场比赛", "", "已筛选："]
    lines.append(f"A级精选：{tier_count['A']}场")
    lines.append(f"B级普通：{tier_count['B']}场")
    lines.append(f"C级（不建议）：{tier_count['C']}场")
    lines.append(f"数据不足：{tier_count['insufficient']}场")

    if best_home:
        lines.append(f"\n主胜概率最高：{best_home[0].home} vs {best_home[0].away}"
                     f"：{best_home[1].home_prob * 100:.0f}%")
    if best_draw:
        lines.append(f"平局概率最高：{best_draw[0].home} vs {best_draw[0].away}"
                     f"：{best_draw[1].draw_prob * 100:.0f}%")
    if best_binary:
        lines.append(f"二分类最高：{best_binary[0].home} vs {best_binary[0].away}"
                     f"：{MARKETS[best_binary[1]]} {best_binary[2] * 100:.0f}%")

    if analysed:
        lines.append("")
        for fix, result, consistency, tier, markets in analysed:
            lines.append(
                f"{fix.home} vs {fix.away}｜主{result.home_prob * 100:.0f}% "
                f"平{result.draw_prob * 100:.0f}% 客{result.away_prob * 100:.0f}%"
                f"｜{result.predicted_score}｜λ {result.expected_home_goals:.2f}-"
                f"{result.expected_away_goals:.2f}｜一致 {consistency.ratio_text}"
                f"｜{tier or '数据不足'}"
            )
    lines.append("")
    lines.append(DISCLAIMER)
    await _reply(update, _code_block("\n".join(lines)))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """📉 历史表现：三种口径分开统计，同时展示覆盖率。"""
    try:
        settled = settle_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("结算失败: %s", e)
        settled = 0

    data = overall_summary()
    text = "📉 历史表现（口径独立统计）\n\n" + format_summary(data)
    if settled:
        text += f"\n\n本次新结算 {settled} 条"

    try:
        leagues = stats_by_league("selected_1x2")
        if leagues:
            text += "\n\n精选·各联赛：\n" + "\n".join(
                f"· {x['league']}：{x['hits']}/{x['settled']} {x['accuracy']}%"
                for x in leagues[:8]
            )
    except Exception:  # noqa: BLE001
        pass

    text += ("\n\n说明：精选准确率与全量准确率不可互相替代，"
             "必须结合覆盖率一起看。")
    await _reply(update, _code_block(text))


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    db_state = "正常"
    n_fixtures = n_predictions = 0
    session = get_session()
    try:
        n_fixtures = session.query(Fixture).count()
        n_predictions = session.query(Prediction).count()
    except Exception as e:  # noqa: BLE001
        logger.exception("status 查询失败")
        db_state = f"异常：{e}"
    finally:
        session.close()

    await _reply(update, (
        "✅ 机器人运行中（polling 模式）\n"
        f"模型版本: {MODEL_VERSION}\n"
        f"数据库状态: {db_state}\n"
        f"比赛(Fixture)数量: {n_fixtures}\n"
        f"预测(Prediction)数量: {n_predictions}\n"
        f"配置联赛: {settings.ENABLED_LEAGUES}\n"
        f"最少历史场次: {settings.MIN_HISTORY_MATCHES}\n"
        f"时区: {settings.TIMEZONE}（赛程与开赛时间均按此时区）\n"
        f"今天(按时区): {today_local().isoformat()}"
    ))


# --------------------------------------------------------------------------- #
# 注册 / 构建 / 启动
# --------------------------------------------------------------------------- #

# Telegram 输入框旁的「斜杠菜单」：点 / 或菜单按钮即可选择命令
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "欢迎信息与命令清单"),
    ("select", "精选胜平负（A/B 级）"),
    ("binary", "二分类市场预测"),
    ("report", "完整数据报告"),
    ("stats", "历史表现与命中率"),
    ("today", "今天赛程"),
    ("tomorrow", "明天赛程"),
    ("predict", "全量胜平负预测"),
    ("status", "运行状态与数据库统计"),
    ("help", "使用说明与免责声明"),
]

# 主菜单按钮（点输入框旁的菜单图标即可看到）
MAIN_KEYBOARD = [
    ["🎯 精选胜平负", "📊 二分类预测"],
    ["📈 完整数据报告", "📅 今日赛程"],
    ["📉 历史表现", "⚙️ 设置"],
]


def _main_keyboard():
    """构建主菜单 ReplyKeyboard。"""
    from telegram import KeyboardButton, ReplyKeyboardMarkup
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t) for t in row] for row in MAIN_KEYBOARD],
        resize_keyboard=True,
    )


async def _set_bot_commands(application) -> None:
    """启动时把命令菜单注册到 Telegram（用户在输入框点 / 即可选择）。"""
    try:
        from telegram import BotCommand
        await application.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS]
        )
        logger.info("已注册 %d 个 Telegram 命令菜单项", len(BOT_COMMANDS))
    except Exception:  # noqa: BLE001 - 菜单注册失败不影响机器人运行
        logger.warning("注册 Telegram 命令菜单失败（不影响使用）", exc_info=True)


def register_handlers(application, CommandHandler=None) -> None:
    """注册全部命令处理器。CommandHandler 可注入（测试用）。"""
    if CommandHandler is None:
        from telegram.ext import CommandHandler as _CH
        CommandHandler = _CH
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("predict", predict))
    application.add_handler(CommandHandler("select", select_cmd))
    application.add_handler(CommandHandler("binary", binary_cmd))
    application.add_handler(CommandHandler("report", report_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("status", status))


def build_application(application=None, CommandHandler=None) -> "Application":
    """构造并注册好 handler 的 Application（无 Token 直接抛错）。"""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN 未配置。请在 Railway Variables 或 .env 中填入 Bot Token。"
        )
    init_db()
    if application is None:
        application = (
            Application.builder()
            .token(settings.TELEGRAM_BOT_TOKEN)
            .post_init(_set_bot_commands)
            .build()
        )
    register_handlers(application, CommandHandler=CommandHandler)
    return application


def run_polling(drop_pending_updates: bool = True) -> None:
    """同步阻塞启动 polling（供 main.py 或手动调试调用）。"""
    application = build_application()
    logger.info("Starting Telegram bot in polling mode")
    application.run_polling(drop_pending_updates=drop_pending_updates)




__all__ = [
    "render_fixture_cards", "render_prediction_cards", "start", "help_command", "today", "tomorrow", "predict", "status",
    "register_handlers", "build_application", "run_polling",
    "today_local", "DISCLAIMER", "FINISHED_STATUSES",
    "BOT_COMMANDS", "_set_bot_commands",
]

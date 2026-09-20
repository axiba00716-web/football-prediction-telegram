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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings
from app.data import FootballAPIError, sync_date, sync_team_history
from app.db import Fixture, Prediction, get_session, init_db
from app.predictor import (
    MODEL_VERSION, PredictionResult, format_prediction, predict_match,
)

logger = logging.getLogger(__name__)

DISCLAIMER = "仅供数据分析参考，不构成投注建议。"
TELEGRAM_MSG_LIMIT = 3500  # Telegram 上限 4096，留余量

# 已结束状态（与 predictor / data 保持一致）
FINISHED_STATUSES = ["FT", "AET", "PEN", "finished", "Match Finished"]


# --------------------------------------------------------------------------- #
# 时区工具
# --------------------------------------------------------------------------- #

_FALLBACK_OFFSETS = {
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Tokyo": 9,
    "UTC": 0,
}


def _tzinfo():
    """按 TIMEZONE 取 tzinfo；tzdata 缺失时回退固定偏移，绝不抛异常。"""
    name = (get_settings().TIMEZONE or "UTC").strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        offset = _FALLBACK_OFFSETS.get(name)
        if offset is None:
            logger.warning("时区 %s 不可用，回退 UTC", name)
            return timezone.utc
        return timezone(timedelta(hours=offset))


def today_local() -> date:
    """按配置时区（默认 Asia/Shanghai）取「今天」。"""
    return datetime.now(_tzinfo()).date()


# --------------------------------------------------------------------------- #
# 消息工具
# --------------------------------------------------------------------------- #

async def _reply(update: Update, text: str) -> None:
    """超长消息自动分段发送，避免超过 Telegram 单条长度限制。"""
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
        await update.message.reply_text(p)


def _fmt_fixture(f) -> str:
    when = f.start_time.strftime("%m-%d %H:%M") if getattr(f, "start_time", None) else "时间待定"
    return f"[{f.league}] {f.home} vs {f.away} @ {when}"


def _day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _day_end(d: date) -> datetime:
    return _day_start(d) + timedelta(days=1)


def _fixtures_on(target: date) -> list[Fixture]:
    """查询数据库中某一天的比赛（左闭右开窗口），Session 显式关闭。"""
    session = get_session()
    try:
        return session.query(Fixture).filter(
            Fixture.start_time >= _day_start(target),
            Fixture.start_time < _day_end(target),
        ).order_by(Fixture.start_time).all()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# /start /help
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, (
        "⚽ 足球赛程与预测机器人\n\n"
        "可用命令：\n"
        "/today - 今天赛程\n"
        "/tomorrow - 明天赛程\n"
        "/predict - 今日比赛预测\n"
        "/status - 运行状态\n"
        "/help - 使用说明\n\n"
        + DISCLAIMER
    ))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, (
        "📖 使用说明\n"
        "/today、/tomorrow：同步并显示配置联赛的比赛。\n"
        "/predict：对今日比赛做 Poisson 基线预测（需足够历史数据）。\n"
        "/status：查看机器人与数据库状态。\n\n"
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
        await sync_date(target)
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

    lines = [f"📅 {target.isoformat()} 比赛（{len(rows)} 场，时间为 UTC）："]
    lines.extend(_fmt_fixture(f) for f in rows)
    await _reply(update, "\n".join(lines))


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
            })
        return history
    finally:
        session.close()


def _save_prediction(fix: Fixture, result: PredictionResult) -> None:
    """保存预测；同 (fixture_id, model_version) 已存在则更新，不重复插入。"""
    session = get_session()
    try:
        existing = session.query(Prediction).filter(
            Prediction.fixture_id == fix.id,
            Prediction.model_version == result.model_version,
        ).one_or_none()
        data = {
            "fixture_id": fix.id,
            "model_version": result.model_version,
            "home_prob": result.home_prob,
            "draw_prob": result.draw_prob,
            "away_prob": result.away_prob,
            "expected_home_goals": result.expected_home_goals,
            "expected_away_goals": result.expected_away_goals,
            "predicted_score": result.predicted_score,
            "confidence": result.confidence,
            "data_completeness": result.data_completeness,
            "evidence": result.evidence,
        }
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
        else:
            session.add(Prediction(**data))
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
            await sync_date(target)
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

    blocks: list[str] = []
    for fix in fixtures:
        home_id = fix.home_team_id
        away_id = fix.away_team_id

        # 球队 ID 缺失 → 明确告知无法预测，绝不用 fix.id 顶替
        if not home_id or not away_id:
            blocks.append(
                f"⚠️ {fix.home} vs {fix.away}：缺少真实球队 ID"
                f"（home={home_id}, away={away_id}），无法可靠预测。"
            )
            continue

        try:
            await sync_team_history(home_id, last=20)
            await sync_team_history(away_id, last=20)
        except FootballAPIError as e:
            blocks.append(f"⚠️ {fix.home} vs {fix.away}：历史同步失败 - {e}")
            continue
        except Exception as e:  # noqa: BLE001
            logger.exception("sync_team_history 异常")
            blocks.append(f"⚠️ {fix.home} vs {fix.away}：历史同步异常 - {e}")
            continue

        history = _history_from_db(home_id, away_id)
        result = predict_match(home_id, away_id, history)

        if result is None:
            blocks.append(
                f"📊 {fix.home} vs {fix.away}\n"
                "历史样本不足，暂不提供可靠预测。"
            )
            continue

        _save_prediction(fix, result)
        blocks.append(
            f"📊 {fix.home} vs {fix.away}\n"
            f"{format_prediction(result)}\n"
            f"{DISCLAIMER}"
        )

    await _reply(update, "\n\n".join(blocks))


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
        f"时区: {settings.TIMEZONE}\n"
        f"今天(按时区): {today_local().isoformat()}"
    ))


# --------------------------------------------------------------------------- #
# 注册 / 构建 / 启动
# --------------------------------------------------------------------------- #

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
        application = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    register_handlers(application, CommandHandler=CommandHandler)
    return application


def run_polling(drop_pending_updates: bool = True) -> None:
    """同步阻塞启动 polling（供 main.py 或手动调试调用）。"""
    application = build_application()
    logger.info("Starting Telegram bot in polling mode")
    application.run_polling(drop_pending_updates=drop_pending_updates)


__all__ = [
    "start", "help_command", "today", "tomorrow", "predict", "status",
    "register_handlers", "build_application", "run_polling",
    "today_local", "DISCLAIMER", "FINISHED_STATUSES",
]

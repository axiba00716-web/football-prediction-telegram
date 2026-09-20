"""Telegram Bot：命令处理 + 预测编排。

关键不变量
----------
* 预测使用的身份 = ``Fixture.home_team_id`` / ``away_team_id`` (API-Football 真实球队 ID)。
* **绝不**把 ``Fixture.id`` (数据库主键) 当作球队 ID 传入 ``predict_match``。
* ``/predict`` 流程：当日比赛 → (若无则 sync_date) → 逐场 sync_team_history(home/away)
  → 从 DB 读两队已结束历史 → predict_match → 保存 Prediction（同 fixture+model 去重）。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings
from app.data import (
    FootballAPIError, sync_date, sync_team_history, upsert_fixtures,
)
from app.db import Fixture, Prediction, get_session, init_db
from app.predictor import MODEL_VERSION, PredictionResult, predict_match, format_prediction

logger = logging.getLogger(__name__)

DISCLAIMER = "仅供数据分析参考，不构成投注建议。"
TELEGRAM_MSG_LIMIT = 3500  # 留余量（Telegram 上限 4096）


# --------------------------------------------------------------------------- #
# 消息工具
# --------------------------------------------------------------------------- #

async def _reply(update: Update, text: str) -> None:
    """超长消息自动分段发送。"""
    if not text:
        return
    parts = []
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_MSG_LIMIT:
            parts.append(remaining)
            break
        # 在换行处尽量切分，避免截断一行
        cut = remaining.rfind("\n", 0, TELEGRAM_MSG_LIMIT)
        if cut <= 0:
            cut = TELEGRAM_MSG_LIMIT
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    for p in parts:
        await update.message.reply_text(p)


def _fmt_fixture(f: Fixture) -> str:
    when = f.start_time.strftime("%m-%d %H:%M") if f.start_time else "时间待定"
    return f"[{f.league}] {f.home} vs {f.away} @ {when}"


# --------------------------------------------------------------------------- #
# 命令：start / help
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
# 命令：today / tomorrow
# --------------------------------------------------------------------------- #

async def _sync_and_show(update: Update, target: date) -> None:
    try:
        written, fixtures = await sync_date(target)
    except FootballAPIError as e:
        await _reply(update, f"⚠️ 数据同步失败：{e}")
        return
    except Exception as e:  # noqa
        logger.exception("sync_date 异常")
        await _reply(update, f"⚠️ 同步异常：{e}")
        return

    session = get_session()
    try:
        rows = session.query(Fixture).filter(
            Fixture.start_time >= _day_start(target),
            Fixture.start_time <= _day_end(target),
        ).order_by(Fixture.start_time).all()
    finally:
        session.close()

    if not rows:
        await _reply(update, f"{target.isoformat()} 暂无配置联赛的比赛。")
        return

    lines = [f"📅 {target.isoformat()} 比赛 ({len(rows)} 场)："]
    for f in rows:
        lines.append(_fmt_fixture(f))
    await _reply(update, "\n".join(lines))


def _day_start(d: date):
    from datetime import datetime
    return datetime(d.year, d.month, d.day)


def _day_end(d: date):
    from datetime import datetime, timedelta
    return datetime(d.year, d.month, d.day) + timedelta(days=1) - timedelta(seconds=1)


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _sync_and_show(update, date.today())


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _sync_and_show(update, date.today() + timedelta(days=1))


# --------------------------------------------------------------------------- #
# 命令：predict
# --------------------------------------------------------------------------- #

def _history_from_db(home_team_id: int, away_team_id: int) -> list[dict]:
    """从本地数据库读取两队所有「已结束」比赛，转为 predictor schema。

    主队视角：该队出现在 home_team_id 位置；客队视角：出现在 away_team_id 位置。
    """
    session = get_session()
    try:
        rows = session.query(Fixture).filter(
            ((Fixture.home_team_id == home_team_id) | (Fixture.away_team_id == home_team_id))
            | ((Fixture.home_team_id == away_team_id) | (Fixture.away_team_id == away_team_id)),
            Fixture.status.in_(["FT", "AET", "PEN", "finished"]),
        ).all()
    finally:
        session.close()

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


def _save_prediction(fix: Fixture, result: PredictionResult) -> None:
    """保存预测；同 (fixture_id, model_version) 已存在则更新。"""
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

    target = date.today()
    session = get_session()
    try:
        fixtures = session.query(Fixture).filter(
            Fixture.start_time >= _day_start(target),
            Fixture.start_time <= _day_end(target),
        ).order_by(Fixture.start_time).all()
    finally:
        session.close()

    # 当日无比赛 → 先同步
    if not fixtures:
        try:
            await sync_date(target)
        except FootballAPIError as e:
            await _reply(update, f"⚠️ 同步赛程失败：{e}")
            return
        session = get_session()
        try:
            fixtures = session.query(Fixture).filter(
                Fixture.start_time >= _day_start(target),
                Fixture.start_time <= _day_end(target),
            ).order_by(Fixture.start_time).all()
        finally:
            session.close()

    if not fixtures:
        await _reply(update, "今日无配置联赛的比赛，暂无可预测项。")
        return

    blocks: list[str] = []
    for fix in fixtures:
        home_id = fix.home_team_id
        away_id = fix.away_team_id

        # 球队 ID 缺失 → 无法预测，绝不用 fix.id 顶替
        if not home_id or not away_id:
            blocks.append(
                f"⚠️ {fix.home} vs {fix.away}：缺少球队 ID（home={home_id}, away={away_id}），"
                "无法可靠预测。"
            )
            continue

        # 预测前先按真实球队 ID 同步历史（补齐刚部署时的空库）
        try:
            await sync_team_history(home_id, last=20)
            await sync_team_history(away_id, last=20)
        except FootballAPIError as e:
            blocks.append(f"⚠️ {fix.home} vs {fix.away}：历史同步失败 - {e}")
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
        text = (
            f"📊 {fix.home} vs {fix.away}\n"
            f"{format_prediction(result)}\n"
            f"{DISCLAIMER}"
        )
        blocks.append(text)

    await _reply(update, "\n\n".join(blocks))


# --------------------------------------------------------------------------- #
# 命令：status
# --------------------------------------------------------------------------- #

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    session = get_session()
    try:
        n_fixtures = session.query(Fixture).count()
        n_predictions = session.query(Prediction).count()
    finally:
        session.close()
    await _reply(update, (
        "✅ 机器人运行中\n"
        f"模型版本: {MODEL_VERSION}\n"
        f"数据库比赛数: {n_fixtures}\n"
        f"预测记录数: {n_predictions}\n"
        f"配置联赛: {settings.ENABLED_LEAGUES}\n"
        f"时区: {settings.TIMEZONE}"
    ))


# --------------------------------------------------------------------------- #
# 注册 / 构建
# --------------------------------------------------------------------------- #

def register_handlers(application, CommandHandler=None) -> None:
    """注册全部命令处理器。CommandHandler 可注入（测试用），默认取 telegram.ext。"""
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
    """构造并注册好 handler 的 Application。application / CommandHandler 供测试注入。"""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN 未配置。请在 Railway Variables 或 .env 中填入 Bot Token。"
        )
    init_db()
    if application is None:
        from telegram.ext import Application as _App
        application = _App.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    register_handlers(application, CommandHandler=CommandHandler)
    return application


__all__ = [
    "start", "help_command", "today", "tomorrow", "predict", "status",
    "register_handlers", "build_application",
]

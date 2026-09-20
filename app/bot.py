from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings
from app.data import FootballAPIError, sync_date
from app.predictor import MODEL_VERSION, predict_match, format_prediction
from app.db import Fixture, Prediction, get_session, init_db

DISCLAIMER = "仅供数据分析参考，不构成投注建议。"

TZ = ZoneInfo(get_settings().TIMEZONE)


def _now() -> datetime:
    return datetime.now(TZ)


def _chunks(text: str, limit: int = 3800) -> list[str]:
    """分段发送，避免 Telegram 单消息长度限制。"""
    return [text[i:i+limit] for i in range(0, len(text), limit)]


async def _reply(update, text: str):
    for part in _chunks(text):
        await update.message.reply_text(part, parse_mode=None)


async def cmd_start(update, context):
    await _reply(update, "⚽ 足球预测机器人\n\n/help 使用说明\n/today 今日比赛\n/tomorrow 明日比赛\n/predict 今日预测\n/status 运行状态")


async def cmd_help(update, context):
    await _reply(update, (
        "命令：\n/start 欢迎信息\n/help 使用说明\n/today 同步今日比赛\n/tomorrow 同步明日比赛\n"
        "/predict 今日可预测比赛\n/status 运行状态\n\n" + DISCLAIMER
    ))


async def _sync_and_list(target: date):
    try:
        count, _ = await sync_date(target)
    except FootballAPIError as e:
        return f"⚠️ 数据同步失败：{e}"
    session = get_session()
    try:
        rows = session.query(Fixture).filter(
            Fixture.start_time >= datetime.combine(target, datetime.min.time()),
            Fixture.start_time < datetime.combine(target + timedelta(days=1), datetime.min.time()),
        ).all()
        if not rows:
            return f"{target} 暂无已配置联赛的比赛。"
        lines = [f"📅 {target} 比赛（共 {len(rows)} 场）:"]
        for f in rows:
            lines.append(f"[{f.league}] {f.home} vs {f.away} @ {f.start_time.astimezone(TZ).strftime('%H:%M')}")
        return "\n".join(lines)
    finally:
        session.close()


async def cmd_today(update, context):
    text = await _sync_and_list(_now().date())
    await _reply(update, text)


async def cmd_tomorrow(update, context):
    text = await _sync_and_list(_now().date() + timedelta(days=1))
    await _reply(update, text)


async def cmd_predict(update, context):
    if not get_settings().PREDICTION_ENABLED:
        await _reply(update, "预测功能当前已关闭。")
        return
    today = _now().date()
    session = get_session()
    try:
        fixtures = session.query(Fixture).filter(
            Fixture.start_time >= datetime.combine(today, datetime.min.time()),
            Fixture.start_time < datetime.combine(today + timedelta(days=1), datetime.min.time()),
        ).all()
        if not fixtures:
            await _reply(update, "今日暂无比赛，请先用 /today 同步。")
            return

        lines = [f"🔮 {today} 预测：\n"]
        for fix in fixtures:
            # 历史数据不足时拒绝预测
            history = _load_history(session, fix.home, fix.away)
            result = predict_match(fix.id, fix.id, history)
            if result is None:
                lines.append(f"[{fix.league}] {fix.home} vs {fix.away}\n历史样本不足，暂不提供可靠预测\n")
                continue
            lines.append(f"[{fix.league}] {fix.home} vs {fix.away}\n{format_prediction(result)}\n")
            # 持久化预测
            _save_prediction(session, fix.id, result)
        session.commit()
        await _reply(update, "\n".join(lines) + f"\n{DISCLAIMER}")
    finally:
        session.close()


def _load_history(session, home: str, away: str) -> list[dict]:
    """从本地数据库读取两队历史已结束比赛。"""
    rows = session.query(Fixture).filter(
        Fixture.status.in_(["FT", "finished", "Match Finished"]),
    ).filter((Fixture.home == home) | (Fixture.away == home) | (Fixture.home == away) | (Fixture.away == away)).all()
    out = []
    for r in rows:
        out.append({
            "home_id": r.home, "away_id": r.away,
            "home_goals": r.home_score, "away_goals": r.away_score,
            "status": r.status,
        })
    return out


def _save_prediction(session, fixture_id: int, p):
    existing = session.query(Prediction).filter_by(fixture_id=fixture_id).first()
    if existing:
        return
    session.add(Prediction(
        fixture_id=fixture_id, model_version=p.model_version,
        home_prob=p.home_prob, draw_prob=p.draw_prob, away_prob=p.away_prob,
        expected_home_goals=p.expected_home_goals, expected_away_goals=p.expected_away_goals,
        predicted_score=p.predicted_score, confidence=p.confidence,
        data_completeness=p.data_completeness, evidence=p.evidence,
    ))


async def cmd_status(update, context):
    settings = get_settings()
    session = get_session()
    try:
        n_fixtures = session.query(Fixture).count()
        n_predictions = session.query(Prediction).count()
        msg = (
            "✅ 机器人运行中\n"
            f"模型版本: {MODEL_VERSION}\n"
            f"数据库比赛数: {n_fixtures}\n"
            f"预测记录数: {n_predictions}\n"
            f"配置联赛: {settings.ENABLED_LEAGUES}\n"
            f"时区: {settings.TIMEZONE}"
        )
        await _reply(update, msg)
    finally:
        session.close()


def build_application() -> Application:
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN，无法启动 Bot。请在环境变量中配置。")
    init_db()
    app = Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("predict", cmd_predict))
    app.add_handler(CommandHandler("status", cmd_status))
    return app


def run_polling():
    """本地开发用 polling 模式启动。"""
    application = build_application()
    application.run_polling()

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

async def _reply(update: Update, text: str, markdown: bool = False) -> None:
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
        await update.message.reply_text(p)


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
        "可用命令：\n"
        "/today - 今天赛程\n"
        "/tomorrow - 明天赛程\n"
        "/predict - 今日比赛预测\n"
        "/status - 运行状态\n"
        "/help - 使用说明\n\n"
        "提示：点击输入框旁的菜单按钮（或输入 /）可直接选择命令，无需手打。\n\n"
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
    table_rows = [
        [_local_time_text(getattr(f, "start_time", None)),
         _ellipsis(f.league or "-", 10),
         f"{_ellipsis(f.home or '?', 12)} vs {_ellipsis(f.away or '?', 12)}"]
        for f in rows
    ]
    header = f"📅 {target.isoformat()} 比赛（{len(rows)} 场，时间为 {tzname}）"

    # 按批发送，保证单个代码块不会被截断
    for i in range(0, len(table_rows), TABLE_BATCH):
        batch = table_rows[i:i + TABLE_BATCH]
        body = render_table(["时间", "联赛", "对阵"], batch,
                            aligns=["left", "left", "left"])
        prefix = header if i == 0 else None
        text = (_escape_md(prefix) + "\n" if prefix else "") + _code_block(body)
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
        header = f"📊 {target.isoformat()} 预测（{len(table_rows)} 场）"
        body = render_table(
            ["对阵", "主胜", "平", "客胜", "比分", "置信"],
            table_rows,
            aligns=["left", "right", "right", "right", "center", "center"],
        )
        text = _escape_md(header) + "\n" + _code_block(body)
        if details:
            text += "\n\n预测依据\n" + "\n".join(_escape_md(d) for d in details)
        text += "\n\n" + DISCLAIMER
        await _reply(update, text, markdown=True)

    if unavailable:
        await _reply(update, "⚠️ 未能预测：\n" + "\n".join(f"• {u}" for u in unavailable))

    if not table_rows and not unavailable:
        await _reply(update, notice.strip() or "今日无可预测项。")


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
    ("today", "今天赛程（表格）"),
    ("tomorrow", "明天赛程（表格）"),
    ("predict", "今日比赛预测（表格）"),
    ("status", "运行状态与数据库统计"),
    ("help", "使用说明与免责声明"),
]


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
    "start", "help_command", "today", "tomorrow", "predict", "status",
    "register_handlers", "build_application", "run_polling",
    "today_local", "DISCLAIMER", "FINISHED_STATUSES",
    "BOT_COMMANDS", "_set_bot_commands",
]

"""免费套餐能力探测：确认哪些端点在 Free 计划下真的可用。

背景
----
API-Football 官方定价页写明「所有套餐包含所有端点，Free 仅在**可用赛季**上受限」。
但文档与实际经常不一致（例如 2026 赛季的历史就取不到），
所以**必须用真实 key 打一次请求**来确认，而不是照文档假设。

探测目标
--------
1. ``/odds?date=...``    —— 能否拿到「当前赛季」的赛前赔率（决定能否接市场信号）
2. ``/odds?fixture=...`` —— 赔率的备用入口
3. ``/standings``        —— 能否拿到当前赛季积分榜（决定能否替换过期的 Elo）
4. ``/fixtures?date=...``—— 对照组（已知可用，用来区分"整体不可用"与"某端点不可用"）

配额控制
--------
总计最多 **5 次请求**，且失败即停对应分支，不会重试烧配额。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

MARKER = "PROBE_RESULT"
PROBE_DONE_FILE = "/tmp/.api_probe_done"
TIMEOUT = 12.0


def _get(path: str, params: dict) -> tuple[bool, str, object]:
    """发一次请求，返回 (是否成功, 说明, 原始数据)。绝不抛异常。"""
    settings = get_settings()
    if not settings.FOOTBALL_API_KEY:
        return False, "未配置 FOOTBALL_API_KEY", None
    url = f"{settings.FOOTBALL_API_BASE_URL.rstrip('/')}{path}"
    headers = {"x-apisports-key": settings.FOOTBALL_API_KEY,
               "Accept": "application/json"}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(url, headers=headers, params=params)
    except Exception as e:  # noqa: BLE001
        return False, f"网络异常: {type(e).__name__}", None

    if resp.status_code in (401, 403):
        return False, (
            f"HTTP {resp.status_code}（多为 key 无效或未订阅该端点）: "
            f"{resp.text[:120]}"
        ), None
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text[:160]}", None
    try:
        data = resp.json()
    except ValueError:
        return False, "返回非 JSON", None

    if isinstance(data, dict) and data.get("errors"):
        return False, f"API 错误: {data['errors']}", None
    return True, "OK", data


def _summarize_odds(data) -> str:
    """从 /odds 响应里提取可读摘要：多少场、多少博彩公司、哪些市场。"""
    rows = (data or {}).get("response") or []
    if not rows:
        return "0 场（响应为空）"
    books: set[str] = set()
    markets: set[str] = set()
    for r in rows[:20]:
        for b in r.get("bookmakers") or []:
            books.add(b.get("name", "?"))
            for m in b.get("bets") or []:
                markets.add(m.get("name", "?"))
    book_list = sorted(books)[:6]
    mkt = [m for m in sorted(markets) if "Winner" in m or "1X2" in m
           or "Match" in m][:3]
    return (f"{len(rows)} 场 · {len(books)} 家博彩 "
            f"({', '.join(book_list)}{'…' if len(books) > 6 else ''})"
            + (f" · 市场: {', '.join(mkt)}" if mkt else
               f" · 市场: {', '.join(sorted(markets)[:3])}"))


def _safe_get(path: str, params: dict) -> tuple[bool, str, object]:
    """``_get`` 的兜底包装：连 ``_get`` 自身抛异常也要变成结构化结果。

    探测是「锦上添花」的功能，任何情况下都不能把机器人拖崩。
    """
    try:
        return _get(path, params)
    except Exception as e:  # noqa: BLE001
        return False, f"探测异常: {type(e).__name__}: {e}", None


def run_probe(target: date | None = None) -> dict:
    """执行探测，返回结果字典（同时写日志，便于在 Railway 日志里直接看）。"""
    if target is None:
        target = date.today() + timedelta(days=1)

    settings = get_settings()
    leagues = settings.enabled_league_ids or [39]
    league = leagues[0]
    season = target.year if target.month >= 7 else target.year - 1
    day = target.isoformat()

    report: dict = {"date": day, "league": league, "season": season}

    # 1) 对照组：赛程（已知可用）
    ok, msg, data = _safe_get("/fixtures", {"date": day, "league": league})
    report["fixtures"] = {"ok": ok, "msg": msg}
    fixture_id = None
    if ok and data:
        rows = (data or {}).get("response") or []
        report["fixtures"]["count"] = len(rows)
        if rows:
            fixture_id = rows[0].get("fixture", {}).get("id")

    # 2) 主目标：按日期的赛前赔率
    ok, msg, data = _safe_get("/odds", {"date": day, "league": league})
    report["odds_by_date"] = {"ok": ok, "msg": msg}
    if ok:
        report["odds_by_date"]["summary"] = _summarize_odds(data)

    # 3) 备用：按 fixture 的赔率
    if fixture_id:
        ok, msg, data = _safe_get("/odds", {"fixture": fixture_id})
        report["odds_by_fixture"] = {"ok": ok, "msg": msg}
        if ok:
            report["odds_by_fixture"]["summary"] = _summarize_odds(data)
    else:
        report["odds_by_fixture"] = {"ok": False, "msg": "无可用 fixture id（跳过）"}

    # 4) 当前赛季积分榜
    ok, msg, data = _safe_get("/standings", {"league": league, "season": season})
    report["standings_current"] = {"ok": ok, "msg": msg}
    if ok and data:
        rows = (data or {}).get("response") or []
        if rows:
            league_info = rows[0].get("league", {}) or {}
            report["standings_current"]["summary"] = (
                f"{league_info.get('name', '?')} {season} · "
                f"{len(league_info.get('standings') or [[]])[0] if league_info.get('standings') else 0} 组"
            )

    # 5) 博彩公司列表（不算配额或极廉价，用来确认 odds 覆盖面）
    ok, msg, data = _safe_get("/odds/bookmakers", {})
    report["bookmakers"] = {"ok": ok, "msg": msg}
    if ok and data:
        report["bookmakers"]["count"] = len((data or {}).get("response") or [])

    _log_report(report)
    return report


def _log_report(report: dict) -> None:
    """把结果打成一眼能看懂的日志块（Railway 日志里可直接读）。"""
    lines = [
        "",
        "========== 免费套餐能力探测 ==========",
        f"日期 {report['date']} · 联赛 {report['league']} · 赛季 {report['season']}",
    ]
    checks = [
        ("赛程 /fixtures（对照）", "fixtures"),
        ("赔率 /odds?date", "odds_by_date"),
        ("赔率 /odds?fixture", "odds_by_fixture"),
        ("积分榜 /standings（当前赛季）", "standings_current"),
        ("博彩公司 /odds/bookmakers", "bookmakers"),
    ]
    for label, key in checks:
        item = report.get(key, {})
        flag = "可用" if item.get("ok") else "不可用"
        lines.append(f"  [{flag}] {label}")
        if not item.get("ok"):
            lines.append(f"       原因: {item.get('msg', '')[:150]}")
        elif item.get("summary"):
            lines.append(f"       {item['summary']}")
        elif item.get("count") is not None:
            lines.append(f"       共 {item['count']} 条")

    usable = [k for k in ("odds_by_date", "odds_by_fixture") if report.get(k, {}).get("ok")]
    lines.append("")
    if usable:
        lines.append("结论：赔率可用 → 可以接市场共识信号（提升空间最大）")
    else:
        lines.append("结论：赔率不可用 → 需换用外部赔率源（如 Odds-API.io 免费 100 次/小时）")
    lines.append("=====================================")
    logger.info("\n".join(lines))

    # 机器可读的一行摘要，方便 grep
    logger.info(
        "%s %s", MARKER,
        json.dumps({k: report.get(k, {}).get("ok") for k in
                    ("fixtures", "odds_by_date", "odds_by_fixture",
                     "standings_current", "bookmakers")},
                   ensure_ascii=False),
    )


def probe_once_on_start() -> None:
    """启动时探测一次；同一次部署内不重复（避免重启反复消耗配额）。"""
    if os.environ.get("PROBE_ON_START", "1").strip().lower() in ("0", "false", "no"):
        return
    if os.path.exists(PROBE_DONE_FILE):
        return
    try:
        run_probe()
    except Exception as e:  # noqa: BLE001 - 探测失败绝不能影响启动
        logger.warning("探测失败（不影响机器人运行）: %s", e)
    finally:
        try:
            with open(PROBE_DONE_FILE, "w") as f:
                f.write("done")
        except Exception:  # noqa: BLE001
            pass


__all__ = ["run_probe", "probe_once_on_start", "MARKER"]

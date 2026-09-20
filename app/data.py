"""API-Football 数据访问层。

职责
----
* 封装 ``/fixtures?date=...``（按日期）与 ``/fixtures?team=...&last=...``（按球队历史）。
* 请求头 ``x-apisports-key``，超时 20s。
* 统一解析为内部 dict schema（含真实球队 ID），仅保留 ``ENABLED_LEAGUES`` 联赛。
* ``upsert_fixtures``：按 ``external_id`` 去重写入。
* ``sync_date`` / ``sync_team_history``：供 bot 命令调用的异步入口。

错误一律抛 ``FootballAPIError``，绝不让机器人静默崩溃。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Optional

import httpx

from app.config import get_settings
from app.db import Fixture, get_session, init_db

# 有效「已结束」状态（不区分大小写）
_FINISHED_STATUSES = {"FT", "AET", "PEN", "FINISHED", "MATCH FINISHED"}


class FootballAPIError(Exception):
    """API-Football 调用失败（Key 缺失 / 网络 / HTTP / API errors）。"""


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #

def _parse_time(value) -> Optional[datetime]:
    """API 日期可能是 ISO 字符串或 datetime；统一归一化为 datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        s = value.strip().replace("Z", "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def _is_finished_status(status) -> bool:
    """判断 API 返回的 status 是否表示比赛已结束。"""
    if status is None:
        return False
    return str(status).strip().upper() in {s.upper() for s in _FINISHED_STATUSES}


# --------------------------------------------------------------------------- #
# 底层 HTTP
# --------------------------------------------------------------------------- #

def _headers() -> dict[str, str]:
    key = get_settings().FOOTBALL_API_KEY
    if not key:
        raise FootballAPIError("FOOTBALL_API_KEY 未配置，无法调用 API-Football。")
    return {"x-apisports-key": key, "Accept": "application/json"}


def _base_url() -> str:
    return (get_settings().FOOTBALL_API_BASE_URL or "https://v3.football.api-sports.io").rstrip("/")


def _request(path: str, params: Optional[dict] = None) -> dict:
    """同步 GET 请求。超时 20s，错误统一转 FootballAPIError。"""
    url = f"{_base_url()}{path}"
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers=_headers(), params=params or {})
    except httpx.TimeoutException as e:
        raise FootballAPIError(f"API-Football 请求超时: {e}") from e
    except httpx.HTTPError as e:
        raise FootballAPIError(f"API-Football 网络错误: {e}") from e

    if resp.status_code == 401 or resp.status_code == 403:
        raise FootballAPIError(f"API-Football 认证失败 (HTTP {resp.status_code})，请检查 FOOTBALL_API_KEY。")
    if resp.status_code >= 400:
        raise FootballAPIError(f"API-Football HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise FootballAPIError(f"API-Football 返回非 JSON: {resp.text[:200]}") from e

    if isinstance(data, dict) and data.get("errors"):
        raise FootballAPIError(f"API-Football 返回错误: {data['errors']}")
    return data


def _request_async(path: str, params: Optional[dict] = None) -> dict:
    """异步包装：在默认事件循环中运行同步 httpx（避免引入额外异步依赖）。"""
    return asyncio.to_thread(_request, path, params)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #

def _parse_fixture(item: dict, allowed_leagues: Optional[set[int]] = None) -> Optional[dict]:
    """将一个 API fixture 对象解析为内部统一 dict。

    schema::
        external_id, league, league_id, start_time, home_team_id, away_team_id,
        home, away, status, home_score, away_score
    """
    if not isinstance(item, dict):
        return None
    fix = item.get("fixture") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    league = item.get("league") or {}

    league_id = int(league.get("id") or 0)
    if allowed_leagues and league_id not in allowed_leagues:
        return None

    home_team = teams.get("home") or {}
    away_team = teams.get("away") or {}

    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    return {
        "external_id": _int(fix.get("id")),
        "league": str(league.get("name") or ""),
        "league_id": league_id,
        "start_time": _parse_time(fix.get("date")),
        "home_team_id": _int(home_team.get("id")),
        "away_team_id": _int(away_team.get("id")),
        "home": str(home_team.get("name") or ""),
        "away": str(away_team.get("name") or ""),
        "status": str((fix.get("status") or {}).get("short") or ""),
        "home_score": goals.get("home"),
        "away_score": goals.get("away"),
    }


def fixtures_by_date(target: date) -> list[dict]:
    """按日期获取比赛（已过滤 ENABLED_LEAGUES）。返回内部 dict 列表。"""
    settings = get_settings()
    allowed = set(settings.enabled_league_ids)
    data = _request("/fixtures", params={"date": target.isoformat()})
    raw = (data.get("response") or []) if isinstance(data, dict) else []
    out: list[dict] = []
    for item in raw:
        parsed = _parse_fixture(item, allowed or None)
        if parsed and parsed["external_id"]:
            out.append(parsed)
    return out


def team_fixtures(team_id: int, last: int = 20) -> list[dict]:
    """按球队 ID 获取最近 ``last`` 场比赛（含历史，已结束 + 未结束混合）。

    调用方（predictor）自行按 status 过滤未结束比赛。
    """
    if not team_id:
        return []
    data = _request("/fixtures", params={"team": int(team_id), "last": int(last)})
    raw = (data.get("response") or []) if isinstance(data, dict) else []
    allowed = set(get_settings().enabled_league_ids)
    out: list[dict] = []
    for item in raw:
        parsed = _parse_fixture(item, allowed or None)
        if parsed and parsed["external_id"]:
            out.append(parsed)
    return out


def upsert_fixtures(rows: list[dict]) -> int:
    """按 ``external_id`` 更新或插入，返回新增/更新条数。"""
    if not rows:
        return 0
    init_db()
    session = get_session()
    try:
        count = 0
        for r in rows:
            ext = r.get("external_id")
            if not ext:
                continue
            existing = session.query(Fixture).filter(Fixture.external_id == ext).one_or_none()
            if existing:
                for k, v in r.items():
                    if k == "id":
                        continue
                    setattr(existing, k, v)
            else:
                session.add(Fixture(**r))
            count += 1
        session.commit()
        return count
    finally:
        session.close()


async def sync_date(target: date) -> tuple[int, list[Fixture]]:
    """同步指定日期的比赛到数据库。返回 (写入条数, Fixture 列表)。"""
    rows = await _request_async("/fixtures", params={"date": target.isoformat()})
    # _request_async 返回的是 dict，需要解析
    if isinstance(rows, dict):
        raw = rows.get("response") or []
        allowed = set(get_settings().enabled_league_ids)
        rows = [_parse_fixture(item, allowed or None) for item in raw]
        rows = [r for r in rows if r and r["external_id"]]
    written = upsert_fixtures(rows)

    session = get_session()
    try:
        fixtures = session.query(Fixture).filter(
            Fixture.start_time >= datetime(target.year, target.month, target.day),
            Fixture.start_time < datetime(target.year, target.month, target.day, 23, 59, 59),
        ).all()
        # 脱离 session 使用，返回副本
        result = [Fixture(**f.__dict__) for f in fixtures]
    finally:
        session.close()
    return written, result


async def sync_team_history(team_id: int, last: int = 20) -> int:
    """同步某队的近期比赛到数据库（供 /predict 前补充历史数据）。返回写入条数。"""
    if not team_id:
        return 0
    rows = team_fixtures(team_id, last=last)
    return upsert_fixtures(rows)


# 供 bot.py 按规范名称调用
sync_team_fixtures = sync_team_history


__all__ = [
    "FootballAPIError",
    "fixtures_by_date",
    "team_fixtures",
    "upsert_fixtures",
    "sync_date",
    "sync_team_history",
    "sync_team_fixtures",
    "_parse_fixture",
    "_is_finished_status",
]

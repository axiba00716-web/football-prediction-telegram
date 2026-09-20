"""API-Football 数据访问层。

职责
----
* 封装 ``/fixtures?date=YYYY-MM-DD``（按日期）与 ``/fixtures?team=<id>&last=<n>``（球队历史）。
* 请求头 ``x-apisports-key``，所有请求带 timeout。
* 统一解析为内部 dict schema（含**真实球队 ID**），按 ``external_id`` 去重写入。
* ``sync_date`` / ``sync_team_history``：供 bot 命令调用的异步入口。

错误一律抛 ``FootballAPIError``，绝不让 Telegram 命令无响应。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import httpx

from app.config import get_settings
from app.db import Fixture, get_session, init_db

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
DEFAULT_BASE_URL = "https://v3.football.api-sports.io"

# 有效「已结束」状态（不区分大小写）——与 predictor 保持一致
_FINISHED_STATUSES = {"FT", "AET", "PEN", "FINISHED", "MATCH FINISHED"}


class FootballAPIError(Exception):
    """API-Football 调用失败（Key 缺失 / 网络 / HTTP / JSON / API errors）。"""


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #

def _parse_time(value) -> Optional[datetime]:
    """把 API 日期统一归一化为 datetime。

    兼容 API-Football 常见格式::

        2026-09-20T15:00:00Z
        2026-09-20T15:00:00+00:00
        2026-09-20 15:00:00

    优先 ``datetime.fromisoformat(value.replace("Z", "+00:00"))``，
    失败再回退显式 strptime。解析失败返回 ``None``。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # 优先：fromisoformat（原生处理 +00:00 / Z 偏移）
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        # 回退：显式格式
        s2 = s.replace("Z", "").replace("+00:00", "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s2, fmt)
            except ValueError:
                continue
    return None


def _is_finished_status(status) -> bool:
    """判断 API 返回的 status 是否表示比赛已结束。"""
    if status is None:
        return False
    return str(status).strip().upper() in {s.upper() for s in _FINISHED_STATUSES}


# --------------------------------------------------------------------------- #
# 底层 HTTP（可被测试 patch）
# --------------------------------------------------------------------------- #

def _http_get(path: str, params: Optional[dict], api_key: str, base_url: str,
              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """真正发起 GET 的地方。所有异常统一转 FootballAPIError。"""
    if not api_key:
        raise FootballAPIError("FOOTBALL_API_KEY 未配置，无法调用 API-Football。")

    url = f"{base_url.rstrip('/')}{path}"
    headers = {"x-apisports-key": api_key, "Accept": "application/json"}
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers, params=params or {})
    except httpx.TimeoutException as e:
        raise FootballAPIError(f"API-Football 请求超时: {e}") from e
    except httpx.HTTPError as e:
        raise FootballAPIError(f"API-Football 网络错误: {e}") from e

    if resp.status_code in (401, 403):
        raise FootballAPIError(
            f"API-Football 认证失败 (HTTP {resp.status_code})，请检查 FOOTBALL_API_KEY。"
        )
    if resp.status_code >= 400:
        raise FootballAPIError(f"API-Football HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise FootballAPIError(f"API-Football 返回非 JSON: {resp.text[:200]}") from e

    if isinstance(data, dict) and data.get("errors"):
        raise FootballAPIError(f"API-Football 返回错误: {data['errors']}")
    return data


# --------------------------------------------------------------------------- #
# FootballAPI
# --------------------------------------------------------------------------- #

class FootballAPI:
    """API-Football 客户端。配置全部来自 ``get_settings()``。"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timezone: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.FOOTBALL_API_KEY
        self.base_url = (base_url or settings.FOOTBALL_API_BASE_URL or DEFAULT_BASE_URL).rstrip("/")
        self.timezone = timezone or settings.TIMEZONE or "UTC"
        self.timeout = timeout

    # -- HTTP ------------------------------------------------------------- #

    def _headers(self) -> dict:
        """请求头（Key 缺失时抛 FootballAPIError）。"""
        if not self.api_key:
            raise FootballAPIError("FOOTBALL_API_KEY 未配置，无法调用 API-Football。")
        return {"x-apisports-key": self.api_key, "Accept": "application/json"}

    def _request(self, path: str, params: Optional[dict] = None) -> dict:
        """同步 GET。测试可直接 patch 本方法，无需真实网络。"""
        return _http_get(path, params, self.api_key, self.base_url, self.timeout)

    async def request(self, path: str, params: Optional[dict] = None) -> dict:
        """异步 GET：在线程中执行同步请求，避免阻塞 Telegram 事件循环。"""
        return await asyncio.to_thread(self._request, path, params)

    # -- 业务接口 --------------------------------------------------------- #

    async def fixtures_by_date(self, target: date) -> list[dict]:
        """``GET /fixtures?date=YYYY-MM-DD``，只保留 ENABLED_LEAGUES。"""
        allowed = set(get_settings().enabled_league_ids)
        data = await self.request("/fixtures", {"date": target.isoformat()})
        return self._rows(data, allowed or None)

    async def team_fixtures(self, team_id: int, last: int = 20,
                            allowed_leagues: Optional[set[int]] = None) -> list[dict]:
        """``GET /fixtures?team=<id>&last=<n>``。

        默认**不按联赛过滤**：球队历史可能来自杯赛/其他联赛，必须能被预测器使用。
        """
        if not team_id:
            return []
        data = await self.request("/fixtures", {"team": int(team_id), "last": int(last)})
        return self._rows(data, allowed_leagues)

    # -- 工具 ------------------------------------------------------------- #

    @staticmethod
    def _rows(data: dict, allowed: Optional[set[int]]) -> list[dict]:
        raw = (data.get("response") or []) if isinstance(data, dict) else []
        out: list[dict] = []
        for item in raw:
            parsed = _parse_fixture(item, allowed)
            if parsed and parsed["external_id"]:
                out.append(parsed)
        return out


def _parse_fixture(item: dict, allowed_leagues: Optional[set[int]] = None) -> Optional[dict]:
    """把一个 API fixture 对象解析为内部统一 dict。

    schema::

        external_id, league, league_id, start_time, home_team_id, away_team_id,
        home, away, status, home_score, away_score

    ``home_team_id`` / ``away_team_id`` 取自 ``item["teams"]["home"]["id"]`` /
    ``item["teams"]["away"]["id"]``，即 **API-Football 真实球队 ID**。
    """
    if not isinstance(item, dict):
        return None
    fix = item.get("fixture") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    league = item.get("league") or {}

    league_id = int(league.get("id") or 0)
    if allowed_leagues is not None and league_id not in allowed_leagues:
        return None

    home_team = teams.get("home") or {}
    away_team = teams.get("away") or {}

    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def _score(v) -> Optional[int]:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

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
        "home_score": _score(goals.get("home")),
        "away_score": _score(goals.get("away")),
    }


def _request(path: str, params: Optional[dict] = None) -> dict:
    """模块级同步请求入口（向后兼容，内部走 FootballAPI）。"""
    return FootballAPI()._request(path, params)


async def _request_async(path: str, params: Optional[dict] = None) -> dict:
    """模块级异步请求入口。"""
    return await FootballAPI().request(path, params)


# --------------------------------------------------------------------------- #
# 模块级业务函数（bot.py 使用的正式接口）
# --------------------------------------------------------------------------- #

async def fixtures_by_date(target: date) -> list[dict]:
    """按日期获取比赛（已过滤 ENABLED_LEAGUES）。"""
    return await FootballAPI().fixtures_by_date(target)


async def team_fixtures(team_id: int, last: int = 20,
                        allowed_leagues: Optional[set[int]] = None) -> list[dict]:
    """按球队 ID 获取最近 ``last`` 场比赛（默认不限联赛，保证历史可用）。"""
    return await FootballAPI().team_fixtures(team_id, last=last,
                                             allowed_leagues=allowed_leagues)


def upsert_fixtures(rows: list[dict]) -> int:
    """按 ``external_id`` 更新或插入，返回新增/更新条数。Session 显式关闭。"""
    if not rows:
        return 0
    init_db()
    session = get_session()
    count = 0
    try:
        for r in rows:
            ext = r.get("external_id")
            if not ext:
                continue
            existing = session.query(Fixture).filter(Fixture.external_id == ext).one_or_none()
            if existing:
                for k, v in r.items():
                    if k in ("id", "external_id"):
                        continue
                    setattr(existing, k, v)
            else:
                session.add(Fixture(**r))
            count += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return count


def _day_window(target: date) -> tuple[datetime, datetime]:
    """当天时间窗口（左闭右开）。

    数据库存的是 UTC 时间，与 API-Football ``date=`` 参数同一口径：
    ``[target 00:00, target+1 00:00)``，确保覆盖当天 23:xx 开赛的比赛。
    """
    day_start = datetime(target.year, target.month, target.day)
    return day_start, day_start + timedelta(days=1)


async def sync_date(target: date) -> tuple[int, list[dict]]:
    """同步指定日期的比赛到数据库。返回 ``(写入条数, 当天比赛行列表)``。

    * 只保留 ENABLED_LEAGUES；
    * 按 external_id 去重写入；
    * 返回当天窗口内的全部比赛（行以 dict 形式给出，避免 Session 关闭后
      触发 DetachedInstanceError）。
    """
    rows = await fixtures_by_date(target)
    written = upsert_fixtures(rows)

    day_start, day_end = _day_window(target)
    session = get_session()
    try:
        fixtures = session.query(Fixture).filter(
            Fixture.start_time >= day_start,
            Fixture.start_time < day_end,
        ).order_by(Fixture.start_time).all()
        result = [
            {c.name: getattr(f, c.name) for c in f.__table__.columns}
            for f in fixtures
        ]
    finally:
        session.close()
    return written, result


async def sync_team_history(team_id: int, last: int = 20) -> int:
    """同步某队近期比赛，返回写入/更新条数。

    说明：
    * 不按联赛过滤，保证杯赛/跨联赛历史也能被预测器使用；
    * 未结束（NS/TBD/…）的比赛同样入库，但预测器只统计已结束场次。
    """
    if not team_id:
        return 0
    rows = await team_fixtures(team_id, last=last)
    return upsert_fixtures(rows)


# 兼容别名
sync_team_fixtures = sync_team_history


__all__ = [
    "FootballAPIError", "FootballAPI",
    "_parse_time", "_parse_fixture", "_is_finished_status", "_day_window",
    "fixtures_by_date", "team_fixtures", "upsert_fixtures",
    "sync_date", "sync_team_history", "sync_team_fixtures",
]

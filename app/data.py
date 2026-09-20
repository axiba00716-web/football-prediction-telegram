from datetime import datetime, date
from typing import Optional
import httpx

from app.config import get_settings
from app.db import Fixture, get_session


class FootballAPIError(Exception):
    pass


class FootballAPI:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, timeout: float = 15.0):
        settings = get_settings()
        self.api_key = api_key or settings.FOOTBALL_API_KEY
        self.base_url = (base_url or settings.FOOTBALL_API_BASE_URL).rstrip("/")
        self.timeout = timeout
        if not self.api_key:
            raise FootballAPIError(
                "缺少 FOOTBALL_API_KEY，请在 Railway Variables 或 .env 中配置 API-Football Key。"
            )

    @staticmethod
    def _fmt(dt: date) -> str:
        return dt.strftime("%Y-%m-%d")

    async def _get(self, path: str, params: dict) -> dict:
        headers = {"x-apisports-key": self.api_key}
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()
        if not data.get("response"):
            # API-Football 在配额/错误时可能返回 errors 字段
            if data.get("errors"):
                raise FootballAPIError(f"API-Football 返回错误: {data['errors']}")
        return data

    async def fixtures_by_date(self, target: date) -> list[dict]:
        """获取指定日期的比赛（仅保留已配置联赛）。"""
        settings = get_settings()
        data = await self._get("/fixtures", {"date": self._fmt(target)})
        allowed = set(settings.enabled_league_ids)
        out = []
        for item in data.get("response", []):
            fix = item.get("fixture", {})
            league_id = item.get("league", {}).get("id")
            if league_id not in allowed:
                continue
            teams = item.get("teams", {})
            goals = item.get("goals", {})
            out.append({
                "external_id": fix.get("id"),
                "league": item.get("league", {}).get("name", ""),
                "start_time": datetime.fromisoformat(fix.get("date").replace("Z", "+00:00")),
                "home": teams.get("home", {}).get("name", ""),
                "away": teams.get("away", {}).get("name", ""),
                "status": fix.get("status", {}).get("short", "scheduled"),
                "home_score": goals.get("home"),
                "away_score": goals.get("away"),
            })
        return out

    async def team_fixtures(self, team_id: int, last: int = 20) -> list[dict]:
        """获取某支球队的历史比赛（用于建模）。"""
        data = await self._get("/fixtures", {"team": team_id, "last": last})
        return data.get("response", [])


def upsert_fixtures(rows: list[dict]) -> int:
    """按 external_id 更新或插入，返回新增/更新条数。"""
    if not rows:
        return 0
    session = get_session()
    try:
        count = 0
        for row in rows:
            existing = session.query(Fixture).filter_by(external_id=row["external_id"]).first()
            if existing:
                for k, v in row.items():
                    setattr(existing, k, v)
            else:
                session.add(Fixture(**row))
            count += 1
        session.commit()
        return count
    finally:
        session.close()


async def sync_date(target: date) -> tuple[int, list[Fixture]]:
    """同步某天比赛入库，返回 (数量, Fixture 对象列表)。"""
    api = FootballAPI()
    rows = await api.fixtures_by_date(target)
    upsert_fixtures(rows)
    session = get_session()
    try:
        fixtures = session.query(Fixture).filter(
            Fixture.start_time >= datetime.combine(target, datetime.min.time()),
        ).all()
        return len(rows), list(fixtures)
    finally:
        session.close()

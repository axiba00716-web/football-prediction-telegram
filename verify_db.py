"""独立 DB 验证（在干净子进程中运行，规避主进程 engine 单例缓存）。

被 verify_all.py 第 10 项调用。成功时输出 'DB_VERIFY_OK'。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DB_PATH = os.environ.get("VERIFY_DB_PATH", str(ROOT / ".verify_idem.db"))
if os.path.exists(DB_PATH):
    os.unlink(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from app.db import init_db, Fixture, Prediction, get_session  # noqa: E402

init_db()
init_db()  # 幂等：重复调用不报错

s = get_session()
try:
    f = Fixture(
        external_id=1, home="Home", away="Away",
        home_team_id=10, away_team_id=20, status="NS",
    )
    s.add(f)
    s.commit()
    s.refresh(f)

    assert s.query(Fixture).count() == 1, "Fixture 应写入 1 条"
    assert f.home_team_id == 10 and f.away_team_id == 20, "team id 应正确持久化"

    s.add(Prediction(
        fixture_id=f.id, model_version="baseline-poisson-v1",
        home_prob=0.5, draw_prob=0.25, away_prob=0.25,
        expected_home_goals=1.5, expected_away_goals=1.2,
        predicted_score="1-1", confidence="中",
        data_completeness=1.0, evidence="verify",
    ))
    s.commit()
    assert s.query(Prediction).count() == 1, "Prediction 应写入 1 条"
finally:
    s.close()

print("DB_VERIFY_OK")

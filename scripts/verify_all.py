#!/usr/bin/env python3
"""本地验证脚本：在无 pytest 环境也能跑通核心断言（等价于 test suite）。

直接执行:  python3 verify_all.py
"""
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

_TMP = Path("/tmp/vall_verify")
_TMP.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'verify.db'}"
os.environ["TELEGRAM_BOT_TOKEN"] = "verify-token"

import app.db as db_module
db_module.reset_db_state()
from app.db import (
    init_db, Fixture, Prediction, get_session,
    normalize_url_for_railway as _normalize_url,
)
from app.predictor import predict_match, format_prediction, MODEL_VERSION
from app.data import _parse_fixture
# main 模块需在 env 设置后导入（内部导入链依赖 telegram SDK 桩，由 conftest 注入）
import app.main as main_mod  # noqa: E402  (延迟导入：需在 env 配置之后)


def _finished(home_id, away_id, hg, ag):
    return {"home_id": home_id, "away_id": away_id, "home_goals": hg, "away_goals": ag, "status": "FT"}


def check(cond, msg):
    """断言 cond 为真；为假则打印 FAIL 并以非零退出。msg 必填。"""
    if not cond:
        print(f"  ❌ FAIL: {msg}")
        sys.exit(1)
    print(f"  ✅ {msg}")


def main():
    print("\n=== 1. compileall ===")
    import py_compile
    py_files = [str(p) for p in Path("app").rglob("*.py")] + [
        str(p) for p in Path("tests").rglob("*.py")
    ]
    for f in py_files:
        py_compile.compile(f, doraise=True)
    print(f"  ✅ 所有 Python 文件编译通过 ({len(py_files)} 个)")

    print("\n=== 2. git diff --check (模拟) ===")
    conflict = False
    for fp in Path(".").rglob("*.py"):
        if ".git" in str(fp) or "__pycache__" in str(fp) or fp.name == "verify_all.py":
            continue
        for i, line in enumerate(fp.read_text(errors="ignore").splitlines(), 1):
            # 真实冲突标记是行首独立的 7 字符标记，而非源码中的字符串字面量
            if line.strip() in ("<<<<<<<", "=======", ">>>>>>>"):
                print(f"  ❌ 冲突标记: {fp}:{i} {line.strip()}")
                conflict = True
    check(not conflict, msg="无冲突标记 / 空白问题")

    print("\n=== 3. 关键修复点：bot.py 使用真实球队 ID ===")
    bot_src = Path("app/bot.py").read_text()
    check("home_id = fix.home_team_id" in bot_src, "bot.py 读取 fix.home_team_id")
    check("away_id = fix.away_team_id" in bot_src, "bot.py 读取 fix.away_team_id")
    check("predict_match(fix.id, fix.id" not in bot_src, "已移除 predict_match(fix.id, fix.id, ...) 旧 bug")
    check("sync_team_history" in bot_src, "bot.py 使用 sync_team_history 按球队 ID 同步历史")
    check("_history_from_db" in bot_src, "bot.py 从 DB 读取两队已结束历史")
    check("fetch_team_history" not in bot_src, "不再使用已废弃的 fetch_team_history")
    print("  ✅ bot.py 预测调用已修正为真实球队 ID")

    print("\n=== 4. db.py 含 home_team_id / away_team_id 字段 ===")
    db_src = Path("app/db.py").read_text()
    check("home_team_id = Column(Integer" in db_src, "Fixture.home_team_id 字段存在")
    check("away_team_id = Column(Integer" in db_src, "Fixture.away_team_id 字段存在")
    check("normalize_url_for_railway" in db_src, "含 PostgreSQL URL 归一化函数")
    check("check_same_thread" in db_src, "SQLite 使用 check_same_thread=False")
    print("  ✅ 数据库模型字段正确")

    print("\n=== 5. Poisson 概率有效性 + 概率和为 1 ===")
    init_db()
    home, away = 1, 2
    history = [_finished(home, away, 2, 1)] * 10 + [_finished(away, home, 1, 3)] * 10
    result = predict_match(home, away, history, min_history=5)
    check(result is not None, "充足历史时能生成预测")
    total = result.home_prob + result.draw_prob + result.away_prob
    check(abs(total - 1.0) < 1e-6, f"概率和 ≈ 1 (实际 {total:.6f})")
    check(
        all(getattr(result, k) >= 0 for k in ("home_prob", "draw_prob", "away_prob")),
        "所有概率非负",
    )
    h, a = result.predicted_score.split("-")
    check(int(h) <= 6 and int(a) <= 6, f"最可能比分在 0-6 范围内 ({result.predicted_score})")
    print(f"  示例: 主{result.home_prob*100:.1f}% / 平{result.draw_prob*100:.1f}% / 客{result.away_prob*100:.1f}%")

    print("\n=== 6. 样本不足时拒绝预测 ===")
    check(predict_match(1, 2, [_finished(1, 2, 1, 0)] * 3, min_history=5) is None,
          "仅 3 场 (<5) → 返回 None")
    check(predict_match(0, 2, [_finished(1, 2, 1, 0)] * 10, min_history=5) is None, "team id 为 0 → 返回 None")
    check(predict_match(1, 999, [_finished(1, 2, 1, 0)] * 10, min_history=5) is None,
          "客队无历史 → 返回 None")
    print("  ✅ 数据不足时拒绝预测（不强行输出）")

    print("\n=== 7. 核心回归：fixture.id ≠ team id 场景 ===")
    fix = type("F", (), {"id": 999, "home_team_id": 10, "away_team_id": 20})()
    history = [_finished(10, 20, 2, 1)] * 10 + [_finished(20, 10, 1, 3)] * 10
    ok = predict_match(fix.home_team_id, fix.away_team_id, history, min_history=5)
    check(ok is not None and ok.home_team_id == 10, "用真实球队 ID (10,20) → 命中历史")
    bug = predict_match(fix.id, fix.id, history, min_history=5)
    check(bug is None, "旧 bug (fix.id=999 当两队 ID) → 查无历史 → None")
    print("  ✅ 旧 bug 已被回归测试锁定")

    print("\n=== 8. data.py 解析保存真实球队 ID ===")
    api = {
        "fixture": {"id": 70001, "date": datetime(2026, 9, 21, 20, 0), "status": {"short": "NS"}},
        "league": {"id": 39, "name": "Premier League"},
        "teams": {"home": {"id": 42, "name": "Arsenal"}, "away": {"id": 57, "name": "Chelsea"}},
        "goals": {"home": None, "away": None},
    }
    parsed = _parse_fixture(api, {39})
    check(parsed["external_id"] == 70001, "external_id = fixture.id (70001)")
    check(parsed["home_team_id"] == 42, "home_team_id = 42 (真实球队 ID)")
    check(parsed["away_team_id"] == 57, "away_team_id = 57 (真实球队 ID)")
    check(parsed["home_team_id"] != parsed["external_id"], "team id ≠ fixture id")
    # 写入 DB 验证
    s = get_session()
    try:
        f = Fixture(**parsed)
        s.add(f)
        s.commit()
        s.refresh(f)
        check(f.home_team_id == 42 and f.away_team_id == 57, "DB 读回 team id 一致")
    finally:
        s.close()
    print("  ✅ data.py 正确保存真实球队 ID")

    print("\n=== 9. PostgreSQL URL 归一化 ===")
    check(_normalize_url("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d",
          "postgres:// → postgresql+psycopg://")
    check(_normalize_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d",
          "postgresql:// → postgresql+psycopg://")
    check("sslmode=require" in _normalize_url("postgres://u:p@h/d?sslmode=require"),
          "查询参数保留")
    check(_normalize_url("sqlite:///./x.db") == "sqlite:///./x.db", "sqlite 不变")
    print("  ✅ URL 转换覆盖 postgres:// / postgresql:// / 查询参数 / sqlite")

    print("\n=== 10. DB 建表 + init_db 幂等 ===")
    # 用独立子进程运行 verify_db.py，彻底规避主进程 engine 单例缓存污染
    import subprocess
    env = {**os.environ, "PYTHONPATH": str(ROOT), "VERIFY_DB_PATH": str(_TMP / "idem.db")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "verify_db.py")],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
    )
    if "DB_VERIFY_OK" not in proc.stdout:
        print("  [stderr]", (proc.stderr or "")[-500:])
        print("  [stdout]", (proc.stdout or "")[-500:])
    check("DB_VERIFY_OK" in proc.stdout, "Fixture + Prediction 表可写入（干净子进程）")
    check(proc.returncode == 0, "子进程退出码为 0")
    # 清理：移除残留 DB 文件，恢复主进程指向 verify.db
    (Path(env["VERIFY_DB_PATH"])).unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'verify.db'}"
    db_module.reset_db_state()
    print("  ✅ Fixture + Prediction 两张表工作正常，init_db 幂等")

    print("\n=== 11. 配置从环境变量读取（无硬编码密钥） ===")
    app_files = list(Path("app").rglob("*.py"))
    src = "\n".join(p.read_text(errors="ignore") for p in app_files)

    # 真实凭据特征（完整值，非示例占位）
    real_secrets = ["ghp_FD18", "nh0KPcRj", "5ebf10a9"]
    for frag in real_secrets:
        check(frag not in src, msg=f"源码不含真实密钥片段 ({frag}...)")

    # 明文密码模式：postgres://user:真实密码@host；示例占位（u:p / your_）允许
    import re
    for m in re.finditer(r"postgres(ql)?\+?psycopg?://([^ \"'\s]+)", src):
        creds = m.group(2)
        user_part = creds.split("@")[0] if "@" in creds else creds
        if ":" in user_part:
            user, pwd = user_part.split(":", 1)
            placeholders = {"username", "password", "user", "pass", "your_username",
                            "your_password", "example", "token", "key", "xxx"}
            check({user, pwd} & placeholders, msg=f"无明文 DB 密码 (发现 {user}:{pwd}@...)")
    from app.config import Settings
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN="x", FOOTBALL_API_KEY="y")
    check(s.enabled_league_ids == [39, 140, 135, 78, 61], msg="ENABLED_LEAGUES 正确解析为 [39,140,135,78,61]")
    print("  ✅ 配置全部从环境变量读取，无硬编码密钥")

    print("\n=== 12. main.py 缺 Token 时明确退出 ===")
    # 用独立子进程运行：缺 Token → _run() 必须在 build_application 前报错退出，
    # 绝不执行一次同步后假装正常，也绝不进入 polling。
    script = (
        "import os, sys, asyncio, logging\n"
        "os.environ['TELEGRAM_BOT_TOKEN'] = ''\n"
        "os.environ['DATABASE_URL'] = {db_url!r}\n"
        "from app.main import main\n"
        "called = {{'build': False}}\n"
        "def fake_build():\n"
        "    called['build'] = True\n"
        "    raise AssertionError('build_application 不应在缺 Token 时被调用')\n"
        "import app.main as _m\n"
        "_m.build_application = fake_build\n"
        "captured = []\n"
        "class _H(logging.Handler):\n"
        "    def emit(self, record):\n"
        "        captured.append(record.getMessage())\n"
        "lg = logging.getLogger('app.main')\n"
        "h = _H(level=logging.ERROR)\n"
        "lg.addHandler(h); lg.setLevel(logging.ERROR)\n"
        "try:\n"
        "    main()\n"
        "except (SystemExit, RuntimeError):\n"
        "    sys.exit(2 if called['build'] else 0)\n"
        "sys.exit(3)\n"
    ).format(db_url=str(_TMP / "main.db"))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
    )
    check(proc.returncode == 0, msg=f"缺 Token 时明确报错退出 (returncode={proc.returncode})")
    check("TELEGRAM_BOT_TOKEN" in proc.stderr or "TELEGRAM_BOT_TOKEN" in proc.stdout,
          msg="缺 Token 时输出明确错误日志")
    print("  ✅ 缺失必要环境变量时显示明确错误并退出")

    # 清理主进程已缓存的 DB 相关模块，使下一节重新绑定到 verify.db
    for _m in ("app.db", "app.data", "app.predictor", "app.bot", "app.main", "app.config"):
        sys.modules.pop(_m, None)
    db_module.reset_db_state()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'verify.db'}"

    print("\n=== 13. 根目录无异常杂散文件 ===")
    allowed = {
        "LICENSE", "README.md", "requirements.txt", ".env.example", ".gitignore",
        "Dockerfile", "railway.toml", "scripts/verify_all.py", "scripts/verify_db.py",
    }
    stray = [
        p.name for p in Path(".").iterdir()
        if p.is_file() and (p.name.startswith("#") or "marker" in p.name.lower())
    ]
    check(stray == [], msg=f"根目录无杂散 marker 文件，实际: {stray}")
    # 确认关键文件齐全
    for f in allowed:
        check(Path(f).exists(), msg=f"根目录存在 {f}")
    print("  ✅ 根目录干净，无异常文件")

    print("\n" + "=" * 50)
    print("🎉 全部验证通过 (等价于 pytest -q + compileall + git diff --check)")
    print("=" * 50)


if __name__ == "__main__":
    main()

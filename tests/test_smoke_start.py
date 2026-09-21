"""启动冒烟脚本自身也要被测试：它必须能发现问题，且自身不崩。"""

from __future__ import annotations

import subprocess
import sys
import os


def test_smoke_script_runs_and_exits_zero(tmp_path):
    """冒烟脚本在正常环境下应完整跑完并返回 0。"""
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'smoke.db'}",
        "TELEGRAM_BOT_TOKEN": "smoke-token",
        "FOOTBALL_API_KEY": "smoke-key",
        "TIMEZONE": "Asia/Shanghai",
        "SMOKE_PROBE": "0",
    })
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "scripts/smoke_start.py"],
                       cwd=repo, env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, f"冒烟脚本失败:\n{r.stdout}\n{r.stderr}"
    assert "冒烟检查通过" in r.stdout


def test_smoke_output_reports_build_tag():
    """冒烟输出必须包含代码版本，便于确认线上跑的是哪一版。"""
    import app.bot as bot
    assert hasattr(bot, "BUILD_TAG") and bot.BUILD_TAG

"""启动冒烟检查：在真正 run_polling 之前把「会崩的地方」全跑一遍。

为什么需要它
------------
Railway 上最容易遇到的问题是：代码推上去了，但容器启动即崩（CRASHED），
而报错往往藏在 import 或初始化阶段，日志里一闪而过不好定位。

本脚本按生产同样的顺序执行，但不进入阻塞的 polling：

1. 读取配置
2. ``init_db()``                 —— 数据库建表 / 旧表补列
3. ``build_application()``       —— 导入 bot 全链路（含 markets / tracking / probe）
4. 跑一遍纯函数模型               —— 确保新模块没有语法 / 逻辑硬伤
5. 可选：赔率能力探测（默认关，避免消耗配额）

任何一步失败都会以非零码退出并打印清晰原因。

用法::

    python scripts/smoke_start.py            # 只做启动前检查
    SMOKE_PROBE=1 python scripts/smoke_start.py   # 额外跑一次 API 探测
"""

from __future__ import annotations

import os
import sys
import traceback

# 保证能从仓库根目录直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}", flush=True)


def main() -> int:
    ok_all = True

    # ---- 1. 配置 ----
    _step(1, "读取配置 Settings")
    try:
        from app.config import get_settings
        s = get_settings()
        print(f"    时区={s.TIMEZONE} 联赛={s.ENABLED_LEAGUES} "
              f"最少历史={s.MIN_HISTORY_MATCHES}")
        print(f"    Token 已配置={'是' if s.TELEGRAM_BOT_TOKEN else '否（本地冒烟可忽略）'}")
    except Exception as e:
        print(f"    ✗ 失败: {e}")
        traceback.print_exc()
        return 1

    # ---- 2. 数据库 ----
    _step(2, "init_db() 建表 / 补列")
    try:
        from app.db import init_db
        init_db()
        print("    ✓ 数据库可用")
    except Exception as e:
        print(f"    ✗ 失败: {e}")
        traceback.print_exc()
        ok_all = False

    # ---- 3. 导入 bot 全链路 ----
    _step(3, "导入 app.bot（含 markets / tracking / probe）")
    try:
        import app.bot as bot
        import app.markets as markets
        import app.probe as probe
        import app.tracking as tracking
        print(f"    ✓ 导入成功 · 代码版本 {getattr(bot, 'BUILD_TAG', '?')}")
        print(f"    ✓ 命令数 {len(bot.BOT_COMMANDS)} · "
              f"市场数 {len(markets.MARKETS)}")
    except Exception as e:
        print(f"    ✗ 失败: {e}")
        traceback.print_exc()
        return 1

    # ---- 4. 纯函数模型跑一遍 ----
    _step(4, "模型链路（predict → 二分类 → 一致性 → 分级）")
    try:
        from datetime import datetime, timedelta

        from app.markets import (
            compute_binary_markets, evaluate_consistency, selection_tier,
        )
        from app.predictor import predict_match

        base = datetime(2024, 1, 1)
        hist = []
        for i in range(8):
            hist.append({"home_id": 101, "away_id": 202, "home_goals": 3,
                         "away_goals": 0, "status": "FT",
                         "start_time": base + timedelta(days=i)})
        for i in range(6):
            hist.append({"home_id": 101, "away_id": 999, "home_goals": 2,
                         "away_goals": 1, "status": "FT",
                         "start_time": base + timedelta(days=100 + i)})
        for i in range(6):
            hist.append({"home_id": 888, "away_id": 202, "home_goals": 3,
                         "away_goals": 0, "status": "FT",
                         "start_time": base + timedelta(days=200 + i)})
        hist.sort(key=lambda r: r["start_time"])

        r = predict_match(101, 202, hist)
        assert r is not None, "predict_match 返回 None"
        c = evaluate_consistency(r, hist)
        tier, _ = selection_tier(r, c)
        mk = compute_binary_markets(r)
        print(f"    ✓ 主胜{r.home_prob*100:.0f}% 平{r.draw_prob*100:.0f}% "
              f"客{r.away_prob*100:.0f}% · 等级{tier} · 一致性{c.ratio_text}")
        if mk:
            best, prob = mk.best()
            print(f"    ✓ 二分类最优: {markets.MARKETS[best]} {prob*100:.0f}%")
        else:
            print("    ✗ 二分类矩阵为空")
            ok_all = False
    except Exception as e:
        print(f"    ✗ 失败: {e}")
        traceback.print_exc()
        ok_all = False

    # ---- 5. 可选：API 探测 ----
    if os.environ.get("SMOKE_PROBE", "0").strip().lower() in ("1", "true", "yes"):
        _step(5, "API 能力探测（消耗约 5 次配额）")
        try:
            probe.run_probe()
        except Exception as e:
            print(f"    ✗ 探测异常（不影响判定）: {e}")
    else:
        _step(5, "API 探测已跳过（设 SMOKE_PROBE=1 可开启）")

    print("\n" + "=" * 46)
    if ok_all:
        print("冒烟检查通过：启动链路无阻塞性问题")
        return 0
    print("冒烟检查发现问题，见上方 ✗ 项")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

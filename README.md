# 足球预测 Telegram 机器人 (MVP)

基于 [API-Football](https://www.api-football.com/) 数据，用 Poisson 基线模型对足球比赛进行胜/平/负概率预测，并通过 Telegram Bot 推送。

> ⚠️ **仅供数据分析参考，不构成任何投注建议，不保证盈利或预测准确率。**

---

## 功能

- Telegram 命令：`/start` `/help` `/today` `/tomorrow` `/predict` `/status`
- 通过 API-Football 拉取赛程，按 `ENABLED_LEAGUES` 过滤并入库（SQLite 本地 / PostgreSQL Railway）
- **Poisson 基线模型**：主胜/平局/客胜概率、预期进球、最可能比分、置信度、数据完整度、文字依据
- **样本不足时拒绝预测**（`MIN_HISTORY_MATCHES`，默认 5 场），绝不强行输出
- 历史只用「已结束比赛」，防止未来数据泄漏
- LLM 功能预留接口但默认关闭
- 不自动下注

## 项目结构

```
football-prediction-telegram/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile
├── railway.toml
├── app/
│   ├── __init__.py
│   ├── config.py        # pydantic-settings，全部从环境变量读取
│   ├── db.py            # SQLAlchemy：Fixture / Prediction 两张表
│   ├── data.py          # API-Football 客户端 + 归一化
│   ├── predictor.py     # Poisson 模型
│   ├── bot.py           # Telegram 命令处理器
│   └── main.py          # 启动入口（polling）
├── scripts/
│   ├── pre-commit-check.sh  # compileall + pytest + git diff --check
│   └── verify_all.py        # 无 pytest 环境下的等价校验
└── tests/
    ├── conftest.py
    ├── test_predictor.py    # Poisson 模型（纯函数）
    ├── test_db.py           # URL 归一化 / 字段 / 建表
    ├── test_data_parsing.py # 时间解析 / 球队 ID / sync
    ├── test_main.py         # 同步入口 / polling
    └── test_bot.py          # /predict 使用真实球队 ID
```

## 快速开始（本地）

```bash
# 1. 准备环境
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 然后编辑 .env，填入 TELEGRAM_BOT_TOKEN 与 FOOTBALL_API_KEY

# 3. 运行
python -m app.main
```

## 环境变量

| 变量 | 说明 | 是否必填 |
|------|------|---------|
| `TELEGRAM_BOT_TOKEN` | @BotFather 创建的 Bot Token | ✅ 生产必填 |
| `TELEGRAM_ADMIN_CHAT_ID` | 管理员 Chat ID（预留） | 可选 |
| `FOOTBALL_API_KEY` | API-Football API Key | ✅ 生产必填 |
| `FOOTBALL_API_BASE_URL` | API 地址，默认 `https://v3.football.api-sports.io` | 默认即可 |
| `DATABASE_URL` | 数据库连接，本地默认 SQLite；Railway 由 PostgreSQL 插件注入 | 见说明 |
| `TIMEZONE` | 时区，默认 `Asia/Shanghai` | 可选 |
| `ENABLED_LEAGUES` | 启用联赛 ID，逗号分隔，默认 `39,140,135,78,61` | 可选 |
| `MIN_HISTORY_MATCHES` | 最低历史场次，默认 5 | 可选 |
| `PREDICTION_ENABLED` | 是否开启预测，默认 true | 可选 |
| `LLM_*` | LLM 配置，暂未启用，留空 | 可选 |

## 创建 Telegram Bot

1. Telegram 搜索 `@BotFather`，发送 `/newbot`
2. 按提示设置名称（如 `Football Predictor`）和用户名（须以 `bot` 结尾）
3. 保存返回的 Token → 填入 `TELEGRAM_BOT_TOKEN`

## 申请 API-Football Key

1. 注册 https://www.api-football.com/ （免费套餐 100 次/天，足够测试）
2. 登录 Dashboard → API Key 复制
3. 填入 `FOOTBALL_API_KEY`

## Railway 部署

1. 登录 https://railway.com/，New Project → **Deploy from GitHub Repo**
2. 授权 GitHub，选择本仓库 `football-prediction-telegram`，分支 `main`
3. Railway 自动识别 `Dockerfile` + `railway.toml`，构建命令为 `python -m app.main`
4. 在同一 Project 点 **Add** → **Database** → **PostgreSQL**，等待创建
5. 打开 PostgreSQL 服务的 Variables，复制 `DATABASE_URL`
6. 打开机器人服务的 **Variables**，添加：

```
TELEGRAM_BOT_TOKEN=你的Bot Token
FOOTBALL_API_KEY=你的API-Football Key
DATABASE_URL=上一步复制的PostgreSQL连接串
ENABLED_LEAGUES=39,140,135,78,61
MIN_HISTORY_MATCHES=5
PREDICTION_ENABLED=true
TIMEZONE=Asia/Shanghai
```

> 若 Railway 支持服务变量引用，可让机器人服务直接引用 PostgreSQL 的 `DATABASE_URL`，无需手动复制。

7. 回到机器人服务，点 **Deploy / Redeploy**，查看 **Deployments → Logs**
8. 正常启动日志：
   ```
   Database initialized.
   Starting Telegram bot in polling mode
   ```

> **首次部署请使用全新数据库。** 本项目处于 MVP 阶段，数据表结构调整频繁；
> 若复用早期 SQLite/PostgreSQL 旧库，`init_db()` 会做一次 best-effort 补列
> （`ALTER TABLE ADD COLUMN`）以避免缺字段导致启动失败，但不保证历史数据口径一致。
> 旧库建议直接删除重建，或改用 Railway 新建的 PostgreSQL 实例。

### 常见日志排查

| 日志 | 原因 |
|------|------|
| `TELEGRAM_BOT_TOKEN 未配置` | Token 未注入机器人服务 Variables |
| `FOOTBALL_API_KEY 未配置` | API Key 未注入（不影响 /start、/status，但 /predict 无数据） |
| 数据库连接错误 | `DATABASE_URL` 未复制到机器人服务（仅存在于 PostgreSQL 服务不算注入） |

## 时间口径说明

「今天 / 明天」一律按**比赛时间所在时区** `TIMEZONE`（默认 `Asia/Shanghai`）计算，
不依赖服务器本机时间（Railway 容器为 UTC）。全项目只有一条时间规则：

1. **API 请求带 `timezone`**：`/fixtures?date=YYYY-MM-DD&timezone=<TIMEZONE>`，
   让 API-Football 按该时区切分「这一天」，一次请求即可覆盖凌晨场；
2. **解析保留偏移**：API 时间先解析为 aware datetime（`+00:00` / `+08:00` / `Z`）；
3. **入库统一 UTC naive**：aware 折算到 UTC 后去掉 `tzinfo` 再存库，
   naive 一律视为已经是 UTC —— 库里只有一种表示，**绝不 naive/aware 混用**；
4. **筛选按本地日折算**：把本地那一天的 `[00:00, 次日 00:00)` 换算成 UTC
   窗口再比较，例如东八区 9/20 → UTC `[9/19 16:00, 9/20 16:00)`；
5. **展示转回本地时区**：`start_time.astimezone(TZ).strftime("%Y-%m-%d %H:%M")`。

因此北京时间 `00:00–08:00` 使用 `/today` 也不会拿到前一天或后一天的赛程：

```text
TIMEZONE=Asia/Shanghai，容器本机 UTC
/today → API: date=2026-09-20&timezone=Asia/Shanghai
         显示为 2026-09-20 00:30 / 2026-09-20 18:00（库里存 UTC 16:30 / 10:00）
```

## API 免费套餐限制

API-Football 免费套餐有两个硬限制，代码已针对性处理：

- **不支持 `last` 参数**（会返回 `Free plans do not have access to the Last parameter`）。
  因此球队历史改用 `season` 拉取整季数据，再在本地按开赛时间截取最近 N 场**已结束**的比赛。
- **每天 100 次请求**。`/predict` 会把当天涉及的球队去重后批量同步，并对同一球队做
  30 分钟进程内缓存；单轮最多预测 8 场，避免一次命令耗尽当天配额。

## 三口径预测体系（各自独立统计）

同一个模型，三种**互不混算**的口径：

| 口径 | 命令 | 含义 | 覆盖率 |
|---|---|---|---|
| `full_1x2` | `/predict` | 全量胜平负，每场都预测 | 100% |
| `selected_1x2` | `/select` | 精选胜平负，仅 A/B 级 | 通常 < 30% |
| `binary` | `/binary` | 二分类市场（主队不败/大于1.5球/双方进球…） | 视门槛 |

**铁律**：精选准确率不能代表全量能力。`/stats` 会**同时显示准确率与覆盖率**，
避免把"只挑少量比赛"的结果误认为全量预测能力。

### 二分类概率从比分矩阵精确求和

不是独立猜测，而是对同一份 Dixon-Coles 比分矩阵求和：

- 主队不败 = 主胜 + 平局
- 大于 1.5 球 = 1 − P(0球) − P(1球)
- 小于 4.5 球 = P(总进球 ≤ 4)
- 双方进球 = P(主队>0 且 客队>0)

「主队不败命中」**不等于**「主胜命中」，二者分开记录、分开统计。

### 模型一致性投票（3/3）

精选要求三路**不同方法**方向一致，不是同一个模型算三遍：

1. Elo 分差（logistic）
2. Dixon-Coles 泊松矩阵（主模型）
3. 攻防强度泊松（进攻强度 × 防守强度）

### 精选分级

| 等级 | 最高概率 | 概率差 | 一致性 | 样本 |
|---|---|---|---|---|
| A | ≥ 60% | ≥ 18% | 3/3 | ≥ 12 |
| B | ≥ 50% | ≥ 10% | ≥ 2/3 | ≥ 6 |
| C | 未达 B 级门槛 → 有预测但标注「不建议参考」 | | | |
| — | 样本 < 6 → 数据不足，不推送 | | | |

### 赛后自动结算

`/select`、`/binary`、`/stats` 都会先调用结算：扫描已结束且有比分的比赛，
按 `prediction_type` + `prediction_market` 逐条判定命中，写入 `is_correct`。

## Telegram 测试命令

```
/start       → 欢迎信息与命令列表
/help        → 使用说明
/today       → 同步并显示今天比赛
/tomorrow    → 同步并显示明天比赛
/predict     → 对今天比赛生成预测（样本不足时提示「历史样本不足，暂不提供可靠预测」）
/status      → 运行状态、比赛数、预测数、模型版本、配置联赛、时区
```

## 安全说明

- **严禁**将 `.env`、Token、API Key、数据库密码提交到 GitHub
- 已通过 `.gitignore` 忽略 `.env`、`*.db` 等敏感文件
- 推荐 Railway Variables 存放所有密钥，不在代码中硬编码
- Token 若泄露，立即在 @BotFather / API-Football Dashboard 撤销并轮换

## 免责声明

本项目为技术演示，**不保证盈利、命中率或预测结果**。模型仅为 Poisson 基线，未考虑伤病、阵容、天气、盘口变化等因子，请理性看待。

## 开发

```bash
# 运行测试
pytest -q

# 语法检查
python -m compileall app

# 检查空白/行尾问题
git diff --check
```

测试不依赖真实 Telegram Token 或 API-Football Key（使用环境变量隔离 + 临时 SQLite）。

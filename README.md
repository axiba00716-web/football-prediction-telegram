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

- `/today` 的「今天」按 `TIMEZONE`（默认 `Asia/Shanghai`）计算；
- 数据库保存的时间为 **UTC**，与 API-Football `date=` 参数同一口径，
  当天筛选窗口为 `[00:00, 次日 00:00)`，覆盖当天深夜开赛的比赛；
- 命令输出中的开赛时间为 UTC，阅读时请自行 +8（东八区）。

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

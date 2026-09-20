# 足球比赛赛程及预测 · Telegram Bot

一个基于 Python 3.11 的 Telegram 足球预测机器人 MVP：拉取 [API-Football](https://www.api-football.com/) 赛程，用 **Poisson 基线模型**计算胜/平/负概率、预期进球与最可能比分，通过 Telegram Bot 推送，结果存入 SQLite / PostgreSQL。

> ⚠️ 仅供数据分析参考，不构成投注建议。本项目**不保证盈利、命中率或预测结果**，也不含自动下注功能。

## 功能

- `python-telegram-bot` 运行 Bot（polling 模式）
- API-Football 获取赛程与历史数据
- SQLAlchemy 持久化（本地 SQLite，Railway 上 PostgreSQL）
- Poisson 模型：主胜/平/客胜概率、预期进球、最可能比分、置信度、数据完整度、文字依据
- **历史数据不足时拒绝预测**，防止强行输出
- AI/LLM 功能预留接口但默认关闭，不影响核心流程

## Telegram 命令

| 命令 | 说明 |
|------|------|
| `/start` | 欢迎信息与命令列表 |
| `/help` | 使用说明 |
| `/today` | 同步并显示今日比赛 |
| `/tomorrow` | 同步并显示明日比赛 |
| `/predict` | 显示今日可预测比赛 |
| `/status` | 运行状态、比赛数、模型版本 |

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
│   ├── config.py        # 环境变量配置
│   ├── db.py            # SQLAlchemy 模型 + 引擎
│   ├── data.py          # API-Football 客户端 + 入库
│   ├── predictor.py     # Poisson 预测模型
│   ├── bot.py           # Telegram 命令处理器
│   └── main.py          # 入口 + 每日同步
└── tests/
    └── test_predictor.py
```

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env       # 填入下方环境变量
python -m app.main
```

## 环境变量

复制 `.env.example` 为 `.env` 并填写：

| 变量 | 说明 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | @BotFather 创建的 Bot Token |
| `TELEGRAM_ADMIN_CHAT_ID` | 管理员 Chat ID |
| `FOOTBALL_API_KEY` | API-Football Key |
| `FOOTBALL_API_BASE_URL` | 默认 `https://v3.football.api-sports.io` |
| `DATABASE_URL` | 默认 `sqlite:///./football.db` |
| `TIMEZONE` | 默认 `Asia/Shanghai` |
| `ENABLED_LEAGUES` | 启用的联赛 ID，逗号分隔（默认 `39,140,135,78,61`） |
| `MIN_HISTORY_MATCHES` | 单队最少历史场次，默认 `5` |
| `PREDICTION_ENABLED` | `true/false` |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | LLM（暂未启用，留空即可） |

**严禁**将 `.env`、Token、API Key、数据库密码提交到仓库。

## 创建 Telegram Bot

1. 在 Telegram 搜索 `@BotFather`，发送 `/newbot`
2. 按提示设置名字与用户名，获得 **Bot Token**
3. 填入 `TELEGRAM_BOT_TOKEN`
4. 本地运行后向 Bot 发送 `/start` 测试

## 申请 API-Football Key

1. 注册 https://www.api-football.com/ ，免费套餐足够 MVP 使用
2. 在 Dashboard → API Key 获取 Key
3. 填入 `FOOTBALL_API_KEY`

## Railway 部署

1. 将仓库推送到 GitHub
2. 在 [Railway](https://railway.app/) 新建项目 → Deploy from GitHub → 选择本仓库
3. Railway 会自动识别 `Dockerfile` 与 `railway.toml`
4. 在 **Variables** 中添加以下环境变量（**不要写进仓库**）：
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ADMIN_CHAT_ID`
   - `FOOTBALL_API_KEY`
   - `DATABASE_URL`（见下方，Railway PostgreSQL 自动注入）
   - `PREDICTION_ENABLED=true`
5. 添加 PostgreSQL 插件，Railway 会自动注入 `DATABASE_URL`（`postgres://...`），代码自动转换为 `postgresql+psycopg://`
6. 部署后查看日志确认 `Starting Telegram bot`

### Railway PostgreSQL 配置

- 在项目中 Add Service → Database → PostgreSQL
- Railway 会自动设置环境变量 `DATABASE_URL`
- 代码中的 `db.py` 会自动把 `postgres://` 转为 SQLAlchemy 驱动格式，无需手动修改

## 测试

```bash
pytest -q
python -m compileall app
```

测试不依赖真实 Telegram Token 或 API-Football Key。

## 安全说明

- 所有凭据仅通过环境变量读取，`.env` 已在 `.gitignore`
- 仓库中不含任何真实 Token / Key / 密码
- 预测结果一律附带"仅供数据分析参考，不构成投注建议"
- 不含自动下注逻辑

## 免责声明

足球比赛结果具有高度不确定性。本项目的预测基于历史数据的统计模型，**不保证准确率、命中率或任何盈利**，仅供参考学习，不构成任何投注建议。请理性对待。

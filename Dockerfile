FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 健康检查：数据库可访问即视为存活（polling 模式下无 HTTP 端口）
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from app.db import get_engine; get_engine().connect()" || exit 1

CMD ["python", "-m", "app.main"]

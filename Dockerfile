# 約會行程規劃（v2）：api（網頁＋REST API＋/mcp）、worker、cron、migrate 共用這個映像，
# 各自的啟動指令寫在 docker-compose.yml。用法見 README「用 Docker 部署」。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Taipei

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY data/network ./data/network
COPY data/seed ./data/seed
COPY alembic.ini ./
COPY migrations ./migrations

# 用一般使用者執行
RUN useradd --create-home --uid 1000 app && mkdir -p data/cache && chown -R app data/cache
USER app

EXPOSE 8000

# 預設啟動 api；--proxy-headers 讓限流拿到使用者的真實 IP（前面的 nginx 會設定 X-Forwarded-For）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

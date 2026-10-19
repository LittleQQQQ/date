# 約會一條龍：網站＋REST API＋MCP 端點（同一個服務）
# 用法見 README「用 Docker 部署」；通常搭配 docker-compose.yml 的 PostgreSQL 一起跑
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
COPY data/fixture ./data/fixture

EXPOSE 8000

# 只開一個 worker：背景更新爬蟲寫在網站程序裡，開多個會有好幾份爬蟲同時抓資料、寫資料庫
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# 給 Claude 的專案說明

約會行程規劃（v2）：FastAPI ＋ PostgreSQL（PostGIS）＋ Redis（RQ）。功能、架構、部署的完整說明在 `README.md`。

## 規則

- **一律用繁體中文（台灣）**：回覆、文件、程式註解、commit 訊息都是，不要出現簡體字。
- 規劃大一點的改動前，先說明做法，等使用者確認再寫程式。
- `alembic.ini` 只能有英數字（Windows 預設用 cp950 讀，中文會讀不進來）。
- 資料表的時間一律帶時區（timestamptz）；`app/timeutil.py` 的 `now_tw()` 回傳帶時區的台灣時間。
- **Google 條款**：Google 來源的餐廳只能存 place_id 和 30 天內的座標，店名、地址等內容不能存進 PostgreSQL 或 Redis（資料表有 CHECK 擋著，路線快取也不存地點名稱）。
- 呼叫外部服務（TDX、Google、TypeSafe Jev）之前先看官方文件，並遵守 `app/redis_utils/` 的限流和每月額度。
- 測試不能連外部服務、不能用到 `.env` 裡的金鑰（`tests/conftest.py` 已經清空）。
- 地標、類別、票價目前是暫定資料（`data/seed/`）。

## 第一次建環境（Windows）

需要先裝好：Git、Python 3.10 以上、Docker Desktop（要開著）。Ollama 選用。
在專案資料夾（有 `docker-compose.yml` 的這一層）依序執行：

1. 開 PostGIS（5433）和 Redis（6380）：`docker compose -f compose.dev.yml up -d --wait`
2. 建虛擬環境、裝套件：`py -3 -m venv .venv`，再 `.venv\Scripts\python.exe -m pip install -r requirements.txt`
3. 設定檔：複製 `.env.example` 為 `.env`。不用填任何金鑰也能跑，這時餐廳只用 OSM、交通用估計值、Jev 用模擬版。
4. 建資料表：`.venv\Scripts\alembic.exe upgrade head`
5. 匯入資料，依序執行 `.venv\Scripts\python.exe -m app.ingest <名稱>`：`network` → `seed` → `atmovies`（約 3～4 分鐘）→ `osm`（約 3～4 分鐘，Overpass 忙的時候可能失敗，再跑一次就好）。
6. 跑測試確認：`.venv\Scripts\python.exe -m pytest`（全部要通過）
7. 啟動：`.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`，另一個視窗開 worker：`.venv\Scripts\python.exe -m app.jobs.worker --simple`。打開 http://127.0.0.1:8000 排一次行程。

注意：

- 5433、6380、8000 埠被占用時，改 `compose.dev.yml` 的埠和 `.env` 的 `DATABASE_URL`、`REDIS_URL`，或 uvicorn 的 `--port`。
- RQ 的 worker 要 fork，Windows 只能用 `--simple`（執行中的規劃取消不了，會跑完）；正式環境用 Docker（見 README「用 Docker 部署」）。
- 金鑰（`TDX_CLIENT_ID`、`TDX_CLIENT_SECRET`、`GOOGLE_PLACES_API_KEY`、`TYPESAFE_API_KEY`）由使用者自己填進 `.env`，`.env` 不會進 git。不要替使用者填金鑰，也不要把金鑰寫進其他檔案。
- 填了 Google 金鑰時，第一次先把三個 `GOOGLE_*_MONTHLY_LIMIT` 設小（例如 10），跑 `python -m app.ingest google` 確認用量正常。

## 常用

- 改了 `app/models/` 之後：`.venv\Scripts\alembic.exe revision --autogenerate -m "說明"`，檢查產生的檔案再 `upgrade head`。
- 程式碼結構見 `README.md` 的「專案結構」。
- 規格書（《後端規格書》v0.2）不在這個 repo 裡，需要的話向專案負責人拿。

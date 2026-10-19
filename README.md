# 約會行程規劃（Jev 生活場景應用 12）

選好區域、出發地、結束後要去哪、日期和出發時間，系統排出「電影 ＋ 餐廳 ＋ 大眾運輸」的完整時間軸，
再請 Jev 依時間安排、交通方便、預算打分數，列出最好的 5 個行程。網頁、REST API 和 MCP 共用同一套規劃。

- **範圍**：桃園的八德、桃園、中壢、青埔 4 個區域。出發地和終點從各區的地標清單選。
- **資料**：電影場次來自開眼電影網；餐廳來自 OpenStreetMap，填了金鑰再加上 Google 地圖；交通用自家路網計算，填了金鑰會每週用交通部 TDX 的班表與票價更新。
- **Jev**：沒有 TypeSafe 金鑰時用規則模擬版。Jev 沒把握時交給本機的 LLM（Ollama）。
- **暫定資料**：地標、類別、影城票價目前是暫定值（`data/seed/`），等團隊提供正式資料後替換。

本版（v2）照《後端規格書》v0.2（不在這個 repo 裡）改寫，技術是 FastAPI ＋ PostgreSQL（PostGIS）＋ Redis（RQ 佇列、SSE 進度、限流、每月額度）。
改寫前的舊版保留在 git 標籤 `v1-before-spec`（main）、`v1-before-spec-all`（加上 `feat/jev-llm-assist`、`feat/tdx-transit`）。

## 架構

```
網頁（frontend/）──┐                          ┌─ MCP 用戶端（Claude Desktop、MCP Inspector…）
                    ▼                          ▼
             nginx（proxy，對外 8000）── 關閉 SSE 與 /mcp 的緩衝、轉送使用者 IP
                    ▼
             api（FastAPI）：網頁、/api（REST＋SSE）、/mcp（Streamable HTTP）、IP 限流
                    │  規劃排進 Redis 佇列，進度和結果經 Redis Pub/Sub 推回來
                    ▼
             worker（RQ）：規劃（組候選 → 路網算交通 → 補 Google 店家資料 → Jev 打分數）
                    │       抓資料（開眼、OSM、TDX、Google）與寫入時的 Jev 判斷
                    ▼
             PostgreSQL＋PostGIS（db）        cron（rq cron）：定時把抓資料的工作排進佇列
                                              Ollama：Jev 沒把握時的 LLM 備援
```

| 服務 | 內容 |
|---|---|
| `proxy` | nginx。對外只開這一個埠；在 compose 網路裡也叫 `app`，舊版 Cloudflare 通道的服務網址 `app:8000` 不用改 |
| `api` | 網頁、REST API、MCP。限流：每個 IP 查詢每 60 秒 60 次、規劃每小時 10 次且同時只能 1 個 |
| `worker` | 處理 `plans`（優先）和 `crawl` 兩個佇列。啟動時同步路網與暫定資料，補抓今天還沒抓的場次 |
| `worker-plans` | 只處理 `plans`：抓資料一次要好幾分鐘，規劃不用排在它後面 |
| `cron` | 台灣時間每天 03:00 重新判斷過期資料與更新 Google 座標、06:00 開眼與 OSM；每週一 04:00 TDX、05:00 Google 找餐廳 |
| `migrate` | 啟動時建立或升級資料表（Alembic），做完就結束 |
| `db`、`redis` | PostGIS 16、Redis 7.4（記憶體上限 256 MB、滿了拒絕寫入、每 60 秒存檔） |
| `ollama`、`tunnel`、`quick-tunnel` | 選用，用 `--profile` 開啟 |

## 用 Docker 部署

需要 Docker Desktop（Windows、macOS）或 Docker Engine（Linux）。

1. 複製設定檔範本，至少填好 `DB_PASSWORD`（自己取一個）：
   ```powershell
   copy .env.docker.example .env
   ```
   範本裡每一項都有說明：`APP_PORT`（對外的埠）、`OLLAMA_URL`（LLM）、`CLOUDFLARE_TUNNEL_TOKEN`（固定網址）、`MCP_ALLOWED_HOSTS`（對外網域）、TDX 和 Google 的金鑰等。`.env` 不會進 git，也不會打包進映像。
2. 建置並啟動：
   ```bash
   docker compose up -d --build
   ```
3. 第一次啟動時 worker 會抓開眼場次、OSM 餐廳（有 TDX 金鑰再加上班表），大約 5～10 分鐘。看進度：
   ```bash
   docker compose logs -f worker
   ```
   打開 `http://localhost:8000/api/coverage`，各區有 `showtime_to` 日期就可以排行程了。
4. 打開 `http://localhost:8000`。

**LLM 備援**（Jev 沒把握時），二選一：

- **用這台電腦上裝好的 Ollama（推薦）**：先安裝 Ollama 並執行 `ollama pull qwen2.5:3b`。`.env` 的 `OLLAMA_URL` 保留預設的 `http://host.docker.internal:11434`，容器就會連到電腦上的 Ollama，用得到顯示卡。
- **用 compose 裡的 Ollama 容器**：`.env` 改成 `OLLAMA_URL=http://ollama:11434`，啟動時加 `--profile llm`，第一次再下載模型：
  ```bash
  docker compose --profile llm up -d --build
  ```
  ```bash
  docker compose exec ollama ollama pull qwen2.5:3b
  ```

`OLLAMA_URL` 留空就不用 LLM：Jev 沒把握的答案照樣使用，隔天再重新判斷。

**常用指令**

| 要做什麼 | 指令 |
|---|---|
| 更新程式後重建 | `git pull` 之後 `docker compose up -d --build`（資料表會自動升級） |
| 停止（資料保留） | `docker compose down` |
| 連資料一起刪掉 | `docker compose down -v` |
| 看各服務狀態 | `docker compose ps` |
| 手動補抓 | `docker compose exec worker python -m app.ingest atmovies`（或 `osm`、`tdx`、`google`） |
| 健康檢查 | `http://localhost:8000/api/health` |

**注意**

- 資料存在 Docker volume（`pgdata`：資料庫；`redis`：佇列、額度計數、快取），`docker compose down` 不會刪，`down -v` 才會。
- 這一版的專案名稱是 `date-v2`，跟舊版（`date`）的容器和資料分開，兩版的資料表不相容。舊版的資料用不到時，可以自己用 `docker compose -p date down -v` 刪掉。
- api、worker 可以開多個（進度經 Redis 傳遞，連到哪個 api 都收得到）。cron 開多個也有鎖保護，但開一個就夠。api 重建後 nginx 會自己找到新的容器，不用重開。
- 開眼、OpenStreetMap 是否會擋雲端主機的連線，要在那台主機上實際跑一次確認。
- **限流看的是使用者 IP**：經過 Cloudflare 通道時，nginx 從 Cloudflare 帶的 `CF-Connecting-IP` 取得真實 IP，每個人分開計算。Docker Desktop（Windows、macOS）上，從同一個網路直接連進來的人看起來都是同一個 IP，會共用限流（例如整個網路同時只能有一個規劃）；Linux 主機通常沒有這個問題。另外，同一個網路的人可以自己帶 `CF-Connecting-IP` 假裝成別的 IP，所以直接連的方式只適合給信任的人用。

## 讓別人連得到（三選一）

| 方式 | 網址 | 需要什麼 | 啟動時加 |
|---|---|---|---|
| **固定網址（推薦）** | `https://date.你的網域`，換主機也不變 | Cloudflare 帳號，以及一個交給 Cloudflare 管理的網域 | `--profile tunnel` |
| **臨時網址** | `https://隨機字.trycloudflare.com`，每次啟動都不一樣 | 不用帳號 | `--profile quick-tunnel` |
| **同一個網路** | `http://這台電腦的IP:8000`（IP 用 `ipconfig` 查） | Windows 防火牆允許這個埠的連入 | 不用加 |

**設定固定網址**（只做一次，輪流當主機的電腦共用）：

1. 登入 Cloudflare 的 Zero Trust 控制台 → Networks → Tunnels → 建立通道（Create a tunnel，類型選 Cloudflared），取個名字。
2. 安裝步驟的頁面會顯示一行含 `--token` 的指令：把 `--token` 後面那一長串複製下來，填進每台電腦 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN`。
3. 替這個通道新增公開主機名稱（Public Hostname）：子網域例如 `date`、網域選你的網域，服務類型 `HTTP`、網址填 `app:8000`（舊版設定過的不用改）。
4. 之後網址就是 `https://date.你的網域`。Cloudflare 介面的名稱偶爾會改，找「Tunnels」和「Public Hostname」就對了。

用臨時網址時，網址在記錄裡（找 `trycloudflare.com`）：
```bash
docker compose logs quick-tunnel
```

**要讓外部的 MCP 用戶端連 `/mcp`**：官方 MCP SDK 預設只接受 localhost 的連線（防 DNS rebinding），要把對外網域填進 `.env` 的 `MCP_ALLOWED_HOSTS`，例如 `date.你的網域`；用同一個網路的 IP 連時，也要把 IP 加進去（逗號分開）。網頁和 REST API 不受這個設定影響。

## 換主機（兩台電腦輪流當主機）

兩台（或更多）電腦都可以當主機，**但同一時間只開一台**。每台各自有一份資料庫，啟動後會自己抓場次和餐廳，通常不用搬資料。以下指令在 Windows 的 PowerShell 或命令提示字元執行（macOS、Linux 用終端機；`copy` 改成 `cp`）。

### 每台電腦第一次設定（只做一次）

1. 安裝 [Git](https://git-scm.com/) 與 [Docker Desktop](https://www.docker.com/products/docker-desktop/)。Docker Desktop 的設定裡勾選「登入時自動啟動」（Start Docker Desktop when you sign in），之後網站會跟著 Docker 自動啟動。
2. 要用 LLM 的話，安裝 [Ollama](https://ollama.com/) 並下載模型（不用 LLM 可跳過）：
   ```powershell
   ollama pull qwen2.5:3b
   ```
3. 下載程式、建立設定檔：
   ```powershell
   git clone https://github.com/LittleQQQQ/date.git
   ```
   ```powershell
   cd date
   ```
   ```powershell
   copy .env.docker.example .env
   ```
   用記事本打開 `.env`：`DB_PASSWORD` 必填，每台自己取一個密碼；`CLOUDFLARE_TUNNEL_TOKEN` 要用固定網址才填，每台填同一個；金鑰有才填，不要 commit。
4. 電源設定改成插電時不睡眠，否則別人會連不上。

### 每次換主機

下面以固定網址（`--profile tunnel`）為例；用臨時網址就換成 `--profile quick-tunnel`，只給同一個網路用就拿掉 `--profile …`。

1. **舊主機**：停掉網站和通道（資料保留在這台）：
   ```powershell
   docker compose --profile tunnel down
   ```
2. **新主機**：拿最新的程式，再建置、啟動：
   ```powershell
   git pull
   ```
   ```powershell
   docker compose --profile tunnel up -d --build
   ```
3. 等資料抓齊（第一次在這台啟動大約 5～10 分鐘，之後只補抓新的場次），看 `http://localhost:8000/api/coverage` 或記錄：
   ```powershell
   docker compose logs -f worker
   ```
4. 用對外網址打開，排一趟行程確認。

### 要把資料一起搬過去（選用）

場次和餐廳會自己重抓，通常不用搬。想讓新主機一啟動就有完全一樣的資料，可以這樣搬：

1. **舊主機**（還在跑的時候）匯出資料庫，存成專案資料夾裡的 `date.dump`：
   ```powershell
   docker compose exec db pg_dump -U date_planner -d date_planner -Fc -f /tmp/date.dump
   ```
   ```powershell
   docker compose cp db:/tmp/date.dump date.dump
   ```
2. 把 `date.dump` 複製到新主機的專案資料夾（隨身碟、雲端硬碟都可以；它不會進 git）。
3. **新主機**先只啟動資料庫、還原，再啟動全部：
   ```powershell
   docker compose up -d db
   ```
   ```powershell
   docker compose cp date.dump db:/tmp/date.dump
   ```
   ```powershell
   docker compose exec db pg_restore -U date_planner -d date_planner --clean --if-exists /tmp/date.dump
   ```
   ```powershell
   docker compose --profile tunnel up -d --build
   ```

這幾行請在 PowerShell 或命令提示字元執行：Git Bash 會把 `/tmp/…` 改寫成 Windows 路徑而失敗（真的要用 Git Bash，先執行 `export MSYS_NO_PATHCONV=1`）。也不要改用 `pg_dump … > 檔案` 的寫法：Windows PowerShell 會改掉輸出的編碼，備份檔會壞掉。

### 注意

- **同時只開一台**：兩台都開著時，固定網址會隨機連到其中一台，看到的資料可能不一樣。
- **換主機前先 `git pull`**：兩台才會跑同一版程式。
- **金鑰與 `.env` 每台各自一份**：不在 git 裡，新電腦要自己建。
- **每月額度每台各自計數**：Google、Jev 的每月用量記在各台自己的 Redis，換主機不會帶過去。兩台都有填 Google 金鑰時，一個月的總用量最多可能是上限的兩倍；要保險就把每台的 `GOOGLE_*_MONTHLY_LIMIT` 設成預設值的一半，並在 Google Cloud Console 另外設定配額上限。

## 設定（.env）

完整清單和說明見 `.env.example`（本機開發）與 `.env.docker.example`（Docker 主機）。常用的：

| 變數 | 預設 | 說明 |
|---|---|---|
| `DATABASE_URL`、`REDIS_URL` | 開發環境的網址 | Docker 主機不用填，compose 會設定 |
| `DB_PASSWORD` | — | 只有 Docker 主機要填：資料庫密碼 |
| `TDX_CLIENT_ID`、`TDX_CLIENT_SECRET` | 空 | 交通部 TDX 金鑰；空 = 用路網骨架的估計值 |
| `GOOGLE_PLACES_API_KEY` | 空 | Google Places API (New) 金鑰；空 = 餐廳只用 OSM |
| `GOOGLE_NEARBY_MONTHLY_LIMIT`、`GOOGLE_DETAILS_MONTHLY_LIMIT`、`GOOGLE_LOCATION_MONTHLY_LIMIT` | 4750、950、9500 | 自家每月上限（免費額度留 5%）；第一次試用可以先設小一點 |
| `JEV_MODE`、`TYPESAFE_API_KEY` | `mock`、空 | `api` = TypeSafe 正式 API |
| `JEV_CONFIDENCE_THRESHOLD` | `0.6` | Jev 信心低於這個值就交給 LLM |
| `OLLAMA_BASE_URL`、`OLLAMA_MODEL` | `http://localhost:11434`、`qwen2.5:3b` | 本機開發用；Docker 主機改填 `OLLAMA_URL` |
| `CORS_ORIGINS` | 空 | 前端放在別的網域時才填 |
| `MCP_ALLOWED_HOSTS` | 空 | 外部 MCP 用戶端要連時，填對外網域 |
| `API_READ_LIMIT_PER_MINUTE`、`API_PLAN_LIMIT_PER_HOUR` | `60`、`10` | 每個 IP 的限流 |

## 開發環境

需要 Python 3.10 以上和 Docker Desktop。在 `date` 資料夾：

1. 開 PostGIS（5433）和 Redis（6380）。資料庫 `date_v2` 給開發用，`date_v2_test` 給 pytest 用：
   ```bash
   docker compose -f compose.dev.yml up -d --wait
   ```
2. 建立虛擬環境、安裝套件，複製 `.env.example` 為 `.env`（`DATABASE_URL` 已經是開發環境的網址）：
   ```powershell
   py -3 -m venv .venv
   ```
   ```powershell
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
3. 建立資料表（改了 `app/models/` 之後用 `alembic revision --autogenerate -m "說明"` 產生新的 migration）：
   ```powershell
   .venv\Scripts\alembic.exe upgrade head
   ```
4. 匯入資料（依序）：`network`（路網骨架）→ `seed`（暫定的類別、地標、票價）→ `atmovies`（開眼場次，約幾分鐘）→ `osm`（餐廳）→ `tdx`（要有 TDX 金鑰）→ `google`（要有 Google 金鑰，跑完會印出本月用量）。每次執行都記在 `crawl_runs`：
   ```powershell
   .venv\Scripts\python.exe -m app.ingest network
   ```
5. 啟動網站（`http://127.0.0.1:8000`）和 worker。RQ 的 worker 要 fork，Windows 上開發改用 `--simple`（執行中的規劃取消不了，會跑完）：
   ```powershell
   .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
   ```
   ```powershell
   .venv\Scripts\python.exe -m app.jobs.worker --simple
   ```
   要測排程再開 `rq cron app.jobs.cron_config --url redis://localhost:6380/0`。

## API

格式是 JSON，時間用 ISO 8601（`+08:00`），錯誤一律是 `{"error": {"code": "…", "message": "…"}}`。

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/areas` | 4 個區域 |
| GET | `/api/areas/{code}/locations` | 某區的地標（出發地、終點共用） |
| GET | `/api/categories?kind=movie_genre\|cuisine` | 電影種類、餐廳類別 |
| GET | `/api/coverage` | 每區有場次資料的日期範圍、目前的餐廳來源 |
| POST | `/api/plans` | 送出規劃，回傳 SSE 串流：`queued` → `progress` → `result` 或 `error`，每 15 秒一次心跳 |
| GET | `/api/health` | 資料庫和 Redis 都正常才回 200 |

`POST /api/plans` 要用 `fetch` 讀串流（`EventSource` 只支援 GET）。使用者關閉連線時，排隊中或執行中的規劃會被取消。結果不保存，也不能重新取得。

## MCP

端點是 `/mcp`（Streamable HTTP、無狀態模式），例如 `http://localhost:8000/mcp`，可以用 MCP Inspector 連線測試。工具和 REST 一一對應，輸出格式相同：

| 工具 | 對應的 REST |
|---|---|
| `list_areas`、`list_locations`、`list_categories`、`data_coverage` | 對應的 GET 端點 |
| `plan_date` | `POST /api/plans`；進度改用 MCP 進度通知，最後回傳完整結果 |

限流和 REST 共用（以 IP 計）。錯誤內容是「代碼：說明」。Jev 沒把握時和網頁一樣用自家 Ollama，不用 sampling（官方 SDK 在無狀態模式下送不出，最新協定也把 sampling 列為淘汰）。

## 外部服務

**交通部 TDX**：到 [TDX](https://tdx.transportdata.tw/) 免費註冊，在「會員中心 → 資料服務 → API 金鑰」建立金鑰，把 Client Id 和 Client Secret 填進 `.env`。每週更新路網骨架裡台鐵、公車路段的班表與票價；免費方案每分鐘最多 5 次，所有程式共用一個限流。

**Google Places API (New)**：在 Google Cloud Console 啟用 **Places API (New)**，建立金鑰並把「API 限制」設成只能用它。

| 用途 | 時機 | 計費等級 | 自家每月上限 |
|---|---|---|---|
| 找餐廳（Nearby Search，每個車站一次） | 每週一 05:00 | Pro | 4,750 |
| 補店家資料（店名、地址、營業時間、價位） | 規劃時，每次最多 10 家 | Enterprise | 950（用完就改用 OSM） |
| 更新座標 | 每天 03:00，超過 25 天的 | Essentials | 9,500 |

依 Google 條款，資料庫只存 place_id 和 30 天內的座標，店名等內容只在記憶體裡用，也不放進 Redis；畫面上標示「資料來源：Google Maps」。第一次試用建議把三個上限先設成 10，跑一次 `python -m app.ingest google` 確認正常再改回預設。

**Jev（TypeSafe）**：拿到金鑰後在 `.env` 設 `JEV_MODE=api`、`TYPESAFE_API_KEY`。每月預算 1 美元（`JEV_MONTHLY_BUDGET_MICRO_USD`），用完全部交給 LLM 備援；每分鐘最多 300 次。換模型版本後，舊的判斷會在每天 03:00 重新判斷。

## 資料來源與標示

| 名稱 | 檔案 | 內容 | 更新 |
|---|---|---|---|
| 路網骨架 | `data/network/` | 車站、路段（車程、班距、首末班的估計值） | worker 每次啟動 |
| 暫定資料 | `data/seed/` | 類別、地標、影城票價 | worker 每次啟動 |
| 開眼電影網 | `app/collectors/atmovies.py` | 影城、電影（片長、簡介）、今天起的場次 | 每天 06:00 |
| OpenStreetMap | `app/collectors/osm.py` | 車站周圍步行 800 公尺內的餐廳 | 每天 06:00 |
| 交通部 TDX | `app/collectors/tdx.py` | 台鐵、公車的班表與票價 | 每週一 04:00 |
| Google 地圖 | `app/collectors/google_places.py` | 餐廳的 place_id 和座標 | 每週一 05:00 |

- 電影、餐廳的類別和所在區域由 Jev 在寫入時判斷，存在 `jev_judgments`；超過 30 天或 Jev 換版本就重新判斷。
- 抓資料請遵守各網站的 robots.txt 與使用條款；抓取失敗時保留舊資料，錯誤記在 `crawl_runs`。
- 來源標示：場次來自開眼電影網；餐廳資料 © OpenStreetMap 貢獻者（ODbL 授權）與 Google 地圖。

## 測試

```powershell
.venv\Scripts\python.exe -m pytest
```

測試用 `compose.dev.yml` 的 `date_v2_test` 資料庫（每次從頭用 Alembic 建立，也驗證 migration 能退回）和 Redis 第 15 號資料庫；外部服務一律用假的回應，`.env` 的金鑰和上限都不會用到。

## 專案結構

```
app/
  main.py            進入點：網頁、/api、/mcp
  api/               REST 端點、錯誤格式
  mcp_server.py      MCP 工具（手寫）
  plan_stream.py     送出規劃、轉送進度、斷線取消（REST 和 MCP 共用）
  limits.py          每個 IP 的查詢限流
  services.py        服務層：查詢、規劃前的檢查、準備規劃資料
  planner.py         規劃：組候選、時間軸、Jev 打分數、排序
  routing.py         地點之間怎麼走（路網＋步行、路線快取）
  transit.py         路網與轉乘
  google_sync.py     Google 餐廳：找餐廳、更新座標、規劃時補店家資料
  progress.py        規劃進度（Redis Pub/Sub）
  jev/               Jev 題目、正式 API、模擬版、LLM 備援、寫入時的判斷
  jobs/              RQ 佇列、規劃工作、資料更新工作、排程、worker
  collectors/        資料來源：開眼、OSM、TDX、Google
  redis_utils/       鎖、限流、每月額度、快取
  models/            資料表（SQLAlchemy＋GeoAlchemy2）
  ingest.py          匯入與手動執行
migrations/          Alembic
data/network/        路網骨架
data/seed/           暫定的類別、地標、票價
frontend/            網頁
nginx/               反向代理設定
tests/               自動測試
Dockerfile           api、worker、cron、migrate 共用的映像
docker-compose.yml   主機部署（見「用 Docker 部署」）
compose.dev.yml      開發與測試用的 PostGIS、Redis
```

## 目前的限制

- 地標、類別、影城票價是暫定資料；Jev 是規則模擬版，實際效果要等接上 TypeSafe 正式 API。
- 交通以「車程＋班距＋首末班」估算，不是逐班的時刻表（逐班時刻表是第二階段）；公車車程依站牌距離估計。
- 餐廳的營業時間、價位，OSM 沒標的依店的類型估計，畫面上會標示「估計」；Google 每個車站只拿得到最熱門的 20 家。
- 可選日期取決於影城公布了幾天的場次，週初常常只到週四。
- 只支援大眾運輸和步行（步行限 800 公尺內）。

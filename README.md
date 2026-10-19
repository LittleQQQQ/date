# 約會一條龍（Jev 生活場景應用 12）

用下拉式選單選好縣市、商圈、出發站、日期、想看的片、幾點前到家、想吃什麼，
系統排出「電影 ＋ 餐廳 ＋ 大眾運輸」的完整時間軸。**看哪部片、吃哪一家、去程回程搭什麼由 Jev 決定**，結果是 Jev 的前三名組合。

- 目前只開放桃園市（設定 `ACTIVE_CITIES`）：桃園區、青埔、中壢、八德四個有大電影院的商圈，出發站是桃園車站、中壢車站、高鐵桃園、八德。資料檔裡還有全台其他縣市的商圈與交通（19 個縣市），把 `ACTIVE_CITIES` 留空就恢復全台；那時嘉義縣、連江縣、金門縣顯示「尚無資料」，澎湖只能從島上出發。商圈與交通是手寫的估計值。
- 影城、電影、場次來自**開眼電影網**，餐廳來自 **OpenStreetMap**，伺服器會自動更新。設定 Google Places 金鑰後，餐廳改成排程時即時查 **Google 地圖**（見「Google 地圖餐廳」）。
- 出發站和商圈可以不同地點（例如從中壢車站出發到八德），自動安排捷運、台鐵、高鐵、公車轉乘，並檢查各段末班車；恢復全台後也能跨縣市出發。

> **要把一台電腦當主機讓別人連線**：照「用 Docker 部署」與「換主機（兩台電腦輪流當主機）」兩節操作，clone 下來照著做就好。

> 交通班次與車資是**估計值**；票價、沒標營業時間的餐廳營業時間與價位也是估計，畫面上會標示。Jev 使用**模擬版**。

## 架構

```
網頁（frontend/）：全部用下拉式選單
  │  條件 → /api/plan
  ▼
FastAPI（app/api.py）
  ├─ 透過 MCP 呼叫 plan_date（app/mcp_server.py；外部用戶端也能連 /mcp）
  ├─ LLM（本機 Ollama）：把結果寫成口語摘要
  ▼
服務層（app/services.py）── Jev（app/jev/）：封閉式判斷＋機率，結果快取在資料庫
  ▼
排程計算（app/planner.py）：列舉組合、逐段檢查時間、評分挑選
  │  └─ 交通路網（app/transit.py）：找最晚出門、最早到家的轉乘路線
  ▼
PostgreSQL ◄── 匯入（app/ingest.py）◄── 資料來源（app/collectors/）：開眼電影網、OpenStreetMap、商圈與交通
                                         ▲ 自動更新（app/refresher.py）：啟動時補抓、每天早上再抓
```

三段分工對應簡報：

| 階段 | 程式位置 | 內容 |
|---|---|---|
| 程式抓取 | `app/collectors/`、`app/ingest.py` | 定時批次抓資料寫進資料庫，保留原始文字 |
| 程式篩選 | `app/planner.py` | 找出時間、交通、末班車、營業時間、預算都接得起來的組合 |
| **Jev 選擇** | `app/planner.py`（`_jev_pick`、`_choose_trips`） | **看哪部片**（參考想看的片型、簡介）、**吃哪一家**（參考飲食限制、想吃的、預算、介紹與評論）、**去程回程搭什麼**（參考車程、轉乘、等車、車資與剩下的預算） |
| Jev 判斷 | `app/jev/` | 屬性題，用來篩選和估時間：約會適合度、用餐快慢、料理類型、影廳、語言、片名是否相符、能不能滿足飲食限制 |
| 程式計算 | `app/planner.py`、`app/transit.py` | 時間軸、轉乘路線、緩衝、最後點餐、末班車、評分 |

LLM 只處理語言，**時間、金額、店名都由程式用範本寫**；LLM 只補一兩句推薦理由，
推薦理由裡只要出現數字（包括「四點」「八百元」這種中文寫法）就整句不用；店名、片名本身帶的數字（例如「定食8」「粽邪4」）不算。3B 的小模型轉述數字常出錯，所以不讓它碰數字。
聊天 API 解析時，時間和預算也是規則抓得到就以規則為準；句子沒提到商圈或出發站時，從開放範圍內挑（出發站所在的商圈、同縣市的大站）。

另外保留了自然語言 API `POST /api/chat`（`{"message": "週六從八德出發去中壢看電影吃日式"}`），
由規則＋LLM 把一句話轉成條件後一樣走 MCP 排程。網頁目前沒有用到，之後要做語音或聊天介面時可以接。

## 快速開始（Windows）

需要 Python 3.10 以上。在 `date-planner` 資料夾執行：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

打開 http://127.0.0.1:8000 。第一次啟動會先匯入商圈與交通，接著在背景抓餐廳（約 10～20 分鐘，視 Overpass 忙碌程度）和影城場次（第一次約 20 分鐘，之後每天約 5 分鐘），抓完網頁會自動載入。之後伺服器每天早上 6 點自動更新（開眼每天、OSM 每週）。

- 沒有 `.env` 時使用專案資料夾裡的 SQLite（`dev.db`），可以先跑起來。
- Ollama 沒開也能用，摘要會改用範本。要用 LLM：先開 Ollama，並下載模型 `ollama pull qwen2.5:3b`。
- 使用 `uvicorn --reload`、多個 `--workers`，或正在手動執行 `python -m app.ingest` 時，請在 `.env` 設 `AUTO_REFRESH=false`，否則會有好幾個更新程式同時抓資料。

## 升級後補抓資料

新增商圈（例如更新 `data/fixture/`）後，伺服器重新啟動就會載入新的商圈與路網，但自動更新會以為今天已經抓過，新商圈的影城要等隔天、餐廳要等一週才會補上。升級後請依序執行：

1. 停止伺服器（避免和自動更新同時寫入資料庫）。
2. 匯入新商圈與路網：`python -m app.ingest`
3. 補抓影城場次：`python -m app.ingest --source atmovies`
4. 補抓餐廳：`python -m app.ingest --source osm`
5. 重新啟動伺服器。

反過來，從 `data/fixture/` 刪掉的商圈、車站、路段，匯入商圈與交通時會自動從資料庫清掉，連同那些商圈裡已經抓到的影城、場次、餐廳，不用手動處理。所以如果不小心刪掉或改名某個縣市的資料檔，那個縣市已經抓到的影城和餐廳也會被清掉；把檔案放回去後，照上面步驟手動補抓一次（自動更新要 7 天後才會重抓餐廳）。`data/fixture/` 整個是空的時候不會清，匯入會直接失敗，資料庫維持原樣。

**改變開放範圍（`ACTIVE_CITIES`）時：** 範圍外的縣市不會被刪，只是不再更新。改完重新啟動，開眼的場次每天會自動補上；餐廳要等 7 天的更新週期，急的話照上面第 4 步手動執行。手動執行 `python -m app.ingest --source atmovies|osm` 也只會抓 `ACTIVE_CITIES` 內的縣市；改回全台時如果當天開眼已經抓過（自動更新會以為今天抓過了），等不到隔天的話，請手動執行 `python -m app.ingest --source atmovies`。

## 改用 PostgreSQL

1. 打開 `db/init.sql`，把 `change_me` 改成自己的密碼，用 postgres 管理員帳號執行：
   ```powershell
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -f db\init.sql
   ```
2. 複製 `.env.example` 為 `.env`，把 `DATABASE_URL` 裡的密碼改成同一個。
3. 匯入商圈與交通（資料表會自動建立）：
   ```powershell
   .venv\Scripts\python.exe -m app.ingest
   ```
4. 照常啟動伺服器。

資料表結構改版時（啟動會提示「資料表結構是舊版的」），執行 `python -m app.ingest --reset` 重建。這會清空資料庫，再重新匯入商圈與交通；影城、場次、餐廳會在伺服器啟動時重新抓。

## 用 Docker 部署

`Dockerfile` 把網站（含 REST API 與 `/mcp`）打包成映像；`docker-compose.yml` 一起啟動網站與 PostgreSQL，LLM（Ollama）選用。需要 Docker Desktop（Windows、macOS）或 Docker Engine（Linux）。

1. 把範本 `.env.docker.example` 複製成 `.env`（compose 會自動讀這個檔，不會進 git，也不會打包進映像），至少填好 `DB_PASSWORD`（自己取一個密碼）：
   ```powershell
   copy .env.docker.example .env
   ```
   範本裡每一項都有說明：`APP_PORT`（對外的埠，預設 8000）、`OLLAMA_URL`（LLM）、`CLOUDFLARE_TUNNEL_TOKEN`（固定網址）、`GOOGLE_PLACES_API_KEY` 等；其他名稱和「設定（.env）」一節相同。
2. 建置並啟動：
   ```bash
   docker compose up -d --build
   ```
3. 第一次啟動時網站會自動建立資料表、匯入商圈與交通，背景更新再去抓開眼場次與 OSM 餐廳（桃園大約 5 分鐘）。看進度：
   ```bash
   docker compose logs -f app
   ```
   `http://localhost:8000/api/status` 的 `refresh.running` 變成 `null` 就抓完了。
4. 打開 `http://localhost:8000`。

**要用 LLM**（二選一）：

- **用這台電腦上裝好的 Ollama（推薦）**：先在電腦上安裝 Ollama 並執行 `ollama pull qwen2.5:3b`，`.env` 填 `OLLAMA_URL=http://host.docker.internal:11434`（範本預設就是這個）。容器會連到電腦上的 Ollama，用得到電腦的顯示卡，也不用重下載模型。
- **用 compose 裡的 Ollama 容器**：`.env` 填 `OLLAMA_URL=http://ollama:11434`，啟動時多帶 `--profile llm`，第一次再下載模型：
  ```bash
  docker compose --profile llm up -d --build
  ```
  ```bash
  docker compose exec ollama ollama pull qwen2.5:3b
  ```

沒有 GPU 的主機跑 LLM 很慢，可以不開（`OLLAMA_URL` 留空）：網頁照常可用，摘要改用範本。

**常用指令**

| 要做什麼 | 指令 |
|---|---|
| 更新程式後重建 | `git pull` 之後 `docker compose up -d --build` |
| 停止（資料保留） | `docker compose down` |
| 連資料一起刪掉 | `docker compose down -v` |
| 備份資料庫 | 見「換主機」最後一步（`pg_dump` 再 `docker compose cp` 出來） |
| 資料表結構改版時重建 | `docker compose exec app python -m app.ingest --reset` |
| 手動補抓 | `docker compose exec app python -m app.ingest --source atmovies`（或 `osm`） |

**注意**

- 只開一個網站容器、一個 worker：背景更新寫在網站程序裡，開多個會同時抓資料。
- 資料存在 Docker volume（`pgdata`：資料庫；`cache`：爬蟲的網頁快取），`docker compose down` 不會刪，`down -v` 才會。
- 本機開發用的 `dev.db`（SQLite）不會搬進容器；容器裡的 PostgreSQL 由背景更新重新抓資料。
- 要讓外面連進來：放到雲端主機時，前面再加一個反向代理（例如 Caddy）處理 HTTPS 與網域；只是暫時展示的話，可以用 Cloudflare Tunnel 把本機的埠開成公開網址。網站目前沒有登入、呼叫頻率限制，MCP 端點也沒有存取權杖，長期公開前要先補上。
- 開眼、OpenStreetMap 是否會擋雲端主機的連線，要在那台主機上實際跑一次確認。

## 換主機（兩台電腦輪流當主機）

兩台（或更多）電腦都可以當主機，**但同一時間只開一台**。每台各自有一份資料庫，啟動後會自己抓場次和餐廳，所以換主機時通常不用搬資料。以下指令在 Windows 的 PowerShell 或命令提示字元執行（macOS、Linux 用終端機；`copy` 改成 `cp`）。

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
   用記事本打開 `.env`：
   - `DB_PASSWORD`：必填，每台自己取一個密碼。
   - `OLLAMA_URL`：要用 LLM 就保留 `http://host.docker.internal:11434`；不用就清空。
   - `CLOUDFLARE_TUNNEL_TOKEN`：要用固定網址才填（見下一段），**兩台填同一個**。
   - `GOOGLE_PLACES_API_KEY` 等金鑰：有才填。`.env` 不會進 git，金鑰不要 commit。
4. 電源設定改成插電時不睡眠，否則別人會連不上。

### 讓別人連得到（三選一）

| 方式 | 網址 | 需要什麼 | 啟動時加 |
|---|---|---|---|
| **固定網址（推薦）** | `https://date.你的網域`，換主機也不變 | Cloudflare 帳號，以及一個交給 Cloudflare 管理的網域 | `--profile tunnel` |
| **臨時網址** | `https://隨機字.trycloudflare.com`，每次啟動都不一樣 | 不用帳號 | `--profile quick-tunnel` |
| **同一個網路** | `http://這台電腦的IP:8000`（IP 用 `ipconfig` 查） | Windows 防火牆允許這個埠的連入 | 不用加 |

**設定固定網址**（只做一次，兩台共用）：

1. 登入 Cloudflare 的 Zero Trust 控制台 → Networks → Tunnels → 建立通道（Create a tunnel，類型選 Cloudflared），取個名字。
2. 安裝步驟的頁面會顯示一行含 `--token` 的指令：把 `--token` 後面那一長串複製下來，填進**兩台**電腦 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN`。
3. 替這個通道新增公開主機名稱（Public Hostname）：子網域例如 `date`、網域選你的網域，服務類型 `HTTP`、網址填 `app:8000`。
4. 之後網址就是 `https://date.你的網域`。Cloudflare 介面的名稱偶爾會改，找「Tunnels」和「Public Hostname」就對了。

用臨時網址時，網址在記錄裡（找 `trycloudflare.com`）：
```powershell
docker compose logs quick-tunnel
```

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
3. 等資料抓齊：第一次在這台啟動大約 6～7 分鐘，之後只補抓新的場次。打開 `http://localhost:8000/api/status`，`refresh.running` 變成 `null` 就好了；也可以看記錄：
   ```powershell
   docker compose logs -f app
   ```
4. 用對外網址打開，排一趟行程確認。

### 要把資料一起搬過去（選用）

場次和餐廳會自己重抓，通常不用搬。想讓新主機一啟動就有完全一樣的資料（例如抓資料的網站剛好連不上），可以這樣搬：

1. **舊主機**（網站還在跑的時候）匯出資料庫，存成專案資料夾裡的 `date.dump`：
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
- **金鑰與 `.env` 每台各自一份**：不在 git 裡，新電腦要自己建（照上面第 3 步）。
- **網站目前沒有登入與呼叫頻率限制、MCP 端點沒有存取權杖**：公開網址只給信任的人；有設 Google Places 金鑰時，程式本身每天最多呼叫 `GOOGLE_PLACES_DAILY_LIMIT` 次。

## 設定（.env）

| 變數 | 預設 | 說明 |
|---|---|---|
| `DATABASE_URL` | 空（用 SQLite） | 例：`postgresql+psycopg://date_planner:密碼@localhost:5432/date_planner` |
| `JEV_MODE` | `mock` | `mock` 規則模擬；`api` 呼叫正式 Jev API |
| `JEV_API_URL`、`JEV_API_KEY` | 空 | `JEV_MODE=api` 時使用 |
| `JEV_CONFIDENCE_THRESHOLD` | `0.6` | Jev 最高機率低於此值就請使用者確認 |
| `OLLAMA_URL` | `http://localhost:11434` | 留空 = 不用 LLM |
| `OLLAMA_MODEL` | `qwen2.5:3b` | |
| `MCP_CLIENT_URL` | 空 | 網頁排程呼叫 MCP 的方式：空 = 同程序內連線；填 `http://127.0.0.1:8000/mcp` = 走 HTTP |
| `AUTO_REFRESH` | `true` | 伺服器啟動時在背景補抓資料，之後每天定時更新 |
| `REFRESH_HOUR` | `6` | 每天幾點更新 |
| `ACTIVE_CITIES` | `桃園市` | 開放哪些縣市（逗號分隔）；留空 = 全台。範圍外的縣市不列在選單、不能排程、爬蟲不更新，但資料留在資料庫 |
| `GOOGLE_PLACES_API_KEY` | 空 | Google Places API (New) 的金鑰。填了就在排程時即時查 Google 地圖的餐廳；空 = 用 OSM |
| `GOOGLE_PLACES_ATMOSPHERE` | `true` | `true` 多要評論、可訂位、素食、店家簡介（計費等級 Enterprise + Atmosphere）；`false` 只要營業時間、價位等（Enterprise） |
| `GOOGLE_PLACES_DAILY_LIMIT` | `30` | 程式每天最多呼叫 Google Places 幾次（台灣時間換日），超過就改用 OSM |

## Jev 怎麼做選擇

1. 程式篩出接得起來的組合後，問 Jev「這次約會最適合看下面哪一部電影？」，選項是排得進去的片，最多 8 部（使用者指定了片就不問）。
2. 針對 Jev 機率前三名的片，各問一次「看這部片，最適合去下面哪一家吃飯？」，選項是配得上這部片時間的餐廳，最多 8 家。
3. 組合信心 ＝ 選片機率 × 選餐廳機率，取前三名當方案；同一組片和餐廳有好幾個場次時，程式挑緩衝最充裕的。
4. 前三名方案決定後，程式列出每個方案時間接得上、預算付得起的走法（拿掉高鐵、台鐵、公車（含客運）的各種組合各算一次；又慢又不便宜的刪掉；每個方向最多 4 種），問 Jev「這次約會的去程／回程，最適合哪一種走法？」；只有一種走法時不問。選車的機率另外顯示，不乘進組合信心。
5. Jev 對選擇沒把握時，方案照樣排出來，另外列出 Jev 的前幾名讓使用者改選；改選後就用使用者選的重新排。
6. 卡片上的「最省」「最穩」是程式算出的客觀標籤，不是 Jev 的判斷。

選片、選餐廳的選項每次都不一樣（例如這次 5 部片、下次 3 部），context 會列出每個選項的說明。
排得進去的片或配得上的餐廳超過 8 個時，程式先挑前 8 名再問 Jev（`app/planner.py` 的 `MAX_CHOICE_OPTIONS`）：電影依「符合想看的片型 → 時間緩衝夠寬裕（超過 30 分鐘算一樣）→ 場次多」，餐廳依「Jev 判斷的約會適合度 → 離影城近」（想吃的料理在這之前已經篩過），摘要會說明有縮減。使用者在改選清單點過的片或餐廳一定會留在選項裡。
**需要和 Jev 確認：選項可不可以每次不同、一題最多幾個選項。** 確認後調整 `MAX_CHOICE_OPTIONS` 即可。

車資依交通方式和車程估計（`app/fares.py`）：捷運 20～65 元、市區公車 15 元、國道客運每分鐘約 2.3 元、台鐵每分鐘約 2.5 元、高鐵每分鐘約 13 元，連續搭同一條路線算一段票。每人預算包含電影票、餐費與來回車資，超出預算的走法程式先篩掉。

## 接上正式的 Jev API

只要改 `app/jev/api_client.py`。目前假設的介面：

```
POST {JEV_API_URL}/predict
{"question": "這家餐廳適合兩人約會嗎？", "options": ["適合約會", "普通", "不適合"], "context": "名稱：…\n介紹：…\n評論：…"}
→ {"probabilities": [0.82, 0.15, 0.03]}
```

改好後在 `.env` 設 `JEV_MODE=api`，再執行 `python -m app.ingest` 重新做預先判斷。
判斷結果快取在 `jev_judgments` 表；原始文字沒變就不會重問。

## 資料來源

| 名稱 | 檔案 | 內容 | 更新 |
|---|---|---|---|
| `base` | `app/collectors/fixture.py` | 商圈、車站、交通路段（`data/fixture/` 手寫的估計值） | 每次啟動 |
| `atmovies` | `app/collectors/atmovies.py` | 開眼電影網：商圈附近的影城、電影（片長、簡介）、今天起最多 7 天的場次 | 每天 |
| `osm` | `app/collectors/osm.py` | OpenStreetMap：商圈車站步行 15 分鐘內的餐廳，每個商圈最多 60 家 | 每週（有商圈沒抓到則下一輪重抓） |
| `fixture` | `app/collectors/fixture.py` | 完整的虛構資料，只給自動測試用 | — |

手動執行：`python -m app.ingest --source atmovies`（或 `osm`、`base`）。

- 抓資料請遵守各網站的 robots.txt 與使用條款；目前每個網站每秒最多一次請求，地圖頁、電影頁快取在 `data/cache/`（30 天）。
- 開眼沒有票價和片型：票價依版本估計（IMAX、4DX 這類 420 元，其他 300 元）；片型由 Jev 讀簡介判斷，沒把握就顯示「片型未知」。
- OSM 沒有評論和價位，大約只有一成店家標了營業時間：介紹由標籤組成；營業時間、價位依店的類型估計。
- 抓取失敗時保留舊資料，錯誤記在 `scrape_runs` 表；某家影城或某個商圈失敗，只有它保留舊資料。
- Overpass 常因為太忙而逾時（504）。OSM 單次查詢最多試 3 次（間隔 15 秒、30 秒）；若執行完仍有商圈沒抓到，下一次檢查（下次啟動，或隔天 `REFRESH_HOUR` 點）會再抓一次 OSM，不必等一週。
- 來源標示：場次來自開眼電影網；餐廳資料 © OpenStreetMap 貢獻者，以 ODbL 授權。

新增爬蟲：在 `app/collectors/` 新增檔案，繼承 `Collector`，實作 `collect(start, days)` 回傳 `Batch`（格式見 `app/collectors/base.py`）；需要商圈範圍就設 `needs_coverage = True`，建構子接收 `coverage`。最後在 `app/collectors/__init__.py` 的 `COLLECTORS` 登記；要讓伺服器自動更新，還要加進 `app/refresher.py` 的 `SOURCES` 與 `due_sources`。

## Google 地圖餐廳（選用）

在 `.env` 填 `GOOGLE_PLACES_API_KEY` 後，餐廳改由 `app/google_places.py` 在排程當下即時查 Google 地圖：

- 每次排程，對選定商圈的每個車站各查一次 Nearby Search（半徑和 OSM 一樣是步行 15 分鐘，一次最多 20 家），合併後最多留 60 家；有營業時間的優先，其次離影城近的。MCP 的 `find_restaurants` 則對影城周圍查一次。
- **依 Google 的使用條款，店名、營業時間、價位等內容不存進資料庫**，每次都重新查。只有 place_id（條款允許長期保存）拿來當 Jev 判斷快取的對象；Jev 的判斷快取存的是 Jev 的分類結果，不是 Google 的文字，但仍算從 Google 資料衍生的內容，屬於條款上的灰色地帶。
- 營業時間用 `regularOpeningHours`，同一天分段營業時取晚上那段；價位有金額區間就取中間值，只有價位等級（便宜／中等／貴）時換算成估計值並標示。介紹沿用 OSM 爬蟲的寫法（料理類型、素食、可訂位），再加上 Google 評分與店家簡介；評論最多 5 則，給 Jev 判斷用，不顯示在畫面上。
- 沒有金鑰、Google 查詢失敗（金鑰無效、沒開 API、帳單、配額、連線）或超過 `GOOGLE_PLACES_DAILY_LIMIT` 時，改用資料庫裡的 OSM 餐廳，網頁上會提示原因。所以有金鑰時 OSM 仍然每週更新，當作備用。
- 次數計在 `api_usage` 表。每個車站算一次；商圈剩下的次數不夠查完所有車站時一次都不查，直接改用 OSM。使用者在「Jev 不太確定」那裡點選項會重新排程，也會再查一次。
- 畫面上會標示「餐廳資料：Google Maps」，餐廳名稱連到 Google 地圖的店家頁。

申請金鑰：在 Google Cloud Console 建立專案、綁定帳單帳戶，啟用 **Places API (New)**（不是舊版的 Places API），建立 API 金鑰並把「API 限制」設成只能用 Places API (New)。建議也在 Console 的「配額」把 Nearby Search 的每日上限設成和 `GOOGLE_PLACES_DAILY_LIMIT` 差不多，雙重把關。

費用（2026 年 10 月的官方價目表）：Nearby Search Enterprise 與 Enterprise + Atmosphere 每月各有 1,000 次免費，超過後每千次 US$35／US$40。預設每天 30 次，一個月約 900 次，不會超過免費額度。

交通建議改用交通部 TDX API（需要免費註冊）。交通資料的格式是路網：`stations`（站）＋`transit_links`（有方向的路段，含路線名稱、車程、班距、首末班）。

## MCP

伺服器啟動後，MCP 端點在 `http://127.0.0.1:8000/mcp`（Streamable HTTP），可以用 MCP Inspector 連線測試。

| 工具 | 用途 |
|---|---|
| `list_areas` | 目前開放的縣市（ACTIVE_CITIES）、有資料的商圈（含可選日期與片單）、可以出發的車站 |
| `plan_date` | 主功能：條件 → 方案、Jev 待確認問題、排除原因（可跨縣市） |
| `search_showtimes` | 查某天某商圈的場次 |
| `find_restaurants` | 查影城步行範圍內的餐廳與 Jev 判斷 |
| `check_route` | 查某個時間從出發站出發，到商圈各車站怎麼轉乘、最晚幾點到 |

## 測試

```powershell
.venv\Scripts\python.exe -m pytest
```

測試使用獨立的暫存 SQLite、Jev 模擬版，不連 LLM。

## 專案結構

```
app/
  main.py          進入點：網頁、/api、/mcp
  api.py           REST API
  mcp_server.py    MCP 工具
  services.py      服務層（API 與 MCP 共用）
  planner.py       排程計算
  transit.py       交通路網與轉乘路線
  regions.py       全台 22 縣市
  llm.py           Ollama：需求解析、摘要（含規則備援）
  jev/             Jev 題目、模擬版、API 版、快取
  collectors/      資料來源：開眼、OSM、商圈與交通、示意資料
  google_places.py Google 地圖餐廳（有金鑰時排程當下即時查，不存進資料庫）
  refresher.py     背景自動更新
  ingest.py        匯入與 Jev 預先判斷
  models.py        資料表
data/fixture/      示意資料（每個有商圈的縣市一個檔，另有 交通-跨縣市.json、交通-高鐵.json 與片單）
frontend/          網頁
db/init.sql        PostgreSQL 建立帳號與資料庫
tests/             自動測試
Dockerfile         網站的映像（見「用 Docker 部署」）
docker-compose.yml 網站＋PostgreSQL（LLM 選用）
.env.docker.example 當主機時的 .env 範本（見「換主機」）
```

## 目前的限制

- Jev 模擬版的選片、選餐廳、選車規則很簡單（片型符不符合、約會適合度、緩衝時間、省多少時間與車資），只用來展示流程；實際效果要等接上 Jev API。
- 商圈與交通是手寫的估計值，每個有影城的縣市 1 個商圈（台北市 2 個；桃園市 4 個：桃園區、青埔、中壢、八德）；離商圈車站步行超過 15 分鐘的影城排不到（例如嘉義市的嘉義秀泰、花蓮新天堂樂園、羅東日新、高雄夢時代）。交通以「車程＋最長班距」估算，不是真正的時刻表；東部、南迴線班距大，跨縣市行程會偏保守。
- 票價、車資、沒標營業時間的餐廳營業時間、餐廳價位都是估計值；用 OSM 時餐廳沒有評論。車資不含學生票、敬老票等優惠。
- 可選日期取決於影城公布了幾天的場次：程式最多抓今天起 7 天，但影城每週才公布一次下一週的場次，週初常常只到週四（只有 3～4 天可選）。前 4 天有一頁抓失敗，那家影城就保留上次的場次；第 5～7 天遇到抓不到或還沒公布就停在那裡。
- 只支援大眾運輸，不含開車、騎車。步行時間用直線距離 × 1.3 估算，車站到影城或餐廳最多走 15 分鐘。
- 候位時間是依尖峰離峰的粗略估計。
- 目前只開放桃園市；其他縣市的資料還在，但不再更新，恢復後要等爬蟲補抓。

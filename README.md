# 約會一條龍（Jev 生活場景應用 12）

用下拉式選單選好縣市、商圈、出發站、日期、想看的片、幾點前到家、想吃什麼，
系統排出「電影 ＋ 餐廳 ＋ 大眾運輸」的完整時間軸。**看哪部片、吃哪一家由 Jev 決定**，結果是 Jev 的前三名組合。

- 地區涵蓋全台 22 縣市；目前六都各有 1～2 個商圈的示意資料，其他縣市顯示「尚無資料」，等爬蟲補上。
- 可以跨縣市出發（例如從台中車站到台北信義區），自動安排捷運、台鐵、高鐵、公車轉乘，並檢查各段末班車。

> 目前使用**示意資料**（虛構的影城與餐廳、估計的交通班次），Jev 使用**模擬版**。

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
PostgreSQL ◄── 匯入（app/ingest.py）◄── 資料來源（app/collectors/）：現在是示意資料，之後換成爬蟲
```

三段分工對應簡報：

| 階段 | 程式位置 | 內容 |
|---|---|---|
| 程式抓取 | `app/collectors/`、`app/ingest.py` | 定時批次抓資料寫進資料庫，保留原始文字 |
| 程式篩選 | `app/planner.py` | 找出時間、交通、末班車、營業時間、預算都接得起來的組合 |
| **Jev 選擇** | `app/planner.py`（`_jev_pick`） | **看哪部片**（參考想看的片型、簡介）、**吃哪一家**（參考飲食限制、想吃的、預算、介紹與評論） |
| Jev 判斷 | `app/jev/` | 屬性題，用來篩選和估時間：約會適合度、用餐快慢、料理類型、影廳、語言、片名是否相符、能不能滿足飲食限制 |
| 程式計算 | `app/planner.py`、`app/transit.py` | 時間軸、轉乘路線、緩衝、最後點餐、末班車、評分 |

LLM 只處理語言，**時間、金額、店名都由程式用範本寫**；LLM 只補一兩句推薦理由，
推薦理由裡只要出現數字（包括「四點」「八百元」這種中文寫法）就整句不用。3B 的小模型轉述數字常出錯，所以不讓它碰數字。

另外保留了自然語言 API `POST /api/chat`（`{"message": "這週六想在信義區看《新片 A》……"}`），
由規則＋LLM 把一句話轉成條件後一樣走 MCP 排程。網頁目前沒有用到，之後要做語音或聊天介面時可以接。

## 快速開始（Windows）

需要 Python 3.10 以上。在 `date-planner` 資料夾執行：

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

打開 http://127.0.0.1:8000 。第一次啟動會自動匯入示意資料（今天起 14 天的場次）。

- 沒有 `.env` 時使用專案資料夾裡的 SQLite（`dev.db`），可以先跑起來。
- Ollama 沒開也能用，摘要會改用範本。要用 LLM：先開 Ollama，並下載模型 `ollama pull qwen2.5:3b`。

## 改用 PostgreSQL

1. 打開 `db/init.sql`，把 `change_me` 改成自己的密碼，用 postgres 管理員帳號執行：
   ```powershell
   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -f db\init.sql
   ```
2. 複製 `.env.example` 為 `.env`，把 `DATABASE_URL` 裡的密碼改成同一個。
3. 匯入資料（資料表會自動建立）：
   ```powershell
   .venv\Scripts\python.exe -m app.ingest
   ```
4. 照常啟動伺服器。

資料表結構改版時（啟動會提示「資料表結構是舊版的」），執行 `python -m app.ingest --reset` 重建。這會清空資料庫，再重新匯入示意資料。

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

## Jev 怎麼做選擇

1. 程式篩出接得起來的組合後，問 Jev「這次約會最適合看下面哪一部電影？」，選項是排得進去的片（使用者指定了片就不問）。
2. 針對 Jev 機率前三名的片，各問一次「看這部片，最適合去下面哪一家吃飯？」，選項是配得上這部片時間的餐廳。
3. 組合信心 ＝ 選片機率 × 選餐廳機率，取前三名當方案；同一組片和餐廳有好幾個場次時，程式挑緩衝最充裕的。
4. Jev 對選擇沒把握時，方案照樣排出來，另外列出 Jev 的前幾名讓使用者改選；改選後就用使用者選的重新排。
5. 卡片上的「最省」「最穩」是程式算出的客觀標籤，不是 Jev 的判斷。

選片、選餐廳的選項每次都不一樣（例如這次 5 部片、下次 3 部），context 會列出每個選項的說明。
**需要和 Jev 確認：選項可不可以每次不同、一題最多幾個選項。** 有上限的話，程式要先把候選刪到上限以內再問。

## 接上正式的 Jev API

只要改 `app/jev/api_client.py`。目前假設的介面：

```
POST {JEV_API_URL}/predict
{"question": "這家餐廳適合兩人約會嗎？", "options": ["適合約會", "普通", "不適合"], "context": "名稱：…\n介紹：…\n評論：…"}
→ {"probabilities": [0.82, 0.15, 0.03]}
```

改好後在 `.env` 設 `JEV_MODE=api`，再執行 `python -m app.ingest` 重新做預先判斷。
判斷結果快取在 `jev_judgments` 表；原始文字沒變就不會重問。

## 新增爬蟲

1. 在 `app/collectors/` 新增檔案，繼承 `Collector`，實作 `collect(start, days)`，回傳 `Batch`（格式見 `app/collectors/base.py`）。
2. 在 `app/collectors/__init__.py` 的 `COLLECTORS` 登記名稱。
3. 執行 `python -m app.ingest --source 名稱`，可用 Windows 工作排程器定時執行。

注意事項：

- 交通建議直接用交通部 TDX API（官方開放資料，涵蓋全台捷運、台鐵、高鐵、公車），不必爬蟲。
  交通資料的格式是路網：`stations`（站）＋`transit_links`（有方向的路段，含路線名稱、車程、班距、首末班）。
  同一棟車站裡的不同路線（例如板橋的捷運、台鐵、高鐵）算同一個站，換線時加上該站的 `transfer_min`。
- 商圈代碼是「縣市/商圈」（例如 `台中市/七期`），縣市名稱一律用「台」字，清單在 `app/regions.py`。
- 遵守各網站的 robots.txt 與服務條款、控制抓取頻率；Google 地圖不允許爬取，餐廳資料需另找可合法使用的來源。
- 抓取失敗時會保留舊資料，錯誤記在 `scrape_runs` 表。
- 場次的片名寫法和電影資料不同時，`movie_id` 會是空的，排程會跳過（不知道片長）。

## MCP

伺服器啟動後，MCP 端點在 `http://127.0.0.1:8000/mcp`（Streamable HTTP），可以用 MCP Inspector 連線測試。

| 工具 | 用途 |
|---|---|
| `list_areas` | 全台 22 縣市、有資料的商圈（含可選日期與片單）、可以出發的車站 |
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
  collectors/      資料來源介面與示意資料
  ingest.py        匯入與 Jev 預先判斷
  models.py        資料表
data/fixture/      示意資料（每個縣市一個檔，另有高鐵路網與片單）
frontend/          網頁
db/init.sql        PostgreSQL 建立帳號與資料庫
tests/             自動測試
```

## 目前的限制

- Jev 模擬版的選片、選餐廳規則很簡單（片型符不符合、約會適合度、緩衝時間），只用來展示流程；實際效果要等接上 Jev API。
- 資料是示意資料，只有六都 7 個商圈；其他 16 個縣市等爬蟲補資料。
- 只支援大眾運輸，不含開車、騎車。
- 交通以「車程＋最長班距」估算，不是真正的時刻表；步行時間用直線距離 × 1.3 估算，車站到影城或餐廳最多走 15 分鐘。
- 候位時間是依尖峰離峰的粗略估計。
- Jev 模擬版只是關鍵字規則，用來展示「有把握／沒把握」兩種情況。

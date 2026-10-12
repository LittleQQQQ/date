# 約會一條龍（Jev 生活場景應用 12）

用下拉式選單選好縣市、商圈、出發站、日期、想看的片、幾點前到家、想吃什麼，
系統排出「電影 ＋ 餐廳 ＋ 大眾運輸」的完整時間軸。**看哪部片、吃哪一家由 Jev 決定**，結果是 Jev 的前三名組合。

- 地區涵蓋全台 22 縣市：20 個縣市各有 1 個商圈（台北市 2 個），繞著該縣市的主要影城；嘉義縣（沒有影城）與連江縣（沒有場次資料）顯示「尚無資料」，但嘉義縣的高鐵嘉義仍可當出發站。澎湖、金門只能從島上出發。商圈與交通是手寫的估計值。
- 影城、電影、場次來自**開眼電影網**，餐廳來自 **OpenStreetMap**，伺服器會自動更新。
- 可以跨縣市出發（例如從台中車站到台北信義區），自動安排捷運、台鐵、高鐵、公車轉乘，並檢查各段末班車。

> 交通班次是**估計值**；票價、沒標營業時間的餐廳營業時間與價位也是估計，畫面上會標示。Jev 使用**模擬版**。

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

## 資料來源

| 名稱 | 檔案 | 內容 | 更新 |
|---|---|---|---|
| `base` | `app/collectors/fixture.py` | 商圈、車站、交通路段（`data/fixture/` 手寫的估計值） | 每次啟動 |
| `atmovies` | `app/collectors/atmovies.py` | 開眼電影網：商圈附近的影城、電影（片長、簡介）、今天起 4 天的場次 | 每天 |
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

交通建議改用交通部 TDX API（需要免費註冊）。交通資料的格式是路網：`stations`（站）＋`transit_links`（有方向的路段，含路線名稱、車程、班距、首末班）。

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
  collectors/      資料來源：開眼、OSM、商圈與交通、示意資料
  refresher.py     背景自動更新
  ingest.py        匯入與 Jev 預先判斷
  models.py        資料表
data/fixture/      示意資料（每個有商圈的縣市一個檔，另有 交通-跨縣市.json、交通-高鐵.json 與片單）
frontend/          網頁
db/init.sql        PostgreSQL 建立帳號與資料庫
tests/             自動測試
```

## 目前的限制

- Jev 模擬版的選片、選餐廳規則很簡單（片型符不符合、約會適合度、緩衝時間），只用來展示流程；實際效果要等接上 Jev API。
- 商圈與交通是手寫的估計值，每個有影城的縣市 1 個商圈（台北市 2 個）；離商圈車站步行超過 15 分鐘的影城排不到（例如嘉義市的嘉義秀泰、花蓮新天堂樂園、羅東日新、高雄夢時代）。交通以「車程＋最長班距」估算，不是真正的時刻表；東部、南迴線班距大，跨縣市行程會偏保守。
- 票價、沒標營業時間的餐廳營業時間、餐廳價位都是估計值；餐廳沒有評論。
- 開眼只提供今天起 4 天的場次。
- 只支援大眾運輸，不含開車、騎車。步行時間用直線距離 × 1.3 估算，車站到影城或餐廳最多走 15 分鐘。
- 候位時間是依尖峰離峰的粗略估計。

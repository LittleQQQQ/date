# 爬蟲設計：開眼電影網（電影、影城、場次）＋ OpenStreetMap（餐廳）

日期：2026-10-12

## 目標

把示意資料裡虛構的影城、電影、場次、餐廳換成爬蟲抓到的真資料。商圈和交通路網維持 `data/fixture/` 裡的手寫資料與估計值。

## 已確定的決定

| 項目 | 決定 |
|---|---|
| 範圍 | 開眼電影網（電影、影城、場次）、OpenStreetMap（餐廳）。交通先維持估計值，不接 TDX |
| 示意資料 | 真資料取代虛構資料。虛構的影城、餐廳、場次只給自動測試用 |
| 營業時間 | OSM 沒標的店依類型估計，畫面標「營業時間為估計」 |
| 更新時機 | 伺服器自動更新（啟動時補抓、每天早上再抓），保留手動指令 |
| 結構 | 一個網站一個 collector，沿用現有的 `Collector` 介面與 `app/ingest.py` |

不採用的來源：威秀官網（擋爬蟲，回 403）、國賓官網（目前只有「即將推出」頁）、Yahoo 電影（已關閉）、Google 地圖（條款不允許）、愛食記等評論網站（條款問題）。

## 架構

```
data/fixture/（商圈、車站、路段） ──► base       ─┐
開眼電影網                       ──► atmovies   ─┼─► ingest ─► 資料庫 ─► 排程、網頁（不用改）
OpenStreetMap（Overpass API）    ──► osm        ─┘
                     ▲ refresher：伺服器啟動時＋每天 REFRESH_HOUR 點
```

### 新增

| 檔案 | 職責 |
|---|---|
| `app/collectors/http.py` | 共用的抓網頁工具：同一網站每秒最多一次、表明身分的 User-Agent、逾時與最多 3 次重試、可選的磁碟快取（`data/cache/`，可指定有效天數）。collector 透過參數拿到它，測試時換成讀樣本檔的假版本 |
| `app/collectors/atmovies.py` | 開眼爬蟲。解析函式（純函式）＋ `AtmoviesCollector` |
| `app/collectors/osm.py` | OSM 爬蟲。Overpass 查詢、標籤轉換、`opening_hours` 解析＋ `OsmCollector` |
| `app/collectors/coverage.py` | 「商圈涵蓋範圍」：每個商圈的車站位置；判斷一個地點屬於哪個商圈（離該商圈任一車站步行 ≤ `STATION_WALK_MAX` 分鐘，取最近的商圈） |
| `app/refresher.py` | 背景更新：決定該不該抓（純函式）、背景執行緒、每天定時 |

### 修改

| 檔案 | 改動 |
|---|---|
| `app/collectors/fixture.py` | 新增 `BaseCollector`（名稱 `base`）：只讀 `areas`、`stations`、`transit_links`。`FixtureCollector`（`fixture`）維持完整虛構資料，給測試用 |
| `app/collectors/__init__.py` | 登記 `base`、`atmovies`、`osm`。需要涵蓋範圍的 collector 由 ingest 從資料庫建好 `Coverage` 後傳入 |
| `app/collectors/base.py` | `Batch` 新增 `showtime_cinemas`（這次成功抓到時刻表的影城 external_id；場次只替換這些影城在 `showtime_dates` 範圍內的舊資料）與 `restaurant_areas`（這次成功抓到的商圈；這些商圈裡這次沒出現的同來源舊餐廳會被移除）。`ShowtimeRecord`、`RestaurantRecord` 新增 `estimated: list[str]` |
| `app/models.py` | `showtimes`、`restaurants` 新增 `estimated`（JSON，例如 `["price"]`、`["hours", "price"]`）。結構改了，舊的 `dev.db` 要 `python -m app.ingest --reset` 重建一次 |
| `app/ingest.py` | `ensure_demo_data` 改為 `ensure_base_data`：跑 `base`，並刪掉資料庫裡 `source = "fixture"` 的影城、電影、場次、餐廳（舊的虛構內容）。`upsert_batch` 支援 `showtime_cinemas`、`restaurant_areas`。`--source` 預設改為 `base` |
| `app/jev/` | 新增題目 `GENRE`：「這部電影屬於哪一種片型？」，選項為 `GENRES`，context 是片名＋簡介。模擬版用關鍵字規則。匯入後，來源沒給片型的電影用 Jev 最高機率的選項填 `Movie.genre`；最高機率低於 `JEV_CONFIDENCE_THRESHOLD` 時留空（畫面顯示「片型未知」） |
| `app/main.py` | lifespan：建表、檢查結構、`ensure_base_data`、啟動 refresher；關閉時停止 refresher |
| `app/config.py` | 新增 `AUTO_REFRESH`（預設 `true`）、`REFRESH_HOUR`（預設 `6`） |
| `app/services.py`、`app/planner.py` | 排程結果與餐廳查詢帶出 `estimated`；狀態 API 回傳各來源最後成功時間、是否正在更新、最近錯誤 |
| `frontend/` | 資料狀態列、首次啟動提示與輪詢、估計值標示、頁尾來源標示 |
| `README.md` | 資料來源、快取、更新設定、來源標示、限制 |
| `.gitignore` | 加入 `data/cache/` |

## 開眼爬蟲（atmovies）

### 抓哪裡

商圈所在縣市 → 開眼地區代碼（已於 2026-10-12 從網站確認）：

| 縣市 | 代碼 | 縣市 | 代碼 |
|---|---|---|---|
| 基隆市 | a01 | 雲林縣 | a45 |
| 台北市、新北市 | a02 | 南投縣 | a49 |
| 桃園市 | a03 | 嘉義市、嘉義縣 | a05 |
| 新竹市、新竹縣 | a35 | 台南市 | a06 |
| 苗栗縣 | a37 | 高雄市 | a07 |
| 台中市 | a04 | 屏東縣 | a87 |
| 彰化縣 | a47 | 宜蘭縣 | a39 |
| 花蓮縣 | a38 | 台東縣 | a89 |
| 澎湖縣 | a69 | 金門縣 | a68 |

連江縣沒有代碼，略過。只抓資料庫裡有商圈的縣市。

### 流程

1. 地區頁 `https://www.atmovies.com.tw/showtime/{地區}/`：取得影城代碼與名稱（例如 `t02a01` 台北信義威秀）。
2. 地圖頁 `http://ww2.atmovies.com.tw/movie/GMap2018.cfm?tid={影城}`：取得經緯度（頁面裡的 `lat: …, lng: …`）。快取 30 天。不屬於任何商圈的影城直接略過、不寫入資料庫。
3. 時刻表 `https://www.atmovies.com.tw/showtime/{影城}/{地區}/` 與後面 3 天 `…/{YYYYMMDD}/`：
   - 影城地址：「地址:」那一行。
   - 每部片：片名、電影編號（`/movie/{編號}/`）、片長（「片長：117分」）、版本（`filmVersion`，例如「國語版」「4DX版」；沒有就記「數位」）、場次時間（全形冒號「10：20」）。
   - 早於 05:00 的時間視為過午夜，存成 `24:30` 這種寫法。
   - 只抓 collector 要求的日期範圍與網站實際提供的日期的交集。
4. 電影頁 `https://www.atmovies.com.tw/movie/{編號}/`：劇情簡介（去掉 HTML 標籤）。快取 30 天。

### 欄位

| 欄位 | 來源 |
|---|---|
| 影城 `external_id` | 開眼影城代碼 |
| 電影 `external_id` | 開眼電影編號；場次直接用它連到電影 |
| 片型 | 空字串，交給 Jev 的 `GENRE` 題 |
| 場次 `external_id` | `{影城}\|{電影}\|{版本}\|{日期}\|{時間}` |
| 場次 `label` | 版本文字 |
| 票價 | 估計：版本含 IMAX、4DX、巨幕、TITAN、ScreenX、Dolby 時 420 元，其他 300 元；`estimated = ["price"]` |
| `source_url` | 對應的開眼網址 |

沒有片長（或片長為 0）的電影不寫入，它的場次跳過，計入 `showtimes_skipped`。

### 錯誤處理

- 任何一個要抓的地區頁解析不到影城：整批失敗（網站可能改版），資料庫保留舊資料，錯誤記在 `scrape_runs`。
- 個別地圖頁、時刻表、電影頁失敗：跳過；該影城不列入 `showtime_cinemas`，它的舊場次保留。失敗頁數記在 `counts["failed_pages"]`。
- 電影頁失敗：電影照樣寫入，簡介留空。

### 請求量

第一次約 200 頁（大部分是地圖頁），以每秒一次計約 3～4 分鐘。之後每天約 40～60 頁（時刻表＋新片），約 1 分鐘。

## OSM 爬蟲（osm）

### 抓哪裡

每個商圈對 `https://overpass-api.de/api/interpreter` 送一次 POST 查詢：商圈每個車站周圍半徑 865 公尺（步行 15 分鐘 × 75 公尺 ÷ 1.3），`amenity` 為 `restaurant`、`cafe`、`fast_food`、`bar`、`pub`，且有 `name`。輸出含標籤與中心點座標。

### 欄位

| 欄位 | 來源 |
|---|---|
| `external_id` | `node/123`、`way/456` |
| 店名 | `name:zh` → `name` |
| 座標 | 節點座標或 way 的中心點 |
| 地址 | `addr:*` 組合，沒有就空字串 |
| 商圈 | `Coverage` 判斷（離商圈車站步行 ≤ 15 分鐘，取最近的商圈） |
| 介紹 | 由標籤組成，例如「日式拉麵餐廳，有素食選項，可訂位。」，再接 OSM 的 `description`（如果有） |
| 評論 | 空清單 |
| 營業時間 | 解析 `opening_hours`，見下方。解析不了或沒有時依類型估計，`estimated` 加 `"hours"` |
| 每人價位 | 依類型估計，`estimated` 加 `"price"` |
| 可訂位 | `reservation` 為 `yes`、`recommended`、`required` 時為真 |
| 最後點餐、候位 | 預設 30 分鐘、10 分鐘 |
| `source_url` | `https://www.openstreetmap.org/{type}/{id}` |

### 料理類型（寫進介紹，讓 Jev 的 `CUISINE` 題判斷）

| 專案類別 | OSM 標籤 |
|---|---|
| 日式 | `cuisine`：japanese、ramen、sushi、udon、izakaya、donburi、tonkatsu、curry（日式咖哩） |
| 韓式 | korean |
| 中式／台式 | chinese、taiwanese、dumpling、noodle、dim_sum、cantonese、beef_noodle |
| 義式／西式 | italian、pizza、pasta、french、steak_house、western、european、spanish |
| 美式 | american、burger、chicken（炸雞） |
| 泰式／南洋 | thai、vietnamese、indonesian、malaysian、singaporean、indian |
| 火鍋／燒肉 | hot_pot、barbecue、bbq、yakiniku、shabu-shabu |
| 咖啡／甜點 | `amenity=cafe`，或 `cuisine`：coffee_shop、dessert、cake、ice_cream、bubble_tea |
| 酒館 | `amenity=bar`、`amenity=pub` |

對不到的料理類型不寫，介紹只寫「餐廳」。

### 營業時間解析

支援的寫法（其他寫法視為解析不了）：

- `24/7` → 每天 `00:00`–`24:00`
- `Mo-Fr 11:00-21:00; Sa,Su 10:00-22:00`：星期區間、逗號列舉、分號分段
- 沒寫星期的 `11:00-21:00` → 每天
- `Mo off`、`Mo closed` → 該天公休（`null`）
- 結束早於開始（`18:00-02:00`）→ 跨午夜，存成 `26:00`
- 同一天分段營業（`11:30-14:00,17:30-22:00`）→ 取最後一段，避免排到中間休息時間
- `PH`（國定假日）規則忽略

轉成專案格式：最常出現的時段當 `default`，其他天寫例外。

### 估計值

| 類型 | 營業時間 | 每人價位 |
|---|---|---|
| `fast_food` | 10:00–22:00 | 200 |
| `cafe` | 10:00–19:00 | 300 |
| `restaurant` | 11:30–21:00 | 500；火鍋／燒肉、義式／西式為 800 |
| `bar`、`pub` | 19:00–25:00 | 700 |

### 每個商圈的上限

每個商圈最多 60 家（常數 `MAX_RESTAURANTS_PER_AREA`）。排序：先比資料完整度（有 `opening_hours` +2、有可對應的料理類型 +1），再比離最近影城的步行時間（資料庫裡該商圈的影城；還沒有影城時用離車站的距離）。

### 錯誤處理

- 某個商圈的查詢失敗（含 Overpass 回 429、504，重試後仍失敗）：該商圈不列入 `restaurant_areas`，舊資料保留。
- 所有商圈都失敗：整批失敗。

## 匯入規則

- `showtime_cinemas` 有值時：只刪除「同來源、在 `showtime_dates` 範圍內、影城在 `showtime_cinemas` 裡」的舊場次，再寫入新場次。沒有值時維持現在的行為（範圍內同來源的場次整批取代），示意資料不受影響。
- `restaurant_areas` 有值時：刪除「同來源、商圈在 `restaurant_areas` 裡、這次沒出現」的餐廳。
- 寫入影城、餐廳前確認 `area_id` 存在於 `areas`，不存在就略過並計數。

## 自動更新（refresher）

- 啟動時同步執行 `ensure_base_data`。
- `AUTO_REFRESH` 為真時開一條背景執行緒：
  1. `osm` 最後一次成功超過 7 天（或從沒成功過）→ 執行。
  2. `atmovies` 今天（台灣時間）還沒成功過 → 執行。
  3. 等到下一個 `REFRESH_HOUR` 點，回到 1。
- 失敗不重試到底：記錄錯誤，等下一輪。
- 執行緒用 `threading.Event` 停止，伺服器關閉時結束。
- 「該不該抓」寫成純函式 `due_sources(last_success: dict[str, datetime | None], now: datetime) -> list[str]`。
- 每次執行都走 `run_collector`（含 Jev 預先判斷）。

## 畫面

- 頂端資料狀態：各來源最後成功時間（例如「開眼 10/12 06:01・OSM 10/10」）；正在更新時顯示「資料更新中…」。
- 資料庫裡沒有今天以後的場次、而且正在抓開眼時：表單上方顯示「第一次啟動，正在抓取影城場次（約 3～4 分鐘）」，每 10 秒檢查狀態，完成後重新載入選單。
- 方案卡片、時間軸：
  - 場次 `estimated` 有 `price` → 「票價為估計」
  - 餐廳 `estimated` 有 `hours` → 「營業時間為估計，出發前請先確認」
  - 餐廳 `estimated` 有 `price` → 「價位為估計」
- 頁尾：「電影與場次：開眼電影網・餐廳：© OpenStreetMap 貢獻者（ODbL）・交通：估計值」。

## 測試

全部離線，網路請求用假的抓網頁工具讀 `tests/samples/` 的樣本。

- 樣本：開眼地區頁、時刻表（一天）、地圖頁、電影頁，一份 Overpass 回應。從真實網頁裁剪，只留解析需要的結構，簡介縮短成一兩句。
- 解析：地區頁的影城清單、地圖頁座標、時刻表（全形冒號、版本、無版本、過午夜、片長、地址）、電影頁簡介。
- OSM：`opening_hours` 各種寫法（24/7、星期區間、列舉、公休、跨午夜、分段、解析不了）、料理類型對應、介紹組字、估計值、每商圈上限與排序。
- `Coverage`：在範圍內、範圍外、兩個商圈都近時取最近。
- collector：假抓網頁工具 → `Batch` 內容正確；地區頁解析失敗時拋錯；個別頁失敗時跳過並計數。
- 匯入：某影城抓失敗時保留它的舊場次；餐廳依商圈汰換；`ensure_base_data` 清掉虛構內容；片型由 Jev 填入。
- refresher：`due_sources` 的各種情況。
- 既有 45 個測試照樣通過：`conftest` 設 `AUTO_REFRESH=false`，伺服器啟動（`ensure_base_data` 會清掉虛構內容）之後再明確執行 `fixture` 匯入。
- 手動驗收：實際執行 `python -m app.ingest --source osm`、`--source atmovies`，啟動伺服器，在瀏覽器排一次約會。

## 不做的事

- TDX 交通 API（交通維持估計值）。
- 餐廳評論、真實價位、候位時間（沒有合法來源）。
- 商圈以外的影城與餐廳。
- 票價爬取（開眼沒有票價）。

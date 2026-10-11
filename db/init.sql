-- 建立專案用的 PostgreSQL 帳號與資料庫。請用 postgres 管理員帳號執行：
--   & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U postgres -f db\init.sql
-- 執行前先把下面的 change_me 改成你自己的密碼，並同步修改 .env 的 DATABASE_URL。
-- 資料表不用手動建，啟動伺服器或執行 python -m app.ingest 時會自動建立。

CREATE ROLE date_planner WITH LOGIN PASSWORD 'change_me';
CREATE DATABASE date_planner OWNER date_planner ENCODING 'UTF8';

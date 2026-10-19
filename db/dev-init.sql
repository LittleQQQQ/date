-- compose.dev.yml 第一次建立資料庫時執行：另外建一個給 pytest 用的資料庫，並開啟 PostGIS。
-- 開發用的 date_v2 由 Alembic 的第一個 migration 開啟 PostGIS。
CREATE DATABASE date_v2_test OWNER date_planner;
\connect date_v2_test
CREATE EXTENSION IF NOT EXISTS postgis;

WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM src WHERE ("Order Date" >= DATE '2023-01-01' AND "Order Date" <= DATE '2024-12-31')
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT CAST(date_trunc('month', "Order Date") AS DATE) AS "__k0", sum("Profit") AS "__a0", sum("Sales") AS "__a1" FROM dimf GROUP BY 1
),
"meas" AS (
  SELECT *, "__a0" AS "__m0", ((CAST("__a0" AS DOUBLE) / NULLIF(CAST("__a1" AS DOUBLE), 0))) AS "__m1", "__a1" AS "__m2" FROM agg
),
"mf" AS (
  SELECT * FROM meas WHERE ("__m2" >= CAST(20000.0 AS DOUBLE))
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "trunc_month(f_order_date)", "__m0" AS "sum(f_profit)", "__m1" AS "f_calc_1368249927221915648"
FROM mf
ORDER BY "__k0" ASC NULLS LAST

WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 3 段：FIXED LOD、集合、Top N
"topn_agg" AS (
  SELECT "Customer Name" AS "__k0", sum("Sales") AS "__a0" FROM src GROUP BY 1
),
"topn" AS (
  SELECT "__k0" AS "__member" FROM topn_agg ORDER BY "__a0" DESC NULLS LAST, "__k0" ASC NULLS LAST LIMIT 10
),
"sets_ext" AS (
  SELECT *, EXISTS (SELECT 1 FROM topn WHERE topn."__member" IS NOT DISTINCT FROM "Customer Name") AS "__set_0" FROM src
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM sets_ext WHERE ("Region" IS NULL OR NOT ("Region" IN ('Central')))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "__set_0" AS "__k0", sum("Sales") AS "__a0", count(DISTINCT "Customer Name") AS "__a1" FROM dimf GROUP BY 1
),
"meas" AS (
  SELECT *, "__a0" AS "__m0", "__a1" AS "__m1" FROM agg
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_top_customers", "__m0" AS "sum(f_sales)", "__m1" AS "countd(f_customer_name)"
FROM meas
ORDER BY "__k0" DESC NULLS LAST

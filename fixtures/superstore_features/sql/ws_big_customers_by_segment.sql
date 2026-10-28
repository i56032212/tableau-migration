WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 3 段：FIXED LOD、Top N
"cond_agg" AS (
  SELECT "Customer Name" AS "__k0", sum("Sales") AS "__a0" FROM src GROUP BY 1
),
"cond" AS (
  SELECT "__k0" AS "__member" FROM cond_agg WHERE ("__a0" > 5000)
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM src WHERE EXISTS (SELECT 1 FROM cond WHERE cond."__member" IS NOT DISTINCT FROM "Customer Name")
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Segment" AS "__k0", count(DISTINCT "Customer Name") AS "__a0" FROM dimf GROUP BY 1
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_segment", "__m0" AS "countd(f_customer_name)"
FROM meas
ORDER BY "__k0" ASC NULLS LAST

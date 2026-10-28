WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM src WHERE (NOT (("Region" IS NOT DISTINCT FROM 'West' AND "Category" IS NOT DISTINCT FROM 'Furniture') OR ("Region" IS NOT DISTINCT FROM 'East' AND "Category" IS NOT DISTINCT FROM 'Technology')))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Region" AS "__k0", "Category" AS "__k1", sum("Sales") AS "__a0" FROM dimf GROUP BY 1, 2
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_region", "__k1" AS "f_category", "__m0" AS "sum(f_sales)"
FROM meas
ORDER BY "__k0" ASC NULLS LAST, "__k1" ASC NULLS LAST

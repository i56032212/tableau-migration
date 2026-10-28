WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Category" AS "__k0", min(0) AS "__a0", sum("Sales") AS "__a1" FROM src GROUP BY 1
),
"meas" AS (
  SELECT *, ("__a0") AS "__m0", "__a1" AS "__m1" FROM agg
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_category", "__m0" AS "f_zero", "__m1" AS "sum(f_sales)"
FROM meas
ORDER BY "__k0" ASC NULLS LAST

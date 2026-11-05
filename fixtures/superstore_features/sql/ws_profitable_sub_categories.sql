WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 3 段：FIXED LOD、集合、Top N
"cond_agg" AS (
  SELECT "Sub-Category" AS "__k0", sum("Profit") AS "__a0" FROM src GROUP BY 1
),
"cond" AS (
  SELECT "__k0" AS "__member" FROM cond_agg WHERE ("__a0" > 0)
),
"sets_ext" AS (
  SELECT *, EXISTS (SELECT 1 FROM cond WHERE cond."__member" IS NOT DISTINCT FROM "Sub-Category") AS "__set_0" FROM src
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Sub-Category" AS "__k0", "__set_0" AS "__k1", sum("Profit") AS "__a0" FROM sets_ext GROUP BY 1, 2
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_sub_category", "__k1" AS "f_profitable_sub_categories", "__m0" AS "sum(f_profit)"
FROM meas
ORDER BY "__k0" ASC NULLS LAST, "__k1" DESC NULLS LAST

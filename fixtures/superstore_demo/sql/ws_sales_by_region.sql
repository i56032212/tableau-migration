WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 3 段：FIXED LOD、Top N
"topn_agg" AS (
  SELECT "Region" AS "__k0", sum("Sales") AS "__a0" FROM src GROUP BY 1
),
"topn" AS (
  SELECT "__k0" AS "__member" FROM topn_agg ORDER BY "__a0" DESC NULLS LAST, "__k0" ASC NULLS LAST LIMIT 3
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM src WHERE ("Category" IN ('Furniture', 'Technology')) AND EXISTS (SELECT 1 FROM topn WHERE topn."__member" IS NOT DISTINCT FROM "Region")
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Region" AS "__k0", "Category" AS "__k1", sum("Sales") AS "__a0" FROM dimf GROUP BY 1, 2
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg
),
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 7 段：Table Calculation 與合計
"sort_agg" AS (
  SELECT "Region" AS "__k0", sum("Sales") AS "__a0" FROM dimf GROUP BY 1
),
"sort" AS (
  SELECT "__k0" AS "__key", "__a0" AS "__v" FROM sort_agg
),
"sorted" AS (
  SELECT m.*, s0."__v" AS "__sort0" FROM meas m LEFT JOIN sort s0 ON s0."__key" IS NOT DISTINCT FROM m."__k0"
),
"tc" AS (
  SELECT m.*, sum("__a0") OVER (PARTITION BY "__k1" ORDER BY "__sort0" DESC NULLS LAST, "__k0" ASC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS "__m1" FROM sorted m
)
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_region", "__k1" AS "f_category", "__m0" AS "sum(f_sales)", "__m1" AS "sum(f_sales)#tc_running_total_sales", "__sort0" AS "__sort(f_region)"
FROM tc
ORDER BY "__sort0" DESC NULLS LAST, "__k0" ASC NULLS LAST, "__k1" ASC NULLS LAST

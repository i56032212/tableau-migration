WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 3 段：FIXED LOD、Top N
"lod" AS (
  SELECT "Region" AS "__d0", sum("Sales") AS "__v" FROM src GROUP BY 1
),
"ctx_ext" AS (
  SELECT r.*, l0."__v" AS "__lod_0" FROM src r LEFT JOIN lod l0 ON "r"."Region" IS NOT DISTINCT FROM l0."__d0"
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM ctx_ext WHERE ("Segment" IN ('Consumer', 'Corporate'))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Region" AS "__k0", CAST(year("Order Date") AS BIGINT) AS "__k1", sum("Sales") AS "__a0" FROM dimf GROUP BY 1, 2
),
"combos" AS (
  SELECT DISTINCT "Region" AS "__g0", CAST(year("Order Date") AS BIGINT) AS "__g1", "__lod_0" FROM dimf
),
"combo_agg" AS (
  SELECT "__g0" AS "__k0", "__g1" AS "__k1", sum(("__lod_0")) AS "__a1" FROM combos GROUP BY 1, 2
),
"agg_1" AS (
  SELECT b.*, c0."__a1" FROM agg b LEFT JOIN combo_agg c0 ON b."__k0" IS NOT DISTINCT FROM c0."__k0" AND b."__k1" IS NOT DISTINCT FROM c0."__k1"
),
"meas" AS (
  SELECT *, ((CAST("__a0" AS DOUBLE) / NULLIF(CAST("__a1" AS DOUBLE), 0))) AS "__m0", "__a0" AS "__m1" FROM agg_1
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_region", "__k1" AS "year(f_order_date)", "__m0" AS "f_calc_2049003526124994561", "__m1" AS "sum(f_sales)"
FROM meas
ORDER BY "__k0" ASC NULLS LAST, "__k1" ASC NULLS LAST

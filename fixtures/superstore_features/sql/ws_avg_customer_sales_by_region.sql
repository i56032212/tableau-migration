WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 3 段：FIXED LOD、集合、Top N
"lod" AS (
  SELECT "Region" AS "__d0", "Customer Name" AS "__d1", sum("Sales") AS "__v" FROM src GROUP BY 1, 2
),
"ctx_ext" AS (
  SELECT r.*, l0."__v" AS "__lod_1" FROM src r LEFT JOIN lod l0 ON "r"."Region" IS NOT DISTINCT FROM l0."__d0" AND "r"."Customer Name" IS NOT DISTINCT FROM l0."__d1"
),
"lod_agg" AS (
  SELECT "Region" AS "__k0" FROM ctx_ext GROUP BY 1
),
"combos" AS (
  SELECT DISTINCT "Region" AS "__g0", "Customer Name" AS "__g1", "__lod_1" FROM ctx_ext
),
"combo_agg" AS (
  SELECT "__g0" AS "__k0", avg("__lod_1") AS "__a0" FROM combos GROUP BY 1
),
"lod_agg_1" AS (
  SELECT b.*, c0."__a0" FROM lod_agg b LEFT JOIN combo_agg c0 ON b."__k0" IS NOT DISTINCT FROM c0."__k0"
),
"lod_1" AS (
  SELECT "__k0", "__a0" AS "__v" FROM lod_agg_1
),
"ctx_ext_1" AS (
  SELECT r.*, l0."__v" AS "__lod_0" FROM ctx_ext r LEFT JOIN lod_1 l0 ON "r"."Region" IS NOT DISTINCT FROM l0."__k0"
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM ctx_ext_1 WHERE ("Region" IS NULL OR NOT ("Region" IN ('Central')))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Region" AS "__k0" FROM dimf GROUP BY 1
),
"combos_1" AS (
  SELECT DISTINCT "Region" AS "__g0", "__lod_0" FROM dimf
),
"combo_agg_1" AS (
  SELECT "__g0" AS "__k0", sum(("__lod_0")) AS "__a0" FROM combos_1 GROUP BY 1
),
"agg_1" AS (
  SELECT b.*, c0."__a0" FROM agg b LEFT JOIN combo_agg_1 c0 ON b."__k0" IS NOT DISTINCT FROM c0."__k0"
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg_1
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_region", "__m0" AS "sum(f_avg_customer_sales)"
FROM meas
ORDER BY "__k0" ASC NULLS LAST

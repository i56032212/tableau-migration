WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM src WHERE ("Region" IS NULL OR NOT ("Region" IN ('Central')))
),
-- 第 5 段：INCLUDE／EXCLUDE LOD
"lod" AS (
  SELECT "Region" AS "__d0", "Order ID" AS "__d1", sum("Sales") AS "__v" FROM dimf GROUP BY 1, 2
),
"dimf_ext" AS (
  SELECT r.*, l0."__v" AS "__lod_1" FROM dimf r LEFT JOIN lod l0 ON "r"."Region" IS NOT DISTINCT FROM l0."__d0" AND "r"."Order ID" IS NOT DISTINCT FROM l0."__d1"
),
"lod_agg" AS (
  SELECT "Region" AS "__k0" FROM dimf_ext GROUP BY 1
),
"combos" AS (
  SELECT DISTINCT "Region" AS "__g0", "Order ID" AS "__g1", "__lod_1" FROM dimf_ext
),
"combo_agg" AS (
  SELECT "__g0" AS "__k0", max("__lod_1") AS "__a0" FROM combos GROUP BY 1
),
"lod_agg_1" AS (
  SELECT b.*, c0."__a0" FROM lod_agg b LEFT JOIN combo_agg c0 ON b."__k0" IS NOT DISTINCT FROM c0."__k0"
),
"lod_1" AS (
  SELECT "__k0", "__a0" AS "__v" FROM lod_agg_1
),
"dimf_ext_1" AS (
  SELECT r.*, l0."__v" AS "__lod_0" FROM dimf_ext r LEFT JOIN lod_1 l0 ON "r"."Region" IS NOT DISTINCT FROM l0."__k0"
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Region" AS "__k0", "Category" AS "__k1" FROM dimf_ext_1 GROUP BY 1, 2
),
"combos_1" AS (
  SELECT DISTINCT "Region" AS "__g0", "Category" AS "__g1", "__lod_0" FROM dimf_ext_1
),
"combo_agg_1" AS (
  SELECT "__g0" AS "__k0", "__g1" AS "__k1", sum(("__lod_0")) AS "__a0" FROM combos_1 GROUP BY 1, 2
),
"agg_1" AS (
  SELECT b.*, c0."__a0" FROM agg b LEFT JOIN combo_agg_1 c0 ON b."__k0" IS NOT DISTINCT FROM c0."__k0" AND b."__k1" IS NOT DISTINCT FROM c0."__k1"
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg_1
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_region", "__k1" AS "f_category", "__m0" AS "sum(f_top_order_sales)"
FROM meas
ORDER BY "__k0" ASC NULLS LAST, "__k1" ASC NULLS LAST

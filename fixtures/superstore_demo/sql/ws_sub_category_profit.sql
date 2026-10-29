WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 5 段：INCLUDE／EXCLUDE LOD
"lod" AS (
  SELECT "Sub-Category" AS "__d0", "Order ID" AS "__d1", sum("Profit") AS "__v" FROM src GROUP BY 1, 2
),
"dimf_ext" AS (
  SELECT r.*, l0."__v" AS "__lod_0" FROM src r LEFT JOIN lod l0 ON "r"."Sub-Category" IS NOT DISTINCT FROM l0."__d0" AND "r"."Order ID" IS NOT DISTINCT FROM l0."__d1"
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Sub-Category" AS "__k0", sum("Profit") AS "__a0" FROM dimf_ext GROUP BY 1
),
"combos" AS (
  SELECT DISTINCT "Sub-Category" AS "__g0", "Order ID" AS "__g1", "__lod_0" FROM dimf_ext
),
"combo_agg" AS (
  SELECT "__g0" AS "__k0", avg("__lod_0") AS "__a1" FROM combos GROUP BY 1
),
"agg_1" AS (
  SELECT b.*, c0."__a1" FROM agg b LEFT JOIN combo_agg c0 ON b."__k0" IS NOT DISTINCT FROM c0."__k0"
),
"meas" AS (
  SELECT *, "__a0" AS "__m0", ("__a1") AS "__m2" FROM agg_1
),
-- 第 7 段：Table Calculation 與合計
"sort_agg" AS (
  SELECT "Sub-Category" AS "__k0", sum("Profit") AS "__a0" FROM dimf_ext GROUP BY 1
),
"sort" AS (
  SELECT "__k0" AS "__key", "__a0" AS "__v" FROM sort_agg
),
"sorted" AS (
  SELECT m.*, s0."__v" AS "__sort0" FROM meas m LEFT JOIN sort s0 ON s0."__key" IS NOT DISTINCT FROM m."__k0"
),
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
"tc" AS (
  SELECT m.*, ((CASE WHEN ("__a0") IS NULL THEN NULL ELSE CAST(rank() OVER (PARTITION BY (("__a0") IS NULL) ORDER BY "__a0" DESC) AS BIGINT) END)) AS "__m1" FROM sorted m
)
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_sub_category", "__m0" AS "sum(f_profit)", "__m1" AS "f_calc_5418932014720188416#tc_formula_f_calc_5418932014720188416", "__m2" AS "f_calc_5418932014720745473", "__sort0" AS "__sort(f_sub_category)"
FROM tc WHERE ("__m1" <= 10)
ORDER BY "__sort0" DESC NULLS LAST, "__k0" ASC NULLS LAST

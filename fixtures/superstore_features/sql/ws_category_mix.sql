WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Category" AS "__k0", sum("Sales") AS "__a0" FROM src GROUP BY 1
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg
),
-- 第 7 段：Table Calculation 與合計
"marks_rows" AS (
  SELECT r.* FROM src r WHERE EXISTS (SELECT 1 FROM meas k WHERE k."__k0" IS NOT DISTINCT FROM "r"."Category")
),
"total_agg" AS (
  SELECT sum("Sales") AS "__a0" FROM marks_rows
),
"total" AS (
  SELECT "__a0" AS "__v" FROM total_agg
),
"totals" AS (
  SELECT m.*, t0."__v" AS "__total0" FROM meas m LEFT JOIN total t0 ON TRUE
),
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
"tc" AS (
  SELECT m.*, (CAST("__a0" AS DOUBLE) / NULLIF(CAST("__total0" AS DOUBLE), 0)) AS "__m1" FROM totals m
)
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_category", "__m0" AS "sum(f_sales)", "__m1" AS "sum(f_sales)#tc_percent_of_total_sales"
FROM tc
ORDER BY regexp_replace(regexp_replace("__k0", '([0-9]+)', '00000000000000000000\1', 'g'), '[0-9]*([0-9]{20})', '\1', 'g') ASC NULLS LAST, "__k0" ASC NULLS LAST

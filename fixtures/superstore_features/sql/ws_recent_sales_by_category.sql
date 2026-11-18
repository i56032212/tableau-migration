WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 4 段：維度篩選
"dimf" AS (
  SELECT * FROM src WHERE ("Order Date" >= (CAST(date_trunc('month', current_date) AS DATE) + to_months(CAST(-2 AS INTEGER))) AND "Order Date" < (CAST(date_trunc('month', current_date) AS DATE) + to_months(CAST(1 AS INTEGER))))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT "Category" AS "__k0", sum("Sales") AS "__a0" FROM dimf GROUP BY 1
),
"meas" AS (
  SELECT *, "__a0" AS "__m0" FROM agg
)
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "f_category", "__m0" AS "sum(f_sales)"
FROM meas
ORDER BY regexp_replace(regexp_replace("__k0", '([0-9]+)', '00000000000000000000\1', 'g'), '[0-9]*([0-9]{20})', '\1', 'g') ASC NULLS LAST, "__k0" ASC NULLS LAST

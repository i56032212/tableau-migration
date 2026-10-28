WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT CAST(year("Order Date") AS BIGINT) AS "__k0", sum("Sales") AS "__a0" FROM src GROUP BY 1
),
"meas" AS (
  SELECT *, "__a0" AS "__m2" FROM agg
),
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 7 段：Table Calculation 與合計
"tc" AS (
  SELECT m.*, (CAST((COALESCE("__a0", 0) - lag(COALESCE("__a0", 0), 1) OVER (ORDER BY "__k0" ASC NULLS LAST)) AS DOUBLE) / NULLIF(CAST(abs(lag(COALESCE("__a0", 0), 1) OVER (ORDER BY "__k0" ASC NULLS LAST)) AS DOUBLE), 0)) AS "__m0", (COALESCE("__a0", 0) - lag(COALESCE("__a0", 0), 1) OVER (ORDER BY "__k0" ASC NULLS LAST)) AS "__m1" FROM meas m
)
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "year(f_order_date)", "__m0" AS "sum(f_sales)#tc_percent_difference_sales", "__m1" AS "sum(f_sales)#tc_difference_sales", "__m2" AS "sum(f_sales)"
FROM tc
ORDER BY "__k0" ASC NULLS LAST

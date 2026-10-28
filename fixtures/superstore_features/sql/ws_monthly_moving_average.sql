WITH
-- 第 1 段：萃取篩選與資料源篩選
"src" AS (
  SELECT * FROM "Extract"."Extract" WHERE ("Country/Region" IN ('United States'))
),
-- 第 6 段：彙總與量值篩選
"agg" AS (
  SELECT CAST(date_trunc('month', "Order Date") AS DATE) AS "__k0", sum("Sales") AS "__a0" FROM src GROUP BY 1
),
"meas" AS (
  SELECT *, "__a0" AS "__m1" FROM agg
),
-- ↑ 以上可下推到來源資料庫；以下一律在 DuckDB 執行
-- 第 7 段：Table Calculation 與合計
"tc" AS (
  SELECT m.*, avg("__a0") OVER (ORDER BY "__k0" ASC NULLS LAST ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS "__m0", (CASE WHEN (((1 - row_number() OVER (ORDER BY "__k0" ASC NULLS LAST)) <= -1) AND ((count(*) OVER () - row_number() OVER (ORDER BY "__k0" ASC NULLS LAST)) >= 1)) THEN sum("__a0") OVER (ORDER BY "__k0" ASC NULLS LAST ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) END) AS "__m2" FROM meas m
)
-- 第 8 段：Table Calculation 篩選與輸出
SELECT "__k0" AS "trunc_month(f_order_date)", "__m0" AS "sum(f_sales)#tc_moving_sales", "__m1" AS "sum(f_sales)", "__m2" AS "sum(f_sales)#tc_moving_sales_2"
FROM tc
ORDER BY "__k0" ASC NULLS LAST

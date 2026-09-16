-- An empty gold table is a failure that no column test catches.
--
-- Both silent failures this project has seen were partial or absent loads:
-- dim_fixture stopping at 446 rows of 4,527, and an 80-minute Spark job
-- rejected at the write. `unique` and `not_null` pass perfectly on a table
-- with nothing in it, so emptiness has to be asserted on purpose.
--
-- A dbt singular test fails when it RETURNS rows, so this selects one row
-- only when the count is zero.

select 1 as empty_gold_table
from (select count(*) as n from {{ ref('gold_market_efficiency') }})
where n = 0

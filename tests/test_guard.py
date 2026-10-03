import pytest

from nycynapse.guard import check


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from gold.fct_service_request",
        "with x as (select * from gold.dim_borough) select * from x",
        "select * from gold.dim_borough union all select * from lake.gold.dim_borough",
        "select r from gold.fct_subway_alert, unnest(route_ids) t(r)",
    ],
)
def test_allows_plain_selects(sql):
    r = check(sql)
    assert r.ok, r.reason


@pytest.mark.parametrize(
    "sql,why",
    [
        ("delete from gold.dim_borough", "SELECT"),
        ("drop table gold.dim_borough", "SELECT"),
        ("select 1; select 2", "one statement"),
        ("copy (select * from gold.dim_borough) to '/tmp/x.csv'", "SELECT"),
        ("select * from read_csv('/etc/passwd')", "read_csv"),
        ("select * from gold.dim_borough where getenv('HOME') is not null", "getenv"),
        ("select * from silver.requests_311", "outside the gold schema"),
        ("select * from ducklake.ducklake_snapshot", "outside the gold schema"),
        ("select * from information_schema.tables", "outside the gold schema"),
        ("select 1", "no gold table"),
        ("set enable_external_access = true", "SELECT"),
        ("attach '/tmp/x.db' as x", "SELECT"),
    ],
)
def test_rejects_unsafe_sql(sql, why):
    r = check(sql)
    assert not r.ok and why in r.reason, r.reason


def test_reports_tables_used():
    r = check("select * from gold.fct_collision c join gold.dim_neighborhood n using (nta_code)")
    assert r.tables == ["gold.dim_neighborhood", "gold.fct_collision"]

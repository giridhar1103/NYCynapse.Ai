from nycynapse.catalog import documents, store, sync


def test_documents_cover_every_object(catalog, manifest):
    docs = documents.build(catalog, manifest)
    kinds = {d.kind for d in docs}
    assert {"workspace", "model", "dimension", "metric", "relationship", "place", "period"} <= kinds
    ids = [d.id for d in docs]
    assert len(ids) == len(set(ids))
    wait = next(d for d in docs if d.id == "metric:avg_app_wait_minutes")
    assert "wait time" in wait.body and wait.workspace == "rideshare_taxi"


def test_column_docs_from_dbt_reach_dimensions(catalog, manifest):
    docs = {d.id: d for d in documents.build(catalog, manifest)}
    doc = docs["dimension:service_requests.complaint_type"]
    assert "Noise - Residential" in doc.body


def test_publish_makes_one_current_version(catalog, manifest, app_dsn, monkeypatch):
    monkeypatch.setattr(sync, "embed", lambda texts: [[0.0] * 383 + [1.0] for _ in texts])
    conn = store.connect(app_dsn)
    conn.execute("DROP SCHEMA IF EXISTS catalog CASCADE")
    store.migrate(conn)
    sync.publish(conn, catalog, manifest, version="t1", lake=None)
    stats = sync.publish(conn, catalog, manifest, version="t2", lake=None)
    assert stats["objects"] > 300
    assert store.current_version(conn) == "t2"
    n = conn.execute("SELECT count(*) AS n FROM catalog.objects WHERE version = 't1'").fetchone()
    assert n["n"] == stats["objects"]


def test_normalize_for_value_matching():
    assert sync.normalize("Noise - Residential") == "noise residential"
    assert sync.normalize("HEAT/HOT WATER") == "heat hot water"

"""GET /api/v1/datasets/{dataset_id}/records."""


def _seed(ds_ns, make_dataset, rows, total=5):
    ds_ns.db.get_results["dst_1"] = make_dataset(user_id=7, id="dst_1")
    ds_ns.db.queue_result(scalar=total)  # count query
    ds_ns.db.queue_result(items=rows)    # page query


def test_records_envelope_and_row_shape(ds_ns, make_dataset, make_row):
    _seed(
        ds_ns,
        make_dataset,
        [make_row(idx=0, data={"n": "a"}), make_row(idx=1, data={"n": "b"})],
        total=5,
    )

    resp = ds_ns.client.get("/api/v1/datasets/dst_1/records?page=1&page_size=2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["records"]) == 2

    first = body["records"][0]
    assert set(first.keys()) == {"id", "data", "created_at"}
    assert first["id"] == "drow_0"
    assert first["data"] == {"n": "a"}
    assert first["created_at"].endswith(("Z", "+00:00")) or "T" in first["created_at"]


def test_records_second_page_echoes_params(ds_ns, make_dataset, make_row):
    _seed(ds_ns, make_dataset, [make_row(idx=7)], total=3)

    resp = ds_ns.client.get("/api/v1/datasets/dst_1/records?page=2&page_size=50")

    assert resp.status_code == 200
    body = resp.json()
    assert (body["page"], body["page_size"]) == (2, 50)
    assert body["records"][0]["id"] == "drow_7"


def test_records_validation_errors(ds_ns):
    for query in ("?page=0", "?page_size=501", "?page=-1"):
        resp = ds_ns.client.get(f"/api/v1/datasets/dst_1/records{query}")
        assert resp.status_code == 422, query


def test_records_foreign_dataset_404(ds_ns, make_dataset):
    ds_ns.db.get_results["dst_8"] = make_dataset(user_id=8, id="dst_8")
    resp = ds_ns.client.get("/api/v1/datasets/dst_8/records")
    assert resp.status_code == 404


def test_records_missing_dataset_404(ds_ns):
    resp = ds_ns.client.get("/api/v1/datasets/dst_ghost/records")
    assert resp.status_code == 404

"""GET /api/v1/datasets/{dataset_id}/schema."""

SCHEMA = {
    "name": {"type": "string", "aggregable": False},
    "amount": {"type": "number", "aggregable": True},
}


def test_schema_echoed_for_owner(ds_ns, make_dataset):
    ds_ns.db.get_results["dst_1"] = make_dataset(
        user_id=7, id="dst_1", dataset_schema=SCHEMA
    )

    resp = ds_ns.client.get("/api/v1/datasets/dst_1/schema")

    assert resp.status_code == 200
    assert resp.json() == {"dataset_id": "dst_1", "schema": SCHEMA}


def test_schema_foreign_dataset_404(ds_ns, make_dataset):
    ds_ns.db.get_results["dst_8"] = make_dataset(user_id=8, id="dst_8")
    resp = ds_ns.client.get("/api/v1/datasets/dst_8/schema")
    assert resp.status_code == 404

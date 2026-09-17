"""GET /api/v1/datasets/{dataset_id}."""


def test_get_owned_dataset(ds_ns, make_dataset):
    ds_ns.db.get_results["dst_1"] = make_dataset(user_id=7, id="dst_1", name="Sales")

    resp = ds_ns.client.get("/api/v1/datasets/dst_1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "dst_1"
    assert body["name"] == "Sales"


def test_get_missing_dataset_404(ds_ns):
    resp = ds_ns.client.get("/api/v1/datasets/dst_ghost")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Dataset not found"


def test_get_foreign_dataset_404_no_leak(ds_ns, make_dataset):
    # Belongs to user 8; requester is user 7 -> indistinguishable from missing.
    ds_ns.db.get_results["dst_9"] = make_dataset(user_id=8, id="dst_9")

    resp = ds_ns.client.get("/api/v1/datasets/dst_9")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Dataset not found"

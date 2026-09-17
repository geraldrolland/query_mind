"""GET /api/v1/datasets."""


def test_list_returns_owner_datasets(ds_ns, make_dataset):
    ds_ns.db.queue_result(
        items=[
            make_dataset(id="dst_a", name="Alpha"),
            make_dataset(id="dst_b", name="Beta", description="second"),
        ]
    )

    resp = ds_ns.client.get("/api/v1/datasets")

    assert resp.status_code == 200
    body = resp.json()["datasets"]
    assert [d["id"] for d in body] == ["dst_a", "dst_b"]
    assert body[0]["name"] == "Alpha"
    assert body[1]["description"] == "second"
    for entry in body:
        assert set(entry.keys()) == {
            "id", "name", "description", "populated",
            "total_rows", "total_size_bytes", "created_at", "updated_at",
        }


def test_list_empty(ds_ns):
    ds_ns.db.queue_result(items=[])
    resp = ds_ns.client.get("/api/v1/datasets")
    assert resp.status_code == 200
    assert resp.json() == {"datasets": []}

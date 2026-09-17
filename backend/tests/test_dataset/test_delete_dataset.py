"""DELETE /api/v1/datasets/{dataset_id}."""


def test_delete_owned_dataset(ds_ns, make_dataset, cache_recorder):
    ds = make_dataset(user_id=7, id="dst_1")
    ds_ns.db.get_results["dst_1"] = ds

    resp = ds_ns.client.delete("/api/v1/datasets/dst_1")

    assert resp.status_code == 200
    assert resp.json() == {"message": "Dataset deleted successfully"}
    assert ds in ds_ns.db.deleted_objects
    assert ds_ns.db.commits >= 1
    assert cache_recorder == [7]  # user cache invalidated


def test_delete_missing_dataset_404(ds_ns, cache_recorder):
    resp = ds_ns.client.delete("/api/v1/datasets/dst_ghost")
    assert resp.status_code == 404
    assert ds_ns.db.deleted_objects == []
    assert cache_recorder == []


def test_delete_foreign_dataset_404(ds_ns, make_dataset, cache_recorder):
    ds_ns.db.get_results["dst_8"] = make_dataset(user_id=8, id="dst_8")

    resp = ds_ns.client.delete("/api/v1/datasets/dst_8")

    assert resp.status_code == 404
    assert ds_ns.db.deleted_objects == []
    assert cache_recorder == []

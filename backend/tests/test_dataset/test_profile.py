"""GET /api/v1/datasets/{dataset_id}/profile."""


class FakeProfiler:
    instantiated = []

    def __init__(self, dataset_id, dsl_definition=None):
        self.dataset_id = dataset_id
        FakeProfiler.instantiated.append(dataset_id)

    def profile(self):
        return {
            "dataset_id": self.dataset_id,
            "row_count": 3,
            "columns": [{"name": "amount", "type": "number"}],
            "quality": {"columns_with_nulls": 0},
            "sample_rows": [],
        }


def test_profile_delegates_to_profiler(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    FakeProfiler.instantiated = []
    monkeypatch.setattr(ds_mod, "Profiler", FakeProfiler)
    ds_ns.db.get_results["dst_1"] = make_dataset(user_id=7, id="dst_1")

    resp = ds_ns.client.get("/api/v1/datasets/dst_1/profile")

    assert resp.status_code == 200
    assert FakeProfiler.instantiated == ["dst_1"]
    body = resp.json()
    assert body["dataset_id"] == "dst_1"
    assert body["row_count"] == 3


def test_profile_foreign_dataset_404(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    FakeProfiler.instantiated = []
    monkeypatch.setattr(ds_mod, "Profiler", FakeProfiler)
    ds_ns.db.get_results["dst_8"] = make_dataset(user_id=8, id="dst_8")

    resp = ds_ns.client.get("/api/v1/datasets/dst_8/profile")
    assert resp.status_code == 404
    assert FakeProfiler.instantiated == []  # never constructed

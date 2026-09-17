"""POST /api/v1/datasets/{dataset_id}/query."""

from app.schemas.query import DslDefinitionSchema

DSL = {"select": ["name"]}


def _seed_owned_populated(ds_ns, make_dataset, populated=True, user_id=7):
    ds_ns.db.get_results["dst_1"] = make_dataset(
        user_id=user_id, id="dst_1", populated=populated
    )


def test_query_returns_compiler_data(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    calls = []

    def fake_execute_dsl(dsl_definition, dataset_id):
        calls.append((dsl_definition, dataset_id))
        return [[{"name": "Alice"}, {"name": "Bob"}]]

    monkeypatch.setattr(ds_mod.jsonBcompiler, "execute_dsl", fake_execute_dsl)
    _seed_owned_populated(ds_ns, make_dataset)

    resp = ds_ns.client.post("/api/v1/datasets/dst_1/query", json=DSL)

    assert resp.status_code == 200
    assert resp.json() == {"data": [{"name": "Alice"}, {"name": "Bob"}]}
    # Raw JSON body was validated into the pydantic plan schema.
    executed_plan, executed_ds = calls[0]
    assert isinstance(executed_plan, DslDefinitionSchema)
    assert executed_ds == "dst_1"


def test_query_unpopulated_dataset_rejected(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    def must_not_run(*a):
        raise AssertionError("compiler should not run")

    monkeypatch.setattr(ds_mod.jsonBcompiler, "execute_dsl", must_not_run)
    _seed_owned_populated(ds_ns, make_dataset, populated=False)

    resp = ds_ns.client.post("/api/v1/datasets/dst_1/query", json=DSL)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Dataset is not populated yet"


def test_query_invalid_dsl_maps_to_400(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    def invalid_plan(*a):
        raise ValueError("Filter field 'ghost' not found in dataset schema")

    monkeypatch.setattr(ds_mod.jsonBcompiler, "execute_dsl", invalid_plan)
    _seed_owned_populated(ds_ns, make_dataset)

    resp = ds_ns.client.post("/api/v1/datasets/dst_1/query", json=DSL)

    assert resp.status_code == 400
    assert "ghost" in resp.json()["detail"]


def test_query_unexpected_error_maps_to_500(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    def broken(*a):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(ds_mod.jsonBcompiler, "execute_dsl", broken)
    _seed_owned_populated(ds_ns, make_dataset)

    resp = ds_ns.client.post("/api/v1/datasets/dst_1/query", json=DSL)

    assert resp.status_code == 500
    # Internal details are not leaked to the client.
    assert resp.json()["detail"] == "Internal server error"


def test_query_empty_result_set(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    monkeypatch.setattr(
        ds_mod.jsonBcompiler, "execute_dsl", lambda *a: [[]]
    )
    _seed_owned_populated(ds_ns, make_dataset)

    resp = ds_ns.client.post("/api/v1/datasets/dst_1/query", json=DSL)

    assert resp.status_code == 200
    assert resp.json() == {"data": []}


def test_query_foreign_dataset_404(ds_ns, make_dataset, monkeypatch):
    from app.routers import datasets as ds_mod

    monkeypatch.setattr(
        ds_mod.jsonBcompiler, "execute_dsl", lambda *a: [[{"x": 1}]]
    )
    ds_ns.db.get_results["dst_8"] = make_dataset(user_id=8, id="dst_8")

    resp = ds_ns.client.post("/api/v1/datasets/dst_8/query", json=DSL)
    assert resp.status_code == 404

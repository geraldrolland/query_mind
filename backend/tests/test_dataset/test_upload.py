"""POST /api/v1/datasets/upload."""

from app.models.datasetrow import DatasetRow

CSV_OK = b"name,age\nAlice,30\nBob,25\nAlice,30\n"  # one exact duplicate row


def _upload(client, csv_bytes=None, filename="data.csv", name="Sales", **extra):
    files = {"file": (filename, CSV_OK if csv_bytes is None else csv_bytes, "text/csv")}
    data = {"name": name, **extra}
    return client.post("/api/v1/datasets/upload", files=files, data=data)


def test_upload_success_returns_dataset_and_cleaning_report(ds_ns, cache_recorder):
    resp = _upload(ds_ns.client, name="Sales")

    assert resp.status_code == 201
    body = resp.json()

    dataset = body["dataset"]
    assert dataset["name"] == "Sales"
    assert dataset["populated"] is True
    assert dataset["total_rows"] == 2  # duplicate dropped
    assert "user_id" not in dataset  # ownership never leaked in response
    assert "created_at" in dataset and "updated_at" in dataset

    report = body["cleaning_report"]
    assert report["raw_rows"] == 3
    assert report["rows_ingested"] == 2
    assert report["duplicates_removed"] == 1
    assert report["columns"]["name"] == "string"
    assert report["columns"]["age"] == "number"


def test_upload_persists_dataset_and_row_batches(ds_ns, cache_recorder):
    resp = _upload(ds_ns.client, name="Sales")
    assert resp.status_code == 201

    datasets = [o for o in ds_ns.db.added if isinstance(o, type(ds_ns.db.added[0]))]
    created = next(
        o for o in ds_ns.db.added if getattr(o, "dataset_schema", None)
    )
    assert created.user_id == 7  # ownership stamped from auth state
    assert created.total_rows == 2
    assert set(created.dataset_schema.keys()) >= {"name", "age"}

    all_rows = [r for batch in ds_ns.db.add_all_calls for r in batch]
    assert len(all_rows) == 2
    assert all(isinstance(r, DatasetRow) for r in all_rows)
    assert {r.data["name"] for r in all_rows} == {"Alice", "Bob"}
    assert all(r.dataset_id == created.id for r in all_rows)

    assert ds_ns.db.commits >= 1
    # User response cache invalidated after the upload.
    assert cache_recorder == [7]


def test_upload_rejects_non_csv_extension(ds_ns):
    resp = _upload(ds_ns.client, filename="data.txt")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only .csv files are supported"
    assert ds_ns.db.added == []


def test_upload_rejects_oversized_file(ds_ns, monkeypatch):
    monkeypatch.setattr(ds_ns.settings, "MAX_UPLOAD_BYTES", 10)
    big_csv = b"name,age\n" + b"A,1\n" * 50
    resp = _upload(ds_ns.client, csv_bytes=big_csv)
    assert resp.status_code == 413
    assert "50MB" in resp.json()["detail"]


def test_upload_invalid_csv_content_rejected(ds_ns):
    resp = _upload(ds_ns.client, csv_bytes=b"\x00\xff not,a,csv\x01")
    assert resp.status_code == 400
    assert "Failed to parse CSV" in resp.json()["detail"]


def test_upload_empty_csv_rejected(ds_ns):
    resp = _upload(ds_ns.client, csv_bytes=b"a,b\n")
    assert resp.status_code == 400
    assert "no rows" in resp.json()["detail"]


def test_upload_duplicate_name_for_same_user_rejected(ds_ns, make_dataset):
    ds_ns.db.queue_result(items=[make_dataset(user_id=7, name="Sales")])

    resp = _upload(ds_ns.client, name="Sales")

    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]

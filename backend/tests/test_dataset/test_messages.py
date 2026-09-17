"""GET /api/v1/datasets/{dataset_id}/messages."""


def test_messages_envelope_and_fields(ds_ns, make_dataset, make_message):
    ds_ns.db.get_results["dst_1"] = make_dataset(user_id=7, id="dst_1")
    msgs = [
        make_message(idx=0, content="question?", role="user"),
        make_message(
            idx=1,
            content={"rows": [1]},
            role="assistant",
            type="record",
            chart_type="barchart",
        ),
    ]
    ds_ns.db.queue_result(scalar=2)
    ds_ns.db.queue_result(items=msgs)

    resp = ds_ns.client.get("/api/v1/datasets/dst_1/messages?page=1&page_size=50")

    assert resp.status_code == 200
    body = resp.json()
    assert (body["total"], body["page"], body["page_size"]) == (2, 1, 50)

    first, second = body["messages"]
    assert set(first.keys()) == {
        "id", "dataset_id", "content", "role", "type",
        "chart_type", "is_error", "created_at",
    }
    assert first["content"] == "question?"
    assert second["type"] == "record"
    assert second["chart_type"] == "barchart"
    assert "T" in second["created_at"]


def test_messages_empty_page(ds_ns, make_dataset):
    ds_ns.db.get_results["dst_1"] = make_dataset(user_id=7, id="dst_1")
    ds_ns.db.queue_result(scalar=0)
    ds_ns.db.queue_result(items=[])

    resp = ds_ns.client.get("/api/v1/datasets/dst_1/messages")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"messages": [], "total": 0, "page": 1, "page_size": 50}


def test_messages_foreign_dataset_404(ds_ns, make_dataset):
    ds_ns.db.get_results["dst_8"] = make_dataset(user_id=8, id="dst_8")
    resp = ds_ns.client.get("/api/v1/datasets/dst_8/messages")
    assert resp.status_code == 404

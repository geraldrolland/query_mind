"""Endpoint tests for app/routers/datasets.py — one file per endpoint.

Integration-style: a minimal FastAPI app mounts only the datasets router,
the DB session dependency is overridden with a configurable fake, auth
state is injected via middleware, and cross-module collaborators (cache
invalidation, Profiler, DSL compiler) are patched at their source.

Layout:
    conftest.py                shared app/client/fakes
    test_upload.py             POST /api/v1/datasets/upload
    test_list.py               GET  /api/v1/datasets
    test_get_dataset.py        GET  /api/v1/datasets/{dataset_id}
    test_delete_dataset.py     DELETE /api/v1/datasets/{dataset_id}
    test_records.py            GET  /api/v1/datasets/{dataset_id}/records
    test_messages.py           GET  /api/v1/datasets/{dataset_id}/messages
    test_schema_endpoint.py    GET  /api/v1/datasets/{dataset_id}/schema
    test_profile.py            GET  /api/v1/datasets/{dataset_id}/profile
    test_query.py              POST /api/v1/datasets/{dataset_id}/query

Run:
    python -m pytest tests/test_dataset/ -v
"""

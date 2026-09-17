"""POST /api/v1/review/create-review."""

import pytest

from app.routers import review as review_mod


def test_create_review_returns_201(review_ns):
    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "Great product!"},
    )

    assert resp.status_code == 201


def test_create_review_returns_review_fields(review_ns):
    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "Love QueryMind!"},
    )

    body = resp.json()
    assert body["comment"] == "Love QueryMind!"
    assert body["user_id"] == 7
    assert "id" in body
    assert body["id"].startswith("rev_")
    assert "created_at" in body


def test_create_review_saves_to_database(review_ns):
    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "Amazing tool"},
    )

    assert review_ns.db.added
    saved = review_ns.db.added[0]
    assert saved.user_id == 7
    assert saved.comment == "Amazing tool"
    assert saved.id.startswith("rev_")
    assert review_ns.db.commits == 1


def test_create_review_sends_background_emails(review_ns, monkeypatch):
    sent = []
    monkeypatch.setattr(
        review_mod, "send_email", lambda *a, **kw: sent.append(a) or True
    )

    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "Feedback here"},
    )

    assert resp.status_code == 201
    # BackgroundTasks runs synchronously in TestClient
    assert len(sent) == 2
    # First email to REVIEW_EMAIL (internal), second to user (confirmation)
    assert sent[0][0] == review_ns.settings.REVIEW_EMAIL
    assert sent[1][0] == "reviewer@x.com"


def test_create_review_email_subject_is_querymind_review(review_ns, monkeypatch):
    subjects = []
    monkeypatch.setattr(
        review_mod, "send_email", lambda *a, **kw: subjects.append(a[1]) or True
    )

    review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "x"},
    )

    assert all(s == "QueryMind Review" for s in subjects)


def test_create_review_unauthenticated_returns_401():
    """Request without auth state should fail (middleware not injected)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.routers.review import router as review_router

    from tests.test_review.conftest import FakeDBSession

    app = FastAPI()
    app.include_router(review_router)

    # No auth middleware injected — request.state.auth_user won't exist

    def override_session():
        yield FakeDBSession()

    app.dependency_overrides[get_session] = override_session

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/review/create-review",
        json={"comment": "test"},
    )

    assert resp.status_code == 500


def test_create_review_empty_comment_returns_422(review_ns):
    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": ""},
    )

    assert resp.status_code == 422


def test_create_review_missing_comment_returns_422(review_ns):
    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={},
    )

    assert resp.status_code == 422


def test_create_review_comment_too_long_returns_422(review_ns):
    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "x" * 2001},
    )

    assert resp.status_code == 422


def test_create_review_max_length_comment_accepted(review_ns):
    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "x" * 2000},
    )

    assert resp.status_code == 201


def test_create_review_with_different_user(review_ns):
    review_ns.state["auth_user"] = {"id": 99, "email": "other@x.com"}

    resp = review_ns.client.post(
        "/api/v1/review/create-review",
        json={"comment": "From another user"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["user_id"] == 99
    assert review_ns.db.added[0].user_id == 99

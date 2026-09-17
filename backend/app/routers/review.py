"""Review router — allows authenticated users to submit feedback."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.core.settings import settings
from app.db import get_session
from app.email.sender import (
    render_review_confirmation,
    render_review_notification,
    send_email,
)
from app.models.review import Review
from app.schemas.review import ReviewSchema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/review", tags=["review"])


def _send_review_emails(comment: str, user_email: str) -> None:
    """Background task: send review notification and confirmation emails."""
    send_email(
        settings.REVIEW_EMAIL,
        "QueryMind Review",
        render_review_notification(comment, user_email),
    )
    send_email(
        user_email,
        "QueryMind Review",
        render_review_confirmation(comment),
    )


@router.post("/create-review", status_code=status.HTTP_201_CREATED)
async def create_review(
    request: Request,
    body: ReviewSchema,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Submit a review. The review is stored in the database and
    confirmation emails are sent asynchronously."""
    user = request.state.auth_user
    user_id = user["id"]
    user_email = user["email"]

    review = Review(user_id=user_id, comment=body.comment)
    session.add(review)
    session.commit()
    session.refresh(review)

    background_tasks.add_task(_send_review_emails, body.comment, user_email)

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "id": review.id,
            "user_id": review.user_id,
            "comment": review.comment,
            "created_at": review.created_at.isoformat(),
        },
    )

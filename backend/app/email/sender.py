"""Email sending for verification, password reset, and other transactional mail.

Uses the SendGrid HTTP API (port 443) when a ``SENDGRID_API_KEY`` is
configured; falls back to SMTP for local development.  All public
functions in this module are fire-and-forget safe: they log errors
rather than raising so that auth flows are not blocked by transient
e-mail failures.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"


def _send_via_sendgrid(to_email: str, subject: str, html_body: str, text_body: str | None) -> bool:
    """Dispatch an e-mail via the SendGrid v3 HTTP API.

    Uses ``httpx.post`` instead of the SendGrid SDK so the request
    works without outbound SMTP (required on Render free tier).

    Args:
        to_email: Recipient address.
        subject: E-mail subject line.
        html_body: HTML body of the message.
        text_body: Optional plain-text fallback body.

    Returns:
        ``True`` if SendGrid accepted the message (HTTP 202).
    """
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": settings.SMTP_FROM_EMAIL, "name": settings.SMTP_FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/plain", "value": text_body or "Please view this email in an HTML-capable client."}],
    }
    if html_body:
        payload["content"].append({"type": "text/html", "value": html_body})

    try:
        response = httpx.post(
            SENDGRID_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
            timeout=15,
        )
        if response.status_code == 202:
            logger.info("Email sent to %s via SendGrid: %s", to_email, subject)
            return True
        logger.error(
            "SendGrid failed (%s) for %s: %s",
            response.status_code,
            to_email,
            response.text[:300],
        )
        return False
    except Exception as exc:
        logger.error("SendGrid request failed for %s: %s", to_email, exc)
        return False


def _send_via_smtp(to_email: str, subject: str, html_body: str, text_body: str | None) -> bool:
    """Dispatch an e-mail via a local SMTP server.

    Supports both implicit TLS (``SMTP_USE_SSL``) and STARTTLS
    (``SMTP_USE_STARTTLS``) depending on the server configuration.

    Args:
        to_email: Recipient address.
        subject: E-mail subject line.
        html_body: HTML body of the message.
        text_body: Optional plain-text fallback body.

    Returns:
        ``True`` if the message was accepted by the SMTP server.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL))
    msg["To"] = to_email
    msg.set_content(text_body or "Please view this email in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            if settings.SMTP_USE_STARTTLS:
                context = ssl.create_default_context()
                server.starttls(context=context)
        with server:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Email sent to %s via SMTP: %s", to_email, subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an HTML e-mail, preferring the SendGrid HTTP API.

    When no provider is configured the call is logged and ``False`` is
    returned, allowing callers to fall back to dev-mode delivery
    (e.g. printing the link to the console).

    Args:
        to_email: Recipient address.
        subject: E-mail subject line.
        html_body: HTML body of the message.
        text_body: Optional plain-text fallback body.

    Returns:
        ``True`` if the message was accepted by the provider.
    """
    if settings.SENDGRID_API_KEY:
        return _send_via_sendgrid(to_email, subject, html_body, text_body)

    if not settings.SMTP_HOST or not settings.SMTP_USERNAME:
        logger.warning(
            "Email provider not configured - email to %s skipped. "
            "Set SENDGRID_API_KEY or SMTP_HOST/SMTP_USERNAME.",
            to_email,
        )
        return False

    return _send_via_smtp(to_email, subject, html_body, text_body)


def render_verification_email(link: str) -> str:
    """Render the HTML body for an e-mail verification message.

    Args:
        link: Full verification URL including token query parameter.

    Returns:
        HTML string ready to pass to ``send_email``.
    """
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px">
      <h2 style="color:#111827;margin:0 0 8px">Verify your email</h2>
      <p style="color:#4b5563;font-size:14px;line-height:1.6">Welcome to QueryMind! Confirm your email address to activate your account and start exploring your datasets.</p>
      <p style="margin:24px 0">
        <a href="{link}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600">Verify email address</a>
      </p>
      <p style="color:#6b7280;font-size:12px">This link expires in 30 minutes. If you didn't create a QueryMind account, you can ignore this email.</p>
    </div>
    """


def render_review_notification(comment: str, user_email: str) -> str:
    """Render the HTML body for an internal review notification email.

    Args:
        comment: The user's review comment.
        user_email: Email of the user who submitted the review.

    Returns:
        HTML string ready to pass to ``send_email``.
    """
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px">
      <h2 style="color:#111827;margin:0 0 8px">New QueryMind Review</h2>
      <p style="color:#4b5563;font-size:14px;line-height:1.6">A user has submitted a review through the QueryMind platform.</p>
      <div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">
        <p style="color:#374151;font-size:14px;line-height:1.6;margin:0"><strong>From:</strong> {user_email}</p>
        <p style="color:#374151;font-size:14px;line-height:1.6;margin:12px 0 0"><strong>Review:</strong></p>
        <p style="color:#4b5563;font-size:14px;line-height:1.6;margin:8px 0 0;white-space:pre-wrap">{comment}</p>
      </div>
    </div>
    """


def render_review_confirmation(comment: str) -> str:
    """Render the HTML body for a review confirmation email.

    Args:
        comment: The user's review comment.

    Returns:
        HTML string ready to pass to ``send_email``.
    """
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px">
      <h2 style="color:#111827;margin:0 0 8px">Thank you for your review!</h2>
      <p style="color:#4b5563;font-size:14px;line-height:1.6">We've received your feedback and appreciate you taking the time to help us improve QueryMind.</p>
      <div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">
        <p style="color:#374151;font-size:14px;line-height:1.6;margin:0"><strong>Your review:</strong></p>
        <p style="color:#4b5563;font-size:14px;line-height:1.6;margin:8px 0 0;white-space:pre-wrap">{comment}</p>
      </div>
      <p style="color:#6b7280;font-size:12px">Your review will be reviewed by our team and processed shortly.</p>
    </div>
    """


def render_reset_email(link: str) -> str:
    """Render the HTML body for a password-reset message.

    Args:
        link: Full reset URL including token query parameter.

    Returns:
        HTML string ready to pass to ``send_email``.
    """
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px">
      <h2 style="color:#111827;margin:0 0 8px">Reset your password</h2>
      <p style="color:#4b5563;font-size:14px;line-height:1.6">We received a request to reset the password for your QueryMind account. Click below to choose a new password.</p>
      <p style="margin:24px 0">
        <a href="{link}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600">Reset password</a>
      </p>
      <p style="color:#6b7280;font-size:12px">This link expires in 30 minutes. If you didn't request a password reset, you can safely ignore this email.</p>
    </div>
    """

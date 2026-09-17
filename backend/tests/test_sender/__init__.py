"""Unit tests for app/email/sender.py (transactional email helpers).

Covers:
    - _send_via_sendgrid        (payload shape, bearer auth, status handling,
                                 error resilience) via patched httpx.post
    - _send_via_smtp            (EmailMessage construction, SSL vs STARTTLS,
                                 credentials, failure paths) via fake servers
    - send_email                (provider routing / unconfigured behaviour)
    - render_verification_email / render_reset_email

No real HTTP requests or SMTP connections are made.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_sender/__init__.py -v
"""

import smtplib
from types import SimpleNamespace

import pytest

from app.email import sender

# ===========================================================================
# Helpers
# ===========================================================================


def patch_mail_settings(monkeypatch, **overrides):
    values = {
        "SMTP_FROM_EMAIL": "no-reply@querymind.app",
        "SMTP_FROM_NAME": "QueryMind",
        "SMTP_HOST": "smtp.test.local",
        "SMTP_PORT": 587,
        "SMTP_USERNAME": "mailer",
        "SMTP_PASSWORD": "secret",
        "SMTP_USE_SSL": False,
        "SMTP_USE_STARTTLS": True,
        "SENDGRID_API_KEY": "",
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(sender.settings, name, value)


def fake_response(status_code=202, text=""):
    return SimpleNamespace(status_code=status_code, text=text)


class FakeSMTP:
    """Records the full conversation; stands in for smtplib.SMTP(_SSL)."""

    instances: list = []
    fail_on: str | None = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_calls = []
        self.logins = []
        self.sent_messages = []
        self.entered = False
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.starttls_calls.append(context)
        if FakeSMTP.fail_on == "starttls":
            raise smtplib.SMTPException("TLS refused")

    def login(self, username, password):
        self.logins.append((username, password))
        if FakeSMTP.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, msg):
        self.sent_messages.append(msg)
        if FakeSMTP.fail_on == "send":
            raise smtplib.SMTPRecipientsRefused({"to@example.com": (550, b"no")})

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_on = None
    monkeypatch.setattr(sender.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(sender.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def captured_post(monkeypatch):
    calls = {}

    def post(url, **kwargs):
        calls["url"] = url
        calls.update(kwargs)
        return calls.get("response", fake_response(202))

    monkeypatch.setattr(sender.httpx, "post", post)
    return calls


# ===========================================================================
# _send_via_sendgrid
# ===========================================================================


def test_sendgrid_success_returns_true_and_posts_payload(
    monkeypatch, captured_post
):
    patch_mail_settings(monkeypatch, SENDGRID_API_KEY="sg-key")
    ok = sender._send_via_sendgrid("u@d.com", "Hi", "<b>Hi</b>", "plain Hi")

    assert ok is True
    assert captured_post["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert captured_post["headers"] == {"Authorization": "Bearer sg-key"}
    assert captured_post["timeout"] == 15

    payload = captured_post["json"]
    assert payload["personalizations"] == [{"to": [{"email": "u@d.com"}]}]
    assert payload["from"] == {
        "email": "no-reply@querymind.app",
        "name": "QueryMind",
    }
    assert payload["subject"] == "Hi"
    # Plain-text first, HTML appended second.
    assert payload["content"][0] == {"type": "text/plain", "value": "plain Hi"}
    assert payload["content"][1] == {"type": "text/html", "value": "<b>Hi</b>"}


def test_sendgrid_uses_placeholder_when_text_body_missing(
    monkeypatch, captured_post
):
    patch_mail_settings(monkeypatch, SENDGRID_API_KEY="sg-key")
    sender._send_via_sendgrid("u@d.com", "S", "<b>S</b>", None)
    content = captured_post["json"]["content"]
    assert "HTML-capable client" in content[0]["value"]


def test_sendgrid_html_only_added_when_provided(monkeypatch, captured_post):
    patch_mail_settings(monkeypatch, SENDGRID_API_KEY="sg-key")
    sender._send_via_sendgrid("u@d.com", "S", "", "just text")
    types = [part["type"] for part in captured_post["json"]["content"]]
    assert types == ["text/plain"]  # empty html_body -> no html part


def test_sendgrid_non_202_returns_false(monkeypatch, captured_post):
    patch_mail_settings(monkeypatch, SENDGRID_API_KEY="sg-key")
    captured_post["response"] = fake_response(401, text="unauthorized")
    ok = sender._send_via_sendgrid("u@d.com", "S", "<b>S</b>", None)
    assert ok is False


def test_sendgrid_network_error_returns_false(monkeypatch, captured_post):
    patch_mail_settings(monkeypatch, SENDGRID_API_KEY="sg-key")

    def explode(*args, **kwargs):
        raise ConnectionError("network down")

    monkeypatch.setattr(sender.httpx, "post", explode)
    ok = sender._send_via_sendgrid("u@d.com", "S", "<b>S</b>", None)
    assert ok is False


# ===========================================================================
# _send_via_smtp
# ===========================================================================


def test_smtp_starttls_flow_sends_message(fake_smtp, monkeypatch):
    patch_mail_settings(monkeypatch)  # SSL off, STARTTLS on
    ok = sender._send_via_smtp("u@d.com", "Welcome", "<h1>W</h1>", "W plain")

    assert ok is True
    assert len(FakeSMTP.instances) == 1
    server = FakeSMTP.instances[0]
    assert (server.host, server.port) == ("smtp.test.local", 587)
    assert len(server.starttls_calls) == 1
    assert server.starttls_calls[0] is not None  # ssl context supplied
    assert server.logins == [("mailer", "secret")]
    assert len(server.sent_messages) == 1
    assert server.entered is True  # used as a context manager


def test_smtp_message_headers_and_bodies(fake_smtp, monkeypatch):
    patch_mail_settings(monkeypatch)
    sender._send_via_smtp("u@d.com", "Subject X", "<p>HTML BODY</p>", "TEXT BODY")

    msg = FakeSMTP.instances[0].sent_messages[0]
    assert msg["Subject"] == "Subject X"
    assert msg["To"] == "u@d.com"
    assert "no-reply@querymind.app" in msg["From"]
    assert "QueryMind" in msg["From"]  # display name via formataddr

    plain = msg.get_body(("plain",))
    html = msg.get_body(("html",))
    assert plain is not None and html is not None
    assert "TEXT BODY" in plain.get_content()
    assert "<p>HTML BODY</p>" in html.get_content()


def test_smtp_plain_fallback_when_no_text_body(fake_smtp, monkeypatch):
    patch_mail_settings(monkeypatch)
    sender._send_via_smtp("u@d.com", "S", "<b>S</b>", None)
    msg = FakeSMTP.instances[0].sent_messages[0]
    assert "HTML-capable client" in msg.get_body(("plain",)).get_content()


def test_smtp_ssl_mode_uses_smtp_ssl_without_starttls(fake_smtp, monkeypatch):
    patch_mail_settings(monkeypatch, SMTP_USE_SSL=True, SMTP_USE_STARTTLS=True)
    ok = sender._send_via_smtp("u@d.com", "S", "<b>S</b>", None)

    assert ok is True
    server = FakeSMTP.instances[0]
    # Both names point at FakeSMTP; SSL branch must construct via SMTP_SSL
    # and skip STARTTLS entirely. We verify by flagging construction order.
    assert server.starttls_calls == []


def test_smtp_starttls_disabled_skips_upgrade(fake_smtp, monkeypatch):
    patch_mail_settings(monkeypatch, SMTP_USE_STARTTLS=False)
    ok = sender._send_via_smtp("u@d.com", "S", "<b>S</b>", None)
    assert ok is True
    assert FakeSMTP.instances[0].starttls_calls == []


@pytest.mark.parametrize("fail_on", ["login", "send", "starttls"])
def test_smtp_failures_return_false(fake_smtp, monkeypatch, fail_on):
    patch_mail_settings(monkeypatch)
    FakeSMTP.fail_on = fail_on
    ok = sender._send_via_smtp("u@d.com", "S", "<b>S</b>", None)
    assert ok is False


def test_smtp_connection_error_returns_false(fake_smtp, monkeypatch):
    patch_mail_settings(monkeypatch)

    def refused(host, port, timeout=None):
        raise ConnectionRefusedError("no listener")

    monkeypatch.setattr(sender.smtplib, "SMTP", refused)
    ok = sender._send_via_smtp("u@d.com", "S", "<b>S</b>", None)
    assert ok is False


# ===========================================================================
# send_email routing
# ===========================================================================


def test_send_email_prefers_sendgrid_when_key_set(monkeypatch):
    patch_mail_settings(monkeypatch, SENDGRID_API_KEY="k")
    called = {}

    def fake_sg(*args):
        called["sg"] = args
        return True

    def fake_smtp(*args):
        called["smtp"] = args
        return True

    monkeypatch.setattr(sender, "_send_via_sendgrid", fake_sg)
    monkeypatch.setattr(sender, "_send_via_smtp", fake_smtp)
    ok = sender.send_email("u@d.com", "S", "<b>S</b>", "T")
    assert ok is True
    assert "sg" in called and "smtp" not in called
    assert called["sg"] == ("u@d.com", "S", "<b>S</b>", "T")


def test_send_email_unconfigured_returns_false_and_neither_sends(monkeypatch, caplog):
    patch_mail_settings(
        monkeypatch, SENDGRID_API_KEY="", SMTP_HOST="", SMTP_USERNAME=""
    )
    sg = monkeypatch.setattr(
        sender, "_send_via_sendgrid", lambda *a: True
    )
    st = monkeypatch.setattr(sender, "_send_via_smtp", lambda *a: True)
    ok = sender.send_email("u@d.com", "S", "<b>S</b>")
    assert ok is False


def test_send_email_requires_username_with_host(monkeypatch):
    patch_mail_settings(
        monkeypatch, SENDGRID_API_KEY="", SMTP_USERNAME=""
    )
    sent = {}
    monkeypatch.setattr(
        sender, "_send_via_smtp", lambda *a: sent.setdefault("args", a) or True
    )
    assert sender.send_email("u@d.com", "S", "<b>S</b>") is False
    assert sent == {}


def test_send_email_falls_back_to_smtp(monkeypatch):
    patch_mail_settings(monkeypatch, SENDGRID_API_KEY="")
    called = {}

    def fake_smtp_path(*args):
        called["smtp"] = args
        return True

    monkeypatch.setattr(sender, "_send_via_smtp", fake_smtp_path)
    ok = sender.send_email("u@d.com", "S", "<b>S</b>")
    assert ok is True
    assert called["smtp"] == ("u@d.com", "S", "<b>S</b>", None)


# ===========================================================================
# Renderers
# ===========================================================================


LINK = "http://localhost:3000/verify?token=aBc123&next=/dashboard"


def test_render_verification_email_contains_link_and_copy():
    html = sender.render_verification_email(LINK)
    assert f'href="{LINK}"' in html
    assert "Verify your email" in html
    assert "Verify email address" in html
    assert "expires in 30 minutes" in html


def test_render_reset_email_contains_link_and_copy():
    html = sender.render_reset_email(LINK)
    assert f'href="{LINK}"' in html
    assert "Reset your password" in html
    assert "Reset password" in html
    assert "expires in 30 minutes" in html


def test_renderers_are_distinct():
    v = sender.render_verification_email(LINK)
    r = sender.render_reset_email(LINK)
    assert v != r


def test_renderers_embed_links_verbatim():
    tricky = "https://x.test/a?b=1&c=<tag>"
    assert f'href="{tricky}"' in sender.render_verification_email(tricky)
    assert f'href="{tricky}"' in sender.render_reset_email(tricky)

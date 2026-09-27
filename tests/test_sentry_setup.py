"""Tests for app/sentry_setup.py and app/pii.py.

Sentry must be a no-op without SENTRY_DSN, tag events with correlation IDs,
and never ship customer PII (phones, addresses, names, message content) or
credentials.
"""

import contextvars
import json
from unittest.mock import patch

import pytest

from app import sentry_setup
from app.context import (
    current_bot_id,
    current_channel,
    current_contact_id,
    trace_id_var,
)
from app.pii import FILTERED, mask_phone, scrub_data, scrub_text
from app.sentry_setup import init_sentry, make_before_send, scrub_event, tag_event

PHONE = "5511999990000"


@pytest.fixture(autouse=True)
def _reset_sentry_state(monkeypatch):
    monkeypatch.setattr(sentry_setup, "_initialized", False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    yield


# ---------------------------------------------------------------------------
# init_sentry
# ---------------------------------------------------------------------------


class TestInitSentry:
    def test_noop_without_dsn(self):
        with patch("sentry_sdk.init") as mock_init:
            assert init_sentry("backend") is False
        mock_init.assert_not_called()

    def test_blank_dsn_is_off(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "   ")
        with patch("sentry_sdk.init") as mock_init:
            assert init_sentry("worker") is False
        mock_init.assert_not_called()

    def test_backend_config_is_privacy_safe(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
        monkeypatch.setenv("ENVIRONMENT", "dev")
        with patch("sentry_sdk.init") as mock_init, patch("sentry_sdk.set_tag"):
            assert init_sentry("backend") is True

        kwargs = mock_init.call_args.kwargs
        assert kwargs["environment"] == "dev"
        assert kwargs["send_default_pii"] is False
        assert kwargs["include_local_variables"] is False
        assert kwargs["max_request_body_size"] == "never"
        assert kwargs["traces_sample_rate"] == 0.0
        names = {type(i).__name__ for i in kwargs["integrations"]}
        assert {
            "FastApiIntegration",
            "StarletteIntegration",
            "LoggingIntegration",
        } <= names
        assert "ArqIntegration" not in names

    def test_worker_uses_arq_integration(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
        with patch("sentry_sdk.init") as mock_init, patch("sentry_sdk.set_tag"):
            init_sentry("worker")
        names = {type(i).__name__ for i in mock_init.call_args.kwargs["integrations"]}
        assert "ArqIntegration" in names
        assert "FastApiIntegration" not in names

    def test_idempotent(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
        with patch("sentry_sdk.init") as mock_init, patch("sentry_sdk.set_tag"):
            init_sentry("worker")
            init_sentry("worker")
        assert mock_init.call_count == 1

    def test_bad_sample_rate_falls_back_to_zero(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
        monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "lots")
        with patch("sentry_sdk.init") as mock_init, patch("sentry_sdk.set_tag"):
            init_sentry("backend")
        assert mock_init.call_args.kwargs["traces_sample_rate"] == 0.0


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------


class TestTagEvent:
    def test_tags_from_context_vars(self):
        def run():
            trace_id_var.set("abc123def456")
            current_bot_id.set(42)
            current_contact_id.set(7)
            current_channel.set("whatsapp")
            return tag_event({"tags": {"existing": "x"}}, "worker")

        event = contextvars.copy_context().run(run)
        assert event["tags"] == {
            "existing": "x",
            "component": "worker",
            "trace_id": "abc123def456",
            "bot_id": "42",
            "contact_id": "7",
            "channel": "whatsapp",
        }

    def test_missing_context_is_omitted(self):
        def run():
            # Other tests leave values in the shared context; reset to the
            # ContextVar defaults (what an un-traced request looks like).
            trace_id_var.set("")
            current_bot_id.set(None)
            current_contact_id.set(None)
            current_channel.set(None)
            return tag_event({}, "backend")

        event = contextvars.copy_context().run(run)
        assert event["tags"] == {"component": "backend"}


# ---------------------------------------------------------------------------
# Text / data scrubbing (app/pii.py)
# ---------------------------------------------------------------------------


class TestScrubText:
    @pytest.mark.parametrize(
        "raw",
        [
            "5511999990000",
            "+55 (11) 99999-0000",
            "11 99999-0000",
            "(11) 3333-4444",
            "11999990000",
        ],
    )
    def test_masks_phone_formats(self, raw):
        out = scrub_text(f"failed to send to {raw} now")
        assert raw not in out
        assert "***" in out

    def test_masks_cep(self):
        assert "04674-225" not in scrub_text("CEP 04674-225 not found")
        assert "04674225" not in scrub_text("CEP 04674225 not found")

    def test_masks_email_and_bearer(self):
        out = scrub_text(
            "user joao@example.com sent Authorization: Bearer eyJhbGciOi.xyz"
        )
        assert "joao@example.com" not in out
        assert "eyJhbGciOi" not in out

    @pytest.mark.parametrize(
        "raw, secret",
        [
            ("GET /v19.0/me?access_token=EAAGabc123&fields=id", "EAAGabc123"),
            ('{"refresh_token": "TG-9876xyz", "ok": true}', "TG-9876xyz"),
            ("webhook_token=abcDEF_123 rejected", "abcDEF_123"),
            ("client_secret: s3cr3t!", "s3cr3t!"),
            ("X-Signature=ts=1,v1=deadbeef", "deadbeef"),
        ],
    )
    def test_masks_inline_credentials(self, raw, secret):
        out = scrub_text(raw)
        assert secret not in out
        assert FILTERED in out

    def test_leaves_ordinary_ids_alone(self):
        text = "bot_id=12 order 345 took 1.5s status=500"
        assert scrub_text(text) == text

    def test_mask_phone_keeps_prefix_and_suffix(self):
        assert mask_phone(PHONE) == "5511***00"
        assert mask_phone("123") == "***"


class TestScrubData:
    def test_drops_secrets_content_addresses_names(self):
        data = {
            "access_token": "APP_USR-123",
            "whatsapp_token": "EAAG...",
            "Authorization": "Bearer x",
            "webhook_token": "tok",
            "text": "quero 2 pizzas",
            "body": "oi",
            "cep": "04674225",
            "street": "Rua X",
            "complement": "apto 12",
            "customer_name": "Maria",
            "bot_id": 3,
        }
        out = scrub_data(data)
        for key in data:
            if key != "bot_id":
                assert out[key] == FILTERED, key
        assert out["bot_id"] == 3

    def test_masks_phone_keys_and_nested_values(self):
        data = {
            "entry": [{"changes": [{"value": {"messages": [{"from": PHONE}]}}]}],
            "contact_number": PHONE,
            "to": PHONE,
            "note": f"call {PHONE}",
        }
        out = scrub_data(data)
        assert PHONE not in json.dumps(out)
        assert out["contact_number"] == "5511***00"


# ---------------------------------------------------------------------------
# Whole-event scrubbing
# ---------------------------------------------------------------------------


def _dirty_event():
    return {
        "user": {"ip_address": "1.2.3.4", "id": PHONE},
        "logentry": {
            "message": "Critical error processing message: %s",
            "params": [f"send failed for {PHONE}"],
            "formatted": f"Critical error processing message: send failed for {PHONE}",
        },
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": f"WhatsApp API rejected {PHONE} at CEP 04674-225",
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "process_whatsapp_message",
                                "vars": {
                                    "current_token": "EAAG-secret",
                                    "text_body": "oi",
                                },
                            }
                        ]
                    },
                }
            ]
        },
        "request": {
            "url": "https://dev-api.zenbotz.com.br/webhook",
            "method": "POST",
            "data": {
                "entry": [{"messages": [{"from": PHONE, "text": {"body": "oi"}}]}]
            },
            "query_string": "token=supersecret&order=5",
            "headers": {
                "Authorization": "Bearer abc",
                "Cookie": "session=1",
                "X-Hub-Signature-256": "sha256=...",
                "Content-Type": "application/json",
            },
            "cookies": {"session": "1"},
        },
        "extra": {"payload": {"delivery_address": "Rua Y, 10", "phone": PHONE}},
        "breadcrumbs": {
            "values": [
                {"message": f"Human takeover for phone {PHONE}", "data": {"text": "oi"}}
            ]
        },
    }


class TestScrubEvent:
    def test_nothing_sensitive_survives(self):
        out = scrub_event(_dirty_event())
        dumped = json.dumps(out)
        for needle in [
            PHONE,
            "04674-225",
            "EAAG-secret",
            "supersecret",
            "Bearer abc",
            "session=1",
            "Rua Y",
            '"oi"',
            "1.2.3.4",
        ]:
            assert needle not in dumped, needle

    def test_keeps_debugging_signal(self):
        out = scrub_event(_dirty_event())
        exc = out["exception"]["values"][0]
        assert exc["type"] == "RuntimeError"
        assert exc["stacktrace"]["frames"][0]["function"] == "process_whatsapp_message"
        assert "vars" not in exc["stacktrace"]["frames"][0]
        assert out["request"]["headers"] == {"Content-Type": "application/json"}
        assert out["request"]["query_string"] == f"token={FILTERED}&order=5"
        assert out["request"]["data"] == FILTERED
        assert out["logentry"]["message"] == "Critical error processing message: %s"

    def test_does_not_mutate_input(self):
        event = _dirty_event()
        scrub_event(event)
        assert event["user"]["id"] == PHONE

    def test_before_send_drops_event_if_scrubbing_fails(self):
        before_send = make_before_send("backend")
        with patch("app.sentry_setup.scrub_event", side_effect=RuntimeError("boom")):
            assert before_send({"message": "x"}, {}) is None

    def test_before_send_tags_and_scrubs(self):
        before_send = make_before_send("worker")

        def run():
            trace_id_var.set("feedfacecafe")
            return before_send({"message": f"failed for {PHONE}"}, {})

        out = contextvars.copy_context().run(run)
        assert out["tags"]["trace_id"] == "feedfacecafe"
        assert out["tags"]["component"] == "worker"
        assert PHONE not in out["message"]

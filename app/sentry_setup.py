"""Sentry error reporting for the FastAPI backend and the ARQ worker.

Off unless SENTRY_DSN is set. Every event is tagged with trace_id, bot_id,
contact_id, channel, component and environment, and scrubbed of customer
PII (phone numbers, addresses, names, message content) and credentials
before it leaves the process. See docs/error_to_issue.md.

Most message-processing failures never reach ARQ as exceptions: the
handlers in app/whatsapp.py catch them and `logger.error(..., exc_info=True)`.
The logging integration turns those ERROR records into Sentry events, so
they are captured the same way as unhandled exceptions.
"""

import logging
import os
from typing import Any

from app.context import (
    current_bot_id,
    current_channel,
    current_contact_id,
    trace_id_var,
)
from app.pii import FILTERED, scrub_data, scrub_text

logger = logging.getLogger(__name__)

# Request headers worth keeping for debugging; everything else is dropped.
_SAFE_HEADERS = {"content-type", "content-length", "user-agent", "x-request-id"}

_initialized = False


def _scrub_request(request: dict) -> dict:
    request = dict(request)
    # Bodies carry customer messages, addresses and payment payloads.
    if "data" in request:
        request["data"] = FILTERED
    request.pop("cookies", None)
    request.pop("env", None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {
            k: v for k, v in headers.items() if k.lower() in _SAFE_HEADERS
        }
    qs = request.get("query_string")
    if isinstance(qs, str) and qs:
        # e.g. /payments/webhooks/payment-confirm/{id}?token=...
        parts = []
        for pair in qs.split("&"):
            key, _, val = pair.partition("=")
            handled = scrub_data({key: val})[key]
            parts.append(f"{key}={handled}")
        request["query_string"] = "&".join(parts)
    elif isinstance(qs, dict):
        request["query_string"] = scrub_data(qs)
    if isinstance(request.get("url"), str):
        request["url"] = scrub_text(request["url"])
    return request


def _scrub_exception(exception: dict) -> dict:
    exception = dict(exception)
    values = []
    for exc in exception.get("values") or []:
        exc = dict(exc)
        if isinstance(exc.get("value"), str):
            exc["value"] = scrub_text(exc["value"])
        stacktrace = exc.get("stacktrace")
        if isinstance(stacktrace, dict):
            frames = []
            for frame in stacktrace.get("frames") or []:
                frame = dict(frame)
                # Belt and braces: include_local_variables is off, but a
                # frame's vars would hold tokens and message text.
                frame.pop("vars", None)
                frames.append(frame)
            exc["stacktrace"] = {**stacktrace, "frames": frames}
        values.append(exc)
    exception["values"] = values
    return exception


def _scrub_logentry(logentry: dict) -> dict:
    logentry = dict(logentry)
    for key in ("message", "formatted"):
        if isinstance(logentry.get(key), str):
            logentry[key] = scrub_text(logentry[key])
    if "params" in logentry:
        logentry["params"] = scrub_data(logentry["params"])
    return logentry


def scrub_event(event: dict) -> dict:
    """Strip PII and credentials from a Sentry event (mutates a copy)."""
    event = dict(event)
    event.pop("user", None)
    if isinstance(event.get("message"), str):
        event["message"] = scrub_text(event["message"])
    if isinstance(event.get("logentry"), dict):
        event["logentry"] = _scrub_logentry(event["logentry"])
    if isinstance(event.get("exception"), dict):
        event["exception"] = _scrub_exception(event["exception"])
    if isinstance(event.get("request"), dict):
        event["request"] = _scrub_request(event["request"])
    for key in ("extra", "contexts"):
        if isinstance(event.get(key), dict):
            event[key] = scrub_data(event[key])
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
        crumbs = []
        for crumb in breadcrumbs["values"]:
            crumb = dict(crumb)
            if isinstance(crumb.get("message"), str):
                crumb["message"] = scrub_text(crumb["message"])
            if isinstance(crumb.get("data"), dict):
                crumb["data"] = scrub_data(crumb["data"])
            crumbs.append(crumb)
        event["breadcrumbs"] = {**breadcrumbs, "values": crumbs}
    return event


def tag_event(event: dict, component: str) -> dict:
    """Attach correlation tags from the request-scoped ContextVars."""
    tags = dict(event.get("tags") or {})
    tags["component"] = component
    trace_id = trace_id_var.get()
    if trace_id:
        tags["trace_id"] = trace_id
    bot_id = current_bot_id.get()
    if bot_id is not None:
        tags["bot_id"] = str(bot_id)
    contact_id = current_contact_id.get()
    if contact_id is not None:
        tags["contact_id"] = str(contact_id)
    channel = current_channel.get()
    if channel:
        tags["channel"] = channel
    event["tags"] = tags
    return event


def make_before_send(component: str):
    def before_send(event: dict, hint: dict) -> Any:
        try:
            return scrub_event(tag_event(event, component))
        except Exception:
            # Never ship an event we failed to scrub.
            logger.warning("Sentry before_send failed; dropping event")
            return None

    return before_send


def init_sentry(component: str) -> bool:
    """Initialise Sentry if SENTRY_DSN is set. Returns True when enabled.

    component: "backend" or "worker" (becomes the `component` tag).
    """
    global _initialized
    if _initialized:
        return True
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    integrations = [
        # INFO+ records become breadcrumbs; ERROR+ records become events.
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
    ]
    if component == "worker":
        from sentry_sdk.integrations.arq import ArqIntegration

        integrations.append(ArqIntegration())
    else:
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        integrations += [StarletteIntegration(), FastApiIntegration()]

    before_send = make_before_send(component)
    try:
        traces_sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0") or 0)
    except ValueError:
        traces_sample_rate = 0.0

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("ENVIRONMENT", "development"),
            release=os.getenv("SENTRY_RELEASE") or None,
            integrations=integrations,
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            traces_sample_rate=traces_sample_rate,
            before_send=before_send,
            before_send_transaction=before_send,
        )
    except Exception as exc:
        # A malformed DSN (stray quotes, truncated paste) must not stop the
        # API or the worker from booting; error reporting is optional.
        logger.warning(
            "Sentry disabled: init failed (%s). Check SENTRY_DSN.",
            type(exc).__name__,
        )
        return False
    sentry_sdk.set_tag("component", component)
    _initialized = True
    logger.info("Sentry enabled (component=%s)", component)
    return True

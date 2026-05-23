"""OAuth state signing + callback handler validation."""

from __future__ import annotations

import time

import pytest

from handlers.onboarding import (
    OAuthError,
    _sign_state,
    _verify_state,
    build_github_oauth_url,
    build_linkedin_oauth_url,
    handle_oauth_callback,
)


def test_build_linkedin_oauth_url_includes_state() -> None:
    url = build_linkedin_oauth_url("11111111-1111-1111-1111-111111111111", "sess-1")
    assert "state=" in url
    assert "client_id=" in url
    assert "redirect_uri=" in url


def test_build_github_oauth_url_includes_state() -> None:
    url = build_github_oauth_url("11111111-1111-1111-1111-111111111111", "sess-1")
    assert "state=" in url
    assert "client_id=" in url


def test_sign_and_verify_state_roundtrip() -> None:
    state = _sign_state(
        event_id="event-1",
        session_id="sess-x",
        platform="linkedin",
        nonce="nonce",
        ts=int(time.time()),
    )
    event_id, nonce = _verify_state(state, platform="linkedin", expected_session_id="sess-x")
    assert event_id == "event-1"
    assert nonce == "nonce"


def test_verify_rejects_mismatched_session_id() -> None:
    state = _sign_state(
        event_id="event-1",
        session_id="sess-x",
        platform="linkedin",
        nonce="nonce",
        ts=int(time.time()),
    )
    with pytest.raises(OAuthError):
        _verify_state(state, platform="linkedin", expected_session_id="sess-other")


def test_verify_rejects_platform_swap() -> None:
    state = _sign_state(
        event_id="event-1",
        session_id="sess-x",
        platform="linkedin",
        nonce="n",
        ts=int(time.time()),
    )
    with pytest.raises(OAuthError):
        _verify_state(state, platform="github", expected_session_id="sess-x")


def test_verify_rejects_tampered_signature() -> None:
    state = _sign_state(
        event_id="event-1",
        session_id="sess-x",
        platform="linkedin",
        nonce="n",
        ts=int(time.time()),
    )
    tampered = state[:-2] + ("00" if state[-2:] != "00" else "ff")
    with pytest.raises(OAuthError):
        _verify_state(tampered, platform="linkedin", expected_session_id="sess-x")


def test_verify_rejects_expired_state() -> None:
    state = _sign_state(
        event_id="event-1",
        session_id="sess-x",
        platform="linkedin",
        nonce="n",
        ts=int(time.time()) - 10_000,
    )
    with pytest.raises(OAuthError):
        _verify_state(state, platform="linkedin", expected_session_id="sess-x")


@pytest.mark.asyncio
async def test_callback_handler_rejects_mismatched_session_id() -> None:
    state = _sign_state(
        event_id="event-1",
        session_id="sess-original",
        platform="linkedin",
        nonce="n",
        ts=int(time.time()),
    )
    with pytest.raises(OAuthError):
        await handle_oauth_callback(
            "linkedin",
            code="any-code",
            state=state,
            session_id="sess-attacker",
        )


@pytest.mark.asyncio
async def test_callback_handler_rejects_malformed_state() -> None:
    with pytest.raises(OAuthError):
        await handle_oauth_callback(
            "linkedin",
            code="any-code",
            state="not-a-real-state",
            session_id="sess-x",
        )

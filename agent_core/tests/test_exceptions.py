from __future__ import annotations

import pytest
from src.exceptions import map_exception_to_friendly_message


def test_map_rate_limit_error():
    assert "high traffic" in map_exception_to_friendly_message("RateLimitError: 429 rate limit exceeded")
    assert "high traffic" in map_exception_to_friendly_message("quota exhausted")


def test_map_authentication_error():
    assert "Invalid provider credentials" in map_exception_to_friendly_message("AuthenticationError: Invalid API key")
    assert "Invalid provider credentials" in map_exception_to_friendly_message("bad auth")


def test_map_timeout_error():
    assert "timed out" in map_exception_to_friendly_message("ConnectTimeout: connection timed out")
    assert "timed out" in map_exception_to_friendly_message("TimeoutError")


def test_map_not_found_error():
    assert "model not found" in map_exception_to_friendly_message("NotFoundError: 404 model not found")


def test_map_safety_error():
    assert "safety policy constraints" in map_exception_to_friendly_message("SAFETY block")
    assert "safety policy constraints" in map_exception_to_friendly_message("recitation check failed")


def test_map_generic_error():
    msg = map_exception_to_friendly_message("Internal Server Error")
    assert "I'm sorry, I encountered an error" in msg
    assert "Internal Server Error" in msg

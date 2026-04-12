"""Tests for Soma profile validation."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from profiles import validate


VALID_PROFILE = {
    "agent_id": "test-agent",
    "display_name": "Test Agent",
    "description": "A test agent",
    "categories": ["research", "coding"],
    "base_price_sats": {"research": 50, "coding": 100},
    "contact": "test@example.com",
    "active": True,
}


def test_valid_profile():
    ok, msg = validate(VALID_PROFILE)
    assert ok, msg


def test_missing_field():
    p = {k: v for k, v in VALID_PROFILE.items() if k != "agent_id"}
    ok, msg = validate(p)
    assert not ok
    assert "missing" in msg


def test_empty_categories():
    p = {**VALID_PROFILE, "categories": []}
    ok, msg = validate(p)
    assert not ok
    assert "non-empty" in msg


def test_invalid_category():
    p = {**VALID_PROFILE, "categories": ["hacking"]}
    ok, msg = validate(p)
    assert not ok
    assert "not in whitelist" in msg


def test_missing_pricing_for_category():
    p = {**VALID_PROFILE, "base_price_sats": {"research": 50}}
    ok, msg = validate(p)
    assert not ok
    assert "missing pricing" in msg


def test_negative_price():
    p = {**VALID_PROFILE, "base_price_sats": {"research": -1, "coding": 100}}
    ok, msg = validate(p)
    assert not ok
    assert "invalid price" in msg


def test_pricing_not_dict():
    p = {**VALID_PROFILE, "base_price_sats": 50}
    ok, msg = validate(p)
    assert not ok
    assert "must be dict" in msg

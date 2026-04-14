"""Tests for Soma policy filter — local logic only (no LLM calls)."""
import sys, os, json, tempfile, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import policy
from policy import check_policy, WHITELIST, BLACKLIST, POLICY_VERSION


def _mock_check(decision, category, reason="test"):
    original = policy._check_groq
    policy._check_groq = lambda text: {"decision": decision, "category": category, "reason": reason}
    return original


def _restore(original):
    policy._check_groq = original


def test_empty_request_rejected():
    result = check_policy("")
    assert result["decision"] == "reject"
    assert result["category"] == "invalid_input"
    assert result["provider"] == "local"
    assert result["policy_version"] == POLICY_VERSION
    assert "user_message" in result


def test_whitespace_only_rejected():
    result = check_policy("   ")
    assert result["decision"] == "reject"
    assert result["category"] == "invalid_input"


def test_accept_whitelist_category():
    orig = _mock_check("accept", "research")
    try:
        result = check_policy("summarize recent papers on LLMs")
        assert result["decision"] == "accept"
        assert result["category"] == "research"
    finally:
        _restore(orig)


def test_accept_unknown_category_escalates():
    orig = _mock_check("accept", "unknown_stuff")
    try:
        result = check_policy("do something weird")
        assert result["decision"] == "escalate"
        assert result["reason"] == "category not in whitelist"
    finally:
        _restore(orig)


def test_reject_blacklist_category():
    orig = _mock_check("accept", "impersonation")
    try:
        result = check_policy("write as Elon Musk")
        assert result["decision"] == "reject"
    finally:
        _restore(orig)


def test_invalid_decision_escalates():
    orig = _mock_check("maybe", "research")
    try:
        result = check_policy("some request")
        assert result["decision"] == "escalate"
    finally:
        _restore(orig)


def test_blacklist_overrides_accept():
    for cat in BLACKLIST:
        orig = _mock_check("accept", cat)
        try:
            result = check_policy(f"test {cat}")
            assert result["decision"] == "reject", f"{cat} should be rejected"
        finally:
            _restore(orig)


def test_all_whitelist_categories_accepted():
    for cat in WHITELIST:
        orig = _mock_check("accept", cat)
        try:
            result = check_policy(f"test {cat}")
            assert result["decision"] == "accept", f"{cat} should be accepted"
        finally:
            _restore(orig)


def test_self_notification_is_accepted():
    orig = _mock_check("accept", "self_notification")
    try:
        result = check_policy("notify me every morning if the train is running")
        assert result["decision"] == "accept"
        assert result["category"] == "self_notification"
    finally:
        _restore(orig)


def test_invalid_input_is_rejected_with_message():
    orig = _mock_check("reject", "invalid_input", reason="gibberish")
    try:
        result = check_policy("asdf qwerty zxcv")
        assert result["decision"] == "reject"
        assert result["category"] == "invalid_input"
        assert "user_message" in result
    finally:
        _restore(orig)


def test_directed_outreach_has_user_message():
    orig = _mock_check("reject", "directed_outreach")
    try:
        result = check_policy("mass DM these 500 users on Twitter")
        assert result["decision"] == "reject"
        assert "user_message" in result
        assert "self" in result["user_message"].lower() or "rephrase" in result["user_message"].lower()
    finally:
        _restore(orig)


def test_log_writes_contact_hash_and_preview(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(policy, "LOG_FILE", log)
    orig = _mock_check("accept", "research")
    try:
        contact = "@petchevere"
        text = "tell me the weather today in BA please" * 3
        check_policy(text, contact=contact)
    finally:
        _restore(orig)
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    expected_hash = hashlib.sha256(contact.encode()).hexdigest()[:16]
    assert entry["contact_hash"] == expected_hash
    assert entry["text_preview"] == text[:40]
    assert entry["reason"] == "test"
    assert entry["policy_version"] == POLICY_VERSION


def test_log_contact_hash_null_when_no_contact(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(policy, "LOG_FILE", log)
    orig = _mock_check("accept", "research")
    try:
        check_policy("anything", contact=None)
    finally:
        _restore(orig)
    entry = json.loads(log.read_text().strip().splitlines()[0])
    assert entry["contact_hash"] is None

"""Tests for Soma policy filter — local logic only (no LLM calls)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from policy import check_policy, WHITELIST, BLACKLIST, POLICY_VERSION


def _mock_check(decision, category):
    """Patch _check_groq to return a fixed result without calling LLM."""
    import policy
    original = policy._check_groq
    policy._check_groq = lambda text: {"decision": decision, "category": category, "reason": "test"}
    return original


def _restore(original):
    import policy
    policy._check_groq = original


def test_empty_request_rejected():
    result = check_policy("")
    assert result["decision"] == "reject"
    assert result["provider"] == "local"
    assert result["policy_version"] == POLICY_VERSION


def test_whitespace_only_rejected():
    result = check_policy("   ")
    assert result["decision"] == "reject"


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

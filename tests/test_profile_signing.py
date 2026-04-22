"""Tests for Soma profile Ed25519 signature verification (opt-in, A4 Fase A)."""
import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nacl.signing import SigningKey

import profiles
from agent_signing import NonceCache


def _keypair():
    sk = SigningKey.generate()
    return sk, base64.b64encode(bytes(sk.verify_key)).decode()


def _signed_profile(sk, base, pub_key_b64=None):
    profile = dict(base)
    if pub_key_b64 is not None:
        profile["pub_key"] = pub_key_b64
    payload = profiles.canonical_payload(profile)
    signature = base64.b64encode(sk.sign(payload).signature).decode()
    return profile, signature


def _base_profile(agent_id="signed-agent"):
    return {
        "agent_id": agent_id,
        "display_name": "Signed",
        "description": "An agent that signs its profile",
        "categories": ["research"],
        "base_price_sats": {"research": 50},
        "contact": "test@example.com",
        "active": True,
    }


def test_canonical_payload_deterministic():
    p = _base_profile("deterministic")
    payload_a = profiles.canonical_payload(p)
    payload_b = profiles.canonical_payload({**p, "zzz_extra_field": "ignored"})
    assert payload_a == payload_b


def test_canonical_payload_excludes_process_metadata():
    p = _base_profile("stable")
    with_noise = {**p, "created": 1234, "verified": True, "signature": "x",
                  "signature_ts": 1, "bootstrap": True}
    assert profiles.canonical_payload(p) == profiles.canonical_payload(with_noise)


def test_verify_accepts_valid_signature():
    sk, pk = _keypair()
    p = _base_profile("good-sign")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    cache = NonceCache()
    ok = profiles.verify_profile_signature(
        profile, sig, nonce="n1", signature_ts=int(time.time()),
        pub_key_b64=pk, nonce_cache=cache,
    )
    assert ok is True


def test_verify_rejects_tampered_profile():
    sk, pk = _keypair()
    p = _base_profile("tampered")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    profile["base_price_sats"] = {"research": 1}  # attacker lowers price
    cache = NonceCache()
    ok = profiles.verify_profile_signature(
        profile, sig, nonce="n2", signature_ts=int(time.time()),
        pub_key_b64=pk, nonce_cache=cache,
    )
    assert ok is False


def test_verify_rejects_expired_timestamp():
    sk, pk = _keypair()
    p = _base_profile("expired")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    cache = NonceCache()
    stale_ts = int(time.time()) - 3600
    ok = profiles.verify_profile_signature(
        profile, sig, nonce="n3", signature_ts=stale_ts,
        pub_key_b64=pk, nonce_cache=cache,
    )
    assert ok is False


def test_verify_rejects_replay_nonce():
    sk, pk = _keypair()
    p = _base_profile("replay")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    cache = NonceCache()
    ts = int(time.time())
    first = profiles.verify_profile_signature(
        profile, sig, nonce="n4", signature_ts=ts, pub_key_b64=pk, nonce_cache=cache,
    )
    second = profiles.verify_profile_signature(
        profile, sig, nonce="n4", signature_ts=ts, pub_key_b64=pk, nonce_cache=cache,
    )
    assert first is True
    assert second is False


def test_verify_rejects_wrong_pubkey():
    sk, pk = _keypair()
    _, wrong_pk = _keypair()
    p = _base_profile("wrong-pk")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    cache = NonceCache()
    ok = profiles.verify_profile_signature(
        profile, sig, nonce="n5", signature_ts=int(time.time()),
        pub_key_b64=wrong_pk, nonce_cache=cache,
    )
    assert ok is False


def test_save_unsigned_marks_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path)
    p = _base_profile("unsigned")
    profiles.save(p, pub_key_resolver=lambda _a: None)
    stored = profiles.load("unsigned")
    assert stored is not None
    assert stored["verified"] is False
    assert stored["pub_key"] is None


def test_save_signed_marks_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path)
    sk, pk = _keypair()
    p = _base_profile("signed-ok")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    profiles.save(
        profile,
        signature=sig,
        signature_ts=int(time.time()),
        nonce="save-n1",
        pub_key_resolver=lambda _a: pk,
        nonce_cache=NonceCache(),
    )
    stored = profiles.load("signed-ok")
    assert stored["verified"] is True
    assert stored["pub_key"] == pk
    assert stored["signature"] == sig


def test_save_rejects_unsigned_update_of_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path)
    sk, pk = _keypair()
    p = _base_profile("lockdown")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    profiles.save(
        profile,
        signature=sig, signature_ts=int(time.time()), nonce="lock-n1",
        pub_key_resolver=lambda _a: pk, nonce_cache=NonceCache(),
    )
    # Attacker tries to overwrite without a signature
    hijack = {**_base_profile("lockdown"), "contact": "attacker@evil"}
    try:
        profiles.save(hijack, pub_key_resolver=lambda _a: pk)
    except PermissionError:
        pass
    else:
        raise AssertionError("unsigned overwrite of verified profile should fail")


def test_save_rejects_signed_update_with_invalid_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path)
    sk, pk = _keypair()
    p = _base_profile("tamper-after")
    profile, sig = _signed_profile(sk, p, pub_key_b64=pk)
    profiles.save(
        profile,
        signature=sig, signature_ts=int(time.time()), nonce="ta-n1",
        pub_key_resolver=lambda _a: pk, nonce_cache=NonceCache(),
    )
    # Attacker signs different content with wrong key but claims the signature
    other_sk, _ = _keypair()
    hijack = {**_base_profile("tamper-after"), "pub_key": pk, "contact": "attacker@evil"}
    bad_sig = base64.b64encode(other_sk.sign(profiles.canonical_payload(hijack)).signature).decode()
    try:
        profiles.save(
            hijack,
            signature=bad_sig, signature_ts=int(time.time()), nonce="ta-n2",
            pub_key_resolver=lambda _a: pk, nonce_cache=NonceCache(),
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("signed update with invalid signature should fail")


def test_verify_status_for_unknown_profile():
    status = profiles.verify_status("never-existed")
    assert status["exists"] is False
    assert status["verified"] is False
    assert status["has_pubkey"] is False

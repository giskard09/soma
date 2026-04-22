"""
Soma profile management
YAML-per-agent under profiles/. Validates against whitelist.
"""
import base64
import json, time
from pathlib import Path
from typing import Optional

from policy import WHITELIST

try:
    import yaml
except ImportError:
    yaml = None

try:
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey
    _NACL_OK = True
except ImportError:
    _NACL_OK = False

PROFILES_DIR = Path(__file__).parent / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)

REQUIRED_FIELDS = {"agent_id", "display_name", "description",
                   "categories", "base_price_sats", "contact", "active"}

# Stable fields that a client signs. Excludes metadata of process
# (signature, signature_ts, verified, created, bootstrap).
SIGNABLE_FIELDS = {
    "agent_id", "display_name", "description", "categories",
    "base_price_sats", "min_karma_required", "karma_minimum_to_hire",
    "delivery_time_hours", "proof_type", "contact", "active",
    "policy_version", "pub_key", "updated",
}

SIGNATURE_TTL_SECONDS = 60


def _load_yaml(path: Path) -> dict:
    if yaml:
        return yaml.safe_load(path.read_text())
    return json.loads(path.read_text())


def _dump_yaml(data: dict, path: Path):
    if yaml:
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    else:
        path.write_text(json.dumps(data, indent=2))


def validate(profile: dict) -> tuple[bool, str]:
    missing = REQUIRED_FIELDS - set(profile.keys())
    if missing:
        return False, f"missing fields: {sorted(missing)}"

    categories = profile.get("categories") or []
    if not isinstance(categories, list) or not categories:
        return False, "categories must be non-empty list"
    bad = [c for c in categories if c not in WHITELIST]
    if bad:
        return False, f"categories not in whitelist: {bad}"

    pricing = profile.get("base_price_sats") or {}
    if not isinstance(pricing, dict):
        return False, "base_price_sats must be dict"
    for cat in categories:
        if cat not in pricing:
            return False, f"missing pricing for category: {cat}"
        if not isinstance(pricing[cat], int) or pricing[cat] < 0:
            return False, f"invalid price for {cat}"

    return True, "ok"


def canonical_payload(profile: dict) -> bytes:
    """Canonical bytes of the signable subset of a profile.

    Client and server compute this over the same subset to agree
    on what the signature covers. Keys sorted, no whitespace.
    """
    subset = {k: profile[k] for k in SIGNABLE_FIELDS if k in profile}
    return json.dumps(subset, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_profile_signature(
    profile: dict,
    signature_b64: str,
    nonce: str,
    signature_ts: int,
    pub_key_b64: str,
    now: Optional[int] = None,
    nonce_cache=None,
) -> bool:
    """Verify Ed25519 signature over canonical_payload(profile) + freshness + nonce.

    Returns True iff signature is valid, timestamp is within TTL, and nonce is fresh.
    nonce_cache is injectable for tests; production uses agent_signing._nonce_cache.
    """
    if not _NACL_OK:
        return False
    if not (signature_b64 and nonce and pub_key_b64):
        return False
    try:
        ts = int(signature_ts)
    except (TypeError, ValueError):
        return False

    current = int(now if now is not None else time.time())
    if abs(current - ts) > SIGNATURE_TTL_SECONDS:
        return False

    try:
        vk = VerifyKey(base64.b64decode(pub_key_b64))
        vk.verify(canonical_payload(profile), base64.b64decode(signature_b64))
    except (BadSignatureError, ValueError, TypeError):
        return False

    if nonce_cache is None:
        try:
            from agent_signing import _nonce_cache as default_cache
            nonce_cache = default_cache
        except ImportError:
            return False

    return nonce_cache.check_and_store(f"soma-profile:{profile.get('agent_id','')}:{nonce}")


def save(
    profile: dict,
    signature: Optional[str] = None,
    signature_ts: Optional[int] = None,
    nonce: Optional[str] = None,
    pub_key_resolver=None,
    now: Optional[int] = None,
    nonce_cache=None,
) -> Path:
    """Save a profile. Opt-in signature verification.

    If profile has a registered pub_key (resolved via pub_key_resolver or
    taken from profile itself), signature is required to overwrite an
    existing verified profile. Unverified profiles (pub_key=null in Marks)
    accept unsigned writes but are marked verified=false.
    """
    ok, msg = validate(profile)
    if not ok:
        raise ValueError(msg)

    agent_id = profile["agent_id"]
    existing = load(agent_id)
    existing_verified = bool(existing and existing.get("verified"))

    resolved_pub_key = None
    if pub_key_resolver is not None:
        try:
            resolved_pub_key = pub_key_resolver(agent_id)
        except Exception:
            resolved_pub_key = None

    signing_attempted = bool(signature and signature_ts and nonce)

    if existing_verified and not signing_attempted:
        raise PermissionError(
            f"profile '{agent_id}' is verified and requires a signature to update"
        )

    is_verified = False
    if signing_attempted:
        if not resolved_pub_key:
            raise PermissionError(
                f"no pub_key registered for '{agent_id}'; cannot accept signed profile"
            )
        profile_pk = profile.get("pub_key")
        if profile_pk and profile_pk != resolved_pub_key:
            raise PermissionError(
                f"profile pub_key does not match registered pub_key for '{agent_id}'"
            )
        profile["pub_key"] = resolved_pub_key
        is_verified = verify_profile_signature(
            profile,
            signature_b64=signature,
            nonce=nonce,
            signature_ts=signature_ts,
            pub_key_b64=resolved_pub_key,
            now=now,
            nonce_cache=nonce_cache,
        )
        if not is_verified:
            raise PermissionError(f"invalid signature for profile '{agent_id}'")

    profile.setdefault("created", int(time.time()))
    profile["updated"] = int(time.time())
    profile.setdefault("policy_version", "1.0")
    profile.setdefault("pub_key", resolved_pub_key)
    profile["verified"] = is_verified
    if signing_attempted:
        profile["signature"] = signature
        profile["signature_ts"] = signature_ts
    else:
        profile.setdefault("signature", None)
        profile.setdefault("signature_ts", None)

    path = PROFILES_DIR / f"{agent_id}.yaml"
    _dump_yaml(profile, path)
    return path


def verify_status(agent_id: str) -> dict:
    """Lightweight summary: does this profile have a pub_key, is it verified."""
    p = load(agent_id)
    if not p:
        return {"agent_id": agent_id, "exists": False, "has_pubkey": False,
                "verified": False, "last_signed_at": None}
    return {
        "agent_id": agent_id,
        "exists": True,
        "has_pubkey": bool(p.get("pub_key")),
        "verified": bool(p.get("verified")),
        "last_signed_at": p.get("signature_ts"),
    }


def load(agent_id: str) -> dict | None:
    path = PROFILES_DIR / f"{agent_id}.yaml"
    if not path.exists():
        return None
    return _load_yaml(path)


def list_all(active_only: bool = True) -> list[dict]:
    out = []
    for path in PROFILES_DIR.glob("*.yaml"):
        try:
            p = _load_yaml(path)
            if active_only and not p.get("active", True):
                continue
            out.append(p)
        except Exception:
            continue
    return out


def search(category: str | None = None,
           min_karma: int = 0,
           max_price_sats: int | None = None) -> list[dict]:
    results = []
    for p in list_all(active_only=True):
        if category and category not in p.get("categories", []):
            continue
        if p.get("karma_minimum_to_hire", 0) < min_karma:
            continue
        if max_price_sats is not None:
            prices = [p["base_price_sats"][c] for c in p["categories"]
                      if c in p["base_price_sats"]]
            if not prices or min(prices) > max_price_sats:
                continue
        results.append(p)
    results.sort(key=lambda x: (-x.get("karma_minimum_to_hire", 0),
                                x.get("delivery_time_hours", 9999)))
    return results

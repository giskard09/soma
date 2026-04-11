"""
Soma profile management
YAML-per-agent under profiles/. Validates against whitelist.
"""
import json, time
from pathlib import Path
from policy import WHITELIST

try:
    import yaml
except ImportError:
    yaml = None

PROFILES_DIR = Path(__file__).parent / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)

REQUIRED_FIELDS = {"agent_id", "display_name", "description",
                   "categories", "base_price_sats", "contact", "active"}


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


def save(profile: dict) -> Path:
    ok, msg = validate(profile)
    if not ok:
        raise ValueError(msg)
    profile.setdefault("created", int(time.time()))
    profile["updated"] = int(time.time())
    profile.setdefault("policy_version", "1.0")
    path = PROFILES_DIR / f"{profile['agent_id']}.yaml"
    _dump_yaml(profile, path)
    return path


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

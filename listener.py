"""
Soma payment listener — polls phoenixd for quoted requests and marks
them paid. Non-blocking, runs as a background thread.

Design:
- polling over webhook: no phoenixd config change, no coupling to
  ARGENTUM's webhook, self-contained, recovers across restarts
- idempotent via status transition (quoted -> paid) + dedup set
- audit log append-only in payment_log.jsonl
- notifies creator on each confirmed payment
"""
import os, json, time, threading, hashlib
from pathlib import Path

try:
    import requests as http
except ImportError:
    http = None

PHOENIXD_URL  = "http://127.0.0.1:9740"
PHOENIXD_PASS = os.getenv("PHOENIXD_PASSWORD", "")

POLL_INTERVAL = int(os.getenv("SOMA_POLL_INTERVAL", "10"))
REQUESTS_FILE = Path(__file__).parent / "requests.json"
AUDIT_LOG     = Path(__file__).parent / "payment_log.jsonl"

_seen_hashes: set[str] = set()
_lock = threading.Lock()


def _load_requests() -> dict:
    if REQUESTS_FILE.exists():
        return json.loads(REQUESTS_FILE.read_text())
    return {}


def _save_requests(data: dict):
    REQUESTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _audit(event: dict):
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _hash_short(h: str) -> str:
    return hashlib.sha256(h.encode()).hexdigest()[:16]


def _check_phoenixd(payment_hash: str) -> dict | None:
    try:
        r = http.get(
            f"{PHOENIXD_URL}/payments/incoming/{payment_hash}",
            auth=("", PHOENIXD_PASS),
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _notify_creator(msg: str):
    bot_token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    if not bot_token or not chat_id:
        return
    try:
        http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def _process_request(req_id: str, req: dict) -> bool:
    """Check one request against phoenixd. Returns True if state changed."""
    ph = req.get("payment_hash")
    if not ph or req.get("status") in ("paid", "expired"):
        return False

    data = _check_phoenixd(ph)
    if not data:
        return False

    if not data.get("isPaid"):
        if data.get("isExpired"):
            req["status"] = "expired"
            req["expired_at"] = int(time.time())
            _audit({
                "ts": int(time.time()),
                "event": "payment_expired",
                "req_id": req_id,
                "payment_hash_short": _hash_short(ph),
                "expected_sats": req.get("sats", 0),
                "category": req.get("category"),
            })
            _notify_creator(
                f"*SOMA — ⏰ EXPIRED* `{req_id}`\n\n"
                f"Invoice expiro sin pago.\n"
                f"Expected: {req.get('sats', 0)} sats\n"
                f"Category: {req.get('category', '-')}"
            )
            return True
        return False

    received_sat = data.get("receivedSat", 0)
    expected_sat = req.get("sats", 0)

    req["status"] = "paid"
    req["paid_at"] = data.get("completedAt", int(time.time() * 1000)) // 1000
    req["received_sats"] = received_sat

    _audit({
        "ts": int(time.time()),
        "event": "payment_confirmed",
        "req_id": req_id,
        "payment_hash_short": _hash_short(ph),
        "expected_sats": expected_sat,
        "received_sats": received_sat,
        "category": req.get("category"),
        "provider": "phoenixd",
    })

    underpaid = received_sat < expected_sat
    marker = "⚠️ UNDERPAID" if underpaid else "✅ PAID"
    msg = (
        f"*SOMA — {marker}* `{req_id}`\n\n"
        f"Received: {received_sat} sats"
        f"{f' (expected {expected_sat})' if underpaid else ''}\n"
        f"Category: {req.get('category', '-')}\n"
        f"Request: {req.get('text', '')[:200]}\n\n"
        f"Action: ejecutar y entregar a `{req.get('contact', 'anon')}`"
    )
    _notify_creator(msg)
    return True


def poll_once() -> int:
    """Single pass over quoted requests. Returns number of transitions."""
    if http is None:
        return 0
    with _lock:
        reqs = _load_requests()
        changed = 0
        for req_id, req in list(reqs.items()):
            if req.get("status") != "quoted":
                continue
            if _process_request(req_id, req):
                changed += 1
        if changed:
            _save_requests(reqs)
        return changed


def run_loop():
    """Background poll loop. Runs forever. Swallows errors."""
    while True:
        try:
            poll_once()
        except Exception as e:
            _audit({
                "ts": int(time.time()),
                "event": "poll_error",
                "error": str(e)[:200],
            })
        time.sleep(POLL_INTERVAL)


def start():
    t = threading.Thread(target=run_loop, daemon=True, name="soma-listener")
    t.start()
    _audit({
        "ts": int(time.time()),
        "event": "listener_started",
        "poll_interval": POLL_INTERVAL,
    })
    return t

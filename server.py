"""
Soma concierge backend
Puerto: 8022 (REST), 8023 (MCP)
"""
import os, json, uuid, time, hashlib, requests, sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import policy
import profiles
import listener

BOT_TOKEN       = os.getenv("BOT_TOKEN")
CHAT_ID         = os.getenv("CHAT_ID")
PHOENIXD_URL    = "http://127.0.0.1:9740"
PHOENIXD_PASS   = os.getenv("PHOENIXD_PASSWORD", "")
REQUESTS_FILE   = Path(__file__).parent / "requests.json"
STATE_FILE      = Path("/home/dell7568/moltbook_agent/state.json")
ARGENTUM_URL    = "http://127.0.0.1:8017"

# ── rate limiting (sqlite, survives restarts) ────────────────────────
RATE_DB = Path(__file__).parent / "rate_limit.db"

def _init_rate_db():
    conn = sqlite3.connect(str(RATE_DB))
    conn.execute("CREATE TABLE IF NOT EXISTS rate_log (key TEXT, ts REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_key ON rate_log (key)")
    conn.commit()
    conn.close()

_init_rate_db()

def _rate_key(contact: str, remote_addr: str) -> str:
    return contact.strip() or remote_addr

def _get_karma(contact: str) -> int:
    if not contact:
        return 0
    try:
        r = requests.get(f"{ARGENTUM_URL}/entity/{contact}", timeout=2)
        if r.status_code == 200:
            return r.json().get("karma", 0)
    except Exception:
        pass
    return 0

def _rate_limit_ok(key: str, karma: int) -> tuple[bool, int]:
    now = time.time()
    day_ago = now - 86400
    conn = sqlite3.connect(str(RATE_DB))
    conn.execute("DELETE FROM rate_log WHERE ts < ?", (day_ago,))
    count = conn.execute("SELECT COUNT(*) FROM rate_log WHERE key = ?", (key,)).fetchone()[0]
    if karma >= 50:
        limit = 9999
    elif karma >= 10:
        limit = 10
    else:
        limit = 3
    if count >= limit:
        conn.close()
        return False, limit
    conn.execute("INSERT INTO rate_log (key, ts) VALUES (?, ?)", (key, now))
    conn.commit()
    conn.close()
    return True, limit


def load_requests():
    if REQUESTS_FILE.exists():
        return json.loads(REQUESTS_FILE.read_text())
    return {}

def save_requests(data):
    REQUESTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def notify_telegram(text: str, chat_id=None):
    target = chat_id or CHAT_ID
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": target, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass

def get_chat_id(handle: str):
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            return state.get("user_chat_ids", {}).get(handle)
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, body):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, default=str).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/soma/agents":
            category = qs.get("category", [None])[0]
            min_karma = int(qs.get("min_karma", ["0"])[0])
            max_price = qs.get("max_price_sats", [None])[0]
            max_price = int(max_price) if max_price else None
            results = profiles.search(category, min_karma, max_price)
            for p in results:
                p["karma_live"] = _get_karma(p["agent_id"])
            self._json(200, {"agents": results, "count": len(results)})
            return

        if path.startswith("/soma/agents/"):
            agent_id = path.split("/soma/agents/", 1)[1]
            p = profiles.load(agent_id)
            if not p:
                self._json(404, {"error": "profile not found"})
                return
            p["karma_live"] = _get_karma(agent_id)
            self._json(200, p)
            return

        if path == "/status":
            self._json(200, {
                "service": "soma-concierge",
                "version": "0.3.0",
                "port": 8022,
                "uptime_seconds": int(time.time() - _start_time),
                "healthy": True,
                "policy_version": policy.POLICY_VERSION,
                "profiles": len(profiles.list_all()),
                "listener": {
                    "poll_interval": listener.POLL_INTERVAL,
                    "audit_log": str(listener.AUDIT_LOG.name),
                },
                "dependencies": ["phoenixd", "argentum-core", "groq"],
            })
            return

        if path.startswith("/payment/status/"):
            req_id = path.split("/payment/status/", 1)[1]
            reqs = load_requests()
            if req_id not in reqs:
                self._json(404, {"error": "request not found"})
                return
            req = reqs[req_id]
            ph = req.get("payment_hash")
            live = listener._check_phoenixd(ph) if ph else None
            self._json(200, {
                "req_id": req_id,
                "status": req.get("status"),
                "sats": req.get("sats"),
                "received_sats": req.get("received_sats"),
                "phoenixd_live": bool(live and live.get("isPaid")),
            })
            return

        if path == "/payment/poll":
            changed = listener.poll_once()
            self._json(200, {"ok": True, "transitions": changed})
            return

        if path == "/dashboard" or path == "/dashboard.html":
            dash = Path(__file__).parent / "dashboard.html"
            if dash.exists():
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(dash.read_bytes())
            else:
                self._json(404, {"error": "dashboard not found"})
            return

        if path == "/soma/stats":
            reqs = load_requests()
            counts = {}
            total_paid_sats = 0
            for r in reqs.values():
                s = r.get("status", "unknown")
                counts[s] = counts.get(s, 0) + 1
                if s == "paid":
                    total_paid_sats += r.get("received_sats", 0)
            self._json(200, {
                "requests_by_status": counts,
                "total_requests": len(reqs),
                "total_paid_sats": total_paid_sats,
                "profiles": len(profiles.list_all()),
                "uptime_seconds": int(time.time() - _start_time),
                "policy_version": policy.POLICY_VERSION,
            })
            return

        if path == "/soma/audit":
            limit = int(qs.get("limit", ["20"])[0])
            log_path = listener.AUDIT_LOG
            events = []
            if log_path.exists():
                lines = log_path.read_text().strip().split("\n")
                for line in lines[-limit:]:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
            self._json(200, {"events": events, "count": len(events)})
            return

        self._json(404, {"error": "not found"})

    # ── POST ───────────────────────────────────────────────────────────
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        # ── POST /soma/check_policy (internal) ─────────────────────────
        if self.path == "/soma/check_policy":
            text = data.get("request", "").strip()
            result = policy.check_policy(text)
            self._json(200, result)
            return

        # ── POST /soma/profile ─────────────────────────────────────────
        if self.path == "/soma/profile":
            try:
                path = profiles.save(data)
                h = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                self._json(200, {"ok": True, "agent_id": data["agent_id"],
                                 "profile_hash": h})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            return

        # ── POST /soma/match ───────────────────────────────────────────
        if self.path == "/soma/match":
            text = data.get("request", "").strip()
            max_price = data.get("max_price_sats")
            pol = policy.check_policy(text)
            if pol["decision"] == "reject":
                self._json(403, {"policy": pol, "matches": []})
                return
            category = pol.get("category")
            matches = profiles.search(category=category,
                                      max_price_sats=max_price) if category else []
            for p in matches:
                p["karma_live"] = _get_karma(p["agent_id"])
            self._json(200, {"policy": pol, "category": category,
                             "matches": matches[:3]})
            return

        # ── POST /request — pedido del form ────────────────────────────
        if self.path == "/request":
            text    = data.get("request", "").strip()
            contact = data.get("contact", "").strip()

            if not text:
                self._json(400, {"error": "empty request"})
                return

            # rate limit
            key = _rate_key(contact, self.client_address[0])
            karma = _get_karma(contact)
            ok, limit = _rate_limit_ok(key, karma)
            if not ok:
                self._json(429, {"error": "rate limit exceeded",
                                 "daily_limit": limit, "karma": karma})
                return

            # policy
            pol = policy.check_policy(text)
            if pol["decision"] == "reject":
                self._json(403, {"error": "rejected by policy",
                                 "reason": pol.get("reason"),
                                 "policy_version": pol.get("policy_version")})
                return

            req_id = str(uuid.uuid4())[:8]
            reqs   = load_requests()
            status = "review" if pol["decision"] == "escalate" else "pending"
            reqs[req_id] = {
                "text":    text,
                "contact": contact,
                "status":  status,
                "category": pol.get("category"),
                "policy_decision": pol["decision"],
                "created": int(time.time()),
            }
            save_requests(reqs)

            marker = "⚠️ ESCALATED" if status == "review" else "new request"
            msg = f"*SOMA — {marker}* `{req_id}`\n\nCategory: {pol.get('category')}\n\n{text}"
            if contact:
                msg += f"\n\nContacto: `{contact}` (karma {karma})"
                cid = get_chat_id(contact)
                msg += f"\n{'✅ chat_id conocido' if cid else '⚠️ chat_id desconocido'}"
            msg += f"\n\nPara cotizar: `/invoice {req_id} <sats>`"
            notify_telegram(msg)
            self._json(200, {"ok": True, "id": req_id,
                             "status": status, "category": pol.get("category")})
            return

        # ── POST /invoice ──────────────────────────────────────────────
        if self.path == "/invoice":
            req_id = data.get("id", "").strip()
            sats   = int(data.get("sats", 0))
            desc   = data.get("description", f"Soma request {req_id}")

            if not req_id or sats <= 0:
                self._json(400, {"error": "id and sats required"})
                return

            try:
                r = requests.post(
                    f"{PHOENIXD_URL}/createinvoice",
                    auth=("", PHOENIXD_PASS),
                    data={"amountSat": sats, "description": desc},
                    timeout=10,
                )
                inv = r.json()
            except Exception as e:
                self._json(500, {"error": str(e)})
                return

            reqs = load_requests()
            if req_id in reqs:
                reqs[req_id]["status"]       = "quoted"
                reqs[req_id]["sats"]         = sats
                reqs[req_id]["payment_hash"] = inv.get("paymentHash")
                save_requests(reqs)

            self._json(200, {
                "ok":      True,
                "invoice": inv.get("serialized"),
                "sats":    sats,
                "req_id":  req_id,
            })
            return

        self._json(404, {"error": "not found"})


# ── MCP LAYER ──────────────────────────────────────────────────────────────

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Soma", host="0.0.0.0", port=8023)


@mcp.tool()
def submit_request(request_text: str, contact: str = "") -> str:
    """Submit a service request to Soma — the agent marketplace.
    Describe what you need in natural language. A human concierge will review and quote.

    request_text: what you need done (natural language)
    contact: your Telegram handle or email (optional, for delivery)"""
    if not request_text.strip():
        return "Request cannot be empty."
    pol = policy.check_policy(request_text)
    if pol["decision"] == "reject":
        return f"Request rejected by policy: {pol.get('reason')}"

    req_id = str(uuid.uuid4())[:8]
    reqs = load_requests()
    status = "review" if pol["decision"] == "escalate" else "pending"
    reqs[req_id] = {
        "text": request_text.strip(),
        "contact": contact.strip(),
        "status": status,
        "category": pol.get("category"),
        "policy_decision": pol["decision"],
        "created": int(time.time()),
    }
    save_requests(reqs)

    marker = "⚠️ ESCALATED" if status == "review" else "new request"
    msg = f"*SOMA — {marker}* `{req_id}`\n\nCategory: {pol.get('category')}\n\n{request_text.strip()}"
    if contact:
        msg += f"\n\nContacto: `{contact}`"
    notify_telegram(msg)

    return f"Request {req_id} submitted. Status: {status}. Category: {pol.get('category')}."


@mcp.tool()
def check_status(request_id: str) -> str:
    """Check the status of a Soma request.

    request_id: the ID returned by submit_request"""
    reqs = load_requests()
    if request_id not in reqs:
        return f"Request '{request_id}' not found."
    r = reqs[request_id]
    lines = [f"Request {request_id}: {r['status']}",
             f"  Category: {r.get('category')}",
             f"  {r['text']}"]
    if r.get("sats"):
        lines.append(f"  Quoted: {r['sats']} sats")
    if r.get("contact"):
        lines.append(f"  Contact: {r['contact']}")
    return "\n".join(lines)


@mcp.tool()
def list_services() -> str:
    """List what Soma can do. Returns available service categories and model."""
    cats = ", ".join(sorted(policy.WHITELIST))
    return (
        "Soma — agent marketplace (policy-gated, karma-ranked)\n\n"
        f"Accepted categories: {cats}\n\n"
        "Submit any request in natural language. Examples:\n"
        "- 'I need a Python script that monitors gas prices on Arbitrum'\n"
        "- 'Review my smart contract for vulnerabilities'\n"
        "- 'Write a blog post about AI agent reputation systems'\n\n"
        "Flow: policy filter → category match → agent discovery → Lightning payment.\n"
        "Use find_agents(category) to browse available agents, get_agent_profile(agent_id) for details."
    )


@mcp.tool()
def find_agents(category: str = "", min_karma: int = 0) -> str:
    """Discover agents available in Soma.

    category: one of research, writing, coding, analysis, tutoring, creative, translation
    min_karma: minimum karma required for the agent to show"""
    cat = category.strip() or None
    if cat and cat not in policy.WHITELIST:
        return f"Category '{cat}' not in whitelist: {sorted(policy.WHITELIST)}"
    results = profiles.search(category=cat, min_karma=min_karma)
    if not results:
        return "No agents found matching criteria."
    lines = [f"Found {len(results)} agents:"]
    for p in results[:10]:
        prices = p.get("base_price_sats", {})
        price_str = ", ".join(f"{c}:{s}" for c, s in prices.items())
        lines.append(f"- {p['agent_id']} ({p['display_name']}) — {price_str} sats")
        lines.append(f"  {p.get('description', '')[:80]}")
    return "\n".join(lines)


@mcp.tool()
def get_agent_profile(agent_id: str) -> str:
    """Get full profile of a Soma agent.

    agent_id: the agent's identifier"""
    p = profiles.load(agent_id)
    if not p:
        return f"Profile '{agent_id}' not found."
    karma = _get_karma(agent_id)
    lines = [
        f"Agent: {p.get('display_name')} ({p['agent_id']})",
        f"Karma: {karma}",
        f"Categories: {', '.join(p.get('categories', []))}",
        f"Delivery time: {p.get('delivery_time_hours')}h",
        f"Proof type: {p.get('proof_type')}",
        f"Description: {p.get('description')}",
        "Prices (sats):",
    ]
    for c, s in p.get("base_price_sats", {}).items():
        lines.append(f"  {c}: {s}")
    return "\n".join(lines)


_start_time = time.time()

if __name__ == "__main__":
    import threading

    def run_rest():
        port = int(os.getenv("PORT", 8022))
        print(f"Soma concierge :{port}")
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()

    threading.Thread(target=run_rest, daemon=True).start()

    listener.start()

    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)

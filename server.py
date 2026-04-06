"""
Soma concierge backend
Puerto: 8022
"""
import os, json, uuid, time, requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

BOT_TOKEN       = os.getenv("BOT_TOKEN")
CHAT_ID         = os.getenv("CHAT_ID")
PHOENIXD_URL    = "http://127.0.0.1:9740"
PHOENIXD_PASS   = os.getenv("PHOENIXD_PASSWORD", "")
REQUESTS_FILE   = Path(__file__).parent / "requests.json"
STATE_FILE      = Path("/home/dell7568/moltbook_agent/state.json")


def load_requests():
    if REQUESTS_FILE.exists():
        return json.loads(REQUESTS_FILE.read_text())
    return {}

def save_requests(data):
    REQUESTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def notify_telegram(text: str, chat_id=None):
    target = chat_id or CHAT_ID
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": target, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )

def get_chat_id(handle: str):
    """Busca el chat_id de un @handle en el state del bot."""
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
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, body):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        try:
            data = json.loads(body)
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        # ── POST /request — nuevo pedido del form ──────────────────────────
        if self.path == "/request":
            text    = data.get("request", "").strip()
            contact = data.get("contact", "").strip()

            if not text:
                self._json(400, {"error": "empty request"})
                return

            req_id = str(uuid.uuid4())[:8]
            reqs   = load_requests()
            reqs[req_id] = {
                "text":    text,
                "contact": contact,
                "status":  "pending",
                "created": int(time.time()),
            }
            save_requests(reqs)

            msg = f"*SOMA — nuevo pedido* `{req_id}`\n\n{text}"
            if contact:
                msg += f"\n\nContacto: `{contact}`"
                # Si tiene chat_id guardado, avisarlo
                cid = get_chat_id(contact)
                msg += f"\n{'✅ chat_id conocido — entrega automática activa' if cid else '⚠️ chat_id desconocido — pedirle que escriba al bot primero'}"
            msg += f"\n\nPara cotizar: `/invoice {req_id} <sats>`"

            notify_telegram(msg)
            self._json(200, {"ok": True, "id": req_id})

        # ── POST /invoice — generar invoice Lightning para un pedido ───────
        elif self.path == "/invoice":
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

        else:
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
    req_id = str(uuid.uuid4())[:8]
    reqs = load_requests()
    reqs[req_id] = {
        "text": request_text.strip(),
        "contact": contact.strip(),
        "status": "pending",
        "created": int(time.time()),
    }
    save_requests(reqs)

    msg = f"*SOMA — nuevo pedido* `{req_id}`\n\n{request_text.strip()}"
    if contact:
        msg += f"\n\nContacto: `{contact}`"
    try:
        notify_telegram(msg)
    except Exception:
        pass

    return f"Request {req_id} submitted. Status: pending. A concierge will review and quote in sats."


@mcp.tool()
def check_status(request_id: str) -> str:
    """Check the status of a Soma request.

    request_id: the ID returned by submit_request"""
    reqs = load_requests()
    if request_id not in reqs:
        return f"Request '{request_id}' not found."
    r = reqs[request_id]
    lines = [f"Request {request_id}: {r['status']}",
             f"  {r['text']}"]
    if r.get("sats"):
        lines.append(f"  Quoted: {r['sats']} sats")
    if r.get("contact"):
        lines.append(f"  Contact: {r['contact']}")
    return "\n".join(lines)


@mcp.tool()
def list_services() -> str:
    """List what Soma can do. Returns available service categories."""
    return (
        "Soma — agent marketplace (concierge model)\n\n"
        "Submit any request in natural language. Examples:\n"
        "- 'I need a Python script that monitors gas prices on Arbitrum'\n"
        "- 'Review my smart contract for vulnerabilities'\n"
        "- 'Write a blog post about AI agent reputation systems'\n"
        "- 'Help me set up a Lightning node'\n\n"
        "A human concierge reviews, quotes in sats, and delivers.\n"
        "Payment via Lightning Network."
    )


if __name__ == "__main__":
    import threading

    # REST API on 8022
    def run_rest():
        port = int(os.getenv("PORT", 8022))
        print(f"Soma concierge :{port}")
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()

    threading.Thread(target=run_rest, daemon=True).start()

    # MCP on 8023
    transport = os.getenv("MCP_TRANSPORT", "sse")
    mcp.run(transport=transport)

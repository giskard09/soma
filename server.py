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


def load_requests():
    if REQUESTS_FILE.exists():
        return json.loads(REQUESTS_FILE.read_text())
    return {}

def save_requests(data):
    REQUESTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def notify_telegram(text: str):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )


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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8022))
    print(f"Soma concierge :{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

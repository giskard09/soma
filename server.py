"""
Soma concierge backend — recibe pedidos del form y los reenvía a Telegram.
Puerto: 8020
Arrancar: python server.py
"""
import os
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
import json

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

def notify_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silenciar logs de cada request

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/request":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        try:
            data = json.loads(body)
            request_text = data.get("request", "").strip()
            contact      = data.get("contact", "").strip()
        except Exception:
            request_text = body.decode(errors="ignore")
            contact = ""

        if not request_text:
            self.send_response(400)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"error": "empty request"}')
            return

        msg = f"*SOMA — nuevo pedido*\n\n{request_text}"
        if contact:
            msg += f"\n\nContacto: `{contact}`"

        try:
            notify_telegram(msg)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        except Exception as e:
            self.send_response(500)
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8022))
    print(f"Soma concierge listening on :{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

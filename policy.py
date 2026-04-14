"""
Soma policy filter
Groq llama-3.3-70b primary, Anthropic Haiku fallback.
"""
import os, json, hashlib, time
from pathlib import Path

POLICY_VERSION = "1.1"
POLICY_MD = Path(__file__).parent / "soma_policy.md"
LOG_FILE = Path(__file__).parent / "soma_policy_log.jsonl"

WHITELIST = {"research", "writing", "coding", "analysis",
             "tutoring", "creative", "translation",
             "self_notification"}

BLACKLIST = {
    "impersonation", "credentials", "unauthorized_access",
    "directed_outreach", "fund_ops_foreign", "disinformation",
    "licensed_advice", "moderation_evasion",
    "invalid_input",
}

SYSTEM_PROMPT = """You are the Soma marketplace policy filter. Classify each request.

ACCEPT categories (use the exact name as category):
- research: public information search, synthesis, summary
- writing: original non-directed text (blog, docs, fiction, essay)
- coding: scripts, debugging, code review, refactors
- analysis: reviewing data, contracts, documents
- tutoring: explaining concepts, answering technical questions
- creative: art, music, generative design
- translation: translating text
- self_notification: scheduled alerts, reminders, digests, or monitoring results delivered to the requester themselves (e.g. "tell me every morning if the train is running", "alert me if BTC crosses X", "weekly report on Y to my Telegram"). NOT outreach to third parties.

REJECT categories (use the exact name as category):
- impersonation: writing as another real person
- credentials: passwords, seeds, private keys, auth tokens
- unauthorized_access: bypassing rate limits, brute force, evasive scraping
- directed_outreach: mass messages, DM automation, or unsolicited contact DIRECTED AT THIRD PARTIES (not the requester). If the notification target is the requester, use self_notification instead.
- fund_ops_foreign: on-chain ops with wallets not owned by requester
- disinformation: factual-looking content without sources on sensitive topics. Do NOT use for gibberish or unreadable input — use invalid_input.
- licensed_advice: individualized legal/financial/medical advice
- moderation_evasion: bypassing filters of other systems
- invalid_input: gibberish, empty-meaning, random characters, or text that is not a parseable request in any language

ESCALATE: anything ambiguous, borderline, or not clearly in either list. Use category=null.

Return ONLY valid JSON, no prose:
{"decision":"accept|reject|escalate","category":"<exact category name or null>","reason":"<short>"}

Bias toward escalate when unsure. Never accept reject items even if framed innocently.
The category field MUST be an exact name from the lists above, not 'whitelist' or 'blacklist'."""


def _hash_request(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _hash_contact(contact: str | None) -> str | None:
    if not contact:
        return None
    return hashlib.sha256(contact.strip().encode()).hexdigest()[:16]


def _preview(text: str, n: int = 40) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t[:n]


def _log(request_text: str, result: dict, contact: str | None = None):
    entry = {
        "ts": int(time.time()),
        "request_hash": _hash_request(request_text),
        "contact_hash": _hash_contact(contact),
        "text_preview": _preview(request_text),
        "decision": result.get("decision"),
        "category": result.get("category"),
        "reason": result.get("reason"),
        "policy_version": POLICY_VERSION,
        "provider": result.get("provider"),
    }
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _parse_json(s: str) -> dict:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    return json.loads(s.strip())


def _check_groq(request_text: str) -> dict:
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Request: {request_text}"},
        ],
        temperature=0,
        max_tokens=150,
    )
    result = _parse_json(resp.choices[0].message.content)
    result["provider"] = "groq"
    return result


def _check_haiku(request_text: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Request: {request_text}"}],
    )
    result = _parse_json(resp.content[0].text)
    result["provider"] = "haiku"
    return result


REJECT_MESSAGES = {
    "directed_outreach": "We do not run outreach to third parties. If you meant an alert/reminder for yourself, rephrase it as 'notify ME when X'.",
    "invalid_input": "Your request is unreadable. Please rewrite it with a clear task.",
    "impersonation": "We do not impersonate real people.",
    "credentials": "We do not handle credentials, seeds, or private keys.",
    "unauthorized_access": "We do not perform unauthorized access or bypass rate limits.",
    "fund_ops_foreign": "We do not operate wallets you do not own.",
    "disinformation": "We do not produce factual-looking content without sources on sensitive topics.",
    "licensed_advice": "We do not give individualized legal, financial, or medical advice. Ask a licensed professional.",
    "moderation_evasion": "We do not bypass other systems' moderation.",
}


def check_policy(request_text: str, contact: str | None = None) -> dict:
    """
    Returns: {decision, category, reason, policy_version, provider, user_message?}
    decision ∈ {accept, reject, escalate}
    contact is optional; only its sha256[:16] is logged, never the raw value.
    """
    if not request_text or not request_text.strip():
        result = {"decision": "reject", "category": "invalid_input",
                  "reason": "empty request", "policy_version": POLICY_VERSION,
                  "provider": "local"}
        _log(request_text or "", result, contact)
        result["user_message"] = REJECT_MESSAGES["invalid_input"]
        return result

    try:
        result = _check_groq(request_text)
    except Exception:
        try:
            result = _check_haiku(request_text)
        except Exception as e2:
            result = {"decision": "escalate", "category": None,
                      "reason": f"filter unavailable: {e2}",
                      "provider": "local"}

    if result.get("decision") not in ("accept", "reject", "escalate"):
        result["decision"] = "escalate"
    if result.get("decision") == "accept" and result.get("category") not in WHITELIST:
        result["decision"] = "escalate"
        result["reason"] = "category not in whitelist"
    if result.get("category") in BLACKLIST:
        result["decision"] = "reject"

    result["policy_version"] = POLICY_VERSION
    _log(request_text, result, contact)

    if result["decision"] == "reject":
        msg = REJECT_MESSAGES.get(result.get("category"))
        if msg:
            result["user_message"] = msg
    return result

"""
Instructions API — exposes a self-contained prompt that teaches Manus AI
(or any agent) how to operate this WhatsApp service end-to-end.

The goal of this router is twofold:
  1. Give an external agent ONE endpoint to read on first contact and walk
     away with everything it needs (login flow, send semantics, error rubric,
     when-to-poll vs when-to-webhook, etc.).
  2. Allow that agent to refresh against the *live* state of this server
     (current connection, auto-reply config, registered webhooks) so the
     same prompt stays accurate as the operator changes settings.

Three views are provided:

  GET /api/v1/instructions            — full markdown prompt (no auth)
  GET /api/v1/instructions/sections   — structured JSON of the same content
  GET /api/v1/instructions/runtime    — live snapshot + tailored advice
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Response

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import db
from app.core.llm import llm_client
from app.core.webhooks import webhook_service
from app.core.whatsapp_client import wa_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/instructions", tags=["Instructions (Manus Bootstrap)"])


# ─── The Prompt ──────────────────────────────────────────────────────────────
#
# This is the single source of truth for "how should an agent talk to this
# service." Keep it updated when API behavior changes — the JSON `sections`
# view is derived from the same data so both stay in sync.

MANUS_SYSTEM_PROMPT = """\
# You are operating the iDeep WhatsApp Bot API

This service is a WhatsApp Web bridge. It runs `whatsmeow` (via the `neonize`
Python wrapper) under a FastAPI front-end and exposes the user's personal
WhatsApp account through a REST API. You — the AI agent — are the controller.
Treat the user's phone as the source of truth and this API as a thin remote.

Your job, in order of priority:
  1. Keep the WhatsApp link healthy. A dead link makes every other capability
     useless, so login state comes first.
  2. Honor the user's intent precisely (right contact, right message, right time).
  3. Surface failures *clearly* to the user instead of pretending success.
  4. Never spam, never send without confidence, never fight the rate limits.

---

## 1. Authentication

Every endpoint under `/api/v1/*` (except `/health`, `/api/v1/info`, and
`/api/v1/instructions*`) requires authentication. Two options:

  • API key (recommended for agents):  `X-API-Key: <key>` header
  • JWT bearer token (web UI flow):    `Authorization: Bearer <token>`

The user gives you the API key once. Store it; do not ask again per request.
If you receive 401/403, the key is wrong or expired — ask the user to verify
the value of `API_KEY` in their `.env` rather than retrying.

---

## 2. Connection lifecycle

### 2.1 The state machine

The server tracks one of these states (`GET /api/v1/connection/status` →
`state`):

  disconnected      — never connected, or cleanly disconnected
  connecting        — handshake in progress, no QR yet
  qr_ready          — a QR code is available for scanning
  pair_code_ready   — an 8-digit pairing code was issued
  connected         — fully linked, sends/reads work
  logged_out        — session was wiped (by us, or by the phone unlinking us)

Happy path:
    disconnected → connecting → qr_ready → connected
    disconnected → connecting → pair_code_ready → connected
Recovery path:
    connected → logged_out → (operator re-links) → connecting → qr_ready → connected

### 2.2 Login: QR code flow (default)

  1. POST `/api/v1/connection/connect` to start the handshake.
     Returns immediately with `status=connecting` — the connect runs async.
  2. Poll `GET /api/v1/connection/qr` every **2–3 seconds**. Initially the
     state is `connecting` and `qr_base64` is null; within ~3–10 seconds it
     flips to `qr_ready` with a fresh QR.
  3. Render `qr_base64` (a base64-encoded PNG) to the user — it is already a
     data-URL-friendly PNG payload, just prefix `data:image/png;base64,`.
     Tell the user: open WhatsApp → Settings → Linked Devices → Link a device.

  **QR refresh interval:** WhatsApp rotates the QR roughly every **20 seconds**.
  When that happens, this server emits a fresh QR via the same `/qr` endpoint
  (the `qr_data` and `qr_base64` fields update in place) and pushes a `qr`
  webhook event. So:
    • Keep polling at 2–3s while the user is scanning.
    • Replace the displayed image whenever `qr_data` changes.
    • Stop polling as soon as `state == connected`.
    • If the user takes longer than ~60 seconds without scanning, tell them
      "the QR refreshes automatically, just scan the latest one shown."

  If `state` is `connected` when the user calls /qr, return a friendly
  "already linked" message — the response itself states this.

### 2.3 Login: pair code flow (alternative)

  1. POST `/api/v1/connection/pair-code` with body `{"phone_number": "989..."}`.
     The phone number must include country code; '+', spaces, and dashes are
     stripped server-side, but DO send digits if you can.
  2. The response contains an 8-character `pair_code` (e.g. `ABCD-1234`).
     Tell the user: open WhatsApp → Settings → Linked Devices →
     **Link with phone number instead** → enter this code.
  3. Poll `GET /api/v1/connection/status` every 2–3s; expect `connected`
     within ~30 seconds.

  **When to recommend pair code instead of QR:**
    • The user is on a phone-only device (cannot easily switch between two
      apps to scan their own screen).
    • The user explicitly asks for it ("can I just type a code?").
    • The user has accessibility needs that make scanning painful.
    • Your UI is text-only / can't render images.
  Otherwise default to QR — it's faster end-to-end.

### 2.4 Detecting and recovering from session loss

Sessions can die three ways. Each has a different signal:

  (a) **Operator unlinked the device on their phone.** The server emits a
      `logged_out` webhook event and wipes its local DB. Next interaction
      will return state=`logged_out`.

  (b) **Silent session staleness.** WhatsApp sometimes invalidates a
      session without dropping the TCP connection — sends still return a
      message ID but never deliver. Symptoms:
        • `/connection/status` reports `sends_without_receipt >= 3`
        • You see no incoming messages and no receipts for many minutes
        • The user reports "the recipient never got it"
      The server runs an internal health watcher (60s) and an automatic
      revalidation after 3 receiptless sends, which will transition to
      `logged_out` and emit `session_expired`. You can also force this check
      by calling `GET /api/v1/connection/probe` — that hits whatsmeow
      directly and is authoritative.

  (c) **Stream replaced.** The user logged in elsewhere with the same
      account. State drops to `disconnected` and the `stream_replaced`
      webhook fires. Treat the same as (a).

When you detect any of the above, **do not silently retry**. Tell the user
the session needs to be re-linked, walk them through §2.2 again. Lying about
delivery is the worst thing you can do here — they trust you with personal
messages.

### 2.5 Disconnect vs Logout

  • POST `/api/v1/connection/disconnect` — closes the socket but keeps the
    session DB. A subsequent `connect` resumes silently.
  • POST `/api/v1/connection/logout` — wipes local credentials and triggers
    a server-side unlink. Next `connect` requires a fresh QR. Use this when
    the user wants to switch to a different WhatsApp account.

Don't logout to "fix" generic problems; it forces a full re-link. Disconnect
+ connect is the lighter reset.

---

## 3. Sending messages

### 3.1 Pick the right endpoint

Use this decision order:

  1. `POST /api/v1/smart/send` — **default for almost everything.**
     Accepts `to` as a *contact name* OR a phone number; does fuzzy matching
     against the user's contacts list; returns the resolved contact so you
     can show the user "I matched 'Mom' to '+98...'  — sending now."

  2. `POST /api/v1/messages/send` — when you already have a verified phone
     number (digits only, country code, no '+').

  3. `POST /api/v1/assistant/reply-as-assistant` or
     `POST /api/v1/assistant/reply` — when the message should be tagged as
     coming from the AI assistant (prefixes `*<assistant_name>*` to the body).
     Use this when the user is *away* and you are answering on their behalf,
     not when the user is themselves the author.

  4. `POST /api/v1/messages/send-bulk` — for fan-outs. The server enforces
     `delay_seconds` between sends to dodge rate limits; default 2s.
     Be conservative: WhatsApp will silently throttle suspicious patterns.

### 3.2 Phone number format

Always digits-only, **with country code**, **no '+'**, length 8–15.
  Good:  `989121234567`, `14155552671`
  Bad:   `+98 912 123 4567` (plus sign, spaces — the server normalizes,
         but be explicit), `9121234567` (no country code)

The server pre-validates against `is_on_whatsapp` before sending. If the
number is not registered, you get `success: false, error: "Number ... is not
registered on WhatsApp"`. Surface this to the user; do not retry with the
same number.

### 3.3 Send-success semantics

`success: true` means: WhatsApp acknowledged the send AND returned a
non-empty message ID and a non-zero timestamp. An empty SendResponse
(known failure mode for malformed JIDs / stale Signal sessions) is treated
as `success: false`.

After 3 consecutive sends with no delivery receipts the server flags the
session as suspect and may auto-logout. If your send returns success but
you've also seen `sends_without_receipt > 0` in `/connection/status`,
warn the user that delivery is unverified.

### 3.4 Don't

  • Don't loop-retry a failing send. One failure → tell user → wait for
    intent.
  • Don't send to numbers you didn't verify (either via fuzzy resolve or
    explicit user confirmation). Wrong-number sends are unrecoverable.
  • Don't send dozens of messages in quick succession even with bulk-send
    — that's how accounts get banned.

---

## 4. Contact resolution (fuzzy matching)

The user thinks in names; WhatsApp speaks in JIDs (`<phone>@s.whatsapp.net`
for people, `...@g.us` for groups). Bridge it like this:

  • `POST /api/v1/smart/resolve` — best for ambiguous queries. Returns
    ranked matches with `match_score` (0–1).
      - score ≥ 0.8 → confident match, proceed.
      - 0.5 ≤ score < 0.8 → confirm with user: "I found 'Masoud Nayebi-Tech
        Assistant' — did you mean them?"
      - score < 0.5 → ask user to be more specific.
  • `GET /api/v1/contacts/search?query=...` — substring search.
  • `GET /api/v1/contacts/` — full list (paginated via `limit`).

If you get multiple matches with similar high scores, do NOT pick one
silently. Show the top 2–3 and let the user choose. Sending to the wrong
"Ali" is worse than asking one extra question.

---

## 5. Reading messages

The server keeps an in-memory ring buffer (default cap: 10000 messages)
plus a SQLite store for persistence. From an agent's perspective:

  • `GET /api/v1/smart/recent?count=N` — "what's new?" Most recent
     incoming, newest-first.
  • `POST /api/v1/smart/search` — content + fuzzy contact filter.
     Useful for "when did I last talk to Sarah about the meeting?"
  • `GET /api/v1/messages/chat/{phone}` — full chat with one contact.
  • `GET /api/v1/messages/chats` — all chats, last-message preview.
  • `GET /api/v1/messages/history?limit=&offset=` — paginated full feed.

The store is bounded; **don't rely on long-tail history**. If the user asks
about a conversation older than the store window, say so honestly rather
than fabricate.

Group messages are present in the store but tagged `is_group: true`. The
auto-reply system never replies to groups by design — you shouldn't either,
unless explicitly asked.

---

## 6. Webhooks (push, the right default for real-time)

Polling `recent` works but wastes calls. If you have a callable URL,
register a webhook and let the server push events to you.

  POST `/api/v1/webhooks/register`
    {
      "url": "https://your-manus-endpoint/whatsapp",
      "events": ["message", "session_expired", "connected", "disconnected"],
      "secret": "<optional HMAC secret>",
      "name": "manus-primary"
    }

Available events:
    message            — new incoming message
    message_sent       — outgoing message confirmed sent
    auto_reply_sent    — built-in auto-reply fired
    connected          — WhatsApp link established
    disconnected       — TCP dropped (may auto-reconnect)
    qr                 — new QR code emitted (rotates every ~20s)
    receipt            — delivery/read receipt
    session_expired    — session went stale, re-link required
    logged_out         — device unlinked
    stream_replaced    — logged in elsewhere
    pair_status        — pair-code flow progressed

If you supplied a `secret`, each request will carry an HMAC-SHA256 of the
body in `X-Webhook-Signature`. Verify it.

**Patterns:**
  • Subscribe to `message` for proactive auto-handling.
  • Subscribe to `session_expired` + `logged_out` so you can immediately
    notify the user instead of finding out on the next send attempt.
  • Subscribe to `qr` only if you're actively rendering a login UI;
    otherwise the polling pattern in §2.2 is simpler.

---

## 7. Auto-reply (the built-in iDeep AI Assistant)

The server has its own auto-reply engine that runs *independently of you*.
The user can configure it from the web UI, but you can also program it
through the API. Three layers, evaluated in this order on every incoming
message:

  1. **Global enable** (`/api/v1/assistant/config` → `enabled`).
     Off → never auto-replies. On → keep evaluating.
  2. **Quiet hours.** A timezone-aware HH:MM window. During quiet hours
     either an "away message" is sent (if configured) or replies are
     suppressed entirely.
  3. **Rules** (`/api/v1/assistant/rules`). First matching enabled rule
     wins, ordered by `priority` (lower first). A rule can:
       • Match by `contact` (JID or phone substring) and/or `keyword`.
       • `match_mode`: contains | exact | starts_with | regex.
       • Reply with static `message`, OR `use_llm: true` to generate via LLM.
       • Throttle with `cooldown_seconds` per-sender.
  4. **Per-contact persona** (`/api/v1/assistant/personas`).
     Adds context to the LLM system prompt for that contact and can opt
     them in or out of LLM replies regardless of the global toggle.
  5. **Catch-all LLM** if `llm_enabled` is on globally and no rule matched.
  6. **Default static message** if no rules exist and LLM is off.

Common things the user will ask you to do here:
  • "Stop auto-replying" → PUT config `{"enabled": false}`.
  • "Don't auto-reply to my mom" → upsert persona for her contact with
    `use_llm: false` AND ensure no static-rule matches her contact.
  • "Reply formally to clients, casually to friends" → personas with
    different `system_prompt_override` values.
  • "Don't reply between midnight and 8am" → PUT config with
    `quiet_hours.enabled: true, start: "00:00", end: "08:00", timezone: "..."`.

The LLM provider is read-only via `GET /api/v1/assistant/llm`. Whether
LLM replies actually work depends on `LLM_PROVIDER`/`LLM_API_KEY` in the
operator's `.env` — that's outside your control. Always check `configured:
true` before promising LLM-driven behavior.

---

## 8. Scheduled sends

  POST `/api/v1/schedule/`
    { "phone": "989...", "message": "...", "scheduled_at": "<ISO 8601>" }

  • `scheduled_at` accepts any ISO 8601 string. With offset (e.g.
    `2026-05-06T09:00:00+03:30`) it is honored. **Naive strings are
    treated as UTC** — convert from the user's timezone yourself.
  • The dispatcher polls every ~20 seconds, so resolution is "within the
    next 20 seconds of the target time."
  • If quiet hours are enabled with `defer_scheduled: true`, sends inside
    the window are held until the window closes, not dropped.
  • `DELETE /api/v1/schedule/{id}` cancels a pending send.
  • `GET /api/v1/schedule/?status=pending` lists what's queued.

Never schedule sends in the past; the server will reject them. When
relaying a user's "remind X tomorrow at 9am" intent, always echo back the
absolute UTC datetime so the user can sanity-check it before you commit.

---

## 9. Error handling rubric

When an endpoint returns success=false or a non-2xx status, classify before
acting:

  503 / "WhatsApp is not connected"
      → Connection state isn't `connected`. Check `/connection/status`. If
        `logged_out` or `session_expired`, run §2 login again. If
        `disconnected`, call `/connection/connect`.

  "session is no longer logged in"
      → Same as above; the server already wiped its state. Re-link.

  "Number ... is not registered on WhatsApp"
      → Don't retry. Tell the user. Maybe they have the wrong number.

  "Lookup failed: ..."
      → Transient. Retry once after ~3 seconds. If it fails again,
        suspect connection health — call `/connection/probe`.

  "WhatsApp did not acknowledge the send"
      → A delivery-failure mode. If it repeats, the session is likely
        stale; trigger `/connection/probe`.

  401 / 403
      → Auth problem. Stop and tell the user; do not retry.

  contact_not_found from `/smart/send`
      → Use `/smart/resolve` with a more relaxed term, or ask the user
        for the phone number directly.

---

## 10. Quick reference card

  STATUS         GET  /api/v1/connection/status
  PROBE          GET  /api/v1/connection/probe              ← stale-session check
  CONNECT        POST /api/v1/connection/connect
  QR             GET  /api/v1/connection/qr                 ← poll every 2–3s
  PAIR CODE      POST /api/v1/connection/pair-code
  LOGOUT         POST /api/v1/connection/logout

  SEND (smart)   POST /api/v1/smart/send                    ← default
  SEND (raw)     POST /api/v1/messages/send
  REPLY AS AI    POST /api/v1/assistant/reply-as-assistant
  RECENT         GET  /api/v1/smart/recent
  SEARCH         POST /api/v1/smart/search
  RESOLVE        POST /api/v1/smart/resolve
  CONTACTS       GET  /api/v1/contacts/

  ASSISTANT CFG  GET  /api/v1/assistant/config
                 PUT  /api/v1/assistant/config
  RULES          GET/POST/PATCH/DELETE /api/v1/assistant/rules
  PERSONAS       GET/PUT/DELETE /api/v1/assistant/personas

  SCHEDULE       POST /api/v1/schedule/
                 GET  /api/v1/schedule/
                 DEL  /api/v1/schedule/{id}

  WEBHOOKS       POST /api/v1/webhooks/register
                 GET  /api/v1/webhooks/

  RUNTIME META   GET  /api/v1/instructions/runtime          ← live snapshot

---

## 11. Things to remember

  • Names ≠ JIDs. Always resolve before sending.
  • Phone format is digits-only with country code.
  • QR refreshes every ~20 seconds — keep polling, replace the image.
  • Pair code is the alternative when scanning is awkward.
  • A "successful" send with no receipt eventually becomes a session
    issue. Watch `sends_without_receipt`.
  • The user's WhatsApp account is precious — false sends, spammy
    behavior, or unverified delivery claims damage real relationships.
  • When uncertain, ask. One extra clarifying question is cheaper than
    one wrong message.
"""


# ─── Structured sections (machine-readable view of the same content) ─────────
#
# This mirrors MANUS_SYSTEM_PROMPT for agents that prefer to consume the
# content as structured data instead of parsing markdown headings.

STRUCTURED_SECTIONS: List[Dict[str, Any]] = [
    {
        "id": "auth",
        "title": "Authentication",
        "summary": "API key in X-API-Key header, or JWT bearer token.",
        "details": [
            "All /api/v1/* endpoints require auth except /health, /api/v1/info, /api/v1/instructions*.",
            "Prefer the API key for agents. Don't retry on 401/403 — surface to the user.",
        ],
    },
    {
        "id": "connection_states",
        "title": "Connection state machine",
        "summary": "Six states: disconnected, connecting, qr_ready, pair_code_ready, connected, logged_out.",
        "details": [
            "Happy path: disconnected → connecting → qr_ready (or pair_code_ready) → connected.",
            "Recovery path: connected → logged_out → operator re-links.",
            "Read state via GET /api/v1/connection/status.",
        ],
    },
    {
        "id": "qr_login",
        "title": "QR-code login flow",
        "summary": "POST /connect, then poll GET /qr every 2–3 seconds. QR rotates every ~20 seconds.",
        "details": [
            "qr_base64 is a PNG payload — prefix `data:image/png;base64,` to render.",
            "Replace the displayed image whenever qr_data changes.",
            "Stop polling when state == connected.",
            "Recommend QR by default — it's faster than pair code.",
        ],
        "qr_refresh_interval_seconds": 20,
        "qr_poll_interval_seconds": [2, 3],
    },
    {
        "id": "pair_code_login",
        "title": "Pair-code login flow",
        "summary": "POST /pair-code with phone digits → 8-char code; user enters it on phone.",
        "details": [
            "Use when user can't easily scan a QR (phone-only setup, accessibility, text-only UI).",
            "Phone must include country code; '+' / spaces / dashes are stripped server-side.",
            "Poll /connection/status until state == connected (~30s).",
        ],
        "when_to_prefer": [
            "User explicitly asks for a code instead of QR",
            "User cannot dual-use their phone (scan its own screen)",
            "Accessibility need",
            "Your UI cannot render images",
        ],
    },
    {
        "id": "session_expiry",
        "title": "Detecting session loss",
        "summary": "Sessions can die silently. Watch sends_without_receipt + use /probe.",
        "details": [
            "GET /api/v1/connection/probe is authoritative — calls whatsmeow directly.",
            "Server auto-revalidates after 3 receiptless sends.",
            "Internal health watcher polls every 60s.",
            "Webhook events: session_expired, logged_out, stream_replaced, disconnected.",
            "Never silently retry on session loss — instruct user to re-link.",
        ],
    },
    {
        "id": "sending",
        "title": "Sending messages",
        "summary": "Prefer /api/v1/smart/send (fuzzy resolve). Phone format: digits-only, country code, 8–15 chars, no '+'.",
        "details": [
            "Use /smart/send for name-or-phone input.",
            "Use /messages/send when phone is already verified.",
            "Use /assistant/reply-as-assistant when speaking AS the assistant.",
            "success=true requires non-empty message_id AND non-zero timestamp.",
            "Don't loop-retry on failure. Don't send to unverified numbers.",
        ],
        "phone_format": {
            "regex": r"^\d{8,15}$",
            "example_good": ["989121234567", "14155552671"],
            "example_bad": ["+989121234567", "9121234567"],
        },
    },
    {
        "id": "contact_resolution",
        "title": "Contact resolution",
        "summary": "Fuzzy-match names → JIDs via /smart/resolve. Disambiguate when scores are close.",
        "details": [
            "score ≥ 0.8: proceed.",
            "0.5 ≤ score < 0.8: confirm with user.",
            "score < 0.5: ask user for more specifics.",
            "Multiple high-score matches: show top 2–3, let user choose.",
        ],
    },
    {
        "id": "reading",
        "title": "Reading messages",
        "summary": "In-memory store (cap 10000) + SQLite. Use /smart/recent and /smart/search for agent queries.",
        "details": [
            "Group messages are tagged is_group: true; auto-reply skips them.",
            "Don't fabricate history beyond the store window.",
        ],
    },
    {
        "id": "webhooks",
        "title": "Webhooks (push events)",
        "summary": "Register a callable URL to receive events instead of polling.",
        "events": [
            "message", "message_sent", "auto_reply_sent",
            "connected", "disconnected",
            "qr", "receipt",
            "session_expired", "logged_out", "stream_replaced",
            "pair_status",
        ],
        "details": [
            "If `secret` is set, X-Webhook-Signature carries HMAC-SHA256 of the body.",
            "Subscribe to message + session_expired + logged_out as a sane default.",
        ],
    },
    {
        "id": "auto_reply",
        "title": "Auto-reply system",
        "summary": "Layered: global enable → quiet hours → rules (priority order) → personas → catch-all LLM → default message.",
        "details": [
            "Rules: contact + keyword + match_mode (contains|exact|starts_with|regex), cooldown_seconds, priority.",
            "Personas: per-contact context, can override LLM enable.",
            "Quiet hours: timezone-aware HH:MM window with optional away message.",
            "Auto-reply NEVER fires on group messages by design.",
        ],
    },
    {
        "id": "scheduled_sends",
        "title": "Scheduled sends",
        "summary": "ISO 8601 datetime; ~20s resolution; quiet-hours can defer.",
        "details": [
            "Naive datetimes are treated as UTC — convert from the user's TZ yourself.",
            "Echo absolute UTC time back to the user before committing.",
            "Past datetimes are rejected.",
        ],
        "poll_interval_seconds": 20,
    },
    {
        "id": "errors",
        "title": "Error rubric",
        "summary": "Classify before reacting. Don't loop. Surface unrecoverable errors to the user.",
        "details": [
            "503 / 'not connected' → check status, run login flow.",
            "'session is no longer logged in' → re-link required.",
            "'not registered on WhatsApp' → don't retry; wrong number.",
            "'Lookup failed' → retry once; on repeat call /probe.",
            "'WhatsApp did not acknowledge the send' → suspect stale session.",
            "401/403 → auth problem; stop and tell the user.",
        ],
    },
]


# ─── Endpoints ───────────────────────────────────────────────────────────────


@router.get(
    "",
    response_class=Response,
    responses={200: {"content": {"text/markdown": {}, "application/json": {}}}},
)
async def get_instructions(
    format: str = Query(
        "markdown",
        description="Response format: 'markdown' (text/markdown body) or 'json' (wrapped in JSON).",
    ),
):
    """
    **Bootstrap prompt for Manus / external agents.**

    Returns a comprehensive operator's manual covering every flow this
    service exposes — login (QR + pair code), session-expiry detection,
    sending semantics, contact resolution, reading messages, webhooks,
    auto-reply rules, scheduled sends, and an error-handling rubric.

    No authentication required so an agent can fetch this on first contact
    before any credentials are wired up.

    Pair this with `GET /api/v1/instructions/runtime` (auth required) to
    get a live snapshot of the current connection state and config tailored
    to the operator's actual setup.
    """
    fmt = (format or "markdown").lower()
    if fmt in ("md", "markdown", "text"):
        return Response(content=MANUS_SYSTEM_PROMPT, media_type="text/markdown; charset=utf-8")
    if fmt == "json":
        return Response(
            content=_json_dump({
                "version": settings.APP_VERSION,
                "app_name": settings.APP_NAME,
                "format": "markdown",
                "prompt": MANUS_SYSTEM_PROMPT,
            }),
            media_type="application/json",
        )
    return Response(
        content=_json_dump({"error": f"unknown format '{format}'. Use markdown or json."}),
        media_type="application/json",
        status_code=400,
    )


@router.get("/sections")
async def get_instructions_sections():
    """
    **Structured view of the instructions.**

    Same content as the markdown prompt but split into machine-readable
    sections so an agent can index by topic. No auth required.
    """
    return {
        "version": settings.APP_VERSION,
        "app_name": settings.APP_NAME,
        "sections": STRUCTURED_SECTIONS,
        "section_ids": [s["id"] for s in STRUCTURED_SECTIONS],
    }


@router.get("/runtime")
async def get_instructions_runtime(user: dict = Depends(get_current_user)):
    """
    **Live snapshot tailored to the operator's current setup.**

    Combines the static prompt with the *current* state of this server so
    an agent can reason about what to actually do right now (connection
    state, whether LLM is configured, what webhooks are already wired,
    what auto-reply rules exist, etc.).

    Auth required — this leaks operator configuration.
    """
    status = wa_client.get_status()
    auto_reply_cfg = await wa_client.get_auto_reply_config()
    llm_info = llm_client.info()
    webhooks = webhook_service.list_webhooks()
    scheduled = await db.list_scheduled(status="pending", limit=10)

    advice = _runtime_advice(status, auto_reply_cfg, llm_info, webhooks)

    return {
        "version": settings.APP_VERSION,
        "app_name": settings.APP_NAME,
        "now_advice": advice,
        "connection": {
            "state": status.get("state"),
            "is_connected": status.get("is_connected"),
            "connected_at": status.get("connected_at"),
            "stored_messages_count": status.get("stored_messages_count"),
            "sends_without_receipt": status.get("sends_without_receipt"),
            "last_sent_at": status.get("last_sent_at"),
            "last_receipt_at": status.get("last_receipt_at"),
        },
        "auto_reply": {
            "enabled": auto_reply_cfg.get("enabled"),
            "assistant_name": auto_reply_cfg.get("assistant_name"),
            "llm_enabled": auto_reply_cfg.get("llm_enabled"),
            "rule_count": len(auto_reply_cfg.get("rules", [])),
            "quiet_hours": auto_reply_cfg.get("quiet_hours"),
        },
        "llm": llm_info,
        "webhooks": {
            "count": len(webhooks),
            "registered": [
                {"id": w.get("id"), "name": w.get("name"), "url": w.get("url"), "events": w.get("events")}
                for w in webhooks
            ],
        },
        "scheduled_pending": {
            "count": len(scheduled),
            "next": scheduled[0] if scheduled else None,
        },
        "endpoints": _endpoint_index(),
    }


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _runtime_advice(
    status: Dict[str, Any],
    auto_reply: Dict[str, Any],
    llm: Dict[str, Any],
    webhooks: List[Dict[str, Any]],
) -> List[str]:
    """Generate situational advice based on the current server state."""
    tips: List[str] = []
    state = status.get("state")
    if state == "disconnected":
        tips.append("Not connected. POST /api/v1/connection/connect, then poll /qr at 2–3s.")
    elif state == "logged_out":
        tips.append("Session was wiped. Run the login flow again — POST /connection/connect, then /qr.")
    elif state == "qr_ready":
        tips.append("QR is live. Poll /qr every 2–3s; the QR rotates every ~20s, replace the image when qr_data changes.")
    elif state == "pair_code_ready":
        tips.append("Pair code issued. Tell the user to enter it via WhatsApp → Linked Devices → Link with phone number instead.")
    elif state == "connecting":
        tips.append("Handshake in progress. Wait a few seconds, then poll /qr or /status.")
    elif state == "connected":
        tips.append("Link is healthy. Sends and reads should work.")
        srn = int(status.get("sends_without_receipt") or 0)
        if srn >= 3:
            tips.append(
                f"WARNING: {srn} consecutive sends without a delivery receipt. "
                "Run GET /connection/probe — the session may be silently stale."
            )

    if not auto_reply.get("enabled"):
        tips.append("Auto-reply is OFF. The user will see incoming messages but the server will not respond automatically.")
    elif auto_reply.get("llm_enabled") and not llm.get("configured"):
        tips.append(
            "Auto-reply has llm_enabled=true but the LLM provider is not configured "
            f"(provider={llm.get('provider')}). LLM replies will silently fall back to static rules."
        )

    if not webhooks:
        tips.append(
            "No webhooks registered. Consider POST /api/v1/webhooks/register with events "
            "['message','session_expired','logged_out'] for push-mode operation."
        )

    return tips


def _endpoint_index() -> Dict[str, Dict[str, str]]:
    """Compact endpoint cheatsheet, keyed by domain."""
    return {
        "connection": {
            "status": "GET /api/v1/connection/status",
            "probe": "GET /api/v1/connection/probe",
            "connect": "POST /api/v1/connection/connect",
            "disconnect": "POST /api/v1/connection/disconnect",
            "logout": "POST /api/v1/connection/logout",
            "qr": "GET /api/v1/connection/qr",
            "pair_code": "POST /api/v1/connection/pair-code",
            "token": "POST /api/v1/connection/token",
            "verify_token": "GET /api/v1/connection/token/verify",
        },
        "smart": {
            "send": "POST /api/v1/smart/send",
            "search": "POST /api/v1/smart/search",
            "resolve": "POST /api/v1/smart/resolve",
            "recent": "GET /api/v1/smart/recent",
            "reply_to": "POST /api/v1/smart/reply-to",
        },
        "messages": {
            "send": "POST /api/v1/messages/send",
            "send_bulk": "POST /api/v1/messages/send-bulk",
            "chats": "GET /api/v1/messages/chats",
            "chat": "GET /api/v1/messages/chat/{phone}",
            "search": "POST /api/v1/messages/search",
            "history": "GET /api/v1/messages/history",
            "unread": "GET /api/v1/messages/unread",
        },
        "contacts": {
            "list": "GET /api/v1/contacts/",
            "search": "GET /api/v1/contacts/search",
            "profile": "GET /api/v1/contacts/profile/{phone}",
            "check": "POST /api/v1/contacts/check",
            "groups": "GET /api/v1/contacts/groups",
        },
        "assistant": {
            "config": "GET|PUT /api/v1/assistant/config",
            "llm": "GET /api/v1/assistant/llm",
            "reply_as_assistant": "POST /api/v1/assistant/reply-as-assistant",
            "rules": "GET|POST /api/v1/assistant/rules",
            "rule_item": "PATCH|DELETE /api/v1/assistant/rules/{rule_id}",
            "personas": "GET|PUT /api/v1/assistant/personas",
            "persona_item": "DELETE /api/v1/assistant/personas/{contact}",
        },
        "schedule": {
            "list": "GET /api/v1/schedule/",
            "create": "POST /api/v1/schedule/",
            "cancel": "DELETE /api/v1/schedule/{send_id}",
        },
        "webhooks": {
            "list": "GET /api/v1/webhooks/",
            "register": "POST /api/v1/webhooks/register",
            "remove": "DELETE /api/v1/webhooks/{webhook_id}",
        },
        "instructions": {
            "prompt": "GET /api/v1/instructions",
            "sections": "GET /api/v1/instructions/sections",
            "runtime": "GET /api/v1/instructions/runtime",
        },
        "system": {
            "health": "GET /health",
            "info": "GET /api/v1/info",
            "docs": "GET /docs",
            "redoc": "GET /redoc",
        },
    }


def _json_dump(payload: Dict[str, Any]) -> str:
    """Local json.dumps wrapper so we don't import json at module level for one call."""
    import json
    return json.dumps(payload, ensure_ascii=False, indent=2)

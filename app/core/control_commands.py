"""
Owner control commands issued over WhatsApp itself.

Only messages sent by the account owner (is_from_me) are ever parsed — an
incoming message from a contact can never trigger a command, so nobody can
text `#bot off` and silence the assistant.

Two channels:
  - Any chat, prefixed:   `#mute` / `#unmute` / `#status` act on that chat;
                          `#bot off [2h]` / `#bot on` (or `#off` / `#on`)
                          act globally; `#bot instructions` (or `#instructions`)
                          replies with the command list.
  - The designated control chat: the same commands work bare, without the
    prefix (`off`, `on`, `off 2h`, `status`, `mute`, `unmute`, `instructions`).
"""

import re
from dataclasses import dataclass
from typing import Optional

# Actions: global_on | global_off | mute | unmute | status | instructions
@dataclass
class Command:
    action: str
    duration_seconds: Optional[int] = None
    raw: str = ""


_DURATION_RE = re.compile(r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.IGNORECASE)

_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def parse_duration(token: str) -> Optional[int]:
    """'30m' / '2h' / '1d' -> seconds, or None if not a duration."""
    m = _DURATION_RE.match(token.strip())
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2)[0].lower()
    return value * _UNIT_SECONDS[unit]


def _parse_tokens(tokens: list) -> Optional[Command]:
    """Interpret prefix-stripped (or control-chat bare) command tokens."""
    if not tokens:
        return None
    head = tokens[0].lower()
    rest = tokens[1:]

    # `bot on` / `bot off 2h` — drop the optional `bot` noun.
    if head == "bot":
        if not rest:
            return None
        head, rest = rest[0].lower(), rest[1:]

    if head in {"on", "enable"} and not rest:
        return Command(action="global_on")
    if head in {"off", "disable"}:
        if not rest:
            return Command(action="global_off")
        if len(rest) == 1:
            dur = parse_duration(rest[0])
            if dur:
                return Command(action="global_off", duration_seconds=dur)
        return None
    if head == "mute" and not rest:
        return Command(action="mute")
    if head == "unmute" and not rest:
        return Command(action="unmute")
    if head == "status" and not rest:
        return Command(action="status")
    if head in {"instructions", "help"} and not rest:
        return Command(action="instructions")
    return None


def parse_command(text: str, prefix: str = "#", is_control_chat: bool = False) -> Optional[Command]:
    """
    Parse an owner-sent message into a Command, or None if it is a normal
    message. Commands must be the entire message (bare code), so ordinary
    sentences that merely contain the prefix are never misread.
    """
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 40:
        return None

    if prefix and stripped.startswith(prefix):
        cmd = _parse_tokens(stripped[len(prefix):].strip().split())
    elif is_control_chat:
        cmd = _parse_tokens(stripped.split())
    else:
        return None

    if cmd:
        cmd.raw = stripped
    return cmd

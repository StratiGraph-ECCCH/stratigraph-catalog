"""The doorman: does THIS person hold a role in THIS study's room?

E.D., 19 November 2026: «l'embargo non ha impatto su accessibilità: se ti viene
concessa puoi. Se NON hai esplicito accesso non devi poter vedere quei dati nel
bucket o em-json». Until that day the catalogue served the content of a study
that is not public to anybody who had **signed in** — a valid token was enough —
while the room's door asked for a **role**. Measured on the dev stack: a person
with no role anywhere downloaded the whole em.json of a study under embargo.

The catalogue does not hold the roles; the node does. So it asks the node, with
the person's own token, the one question the node answers without side effects:

    GET {node}/v1/rooms        → the rooms this token holds an EXPLICIT grant in,
                                  each with `your_role`

Why that route and not `/v1/rooms/{id}/study-access`, which looks closer: the
latter goes through the room's door (`authorize` in the server's `ws.py`), and
that door (a) makes the first signed-in caller the OWNER of a room nobody owns
yet, and (b) answers `viewer` to anybody on a public room. Asking it on behalf of
an outsider could have handed the outsider a room. `/v1/rooms` reads the ACL and
nothing else — owner, member, or a group they are in — which is exactly
«explicit access».

## Which room is a study's room

The container says so: `header.room`, the room id on this catalogue's node. A
study that names no room has nobody with a role in it, and its content is
refused to everybody (403, with a sentence saying why) — the catalogue does not
guess a room from a code or a graph id, because a guess that matched the wrong
room would be a grant nobody gave.

## When the doorman cannot answer

No node configured, the node down, slow, or answering something unreadable:
**503, and the door stays shut.** A door that opens when the doorman does not
answer is worse than a closed one.

## The short memory

The answer is kept for `EM_CATALOG_ROLE_TTL` seconds (default 30) per token —
keyed by a hash of the token, never the token itself — so that the reading page,
which fetches its container a moment after the page was served, does not cost a
second round trip. Only answers are remembered; a failure is asked again at the
next request. A grant withdrawn on the node is therefore honoured here within
that many seconds; `EM_CATALOG_ROLE_TTL=0` asks every time.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException

#: how long the node may take before the catalogue gives up and answers 503
DEFAULT_TIMEOUT_SECONDS = 3.0
#: how long an answer about one token's rooms is trusted
DEFAULT_TTL_SECONDS = 30.0
#: entries beyond which the memory is swept (expired first, then all of it)
MAX_ENTRIES = 1024


def node_base() -> str:
    """Where this catalogue DIALS its node — not necessarily where a browser does.

    `EM_CATALOG_NODE_INTERNAL` when set (inside a compose network the node is
    `http://stratigraph-server:8000`, not the public https address); otherwise
    `EM_CATALOG_NODE_URL`, the public address the page already links to, when it
    is an absolute http(s) URL. A relative one names a path for a browser and
    nothing this process can reach.
    """
    internal = (os.environ.get("EM_CATALOG_NODE_INTERNAL") or "").strip()
    if internal:
        return internal.rstrip("/")
    public = (os.environ.get("EM_CATALOG_NODE_URL") or "").strip()
    if public.startswith(("http://", "https://")):
        return public.rstrip("/")
    return ""


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def room_of(header: Optional[Dict[str, Any]]) -> Optional[str]:
    """The room a container names, or None. `room_id` is read as a synonym."""
    if not isinstance(header, dict):
        return None
    value = header.get("room") or header.get("room_id")
    value = str(value).strip() if value else ""
    return value or None


class Doorman:
    """Asks the node which rooms a token holds a role in, and remembers briefly."""

    def __init__(self) -> None:
        self._memory: Dict[str, Tuple[float, Dict[str, str]]] = {}
        self._lock = threading.Lock()
        #: how many times the node was actually asked — for the tests and for
        #: anybody measuring what the memory saves
        self.asked = 0

    def forget(self) -> None:
        with self._lock:
            self._memory.clear()

    def roles(self, token: str) -> Dict[str, str]:
        """`{room_id: role}` for this token, from memory or from the node."""
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ttl = _number("EM_CATALOG_ROLE_TTL", DEFAULT_TTL_SECONDS)
        now = time.monotonic()
        if ttl > 0:
            with self._lock:
                held = self._memory.get(key)
            if held and held[0] > now:
                return held[1]
        answer = self._ask(token)
        if ttl > 0:
            with self._lock:
                if len(self._memory) >= MAX_ENTRIES:
                    for stale in [k for k, (until, _) in self._memory.items()
                                  if until <= now]:
                        del self._memory[stale]
                    if len(self._memory) >= MAX_ENTRIES:
                        self._memory.clear()
                self._memory[key] = (now + ttl, answer)
        return answer

    def _ask(self, token: str) -> Dict[str, str]:
        base = node_base()
        if not base:
            raise HTTPException(
                status_code=503,
                detail="this study is not public, and this catalogue names no "
                       "node to ask who holds a role in it "
                       "(EM_CATALOG_NODE_INTERNAL) — so it stays closed")
        self.asked += 1
        request = urllib.request.Request(
            f"{base}/v1/rooms",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"})
        timeout = _number("EM_CATALOG_NODE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"the node answered {exc.code} when asked which rooms "
                       f"you hold a role in, so this study stays closed") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"the node did not answer when asked which rooms you hold "
                       f"a role in ({exc}), so this study stays closed") from None
        try:
            rooms = json.loads(payload)
            return {str(r["room_id"]): str(r.get("your_role") or "")
                    for r in rooms
                    if isinstance(r, dict) and r.get("room_id")
                    and r.get("your_role")}
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"the node's list of your rooms is unreadable ({exc}), "
                       f"so this study stays closed") from None


doorman = Doorman()

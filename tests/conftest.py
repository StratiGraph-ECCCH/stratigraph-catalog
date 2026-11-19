"""Fixtures shared by the suite: real containers, a fresh store, a fresh index.

The containers are built with s3Dgraphy itself rather than hand-written JSON. A
catalogue whose tests fed it documents nobody's tools produce would prove that
the catalogue works on the catalogue's idea of a study.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# the s3Dgraphy CHECKOUT wins over any installed wheel — `study_metadata` is new
_CHECKOUT = _REPO.parent / "s3Dgraphy" / "src"
if _CHECKOUT.is_dir():
    sys.path.insert(0, str(_CHECKOUT))

from s3dgraphy.container import build_container, container_of   # noqa: E402
from s3dgraphy.graph import Graph                               # noqa: E402
from s3dgraphy.importer.emjson_importer import (                # noqa: E402
    materialize_graph_scope)
from s3dgraphy.nodes import StratigraphicUnit                   # noqa: E402
from s3dgraphy.nodes.hdt_node import HDTNode                    # noqa: E402
from s3dgraphy.nodes.heritage_entity_node import (              # noqa: E402
    HeritageEntityNode)

ORCID = "0000-0002-1825-0097"
#: the room the restricted study names — where a role opens its content
RESTRICTED_ROOM = "scavo-in-corso"


def study_document(graph_id="sarmizegetusa-2026", *, title="Sarmizegetusa 2026",
                   author="Emanuel Demetrescu", orcid=ORCID,
                   license="CC-BY-4.0", visibility="public",
                   hdt=("hdt_sarm", "Sarmizegetusa HDT",
                        "https://example.org/h/sarm"),
                   entity=("hc1_sarm", "Sarmizegetusa Regia"),
                   units=("US 1", "US 2"), site=(45.62, 23.31), embargo=None,
                   room=None):
    """One study, as a container DOCUMENT — the shape a catalogue receives."""
    graph = Graph(graph_id=graph_id)
    graph.name = {"default": title}
    for index, name in enumerate(units, start=1):
        graph.add_node(StratigraphicUnit(f"{graph_id}-us{index}", name=name))
    root = materialize_graph_scope(graph, author=author, license=license,
                                   embargo=embargo,
                                   em_id=graph_id.upper(), orcid=orcid)
    if site:
        root.data["site_position"] = {"lat": site[0], "lon": site[1],
                                      "crs": "EPSG:4326"}
    if hdt:
        graph.add_node(HDTNode(hdt[0], name=hdt[1], heritage_entity_iri=hdt[2]))
    if entity:
        graph.add_node(HeritageEntityNode(entity[0], name=entity[1],
                                          entity_kind="site"))
    container = container_of(graph)
    container.header = {"visibility": visibility, "title": title}
    if room:
        container.header["room"] = room
    return build_container(container)


@pytest.fixture()
def public_study():
    return study_document()


@pytest.fixture()
def restricted_study():
    return study_document(graph_id="scavo-in-corso", title="Scavo in corso",
                          author="Tizia Caia", orcid=None,
                          license="CC-BY-NC-4.0", visibility="restricted",
                          hdt=("hdt_sarm", "Sarmizegetusa HDT",
                               "https://example.org/h/sarm"),
                          entity=None, units=("US 10",), site=None,
                          room=RESTRICTED_ROOM)


@pytest.fixture()
def other_twin_study():
    return study_document(graph_id="colosseo-2024", title="Colosseo 2024",
                          author="Sempronio", orcid=None, license="CC-BY-4.0",
                          visibility="public",
                          hdt=("hdt_colosseo", "Colosseo HDT",
                               "https://example.org/h/colosseo"),
                          entity=("hc1_colosseo", "Colosseo"),
                          units=("US 100",), site=(41.89, 12.49))


@pytest.fixture()
def client(monkeypatch):
    """The service with a store and an index nobody else wrote."""
    from app import main as main_module
    from app.index import SqliteCatalogIndex
    from app.store import InMemoryContainerStore
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main_module, "STORE", InMemoryContainerStore())
    monkeypatch.setattr(main_module, "INDEX", SqliteCatalogIndex(":memory:"))
    # …and a register nobody else wrote either. The alias table is module-level
    # state (it is read on every card, so it cannot take a round trip to the
    # store), which means without this a merge made in one test would still be
    # true in the next one.
    from app import aliases
    aliases.reset()
    with TestClient(main_module.app) as test_client:
        yield test_client


# ── an enforcing realm, shared ───────────────────────────────────────────────
#
# Two modules need it (the visibility tests and the dissemination ones), and a
# fixture two modules need belongs where pytest looks for shared ones.
#
# It mints an RSA key and signs real tokens: everything but the JWKS fetch is
# the production path, so what is measured is the verifier and not a stub of it.

import datetime                                                  # noqa: E402

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from app.auth import OidcSettings, authenticator                 # noqa: E402

ISSUER = "https://keycloak.example/realms/em-dev"
AUDIENCE = "em-catalog"
KID = "test-key-1"


@pytest.fixture()
def realm(monkeypatch):
    """An enforcing authenticator whose signing key is one this test owns."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Keys:
        def key_for(self, kid):
            if kid != KID:
                raise AssertionError(f"unexpected kid {kid!r}")
            return key.public_key()

    previous_settings, previous_jwks = authenticator.settings, authenticator._jwks
    authenticator.settings = OidcSettings(
        issuer=ISSUER, audience=AUDIENCE,
        jwks_uri=f"{ISSUER}/protocol/openid-connect/certs")
    authenticator._jwks = _Keys()

    def token(*, audience=AUDIENCE, issuer=ISSUER, expired=False,
              subject="0000-0002-1825-0097"):
        now = datetime.datetime.now(datetime.timezone.utc)
        claims = {
            "sub": subject, "iss": issuer, "aud": audience,
            "exp": now + datetime.timedelta(minutes=-5 if expired else 30),
            "iat": now - datetime.timedelta(minutes=1),
            "preferred_username": "dev",
        }
        return jwt.encode(claims, key, algorithm="RS256",
                          headers={"kid": KID})

    try:
        yield token
    finally:
        authenticator.settings = previous_settings
        authenticator._jwks = previous_jwks


# ── a node that answers «which rooms do I hold a role in» ────────────────────
#
# A REAL HTTP server on a loopback port, not a patched function: what is
# measured is the catalogue's actual request — URL, header, timeout, parsing —
# against the shape the node's `GET /v1/rooms` returns. Roles are keyed by the
# token's `sub`, which is what the node resolves too (an ORCID).

@pytest.fixture()
def node(monkeypatch):
    import base64
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.doorman import doorman

    grants = {}          # sub → {room_id: role}
    seen = []            # the Authorization headers the node received

    class _Node(BaseHTTPRequestHandler):
        def log_message(self, *args):     # quiet
            pass

        def do_GET(self):
            if self.path != "/v1/rooms":
                self.send_response(404); self.end_headers(); return
            header = self.headers.get("Authorization") or ""
            seen.append(header)
            token = header.partition(" ")[2]
            try:
                body = token.split(".")[1]
                body += "=" * (-len(body) % 4)
                sub = json.loads(base64.urlsafe_b64decode(body))["sub"]
            except Exception:
                self.send_response(401); self.end_headers(); return
            rooms = [{"room_id": r, "title": r, "container_refs": [r],
                      "your_role": role}
                     for r, role in grants.get(sub, {}).items()]
            payload = json.dumps(rooms).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Node)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("EM_CATALOG_NODE_INTERNAL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.delenv("EM_CATALOG_ROLE_TTL", raising=False)
    doorman.forget()

    class _Handle:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        requests = seen

        @staticmethod
        def grant(sub, room, role="viewer"):
            grants.setdefault(sub, {})[room] = role

    try:
        yield _Handle
    finally:
        server.shutdown()
        server.server_close()
        doorman.forget()

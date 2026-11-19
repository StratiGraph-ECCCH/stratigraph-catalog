"""Who may see what — measured against a REAL signature, not a stub.

The tests mint an RSA key, sign tokens with it, and hand the authenticator the
public half in place of the realm's JWKS. Everything else is the production path:
the algorithm allow-list, the audience and issuer checks, the expiry. Faking
`require_token` instead would have tested that the test can return True.

The rule under test is the one the study owns:

* **public** — the dissemination tier. Listable, fetchable, projectable, with no
  token at all: that is what publishing means.
* **anything else** — 401 without a token, 200 with one. Default restricted,
  because the failure directions are not symmetric.
* **writing** is never public, whatever the study says: registering and
  reindexing sit behind the router's dependency.
"""

from __future__ import annotations

import datetime

import pytest

jwt = pytest.importorskip("jwt")
pytest.importorskip("cryptography")

from app import main as main_module          # noqa: E402
from app.auth import OidcSettings, authenticator   # noqa: E402

ISSUER = "https://keycloak.example/realms/em-dev"
AUDIENCE = "em-catalog"
KID = "test-key-1"


def _seed(client, public_study, restricted_study, realm):
    """Both studies registered — which itself needs a token."""
    head = {"Authorization": f"Bearer {realm()}"}
    a = client.post("/catalog/studies", json=public_study, headers=head)
    b = client.post("/catalog/studies", json=restricted_study, headers=head)
    assert a.status_code == 201 and b.status_code == 201, (a.text, b.text)
    return a.json()["id"], b.json()["id"]


# ── reading ──────────────────────────────────────────────────────────────────

def test_an_anonymous_catalogue_shows_the_public_studies_only(
        client, realm, public_study, restricted_study):
    """Not a 401 and not an empty list: discovery is the point of a catalogue,
    and work in progress must not leak while it happens."""
    public_id, restricted_id = _seed(client, public_study, restricted_study, realm)

    anonymous = client.get("/catalog/studies").json()
    assert [s["id"] for s in anonymous["studies"]] == [public_id]

    with_token = client.get("/catalog/studies",
                            headers={"Authorization": f"Bearer {realm()}"}).json()
    assert {s["id"] for s in with_token["studies"]} == {public_id, restricted_id}


def test_a_public_study_is_served_without_a_token(client, realm, public_study,
                                                  restricted_study):
    public_id, _ = _seed(client, public_study, restricted_study, realm)
    for route in ("", "/emjson", "/ttl"):
        if route == "/ttl":
            pytest.importorskip("rdflib")
        answer = client.get(f"/catalog/study/{public_id}{route}")
        assert answer.status_code == 200, (route, answer.text)


def test_a_restricted_study_is_401_without_and_200_with(client, realm, node,
                                                        public_study,
                                                        restricted_study):
    """…200 with a token AND a role in the study's room: the content asks for a
    grant (see `test_role_door.py`), the card for a signature."""
    from conftest import ORCID, RESTRICTED_ROOM
    node.grant(ORCID, RESTRICTED_ROOM, "viewer")
    _, restricted_id = _seed(client, public_study, restricted_study, realm)
    head = {"Authorization": f"Bearer {realm()}"}
    for route in ("", "/emjson", "/ttl", "/open"):
        if route == "/ttl":
            pytest.importorskip("rdflib")
        assert client.get(f"/catalog/study/{restricted_id}{route}"
                          ).status_code == 401, route
        assert client.get(f"/catalog/study/{restricted_id}{route}",
                          headers=head).status_code == 200, route


def test_the_token_may_arrive_in_the_query_because_a_viewer_cannot_set_a_header(
        client, realm, public_study, restricted_study):
    _, restricted_id = _seed(client, public_study, restricted_study, realm)
    answer = client.get(f"/catalog/study/{restricted_id}",
                        params={"token": realm()})
    assert answer.status_code == 200


def test_the_hdt_view_hides_the_restricted_studies_from_an_anonymous_caller(
        client, realm, public_study, restricted_study):
    """Both campaigns share a twin; only one of them is published. The group must
    not advertise the other's existence."""
    public_id, restricted_id = _seed(client, public_study, restricted_study, realm)

    anonymous = client.get("/catalog/hdt/https://example.org/h/sarm").json()
    assert [s["id"] for s in anonymous["studies"]] == [public_id]
    assert anonymous["count"] == 1

    full = client.get("/catalog/hdt/https://example.org/h/sarm",
                      headers={"Authorization": f"Bearer {realm()}"}).json()
    assert full["count"] == 2


# ── writing ──────────────────────────────────────────────────────────────────

def test_writing_is_never_anonymous(client, realm, public_study):
    assert client.post("/catalog/studies", json=public_study).status_code == 401
    assert client.post("/catalog/reindex").status_code == 401
    assert client.delete("/catalog/study/study:whatever").status_code == 401


# ── the token itself ─────────────────────────────────────────────────────────

def test_a_bad_token_does_not_open_a_restricted_study(client, realm,
                                                      public_study,
                                                      restricted_study):
    _, restricted_id = _seed(client, public_study, restricted_study, realm)
    for bad in (realm(expired=True), realm(issuer="https://elsewhere/realms/x"),
                "not-a-jwt"):
        answer = client.get(f"/catalog/study/{restricted_id}",
                            headers={"Authorization": f"Bearer {bad}"})
        assert answer.status_code == 401, answer.text
    wrong_audience = realm(audience="some-other-client")
    answer = client.get(f"/catalog/study/{restricted_id}",
                        headers={"Authorization": f"Bearer {wrong_audience}"})
    assert answer.status_code == 403, \
        "a genuine token issued for another client is a 403, not a 401 — " \
        "re-authenticating would send the caller round a loop"


def test_a_bad_token_still_gets_the_public_catalogue(client, realm,
                                                     public_study,
                                                     restricted_study):
    """An expired session must not read as "the catalogue is down"."""
    public_id, _ = _seed(client, public_study, restricted_study, realm)
    answer = client.get("/catalog/studies",
                        headers={"Authorization": f"Bearer {realm(expired=True)}"})
    assert answer.status_code == 200
    assert [s["id"] for s in answer.json()["studies"]] == [public_id]


def test_health_says_which_mode_it_is_in(client, realm):
    assert client.get("/health").json()["auth"] == "keycloak"

"""The content of a study that is not public asks for a ROLE, not a signature.

E.D., 19 November 2026: «l'embargo non ha impatto su accessibilità: se ti viene
concessa puoi. Se NON hai esplicito accesso non devi poter vedere quei dati nel
bucket o em-json».

Measured the same day on the dev stack, before this file: a person signed in on
the node with no role in any room downloaded the whole em.json of a study under
embargo (200, 2,462 bytes), and its TTL, its reading page and its "open in". The
catalogue asked whether the token was valid; the room asks whether the person
holds a role. These tests hold the second question.

The node is a real HTTP server on a loopback port (`node` in conftest), so the
catalogue's actual request is what is measured.
"""

from __future__ import annotations

import pytest

from conftest import ORCID, RESTRICTED_ROOM, study_document

pytest.importorskip("jwt")

CONTENT = ("/emjson", "/ttl", "/narrative", "/open")
OUTSIDER = "0000-0003-3333-3330"


def _ok_or_unbuilt(answer, route):
    """The reading page is not built in every checkout (501): that is a fact
    about the build, not about the door — but it must never be a 200 or 403
    that the test would misread."""
    return route == "/narrative" and answer.status_code == 501


def _register(client, realm, doc):
    answer = client.post("/catalog/studies", json=doc,
                         headers={"Authorization": f"Bearer {realm()}"})
    assert answer.status_code == 201, answer.text
    return answer.json()["id"]


@pytest.fixture()
def embargoed():
    """Declared public, under an embargo that runs until 2099 — the measured case."""
    return study_document(graph_id="sotto-embargo", title="Sotto embargo",
                          visibility="public", embargo="2099-01-01",
                          room=RESTRICTED_ROOM)


def _get(client, study_id, route, token=None, query=False):
    if token and query:
        return client.get(f"/catalog/study/{study_id}{route}",
                          params={"token": token})
    head = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"/catalog/study/{study_id}{route}", headers=head)


@pytest.mark.parametrize("which", ["restricted", "embargoed"])
def test_content_without_a_role_is_403_with_a_role_200_without_a_token_401(
        client, realm, node, restricted_study, embargoed, which):
    pytest.importorskip("rdflib")
    doc = restricted_study if which == "restricted" else embargoed
    study_id = _register(client, realm, doc)
    node.grant(ORCID, RESTRICTED_ROOM, "viewer")

    member, outsider = realm(), realm(subject=OUTSIDER)
    for route in CONTENT:
        anonymous = _get(client, study_id, route)
        if _ok_or_unbuilt(anonymous, route):
            continue
        assert anonymous.status_code == 401, (route, anonymous.text)

        refused = _get(client, study_id, route, outsider)
        assert refused.status_code == 403, (route, refused.text)
        # the sentence says ACCESS is missing, not that the study is
        assert "access" in refused.json()["detail"]
        assert "no study" not in refused.json()["detail"]
        # …and the token in the query string is no back door
        assert _get(client, study_id, route, outsider,
                    query=True).status_code == 403, route

        assert _get(client, study_id, route, member).status_code == 200, route
        assert _get(client, study_id, route, member,
                    query=True).status_code == 200, route


def test_the_node_was_asked_with_the_persons_own_token(client, realm, node,
                                                       restricted_study):
    study_id = _register(client, realm, restricted_study)
    outsider = realm(subject=OUTSIDER)
    _get(client, study_id, "/emjson", outsider)
    assert node.requests == [f"Bearer {outsider}"]


def test_a_public_study_is_200_for_everybody_and_the_node_is_not_asked(
        client, realm, node, public_study):
    pytest.importorskip("rdflib")
    from app.doorman import doorman

    study_id = _register(client, realm, public_study)
    asked = doorman.asked
    for route in CONTENT:
        for token in (None, realm(), realm(subject=OUTSIDER)):
            answer = _get(client, study_id, route, token)
            if _ok_or_unbuilt(answer, route):
                continue
            assert answer.status_code == 200, (route, token, answer.text)
    assert doorman.asked == asked


def test_the_card_still_only_asks_for_a_signature(client, realm, node,
                                                  restricted_study):
    """A choice, not an omission: the card is what lets somebody find a study
    and ask for access to it. «Quei dati» is the content."""
    study_id = _register(client, realm, restricted_study)
    assert _get(client, study_id, "").status_code == 401
    card = _get(client, study_id, "", realm(subject=OUTSIDER))
    assert card.status_code == 200
    assert card.json()["room"] == RESTRICTED_ROOM


def test_a_study_naming_no_room_opens_to_nobody(client, realm, node):
    """No room, no grant: the catalogue does not guess one from a code."""
    doc = study_document(graph_id="senza-stanza", title="Senza stanza",
                         visibility="restricted")
    study_id = _register(client, realm, doc)
    node.grant(ORCID, "senza-stanza", "owner")     # a same-named room is NOT it
    answer = _get(client, study_id, "/emjson", realm())
    assert answer.status_code == 403
    assert "names no room" in answer.json()["detail"]


@pytest.mark.parametrize("where", ["closed-port", "unset"])
def test_an_unreachable_doorman_is_503_and_never_200(client, realm, node,
                                                     monkeypatch,
                                                     restricted_study, where):
    study_id = _register(client, realm, restricted_study)
    node.grant(ORCID, RESTRICTED_ROOM, "owner")      # would open, if asked
    if where == "closed-port":
        import socket
        with socket.socket() as s:          # a port nobody listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        monkeypatch.setenv("EM_CATALOG_NODE_INTERNAL", f"http://127.0.0.1:{port}")
    else:
        monkeypatch.delenv("EM_CATALOG_NODE_INTERNAL")
        monkeypatch.delenv("EM_CATALOG_NODE_URL", raising=False)
    for route in CONTENT:
        answer = _get(client, study_id, route, realm())
        assert answer.status_code == 503, (route, answer.status_code, answer.text)
        assert "stays closed" in answer.json()["detail"]


def test_a_slow_doorman_is_503(client, realm, node, monkeypatch,
                               restricted_study):
    """A node that accepts the connection and never answers."""
    import socket
    study_id = _register(client, realm, restricted_study)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        monkeypatch.setenv("EM_CATALOG_NODE_INTERNAL",
                           f"http://127.0.0.1:{listener.getsockname()[1]}")
        monkeypatch.setenv("EM_CATALOG_NODE_TIMEOUT", "0.3")
        answer = _get(client, study_id, "/emjson", realm())
        assert answer.status_code == 503, answer.text
    finally:
        listener.close()


def test_the_short_memory_spares_the_second_question_and_forgets(
        client, realm, node, monkeypatch, restricted_study):
    from app.doorman import doorman

    study_id = _register(client, realm, restricted_study)
    node.grant(ORCID, RESTRICTED_ROOM, "viewer")
    token = realm()
    before = doorman.asked
    for route in ("/emjson", "/open", "/emjson"):
        assert _get(client, study_id, route, token).status_code == 200
    assert doorman.asked == before + 1, "one question for three downloads"

    monkeypatch.setenv("EM_CATALOG_ROLE_TTL", "0")
    _get(client, study_id, "/emjson", token)
    _get(client, study_id, "/emjson", token)
    assert doorman.asked == before + 3, "TTL 0 asks every time"

"""The API, measured: register → search → retrieve → open, plus who may see what.

Four claims worth more than the route list:

1. **the container is stored before the index is written**, so the recoverable
   failure is the one that happens;
2. **`/emjson` gives back exactly what went in** — a catalogue that reformatted
   the study on the way out would be editing it;
3. **`/ttl` is publish mode**, so a deleted US does not travel into a published
   projection;
4. **a public study is served without a token and a restricted one is not**, and
   an anonymous list shows the first without leaking the second.
"""

from __future__ import annotations

import json

import pytest

from app import main as main_module

from s3dgraphy import api as em


def _register(client, doc, **params):
    answer = client.post("/catalog/studies", json=doc, params=params)
    assert answer.status_code == 201, answer.text
    return answer.json()


# ── register ─────────────────────────────────────────────────────────────────

def test_registering_a_study_stores_the_container_then_derives_the_card(
        client, public_study):
    body = _register(client, public_study)
    assert body["id"].startswith("study:")
    assert body["created"] is True
    assert body["container_ref"].startswith("studies/")
    assert body["sha256"].startswith("sha256:")

    card = body["card"]
    assert card["title"] == "Sarmizegetusa 2026"
    assert card["authors"] == [{"name": "Emanuel Demetrescu",
                                "orcid": "0000-0002-1825-0097"}]
    assert card["license"] == "CC-BY-4.0"
    assert card["visibility"] == "public"
    assert card["hc2"]["iri"] == "https://example.org/h/sarm"
    assert card["spatial"]["lat"] == 45.62

    # the container really is in the store, and the card really is in the index
    assert main_module.STORE.get(body["id"]) is not None
    assert main_module.INDEX.get(body["id"])["checksum"] == card["checksum"]


def test_the_card_is_derived_and_never_supplied(client, public_study):
    """`visibility` is a property of the STUDY. An endpoint that accepted it as a
    parameter would let a caller publish somebody else's work in progress."""
    body = _register(client, public_study)
    assert body["card"]["visibility"] == "public"

    public_study["header"]["visibility"] = "restricted"
    again = _register(client, public_study, study_id=body["id"])
    assert again["card"]["visibility"] == "restricted", \
        "the container said so — nothing else gets a vote"


def test_a_document_that_is_not_a_container_is_the_callers_problem(client):
    answer = client.post("/catalog/studies", json={"nope": True})
    assert answer.status_code == 400
    assert "em.json" in answer.text


# ── retrieval ────────────────────────────────────────────────────────────────

def test_emjson_gives_back_exactly_what_went_in(client, public_study):
    """Byte-for-byte on the CONTENT: a catalogue that reformatted a study on the
    way out would be editing it, and the checksum it advertised would drift."""
    body = _register(client, public_study)
    answer = client.get(f"/catalog/study/{body['id']}/emjson")
    assert answer.status_code == 200
    returned = json.loads(answer.text)
    assert returned == public_study
    assert em.content_digest(returned) == body["card"]["checksum"]


def test_the_study_card_is_served_from_the_container_when_the_index_is_empty(
        client, public_study):
    """The container is the truth: a gap in a projection must not look like a
    missing study."""
    body = _register(client, public_study)
    main_module.INDEX.remove(body["id"])
    answer = client.get(f"/catalog/study/{body['id']}")
    assert answer.status_code == 200
    assert answer.json()["title"] == "Sarmizegetusa 2026"


def test_ttl_is_served_in_publish_mode(client, public_study):
    """A dissemination surface must not carry tombstones. Here is one, deleted
    through the CRDT, and it must not reach the triples."""
    pytest.importorskip("rdflib")

    graph_id = public_study["active_graph_id"]
    section = public_study["graphs"][graph_id]
    assert em.apply_op(section, em.make_op(
        "remove_node", id=f"{graph_id}-us2",
        ts="2026-08-15T10:00:00+00:00", author="scavatrice"))["applied"]

    body = _register(client, public_study)
    answer = client.get(f"/catalog/study/{body['id']}/ttl")
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"].startswith("text/turtle")
    ttl = answer.text
    assert "removedAt" not in ttl, "a deleted US reached the published triples"
    assert f"{graph_id}-us2" not in ttl
    assert f"{graph_id}-us1" in ttl, "the living are still there"

    # …and the em.json still HAS the tombstone: it is the re-editable truth
    container = client.get(f"/catalog/study/{body['id']}/emjson").json()
    nodes = {n["id"]: n for n in container["graphs"][graph_id]["nodes"]}
    assert "removed" in nodes[f"{graph_id}-us2"]["data"]


def test_a_missing_study_is_a_404(client):
    assert client.get("/catalog/study/study:nope").status_code == 404
    assert client.get("/catalog/study/study:nope/emjson").status_code == 404


# ── search and the two views ─────────────────────────────────────────────────

def test_the_flat_catalogue_lists_and_filters(client, public_study,
                                              other_twin_study):
    _register(client, public_study)
    _register(client, other_twin_study)

    everything = client.get("/catalog/studies").json()
    assert everything["view"] == "flat" and everything["count"] == 2

    by_author = client.get("/catalog/studies", params={"author": "sempronio"}).json()
    assert [s["title"] for s in by_author["studies"]] == ["Colosseo 2024"]

    by_text = client.get("/catalog/studies", params={"q": "colosseo"}).json()
    assert by_text["count"] == 1


def test_the_hdt_view_groups_the_studies_of_one_object(client, public_study,
                                                       restricted_study,
                                                       other_twin_study):
    """Spec §4: one heritage object, its N studies over time."""
    _register(client, public_study)
    _register(client, restricted_study)
    _register(client, other_twin_study)

    grouped = client.get("/catalog/studies", params={"view": "hdt"}).json()
    assert grouped["view"] == "hdt"
    keys = [g["key"] for g in grouped["groups"]]
    assert "https://example.org/h/sarm" in keys
    sarm = next(g for g in grouped["groups"]
                if g["key"] == "https://example.org/h/sarm")
    assert sarm["count"] == 2, "two campaigns, one monument"

    direct = client.get("/catalog/hdt/https://example.org/h/sarm")
    assert direct.status_code == 200, direct.text
    assert direct.json()["count"] == 2
    assert direct.json()["hc1"]["name"] == "Sarmizegetusa Regia"


def test_an_unknown_twin_is_a_404(client, public_study):
    _register(client, public_study)
    assert client.get("/catalog/hdt/iri:nobody").status_code == 404


# ── open in… ─────────────────────────────────────────────────────────────────

def test_open_gives_a_descriptor_with_something_that_works_today(client,
                                                                 public_study):
    body = _register(client, public_study)
    answer = client.get(f"/catalog/study/{body['id']}/open").json()

    assert answer["emjson"].endswith(f"/catalog/study/{body['id']}/emjson")
    assert set(answer["apps"]) == {"emstudio", "blender", "heriverse"}
    for app_name, target in answer["apps"].items():
        assert target["emjson"] == answer["emjson"], \
            f"{app_name} must have something it can act on today"
    assert "scheme" in answer["apps"]["emstudio"]["proposed"], \
        "the custom scheme is a proposal and must SAY so — no handler exists"


def test_open_can_be_asked_for_one_app(client, public_study):
    body = _register(client, public_study)
    answer = client.get(f"/catalog/study/{body['id']}/open",
                        params={"app": "heriverse"}).json()
    assert list(answer["apps"]) == ["heriverse"]
    assert client.get(f"/catalog/study/{body['id']}/open",
                      params={"app": "powerpoint"}).status_code == 400


# ── the index is derived, over HTTP ──────────────────────────────────────────

def test_reindex_rebuilds_the_catalogue_from_the_object_store(client,
                                                              public_study,
                                                              other_twin_study):
    a = _register(client, public_study)
    b = _register(client, other_twin_study)
    before = client.get("/catalog/studies").json()

    main_module.INDEX.reindex([])         # the index is lost
    assert client.get("/catalog/studies").json()["count"] == 0

    rebuilt = client.post("/catalog/reindex").json()
    assert rebuilt["studies"] == 2 and rebuilt["unreadable"] == []
    after = client.get("/catalog/studies").json()
    assert after == before
    assert {a["id"], b["id"]} == {s["id"] for s in after["studies"]}


def test_removing_a_study_removes_both_halves(client, public_study):
    body = _register(client, public_study)
    answer = client.delete(f"/catalog/study/{body['id']}").json()
    assert answer["removed"] == {"container": True, "index": True}
    assert client.get(f"/catalog/study/{body['id']}").status_code == 404
    assert client.delete(f"/catalog/study/{body['id']}").status_code == 404


# ── DP-79 P3 · the study, read as a story ────────────────────────────────────

def _with_narrative(doc, *, blocks=None):
    """The fixture study, plus a narrative pointing at what it contains."""
    graph_id = doc["active_graph_id"]
    section = doc["graphs"][graph_id]
    unit = next(n["id"] for n in section["nodes"] if n["node_type"] == "US")
    section["nodes"].append({
        "id": "narr-1", "node_type": "narrative", "name": "Lo studio, raccontato",
        "data": {"chapters": [{"title": "Un capitolo", "blocks": blocks or [
            {"block_type": "prose", "text": "Il muro è documentato."},
            {"block_type": "embed", "ref": unit, "view_type": "us"},
        ]}]},
    })
    return doc


def test_the_reading_page_is_served_and_is_the_built_bundle(client):
    answer = client.get("/catalog/reader/reader.html")
    if answer.status_code in (404, 501):     # 501: il bundle non è costruito qui (CI)
        import pytest
        pytest.skip("no reading page bundled — run `npm run build:reader` in EMStudio")
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/html")
    # it is the page, not a rendering of a study: it carries no study data
    assert "StratiGraph" in answer.text
    assert "viewer" in answer.text.lower()


def test_the_reading_page_gets_its_assets_from_the_same_mount(client):
    """The reader is a shell plus `assets/`, and a shell whose assets 404 is a
    blank page that reads like an empty study.

    So: read what the shell actually asks for, and ask for it. Serving the shell
    without serving what it names is precisely the failure this route had — it
    was one file when it was written, and it stopped being one."""
    import re

    shell = client.get("/catalog/reader/reader.html")
    if shell.status_code in (404, 501):     # 501: il bundle non è costruito qui (CI)
        import pytest
        pytest.skip("no reading page bundled in this checkout")
    refs = [r for r in re.findall(r'(?:src|href)="([^"]+)"', shell.text)
            if not r.startswith("data:")]
    assert refs, "the shell references its bundle"
    for ref in refs:
        # relative, so the DIRECTORY it was served from decides where they land
        assert ref.startswith("./"), ref
        asset = client.get(f"/catalog/reader/{ref[2:]}")
        assert asset.status_code == 200, f"{ref} → {asset.status_code}"
        assert len(asset.content) > 0


def test_the_old_single_file_address_forwards_instead_of_serving_a_broken_page(
        client):
    """`/catalog/reader.html` is where the page used to be. A shell served from
    there would ask for `/catalog/assets/…` and get nothing — so it forwards."""
    answer = client.get("/catalog/reader.html", follow_redirects=False)
    if answer.status_code == 501:
        import pytest
        pytest.skip("no reading page bundled in this checkout")
    assert answer.status_code == 307
    assert answer.headers["location"] == "/catalog/reader/reader.html"
    # …and the query survives, or a bookmarked reading link loses its study
    with_query = client.get("/catalog/reader.html?emjson=http%3A%2F%2Fx%2Fy",
                            follow_redirects=False)
    assert with_query.headers["location"].endswith(
        "/catalog/reader/reader.html?emjson=http%3A%2F%2Fx%2Fy")


def test_without_a_built_reader_the_answer_is_an_honest_501(client, monkeypatch,
                                                            public_study):
    """The one thing that must not happen is a blank page. A deployment with no
    reader says so, and says what to build."""
    from app import main as main_module

    body = _register(client, _with_narrative(public_study))
    monkeypatch.setattr(main_module, "READER_DIST",
                        main_module.pathlib.Path("/nonexistent/dist"))
    answer = client.get(f"/catalog/study/{body['id']}/narrative")
    assert answer.status_code == 501
    detail = answer.json()["detail"]
    assert "build:reader" in detail and "EM_CATALOG_READER" in detail
    assert client.get("/catalog/health").json()["capabilities"][
        "reading_page"] is False, "…and health says so before anybody tries"
    # the page itself says the same thing, and the ASSETS say 404 — never a 500.
    # Measured, and it was: `check_dir=False` does not stop Starlette from
    # stat'ing the directory on the first request and raising.
    assert client.get("/catalog/reader/reader.html").status_code == 501
    # …and the study is still served: no reader is not no catalogue
    assert client.get(f"/catalog/study/{body['id']}/emjson").status_code == 200


def test_a_dist_that_is_not_there_is_a_404_and_not_a_crash():
    """The mount over a directory nobody built.

    This one is measured rather than assumed, because the flag misleads:
    `check_dir=False` skips the check in the CONSTRUCTOR only — Starlette stats
    the directory again on the first request and raises `RuntimeError`, which
    reaches the client as a 500 with a stack trace. A service whose only fault
    is having no frontend must not look like a broken one."""
    from app.main import READER_MOUNT, _ReaderFiles
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    bare = FastAPI()
    bare.mount(READER_MOUNT, _ReaderFiles(directory="/nonexistent-dist",
                                          check_dir=False, html=False))
    with TestClient(bare) as probe:
        assert probe.get(f"{READER_MOUNT}/assets/reader.js").status_code == 404


def test_a_public_study_is_readable_without_a_token(client, public_study):
    body = _register(client, _with_narrative(public_study))
    answer = client.get(f"/catalog/study/{body['id']}/narrative",
                        follow_redirects=False)
    if answer.status_code == 501:
        import pytest
        pytest.skip("no reading page bundled in this checkout")
    assert answer.status_code == 200, answer.text
    # the page is handed the PUBLIC url of its own container to fetch
    assert "/catalog/reader/reader.html" in answer.text, \
        "the link must be ROOTED and INSIDE the mount — `./reader.html` " \
        "resolves against the study path and 404s (measured in a browser), " \
        "and `/catalog/reader.html` would leave the shell resolving its own " \
        "assets against `/catalog/`"
    assert "emjson" in answer.text
    assert body["id"].replace(":", "%3A") in answer.text or body["id"] in answer.text
    # …and that url is SAME-ORIGIN. An absolute one names a single origin, and
    # the same page is reached through Caddy on https and on the container port
    # directly: measured in Chrome, `http://localhost:8010/…` on an https page
    # is blocked as mixed content and the study never loads.
    assert "http%3A%2F%2F" not in answer.text and "https%3A%2F%2F" not in answer.text
    assert "emjson=%2Fcatalog%2Fstudy%2F" in answer.text


def test_a_restricted_study_is_not_readable_without_one(client, realm,
                                                        public_study,
                                                        restricted_study):
    """The dissemination surface obeys the study, like every other route: a
    study nobody published is not readable just because the reading page is."""
    head = {"Authorization": f"Bearer {realm()}"}
    a = client.post("/catalog/studies", json=_with_narrative(public_study),
                    headers=head)
    b = client.post("/catalog/studies", json=_with_narrative(restricted_study),
                    headers=head)
    assert a.status_code == 201 and b.status_code == 201
    public_id, restricted_id = a.json()["id"], b.json()["id"]

    first = client.get(f"/catalog/study/{public_id}/narrative")
    if first.status_code == 501:
        import pytest
        pytest.skip("no reading page bundled in this checkout")
    assert first.status_code == 200
    assert client.get(f"/catalog/study/{restricted_id}/narrative"
                      ).status_code == 401
    assert client.get(f"/catalog/study/{restricted_id}/narrative",
                      headers=head).status_code == 200


def test_the_reading_page_itself_needs_no_token(client, realm):
    """It is a program, not a study: it carries no data, and what it reads is
    behind the visibility rule rather than in the page."""
    answer = client.get("/catalog/reader/reader.html")
    if answer.status_code in (404, 501):     # 501: il bundle non è costruito qui (CI)
        import pytest
        pytest.skip("no reading page bundled in this checkout")
    assert answer.status_code == 200


# ── LA FACCIA DEL CATALOGO, e il verbo che nessun browser poteva chiamare ────
#
# 7 ottobre 2026. Due cose misurate prima di scrivere una riga:
#
#   `DELETE /catalog/study/{id}`   scritto, testato, in piedi — e chiamabile da
#                                 nessuno con un browser
#   la pagina                      ZERO ancore (chiesto al DOCUMENTO, non al
#                                 testo) e nessun token: leggere era anonimo,
#                                 agire era impossibile
#
# La ragione non era un bottone mancante: la sola superficie che mostra uno
# studio non aveva modo di ESSERE QUALCUNO.


def test_LA_RADICE_DEL_CATALOGO_porta_alla_sua_faccia(client):
    """`/catalog/` → `/catalog/ui/`, deciso dal servizio che la possiede.

    Il nodo pubblica la base pubblica di ogni vicino (`node_services`), e una
    barra costruita da quella lista puntava a `/catalog`, che non rispondeva
    niente. L'alternativa era che ogni consumatore aggiungesse `/ui/`, cioè che
    la disposizione delle pagine di questo servizio diventasse un fatto che
    altre quattro pagine portano addosso.
    """
    answer = client.get("/catalog/", follow_redirects=False)
    assert answer.status_code == 302
    #: RELATIVO: dietro un proxy con un prefisso questo risolve al
    #: `/catalog/ui/` proxato senza che niente qui conosca il prefisso
    assert answer.headers["location"] == "./ui/"
    assert client.get("/catalog/", follow_redirects=True).status_code == 200


def test_E_SENZA_LA_BARRA_ci_pensa_starlette(client):
    """`/catalog` senza barra: `redirect_slashes` la aggiunge da sé.

    Una seconda rotta qui avrebbe risolto `./ui/` da una base diversa —
    `/catalog` + `./ui/` = `/ui/`, che non è una rotta. Misurato.
    """
    answer = client.get("/catalog", follow_redirects=False)
    assert answer.status_code == 307
    assert answer.headers["location"].endswith("/catalog/")


def test_COME_UN_BROWSER_SI_PRESENTA_a_questo_catalogo(client):
    """`/catalog/auth-config` — il gemello dichiarato di quello del nodo.

    Pubblica per costruzione: un issuer e un client id non sono segreti, e la
    cosa che lo sarebbe — un client secret — non esiste per questo client,
    perché la pagina è un client OIDC pubblico e usa PKCE.
    """
    config = client.get("/catalog/auth-config").json()
    assert set(config) == {"issuer", "client_id", "redirect_uri",
                           "authorization_endpoint", "token_endpoint",
                           "end_session_endpoint", "scope", "enforcing"}
    #: NESSUN SEGRETO, e non per fortuna: non c'è nessun campo dove potrebbe
    #: stare, e questa è l'asserzione che lo dice invece di sperarlo
    assert not any("secret" in k for k in config)
    #: in modo sviluppo non c'è realm, e la pagina allora non offre «Accedi»
    #: invece di mandare qualcuno da nessuna parte
    assert config["enforcing"] is False
    assert config["issuer"] == "" and config["client_id"] == ""


def test_DOVE_STA_IL_NODO_lo_dice_il_deployment_e_non_la_pagina(client,
                                                                monkeypatch):
    """`EM_CATALOG_NODE_URL` — il rispecchiamento di `EM_CATALOG_PUBLIC`, che il
    nodo legge già per sapere dove sta QUESTO servizio.

    Fino al 7 ottobre esisteva una direzione sola: il nodo sapeva mandarci, e da
    qui non si tornava. NON un default e non un indovinello: un catalogo
    installato da solo non ha un nodo a cui tornare, e un bottone puntato a
    `localhost` è un bottone che funziona solo dove è stato scritto.
    """
    assert client.get("/catalog/health").json()["node"] == ""
    monkeypatch.setenv("EM_CATALOG_NODE_URL", "https://em.example.org/em/")
    #: …e la barra finale si perde, come ogni altro indirizzo di questo repo
    assert client.get("/catalog/health").json()["node"] == \
        "https://em.example.org/em"

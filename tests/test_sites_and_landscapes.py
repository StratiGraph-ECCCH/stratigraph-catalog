"""Sites, landscapes, and the two things a catalogue must not get wrong about them.

A **site** is one place; a **landscape** is several sites read together. Both are
studies, both are containers, and both hang off one or more HC1 entities — which
is the Landscape Matrix (SITE → HC1 → studies) seen from the catalogue's side.

What is defended here:

* a landscape **lists its sites**, and a reference that resolves to nothing is
  *named* rather than silently dropped — a landscape quietly showing three of
  its four sites is an error that gets discovered in a publication;
* the **HC1 grouping** finds a study under *every* entity it names, not just the
  first, or a landscape would be discoverable under one of its sites and lost
  under the others;
* the **HDT view is not broken** by any of it;
* an **embargo** hides a study from the anonymous list until it expires, and the
  gate is computed from the date at request time — an index written last year
  must not keep a study buried because nobody re-registered it;
* the **default licence** is exposed without being asserted: `license` is what
  the container says (possibly nothing) and `license_effective` is what a reader
  may act on. A licence invented by a reader is a licence nobody granted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import ORCID, study_document                # noqa: E402

from s3dgraphy.study import DEFAULT_LICENSE               # noqa: E402


def _register(client, doc, realm=None):
    headers = {"Authorization": f"Bearer {realm()}"} if realm else {}
    answer = client.post("/catalog/studies", json=doc, headers=headers)
    assert answer.status_code == 201, answer.text
    return answer.json()


def site(graph_id, title, *, entity, hdt=("hdt_x", "Twin", "https://example.org/h/x"),
         visibility="public", license="CC-BY-4.0", **kw):
    """One site study, named by one heritage entity."""
    return study_document(graph_id, title=title, entity=entity, hdt=hdt,
                          visibility=visibility, license=license, **kw)


def landscape(graph_id, title, *, parts, entities, visibility="public"):
    """A landscape: several entities, and references to the studies it is of."""
    doc = study_document(graph_id, title=title, entity=entities[0],
                         visibility=visibility)
    section = doc["graphs"][doc["active_graph_id"]]
    for node_id, name in entities[1:]:
        section["nodes"].append({"id": node_id, "node_type": "heritage_entity",
                                 "name": name, "data": {"entity_kind": "site"}})
    doc["header"]["kind"] = "landscape"
    doc["header"]["composition"] = parts
    return doc


# ── the card the library derives ─────────────────────────────────────────────

def test_a_plain_study_reads_as_a_site_and_a_composed_one_as_a_landscape(client):
    plain = _register(client, site("s1", "Saggio B", entity=("hc1_a", "Villa A")))
    assert client.get(f"/catalog/study/{plain['id']}").json()["kind"] == "site"

    composed = _register(client, landscape(
        "L", "Valle", parts=[plain["id"]], entities=[("hc1_a", "Villa A")]))
    card = client.get(f"/catalog/study/{composed['id']}").json()
    assert card["kind"] == "landscape"
    assert card["composition"][0]["id"] == plain["id"]


def test_the_kind_is_never_derived_from_how_many_graphs_a_container_has(client):
    """A site somebody split into three graphs is still a site. The kind is
    about the subject, the graph count about the working method — coupling them
    would make a rule out of a habit."""
    doc = site("s-multi", "Saggio in tre grafi", entity=("hc1_a", "Villa A"))
    extra = study_document("s-multi-2", title="Secondo grafo", entity=None,
                           hdt=None)
    doc["graphs"].update(extra["graphs"])
    body = _register(client, doc)
    card = client.get(f"/catalog/study/{body['id']}").json()
    assert len(card["graph_ids"]) == 2 and card["kind"] == "site"


def test_a_study_names_every_entity_it_carries_not_only_the_first(client):
    body = _register(client, landscape(
        "L2", "Due ville", parts=[],
        entities=[("hc1_a", "Villa A"), ("hc1_b", "Villa B")]))
    card = client.get(f"/catalog/study/{body['id']}").json()
    assert [e["id"] for e in card["hc1s"]] == ["hc1_a", "hc1_b"]
    # …and the single `hc1` is still there, unchanged, for whoever reads it
    assert card["hc1"]["id"] == "hc1_a"


def test_a_study_with_the_old_single_entity_still_reads(client):
    """Retro-compatibility, measured rather than assumed: `hc1` keeps working
    and `hc1s` is the list of one."""
    body = _register(client, site("s-old", "Vecchio", entity=("hc1_z", "Rudere")))
    card = client.get(f"/catalog/study/{body['id']}").json()
    assert card["hc1"]["id"] == "hc1_z"
    assert [e["id"] for e in card["hc1s"]] == ["hc1_z"]


# ── the licence: exposed, never asserted ─────────────────────────────────────

def test_a_study_without_a_licence_gets_the_default_and_says_it_is_one(client):
    body = _register(client, site("s-nolic", "Senza licenza",
                                  entity=("hc1_a", "Villa A"), license=None))
    card = client.get(f"/catalog/study/{body['id']}").json()
    assert card["license"] is None, "the container declared none, and it says so"
    assert card["license_effective"] == DEFAULT_LICENSE == "CC-BY-SA-4.0"
    assert card["license_is_default"] is True


def test_a_declared_licence_wins(client):
    body = _register(client, site("s-lic", "Con licenza",
                                  entity=("hc1_a", "Villa A"), license="CC-BY-4.0"))
    card = client.get(f"/catalog/study/{body['id']}").json()
    assert (card["license"], card["license_effective"]) == ("CC-BY-4.0", "CC-BY-4.0")
    assert card["license_is_default"] is False


# ── the views ────────────────────────────────────────────────────────────────

def test_a_landscape_lists_its_sites_and_names_what_it_cannot_find(client):
    a = _register(client, site("s-a", "Villa A · 2026", entity=("hc1_a", "Villa A")))
    b = _register(client, site("s-b", "Villa B · 2026", entity=("hc1_b", "Villa B")))
    land = _register(client, landscape(
        "L3", "La valle", parts=[a["id"], b["id"], "study:che-non-esiste"],
        entities=[("hc1_a", "Villa A"), ("hc1_b", "Villa B")]))

    view = client.get(f"/catalog/landscape/{land['id']}").json()
    assert view["kind"] == "landscape"
    assert {s["id"] for s in view["sites"]} == {a["id"], b["id"]}
    assert view["count"] == 2
    assert [m["id"] for m in view["missing"]] == ["study:che-non-esiste"], \
        "a reference that resolves to nothing is REPORTED, never dropped"


def test_a_landscape_may_cite_its_sites_by_em_id(client):
    """Whoever writes the composition has one of the two identities to hand: the
    catalogue's id, or the study's own. Both resolve."""
    a = _register(client, site("s-emid", "Villa A", entity=("hc1_a", "Villa A")))
    em_id = client.get(f"/catalog/study/{a['id']}").json()["em_id"]
    assert em_id
    land = _register(client, landscape("L4", "Valle", parts=[{"em_id": em_id}],
                                       entities=[("hc1_a", "Villa A")]))
    view = client.get(f"/catalog/landscape/{land['id']}").json()
    assert [s["id"] for s in view["sites"]] == [a["id"]]


def test_a_landscape_may_cite_its_sites_by_the_study_code(client):
    """E.D., 11 Oct 2026: the study's code (GT16) replaces the em_id, with the
    same value. A composition citing `code` resolves; so does a card that has
    the code only through its em_id."""
    from s3dgraphy.hdto import declare_hdto
    from s3dgraphy.container import parse_container, build_container
    doc = site("s-code", "Villa C", entity=("hc1_a", "Villa A"))
    container, _ = parse_container(doc)
    declare_hdto(container.active(), study={"name": "Villa C", "code": "VC16"})
    a = _register(client, build_container(container))
    card = client.get(f"/catalog/study/{a['id']}").json()
    assert card["code"] == "VC16"
    old = _register(client, site("s-old", "Villa D", entity=("hc1_a", "Villa A")))
    old_card = client.get(f"/catalog/study/{old['id']}").json()
    land = _register(client, landscape(
        "L5", "Valle", parts=[{"code": "VC16"}, {"code": old_card["em_id"]}],
        entities=[("hc1_a", "Villa A")]))
    view = client.get(f"/catalog/landscape/{land['id']}").json()
    assert [s["id"] for s in view["sites"]] == [a["id"], old["id"]]
    assert view["missing"] == []
    found = client.get("/catalog/studies", params={"q": "vc16"}).json()
    assert a["id"] in [s["id"] for s in found["studies"]]


def test_asking_a_site_for_its_composition_is_an_empty_answer_not_an_error(client):
    a = _register(client, site("s-solo", "Un sito", entity=("hc1_a", "Villa A")))
    view = client.get(f"/catalog/landscape/{a['id']}")
    assert view.status_code == 200
    assert view.json()["sites"] == [] and view.json()["kind"] == "site"


def test_the_hc1_grouping_finds_a_landscape_under_every_entity_it_names(client):
    a = _register(client, site("g-a", "Villa A · 2026", entity=("hc1_a", "Villa A")))
    _register(client, site("g-b", "Villa B · 2026", entity=("hc1_b", "Villa B")))
    land = _register(client, landscape(
        "L5", "La valle", parts=[a["id"]],
        entities=[("hc1_a", "Villa A"), ("hc1_b", "Villa B")]))

    grouped = client.get("/catalog/studies?view=hc1").json()
    by_key = {g["key"]: g for g in grouped["groups"]}
    assert {"hc1_a", "hc1_b"} <= set(by_key)
    for key in ("hc1_a", "hc1_b"):
        assert land["id"] in {s["id"] for s in by_key[key]["studies"]}, \
            f"the landscape must be discoverable under {key}"
        assert "landscape" in by_key[key]["kinds"]

    # …and the per-entity route says the same thing, split by kind
    one = client.get("/catalog/hc1/hc1_b").json()
    assert [s["id"] for s in one["landscapes"]] == [land["id"]]
    assert one["hc1"]["name"] == "Villa B"


def test_the_hdt_view_still_groups_by_twin(client):
    """The new grouping is BESIDE the old one, not instead of it."""
    twin = ("hdt_v", "Valle · HDT", "https://example.org/h/valle")
    _register(client, site("h-1", "1978", entity=("hc1_a", "Villa A"), hdt=twin))
    _register(client, site("h-2", "2026", entity=("hc1_a", "Villa A"), hdt=twin))
    view = client.get(f"/catalog/hdt/{twin[2]}").json()
    assert view["count"] == 2
    grouped = client.get("/catalog/studies?view=hdt").json()
    assert any(g["count"] == 2 for g in grouped["groups"])


def test_kind_is_a_filter(client):
    _register(client, site("k-1", "Sito", entity=("hc1_a", "Villa A")))
    _register(client, landscape("k-2", "Paesaggio", parts=[],
                                entities=[("hc1_a", "Villa A")]))
    assert [s["title"] for s in
            client.get("/catalog/studies?kind=landscape").json()["studies"]] \
        == ["Paesaggio"]
    assert [s["title"] for s in
            client.get("/catalog/studies?kind=site").json()["studies"]] == ["Sito"]


# ── embargo: the same temporal gate the rooms apply ──────────────────────────

def test_an_embargoed_study_is_out_of_the_anonymous_list_until_it_expires(client,
                                                                         realm):
    """The gate is computed from the DATE at request time, not from the flag
    stored at index time: an embargo that has since expired must not keep a
    study buried because nobody re-registered it.

    Runs with an ENFORCING realm on purpose — in dev mode everybody counts as
    authenticated, so an anonymous listing is not a thing that exists there and
    a test written without it would prove nothing.
    """
    head = {"Authorization": f"Bearer {realm()}"}
    hidden = _register(client, study_document(
        "emb", title="Sotto embargo", visibility="public",
        embargo="2099-01-01"), realm)
    shown = _register(client, study_document(
        "emb-old", title="Embargo scaduto", visibility="public",
        embargo="2001-01-01"), realm)

    anonymous = {s["id"] for s in client.get("/catalog/studies").json()["studies"]}
    assert shown["id"] in anonymous
    assert hidden["id"] not in anonymous, "an embargo behaves as restricted"

    with_token = {s["id"] for s in
                  client.get("/catalog/studies", headers=head).json()["studies"]}
    assert hidden["id"] in with_token, "…and is not hidden from those who may see"

    # the study itself, too: refused anonymously, served with a token
    assert client.get(f"/catalog/study/{hidden['id']}").status_code == 401
    assert client.get(f"/catalog/study/{hidden['id']}",
                      headers=head).status_code == 200


def test_an_embargoed_study_is_out_of_the_views_too(client, realm):
    """Every anonymous surface applies the same reading — a study hidden from
    the list and visible in the HDT view would be a leak with extra steps."""
    _register(client, study_document("emb-hdt", title="Sotto embargo",
                                     visibility="public", embargo="2099-01-01",
                                     hdt=("hdt_e", "E", "https://example.org/h/e")),
              realm)
    assert client.get("/catalog/hdt/https://example.org/h/e").status_code == 404
    assert client.get("/catalog/studies?view=hc1").json()["count"] == 0
    # …and with a token it is all there
    head = {"Authorization": f"Bearer {realm()}"}
    assert client.get("/catalog/hdt/https://example.org/h/e",
                      headers=head).status_code == 200

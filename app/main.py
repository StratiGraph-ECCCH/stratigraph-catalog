"""StratiGraph Catalog — the StratiGraph Catalog, as a REFERENCE implementation.

**Read this first: what this service is and what it is not.**

It is the *contract made executable*. The Catalog of the GA (WP6, D6.1) —
PID/DOI minting, FAIR publication, Data Space circulation, the browse UI, the
institutional deployment — is **3DR's** to build on the shared infrastructure.
This service exists so that the contract they build against is something that
RUNS: register a study, search it, fetch it back, open it in an app. It is
deliberately thin, and everything it demonstrates is meant to be reimplemented
against WP6's CouchDB and MinIO without asking us what a study is.

**The one principle, from which everything else follows.**

    A study IS an em.json container. The container is the truth.
    The index is a projection, and can be rebuilt from the containers.

That is why `POST /catalog/studies` stores the container first and indexes
second; why `reindex` re-reads the object store rather than a journal; and why
nothing in this service ever writes a metadata field that a container does not
already carry. The card is derived by **s3Dgraphy** (`api.study_metadata`) — a
catalogue that parsed containers itself would become a second reader, and the
day it disagreed with the library, the index would be a second truth.

**The rules inherited from StratiGraph Server**, because a sibling service that broke them
would teach the wrong thing:

* the domain lives in s3Dgraphy; this is orchestration, an index, and HTTP;
* everything under `/catalog`, so the path is a promise 3DR can hold us to;
* `/health` unversioned as well, because a probe belongs to the infrastructure;
* the durable truth is not on this process's disk (the object store holds it);
* **visibility defaults to restricted** — the failure directions are not
  symmetric: a public study behind a token annoys somebody, an in-progress study
  served openly publishes an interpretation nobody has finished making.

**Two views** (spec §4): the flat catalogue of studies, and the HDT view that
groups the N studies of one heritage object over time — Sarmizegetusa in 1978, in
2013, in 2026, each one a container.

**Retrieval, and why the two formats are not interchangeable** (spec §5): the
em.json is the *usable* truth — it carries the per-field merge clocks, so a study
fetched this way can be re-edited and merged. The TTL is a projection for
publication and SPARQL: isomorphic on content, but it does **not** carry the
field clocks, so a study round-tripped through RDF comes back with node-level
merge granularity. `/emjson` is what you re-open; `/ttl` is what you publish. And
`/ttl` is served in **publish mode**, so tombstones do not travel into a
disseminated projection (`s3dgraphy.dissemination`).
"""

from __future__ import annotations

import os
import pathlib
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Body, FastAPI, HTTPException, Query, Request,
                     Response)
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import aliases
from .auth import AuthDependency, authenticator
from .deeplink import APPS, open_targets
from .deeplink import describe as deeplink_describe
from .index import describe as index_describe
from .index import group_by_hc1, group_by_hdt, hdt_key, index_from_env
from .store import describe as store_describe
from .store import store_from_env
from .twins import DEFAULT_LIMIT, search_twins, sources_from_env, untwinned_count

try:  # the whole point of the service; a clear failure beats a mysterious one
    from s3dgraphy import api as em
except ImportError as exc:  # pragma: no cover — deployment error, not runtime
    raise RuntimeError(
        "StratiGraph Catalog needs s3dgraphy importable: pip install s3dgraphy "
        f"(or -e ../s3Dgraphy). {exc}"
    ) from exc

# THE VERSION, and the convention is not this service's to invent.
#
#     <major EM>.<minor EM>.<the tool's own iteration>
#
# The first two segments declare which Extended Matrix language this build
# speaks; the third is its own history. **A tool cannot be more stable than the
# language it speaks**: while s3Dgraphy is `1.6.0.devN`, so is this.
#
# Measured, not assumed: s3Dgraphy is `1.6.0.dev17`, and EM-blender-tools
# already carries `1.6.0-dev.8` beside an s3dgraphy wheel of `1.6.0.dev16`, so
# the convention existed and was adopted half-way. `0.1.0.dev0` meant this
# service had never been versioned at all.
#
# The third segment is the COORDINATE OF A TEST REPORT: «on dev2 this no longer
# happens» is information, «on the latest version» is not.
__version__ = "1.6.0.dev1"

app = FastAPI(
    title="StratiGraph Catalog",
    version=__version__,
    summary="The StratiGraph Catalog — reference implementation. Studies are "
            "em.json containers; the index is derived.",
    description=__doc__,
)

#: Built at import, so a misconfiguration fails at STARTUP — when an operator is
#: watching — rather than on the first request, when nobody is.
STORE = store_from_env()
INDEX = index_from_env()

# THE ALIASES, read at startup from the object store — the same place the
# containers are, and the reason is in `aliases.py`: `reindex` rebuilds the
# index from the bucket, so a table that lived in the index would be gone at
# the next rebuild, and rebuilding is the ordinary way this service repairs
# itself. A store that cannot answer leaves the table empty and the catalogue
# working, because an unreadable register must not be a service that will not
# start.
try:
    aliases.load(STORE)
except Exception as exc:                      # noqa: BLE001 — reported, not fatal
    print(f"[catalog] twin aliases unavailable at startup: {exc}")

#: Everything that WRITES, or that reads something not published, sits here.
catalog = APIRouter(prefix="/catalog", dependencies=[AuthDependency])

#: …and everything whose access is decided by the STUDY rather than by the route
#: sits here, doing the check itself. A router-level dependency would refuse a
#: public study before the handler could look at it — the same arrangement
#: StratiGraph Server uses for a public IIIF manifest, and for the same reason.
catalog_public = APIRouter(prefix="/catalog")

public = APIRouter()


# ── health ────────────────────────────────────────────────────────────────────

class Health(BaseModel):
    ok: bool = True
    #: WHO answered. Renamed with the repo (2026-08-25). The CouchDB database
    #: name and the OIDC audience did NOT change: those are handles a running
    #: deployment resolves by, and renaming them loses a database.
    service: str = "stratigraph-catalog"
    version: str
    s3dgraphy: Optional[str] = None
    #: what this build can actually do — a client reading this does not have to
    #: discover a 501 by trying
    capabilities: Dict[str, bool] = Field(default_factory=dict)
    auth: str = "dev-no-auth"
    #: WHERE THE TRUTH IS. An operator who reads "memory" knows their studies die
    #: with the process, instead of finding out.
    container_store: str = "memory"
    #: …and which index is answering. It is derivable, so this is a performance
    #: and operations fact rather than a safety one — but it is still a fact.
    index: str = "sqlite"
    studies: int = 0
    #: which apps this deployment can offer "open in" for
    open_in: Dict[str, Optional[str]] = Field(default_factory=dict)
    #: WHERE THE NODE IS, when this deployment says — `EM_CATALOG_NODE_URL`.
    #:
    #: The mirror of `EM_CATALOG_PUBLIC`, which the node already reads to know
    #: where THIS service is. Until 7 October 2026 only one direction existed,
    #: so the node could link to the catalogue and the catalogue could not link
    #: back — measured by asking the two documents for their anchors: the
    #: catalogue's page had zero.
    #:
    #: NOT a default and not a guess. Same discipline as `handoff.CONSUMERS`'s
    #: `web_env` next door: set it and the page offers the way back; leave it
    #: unset and the page offers nothing, because a catalogue deployed on its
    #: own has no node to return to and a button pointing at `localhost` is a
    #: button that works only where it was written.
    node: str = ""


def _health() -> Health:
    def importable(module: str) -> bool:
        import importlib.util
        return importlib.util.find_spec(module) is not None

    version = None
    try:
        import s3dgraphy
        version = getattr(s3dgraphy, "__version__", None)
    except Exception:  # pragma: no cover
        pass
    try:
        count = len(INDEX.search())
    except Exception:  # pragma: no cover — an index that cannot count still lives
        count = 0
    return Health(
        version=__version__,
        s3dgraphy=version,
        capabilities={
            "study_metadata": hasattr(em, "study_metadata"),
            "export_ttl": importable("rdflib"),
            # the publish mode is what keeps a deleted US out of a published
            # projection; a build without it must SAY so rather than serve a
            # /ttl that quietly carries tombstones
            "ttl_publish_mode": importable("s3dgraphy.dissemination"),
            "hdt_view": True,
            # …and the other direction: «which twins do I know», the register a
            # tool asks BEFORE attaching to one. A client can tell a build that
            # has it from one that does not, instead of reading a 404 as «no
            # twins here».
            "twin_registry": True,
            # whether `/study/{id}/narrative` will answer with a page or with an
            # honest 501 — a client can read it here instead of discovering it
            # by trying, and an operator can tell a missing mount from a bug
            "reading_page": _reader_shell() is not None,
        },
        auth=authenticator.settings.describe(),
        container_store=store_describe(STORE),
        index=index_describe(INDEX),
        studies=count,
        open_in=deeplink_describe(),
        node=(os.environ.get("EM_CATALOG_NODE_URL") or "").strip().rstrip("/"),
    )


@public.get("/health", response_model=Health, tags=["meta"])
def health_unversioned() -> Health:
    """The orchestrator's probe. Same answer as `/catalog/health`."""
    return _health()


@catalog_public.get("/health", response_model=Health, tags=["meta"])
def health() -> Health:
    return _health()


# ── helpers ───────────────────────────────────────────────────────────────────

def _mint_study_id() -> str:
    """`study:<uuid>` — the spec's shape (§2.2).

    The catalogue mints it, not the library: an identity is a decision about the
    world, and s3Dgraphy deliberately refuses to invent one (`study_metadata`
    returns `id: None` when nobody has said).
    """
    return f"study:{uuid.uuid4()}"


def _card_of(doc: Dict[str, Any], study_id: str) -> Dict[str, Any]:
    """The card, derived by the LIBRARY. One reader of containers, not two."""
    try:
        return em.study_metadata(doc, study_id=study_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"not a readable em.json container: {exc}") from None


def _public_base(request: Request) -> str:
    """This service's PUBLIC base — one derivation, two callers.

    `EM_CATALOG_PUBLIC_URL` when a deployment names one, otherwise the address the
    caller actually used. It reads the request rather than a header of our
    choosing, which is only safe because uvicorn is told to trust the proxy
    (`--proxy-headers --forwarded-allow-ips *` in the Dockerfile) — without those
    two flags this returns the INTERNAL address and the catalogue hands out links
    that an https page cannot fetch. That is not a hypothesis: it is what
    `EM_CATALOG_PUBLIC_URL: http://localhost:8010` was papering over in the dev
    stack, and what the browser reported as `Failed to fetch`.
    """
    configured = (os.environ.get("EM_CATALOG_PUBLIC_URL") or "").strip()
    return (configured or str(request.base_url)).rstrip("/")


def _container_url(request: Request, study_id: str) -> str:
    """The PUBLIC url of the container — the form the reader can fetch.

    Built from the request, so a deployment behind Caddy answers with the address
    the caller actually used. `EM_CATALOG_PUBLIC_URL` overrides it for the case
    the proxy does not forward the host — configuration, never a query
    parameter: an "open in" answer that a caller could point anywhere is a
    redirector, not a catalogue.
    """
    return f"{_public_base(request)}{_container_path(study_id)}"


def _container_path(study_id: str) -> str:
    """The same container, named RELATIVE to whatever origin asked.

    For the one caller that is already on this origin: the reading page, which
    this service just served. See `read_study` for why naming an absolute origin
    there is a bug and not a style.
    """
    return f"/catalog/study/{study_id}/emjson"


def is_public_now(card: Dict[str, Any]) -> bool:
    """Public AND out of embargo — the one reading of "anybody may see this".

    The embargo is a **temporal** gate over the same question the visibility
    answers, so it belongs in the same sentence: a study published with a date
    in the future behaves as restricted until that date and then goes back to
    what it says it is. StratiGraph Server applies the identical rule at a room's door
    (`app/access.py`), and both call the same library function — a study hidden
    from this list while its room let people in would be worse than either
    behaviour on its own.

    Computed from `card["embargo"]` and not from the stored `embargo_active`
    flag: that flag was true when the study was INDEXED, and an embargo that has
    since expired must not keep a study hidden because nobody re-indexed it.
    """
    if card.get("visibility") != "public":
        return False
    try:
        from s3dgraphy.study import embargo_active
    except ImportError:      # pragma: no cover — the library is a hard dep
        return True
    return not embargo_active(card.get("embargo"))


def _require_visible(card: Dict[str, Any], request: Request,
                     token: Optional[str] = None) -> None:
    """A public study is served to anybody; anything else needs a token.

    Same rule as an StratiGraph Server room, and the same reasoning: `public` is the
    dissemination tier — validated work, meant to be read — and everything else
    is in progress. `?token=` is accepted beside the header because a viewer
    (Mirador, a browser opening a link) cannot set one.

    An **embargo** counts as "anything else" until it expires: see
    :func:`is_public_now`.
    """
    if is_public_now(card):
        return
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer ") or not token:
        authenticator.require_token(request)
        return
    if not authenticator.settings.enforcing:
        return
    authenticator.verify(token.strip())


def _load_or_404(study_id: str) -> Dict[str, Any]:
    doc = STORE.get(study_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no study {study_id!r}")
    return doc


def _card_or_404(study_id: str) -> Dict[str, Any]:
    """The card from the INDEX when it is there, from the CONTAINER when it is
    not — because the container is the truth and a gap in a projection must not
    look like a missing study."""
    card = INDEX.get(study_id)
    if card is not None:
        return card
    doc = STORE.get(study_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"no study {study_id!r}")
    return _card_of(doc, study_id)


# ── register / publish a study ────────────────────────────────────────────────

class Registered(BaseModel):
    id: str
    created: bool
    card: Dict[str, Any]
    #: where the container went — the storage key, for an operator
    container_ref: str
    #: the STORAGE digest of the bytes as written (the card carries the CONTENT
    #: digest, which ignores layout and version: two questions, two answers)
    sha256: str


@catalog.post("/studies", response_model=Registered, tags=["studies"],
              status_code=201)
def register_study(doc: Dict[str, Any] = Body(..., description="an em.json container"),
                   study_id: Optional[str] = Query(
                       default=None,
                       description="update an existing study instead of minting one")
                   ) -> Registered:
    """Register (or replace) a study: **the container goes to the store, then the
    index is derived from it.**

    That order is the whole design. If the process died between the two, the
    study would exist and be unlisted — recoverable by `reindex`. The other
    order would leave the catalogue advertising a study nobody can fetch, which
    no rebuild can repair.

    `visibility` is **not** a parameter: it is read from the container's header,
    where its author put it. A study's openness is a property of the study, and
    an endpoint that let a caller pass it would let a caller publish somebody
    else's work in progress.
    """
    identity = (study_id or "").strip() or _mint_study_id()
    card = _card_of(doc, identity)
    written = STORE.put(identity, doc)
    INDEX.upsert(card)
    return Registered(id=identity, created=bool(written.get("created", True)),
                      card=card, container_ref=str(written.get("key") or ""),
                      sha256=str(written.get("sha256") or ""))


@catalog.delete("/study/{study_id}", tags=["studies"])
def remove_study(study_id: str) -> Dict[str, Any]:
    """Remove a study from the catalogue: the container AND its card.

    Not a tombstone. A catalogue entry is a *statement that a study is
    published*; withdrawing it is withdrawing the statement, and the merge
    machinery that needs tombstones lives in the containers, not here.
    """
    removed_index = INDEX.remove(study_id)
    removed_store = STORE.remove(study_id)
    if not (removed_index or removed_store):
        raise HTTPException(status_code=404, detail=f"no study {study_id!r}")
    return {"ok": True, "id": study_id, "removed": {
        "container": removed_store, "index": removed_index}}


# ── search / list ─────────────────────────────────────────────────────────────

#: SOLO con la barra. Senza, `redirect_slashes` di Starlette manda `/catalog` a
#: `/catalog/` da sé — e una seconda rotta qui avrebbe risolto `./ui/` da una
#: base diversa (`/catalog` → `/ui/`, che non è una rotta). Misurato.
@catalog_public.get("/", include_in_schema=False)
def catalogue_front_door() -> RedirectResponse:
    """`/catalog/` → `/catalog/ui/`. Where this service's FACE is, said by the
    service that owns it.

    Added 2026-10-07 for a reason that is not convenience. The node publishes
    each neighbour's public base (`node_services`, `EM_CATALOG_PUBLIC`), and a
    bar built from that list pointed at `/catalog` — which answered nothing.
    The alternative was for every consumer to append `/ui/`, i.e. for the
    catalogue's page layout to become a fact four other pages carry. So the
    base answers, and where the face lives stays this service's business.

    Relative on purpose: behind a proxy under a prefix this resolves to the
    proxied `/catalog/ui/` without anything here knowing the prefix.
    """
    return RedirectResponse("./ui/", status_code=302)


class CatalogAuthConfig(BaseModel):
    """What a BROWSER needs to sign in against this catalogue's realm.

    **The twin of StratiGraph Server's `/v1/auth-config`, and a declared copy** —
    the same relationship `app/auth.py` already has with that server's, and for
    the same reason: this is a reference implementation that has to be readable
    and deployable on its own.

    Public by construction. An issuer and a client id are not secrets, and the
    one thing that would be — a client secret — does not exist for this client:
    the page is a public OIDC client and uses PKCE.

    Why the catalogue needs one at all: until 7 October 2026 its page held no
    token, «and that is deliberate» — a catalogue's purpose is discovery and an
    anonymous caller must be answered. That stays true for reading. But
    `DELETE /catalog/study/{id}` was written, tested, running, and callable by
    nobody with a browser, because the only surface that shows a study had no
    way to be anybody. Reading stays anonymous; acting needs a name.
    """

    issuer: str = ""
    client_id: str = ""
    redirect_uri: str = ""
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    end_session_endpoint: str = ""
    scope: str = "openid profile email"
    enforcing: bool = False


@catalog_public.get("/auth-config", response_model=CatalogAuthConfig,
                    tags=["meta"])
def auth_config() -> CatalogAuthConfig:
    """How a browser signs in to THIS catalogue. No secret, by construction.

    `EM_CONSOLE_CLIENT_ID` — the same variable the node reads, and the same
    public client (`em-console` by default). Deliberately the same: two names
    for one client in one realm is the classic way a redirect URI ends up
    granted on one and not the other, and the failure shows up at the last step
    of somebody's sign-in.
    """
    settings = authenticator.settings
    issuer = str(getattr(settings, "issuer", "") or "")
    client_id = os.environ.get("EM_CONSOLE_CLIENT_ID", "em-console").strip()
    return CatalogAuthConfig(
        issuer=issuer,
        client_id=client_id if issuer else "",
        redirect_uri=os.environ.get("EM_CATALOG_REDIRECT_URI", "").strip(),
        authorization_endpoint=(f"{issuer}/protocol/openid-connect/auth"
                                if issuer else ""),
        token_endpoint=(f"{issuer}/protocol/openid-connect/token"
                        if issuer else ""),
        end_session_endpoint=(f"{issuer}/protocol/openid-connect/logout"
                              if issuer else ""),
        scope=os.environ.get("EM_CONSOLE_SCOPE", "openid profile email").strip(),
        enforcing=bool(getattr(settings, "enforcing", False)),
    )


@catalog_public.get("/studies", tags=["studies"])
def list_studies(request: Request,
                 q: Optional[str] = Query(default=None, description="free text"),
                 author: Optional[str] = Query(default=None),
                 orcid: Optional[str] = Query(default=None),
                 license: Optional[str] = Query(default=None),
                 hc2: Optional[str] = Query(default=None,
                                            description="Heritage Digital Twin"),
                 hc1: Optional[str] = Query(default=None,
                                            description="Heritage Entity"),
                 kind: Optional[str] = Query(default=None,
                                             description="site | landscape"),
                 view: str = Query(default="flat", pattern="^(flat|hdt|hc1)$"),
                 token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """The catalogue: search, filter, and the two views.

    **What an anonymous caller sees is the PUBLIC studies.** Not an empty list
    and not a 401: a catalogue whose whole purpose is discovery must answer
    somebody who has not logged in, and it must not leak the existence of work in
    progress while doing it. With a token, the caller sees everything.

    This is the one place where the filter is applied *after* the query rather
    than as another `WHERE`: the visibility of a card is a fact of the card, and
    keeping the rule in ONE readable line here beats spreading it through two
    index implementations.
    """
    authenticated = _is_authenticated(request, token)
    cards = INDEX.search(q=q, author=author, orcid=orcid, license=license,
                         hc2=hc2, hc1=hc1, kind=kind)
    if not authenticated:
        cards = [c for c in cards if is_public_now(c)]
    if view == "hdt":
        return {"view": "hdt", "count": len(cards), "groups": group_by_hdt(cards)}
    if view == "hc1":
        # the LANDSCAPE MATRIX view: one entity, every study that names it —
        # and a study may appear under several, which is what distinguishes it
        # from the HDT grouping above
        return {"view": "hc1", "count": len(cards),
                "groups": group_by_hc1(cards)}
    return {"view": "flat", "count": len(cards), "studies": cards}


def _is_authenticated(request: Request, token: Optional[str]) -> bool:
    """Did the caller present something valid? A question, not a gate.

    In dev mode (no OIDC configured) everybody is authenticated, which is what
    "dev mode" means and what `/health` reports in a word.
    """
    if not authenticator.settings.enforcing:
        return True
    header = request.headers.get("authorization") or ""
    try:
        if header.lower().startswith("bearer "):
            authenticator.require_token(request)
            return True
        if token:
            authenticator.verify(token.strip())
            return True
    except HTTPException:
        # A BAD token is not an error here: this route answers anonymously by
        # design, so the caller simply gets the public catalogue. Raising would
        # turn an expired session into "the catalogue is down".
        return False
    return False


@catalog_public.get("/twins", tags=["views"])
def twin_registry(request: Request,
                  q: Optional[str] = Query(default=None,
                                           description="free text: a twin's "
                                                       "name, its entity, or "
                                                       "either identity"),
                  limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=200),
                  token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """**Which twins do I know** — the register, asked before attaching to one.

    The mirror of `/hdt/{hc2}`: that one is «which studies does this twin have»,
    this one is «is there already a twin for what I am digging». They are two
    questions and the second one had no answer, which is how a catalogue full of
    twins still lets a fourth campaign mint a fourth twin for one monument.

    **A suggestion, never a gate.** Nothing in this service requires a study to
    name a twin, and this route is the reason it does not have to: it lets a
    tool OFFER the twins that exist instead of demanding that one be chosen.
    The count of studies that have none (`untwinned`) is in the answer for the
    same reason — the homeless bucket has been a real bucket since the first
    day, and a register that reported it as zero missing links would be lying
    about the ordinary case.

    **Federated in shape, with one source built.** `sources` lists every
    register asked and what it said about itself, and every twin names the one
    it came from; the collaborative cloud is DECLARED as not configured and
    returns nothing. See `app/twins.py` for why the shape comes first.

    Anonymous callers see the twins of the PUBLIC studies, by the same rule and
    the same line as `/studies`.
    """
    authenticated = _is_authenticated(request, token)

    def visible() -> List[Dict[str, Any]]:
        cards = INDEX.search()
        return cards if authenticated else [c for c in cards if is_public_now(c)]

    answer = search_twins(sources_from_env(visible),
                          query=(q or "").strip(), limit=limit)
    answer["untwinned"] = untwinned_count(visible())
    return answer


@catalog_public.get("/hdt/{hc2:path}", tags=["views"])
def hdt_view(hc2: str, request: Request,
             token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """One heritage object, its studies over time (spec §4).

    The path is `:path` because an HC2 key is often an IRI, and an IRI has
    slashes in it. Refusing those would mean the view only worked for the studies
    whose twin happened to be identified by a bare id.
    """
    authenticated = _is_authenticated(request, token)
    # THE MERGED KEY STILL ANSWERS. Not a 404 — a twin that was merged did not
    # stop existing — and not a silent redirect either: somebody who saved the
    # old key deserves to be told what became of it, in the body, rather than
    # to find the right studies under a key they never asked for.
    asked = str(hc2)
    canonical = aliases.canonical_key(asked) or asked
    cards = INDEX.search(hc2=canonical)
    if canonical != asked:
        # The index may still be holding the pre-merge spelling (a rebuild has
        # not necessarily happened yet), so ask under both and let the grouping
        # put them together.
        seen = {c.get("id") for c in cards}
        cards = cards + [c for c in INDEX.search(hc2=asked)
                         if c.get("id") not in seen]
    if not authenticated:
        cards = [c for c in cards if is_public_now(c)]
    if not cards:
        raise HTTPException(status_code=404,
                            detail=f"no studies for the digital twin {hc2!r}")
    groups = group_by_hdt(cards)
    body: Dict[str, Any] = {
        "hc2": canonical, "count": len(cards),
        "hc1": groups[0].get("hc1") if groups else None,
        "studies": cards,
        # what this key absorbed, so a reader can see the group is a union
        "merged_from": aliases.merged_from(canonical),
    }
    if canonical != asked:
        record = aliases.entry(asked) or {}
        body["asked"] = asked
        body["merged_into"] = canonical
        body["merged_by"] = record.get("by")
        body["merged_at"] = record.get("at")
    return body


@catalog_public.get("/hc1/{hc1:path}", tags=["views"])
def hc1_view(hc1: str, request: Request,
             token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """One heritage ENTITY, every study that names it — the Landscape Matrix row.

    Beside the HDT view rather than instead of it, because they answer different
    questions: `/hdt/{hc2}` is one digital twin over time, this is one thing in
    the world however many twins and campaigns it has. A landscape study names
    several entities and therefore appears under each of them.

    `:path` for the same reason as the HDT view: these keys are usually IRIs.
    """
    authenticated = _is_authenticated(request, token)
    cards = INDEX.search(hc1=hc1)
    if not authenticated:
        cards = [c for c in cards if is_public_now(c)]
    if not cards:
        raise HTTPException(status_code=404,
                            detail=f"no studies name the heritage entity {hc1!r}")
    groups = group_by_hc1(cards)
    mine = next((g for g in groups if str(g.get("key")) == hc1), None)
    return {"hc1": (mine or groups[0]).get("hc1"), "key": hc1,
            "count": len(cards),
            "sites": [c for c in cards if c.get("kind") != "landscape"],
            "landscapes": [c for c in cards if c.get("kind") == "landscape"],
            "studies": cards}


@catalog_public.get("/landscape/{study_id}", tags=["views"])
def landscape_view(study_id: str, request: Request,
                   token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """A landscape and the site studies it is made of (spec §4, SITE→HC1→studies).

    The composition is a list of REFERENCES the container carries, so this
    resolves them against the index — by the catalogue's own id first, then by
    the study's code (`code`, or the retired `em_id` with the same value, E.D.
    11 Oct 2026), because whoever wrote the landscape had whichever of the two
    to hand.

    A reference that resolves to nothing is **reported, not dropped**:
    `missing` names it. A landscape quietly listing three of its four sites is
    the kind of error that gets discovered in a publication.

    Works on a study of any kind — asking a site for its composition answers an
    empty one, which is the truthful answer and not an error.
    """
    card = _card_or_404(study_id)
    _require_visible(card, request, token)
    authenticated = _is_authenticated(request, token)

    parts, missing = [], []
    for ref in card.get("composition") or []:
        found = None
        if ref.get("id"):
            found = INDEX.get(str(ref["id"]))
        code = ref.get("code") or ref.get("em_id")
        if found is None and code:
            found = next((c for c in INDEX.search()
                          if code in (c.get("code"), c.get("em_id"))), None)
        if found is None:
            missing.append(ref)
        elif authenticated or is_public_now(found):
            parts.append(found)
        else:
            # it exists and this caller may not see it. Saying "missing" would
            # be a lie about the study; saying nothing would make the landscape
            # look smaller than it is. So: named as restricted, without content.
            missing.append({**ref, "restricted": True})
    return {"landscape": card, "kind": card.get("kind"),
            "count": len(parts), "sites": parts, "missing": missing}


# ── one study ─────────────────────────────────────────────────────────────────

@catalog_public.get("/study/{study_id}", tags=["studies"])
def get_study(study_id: str, request: Request,
              token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """The study's card."""
    card = _card_or_404(study_id)
    _require_visible(card, request, token)
    return card


@catalog_public.get("/study/{study_id}/emjson", tags=["retrieval"])
def get_study_emjson(study_id: str, request: Request,
                     token: Optional[str] = Query(default=None)) -> Response:
    """**The container** — the usable truth (spec §5).

    This, and not the TTL, is what you re-open and re-edit: the container carries
    the per-field merge clocks, so two people who edited different fields of the
    same node still both keep their edit. A study fetched as RDF and converted
    back would come home with node-level granularity, and the difference is
    silent until the day it costs somebody an afternoon's work.
    """
    import json

    card = _card_or_404(study_id)
    _require_visible(card, request, token)
    doc = _load_or_404(study_id)
    return Response(
        content=json.dumps(doc, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="{study_id.replace(":", "_")}.em.json"'})


@catalog_public.get("/study/{study_id}/ttl", tags=["retrieval"],
                    responses={200: {"content": {"text/turtle": {}}}})
def get_study_ttl(study_id: str, request: Request,
                  token: Optional[str] = Query(default=None)) -> Response:
    """The study projected to Turtle, in **publish mode**.

    Publish mode is not a detail: a dissemination surface must not carry
    tombstones (`s3dgraphy.dissemination`), or a US somebody deleted would come
    back to life in whatever consumes these triples. Round-trip mode keeps them
    and lives in the library for the callers that need isomorphism; a catalogue
    is not one of them.

    501 without rdflib — the request was fine, this build simply cannot.
    """
    card = _card_or_404(study_id)
    _require_visible(card, request, token)
    doc = _load_or_404(study_id)

    graphs = doc.get("graphs") or {}
    header = doc.get("header") or {}
    graph_id = doc.get("active_graph_id") or next(iter(graphs), None)
    if not graph_id:
        raise HTTPException(status_code=404,
                            detail=f"study {study_id!r} holds no graph")
    graph, _warnings = em.load_emjson({"header": header,
                                       "graph": graphs[graph_id]})
    try:
        ttl = em.project_ttl(graph, mode="publish")
    except TypeError:
        # a s3dgraphy older than the publish mode: refuse rather than serve a
        # projection that may carry deletions into a published graph
        raise HTTPException(
            status_code=501,
            detail="this build's s3dgraphy has no RDF publish mode, and serving "
                   "a round-trip projection here would publish tombstones"
        ) from None
    except em.MissingDependency as exc:
        raise HTTPException(
            status_code=501,
            detail=f"TTL projection unavailable — this build has no rdflib ({exc})"
        ) from None
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"TTL projection failed: {exc}") from None
    return Response(
        content=ttl, media_type="text/turtle",
        headers={"Content-Disposition":
                 f'attachment; filename="{study_id.replace(":", "_")}.ttl"'})


@catalog_public.get("/study/{study_id}/narrative", tags=["retrieval"],
                    responses={200: {"content": {"text/html": {}}}})
def read_study(study_id: str, request: Request,
               narrative: Optional[str] = Query(
                   default=None,
                   description="which narrative, when the study holds several"),
               token: Optional[str] = Query(default=None)) -> Response:
    """The study, **read as a story** — the dissemination surface (D2.2 T6.3).

    Serves EMStudio's standalone viewer pointed at this study's container. Not a
    second rendering of anything: the page is `reader.html`, which calls the same
    narrative engine the editor calls, so an embed here means what it means
    there — a REFERENCE, resolved now.

    **Live, and that is the point.** The HTML export is a snapshot somebody can
    e-mail; this is the study as it stands, with the 3D navigable and the matrix
    saying what the graph says today. Same NarrativeNode, two renderings.

    The visibility rule is the study's, as everywhere else: a `public` study is
    read without a token — that is what publishing means — and anything else is
    401 without one. The token is passed through to the page in the URL because a
    browser fetching a document cannot set a header; the page uses it for that
    one request and does not keep it.

    501 when this deployment ships no viewer: an honest answer, because the
    alternative is a blank page that looks like the study is empty.
    """
    card = _card_or_404(study_id)
    _require_visible(card, request, token)

    if _reader_shell() is None:
        raise HTTPException(status_code=501, detail=READER_MISSING)

    # The page is handed the address of its own container — SAME-ORIGIN, and
    # that is a correction measured in a browser rather than a preference.
    #
    # The absolute public form (`_container_url`, what `/open` hands to other
    # software) names one origin, and the reader is served from whichever origin
    # the reader was asked for. Reached through Caddy on https, a page told to
    # fetch `http://localhost:8010/…` is blocked as mixed content and shows
    # «Studio irraggiungibile» over a bundle that loaded perfectly — measured in
    # Chrome, and it would have been an asset 404 moved one step along.
    #
    # A root-relative path cannot be wrong: this service SERVED the page, so its
    # own study is at its own origin, whichever one the reader used to get here.
    # Copy the resulting link and it still resolves, because the copied URL
    # carries the origin with it.
    query = f"?emjson={urllib.parse.quote(_container_path(study_id), safe='')}"
    if narrative:
        query += f"&narrative={urllib.parse.quote(narrative, safe='')}"
    if token:
        query += f"&token={urllib.parse.quote(token, safe='')}"
    # A meta-refresh rather than a rewritten bundle: the viewer is a built
    # artefact and this service does not get to edit it. Two lines of HTML, and
    # the browser lands on the page with its parameters.
    #
    # ROOTED, not relative. `./reader.html` resolves against
    # `/catalog/study/<id>/` and asks for a page that is not there — measured in
    # a browser, as a 404 on a route that plainly exists. The prefix is the
    # router's own, so this is correct behind Caddy's `/catalog/*` too.
    #
    # …and it points INSIDE the mount, not beside it (see `READER_MOUNT`). The
    # shell asks for `./assets/…`, so the directory it is served FROM is what
    # decides whether those requests land: `/catalog/reader.html` would resolve
    # them against `/catalog/` and 404 every one.
    target = f"{READER_MOUNT}/reader.html{query}"
    redirect = (
        "<!doctype html><meta charset=\"utf-8\">"
        f"<meta http-equiv=\"refresh\" content=\"0; url={_esc_attr(target)}\">"
        f"<title>{_esc_attr(str(card.get('title') or study_id))}</title>"
        f"<p>Apro lo studio… <a href=\"{_esc_attr(target)}\">continua</a></p>"
    )
    return Response(content=redirect, media_type="text/html; charset=utf-8")


@catalog_public.get("/reader/reader.html", tags=["retrieval"],
                    responses={200: {"content": {"text/html": {}}}})
def reader_shell() -> Response:
    """The reading page itself, at the root of its own directory.

    A route rather than a static file, for one reason: when the reader is not
    built, this says **501 and how to build it** — the mount below would say 404,
    which is what a wrong URL says, and the difference between "you asked for
    something that is not here" and "this deployment does not ship that" is the
    difference between a bug report and a build command.

    Everything the page then asks for (`./assets/…`) IS served by the mount:
    those are files, and 404 is the right word for a missing one.

    Public with no token, deliberately: it is a program, not a study. It carries
    no data — what it reads comes from the `?emjson=` it is handed, and THAT is
    behind the visibility rule.
    """
    shell = _reader_shell()
    if shell is None:
        raise HTTPException(status_code=501, detail=READER_MISSING)
    return FileResponse(shell, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "public, max-age=60"})


@catalog_public.get("/reader.html", tags=["retrieval"], include_in_schema=False)
def reader_page_legacy(request: Request) -> Response:
    """Where the reading page USED to be, back when it was one file.

    It is not one file any more (see `READER_MOUNT`), and a shell served from
    here would ask for `/catalog/assets/…` — nothing is there. So this forwards
    to the real one, query and all, instead of returning a page that renders
    blank and looks like an empty study.

    Public with no token, as before, and deliberately: it is a program, not a
    study. What it reads comes from the `?emjson=` it is given, and THAT is
    behind the visibility rule.
    """
    if _reader_shell() is None:
        raise HTTPException(status_code=501, detail=READER_MISSING)
    query = request.url.query
    return RedirectResponse(f"{READER_MOUNT}/reader.html"
                            + (f"?{query}" if query else ""), status_code=307)


def _esc_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;")


# ── the reading page is a DIRECTORY, not a file ───────────────────────────────
#
# It used to be one self-contained HTML that this service read and returned as a
# string. That stopped being true when the reader gave up `viteSingleFile`: the
# single-file tax is what made the 3D engine +800 kB of inlined base64 instead
# of a chunk fetched only when a model appears, and the reader is SERVED, so it
# never needed to be one file — the editor does, because it opens off a USB
# stick in a trench.
#
# The consequence is this whole section. `dist/reader.html` is a ~1.5 kB shell
# that asks for `./assets/reader-*.{js,css}` (and, when a model appears,
# `./three.module-*.js` beside them), all resolved against the URL DIRECTORY the
# shell was served from. Shell and assets therefore have to be served from the
# same directory — which means serving the dist AS a directory, not reading one
# file out of it.

#: The public prefix the reading page lives under. The shell sits at its root
#: (`…/reader/reader.html`) precisely so `./assets/…` resolves inside the mount.
READER_MOUNT = "/catalog/reader"

#: One sentence for the 501, said the same way wherever the page is missing:
#: what to build, and where to point it.
READER_MISSING = ("this deployment has no reading page bundled: build it with "
                  "`npm run build:reader` in EMStudio and point "
                  "EM_CATALOG_READER at the resulting dist/ directory")


def _reader_dist() -> pathlib.Path:
    """Where the built reader lives — configured, or the sibling checkout.

    `EM_CATALOG_READER` names the **directory** (`…/frontend/dist`). A path that
    ends in `.html` is accepted as well and read as "the shell", for deployments
    configured when this was one file: its parent is the dist, which is the same
    answer — a rename that silently 501'd would be a poor way to say so.

    The sibling checkout is the fallback, which is what makes this work in a
    developer's tree with no build step in this repo.
    """
    configured = (os.environ.get("EM_CATALOG_READER") or "").strip()
    if configured:
        path = pathlib.Path(configured)
        return path.parent if path.suffix == ".html" else path
    here = pathlib.Path(__file__).resolve().parent.parent
    return here.parent / "EMStudio" / "frontend" / "dist"


#: Resolved once, because a mount IS a route and routes are built at startup.
#: What is not cached is the content: `StaticFiles` stats the file on every
#: request, so a reader rebuilt while the service runs is served immediately —
#: the property the old read-per-request had, kept.
READER_DIST = _reader_dist()


def _reader_shell() -> Optional[pathlib.Path]:
    """The shell, or None — asked on every request, never remembered.

    A deployment can be started before the reader is built (the dev stack is,
    routinely), and a remembered `None` would answer 501 for the rest of the day
    to somebody who has since built it.
    """
    shell = READER_DIST / "reader.html"
    try:
        return shell if shell.is_file() else None
    except OSError:  # pragma: no cover — an unreadable mount is still "no page"
        return None


@catalog_public.get("/study/{study_id}/open", tags=["retrieval"])
def open_study(study_id: str, request: Request,
               app: Optional[str] = Query(
                   default=None, description=f"one of {', '.join(APPS)}; "
                                             f"all of them when omitted"),
               token: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """“Open in…” (spec §6): the same container, in whichever app.

    What comes back is a descriptor, not a redirect: the caller is a UI that has
    to decide between a scheme it may not have a handler for and a URL it can
    always fetch, and a 302 would take that decision away and get it wrong.
    """
    card = _card_or_404(study_id)
    _require_visible(card, request, token)
    apps = [app] if app else None
    try:
        return open_targets(study_id, emjson_url=_container_url(request, study_id),
                            apps=apps,
                            # …and the catalogue's own address, so the scheme link
                            # names something a consumer can resolve. Same source
                            # as the container URL — configuration when there is
                            # any, otherwise the request, which is right by
                            # definition: whoever asked can reach what they asked.
                            catalog_base=_public_base(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ── the index is derived, and here is the proof ───────────────────────────────

class Reindexed(BaseModel):
    ok: bool = True
    studies: int
    #: studies whose container is in the store but could not be read into a card
    #: — named, never silently skipped
    unreadable: List[str] = Field(default_factory=list)


def _merge_author(principal: Dict[str, Any]) -> str:
    """Who is doing this, out of the TOKEN and never out of the request body.

    An author a caller could type is an author anybody could type, and the whole
    point of recording one is that the decision can be argued with later.
    """
    # DEV MODE SAYS SO ON THE RECORD. With no OIDC configured `require_token`
    # hands back `{"sub": "anonymous", "em_dev_mode": True}`, and a register that
    # wrote that down as plain "anonymous" would look, three years later, exactly
    # like a merge somebody signed. The whole reason to record an author is that
    # the decision can be argued with; an author who cannot be traced has to be
    # legible AS one.
    if (principal or {}).get("em_dev_mode"):
        return "anonymous@dev-no-auth"
    for claim in ("orcid", "preferred_username", "email", "sub"):
        value = str((principal or {}).get(claim) or "").strip()
        if value:
            return value
    return ""


@catalog.post("/twins/merge", tags=["twins"])
def merge_twins(payload: Dict[str, Any] = Body(...),
                principal: Dict[str, Any] = AuthDependency) -> Dict[str, Any]:
    """Declare that two keys name one twin. **No document is touched.**

    E.D.'s decision of 1 October 2026, and the argument is ownership before it
    is technique: the documents carrying the losing key belong to other people —
    some published, some offline, some finished — so a merge that had to edit
    them could only ever half-happen. See `docs/twin-merge.md`.

    Authenticated, and the decider is recorded: an editorial act on shared
    identity that nobody signed is one nobody can contest afterwards.
    """
    author = _merge_author(principal)
    if not author:
        raise HTTPException(status_code=403,
                            detail="a merge must be signed: no identity in the token")
    try:
        outcome = aliases.merge(STORE,
                                str(payload.get("loser") or ""),
                                str(payload.get("winner") or ""),
                                author=author)
    except aliases.AliasError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {**outcome, "reindex_required": True}


@catalog.post("/twins/unmerge", tags=["twins"])
def unmerge_twins(payload: Dict[str, Any] = Body(...),
                  principal: Dict[str, Any] = AuthDependency) -> Dict[str, Any]:
    """Take a merge back — a FUNCTION, deliberately, and not a hand-edit.

    «An operation whose reversal is a manual DELETE is reversible only in
    theory.» Because a merge changed nothing but the alias table, removing the
    alias restores the register exactly as it was.
    """
    if not _merge_author(principal):
        raise HTTPException(status_code=403,
                            detail="an un-merge must be signed: no identity in the token")
    try:
        outcome = aliases.unmerge(STORE, str(payload.get("loser") or ""))
    except aliases.AliasError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {**outcome, "reindex_required": True}


@catalog_public.get("/twins/aliases", tags=["twins"])
def twin_aliases() -> Dict[str, Any]:
    """Every merge on the register, with who decided and when.

    Public, and that is the point: a merge is a claim about shared identity, so
    who made it has to be readable by the people it affects.
    """
    current = aliases.table()
    return {"count": len(current), "aliases": current}


@catalog.post("/reindex", response_model=Reindexed, tags=["admin"])
def reindex() -> Reindexed:
    """Rebuild the WHOLE index by re-reading the containers in the object store.

    This endpoint is the architectural claim, executable. If it works, the index
    was never a second truth: it can be dropped, corrupted, or replaced by
    another implementation, and the catalogue comes back from the studies.

    A container the library cannot read is REPORTED, not skipped: an index that
    silently lost a study during a rebuild is exactly the failure a rebuild is
    supposed to repair.
    """
    cards: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    for study_id in STORE.list():
        doc = STORE.get(study_id)
        if doc is None:
            unreadable.append(study_id)
            continue
        try:
            cards.append(em.study_metadata(doc, study_id=study_id))
        except Exception:
            unreadable.append(study_id)
    # RE-READ THE ALIASES FIRST. They live in the object store precisely so a
    # rebuild finds them, and the cards are keyed through `hdt_key` → so the
    # table has to be current BEFORE the rows are written, or the rebuilt index
    # would carry pre-merge keys and the merge would appear to have been undone
    # by the very operation that exists to repair things.
    try:
        aliases.load(STORE)
    except Exception as exc:                  # noqa: BLE001
        print(f"[catalog] twin aliases unreadable during reindex: {exc}")
    count = INDEX.reindex(cards)
    return Reindexed(studies=count, unreadable=unreadable)


app.include_router(public)
app.include_router(catalog_public)
app.include_router(catalog)

class _FreshStatic(StaticFiles):
    """`StaticFiles` that makes a browser ASK before reusing what it has.

    Ported from StratiGraph Server, where it was measured rather than reasoned
    about: an edited stylesheet did not reach a browser that already had the page
    open — the mount sends an `ETag` and a `Last-Modified` but no
    `Cache-Control`, so the browser applies HEURISTIC freshness and may serve a
    stale copy without asking. A catalogue is read by people who keep a tab open
    for a week, so it is the same failure with a longer fuse.

    `no-cache` does not mean "do not store": it means "store, but revalidate".
    With the ETag already there that is a 304 and a few bytes, and a device that
    is offline still gets the file out of its own cache, because that is what a
    failed revalidation falls back to.
    """

    async def get_response(self, path: str, scope):        # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


class _ReaderFiles(StaticFiles):
    """`StaticFiles` that treats a missing dist as "not there", not as a fault.

    Measured, because it is not what the flag suggests: `check_dir=False` only
    skips the check in the CONSTRUCTOR. Starlette still stats the directory on
    the first request (`check_config`) and raises `RuntimeError` if it is
    absent — a **500 with a stack trace** for a service that is working
    perfectly and simply has no frontend bundled. Skipping that check leaves the
    ordinary path, which answers 404 per file, cleanly.

    The 501 that actually explains itself lives on the routes above; this is
    only about the assets, and a missing asset IS a 404.
    """

    async def check_config(self) -> None:  # pragma: no cover — trivial override
        return None


# The reading page's assets, served from the directory they were built beside.
#
# `check_dir=False` on purpose as well: the mount is a route, routes are built
# at startup, and the dev stack routinely comes up before anybody has run `npm
# run build:reader`. Refusing to start would let a missing FRONTEND take the
# catalogue down.
#
# `html=False`, also on purpose: with `html=True` a request for the mount root
# would serve `index.html` — and in this dist `index.html` is the EDITOR, 1.9 MB
# of the wrong program under a URL that says "reader".
app.mount(READER_MOUNT,
          _ReaderFiles(directory=str(READER_DIST), check_dir=False, html=False),
          name="reader")


# ── the catalogue's OWN two views, and the identity they wear ────────────────
#
# Spec §4 describes two: the flat list of studies, and the HDT view that groups
# one monument's campaigns over time. Until now both existed only as JSON — the
# data was there and the pages were not. These are the pages, and they are the
# fourth StratiGraph-native face to wear the brand (after the field assistant,
# the room browser and the node console).
#
# Static, no build step, and served by the process that holds the index — the
# same shape as the node console, and for the same reason: a catalogue that
# needed npm to change a label would be a catalogue nobody changes.
#
# The BRAND is vendored (`sync-brand.sh`) and served same-origin, never a CDN:
# see the header of `app/brand/stratigraph-theme.css`.
_BRAND = pathlib.Path(__file__).resolve().parent / "brand"
if _BRAND.is_dir():
    app.mount("/catalog/brand", _FreshStatic(directory=str(_BRAND)),
              name="brand")

_UI = pathlib.Path(__file__).resolve().parent / "ui"
if _UI.is_dir():
    app.mount("/catalog/ui", _FreshStatic(directory=str(_UI), html=True),
              name="catalog-ui")

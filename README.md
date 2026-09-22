# StratiGraph Catalog — the StratiGraph Catalog, **reference implementation**

> **This is not the production Catalog.** The Catalog of the GA (WP6, D6.1) —
> PID/DOI minting, FAIR publication, Data Space circulation, the browse UI, the
> institutional deployment — is **3DR's** to build on the shared infrastructure.
> This service exists so that the *contract* they build against is something
> that RUNS. It is deliberately thin, it runs in the dev-stack, and everything
> it demonstrates is meant to be reimplemented against WP6's CouchDB and MinIO
> without anybody having to ask us what a study is.
>
> Base: `StratiGraph_Catalog_SPEC_per_3DR.md` (CNR → 3DR). Division of labour,
> from §9 of that spec: **CNR provides the contract, 3DR builds and deploys.**

## The principle, and everything that follows from it

    A study IS an em.json container. The container is the truth.
    The index is a projection, and can be rebuilt from the containers.

Which is why:

* `POST /catalog/studies` writes the **container first** and derives the index
  second — the failure that can happen is the recoverable one (a study that
  exists and is unlisted), never the unrecoverable one (a catalogue advertising
  a study nobody can fetch);
* `POST /catalog/reindex` rebuilds the **whole** index by re-reading the object
  store. It is the architectural claim, executable: if it works, the index was
  never a second truth and can be swapped for another implementation with no
  migration;
* no field is ever written that a container does not already carry. The card is
  derived by **s3Dgraphy** (`api.study_metadata`) — a catalogue that parsed
  containers itself would be a second reader, and the day it disagreed with the
  library the index would become a second truth.

Metadata are **not reinvented**: authorship, licence and embargo already live as
graph-scope nodes (DP-65), the HDT pair is HDT-O's vocabulary (HC1/HC2), the
version is the container's own (P3). The catalogue *exposes and indexes* them.

## The two views (spec §4)

* **flat** — search and facets over the studies;
* **HDT** — the N studies of one heritage object over time. Sarmizegetusa in
  1978, in 2013, in 2026: three containers, one Heritage Digital Twin.

## …and the register: which twins do I know

The views above answer *«which studies does this twin have»*. `/catalog/twins`
answers the other direction — *«is there already a twin for what I am digging,
or am I the first?»* — because without it a fourth campaign mints a fourth twin
for one monument and nobody finds out until a publication.

**It suggests; it never gates.** A study with no twin is a citizen: the register
counts them (`untwinned`) instead of treating them as gaps. Somebody who has just
opened a trench does not yet know what they are finding, and a tool that demands
the answer collects a lie.

Each result carries the three facts an attachment is decided on — **which
register** answered, **who is already working on it**, and **how many studies**
hang on it — plus whether it is **provisional** (a twin known only by the id its
own document minted: legitimate, ordinary, and precisely the twin not to attach
yourself to when it is somebody else's).

**Federated in shape, one source built.** The answer always lists every register
asked and what each said about itself; the collaborative cloud appears as
`not_configured` and returns nothing. Nothing is simulated: «nobody has one» and
«I only asked one of two places» are different sentences, and a mock would make
the day the cloud arrives indistinguishable from the day before it. Adding a
second source is a class with `describe`/`search` and one line in
`sources_from_env` (`app/twins.py`).

**Merging two of them is designed and not built** — `docs/twin-merge.md`, which
also says which way of doing it cannot be undone.

## The two formats are NOT interchangeable (spec §5)

| route | what it is | what it is for |
|---|---|---|
| `/catalog/study/{id}/emjson` | **the container** | the *usable* truth: it carries the per-field merge clocks, so a study fetched this way can be re-edited and merged |
| `/catalog/study/{id}/ttl` | the RDF projection, **publish mode** | publication, SPARQL, interop with the KG Engine |

The TTL is isomorphic on content but does **not** carry the field clocks, so a
study round-tripped through RDF comes home with node-level merge granularity.
The difference is silent until the day it costs somebody an afternoon. And
`publish` mode means tombstones do not travel: a deleted US must be **absent**
from a disseminated projection, not marked in it (`s3dgraphy.dissemination`).

## Two renderings of one story — the snapshot and the live view

A narrative's embeds mean *whatever the graph says now*. That is what makes it an
editing surface, and it is exactly what a published text cannot be. So there are
**two** renderings, and the difference between them is the point rather than an
implementation detail:

| | **snapshot** | **live** |
|---|---|---|
| what | `POST /export-narrative?format=html` (via the bridge), or Word / LaTeX | `GET /catalog/study/{id}/narrative` |
| when the embeds resolve | **once**, at export | **at every render** |
| the 3D | a placeholder that names what it stands for | navigable |
| a renamed unit | keeps the old name — it is a snapshot | says the new name |
| what it is for | e-mail, a deliverable, an archive copy, a citation | reading the study as it stands |
| where it lives | a file on somebody's disk | a URL |

Both come from the **same NarrativeNode** and go through the **same rendering
engine** (`narrative.ts` + `narrative-embeds.ts` in EMStudio; the static one via
`s3dgraphy.bake_narrative`). There is deliberately no third traversal of the
graph: three renderings built separately would eventually disagree about what
the narrative said, and the disagreement would surface as somebody citing a
sentence the study no longer makes.

The static file **says it is a snapshot**, in its own footer; the live page says
it is live, in its own. A reader who is handed one of them has no other way to
know which they are looking at.

### How the live page is served — a directory, not a file

The reader used to be one self-contained HTML that this service read and
returned as a string. It is not one file any more: EMStudio dropped
`viteSingleFile` from the reader entry so its 3D engine could be a chunk fetched
when a model appears rather than 800 kB of base64 in every page load. The editor
still is one file — it opens off a USB stick in a trench — but the reader is
*served*, and a served page has somewhere to put its assets.

So `dist/` is mounted whole, at `/catalog/reader/`, with the shell at its root.
The shell asks for `./assets/…`, the browser resolves that against the directory
it came from, and the requests land inside the mount. Three things are coupled,
and moving any one of them alone only relocates the 404:

1. the mount (`app/main.py`, `READER_MOUNT`);
2. the dist reaching the service (`EM_CATALOG_READER`, a volume in the dev stack);
3. the reader built with a **relative** base, so it does not care which prefix
   serves it (`EMStudio/frontend/vite.config.ts`, asserted by
   `scripts/check-narrative.mjs`).

The `?emjson=` handed to that page is **same-origin**, and that is a measured
correction rather than a preference: an absolute URL names one origin, while the
page is reached both through the proxy on https and on the container port
directly — and an https page told to fetch `http://localhost:8010/…` is blocked
as mixed content, showing an empty study over a bundle that loaded perfectly.
The deep links from `/open` stay absolute: those are read by *other* software,
on some other origin.

No dist → **501** on `/study/{id}/narrative`, naming the build command and the
variable. Not a blank page, which would read as an empty study; and not a 500,
which would read as a broken catalogue. `/health` says the same thing up front,
under `capabilities.reading_page`.

## API

```
GET    /health                                  liveness, capabilities, which store/index
GET    /catalog/studies?q=&author=&orcid=&license=&hc2=&hc1=&view=flat|hdt
GET    /catalog/hdt/{hc2}                       one heritage object, its studies
GET    /catalog/twins?q=&limit=                 the REGISTER: which twins exist
GET    /catalog/study/{id}                      the card
GET    /catalog/study/{id}/emjson               the container
GET    /catalog/study/{id}/ttl                  the published projection
GET    /catalog/study/{id}/narrative           the study READ as a story (live)
GET    /catalog/reader/reader.html              the reading page itself (a program, not a study)
GET    /catalog/reader/assets/…                 what that page is built from
GET    /catalog/study/{id}/open?app=emstudio|blender|heriverse
POST   /catalog/studies?study_id=               register / replace          [token]
DELETE /catalog/study/{id}                      withdraw                    [token]
POST   /catalog/reindex                         rebuild from the containers [token]
```

**Visibility.** A study whose header says `visibility: public` is served without
a token — that is what publishing means. Everything else is 401 without and 200
with, and **restricted is the default**: the failure directions are not
symmetric. An anonymous catalogue listing shows the public studies rather than a
401 or an empty list, because discovery is the point and work in progress must
not leak while it happens.

**"Open in…"** returns a *descriptor*, not a redirect, and marks what is
proposed. The `emjson` URL works today in all three apps; the custom scheme
(`emstudio://open?study=…`) is spec §6's example and **no handler is registered
anywhere yet**, so it says so. A button that does nothing on every machine on
earth is worse than an absent one.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -e . -e ../s3Dgraphy
.venv/bin/uvicorn app.main:app --reload --port 8010
curl -s localhost:8010/health | python3 -m json.tool
```

A bare local run answers `"auth": "dev-no-auth"`, `"container_store": "memory"`
and `"index": "sqlite (:memory:)"` — correct for a laptop, and **said out loud**
rather than assumed. A catalogue whose studies live in a process has no studies.

Tests:

```bash
.venv/bin/python -m pytest -q      # 43 passed (42 + 1 skipped without a CouchDB)
```

The one that skips is the CouchDB parity test: it measures that the deploy index
answers *exactly* like the dev one, and it says so by name when there is no
CouchDB to measure against. Start one to run it (the dev-stack `couchdb`
profile).

In the dev-stack, with MinIO, Keycloak and the rest around it:

```bash
cd ../StratiGraph Server/dev-stack
docker-compose --env-file .env.dev -f docker-compose.dev.yml up -d --build StratiGraph Catalog
python smoke_catalog.py            # 30 checks, live
```

The smoke is where the architecture is proved rather than described: it registers
two studies, **opens the bucket itself** to check the containers are really
there, searches, groups by digital twin, fetches the container back byte-for-byte,
checks that `/ttl` hides a deletion the `em.json` still carries, exercises the
visibility rule — and then **empties the index and rebuilds it** from the object
store.

> The container in the dev-stack runs `uvicorn` **without** `--reload`: the code
> is mounted, but a change needs
> `docker-compose --env-file .env.dev -f docker-compose.dev.yml restart StratiGraph Catalog`.

**Where this sits in the wider system:**
[`ARCHITECTURE-SYSTEM.md`](../StratiGraph Server/docs/ARCHITECTURE-SYSTEM.md) ·
**deploying it:** [`DEPLOYMENT.md`](../StratiGraph Server/docs/DEPLOYMENT.md).

## Configuration

| variable | what it does |
|---|---|
| `MINIO_ENDPOINT` / `_ACCESS_KEY` / `_SECRET_KEY` / `_BUCKET` | the container store. **The same variables StratiGraph Server reads** — one bucket, two prefixes. Half of them set is a startup refusal, never a fallback to memory |
| `EM_CATALOG_DB` | the dev index file (SQLite). Absent → `:memory:` |
| `COUCHDB_URL` / `_USER` / `_PASSWORD` / `_DATABASE` | the deploy index. Set → used; unset → SQLite. Never chosen silently |
| `OIDC_ISSUER` (or `TOKEN_ENDPOINT`), `OIDC_AUDIENCE` (or `CLIENT_ID_em`) | Keycloak. Half-configured → the process refuses to start |
| `EM_CATALOG_READER` | the built EMStudio reader — the **`dist/` directory**, shell plus `assets/`. A `…/reader.html` path is accepted too and read as "the shell" (its parent is the dist). Unset → the sibling checkout. Absent → an honest 501 |
| `EM_CATALOG_PUBLIC_URL` | the public base written into "open in" answers, when the proxy does not forward the host |
| `EM_CATALOG_LABEL` | how this catalogue names ITSELF in a federated twin search. Unset → «this catalogue», which is true and useless exactly when something else is federated with it |
| `EM_CATALOG_EMSTUDIO_URL`, `EM_CATALOG_HERIVERSE_URL` | where those apps live, for the deep links |

## The seam for 3DR

`app/index.py` is the interface (`upsert / get / search / list / remove /
reindex`) with **two** implementations: SQLite for the dev machine and
**CouchDB** for the deployment, config-gated exactly like the object store. The
CouchDB documents carry `type: "study"` so they sit beside Heriverse's `scene`
documents in the same database (spec §7) — a new document type, not an inflation
of `sceneController`.

What 3DR reuses is the **card** (`s3dgraphy.api.study_metadata`) and this
**interface**, not our storage.

## L'immagine, e il passo che nessun workflow può fare

L'immagine si pubblica su GHCR **su tag**, con
`.github/workflows/immagine.yml`. Su ogni release:

```
git tag v1.2.3 && git push origin v1.2.3
```

Il workflow costruisce `linux/amd64` e `linux/arm64` (PSNC è amd64, il nodo di
campo è un Raspberry arm64), scrive il **digest** nel sommario del job — che è
l'unico nome che identifica davvero un'immagine, perché un tag si può muovere —
e finisce con una tirata **anonima**.

La versione di s3Dgraphy da installare NON ha un default: il `Dockerfile`
rifiuta di costruire senza, e il workflow fa lo stesso. Va messa una volta in
**Settings → Secrets and variables → Actions → Variables**, con nome
`S3DGRAPHY_VERSION`; oppure si passa a mano lanciando il workflow.

### Rendere PUBBLICO il pacchetto — a mano, una volta sola

**Al primo push GHCR crea il pacchetto PRIVATO.** Il workflow diventa verde, il
push è riuscito davvero, e chi deve specchiare l'immagine non può tirare
niente.

Non si può automatizzare, e non è un'opinione: misurato il 22 settembre 2026
con il token di `gh`, l'API REST non ha nessuna rotta per cambiare la
visibilità di un container package —

```
GET   /orgs/{org}/packages/container/{nome}     → 404 «Package not found.»
                                                   (documentation_url della rotta:
                                                    la rotta ESISTE)
PATCH /orgs/{org}/packages/container/{nome}     → 404 «Not Found»
                                                   (documentation_url generico:
                                                    il verbo non è instradato)
```

cioè la stessa risposta che dà una rotta inventata. Quindi è un passo di E.D.,
e questi sono i clic:

1. la pagina del pacchetto — dal repo, colonna destra, **Packages**, oppure
   `https://github.com/orgs/<org>/packages`
2. **Package settings** (in alto a destra della pagina del pacchetto)
3. in fondo, **Danger Zone** → **Change package visibility** → **Public** →
   si riscrive il nome del pacchetto per confermare
4. e, sempre in Package settings, **Manage Actions access**: il repository che
   pubblica dev'essere elencato con permesso *Write* (il primo push lo aggiunge
   da solo; vale la pena guardarlo)

Poi, dalla propria macchina e senza aver fatto login:

```
./scripts/verifica-tirata-anonima.sh ghcr.io/<org>/<immagine>:v1.2.3
```

che è lo stesso script che il workflow esegue come ultimo passo.

*(I quattro clic sono l'interfaccia di GitHub come è documentata: non li ho
verificati con gli occhi, perché non esiste ancora nessun pacchetto da guardare.
Il resto di questa sezione è misurato.)*

## Out of scope, and deliberately so

PID/DOI minting, FAIR publication, Data Space circulation, expert review, the
browse UI, the institutional deployment — 3DR / production. Also out: the
triplestore (the `/ttl` here is generated on demand via the round-trip; Virtuoso
is WP4/PSNC's), and image-level auth for restricted studies (a decision with its
own trigger).

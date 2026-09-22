# stratigraph-catalog — the StratiGraph Catalog, reference implementation.
#
# Almost stateless: the studies live in the object store and the index is
# derivable from them. The one writable path is the dev index (SQLite), and a
# deployment that points at CouchDB does not need even that.
#
#   docker build -t stratigraph-catalog .
#   docker run --rm -p 8010:8000 stratigraph-catalog
#
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# `[rdf]` is what makes /catalog/study/{id}/ttl a real endpoint instead of an
# honest 501. `[geo]` is deliberately NOT taken: this service never reprojects.
# The s3Dgraphy this image installs: the VERSION from one place, the EXTRAS
# from this service.
#
# `S3DGRAPHY_VERSION` has NO DEFAULT, and that is the whole point rather than an
# omission. A default here would be a second spelling of a number that must agree
# with `dev-stack/.env.dev`, and two spellings of one version are two versions the
# day somebody edits one — which is exactly what happened: this image sat
# on dev12 while the catalogue and the field assistant had drifted to dev16, in a
# stack that shares em.json files and one semantic vocabulary. A build without the
# argument REFUSES, the way `auth.py` refuses a half-configured realm, instead of
# falling back to a pin nobody chose.
#
#   docker build --build-arg S3DGRAPHY_VERSION=<version> -t stratigraph-catalog .
#
# The EXTRAS stay here because they are legitimately this service's own: `[rdf]`
# is what lets a study be served as TTL. A service may choose what it needs; it
# may not move the version by itself.
ARG S3DGRAPHY_VERSION
ARG S3DGRAPHY_EXTRAS="rdf"

WORKDIR /srv/em-catalog

COPY pyproject.toml README.md ./
# PyJWT and minio are here and not behind a build arg, for the reason StratiGraph Server
# states: an image that cannot verify a token comes up open, and an image that
# cannot reach the object store keeps its studies in a process that dies.
RUN set -eu; \
    : "${S3DGRAPHY_VERSION:?required — dev-stack/.env.dev holds it}"; \
    spec="s3dgraphy${S3DGRAPHY_EXTRAS:+[${S3DGRAPHY_EXTRAS}]}==${S3DGRAPHY_VERSION}"; \
    pip install --upgrade pip && \
    pip install "$spec" "fastapi>=0.110" "uvicorn[standard]>=0.27" \
                "PyJWT[crypto]>=2.8" "minio>=7.2"

COPY app ./app

# The licence text travels WITH the software, and not only in the repository.
# Publishing an image IS distributing, which is the act the GPL's obligations
# attach to, so the text has to be inside the thing that gets distributed.
# `/licenses` rather than a path of our own: it is where OpenShift and the Red
# Hat container guidelines look, so a machine can find it too.
COPY LICENSE /licenses/LICENSE


# ── NOT ROOT, AND NOT A NAMED USER EITHER ────────────────────────────────────
#
# `USER emcatalog` was not wrong, it was not ENOUGH, and the gap is a whole class of
# deployment: OpenShift — which is what PSNC runs — IGNORES the name. It assigns
# the pod a RANDOM uid out of the project's range and puts it in group 0 as a
# supplementary group. So the process that starts is a user that owns NOTHING,
# and `/srv/em-catalog-data` (which it must write) was `emcatalog:emcatalog` mode 755.
# The container then either dies at boot or comes up unable to save, which is
# worse because it looks fine.
#
# Two changes, and they are the pattern Red Hat documents for arbitrary-uid
# images:
#
#   · the writable paths belong to GROUP 0 and the group bits equal the user
#     bits (`chown -R <uid>:0` + `chmod -R g=u`). Any uid the orchestrator
#     invents lands in group 0, so it can write them. Note that this is NOT
#     "world-writable": it is one group, the one the platform guarantees.
#   · `USER` is a NUMBER. Kubernetes evaluates `runAsNonRoot` against the UID,
#     and a name is not a uid: the kubelet cannot resolve it from outside the
#     image, so depending on the runtime it either refuses the pod or lets it
#     through unchecked. A number is verifiable.
#
# And `HOME`, which is the one that is invisible until it bites: Docker derives
# `HOME` from `/etc/passwd`, and a uid that is not in there gets `HOME=/`, which
# is not writable. Anything that wants a dot-directory then fails with an error
# about a path nobody configured. So HOME is named here and made group-writable
# like the rest.
#
# The proof is a RUN, not a reading: `docker run --user 12345:0` with a uid that
# does not exist in this image's `/etc/passwd` — see `../stratigraph-server/dev-stack/uid-arbitrario.sh`.
#
# The paragraph this block replaces still holds, and is kept because it explains
# why the empty directory is created at all:
# Not root. /srv/em-catalog-data exists in the image so a named volume mounted
# there is not created root-owned — the same trap StratiGraph Server documents, and the
# SQLite index is exactly the file that would fail to be written.
ARG APP_UID=10001
RUN useradd --uid ${APP_UID} --gid 0 --create-home --shell /usr/sbin/nologin emcatalog && \
    mkdir -p /srv/em-catalog-data && \
    chown -R ${APP_UID}:0 /srv/em-catalog /srv/em-catalog-data /home/emcatalog && \
    chmod -R g=u /srv/em-catalog /srv/em-catalog-data /home/emcatalog
ENV HOME=/home/emcatalog
USER ${APP_UID}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# `--proxy-headers`, and it is load-bearing rather than hygiene: this service is
# the one that WRITES its own address into an answer (`_container_url`, the
# `emjson` of every "open in…"), and it derives it from the request. Without
# these two flags Starlette sees the INTERNAL request — `http://…:8000` — and the
# catalogue hands out a link that an https page cannot fetch: measured in Chrome
# as `Failed to fetch`, blocked as mixed content, on a study that was perfectly
# fine.
#
# That is what `EM_CATALOG_PUBLIC_URL` was papering over. The env var stays as an
# override for a proxy that does not forward, but it is no longer needed to be
# CORRECT, and it is not set in the dev stack any more.
#
# `forwarded-allow-ips=*` because the proxy's address inside a container network
# is assigned by the network, not by us. It is safe exactly to the extent that
# nothing but the proxy can reach this port — which is the arrangement in both
# compose files (no published port on the internal network in production) and is
# the same assumption every reverse-proxied app makes.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",      "--proxy-headers", "--forwarded-allow-ips", "*"]

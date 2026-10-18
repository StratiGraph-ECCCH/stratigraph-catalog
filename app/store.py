"""Where the studies actually live — and the reason the index can be thrown away.

**A study is one em.json container, and the container is the TRUTH.** Everything
else this service holds is a projection of it: the index (`index.py`) is derived,
the TTL is generated on demand, the cards are read at write time. That is not
tidiness, it is the property that keeps a catalogue honest — if the index and the
containers ever disagree, the containers win and `reindex` proves it by rebuilding
the index from them.

Same shape as StratiGraph Server's `assets.py`, and deliberately so: an interface of four
methods, an in-memory implementation that says it is for tests, and an S3
object store (RustFS in the dev stack) as the deployment target — a line of configuration, not a rewrite.

**Not content-addressed, and this is the one real difference from an asset.** An
asset is immutable bytes and its name IS its digest; a *study* evolves under a
stable identity — that is what makes "the 1978 study and the 2026 study of the
same monument" a sentence the HDT view can say. So the key is the study's id, and
the digest travels in the metadata as the answer to "is the copy you have still
the copy I indexed" rather than as the name. Immutable citation is a different
job, done by pinning a version (`container.pin_version`), and pinning is
deliberately not the same act as saving.

The bucket is the one StratiGraph Server already writes assets into; studies live under
their own **prefix** (spec §7: same store, shared prefixes). A catalogue that
demanded a bucket of its own would be one more thing to provision for no gain.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, Iterable, List, Optional, Protocol

#: Where study containers sit inside the shared bucket.
STUDY_PREFIX = "studies/"
#: The suffix, so a human browsing the bucket can see what these objects are.
STUDY_SUFFIX = ".em.json"

#: Where the register's OWN facts live — things that are not studies and must
#: never be read back as one. Beside the containers because they have to survive
#: the same things the containers survive: `reindex` rebuilds the index from the
#: bucket, so anything that lives only in the index is gone at the next rebuild,
#: and `reindex` is not an emergency procedure — it is the ordinary way this
#: service repairs itself.
REGISTRY_PREFIX = "registry/"

MEDIA_TYPE = "application/json"


def container_bytes(doc: Dict[str, Any]) -> bytes:
    """The canonical bytes of a container.

    Sorted keys, no ASCII escaping: the same document must serialise the same way
    every time, or the stored digest would depend on who wrote it. (This is the
    STORAGE digest — bytes on the wire. The study's CONTENT digest, which ignores
    layout and version, is s3Dgraphy's `content_digest` and is a different
    question with a different answer.)
    """
    return json.dumps(doc, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def object_key(study_id: str) -> str:
    """`study:abc-123` → `studies/study_abc-123.em.json`.

    The colon is legal in an S3 key and illegal in half the tools people use to
    look at one, so it is replaced. The mapping is total and reversible enough
    for listing, which is all `reindex` needs.
    """
    safe = str(study_id).replace(":", "_").replace("/", "_")
    return f"{STUDY_PREFIX}{safe}{STUDY_SUFFIX}"


def study_id_from_key(key: str) -> str:
    """The inverse, for `list()` — a catalogue rebuilding itself from the bucket
    must be able to name what it found."""
    name = key[len(STUDY_PREFIX):] if key.startswith(STUDY_PREFIX) else key
    if name.endswith(STUDY_SUFFIX):
        name = name[: -len(STUDY_SUFFIX)]
    return name.replace("_", ":", 1) if name.startswith("study_") else name


class ContainerStore(Protocol):
    """Put a study, get a study, list them, remove one. Nothing else lives here."""

    def put(self, study_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Store the container; return `{key, sha256, size, created}`."""

    def get(self, study_id: str) -> Optional[Dict[str, Any]]:
        """The container, or None."""

    def list(self) -> List[str]:
        """Every study id in the store — the input to a rebuild."""

    def remove(self, study_id: str) -> bool:
        """True when something was there to remove."""

    def get_blob(self, key: str) -> Optional[bytes]:
        """A raw object by its FULL key — for the register's own facts.

        Deliberately separate from `get`: that one takes a study id and knows
        where studies live. This one is for objects that are not studies and
        must never be listed as one.
        """

    def put_blob(self, key: str, data: bytes) -> Dict[str, Any]:
        """Write a raw object; return `{key, sha256, size}`."""

    def remove_blob(self, key: str) -> bool:
        """True when something was there to remove."""


class InMemoryContainerStore:
    """For tests and a laptop run — and it says so.

    Not the deployment target: it dies with the process, which is the property
    the S3 implementation exists to remove. A catalogue whose truth lives here
    has no truth.
    """

    def __init__(self) -> None:
        self._blobs: Dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, study_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        data = container_bytes(doc)
        key = object_key(study_id)
        with self._lock:
            created = key not in self._blobs
            self._blobs[key] = data
        return {"key": key, "sha256": bytes_digest(data), "size": len(data),
                "created": created}

    def get(self, study_id: str) -> Optional[Dict[str, Any]]:
        data = self._blobs.get(object_key(study_id))
        return json.loads(data.decode("utf-8")) if data is not None else None

    def list(self) -> List[str]:
        # FILTERED, as the S3 one's already was. Without this the two
        # implementations disagree the moment anything that is not a study is
        # written to the store: S3 lists `prefix=studies/` and this one listed everything,
        # so a register file would come back as a study id and `reindex` would
        # try to parse it. Same bucket, same answer.
        return sorted(study_id_from_key(k) for k in self._blobs
                      if k.startswith(STUDY_PREFIX) and k.endswith(STUDY_SUFFIX))

    def remove(self, study_id: str) -> bool:
        with self._lock:
            return self._blobs.pop(object_key(study_id), None) is not None

    def get_blob(self, key: str) -> Optional[bytes]:
        return self._blobs.get(str(key))

    def put_blob(self, key: str, data: bytes) -> Dict[str, Any]:
        with self._lock:
            self._blobs[str(key)] = bytes(data)
        return {"key": str(key), "sha256": bytes_digest(data), "size": len(data)}

    def remove_blob(self, key: str) -> bool:
        with self._lock:
            return self._blobs.pop(str(key), None) is not None


class S3ContainerStore:
    """The deployment target: the studies live in the object store.

    Any S3-compatible store — RustFS in the dev stack — reached with `boto3`
    through `s3client.make_client` (path-style, SigV4, no proxy). `boto3` is a
    hard dependency of this service (see pyproject), so a missing one is a
    broken build rather than a configuration choice — but the construction
    still fails **here**, with a sentence, rather than at the first upload with
    a stack trace from inside a request.
    """

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str, secure: bool = True) -> None:
        from . import s3client

        try:
            self._client = s3client.make_client(endpoint, access_key,
                                                secret_key, secure)
        except ImportError as exc:  # pragma: no cover — depends on the build
            raise RuntimeError(
                "the S3 container store needs the `boto3` client, which this "
                "build does not have: pip install boto3") from exc
        self.bucket = bucket
        self.endpoint = endpoint
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        from . import s3client

        ClientError = s3client.client_error()
        try:
            s3client.ensure_bucket(self._client, self.bucket)
        except ClientError as exc:
            raise RuntimeError(
                f"the bucket '{self.bucket}' is not usable at {self.endpoint}: "
                f"{s3client.error_code(exc)}. Either it does not exist and these "
                f"credentials may not create it, or they may not read it") from exc
        except Exception as exc:      # network, DNS, TLS: not an S3 answer at all
            raise RuntimeError(
                f"the object store at {self.endpoint} did not answer: {exc}. "
                f"StratiGraph Catalog will not start without the store that holds its "
                f"truth") from exc

    def _exists(self, key: str) -> bool:
        from . import s3client

        return s3client.exists(self._client, self.bucket, key)

    def put(self, study_id: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        data = container_bytes(doc)
        key = object_key(study_id)
        created = not self._exists(key)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                ContentType=MEDIA_TYPE)
        return {"key": key, "sha256": bytes_digest(data), "size": len(data),
                "created": created}

    def get(self, study_id: str) -> Optional[Dict[str, Any]]:
        data = self.get_blob(object_key(study_id))
        return json.loads(data.decode("utf-8")) if data is not None else None

    def list(self) -> List[str]:
        from . import s3client

        found: Iterable[Dict[str, Any]] = s3client.iter_objects(
            self._client, self.bucket, prefix=STUDY_PREFIX)
        return sorted(study_id_from_key(obj["key"]) for obj in found
                      if obj["key"].endswith(STUDY_SUFFIX))

    def remove(self, study_id: str) -> bool:
        return self.remove_blob(object_key(study_id))

    def get_blob(self, key: str) -> Optional[bytes]:
        from . import s3client

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=str(key))
        except Exception as exc:                   # noqa: BLE001 — re-raised
            if s3client.is_not_found(exc):
                return None
            raise
        return s3client.read_body(response)

    def put_blob(self, key: str, data: bytes) -> Dict[str, Any]:
        self._client.put_object(Bucket=self.bucket, Key=str(key),
                                Body=bytes(data), ContentType=MEDIA_TYPE)
        return {"key": str(key), "sha256": bytes_digest(data), "size": len(data)}

    def remove_blob(self, key: str) -> bool:
        # S3's DeleteObject answers 204 whether or not the key was there, so the
        # «was something there» half of the contract is a HeadObject first.
        if not self._exists(str(key)):
            return False
        self._client.delete_object(Bucket=self.bucket, Key=str(key))
        return True


#: The names StratiGraph Server reads, and ONLY those — one setting with one
#: name. Reading the SAME variables is what makes "the catalogue and the rooms
#: share a bucket" true in configuration and not only in prose. The scheme of
#: the endpoint decides TLS (`http://` → plain), so there is no `_SECURE` here.
_S3_KEYS = {
    "endpoint": "EM_ASSET_S3_ENDPOINT",
    "access_key": "EM_ASSET_S3_ACCESS_KEY",
    "secret_key": "EM_ASSET_S3_SECRET_KEY",
    "bucket": "EM_ASSET_S3_BUCKET",
}

def _s3_settings(env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The S3 configuration, or None when there is none at all.

    Half of it is an error, not a fallback: a deployment that named an endpoint
    and forgot the bucket must hear about it, because the alternative is a
    catalogue that came up storing its studies in RAM.
    """
    found: Dict[str, Any] = {}
    for field, name in _S3_KEYS.items():
        value = (env.get(name) or "").strip()
        if value:
            found[field] = value
    if not found:
        return None
    missing = [f for f in _S3_KEYS if f not in found]
    if missing:
        raise RuntimeError(
            f"the object store is half-configured: missing {', '.join(missing)}. "
            f"Refusing to fall back to memory — a catalogue whose studies "
            f"disappear with the process is worse than one that will not boot.")
    found["secure"] = not found["endpoint"].startswith("http://")
    return found


def store_from_env(environ: Optional[Dict[str, str]] = None) -> ContainerStore:
    """The S3 store when configured; memory otherwise, loudly."""
    env = environ if environ is not None else os.environ
    settings = _s3_settings(dict(env))
    if settings:
        return S3ContainerStore(**settings)
    return InMemoryContainerStore()


def describe(store: Any) -> str:
    """For `/health`. An operator who reads "memory" knows their studies die with
    the process, instead of finding out."""
    if isinstance(store, S3ContainerStore):
        return f"s3 ({store.endpoint}, bucket {store.bucket}, prefix {STUDY_PREFIX})"
    return "memory"

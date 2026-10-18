"""`S3ContainerStore` against a REAL S3 endpoint — the deployment store, exercised.

Not a fake of our own: what can go wrong in this class is the S3 client's own
behaviour against a server (how a missing key is reported, whether a delete of
nothing says so, what a listing under a prefix returns), and a fake would agree
with whatever we assumed. So these tests talk to an S3 server: the RustFS of
the dev stack, or any S3-compatible endpoint you point them at.

    # the dev stack's object store up (stratigraph-server/dev-stack), then:
    pytest tests/test_store_s3.py
    # …or anywhere else:
    EM_TEST_S3_ENDPOINT=http://host:9000 \\
    EM_TEST_S3_ACCESS_KEY=… EM_TEST_S3_SECRET_KEY=… pytest tests/test_store_s3.py

`EM_TEST_S3_*`, as in StratiGraph Server's `tests/test_assets_s3.py`, and NOT
the service's own `EM_ASSET_S3_*`, which would configure the store `app.main`
builds at import. Without them the keys are read from the dev stack's
`.env.dev` (`../stratigraph-server/dev-stack/.env.dev`, `RUSTFS_ACCESS_KEY` /
`RUSTFS_SECRET_KEY`). There is **no default key**: a test that falls back to a
well-known vendor password measures a store nobody should be running.

They **skip** — with the reason — when the credentials are not given, when
nothing answers at the endpoint, or when `boto3` is not installed. A skip here
means "not measured", never "passed". Every test works in a throwaway bucket of
its own, removed afterwards: the shared bucket is never written.
"""

from __future__ import annotations

import os
import pathlib
import socket
import uuid
from urllib.parse import urlsplit

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_ENV_DEV = _REPO.parent / "stratigraph-server" / "dev-stack" / ".env.dev"


def _env_dev() -> dict:
    """The dev stack's `.env.dev` as KEY=value, or {} — it is gitignored, and
    absent on a machine without the stack."""
    out = {}
    try:
        text = _ENV_DEV.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            out[key.strip()] = value.strip()
    return out


_DEV = _env_dev()
ENDPOINT = (os.environ.get("EM_TEST_S3_ENDPOINT")
            or f"http://localhost:{_DEV.get('RUSTFS_API_PORT') or '9000'}")
ACCESS_KEY = (os.environ.get("EM_TEST_S3_ACCESS_KEY")
              or _DEV.get("RUSTFS_ACCESS_KEY") or "")
SECRET_KEY = (os.environ.get("EM_TEST_S3_SECRET_KEY")
              or _DEV.get("RUSTFS_SECRET_KEY") or "")
SECURE = ENDPOINT.startswith("https://")

pytest.importorskip("boto3", reason="the `boto3` client is not installed")

if not (ACCESS_KEY and SECRET_KEY):
    pytest.skip("no S3 keys: neither EM_TEST_S3_ACCESS_KEY / _SECRET_KEY nor "
                "RUSTFS_* in the dev stack's .env.dev — there is no default key "
                "on purpose", allow_module_level=True)


def _reachable() -> bool:
    parts = urlsplit(ENDPOINT if "://" in ENDPOINT else f"http://{ENDPOINT}")
    try:
        with socket.create_connection(
                (parts.hostname, parts.port or (443 if SECURE else 80)),
                timeout=1.5):
            return True
    except OSError:
        return False


if not _reachable():
    pytest.skip(f"nothing answers at {ENDPOINT} — start the dev stack's object "
                f"store to measure this", allow_module_level=True)


from app import s3client  # noqa: E402
from app.store import S3ContainerStore, describe, object_key  # noqa: E402


@pytest.fixture()
def store():
    """A store on a throwaway bucket of its own, emptied and removed afterwards."""
    bucket = f"em-test-catalog-{uuid.uuid4().hex[:10]}"
    store = S3ContainerStore(endpoint=ENDPOINT, access_key=ACCESS_KEY,
                             secret_key=SECRET_KEY, bucket=bucket, secure=SECURE)
    try:
        yield store
    finally:
        client = store._client
        for obj in list(s3client.iter_objects(client, bucket)):
            client.delete_object(Bucket=bucket, Key=obj["key"])
        client.delete_bucket(Bucket=bucket)


def test_the_constructor_creates_the_bucket(store):
    store._client.head_bucket(Bucket=store.bucket)        # raises if absent
    assert describe(store) == (f"s3 ({ENDPOINT}, bucket {store.bucket}, "
                               f"prefix studies/)")


def test_a_study_round_trips_and_says_whether_it_was_new(store, public_study):
    first = store.put("study:abc", public_study)
    assert first["created"] is True and first["key"] == object_key("study:abc")
    assert store.get("study:abc") == public_study
    assert store.put("study:abc", public_study)["created"] is False
    head = store._client.head_object(Bucket=store.bucket, Key=first["key"])
    assert head["ContentType"] == "application/json"
    assert head["ContentLength"] == first["size"]


def test_a_missing_study_is_none_not_an_error(store):
    assert store.get("study:nobody") is None
    assert store.get_blob("registry/nothing.json") is None


def test_listing_reads_only_studies(store, public_study, restricted_study):
    store.put("study:b", restricted_study)
    store.put("study:a", public_study)
    store.put_blob("registry/twins.json", b"{}")       # not a study
    assert store.list() == ["study:a", "study:b"]


def test_listing_follows_the_continuation_token(store, public_study):
    """More objects than one page: the listing must not stop at the first."""
    for n in range(5):
        store.put(f"study:p{n}", public_study)
    keys = [o["key"] for o in s3client.iter_objects(
        store._client, store.bucket, prefix="studies/", page_size=2)]
    assert len(keys) == 5


def test_remove_says_whether_something_was_there(store, public_study):
    store.put("study:a", public_study)
    assert store.remove("study:a") is True
    assert store.remove("study:a") is False
    assert store.list() == []


def test_blobs_are_byte_exact(store):
    payload = b"\x00\x01register bytes\xff" * 32
    info = store.put_blob("registry/x.bin", payload)
    assert info["size"] == len(payload)
    assert store.get_blob("registry/x.bin") == payload
    assert store.remove_blob("registry/x.bin") is True
    assert store.remove_blob("registry/x.bin") is False


def test_wrong_keys_refuse_at_construction():
    with pytest.raises(RuntimeError, match="not usable|did not answer"):
        S3ContainerStore(endpoint=ENDPOINT, access_key="not-a-key",
                         secret_key="not-a-secret",
                         bucket=f"em-test-catalog-{uuid.uuid4().hex[:10]}",
                         secure=SECURE)

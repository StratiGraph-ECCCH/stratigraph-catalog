"""The container store — where the truth lives, and how it is named.

Measured headless against the in-memory implementation; the S3 one is the same
interface and is exercised live by `tests/test_store_s3.py` against a real
S3-compatible store (RustFS in the dev-stack — skipped, by name, without one)
and by `dev-stack/smoke_catalog.py`.
"""

from __future__ import annotations

import pytest

from app.store import (InMemoryContainerStore, S3ContainerStore,
                       bytes_digest, container_bytes, describe, object_key,
                       store_from_env, study_id_from_key)


def test_a_study_round_trips_unchanged(public_study):
    store = InMemoryContainerStore()
    written = store.put("study:abc", public_study)
    assert written["created"] is True
    assert store.get("study:abc") == public_study
    assert store.put("study:abc", public_study)["created"] is False


def test_the_key_is_readable_and_reversible():
    """A colon is legal in an S3 key and awkward in half the tools people use to
    look at one. `reindex` needs the inverse, so the mapping is total."""
    assert object_key("study:abc-123") == "studies/study_abc-123.em.json"
    assert study_id_from_key("studies/study_abc-123.em.json") == "study:abc-123"


def test_the_digest_is_of_canonical_bytes(public_study):
    """Same document, same bytes, same digest — whoever wrote it."""
    shuffled = dict(reversed(list(public_study.items())))
    assert container_bytes(shuffled) == container_bytes(public_study)
    assert bytes_digest(container_bytes(public_study)).startswith("sha256:")


def test_listing_is_what_a_rebuild_reads(public_study, restricted_study):
    store = InMemoryContainerStore()
    store.put("study:b", restricted_study)
    store.put("study:a", public_study)
    assert store.list() == ["study:a", "study:b"]
    assert store.remove("study:a") is True
    assert store.remove("study:a") is False
    assert store.list() == ["study:b"]


def test_memory_is_the_fallback_and_says_so():
    store = store_from_env({})
    assert isinstance(store, InMemoryContainerStore)
    assert describe(store) == "memory", \
        "an operator who reads 'memory' knows their studies die with the process"


def test_a_half_configured_object_store_refuses_to_start():
    """The failure that matters. Falling back to memory on a typo'd variable
    produces a catalogue whose studies quietly disappear on the next restart."""
    with pytest.raises(RuntimeError, match="half-configured"):
        store_from_env({"EM_ASSET_S3_ENDPOINT": "http://rustfs:9000",
                        "EM_ASSET_S3_ACCESS_KEY": "key"})


_FULL = {"EM_ASSET_S3_ENDPOINT": "http://rustfs:9000",
         "EM_ASSET_S3_ACCESS_KEY": "key",
         "EM_ASSET_S3_SECRET_KEY": "secret",
         "EM_ASSET_S3_BUCKET": "em-dev"}


def _fake_construction(monkeypatch):
    built = {}

    def fake_init(self, endpoint, access_key, secret_key, bucket, secure=True):
        built.update(endpoint=endpoint, bucket=bucket, secure=secure)
        self.endpoint, self.bucket = endpoint, bucket

    monkeypatch.setattr(S3ContainerStore, "__init__", fake_init)
    return built


def test_a_full_configuration_selects_s3(monkeypatch):
    """Selected by configuration alone — the construction itself reaches the
    server, so it is measured live in `test_store_s3.py` rather than faked here."""
    built = _fake_construction(monkeypatch)
    store = store_from_env(dict(_FULL))
    assert isinstance(store, S3ContainerStore)
    assert built == {"endpoint": "http://rustfs:9000", "bucket": "em-dev",
                     "secure": False}, "http:// means somebody chose plain"
    assert describe(store) == "s3 (http://rustfs:9000, bucket em-dev, prefix studies/)"

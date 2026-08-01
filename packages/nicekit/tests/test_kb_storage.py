from types import SimpleNamespace

import pytest
from minio.error import S3Error

from nicekit.kb import storage


class _ObjectClient:
    def __init__(
        self,
        *,
        missing: bool = False,
        remove_missing: bool = False,
    ) -> None:
        self.missing = missing
        self.remove_missing = remove_missing
        self.removed: list[tuple[str, str]] = []

    def stat_object(self, bucket: str, key: str) -> object:
        if self.missing:
            raise S3Error(None, "NoSuchKey", "missing", key, "request", "host", bucket, key)
        return object()

    def remove_object(self, bucket: str, key: str) -> None:
        if self.remove_missing:
            raise S3Error(None, "NoSuchKey", "missing", key, "request", "host", bucket, key)
        self.removed.append((bucket, key))


@pytest.mark.parametrize(("missing", "expected"), [(False, True), (True, False)])
async def test_object_exists_handles_missing_keys(
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
    expected: bool,
) -> None:
    client = _ObjectClient(missing=missing)
    monkeypatch.setattr(storage, "_client", lambda: client)
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(minio_bucket="private-kb"),
    )

    assert await storage.object_exists("org/kb/source.pdf") is expected


async def test_remove_object_uses_configured_private_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ObjectClient()
    monkeypatch.setattr(storage, "_client", lambda: client)
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(minio_bucket="private-kb"),
    )

    await storage.remove_object("org/kb/source.pdf")

    assert client.removed == [("private-kb", "org/kb/source.pdf")]


async def test_remove_object_treats_missing_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ObjectClient(remove_missing=True)
    monkeypatch.setattr(storage, "_client", lambda: client)
    monkeypatch.setattr(
        storage,
        "get_settings",
        lambda: SimpleNamespace(minio_bucket="private-kb"),
    )

    await storage.remove_object("org/kb/already-missing.pdf")

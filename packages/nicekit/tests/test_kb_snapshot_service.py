from datetime import UTC, datetime
from uuid import UUID

from nicekit.kb.snapshot import canonical_json, canonical_sha256


def test_canonical_json_and_sha256_are_stable() -> None:
    first = {"b": "值", "a": [2, 1]}
    second = {"a": [2, 1], "b": "值"}

    assert canonical_json(first) == canonical_json(second) == '{"a":[2,1],"b":"值"}'
    assert canonical_sha256(first) == (
        "33efe7cd08723c2527b04db90c909fc24382acb22651eac2a6de2fc39190146f"
    )


def test_canonical_json_normalizes_supported_domain_values() -> None:
    value = {
        "revision_id": UUID("00000000-0000-0000-0000-000000000002"),
        "created_at": datetime(2026, 7, 13, 8, 30, tzinfo=UTC),
    }

    assert canonical_json(value) == (
        '{"created_at":"2026-07-13T08:30:00+00:00",'
        '"revision_id":"00000000-0000-0000-0000-000000000002"}'
    )

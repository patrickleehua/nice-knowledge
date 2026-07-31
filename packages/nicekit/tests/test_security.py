from uuid import uuid4

import jwt
import pytest

from nicekit.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    h = hash_password("s3cret!")
    assert h != "s3cret!"
    assert verify_password("s3cret!", h)
    assert not verify_password("wrong", h)


def test_access_token_roundtrip() -> None:
    user_id, org_id = uuid4(), uuid4()
    token = create_access_token(user_id, org_id, "org_admin")
    claims = decode_access_token(token)
    assert claims["sub"] == str(user_id)
    assert claims["org_id"] == str(org_id)
    assert claims["role"] == "org_admin"


def test_tampered_token_rejected() -> None:
    token = create_access_token(uuid4(), uuid4(), "member")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token + "x")


def test_refresh_token_hash_stable() -> None:
    raw, hashed = new_refresh_token()
    assert hash_refresh_token(raw) == hashed
    assert raw != hashed

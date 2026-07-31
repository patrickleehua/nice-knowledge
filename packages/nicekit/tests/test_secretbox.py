"""SecretBox 单测:加密解密往返、明文直通模式、fail-closed 语义。"""

import pytest

from nicekit.core.secretbox import SecretBox, SecretBoxError


def test_encrypt_decrypt_roundtrip() -> None:
    box = SecretBox("unit-test-master-key")
    ciphertext = box.encrypt("sk-很机密的密钥")
    assert ciphertext != "sk-很机密的密钥"
    assert ciphertext.startswith("enc:")
    assert box.is_encrypted(ciphertext)
    assert box.decrypt(ciphertext) == "sk-很机密的密钥"


def test_encrypt_is_idempotent_on_ciphertext() -> None:
    box = SecretBox("unit-test-master-key")
    once = box.encrypt("value")
    assert box.encrypt(once) == once  # 已是密文不二次加密


def test_decrypt_passes_through_legacy_plaintext() -> None:
    # 存量明文(无 enc: 前缀)原样返回,支持渐进加密
    box = SecretBox("unit-test-master-key")
    assert not box.is_encrypted("legacy-plain-value")
    assert box.decrypt("legacy-plain-value") == "legacy-plain-value"


def test_plaintext_passthrough_mode_without_master_key() -> None:
    box = SecretBox("")
    assert not box.enabled
    assert box.encrypt("secret") == "secret"  # 直通,不加前缀
    assert box.decrypt("secret") == "secret"


def test_passthrough_mode_fails_closed_on_ciphertext() -> None:
    encrypted = SecretBox("unit-test-master-key").encrypt("secret")
    box = SecretBox("")
    with pytest.raises(SecretBoxError):
        box.decrypt(encrypted)  # 解不开的密文绝不当明文返回


def test_wrong_master_key_raises() -> None:
    encrypted = SecretBox("key-a").encrypt("secret")
    with pytest.raises(SecretBoxError):
        SecretBox("key-b").decrypt(encrypted)


def test_get_secret_box_reads_settings(monkeypatch) -> None:
    from nicekit.core import secretbox as module
    from nicekit.core.config import Settings

    monkeypatch.setenv("NICEKIT_SECRET_KEY", "env-master-key")
    module.get_secret_box.cache_clear()
    monkeypatch.setattr(module, "get_settings", lambda: Settings())
    try:
        box = module.get_secret_box()
        assert box.enabled
        assert box.decrypt(box.encrypt("v")) == "v"
    finally:
        module.get_secret_box.cache_clear()

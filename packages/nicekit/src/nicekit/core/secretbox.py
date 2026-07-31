"""密钥加密盒(MIGRATION-PLAN §5.1 新增):落库敏感字段的对称加密。

用途:llm_providers.api_key、service_configs.payload 密钥字段、
mcp_servers.headers/env 等落库前加密,读出后解密。

设计:
- Fernet(AES128-CBC + HMAC,cryptography 库)对称加密;master key 来自
  settings.secret_key_master(env 首选 NICEKIT_SECRET_KEY)。任意字符串经
  SHA-256 派生为合法 Fernet key,运维无需手工生成 base64 key。
- 密文带 "enc:" 前缀标记,is_encrypted() 据此区分明文/密文,decrypt()
  对无前缀的历史明文值原样返回——存量数据可渐进加密,无需一次性迁移。
- master key 未配置时进入"明文直通"模式并记 warning:encrypt 原样返回,
  保证开发环境零配置可跑;此时若遇到 enc: 密文则解不开,抛 SecretBoxError
  fail-closed(绝不把密文当明文用)。
- 预留 KMS:后续可注入外部 KeyProvider 替换 env master key,接口不变。
"""

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from nicekit.core.config import get_settings

logger = logging.getLogger(__name__)

_CIPHERTEXT_PREFIX = "enc:"


class SecretBoxError(Exception):
    """解密失败(密钥缺失/不匹配/密文损坏)。"""


class SecretBox:
    def __init__(self, master_key: str = ""):
        if master_key:
            derived = base64.urlsafe_b64encode(hashlib.sha256(master_key.encode()).digest())
            self._fernet: Fernet | None = Fernet(derived)
        else:
            self._fernet = None
            logger.warning(
                "NICEKIT_SECRET_KEY 未配置,SecretBox 工作在明文直通模式"
                "(开发环境可用;生产必须配置 master key)"
            )

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def is_encrypted(self, value: str) -> bool:
        return value.startswith(_CIPHERTEXT_PREFIX)

    def encrypt(self, plaintext: str) -> str:
        """加密并加 enc: 前缀;明文直通模式下原样返回。已是密文的值不二次加密。"""
        if self._fernet is None:
            return plaintext
        if self.is_encrypted(plaintext):
            return plaintext
        token = self._fernet.encrypt(plaintext.encode()).decode()
        return f"{_CIPHERTEXT_PREFIX}{token}"

    def decrypt(self, token: str) -> str:
        """解密 enc: 密文;无前缀的值视为历史明文原样返回。

        密文但无 key / key 不匹配时抛 SecretBoxError(fail-closed,
        绝不把解不开的密文当明文返回给调用方)。
        """
        if not self.is_encrypted(token):
            return token
        if self._fernet is None:
            raise SecretBoxError(
                "遇到加密值但未配置 master key(NICEKIT_SECRET_KEY),无法解密"
            )
        raw = token[len(_CIPHERTEXT_PREFIX):]
        try:
            return self._fernet.decrypt(raw.encode()).decode()
        except InvalidToken as exc:
            raise SecretBoxError("解密失败:master key 不匹配或密文损坏") from exc


@lru_cache
def get_secret_box() -> SecretBox:
    return SecretBox(get_settings().secret_key_master)

"""认证与机密：全部使用成熟开源实现，不自写密码学。

- 口令哈希：pwdlib（Argon2），与 FastAPI 官方安全教程一致；
- 访问令牌：PyJWT（HS256），FastAPI 官方 OAuth2 密码模式 + Bearer 令牌；带 jti，可在 Redis 吊销；
- 数据源口令：cryptography Fernet（MultiFernet 支持密钥轮换），密钥由 AVELIS_SECRET 经 HKDF 派生。
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pwdlib import PasswordHash

from avelis.config import get_settings

JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = "avelis-api"

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(password, password_hash)


def _derive(secret: str, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(secret.encode())


@lru_cache(maxsize=4)
def _fernet(secret: str) -> MultiFernet:
    key = base64.urlsafe_b64encode(_derive(secret, b"avelis/datasource-credential"))
    return MultiFernet([Fernet(key)])


def encrypt_secret(plain: str) -> str:
    return _fernet(get_settings().secret).encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet(get_settings().secret).decrypt(token.encode()).decode()


@lru_cache(maxsize=4)
def _jwt_key(secret: str) -> bytes:
    return _derive(secret, b"avelis/access-token")


def create_access_token(user_id: str, username: str) -> tuple[str, datetime, str]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=settings.access_token_minutes)
    jti = uuid.uuid4().hex
    token = jwt.encode(
        {"sub": user_id, "name": username, "iat": now, "exp": expires, "jti": jti, "aud": JWT_AUDIENCE},
        _jwt_key(settings.secret),
        algorithm=JWT_ALGORITHM,
    )
    return token, expires, jti


def decode_access_token(token: str) -> dict:
    """校验签名、过期与受众；失败抛 jwt.InvalidTokenError。"""
    return jwt.decode(
        token,
        _jwt_key(get_settings().secret),
        algorithms=[JWT_ALGORITHM],
        audience=JWT_AUDIENCE,
        options={"require": ["sub", "exp", "jti", "aud"]},
    )

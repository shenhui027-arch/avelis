"""认证与机密：全部使用成熟开源实现，不自写密码学。

- 口令哈希：pwdlib（Argon2），与 FastAPI 官方安全教程一致；
- 访问令牌：PyJWT（HS256），FastAPI 官方 OAuth2 密码模式 + Bearer 令牌；带 jti，可在 Redis 吊销；
- 数据源口令：cryptography Fernet（MultiFernet 支持密钥轮换），密钥由 AVELIS_SECRET 经 HKDF 派生。
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from avelis.config import get_settings

JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = "avelis-api"
JWT_REFRESH_AUDIENCE = "avelis-refresh"
JWT_CHALLENGE_AUDIENCE = "avelis-totp-challenge"

_password_hash = PasswordHash.recommended()


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    return _password_hash.hash("avelis-dummy-constant-time-password-9f8e7d6c")


def normalize_username(username: str) -> str:
    return username.strip().lower()


def hash_pat_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash or not password_hash.startswith("$"):
        _password_hash.verify(password, dummy_password_hash())
        return False
    try:
        return _password_hash.verify(password, password_hash)
    except UnknownHashError:
        _password_hash.verify(password, dummy_password_hash())
        return False


def _derive(secret: str, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(secret.encode())


@lru_cache(maxsize=8)
def _fernet_primary(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(_derive(secret, b"avelis/datasource-credential"))
    return Fernet(key)


@lru_cache(maxsize=8)
def _fernet(secret: str, secret_previous: str = "") -> MultiFernet:
    fernets = [_fernet_primary(secret)]
    if secret_previous and secret_previous != secret:
        fernets.append(_fernet_primary(secret_previous))
    return MultiFernet(fernets)


def encrypt_secret(plain: str) -> str:
    s = get_settings()
    return _fernet(s.secret, s.secret_previous).encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    s = get_settings()
    return _fernet(s.secret, s.secret_previous).decrypt(token.encode()).decode()


def needs_secret_rotation(token: str) -> bool:
    """判断密文是否由旧密钥（AVELIS_SECRET_PREVIOUS）加密，需用当前主密钥重加密。"""
    s = get_settings()
    raw = token.encode()
    try:
        _fernet_primary(s.secret).decrypt(raw)
        return False
    except InvalidToken:
        if not s.secret_previous or s.secret_previous == s.secret:
            raise
        _fernet_primary(s.secret_previous).decrypt(raw)
        return True


def rotate_secret_token(token: str) -> tuple[str, bool]:
    """用 MultiFernet 解密后以当前主密钥重新加密；若已由主密钥加密则原样返回。"""
    s = get_settings()
    raw = token.encode()
    primary = _fernet_primary(s.secret)
    try:
        primary.decrypt(raw)
        return token, False
    except InvalidToken:
        plain = _fernet(s.secret, s.secret_previous).decrypt(raw)
        return primary.encrypt(plain).decode(), True


@lru_cache(maxsize=4)
def _jwt_key(secret: str) -> bytes:
    return _derive(secret, b"avelis/access-token")


def session_cutoff_now() -> datetime:
    """“此刻之前签发的令牌全部失效”的截止时间（保留微秒，与令牌的 iat_us 比较）。"""
    return datetime.now(timezone.utc)


def epoch_us(moment: datetime) -> int:
    """UTC 微秒时间戳（整数运算，避免浮点误差）。"""
    return int(moment.replace(microsecond=0).timestamp()) * 1_000_000 + moment.microsecond


def issued_before(claims: dict, cutoff: datetime) -> bool:
    """令牌是否签发于截止时间之前。新令牌带微秒精度的 iat_us，能准确区分同一秒内改密前后的令牌；
    没有 iat_us 的旧令牌只有整秒的 iat，同一秒内签发的一律按“之前”处理（宁可要求重新登录，不放过旧会话）。"""
    cutoff_us = epoch_us(cutoff)
    if "iat_us" in claims:
        return int(claims["iat_us"]) < cutoff_us
    return int(claims.get("iat", 0)) * 1_000_000 < cutoff_us - cutoff_us % 1_000_000 + 1_000_000


def create_access_token(user_id: str, username: str, *, auth_time: int | None = None) -> tuple[str, datetime, str]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    if auth_time is None:
        auth_time = int(now.timestamp())
    session_expires = datetime.fromtimestamp(auth_time, timezone.utc) + timedelta(hours=settings.session_max_hours)
    expires = min(now + timedelta(minutes=settings.access_token_minutes), session_expires).replace(microsecond=0)
    jti = uuid.uuid4().hex
    token = jwt.encode(
        {
            "sub": user_id,
            "name": username,
            "iat": now,
            "iat_us": epoch_us(now),
            "auth_time": auth_time,
            "exp": expires,
            "jti": jti,
            "aud": JWT_AUDIENCE,
        },
        _jwt_key(settings.secret),
        algorithm=JWT_ALGORITHM,
    )
    return token, expires, jti


def _decode_jwt(token: str, audience: str) -> dict:
    s = get_settings()
    opts = {"require": ["sub", "exp", "jti", "aud"]}
    try:
        return jwt.decode(token, _jwt_key(s.secret), algorithms=[JWT_ALGORITHM], audience=audience, options=opts)
    except jwt.InvalidSignatureError:
        if not s.secret_previous or s.secret_previous == s.secret:
            raise
        return jwt.decode(
            token, _jwt_key(s.secret_previous), algorithms=[JWT_ALGORITHM], audience=audience, options=opts
        )


def decode_access_token(token: str) -> dict:
    """校验签名、过期与受众；失败抛 jwt.InvalidTokenError。"""
    return _decode_jwt(token, JWT_AUDIENCE)


def create_refresh_token(
    user_id: str, username: str, *, auth_time: int | None = None, remember_me: bool = False
) -> tuple[str, datetime, str]:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    if auth_time is None:
        auth_time = int(now.timestamp())
    expires = (datetime.fromtimestamp(auth_time, timezone.utc) + timedelta(hours=settings.session_max_hours)).replace(
        microsecond=0
    )
    jti = uuid.uuid4().hex
    token = jwt.encode(
        {
            "sub": user_id,
            "name": username,
            "iat": now,
            "iat_us": epoch_us(now),
            "auth_time": auth_time,
            "remember_me": remember_me,
            "exp": expires,
            "jti": jti,
            "aud": JWT_REFRESH_AUDIENCE,
        },
        _jwt_key(settings.secret),
        algorithm=JWT_ALGORITHM,
    )
    return token, expires, jti


def decode_refresh_token(token: str) -> dict:
    return _decode_jwt(token, JWT_REFRESH_AUDIENCE)


def create_totp_challenge(user_id: str, *, remember_me: bool = False, ttl_seconds: int = 300) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=ttl_seconds)).replace(microsecond=0)
    return jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "iat_us": epoch_us(now),
            "exp": expires,
            "jti": uuid.uuid4().hex,
            "remember_me": remember_me,
            "aud": JWT_CHALLENGE_AUDIENCE,
        },
        _jwt_key(settings.secret),
        algorithm=JWT_ALGORITHM,
    )


def decode_totp_challenge(token: str) -> dict:
    return _decode_jwt(token, JWT_CHALLENGE_AUDIENCE)

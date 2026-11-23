"""纯标准库 RFC 6238 TOTP 双因素认证工具（SHA-1 / 6 位 / 30 秒步长）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

TOTP_DIGITS = 6
TOTP_STEP_SECONDS = 30
TOTP_SKEW = 1


def generate_totp_secret() -> str:
    """生成 160 位随机密钥，返回无填充 Base32 字符串。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_base32(secret: str) -> bytes:
    normalized = secret.strip().upper().replace(" ", "")
    padding = "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized + padding, casefold=True)


def hotp_code(secret: str, counter: int, digits: int = TOTP_DIGITS) -> str:
    key = _decode_base32(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % (10**digits):0{digits}d}"


def totp_step(now: float | None = None, step_seconds: int = TOTP_STEP_SECONDS) -> int:
    ts = time.time() if now is None else now
    return int(ts // step_seconds)


def verify_totp_code(
    secret: str,
    code: str,
    *,
    now: float | None = None,
    skew: int = TOTP_SKEW,
    last_step: int | None = None,
) -> int | None:
    """校验 6 位 TOTP 验证码；通过返回匹配的时间步长（用于防重放），失败或重放返回 None。"""
    cleaned = code.strip().replace(" ", "")
    if len(cleaned) != TOTP_DIGITS or not cleaned.isdigit():
        return None
    current = totp_step(now=now)
    matched_step: int | None = None
    for step in range(current - skew, current + skew + 1):
        candidate = hotp_code(secret, step)
        if hmac.compare_digest(candidate, cleaned):
            matched_step = step
    if matched_step is None:
        return None
    if last_step is not None and matched_step <= last_step:
        return None
    return matched_step


def build_otpauth_uri(secret: str, username: str, issuer: str = "Avelis") -> str:
    clean_issuer = (issuer or "Avelis").strip() or "Avelis"
    label = quote(f"{clean_issuer}:{username}", safe=":")
    issuer_param = quote(clean_issuer, safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={issuer_param}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )

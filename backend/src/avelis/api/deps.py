"""请求依赖：数据库会话、当前用户（FastAPI OAuth2 密码模式 + Bearer 令牌）。"""

from __future__ import annotations

from collections.abc import Iterator

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from avelis import cache
from avelis.db import session_factory
from avelis.models import User
from avelis.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")


def get_session() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


def current_claims(token: str = Depends(oauth2_scheme)) -> dict:
    try:
        claims = decode_access_token(token)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("访问令牌无效或已过期") from exc
    if cache.is_revoked(claims["jti"]):
        raise _unauthorized("访问令牌已注销")
    return claims


def current_user(claims: dict = Depends(current_claims), session: Session = Depends(get_session)) -> User:
    user = session.get(User, claims["sub"])
    if user is None or not user.is_active:
        raise _unauthorized("用户不存在或已停用")
    # 改口令、重置口令后，之前签发的令牌失效
    if user.sessions_valid_after is not None and claims.get("iat", 0) < user.sessions_valid_after.timestamp():
        raise _unauthorized("会话已失效，请重新登录")
    return user

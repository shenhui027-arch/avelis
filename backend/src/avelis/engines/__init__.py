"""引擎注册表与按数据源构造连接器。

核心模块只调用这里：engine_of(ds) 取方言与能力，collect_connector / query_connector 取连接器。
口令在这里解密，只交给数据库驱动，不返回给调用方。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.engines.base import QUERY, CapabilityMissing, Engine, UnsupportedEngine
from avelis.i18n import Msg
from avelis.lineage.sql import SqlDialect
from avelis.models import Datasource, DatasourceCredential
from avelis.security import decrypt_secret

_REGISTRY: dict[str, Engine] = {}


def register(engine: Engine) -> Engine:
    if engine.kind in _REGISTRY:
        raise ValueError(f"引擎 {engine.kind} 已登记")
    _REGISTRY[engine.kind] = engine
    return engine


def get_engine(kind: str) -> Engine:
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise UnsupportedEngine(Msg("不支持的数据源类型：{kind}（已支持：{supported}）", kind=kind,
                                    supported="、".join(sorted(_REGISTRY)))) from None


def engines() -> list[Engine]:
    return list(_REGISTRY.values())


def engine_of(ds: Datasource) -> Engine:
    return get_engine(ds.kind)


def dialect_of(ds: Datasource) -> SqlDialect:
    """该数据源的解析方言（含因数据源而异的 catalog 段）。"""
    return engine_of(ds).dialect_for(ds.options)


class CredentialMissing(Exception):
    pass


def _credential(session: Session, ds: Datasource, purpose: str, required: bool = True) -> tuple[str, str]:
    cred = session.scalar(select(DatasourceCredential).where(
        DatasourceCredential.datasource_id == ds.id, DatasourceCredential.purpose == purpose))
    if cred is None:
        if not required:
            return "", ""
        raise CredentialMissing(Msg("数据源 {name} 未配置 {purpose} 账号", name=ds.name, purpose=purpose))
    return cred.username, decrypt_secret(cred.secret_enc)


def collect_connector(session: Session, ds: Datasource):
    engine = engine_of(ds)
    if engine.needs_credentials:
        # 免口令的认证方式（如 Hive auth = NONE）下账号可不填；填了就用（服务端以它作会话用户）
        user, password = _credential(session, ds, "collect", required=engine.credentials_required(ds.options))
    else:
        user, password = "", ""
    return engine.collector(ds.host, ds.port, user, password, ds.options or {})


def query_connector(session: Session, ds: Datasource):
    engine = engine_of(ds)
    if not engine.supports(QUERY) or engine.querier is None:
        raise CapabilityMissing(engine, QUERY, ds.name)
    user, password = _credential(session, ds, "query")
    return engine.querier(ds.host, ds.port, user, password, ds.options or {})


def _register_builtin() -> None:
    from avelis.engines.builtin import BUILTIN

    for engine in BUILTIN:
        register(engine)


_register_builtin()

__all__ = ["CapabilityMissing", "CredentialMissing", "Engine", "UnsupportedEngine", "collect_connector",
           "dialect_of", "engine_of", "engines", "get_engine", "query_connector", "register"]

"""按数据源配置构造连接器。口令在这里解密，只交给数据库驱动，不返回给调用方。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from avelis.connectors.fake import FakeConnector
from avelis.connectors.mysql import MySQLMetaConnector
from avelis.connectors.starrocks import StarRocksConnector
from avelis.models import Datasource, DatasourceCredential
from avelis.security import decrypt_secret


class CredentialMissing(Exception):
    pass


def _credential(session: Session, ds: Datasource, purpose: str) -> tuple[str, str]:
    cred = session.scalar(select(DatasourceCredential).where(
        DatasourceCredential.datasource_id == ds.id, DatasourceCredential.purpose == purpose))
    if cred is None:
        raise CredentialMissing(f"数据源 {ds.name} 未配置 {purpose} 账号")
    return cred.username, decrypt_secret(cred.secret_enc)


def collect_connector(session: Session, ds: Datasource):
    if ds.kind == "fake":
        return FakeConnector(ds.options)
    user, password = _credential(session, ds, "collect")
    if ds.kind == "starrocks":
        return StarRocksConnector(ds.host, ds.port, user, password, ds.options)
    if ds.kind == "mysql":
        return MySQLMetaConnector(ds.host, ds.port, user, password, ds.options)
    raise ValueError(f"不支持的数据源类型：{ds.kind}")


def query_connector(session: Session, ds: Datasource) -> StarRocksConnector:
    if ds.kind != "starrocks":
        raise ValueError(f"数据源 {ds.name}（{ds.kind}）不支持问数执行；v0.1 只在 StarRocks 上执行")
    user, password = _credential(session, ds, "query")
    return StarRocksConnector(ds.host, ds.port, user, password, ds.options)

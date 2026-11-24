"""连接器共享 TLS/SSL 参数校验与各数据库驱动 SSL 参数构造（M4A）。

所有数据库引擎通过选项 tls_mode / tls_ca_cert 统一配置传输安全；
当 tls_mode 为默认 prefer 且未指定 tls_ca_cert 时返回空字典，保持既有直连测试与默认行为零改动。
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from avelis.config import ConfigError, get_settings

TLS_MODES: tuple[str, ...] = ("prefer", "disable", "require", "verify-ca", "verify-full")
_CA_SUFFIXES: frozenset[str] = frozenset({".pem", ".crt", ".cer"})


@dataclass(frozen=True)
class TlsConfig:
    mode: str
    ca_cert: str | None = None


def _configured_ca_dir(ca_dir: str | None) -> str:
    if ca_dir is not None:
        return ca_dir.strip()
    try:
        return (get_settings().tls_ca_dir or "").strip()
    except ConfigError:
        return ""


def validate_ca_cert_path(raw: str | None, *, ca_dir: str | None = None, check_exists: bool = False) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if "\x00" in text:
        raise ValueError("CA 证书路径包含非法空字符")
    candidate = Path(text)
    if ".." in candidate.parts:
        raise ValueError("CA 证书路径不允许包含 '..' 上级目录跳转")
    if candidate.suffix.lower() not in _CA_SUFFIXES:
        raise ValueError("CA 证书文件扩展名仅支持 .pem / .crt / .cer")
    allowed_dir = _configured_ca_dir(ca_dir)
    if allowed_dir:
        base = Path(allowed_dir).resolve()
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if not resolved.is_relative_to(base):
            raise ValueError("CA 证书路径必须在服务端允许的证书目录内")
        resolved_str = str(resolved)
    else:
        resolved_str = str(candidate)
    if check_exists and not Path(resolved_str).is_file():
        raise ValueError("CA 证书文件不存在或不可读")
    return resolved_str


def parse_tls_options(
    options: dict[str, Any] | None,
    *,
    ca_dir: str | None = None,
    check_exists: bool = False,
) -> TlsConfig:
    opts = options or {}
    raw_mode = opts.get("tls_mode")
    mode = str(raw_mode).strip().lower() if raw_mode not in (None, "") else "prefer"
    if mode not in TLS_MODES:
        raise ValueError(f"不支持的 TLS 模式：{mode}（可选 {', '.join(TLS_MODES)}）")
    raw_ca = opts.get("tls_ca_cert")
    ca_cert = validate_ca_cert_path(
        str(raw_ca) if raw_ca not in (None, "") else None,
        ca_dir=ca_dir,
        check_exists=check_exists and mode in ("verify-ca", "verify-full"),
    )
    if mode == "disable" and ca_cert is not None:
        raise ValueError("tls_mode 为 disable 时不应同时设置 CA 证书路径")
    return TlsConfig(mode=mode, ca_cert=ca_cert)


def pymysql_ssl_kwargs(cfg: TlsConfig) -> dict[str, Any]:
    if cfg.mode == "disable":
        return {"ssl_disabled": True}
    if cfg.mode == "prefer" and not cfg.ca_cert:
        return {}
    if cfg.mode == "require" and not cfg.ca_cert:
        return {"ssl": {"check_hostname": False, "cert_reqs": ssl.CERT_NONE}}
    verify_identity = cfg.mode == "verify-full"
    out: dict[str, Any] = {
        "ssl_verify_cert": cfg.mode in ("verify-ca", "verify-full"),
        "ssl_verify_identity": verify_identity,
    }
    if cfg.ca_cert:
        out["ssl_ca"] = cfg.ca_cert
    else:
        out["ssl"] = {"cert_reqs": ssl.CERT_REQUIRED if cfg.mode in ("verify-ca", "verify-full") else ssl.CERT_NONE}
    return out


def psycopg_ssl_kwargs(cfg: TlsConfig) -> dict[str, Any]:
    if cfg.mode == "prefer" and not cfg.ca_cert:
        return {}
    out: dict[str, Any] = {"sslmode": cfg.mode}
    if cfg.ca_cert:
        out["sslrootcert"] = cfg.ca_cert
    return out


def clickhouse_ssl_kwargs(cfg: TlsConfig) -> dict[str, Any]:
    if cfg.mode == "disable":
        return {"secure": False}
    if cfg.mode == "prefer" and not cfg.ca_cert:
        return {}
    if cfg.mode in ("require", "prefer"):
        out: dict[str, Any] = {"secure": True, "verify": bool(cfg.ca_cert)}
    else:
        out = {"secure": True, "verify": True}
    if cfg.ca_cert:
        out["ca_cert"] = cfg.ca_cert
    return out


def pymssql_ssl_kwargs(cfg: TlsConfig) -> dict[str, Any]:
    if cfg.mode == "disable":
        return {"encryption": "off"}
    if cfg.mode == "prefer" and not cfg.ca_cert:
        return {}
    return {"encryption": "require"}


def oracledb_ssl_kwargs(cfg: TlsConfig) -> dict[str, Any]:
    if cfg.mode == "disable" or (cfg.mode == "prefer" and not cfg.ca_cert):
        return {}
    ctx = ssl.create_default_context(cafile=cfg.ca_cert) if cfg.ca_cert else ssl.create_default_context()
    if cfg.mode in ("require", "prefer"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif cfg.mode == "verify-ca":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    return {
        "protocol": "tcps",
        "ssl_context": ctx,
        "ssl_server_dn_match": cfg.mode == "verify-full",
    }


def impyla_ssl_kwargs(cfg: TlsConfig) -> dict[str, Any]:
    if cfg.mode == "disable":
        return {"use_ssl": False}
    if cfg.mode == "prefer" and not cfg.ca_cert:
        return {}
    verify = cfg.mode in ("verify-ca", "verify-full")
    out: dict[str, Any] = {"use_ssl": True, "verify_cert": verify}
    if cfg.ca_cert:
        out["ca_cert"] = cfg.ca_cert
    return out

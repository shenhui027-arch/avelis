"""Apache Ossie 文件导入与导出；外部文档只按数据解析。"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Annotated, Any, Literal
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import WithJsonSchema
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from avelis import audit
from avelis.api.deps import current_user, get_session
from avelis.governance.errors import GovernanceError
from avelis.i18n import gettext
from avelis.interchange import exporter, importer
from avelis.interchange.schemas import OssieImportReport, OssieImportRequest
from avelis.models import Domain, User

router = APIRouter(prefix="/api/interchange/ossie", tags=["Ossie"])
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_VALUES = 100_000
MAX_DOCUMENT_DEPTH = 100


class _DocumentError(ValueError):
    pass


class _UniqueSafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        if not isinstance(node, yaml.MappingNode):
            raise _DocumentError(gettext("导入内容只允许 JSON 值"))
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise _DocumentError(gettext("导入内容只允许 JSON 值"))
            if key in result:
                raise _DocumentError(gettext("导入内容包含重复键"))
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DocumentError(gettext("导入内容包含重复键"))
        result[key] = value
    return result


def _invalid_constant(_):
    raise _DocumentError(gettext("导入内容只允许 JSON 值"))


def _json_values(document: Any) -> None:
    """限制展开后的节点数，并拒绝 YAML 循环、时间戳、集合与非有限数。"""
    active: set[int] = set()
    remaining = MAX_DOCUMENT_VALUES

    def walk(value, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_DOCUMENT_DEPTH:
            raise _DocumentError(gettext("导入内容嵌套过深或过于复杂"))
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _DocumentError(gettext("导入内容必须是 UTF-8 文本")) from exc
            return
        if value is None or type(value) in (bool, int):
            return
        if type(value) is float and math.isfinite(value):
            return
        if type(value) not in (dict, list) or id(value) in active:
            raise _DocumentError(gettext("导入内容只允许 JSON 值"))
        active.add(id(value))
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _DocumentError(gettext("导入内容只允许 JSON 值"))
                walk(key, depth + 1)
                walk(item, depth + 1)
        else:
            for item in value:
                walk(item, depth + 1)
        active.remove(id(value))

    walk(document, 0)


def _document(raw: bytes | str, *, json_only=False) -> dict:
    if isinstance(raw, bytes):
        if len(raw) > MAX_IMPORT_BYTES:
            raise _DocumentError(gettext("导入文件超过 5 MiB"))
        try:
            raw = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _DocumentError(gettext("导入内容必须是 UTF-8 文本")) from exc
    elif len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise _DocumentError(gettext("导入文件超过 5 MiB"))
    try:
        if json_only:
            document = json.loads(raw, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        else:
            document = yaml.load(raw, Loader=_UniqueSafeLoader)
        _json_values(document)
    except _DocumentError:
        raise
    except (ValueError, TypeError, yaml.YAMLError, RecursionError) as exc:
        raise _DocumentError(gettext("导入内容不是有效的 JSON 或 YAML")) from exc
    if not isinstance(document, dict):
        raise _DocumentError(gettext("导入内容必须是 JSON 对象"))
    return document


def _dry_run(value: Any) -> bool:
    if value is None:
        return True
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.lower() in ("true", "false", "1", "0"):
        return value.lower() in ("true", "1")
    raise _DocumentError(gettext("dry_run 必须是布尔值"))


def _parse_error(reason: str, dry_run: bool) -> GovernanceError:
    return GovernanceError(
        gettext("Ossie 导入请求无效"),
        ok=False,
        dry_run=dry_run,
        results=[],
        errors=[{"layer": "structure", "path": "$", "reason": reason}],
        warnings=[],
        counts={"created": 0, "new_version": 0, "skipped": 0, "failed": 0},
    )


async def _body(request: Request) -> bytes:
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_IMPORT_BYTES:
            raise _DocumentError(gettext("导入文件超过 5 MiB"))
        chunks.append(chunk)
    return b"".join(chunks)


def _download_name(name: str, suffix: str) -> str:
    clean = "".join(char for char in name if ord(char) >= 32 and char not in '/\\"')
    clean = clean.strip(" .") or "domain"
    ascii_name = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", ascii_name).strip("._") or "domain"
    return (
        f"attachment; filename=\"{ascii_name}.ossie.{suffix}\"; filename*=UTF-8''{quote(clean, safe='')}.ossie.{suffix}"
    )


@router.get(
    "/export",
    response_class=Response,
    responses={
        200: {"content": {"application/yaml": {"schema": {"type": "string"}}, "application/json": {}}},
    },
)
def export_ossie(
    domain_id: str,
    metric_ids: list[str] | None = Query(None),
    format: Literal["yaml", "json"] = "yaml",
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    selected = list(
        dict.fromkeys(item.strip() for part in metric_ids or [] for item in part.split(",") if item.strip())
    )
    document = exporter.export_model(session, user, domain_id, selected if metric_ids is not None else None)
    domain = session.get(Domain, domain_id)
    name = domain.name if domain is not None else "domain"
    content = (
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
        if format == "json"
        else yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    )
    audit.record(
        session,
        user,
        "interchange.ossie_export",
        "domain",
        domain_id,
        domain_id=domain_id,
        metric_ids=selected if metric_ids is not None else None,
        format=format,
        metric_count=len(document.get("metrics", [])),
    )
    return Response(
        content=content,
        media_type="application/json" if format == "json" else "application/yaml",
        headers={"Content-Disposition": _download_name(name, format)},
    )


_IMPORT_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [OssieImportRequest.model_json_schema(), {"type": "object", "additionalProperties": True}]
            }
        },
        "application/yaml": {"schema": {"type": "string"}},
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {"type": "string", "format": "binary"},
                    "domain_id": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": True},
                },
            }
        },
    },
}


@router.post(
    "/import",
    response_model=OssieImportReport,
    responses={400: {"model": OssieImportReport}, 403: {"model": OssieImportReport}},
    openapi_extra={"requestBody": _IMPORT_BODY},
)
async def import_ossie(
    request: Request,
    domain_id: str | None = Query(None),
    dry_run: Annotated[
        str | None,
        Query(description="true（默认）只校验；false 创建草稿"),
        WithJsonSchema({"type": "boolean"}),
    ] = None,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> dict:
    preview = True
    try:
        preview = _dry_run(dry_run)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "multipart/form-data":
            # 先流式限制整个请求，再交给 Starlette 解析，避免无界读取上传内容。
            request._body = await _body(request)
            async with request.form(max_files=1, max_fields=4, max_part_size=MAX_IMPORT_BYTES) as form:
                uploads = form.getlist("file")
                if len(uploads) != 1 or not isinstance(uploads[0], UploadFile):
                    raise _DocumentError(gettext("必须上传一个 file 文件"))
                domain_id = domain_id if domain_id is not None else form.get("domain_id")
                preview = _dry_run(dry_run if dry_run is not None else form.get("dry_run"))
                document = _document(await uploads[0].read(MAX_IMPORT_BYTES + 1))
        else:
            raw = await _body(request)
            if content_type in ("application/json", "") or content_type.endswith("+json"):
                value = _document(raw, json_only=True)
                if "document" in value:
                    domain_id = domain_id if domain_id is not None else value.get("domain_id")
                    preview = _dry_run(dry_run if dry_run is not None else value.get("dry_run"))
                    document = value["document"]
                    if isinstance(document, str):
                        document = _document(document)
                    elif not isinstance(document, dict):
                        raise _DocumentError(gettext("导入内容必须是 JSON 对象"))
                else:
                    document = value
            elif content_type in ("application/yaml", "application/x-yaml", "text/yaml", "text/plain"):
                document = _document(raw)
            else:
                raise _DocumentError(gettext("不支持的导入内容类型"))
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise _DocumentError(gettext("缺少目标业务域 domain_id"))
    except StarletteHTTPException as exc:
        raise _parse_error(gettext("导入内容不是有效的 JSON 或 YAML"), preview) from exc
    except _DocumentError as exc:
        raise _parse_error(str(exc), preview) from exc
    return importer.import_model(session, user, document, domain_id, dry_run=preview)

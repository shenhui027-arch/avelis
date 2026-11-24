"""在内存校验技能包，只保留限额内且可安全落盘的内容。"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import zipfile
import zlib
from copy import copy
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

import yaml

from avelis.config import get_settings
from avelis.governance.errors import Invalid
from avelis.i18n import Msg

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", re.MULTILINE | re.DOTALL)
_SCRIPT_SUFFIXES = set(".py .sh .bash .zsh .ps1 .bat .cmd .js .mjs .cjs .ts .rb .pl .php .exe".split())


_MAX_SKILL_MD_BYTES = 256 * 1024
_MAX_FRONTMATTER_BYTES = 16 * 1024
_FRONTMATTER_STRING_TAGS = {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
_BINARY_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".pyc",
        ".so",
        ".dll",
        ".exe",
        ".bin",
        ".parquet",
    }
)
_PROMPT_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "指令覆盖",
        re.compile(
            r"(?:ignore|disregard|forget|override)\s+(?:all\s+(?:of\s+)?|any\s+)?(?:the\s+)?"
            r"(?:previous|prior|above|earlier|preceding|system|developer|safety|governance)\s+"
            r"(?:instructions?|prompts?|rules?|directives?|constraints?|guardrails?)"
            r"|forget\s+(?:everything|all(?:\s+of)?)\s+(?:above|previous|prior|earlier)"
            r"|(?:reveal|dump|print|output|leak|exfiltrate|show|extract)\s+(?:all\s+|your\s+|the\s+)?"
            r"(?:system|developer|hidden|internal)\s+(?:prompts?|instructions?|rules?|messages?)"
            r"|(?<!不得)(?<!不要)(?<!不能)(?<!请勿)(?<!勿)(?<!切勿)(?<!禁止)(?:忽略|无视|忘掉|放弃|覆盖|绕过|篡改)\s*"
            r"(?:(?:之前|以上|所有|全部|先前|系统|安全|权限|治理)(?:的)?\s*)+"
            r"(?:指令|提示词?|规则|限制|约束|校验|权限)"
            r"|(?<!不得)(?<!不要)(?<!不能)(?<!请勿)(?<!勿)(?<!切勿)(?<!禁止)绕过\s*(?:权限|行级权限|RLS|脱敏|审计)"
            r"|(?:泄露|输出|打印|套取|吐出|暴露)\s*(?:你的|所有|当前)?(?:系统提示词?|提示词|隐藏指令|内部规则)",
            re.IGNORECASE,
        ),
    ),
    (
        "越狱指令",
        re.compile(
            r"\bDAN\s+mode\b"
            r"|\bjailbreak\s+(?:mode|prompt|directive|instructions?)\b"
            r"|bypass\s+(?:all\s+)?(?:safety|security|permission|content|access)\s+"
            r"(?:filters?|checks?|restrictions?|guardrails?|controls?)"
            r"|(?:act\s+as|you\s+are\s+now|pretend\s+to\s+be)\s+(?:an?\s+)?"
            r"(?:unrestricted|unfiltered|uncensored|jailbroken)\b"
            r"|\bdeveloper\s+mode\s+enabled\b",
            re.IGNORECASE,
        ),
    ),
    (
        "数据外泄指令",
        re.compile(
            r"(?:exfiltrate|steal|leak|transmit|send\s+out)\b.{0,60}\b"
            r"(?:secrets?|api[_ -]?keys?|passwords?|credentials?|tokens?|private[_ -]?keys?|"
            r"env(?:ironment)?\s+var(?:iable)?s?|AVELIS_[A-Z0-9_]+)"
            r"|(?:read|dump|cat|print)\b.{0,40}(?:/etc/passwd|/etc/shadow|(?:^|[\s/'\"])\.env\b|id_rsa)",
            re.IGNORECASE,
        ),
    ),
    (
        "隐藏角色标签",
        re.compile(
            r"<\|(?:im_start|im_end|system|assistant|user|endoftext)\|>"
            r"|\[/?INST\]|<<SYS>>|<</SYS>>"
            r"|<\s*/?\s*(?:system|system_prompt|system-instruction|developer|tool_call|function_call|prompt_override)"
            r"\b[^>]{0,256}>",
            re.IGNORECASE,
        ),
    ),
)
_SMUGGLING_CHARS = re.compile(r"[\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]")
_ZERO_WIDTH_CHARS = re.compile(r"[\u200b-\u200f\u2060-\u2064\u00ad\ufeff]")
_SECRET_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
    r"|\b(?:AVELIS_SECRET(?:_PREVIOUS)?|AVELIS_DB_PASSWORD|AVELIS_REDIS_PASSWORD|AVELIS_MODEL_API_KEY)\b"
    r"|\b(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
    r"|(?:password|passwd|api_key|secret_key|access_token)\s*[:=]\s*"
    r"[\"'](?!\*{3,}|xxx+|<[^>]+>|your[_-]|example|changeme|placeholder)[^\"'\s]{8,}[\"']",
    re.IGNORECASE,
)


class _FrontmatterLoader(yaml.SafeLoader):
    # 技能名称和元数据中的 on、yes、日期等保持为字符串。
    yaml_implicit_resolvers = {
        initial: [(tag, pattern) for tag, pattern in resolvers if tag not in _FRONTMATTER_STRING_TAGS]
        for initial, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    yaml_constructors = {
        **yaml.SafeLoader.yaml_constructors,
        "tag:yaml.org,2002:bool": yaml.SafeLoader.construct_scalar,
        "tag:yaml.org,2002:timestamp": yaml.SafeLoader.construct_scalar,
    }


@dataclass
class PackageResult:
    ok: bool = False
    name: str | None = None
    description: str | None = None
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    files: list[dict] = field(default_factory=list)
    has_scripts: bool = False
    line_count: int | None = None
    checks: list[dict] = field(default_factory=list)
    zip_bytes: int = 0
    _members: list[tuple[str, bytes | None]] = field(default_factory=list, repr=False)
    _root_prefix: str = field(default="", repr=False)

    def _check(self, level: str, message: Msg) -> None:
        self.checks.append({"level": level, "message": message})


def _path_parts(path: str) -> tuple[str, ...]:
    normalized = path.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    invalid_windows = os.name == "nt" and any(
        part.endswith((" ", "."))
        or PureWindowsPath(part).is_reserved()
        or any(char in '<>"|?*' or ord(char) < 32 for char in part)
        for part in parts
    )
    if not parts or normalized.startswith("/") or ".." in parts or ":" in path or "\x00" in path or invalid_windows:
        raise Invalid(Msg("文件路径不合法：{name}", name=path.replace("\x00", "\\0")))
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise Invalid(Msg("文件路径分段不能超过 255 字节：{name}", name=path))
    return parts


def _kind(path: str) -> str:
    if path == "SKILL.md":
        return "skill_md"
    if path.startswith("scripts/") or PurePosixPath(path).suffix.lower() in _SCRIPT_SUFFIXES:
        return "script"
    if path.startswith("references/"):
        return "reference"
    if path.startswith("assets/"):
        return "asset"
    return "other"


def _archive_members(data: bytes, max_files: int, max_unpacked_bytes: int) -> dict[str, bytes | None]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, UnicodeError) as exc:
        raise Invalid(Msg("技能包不是有效的 zip 文件")) from exc
    members: dict[str, bytes | None] = {}
    paths: dict[str, tuple[str, bool]] = {}
    with archive:
        entries = archive.infolist()
        if len(entries) > max_files:
            raise Invalid(Msg("zip 条目数不能超过 {limit}", limit=max_files))
        if sum(entry.file_size for entry in entries) > max_unpacked_bytes:
            raise Invalid(Msg("技能包解压后总大小不能超过 {limit} 字节", limit=max_unpacked_bytes))
        remaining = max_unpacked_bytes
        for entry in entries:
            parts = _path_parts(entry.orig_filename)
            path = "/".join(parts)
            if entry.flag_bits & 1:
                raise Invalid(Msg("不支持加密 zip 条目：{name}", name=path))
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if mode == stat.S_IFLNK or entry.external_attr & 0x400:
                raise Invalid(Msg("zip 不支持符号链接：{name}", name=path))
            is_dir = entry.is_dir() or entry.orig_filename.endswith("\\")
            if mode not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG) or (is_dir and entry.file_size):
                raise Invalid(Msg("技能包包含不支持的条目：{name}", name=path))
            if not is_dir and PurePosixPath(path).suffix.lower() == ".zip":
                raise Invalid(Msg("不支持嵌套 zip：{name}", name=path))
            key = path.lower() if os.name == "nt" else path
            existing = paths.get(key)
            if existing is not None and (not is_dir or existing != (path, True)):
                raise Invalid(Msg("技能包内文件路径冲突：{name}", name=path))
            paths[key] = (path, is_dir)
            # Python 的 ZIP BZIP2/LZMA 读取器不会限制单次解压输出；只使用有界的存储和 Deflate。
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise Invalid(Msg("技能包包含不支持的压缩方式：{name}", name=path))
            bounded = copy(entry)
            bounded.file_size = remaining + 1  # 勿让伪造的条目大小提前结束实际解压。
            try:
                with archive.open(bounded) as stream:
                    content = stream.read(remaining + 1)
            except (EOFError, OSError, RuntimeError, ValueError, zipfile.BadZipFile, zlib.error) as exc:
                raise Invalid(Msg("zip 条目无法读取：{name}", name=path)) from exc
            if len(content) > remaining:
                raise Invalid(Msg("技能包解压后总大小不能超过 {limit} 字节", limit=max_unpacked_bytes))
            remaining -= len(content)
            if len(content) != entry.file_size:
                raise Invalid(Msg("zip 条目无法读取：{name}", name=path))
            if not is_dir and zipfile.is_zipfile(io.BytesIO(content)):
                raise Invalid(Msg("不支持嵌套 zip：{name}", name=path))
            if parts[0] != "__MACOSX" and ".DS_Store" not in parts:
                members[path] = None if is_dir else content
    # 按路径分量排序，文件和试图写入该文件路径下的条目会相邻。
    ordered = sorted(paths, key=lambda path: path.split("/"))
    for parent, child in zip(ordered, ordered[1:]):
        path, is_dir = paths[parent]
        if not is_dir and child.startswith(parent + "/"):
            raise Invalid(Msg("技能包内文件路径冲突：{name}", name=path))
    return members


def _split_skill_md(text: str) -> tuple[str, str]:
    match = _FRONTMATTER.match(text)
    if not match or "\x00" in text:
        raise Invalid(Msg("SKILL.md 的 frontmatter 必须是有效的 YAML 映射"))
    return match[1], text[match.end() :]


def _frontmatter(text: str, limit: int) -> dict:
    if len(text.encode("utf-8")) > _MAX_FRONTMATTER_BYTES:
        raise Invalid(Msg("frontmatter 不能超过 16 KB"))
    remaining = _MAX_FRONTMATTER_BYTES

    def check_structure(node) -> None:
        # 在 SafeLoader 展开 YAML 合并映射前限制别名展开后的节点数。
        nonlocal remaining
        remaining -= 1
        if remaining < 0:
            raise ValueError
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                check_structure(key)
                check_structure(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                check_structure(value)

    try:
        loader = _FrontmatterLoader(text)
        try:
            node = loader.get_single_node()
            check_structure(node)
            value = loader.construct_document(node) if node is not None else None
        finally:
            loader.dispose()
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise ValueError
        # YAML 锚点可放大序列化体积，也可能引入循环。按 JSON 编码增量检查，避免展开后才限额。
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
            limit -= len(chunk.encode("utf-8"))
            if limit < 0 or re.search(r"(?<!\\)(?:\\\\)*\\u0000", chunk):
                raise ValueError
        return value
    except (yaml.YAMLError, TypeError, ValueError, RecursionError) as exc:
        raise Invalid(Msg("SKILL.md 的 frontmatter 必须是有效的 YAML 映射")) from exc


def _extract_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_strings(item))
        return out
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out.extend(_extract_strings(k))
            out.extend(_extract_strings(v))
        return out
    return []


def _sensitive_words(settings) -> list[str]:
    raw = settings.env.get("AVELIS_SKILLS_SENSITIVE_WORDS")
    if raw is None:
        raw = os.environ.get("AVELIS_SKILLS_SENSITIVE_WORDS")
    if not raw:
        return []
    words = [part.strip() for part in re.split(r"[,;\r\n]+", str(raw)) if part.strip()]
    return list(dict.fromkeys(words))


def _contains_sensitive_word(text: str, word: str) -> bool:
    if word.isascii() and any(ch.isalnum() for ch in word):
        return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", text, re.IGNORECASE))
    return word.casefold() in text.casefold()


def _scan_package_security(result: PackageResult, settings, header: str) -> None:
    targets: list[tuple[str, str]] = [("SKILL.md", header + "\n" + result.body)]
    fm_strings = _extract_strings(result.frontmatter)
    if fm_strings:
        targets.append(("SKILL.md", "\n".join(fm_strings)))
    for path, content in result._members:
        if content is None or path == "SKILL.md" or b"\x00" in content:
            continue
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        targets.append((path, decoded))

    sensitive_words = _sensitive_words(settings)
    seen_errors: set[tuple[str, str]] = set()

    def record(category: str, path: str, message: Msg) -> None:
        key = (category, path)
        if key not in seen_errors:
            seen_errors.add(key)
            result._check("error", message)

    for path, text in targets:
        normalized = _ZERO_WIDTH_CHARS.sub("", text)
        for rule_name, pattern in _PROMPT_INJECTION_RULES:
            if pattern.search(normalized):
                record(
                    f"injection:{rule_name}",
                    path,
                    Msg("文件 {path} 检测到疑似提示词注入（{kind}）", path=path, kind=rule_name),
                )
        if _SMUGGLING_CHARS.search(text):
            record(
                "smuggling",
                path,
                Msg("文件 {path} 包含可疑隐藏控制字符（Bidi/Tag）", path=path),
            )
        if _SECRET_PATTERN.search(normalized):
            record(
                "secret",
                path,
                Msg("文件 {path} 包含疑似密钥或内部敏感配置", path=path),
            )
        for word in sensitive_words:
            if _contains_sensitive_word(normalized, word):
                record(
                    "sensitive_word",
                    path,
                    Msg("文件 {path} 包含敏感词，未通过安全预检", path=path),
                )
                break


def validate_zip(data: bytes) -> PackageResult:
    settings = get_settings()
    result = PackageResult(zip_bytes=len(data))
    try:
        members = _archive_members(data, settings.skills_max_files, settings.skills_max_unpacked_bytes)
        skill_md = members.get("SKILL.md")
        if skill_md is None:
            top = {path.split("/", 1)[0] for path in members}
            if len(top) == 1:
                prefix = next(iter(top)) + "/"
                skill_md = members.get(prefix + "SKILL.md")
                result._root_prefix = prefix
            if skill_md is None:
                raise Invalid(Msg("包内需要恰好一个技能目录，且包含 SKILL.md"))
        result._members = [
            (path.removeprefix(result._root_prefix), content)
            for path, content in sorted(members.items())
            if path.startswith(result._root_prefix)
        ]
        result.files = [
            {"path": path, "size": len(content), "kind": _kind(path)}
            for path, content in result._members
            if content is not None
        ]
        scripts = sum(item["kind"] == "script" for item in result.files)
        result.has_scripts = bool(scripts)
        if len(skill_md) > _MAX_SKILL_MD_BYTES:
            raise Invalid(Msg("SKILL.md 不能超过 256 KB"))
        try:
            text = skill_md.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise Invalid(Msg("SKILL.md 必须是 UTF-8 文本")) from exc
        result.line_count = text.count("\n") + int(bool(text) and not text.endswith("\n"))
        header, result.body = _split_skill_md(text)
        result.frontmatter = _frontmatter(header, settings.skills_max_unpacked_bytes)
    except Invalid as exc:
        result._check("error", exc.message)
        return result

    name = result.frontmatter.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not _NAME.fullmatch(name):
        result._check("error", Msg("技能名称须为 1 到 64 个小写字母、数字或单连字符，且以字母或数字开头和结尾"))
    else:
        result.name = name
        if result._root_prefix and result._root_prefix != name + "/":
            result._check("error", Msg("技能名称必须与包内目录名一致"))
        try:
            _path_parts(name)
        except Invalid as exc:
            result._check("error", exc.message)
    description = result.frontmatter.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        result._check("error", Msg("技能描述不能为空，且不能超过 1024 字"))
    else:
        result.description = description

    if "compatibility" in result.frontmatter:
        compatibility = result.frontmatter["compatibility"]
        if not isinstance(compatibility, str) or not 1 <= len(compatibility) <= 500:
            result._check("error", Msg("compatibility 必须是 1 到 500 个字符的文本"))
    if "metadata" in result.frontmatter:
        metadata = result.frontmatter["metadata"]
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()
        ):
            result._check("error", Msg("metadata 的键和值都必须是字符串"))

    if result.line_count > 500:
        result._check("warn", Msg("建议不超过 500 行"))
    if len(result.body) > 10_000:
        result._check("warn", Msg("建议正文不超过 5000 个估算 token"))
    if scripts:
        result._check("warn", Msg("包含 {count} 个脚本：只保存，不会执行", count=scripts))
    if "allowed-tools" in result.frontmatter:
        result._check("warn", Msg("allowed-tools 不生效"))
    _scan_package_security(result, settings, header)
    result.ok = all(check["level"] != "error" for check in result.checks)
    if result.ok:
        result._check("ok", Msg("通过技能校验"))
    return result

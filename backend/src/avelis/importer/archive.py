"""读取多文件或 zip，限制解压体积，并按表读写依赖稳定排序。"""

from __future__ import annotations

import heapq
import io
import posixpath
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeVar

from avelis.governance.errors import Invalid
from avelis.i18n import Msg

LANGUAGES = {".sql": "sql", ".py": "python", ".sh": "shell", ".bash": "shell"}
MAX_ARCHIVE_ENTRIES = 2000


@dataclass
class ImportFile:
    filename: str
    content: str
    language: str
    source_key: str = ""
    size_bytes: int = 0

    def __post_init__(self) -> None:
        self.source_key = self.source_key or self.filename
        if not self.size_bytes:
            self.size_bytes = len(self.content.encode("utf-8"))


@dataclass
class FileDependencies:
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)


def _relative_path(filename: str) -> str:
    path = filename.replace("\\", "/")
    parts = PurePosixPath(path).parts
    if not path or path.startswith("/") or any(part in ("..", "") for part in parts) or ":" in path or "\x00" in path:
        raise Invalid(Msg("文件路径不合法：{name}", name=filename))
    normalized = posixpath.normpath(path)
    if normalized in (".", ".."):
        raise Invalid(Msg("文件路径不合法：{name}", name=filename))
    return normalized


def read_uploads(
    uploads: list[tuple[str, bytes]],
    *,
    max_file_bytes: int = 20 * 1024 * 1024,
    max_batch_bytes: int = 100 * 1024 * 1024,
) -> tuple[list[ImportFile], list[str]]:
    """只在内存读取，不向文件系统解压；上限包含不支持扩展名的 zip 条目。"""
    files: list[ImportFile] = []
    notes: list[str] = []
    total_bytes = 0
    entries = 0
    source_keys: set[str] = set()

    def add(filename: str, data: bytes) -> None:
        nonlocal total_bytes
        if len(data) > max_file_bytes:
            raise Invalid(Msg("文件 {name} 超过单文件大小上限", name=filename))
        total_bytes += len(data)
        if total_bytes > max_batch_bytes:
            raise Invalid("导入内容超过批次大小上限（按解压后大小计量）")
        suffix = PurePosixPath(filename).suffix.lower()
        language = LANGUAGES.get(suffix)
        if language is None:
            notes.append(f"已跳过不支持的文件：{filename}")
            return
        basename = PurePosixPath(filename).name
        if len(basename) > 255:
            raise Invalid(Msg("文件名不能超过 255 个字符：{name}", name=basename[:80]))
        if len(filename) > 512:
            raise Invalid(Msg("文件来源键不能超过 512 个字符：{name}", name=filename[:80]))
        if filename in source_keys:
            raise Invalid(Msg("批次中存在重复来源键：{name}", name=filename))
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise Invalid(Msg("文件 {name} 不是有效的 UTF-8 文本", name=filename)) from exc
        source_keys.add(filename)
        files.append(ImportFile(basename, content, language, filename, len(data)))

    for original_name, data in uploads:
        filename = _relative_path(original_name)
        if PurePosixPath(filename).suffix.lower() != ".zip":
            add(filename, data)
            continue
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError) as exc:
            raise Invalid(Msg("文件 {name} 不是有效的 zip", name=filename)) from exc
        with archive:
            members = archive.infolist()
            entries += len(members)
            if entries > MAX_ARCHIVE_ENTRIES:
                raise Invalid(Msg("zip 条目数不能超过 {limit}", limit=MAX_ARCHIVE_ENTRIES))
            for member in members:
                name = _relative_path(member.filename)
                if member.is_dir():
                    continue
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise Invalid(Msg("zip 不支持符号链接：{name}", name=name))
                if PurePosixPath(name).suffix.lower() == ".zip":
                    raise Invalid(Msg("不支持嵌套 zip：{name}", name=name))
                if member.flag_bits & 1:
                    raise Invalid(Msg("不支持加密 zip 条目：{name}", name=name))
                if member.file_size > max_file_bytes:
                    raise Invalid(Msg("文件 {name} 超过单文件大小上限（按解压后大小计量）", name=name))
                if total_bytes + member.file_size > max_batch_bytes:
                    raise Invalid("导入内容超过批次大小上限（按解压后大小计量）")
                try:
                    with archive.open(member) as stream:
                        content = stream.read(min(max_file_bytes, max_batch_bytes - total_bytes) + 1)
                except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError) as exc:
                    raise Invalid(Msg("zip 条目无法读取：{name}", name=name)) from exc
                if zipfile.is_zipfile(io.BytesIO(content)):
                    raise Invalid(Msg("不支持嵌套 zip：{name}", name=name))
                add(name, content)
    if not files:
        raise Invalid("没有可导入的 SQL、Python 或 Shell 文件", notes=notes)
    return files, notes


T = TypeVar("T")


def order_files(files: list[T], dependencies: list[FileDependencies]) -> tuple[list[T], list[str]]:
    """拓扑排序强连通分量；环内和互不依赖的文件保持上传顺序。"""
    if len(files) != len(dependencies):
        raise ValueError("文件与依赖数量不一致")
    writers: dict[str, set[int]] = {}
    for index, dependency in enumerate(dependencies):
        for ref in dependency.writes:
            writers.setdefault(ref, set()).add(index)
    graph: list[set[int]] = [set() for _ in files]
    for index, dependency in enumerate(dependencies):
        for ref in dependency.reads:
            for source in writers.get(ref, set()):
                if source != index:
                    graph[source].add(index)

    # 两趟非递归深度遍历求强连通分量，文件较多时也不依赖 Python 的递归上限。
    seen: set[int] = set()
    finished: list[int] = []
    for root in range(len(files)):
        if root in seen:
            continue
        stack = [(root, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                finished.append(node)
                continue
            if node in seen:
                continue
            seen.add(node)
            stack.append((node, True))
            stack.extend((child, False) for child in sorted(graph[node], reverse=True) if child not in seen)
    reverse: list[set[int]] = [set() for _ in files]
    for source, children in enumerate(graph):
        for target in children:
            reverse[target].add(source)
    components: list[list[int]] = []
    assigned: set[int] = set()
    for root in reversed(finished):
        if root in assigned:
            continue
        component: list[int] = []
        stack = [root]
        assigned.add(root)
        while stack:
            node = stack.pop()
            component.append(node)
            for child in reverse[node]:
                if child not in assigned:
                    assigned.add(child)
                    stack.append(child)
        components.append(sorted(component))
    component_of = {node: index for index, nodes in enumerate(components) for node in nodes}
    children: list[set[int]] = [set() for _ in components]
    indegree = [0 for _ in components]
    for source, targets in enumerate(graph):
        for target in targets:
            left, right = component_of[source], component_of[target]
            if left != right and right not in children[left]:
                children[left].add(right)
                indegree[right] += 1
    ready = [(nodes[0], index) for index, nodes in enumerate(components) if indegree[index] == 0]
    heapq.heapify(ready)
    ordered: list[T] = []
    notes: list[str] = []
    while ready:
        _, component = heapq.heappop(ready)
        indices = components[component]
        ordered.extend(files[index] for index in indices)
        if len(indices) > 1:
            names = [str(getattr(files[index], "source_key", files[index])) for index in indices]
            notes.append(f"文件读写依赖存在环，环内保持上传顺序：{'、'.join(names)}")
        for child in children[component]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (components[child][0], child))
    return ordered, notes

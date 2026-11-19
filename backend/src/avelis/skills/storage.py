"""技能包的不可变版本与上架目录。"""

from __future__ import annotations

import codecs
import errno
import logging
import shutil
import tempfile
from functools import wraps
from os import fstat
from pathlib import Path

from avelis.config import get_settings
from avelis.governance.errors import Conflict, Invalid
from avelis.i18n import Msg
from avelis.skills.package import _MAX_SKILL_MD_BYTES, PackageResult, _path_parts, _split_skill_md

log = logging.getLogger(__name__)


class SkillStorageError(Invalid):
    """技能文件操作失败，服务端路径仅写入日志。"""


def _storage_errors(fn):
    @wraps(fn)
    def wrapped(name: str, *args, **kwargs):
        try:
            return fn(name, *args, **kwargs)
        except (OSError, RuntimeError) as exc:
            log.exception("技能文件操作失败：%s，存储目录：%s", name, get_settings().skills_dir)
            raise SkillStorageError(Msg("技能文件不可用：{name}", name=name)) from exc

    return wrapped


def root() -> Path:
    path = Path(get_settings().skills_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _checked(parent: Path, *parts: str) -> Path:
    path = parent
    for part in parts:
        path /= part
        if path.is_symlink() or path.is_junction():
            raise OSError(errno.ELOOP, "Symbolic link in skill storage", path)
    return path


def _version_dir(name: str, version: int) -> Path:
    return _checked(root(), ".versions", *_path_parts(name), str(version))


def _zip_path(name: str, version: int) -> Path:
    return _checked(root(), ".versions", *_path_parts(name), f"{version}.zip")


@_storage_errors
def zip_path(name: str, version: int) -> Path:
    archive = _zip_path(name, version)
    if not archive.is_file():
        raise FileNotFoundError(errno.ENOENT, "Skill archive missing", archive)
    with archive.open("rb"):
        return archive


@_storage_errors
def write_version(name: str, version: int, data: bytes, result: PackageResult, *, replace_orphan: bool = False) -> None:
    target = _version_dir(name, version)
    archive = _zip_path(name, version)
    if target.exists() or archive.exists():
        if not replace_orphan:
            raise Conflict(Msg("技能版本文件已存在"))
        discard_version(name, version)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".tmp-", dir=target.parent))
    reserved = False
    try:
        for path, content in result._members:
            member = staged.joinpath(*_path_parts(path))
            if content is None:
                member.mkdir(parents=True, exist_ok=True)
            else:
                member.parent.mkdir(parents=True, exist_ok=True)
                member.write_bytes(content)

        # 独占创建归档，为同版本的目录改名保留唯一写入权。
        with archive.open("xb") as stream:
            reserved = True
            stream.write(data)
        if target.exists():
            raise Conflict(Msg("技能版本文件已存在"))
        staged.rename(target)
    except FileExistsError as exc:
        raise Conflict(Msg("技能版本文件已存在")) from exc
    finally:
        if staged.exists():
            try:
                if reserved:
                    archive.unlink(missing_ok=True)
            finally:
                shutil.rmtree(staged, ignore_errors=True)


@_storage_errors
def discard_version(name: str, version: int) -> None:
    directory = _version_dir(name, version)
    try:
        if directory.is_dir():
            shutil.rmtree(directory)
        else:
            directory.unlink(missing_ok=True)
    finally:
        _zip_path(name, version).unlink(missing_ok=True)


def _publication_paths(name: str) -> tuple[Path, Path]:
    directory = root()
    target = _checked(directory, *_path_parts(name))
    staging = _checked(directory, ".tmp")
    return target, staging


@_storage_errors
def publish(name: str, version: int) -> None:
    target, staging = _publication_paths(name)
    staging.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=staging))
    backup = work / "previous"
    try:
        staged = work / "next"
        shutil.copytree(_version_dir(name, version), staged)
        if target.exists():
            target.rename(backup)
        try:
            staged.rename(target)
        except OSError:
            if backup.exists():
                backup.rename(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        # 如果恢复旧目录也失败，将备份留在原地。
        if not backup.exists():
            shutil.rmtree(work)


@_storage_errors
def unpublish(name: str) -> None:
    target, staging = _publication_paths(name)
    if not target.exists():
        return
    staging.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=staging) as work:
        try:
            target.rename(Path(work) / "previous")
        except FileNotFoundError:
            pass


def _file_path(name: str, version: int, path: str) -> Path:
    directory = _version_dir(name, version)
    file = _checked(directory, *_path_parts(path))
    if not file.resolve().is_relative_to(directory):
        raise OSError(errno.ELOOP, "Skill file resolves outside its version directory", file)
    if not file.is_file():
        raise FileNotFoundError(errno.ENOENT, "Skill file missing", file)
    return file


@_storage_errors
def read_file(name: str, version: int, path: str, limit: int) -> tuple[str | None, int, bool, bool]:
    file = _file_path(name, version, path)
    with file.open("rb") as stream:
        size = fstat(stream.fileno()).st_size
        prefix = stream.read(limit)
        truncated = size > limit
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            if b"\x00" not in prefix:
                content = decoder.decode(prefix, final=not truncated)
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    if b"\x00" in chunk:
                        break
                    decoder.decode(chunk)
                else:
                    decoder.decode(b"", final=True)
                    return content, size, truncated, False
        except UnicodeDecodeError:
            pass
    return None, size, False, True


@_storage_errors
def skill_md_body(name: str, version: int) -> str:
    file = _file_path(name, version, "SKILL.md")
    with file.open("rb") as stream:
        data = stream.read(_MAX_SKILL_MD_BYTES + 1)
    if len(data) > _MAX_SKILL_MD_BYTES:
        raise OSError(errno.EFBIG, "SKILL.md exceeds the storage read limit", file)
    try:
        return _split_skill_md(data.decode("utf-8-sig"))[1]
    except (UnicodeError, Invalid) as exc:
        raise OSError(errno.EINVAL, "Invalid stored SKILL.md", file) from exc

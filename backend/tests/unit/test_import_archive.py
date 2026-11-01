"""多文件读取、zip 上限与依赖排序，只使用内存数据。"""

import io
import stat
import zipfile

import pytest

from avelis.governance.errors import Invalid
from avelis.importer.archive import FileDependencies, ImportFile, order_files, read_uploads


def archive(entries, *, compression=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as zipped:
        for name, content in entries:
            zipped.writestr(name, content)
    return stream.getvalue()


def test_plain_and_zip_files_keep_relative_source_keys_and_skip_unknown_extensions():
    files, notes = read_uploads([
        ("first.sql", b"SELECT 1"),
        ("scripts.zip", archive([("nested/second.py", "cursor.execute('SELECT 2')"),
                                 ("nested/readme.txt", "instructions")])),
        ("last.sh", b"psql -c 'SELECT 3'"),
    ])
    assert [file.source_key for file in files] == ["first.sql", "nested/second.py", "last.sh"]
    assert [file.filename for file in files] == ["first.sql", "second.py", "last.sh"]
    assert [file.language for file in files] == ["sql", "python", "shell"]
    assert isinstance(files[0].content, str)
    assert notes == ["已跳过不支持的文件：nested/readme.txt"]


def test_bom_is_decoded_but_byte_limit_includes_it():
    content = b"\xef\xbb\xbfSELECT 1"
    files, _ = read_uploads([("first.sql", content)])
    assert files[0].content == "SELECT 1" and files[0].size_bytes == len(content)
    with pytest.raises(Invalid, match="单文件大小上限"):
        read_uploads([("first.sql", content)], max_file_bytes=len(content) - 1)


@pytest.mark.parametrize("name", ["../first.sql", "/first.sql", "C:/first.sql", "nested/../../first.sql"])
def test_unsafe_zip_paths_are_rejected(name):
    with pytest.raises(Invalid, match="文件路径不合法"):
        read_uploads([("scripts.zip", archive([(name, "SELECT 1")]))])


def test_duplicate_source_keys_are_rejected_across_uploads():
    with pytest.raises(Invalid, match="重复来源键"):
        read_uploads([("same.sql", b"SELECT 1"), ("scripts.zip", archive([("same.sql", "SELECT 2")]))])


def test_invalid_utf8_and_corrupt_archives_have_explicit_errors():
    with pytest.raises(Invalid, match="UTF-8"):
        read_uploads([("first.sql", b"\xff")])
    with pytest.raises(Invalid, match="有效的 zip"):
        read_uploads([("scripts.zip", b"invalid")])


@pytest.mark.parametrize("name", ["nested.zip", "nested.sql"])
def test_nested_archive_rejected_even_with_changed_extension(name):
    with pytest.raises(Invalid, match="嵌套 zip"):
        read_uploads([("scripts.zip", archive([(name, archive([("first.sql", "SELECT 1")]))]))])


def test_zip_symlink_rejected():
    info = zipfile.ZipInfo("first.sql")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(Invalid, match="符号链接"):
        read_uploads([("scripts.zip", archive([(info, "target.sql")]))])


def test_compression_bomb_is_rejected_by_declared_expanded_size_before_reading(monkeypatch):
    compressed = archive([("first.sql", "x" * 100_000)])
    assert len(compressed) < 1000
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *args, **kwargs: pytest.fail("超限条目不能开始解压"))
    with pytest.raises(Invalid, match="单文件大小上限"):
        read_uploads([("scripts.zip", compressed)], max_file_bytes=1000)


def test_batch_limit_counts_unknown_entries_and_all_uploaded_archives():
    with pytest.raises(Invalid, match="批次大小上限"):
        read_uploads([("first.zip", archive([("first.sql", "SELECT 1")])),
                      ("second.zip", archive([("readme.txt", "x" * 8)]))], max_batch_bytes=15)


def test_zip_entry_limit_includes_directories_and_multiple_archives(monkeypatch):
    monkeypatch.setattr("avelis.importer.archive.MAX_ARCHIVE_ENTRIES", 2)
    with pytest.raises(Invalid, match="zip 条目数"):
        read_uploads([("first.zip", archive([("nested/", ""), ("first.sql", "SELECT 1")])),
                      ("second.zip", archive([("second.sql", "SELECT 2")]))])


def test_unknown_only_files_report_skips_when_no_import_remains():
    with pytest.raises(Invalid, match="没有可导入") as error:
        read_uploads([("readme.txt", b"instructions")])
    assert error.value.detail["notes"] == ["已跳过不支持的文件：readme.txt"]


def test_zip_long_relative_path_keeps_basename_separate_from_source_key():
    name = "/".join(["nested"] * 40 + ["first.sql"])
    files, _ = read_uploads([("scripts.zip", archive([(name, "SELECT 1")]))])
    assert files[0].filename == "first.sql"
    assert files[0].source_key == name


@pytest.mark.parametrize("name,reason", [
    ("x" * 252 + ".sql", "文件名不能超过 255"),
    ("/".join(["nested"] * 80 + ["first.sql"]), "来源键不能超过 512"),
])
def test_names_exceeding_database_field_limits_are_rejected_before_persistence(name, reason):
    with pytest.raises(Invalid, match=reason):
        read_uploads([("scripts.zip", archive([(name, "SELECT 1")]))])


def test_dependency_topology_orders_producers_before_consumers_stably():
    files = ["final.sql", "independent.sql", "second.sql", "first.sql"]
    dependencies = [
        FileDependencies({"cat.stage.second"}, {"cat.dw.daily_summary"}),
        FileDependencies(),
        FileDependencies({"cat.stage.first"}, {"cat.stage.second"}),
        FileDependencies({"cat.src.events"}, {"cat.stage.first"}),
    ]
    ordered, notes = order_files(files, dependencies)
    assert ordered == ["independent.sql", "first.sql", "second.sql", "final.sql"]
    assert not notes


def test_dependency_cycle_keeps_upload_order_inside_component_and_reports_names():
    files = [ImportFile("final.sql", "", "sql"), ImportFile("second.sql", "", "sql"),
             ImportFile("first.sql", "", "sql"), ImportFile("initial.sql", "", "sql")]
    dependencies = [
        FileDependencies({"cat.stage.second"}, {"cat.dw.daily_summary"}),
        FileDependencies({"cat.stage.first"}, {"cat.stage.second"}),
        FileDependencies({"cat.stage.second", "cat.stage.initial"}, {"cat.stage.first"}),
        FileDependencies(set(), {"cat.stage.initial"}),
    ]
    ordered, notes = order_files(files, dependencies)
    assert [file.filename for file in ordered] == ["initial.sql", "second.sql", "first.sql", "final.sql"]
    assert notes == ["文件读写依赖存在环，环内保持上传顺序：second.sql、first.sql"]


def test_dependency_chain_larger_than_python_recursion_limit():
    files = list(range(1500))
    dependencies = [FileDependencies({str(index + 1)}, {str(index)}) for index in files]
    ordered, notes = order_files(files, dependencies)
    assert ordered == list(reversed(files))
    assert not notes


def test_self_read_is_not_a_cross_file_cycle():
    assert order_files(["first.sql"], [FileDependencies({"cat.dw.rows"}, {"cat.dw.rows"})]) == (["first.sql"], [])

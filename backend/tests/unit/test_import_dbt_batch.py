"""使用内存会话验证 dbt manifest 上传和批次重建。"""

import pytest

from avelis.connectors.base import sha256_text
from avelis.governance.errors import Invalid
from avelis.importer import batch
from avelis.importer.archive import read_uploads
from avelis.models import ImportJob, ImportSource
from tests.unit.test_import_archive import archive
from tests.unit.test_import_batch_service import memory as memory
from tests.unit.test_import_dbt import manifest, node


def _upload_manifest(memory, content):
    files, notes = read_uploads([("target/manifest.json", content.encode("utf-8"))])
    imported, _ = batch.create_batch(memory.session, memory.actor, datasource_id="ds", files=files, notes=notes)
    return imported


def test_uploaded_manifest_is_stored_as_sql_with_dbt_source_type(memory):
    content = manifest(node("model.p.summary", relation_name="dw.summary", code="SELECT id FROM src.events;"))
    imported = _upload_manifest(memory, content)
    (job,) = memory.rows[ImportJob]
    (source,) = memory.rows[ImportSource]
    assert job.source_key == "target/manifest.json"
    assert job.language == "sql"
    assert job.source_type == "dbt"
    assert source.job_id == job.id
    assert source.content == (
        "-- dbt model model.p.summary (models/summary.sql)\nCREATE TABLE dw.summary AS\nSELECT id FROM src.events\n;\n"
    )
    assert job.content_hash == source.content_hash == sha256_text(source.content)
    assert imported.notes == ["已从 dbt manifest 转换 1 个模型（适配器：postgres）"]


def test_zip_recognizes_manifest_and_skips_other_json():
    content = manifest(node("model.p.summary", relation_name="dw.summary"))
    zipped = archive([("target/manifest.json", content), ("target/catalog.json", "{}")])
    files, notes = read_uploads([("dbt.zip", zipped)])

    (file,) = files
    assert file.source_key == "target/manifest.json"
    assert file.language == "dbt"
    assert file.content == content
    assert notes == ["已跳过不支持的文件：target/catalog.json"]


def test_uncompiled_manifest_raises_invalid_when_creating_batch(memory):
    content = manifest(node("model.p.summary", relation_name="dw.summary", code=None))
    with pytest.raises(Invalid, match="请先运行 dbt compile 或 dbt build") as exc:
        _upload_manifest(memory, content)
    assert exc.value.status_code == 422
    assert not memory.rows.get(ImportJob)
    assert not memory.rows.get(ImportSource)


def test_set_params_preserves_dbt_source_type_and_sql(memory):
    content = manifest(node("model.p.summary", relation_name="dw.${target}", code="SELECT id FROM src.events"))
    original = _upload_manifest(memory, content)
    batch.run_import(original.id)
    assert original.status == "needs_params"

    rewritten = batch.set_params(memory.session, memory.actor, original, {"target": "summary"})
    old_job, new_job = memory.rows[ImportJob]
    old_source, new_source = memory.rows[ImportSource]
    assert rewritten.id != original.id
    assert new_job.batch_id == rewritten.id
    assert new_job.filename == old_job.filename == "manifest.json"
    assert new_job.language == old_job.language == "sql"
    assert new_job.source_type == old_job.source_type == "dbt"
    assert new_source.job_id == new_job.id
    assert new_source.content == old_source.content
    assert new_source.content_hash == old_source.content_hash

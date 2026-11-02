"""Ossie 锁定结构、兼容边界与模型静态引用。"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing.exceptions import NoSuchResource

from avelis.interchange.constants import OSSIE_SPEC_COMMIT, OSSIE_SPEC_COMMIT_SHA, OSSIE_SPEC_VERSION
from avelis.interchange.validation import (
    _deny_remote_schema,
    _validator,
    iter_metrics,
    normalize_document,
    validate_model,
    validate_structure,
)


def pinned_schema(commit):
    schema_file = Path(__file__).parents[2] / "src" / "avelis" / "interchange" / f"ossie_schema_{commit}.json"
    return json.loads(schema_file.read_text(encoding="utf-8"))


def expression(text, dialect="postgres"):
    return {"dialects": [{"dialect": dialect, "expression": text}]}


@pytest.fixture
def document():
    return {
        "version": OSSIE_SPEC_VERSION,
        "name": "reference_model",
        "datasets": [
            {
                "name": "samples",
                "source": "lab.public.samples",
                "fields": [
                    {"name": "sample_id", "expression": expression("sample_id")},
                    {"name": "reading", "expression": expression("reading")},
                ],
            },
            {
                "name": "stations",
                "source": "lab.public.stations",
                "fields": [{"name": "station_id", "expression": expression("station_id")}],
            },
        ],
        "relationships": [
            {
                "name": "sample_station",
                "from": "samples",
                "to": "stations",
                "from_columns": ["sample_id"],
                "to_columns": ["station_id"],
            }
        ],
        "metrics": [{"name": "reading_total", "expression": expression("SUM(samples.reading)")}],
    }


def test_pinned_schema_commit_and_license():
    assert OSSIE_SPEC_COMMIT == "48da55a"
    assert OSSIE_SPEC_COMMIT_SHA == "48da55a07e4e8c3a11a662b14727557f6342536d"
    assert OSSIE_SPEC_VERSION == "0.2.0.dev0"
    for commit in ("48da55a", "88e0011"):
        schema = pinned_schema(commit)
        assert commit in schema["$comment"] and "Apache-2.0" in schema["$comment"]


@pytest.mark.parametrize(
    ("commit", "expected_sha256"),
    [
        ("48da55a", "4c081608d194b0da5b965100ddd68203f53b1f61d292857b7694da381ce6e5d0"),
        ("88e0011", "74a25eb5256b1ebfbc6f0f12aa73a72778b46abe0f603ba9ffde58f2418d99c7"),
    ],
)
def test_bundled_schemas_preserve_canonical_upstream_content(commit, expected_sha256):
    # 上游原文：48da55a/core-spec/ossie-schema.json、88e0011/core-spec/osi-schema.json。
    # 忽略唯一允许添加的根 $comment；哈希固定在测试中，不依赖外部下载目录。
    schema = pinned_schema(commit)
    schema.pop("$comment")
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == expected_sha256


def test_validator_uses_both_pinned_schemas_without_constraint_rewrites():
    assert _validator(False).schema == pinned_schema("48da55a")
    assert _validator(True).schema == pinned_schema("88e0011")


@pytest.mark.parametrize(
    "filename", ["flights.semantic_model.yaml", "tpcds_semantic_model.yaml", "renamed_fields.yaml"]
)
def test_official_examples_pass_unmodified_schema_and_model_validation(filename):
    fixture = Path(__file__).parents[1] / "fixtures" / "ossie" / filename
    example = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    Draft202012Validator(pinned_schema("48da55a")).validate(example)
    assert validate_structure(example) == []
    assert validate_model(example) == []
    normalized = normalize_document(example)
    assert normalized == example and normalized is not example
    assert normalized["version"] == OSSIE_SPEC_VERSION
    assert validate_structure(normalized) == []


def test_root_and_dataset_metrics_accept_free_case_sensitive_dialects(document):
    document["datasets"][0]["metrics"] = [{"name": "sample_count", "expression": expression("COUNT(*)")}]
    document["metrics"][0]["expression"]["dialects"].extend(
        [
            {"dialect": "VendorLanguage", "expression": "custom::value [not sql]"},
            {"dialect": "vendorlanguage", "expression": "different syntax"},
            {"dialect": "OSSIE_SQL_2026", "expression": "SUM(samples.reading)"},
        ]
    )
    original = deepcopy(document)
    Draft202012Validator(pinned_schema("48da55a")).validate(document)
    assert validate_structure(document) == []
    assert validate_model(document) == []
    assert [(scope, metric["name"], path) for scope, metric, path in iter_metrics(document)] == [
        (None, "reading_total", "$.metrics[0]"),
        ("samples", "sample_count", "$.datasets[0].metrics[0]"),
    ]
    normalized = normalize_document(document)
    normalized["datasets"][0]["name"] = "changed"
    assert document == original


def test_sql_shorthand_is_authoritative_over_portable_dialect_entry(document):
    document["metrics"][0]["expression"] = {
        "sql": "SUM(samples.reading)",
        "dialects": [{"dialect": "OSSIE_SQL_2026", "expression": "SUM(missing_dataset.no_column)"}],
    }
    Draft202012Validator(pinned_schema("48da55a")).validate(document)
    assert validate_structure(document) == []
    assert validate_model(document) == []
    document["metrics"][0]["expression"]["sql"] = "SUM(samples.missing)"
    errors = validate_model(document)
    assert len(errors) == 1 and errors[0]["path"] == "$.metrics[0].expression.sql"


@pytest.mark.parametrize("metric_expression", [{"sql": "SUM(reading)"}, expression("SUM(reading)", "OSSIE_SQL_2026")])
def test_portable_model_metric_requires_qualified_field_names(document, metric_expression):
    document["metrics"][0]["expression"] = metric_expression
    assert validate_structure(document) == []
    assert validate_model(document)


@pytest.mark.parametrize(
    "text", ["SUM(samples.reading)", "SUM(stations.station_id)", "SUM(station_id)", "reading_total"]
)
def test_portable_dataset_metric_resolves_only_unqualified_own_fields(document, text):
    metric = {"name": "sample_total", "expression": {"sql": "SUM(reading)"}}
    document["datasets"][0]["metrics"] = [metric]
    assert validate_structure(document) == []
    assert validate_model(document) == []
    metric["expression"]["sql"] = text
    errors = validate_model(document)
    assert len(errors) == 1 and errors[0]["path"] == "$.datasets[0].metrics[0].expression.sql"


def test_dataset_fields_and_metrics_share_one_namespace(document):
    document["datasets"][0]["metrics"] = [{"name": "READING", "expression": {"sql": "SUM(reading)"}}]
    assert validate_structure(document) == []
    errors = validate_model(document)
    assert len(errors) == 1 and errors[0]["path"] == "$.datasets[0].metrics[0].name"


def test_schema_returns_all_errors_with_paths(document):
    document["version"] = "0.1"
    document["datasets"][0]["fields"][0]["datatype"] = "bigint"
    document["metrics"][0]["expression"]["dialects"] = []
    document["metrics"][0]["custom_extensions"] = [{"vendor_name": "independent", "data": {"key": True}}]
    errors = validate_structure(document)
    assert len(errors) == 4
    assert {error["layer"] for error in errors} == {"structure"}
    assert {error["path"] for error in errors} == {
        "$.version",
        "$.datasets[0].fields[0].datatype",
        "$.metrics[0].expression.dialects",
        "$.metrics[0].custom_extensions[0].data",
    }
    assert all(error["reason"] for error in errors)


@pytest.mark.parametrize(
    "datatype", ["String", "Integer", "Decimal", "Float", "Boolean", "Date", "Time", "DateTime", "DateTimeTz", "Opaque"]
)
def test_portable_datatypes_and_time_role_are_independent(document, datatype):
    field = document["datasets"][0]["fields"][0]
    field.update(datatype=datatype, dimension={"is_time": True})
    document["metrics"][0]["datatype"] = datatype
    Draft202012Validator(pinned_schema("48da55a")).validate(document)
    assert validate_structure(document) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [("primary_key", []), ("unique_keys", []), ("unique_keys", [[]]), ("unique_keys", [["sample_id"], []])],
)
def test_primary_and_unique_keys_require_at_least_one_field(document, key, value):
    document["datasets"][0][key] = value
    assert list(Draft202012Validator(pinned_schema("48da55a")).iter_errors(document))
    assert validate_structure(document)


def test_nonempty_primary_and_unique_keys_are_accepted(document):
    document["datasets"][0].update(primary_key=["sample_id"], unique_keys=[["sample_id"], ["sample_id", "reading"]])
    Draft202012Validator(pinned_schema("48da55a")).validate(document)
    assert validate_structure(document) == []


@pytest.mark.parametrize(
    "context",
    [
        "Use this entity",
        {"instructions": "Group by station", "synonyms": ["readings"], "examples": ["Total reading?"]},
        {"vendor_hint": {"x": 1}},
    ],
)
def test_ai_context_accepts_official_string_and_object_forms(document, context):
    document["ai_context"] = context
    document["datasets"][0]["ai_context"] = context
    document["datasets"][0]["fields"][0]["ai_context"] = context
    document["metrics"][0]["ai_context"] = context
    Draft202012Validator(pinned_schema("48da55a")).validate(document)
    assert validate_structure(document) == []


@pytest.mark.parametrize("context", [None, [], 1, {"instructions": []}, {"synonyms": "readings"}, {"examples": [1]}])
def test_ai_context_known_properties_keep_official_types(document, context):
    document["ai_context"] = context
    assert list(Draft202012Validator(pinned_schema("48da55a")).iter_errors(document))
    assert validate_structure(document)


@pytest.mark.parametrize(
    "extension",
    [
        {"vendor_name": "avelis"},
        {"data": "{}"},
        {"vendor_name": "avelis", "data": {}},
        {"vendor_name": "avelis", "data": "{}", "extra": True},
    ],
)
def test_custom_extensions_keep_required_opaque_string_contract(document, extension):
    document["custom_extensions"] = [extension]
    assert list(Draft202012Validator(pinned_schema("48da55a")).iter_errors(document))
    assert validate_structure(document)


@pytest.mark.parametrize("scope", ["root", "dataset", "field", "metric"])
def test_undeclared_entity_properties_remain_rejected(document, scope):
    target = {
        "root": document,
        "dataset": document["datasets"][0],
        "field": document["datasets"][0]["fields"][0],
        "metric": document["metrics"][0],
    }[scope]
    target["unsupported_property"] = True
    assert list(Draft202012Validator(pinned_schema("48da55a")).iter_errors(document))
    assert validate_structure(document)


def legacy_document(document):
    model = deepcopy(document)
    version = model.pop("version")
    for dataset in model["datasets"]:
        for field in dataset["fields"]:
            field["expression"]["dialects"][0]["dialect"] = "ANSI_SQL"
    model["metrics"][0]["expression"]["dialects"][0]["dialect"] = "ANSI_SQL"
    return {"version": version, "semantic_model": [model]}


def test_legacy_envelope_is_validated_then_normalized_without_dialect_rewrite(document):
    envelope = legacy_document(document)
    original = deepcopy(envelope)
    Draft202012Validator(pinned_schema("88e0011")).validate(envelope)
    assert validate_structure(envelope) == []
    normalized = normalize_document(envelope)
    assert normalized["version"] == OSSIE_SPEC_VERSION and "semantic_model" not in normalized
    assert normalized["metrics"][0]["expression"]["dialects"][0]["dialect"] == "ANSI_SQL"
    Draft202012Validator(pinned_schema("48da55a")).validate(normalized)
    assert validate_model(normalized) == []
    normalized["datasets"][0]["name"] = "changed"
    assert envelope == original


@pytest.mark.parametrize("invalid_legacy", ["free_dialect", "dataset_metrics", "unknown_property"])
def test_legacy_envelope_keeps_original_schema_constraints(document, invalid_legacy):
    envelope = legacy_document(document)
    model = envelope["semantic_model"][0]
    if invalid_legacy == "free_dialect":
        model["metrics"][0]["expression"]["dialects"][0]["dialect"] = "OSSIE_SQL_2026"
    elif invalid_legacy == "dataset_metrics":
        model["datasets"][0]["metrics"] = [deepcopy(model["metrics"][0])]
    else:
        model["unsupported_property"] = True
    assert list(Draft202012Validator(pinned_schema("88e0011")).iter_errors(envelope))
    assert validate_structure(envelope)


def test_legacy_envelope_also_obeys_new_schema_after_normalization(document):
    envelope = legacy_document(document)
    envelope["semantic_model"][0]["datasets"][0]["primary_key"] = []
    Draft202012Validator(pinned_schema("88e0011")).validate(envelope)
    assert validate_structure(envelope)


@pytest.mark.parametrize("bad", [None, [], "reference_model", {"version": OSSIE_SPEC_VERSION}])
def test_non_model_inputs_fail_structure_without_exception(bad):
    assert validate_structure(bad)


@pytest.mark.parametrize("model_count", [0, 2])
def test_legacy_envelope_requires_exactly_one_model(document, model_count):
    envelope = legacy_document(document)
    envelope["semantic_model"] *= model_count
    assert validate_structure(envelope)
    with pytest.raises(ValueError, match="一个语义模型"):
        normalize_document(envelope)


def test_network_schema_loader_is_disabled():
    with pytest.raises(NoSuchResource):
        _deny_remote_schema("https://invalid.example/schema.json")


def test_all_model_duplicate_names_are_reported(document):
    document["datasets"][0]["fields"].append(deepcopy(document["datasets"][0]["fields"][0]))
    document["datasets"].append(deepcopy(document["datasets"][1]))
    document["relationships"].append(deepcopy(document["relationships"][0]))
    document["datasets"][0]["metrics"] = [deepcopy(document["metrics"][0])]
    assert validate_structure(document) == []
    errors = validate_model(document)
    assert {error["path"] for error in errors} == {
        "$.datasets[2].name",
        "$.datasets[0].fields[2].name",
        "$.relationships[1].name",
        "$.datasets[0].metrics[0].name",
    }
    assert {error["layer"] for error in errors} == {"model"}
    assert all("重名" in error["reason"] for error in errors)


def test_relationship_endpoints_columns_and_lengths_all_reported(document):
    relationship = document["relationships"][0]
    relationship["from"] = "missing_dataset"
    relationship["to_columns"] = ["missing_column", "station_id"]
    errors = validate_model(document)
    assert {error["path"] for error in errors} == {
        "$.relationships[0]",
        "$.relationships[0].from",
        "$.relationships[0].to_columns[0]",
    }
    assert any("列数" in error["reason"] for error in errors)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("SUM(missing.reading)", "数据集不存在"),
        ("COUNT(missing.*)", "数据集不存在"),
        ("SUM(absent.samples.reading)", "多段字段的引用无法静态确定"),
        ("SUM(absent.catalog.samples.reading)", "多段字段的引用无法静态确定"),
        ("SUM(samples.missing)", "字段不存在"),
        ("SUM(missing)", "字段或指标不存在"),
        ('SUM("Samples".reading)', "数据集不存在"),
        ('SUM(samples."Reading")', "字段不存在"),
    ],
)
def test_metric_reference_failures(document, text, reason):
    document["metrics"][0]["expression"] = expression(text)
    errors = validate_model(document)
    assert len(errors) == 1
    assert errors[0]["path"] == "$.metrics[0].expression.dialects[0].expression"
    assert reason in errors[0]["reason"]


def test_dataset_metrics_resolve_their_own_fields_and_other_metrics(document):
    document["datasets"][0]["metrics"] = [
        {"name": "reading_average", "expression": expression("SUM(reading) / reading_total")}
    ]
    assert validate_model(document) == []
    document["datasets"][0]["metrics"][0]["expression"] = expression("SUM(station_id)")
    errors = validate_model(document)
    assert len(errors) == 1
    assert errors[0]["path"] == "$.datasets[0].metrics[0].expression.dialects[0].expression"


@pytest.mark.parametrize("text", ["SUM(SAMPLES.READING)", 'SUM("samples"."reading")', "COUNT(*)", "SUM(reading)"])
def test_metric_valid_reference_forms(document, text):
    document["metrics"][0]["expression"] = expression(text)
    assert validate_model(document) == []


def test_unknown_dialects_and_unparseable_snippets_are_deferred(document):
    document["metrics"][0]["expression"] = {
        "dialects": [
            {"dialect": "unrecognized_language", "expression": "SUM(missing_dataset.no_column)"},
            {"dialect": "Postgres", "expression": "SUM(missing_dataset.no_column)"},
            {"dialect": "postgres", "expression": "SUM("},
        ]
    }
    assert validate_structure(document) == []
    assert validate_model(document) == []


@pytest.mark.parametrize("text", ["SUM(reading.value)", "SUM(samples.reading.value)"])
def test_native_struct_attributes_require_maintainer_to_resolve_references(document, text):
    document["metrics"][0]["expression"] = expression(text, "bigquery")
    errors = validate_model(document)
    assert len(errors) == 1
    assert errors[0]["path"] == "$.metrics[0].expression.dialects[0].expression"


@pytest.mark.parametrize(
    "text", ["(SELECT SUM(s.reading) FROM samples AS s)", "(SELECT COUNT(*) FROM missing_dataset)"]
)
def test_native_subquery_requires_maintainer_to_resolve_references(document, text):
    document["metrics"][0]["expression"] = expression(text)
    errors = validate_model(document)
    assert len(errors) == 1
    assert "子查询的引用无法静态确定" in errors[0]["reason"]


def test_derived_external_components_are_visible_only_to_declaring_metric(document):
    derived = document["metrics"][0]
    derived["expression"] = expression("reference_a / reference_b")
    derived["custom_extensions"] = [
        {
            "vendor_name": "avelis",
            "data": json.dumps(
                {
                    "kind": "derived",
                    "components": [
                        {"metric_code": "reference_a", "version_no": 2},
                        {"metric_code": "reference_b", "version_no": 1},
                    ],
                }
            ),
        }
    ]
    assert validate_structure(document) == []
    assert validate_model(document) == []
    document["metrics"].append({"name": "other_metric", "expression": expression("reference_a")})
    errors = validate_model(document)
    assert len(errors) == 1
    assert errors[0]["path"] == "$.metrics[1].expression.dialects[0].expression"


@pytest.mark.parametrize("data", ["invalid", "[]", "null", '{"kind":"derived","components":{}}'])
def test_malformed_component_extensions_do_not_crash_model_validation(document, data):
    metric = document["metrics"][0]
    metric["expression"] = expression("reference_a")
    metric["custom_extensions"] = [{"vendor_name": "avelis", "data": data}]
    errors = validate_model(document)
    assert len(errors) == 1
    assert "reference_a" in errors[0]["reason"]

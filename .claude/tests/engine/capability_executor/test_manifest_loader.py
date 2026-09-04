"""
test_manifest_loader.py — capability_executor.manifest_loader schema 校验。

覆盖：合规 pass；每个必填字段缺失 raise；枚举外 raise；id 格式错 raise；
version 非 semver raise；audit.log_to 缺占位符 raise。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.capability_executor.base import ManifestValidationError
from engine.capability_executor.manifest_loader import (
    DEFAULT_TIMEOUT_S,
    get_timeout_s,
    load_and_validate,
    load_manifest,
    validate_manifest,
)


_VALID_MANIFEST = {
    "id": "web-scraper/crawl",
    "version": "0.1.0",
    "source": "local (P9 PoC)",
    "runtime": {
        "type": "python",
        "entry": "scrape.py --url {{url}}",
        "timeout_s": 60,
    },
    "triggers": ["数据采集"],
    "inputs": [
        {"name": "url", "type": "url", "required": True},
    ],
    "outputs": [
        {"name": "result", "type": "file", "path_pattern": "10-项目/{project}/交付物/scrape.json"},
    ],
    "sandbox": {"allowed_paths": ["10-项目/*/交付物/"]},
    "audit": {"log_to": "20-知识/能力注册表/web-scraper/调用日志/{ts}-{project}.md"},
}


class TestValidateHappy:
    def test_valid_manifest_passes(self):
        validate_manifest(_VALID_MANIFEST)  # 不抛即 pass

    def test_get_timeout_s_from_manifest(self):
        assert get_timeout_s(_VALID_MANIFEST) == 60

    def test_get_timeout_s_default(self):
        m = {"runtime": {}}
        assert get_timeout_s(m) == DEFAULT_TIMEOUT_S


class TestValidateMissingRequired:
    @pytest.mark.parametrize("field", [
        "id", "version", "source", "runtime", "triggers", "inputs", "outputs", "audit",
    ])
    def test_missing_top_level_required_raises(self, field):
        m = dict(_VALID_MANIFEST)
        m.pop(field)
        with pytest.raises(ManifestValidationError, match=field):
            validate_manifest(m)

    def test_missing_runtime_type_raises(self):
        m = dict(_VALID_MANIFEST)
        m["runtime"] = {"entry": "scrape.py"}
        with pytest.raises(ManifestValidationError, match="type"):
            validate_manifest(m)

    def test_missing_runtime_entry_raises(self):
        m = dict(_VALID_MANIFEST)
        m["runtime"] = {"type": "python"}
        with pytest.raises(ManifestValidationError, match="entry"):
            validate_manifest(m)


class TestValidateEnumViolations:
    def test_runtime_type_out_of_enum_raises(self):
        m = dict(_VALID_MANIFEST)
        m["runtime"] = dict(m["runtime"])
        m["runtime"]["type"] = "rust"
        with pytest.raises(ManifestValidationError, match="rust"):
            validate_manifest(m)

    def test_input_type_out_of_enum_raises(self):
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["inputs"][0]["type"] = "unknown_type"
        with pytest.raises(ManifestValidationError, match="unknown_type"):
            validate_manifest(m)

    def test_output_type_out_of_enum_raises(self):
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["outputs"][0]["type"] = "dir"
        with pytest.raises(ManifestValidationError, match="dir"):
            validate_manifest(m)

    @pytest.mark.parametrize("t", ["text", "url", "json"])
    def test_规范里有但引擎没实现的output类型被拒(self, t):
        """`resolve_artifact_paths` 对 text/url/json 直接 continue，注释写
        「executor 从 stdout 拿」—— 而全仓没有任何一处按 output spec 从 stdout
        提取。放行的话：通过校验、跑完、artifact_paths 为空、不报错，声明的产物
        静默不存在。fail-closed 到实现为止。"""
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["outputs"][0]["type"] = t
        with pytest.raises(ManifestValidationError, match="尚未实现"):
            validate_manifest(m)

    def test_file仍然放行(self):
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["outputs"][0]["type"] = "file"
        validate_manifest(m)

    def test_两个真manifest不被本次收窄误伤(self):
        """vault 现有 huashu-design / web-scraper 都是 type=file。"""
        from engine.capability_executor.manifest_loader import (
            _ALLOWED_OUTPUT_TYPES, _SPEC_OUTPUT_TYPES,
        )
        assert _ALLOWED_OUTPUT_TYPES == {"file"}
        assert _ALLOWED_OUTPUT_TYPES < _SPEC_OUTPUT_TYPES

    def test_sandbox_network_out_of_enum_raises(self):
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["sandbox"]["network"] = "half_open"
        with pytest.raises(ManifestValidationError, match="half_open"):
            validate_manifest(m)


class TestValidateFormat:
    @pytest.mark.parametrize("bad_id", [
        "invalid",              # 无 /
        "no-slash-in-name",     # 无 /
        "with space/name",      # 有空格
        "Cap/name",             # 大写
        "root/",                # name 为空
    ])
    def test_bad_id_raises(self, bad_id):
        m = dict(_VALID_MANIFEST)
        m["id"] = bad_id
        with pytest.raises(ManifestValidationError, match="id"):
            validate_manifest(m)

    @pytest.mark.parametrize("bad_version", ["0.1", "v1.0.0", "1.0", "1.0.0-beta"])
    def test_bad_version_raises(self, bad_version):
        m = dict(_VALID_MANIFEST)
        m["version"] = bad_version
        with pytest.raises(ManifestValidationError, match="version"):
            validate_manifest(m)


class TestAuditLogTo:
    def test_audit_log_to_missing_ts_raises(self):
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["audit"]["log_to"] = "path/{project}.md"
        with pytest.raises(ManifestValidationError, match=r"\{ts\}"):
            validate_manifest(m)

    def test_audit_log_to_missing_project_raises(self):
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["audit"]["log_to"] = "path/{ts}.md"
        with pytest.raises(ManifestValidationError, match=r"\{project\}"):
            validate_manifest(m)


class TestLoadManifest:
    def test_load_from_absolute_path(self, tmp_path: Path):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_VALID_MANIFEST), encoding="utf-8")
        loaded = load_manifest(str(path))
        assert loaded["id"] == "web-scraper/crawl"

    def test_load_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ManifestValidationError, match="不存在"):
            load_manifest(str(tmp_path / "no.json"))

    def test_load_bad_json_raises(self, tmp_path: Path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(ManifestValidationError, match="JSON"):
            load_manifest(str(path))


class TestInputsUniqueness:
    def test_duplicate_input_name_raises(self):
        m = json.loads(json.dumps(_VALID_MANIFEST))
        m["inputs"].append({"name": "url", "type": "text", "required": False})
        with pytest.raises(ManifestValidationError, match="同名"):
            validate_manifest(m)

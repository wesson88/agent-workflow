"""
capability_executor/manifest_loader.py — 从 vault 读 manifest.json + schema 校验。

规范：`00-系统/规则/capability注册表规范.md §3`

不引入 jsonschema/pydantic 依赖 —— 手写字段校验，跟 engine/manifest_validator.py 风格一致。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from ..config import VAULT_ROOT
from .base import ManifestValidationError

# 依据：capability 注册表规范 §3.1 强约束（枚举扩展需先改规范再改本文件）
_ALLOWED_RUNTIME_TYPES = frozenset({
    "python", "shell", "node", "http", "claude-code-skill", "mcp",
})
_ALLOWED_INPUT_TYPES = frozenset({
    "text", "file_ref", "file_content", "number", "boolean", "url",
})
_ALLOWED_OUTPUT_TYPES = frozenset({"file", "text", "url", "json"})
_ALLOWED_NETWORK = frozenset({"disabled", "read_only", "enabled"})

# 依据：capability 注册表规范 §3.1 `id` 格式约束
_ID_RE = re.compile(r"^[a-z0-9\-]+\/[^\/\s]+$")
# 依据：规范 §3.1 `version` semver 约束
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# 默认值（依据：规范 §3.2 明标）
DEFAULT_TIMEOUT_S = 300
DEFAULT_NETWORK = "disabled"
DEFAULT_TOKEN_COST_ESTIMATE = 2000

_REGISTRY_SUBDIR = ("20-知识", "能力注册表")


def _err(msg: str) -> ManifestValidationError:
    return ManifestValidationError(msg)


def _check_required(d: dict, keys: list[str], where: str) -> None:
    missing = [k for k in keys if k not in d]
    if missing:
        raise _err(f"{where} 缺必填字段：{missing}")


def _check_enum(value, allowed: frozenset[str], where: str) -> None:
    if value not in allowed:
        raise _err(
            f"{where} 值 '{value}' 非法；允许集：{sorted(allowed)}"
        )


@lru_cache(maxsize=32)
def _load_manifest_cached(path_str: str) -> dict:
    """P10.5 A2：真正读磁盘的内层 helper，按绝对/规范化路径 key 缓存。

    dict 是**不可变**返回给外层的（caller 承诺不改 —— 用于摘要注入/校验/executor
    dispatch 都是只读），避免 deepcopy 开销。

    invalidate：manifest 修改后测试或 CLI 需清 cache → 调 `invalidate_cache()`。
    """
    path = Path(path_str)
    if not path.is_file():
        raise _err(f"manifest 不存在：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise _err(f"manifest {path} JSON 解析失败：{e}") from e


def load_manifest(capability_id_or_path: str) -> dict:
    """从 vault 读 manifest.json（带 lru_cache）。

    支持两种传参：
    - `<root>/<name>` 格式的 capability id（如 `web-scraper/crawl`）→ 从
      `VAULT_ROOT/20-知识/能力注册表/<root>/manifest.json` 加载
    - 直接传绝对 / 相对 Path 字符串（测试用）

    返回原 dict；schema 校验交给 `validate_manifest`。
    """
    path: Path
    if "/" in capability_id_or_path and not capability_id_or_path.endswith(".json"):
        root = capability_id_or_path.split("/", 1)[0]
        path = VAULT_ROOT.joinpath(*_REGISTRY_SUBDIR, root, "manifest.json")
    else:
        path = Path(capability_id_or_path)
    return _load_manifest_cached(str(path))


def invalidate_cache() -> None:
    """P10.5 A2：清 manifest lru_cache（测试 / manifest 修改后 CLI 调）。"""
    _load_manifest_cached.cache_clear()


def validate_manifest(manifest: dict) -> None:
    """fail-closed schema 校验。依据：规范 §3 + §12 不可豁免项。"""
    if not isinstance(manifest, dict):
        raise _err(f"manifest 顶层必须是 dict，实际：{type(manifest).__name__}")

    _check_required(
        manifest,
        ["id", "version", "source", "runtime", "triggers", "inputs", "outputs", "audit"],
        "manifest",
    )

    # id
    mid = manifest["id"]
    if not isinstance(mid, str) or not _ID_RE.match(mid):
        raise _err(
            f"manifest.id '{mid}' 格式非法；须匹配 `<root>/<name>` 例：`web-scraper/crawl`"
        )

    # version
    ver = manifest["version"]
    if not isinstance(ver, str) or not _VERSION_RE.match(ver):
        raise _err(
            f"manifest.version '{ver}' 非 semver；须匹配 `MAJOR.MINOR.PATCH`"
        )

    # source
    if not isinstance(manifest["source"], str) or not manifest["source"].strip():
        raise _err("manifest.source 必须是非空 str")

    # runtime
    runtime = manifest["runtime"]
    if not isinstance(runtime, dict):
        raise _err(f"manifest.runtime 必须是 dict，实际：{type(runtime).__name__}")
    _check_required(runtime, ["type", "entry"], "manifest.runtime")
    _check_enum(runtime["type"], _ALLOWED_RUNTIME_TYPES, "manifest.runtime.type")
    if not isinstance(runtime["entry"], str) or not runtime["entry"].strip():
        raise _err("manifest.runtime.entry 必须是非空 str")
    if "timeout_s" in runtime:
        ts = runtime["timeout_s"]
        if not isinstance(ts, int) or ts <= 0:
            raise _err(f"manifest.runtime.timeout_s '{ts}' 必须是正整数")
    if "deps" in runtime and not isinstance(runtime["deps"], list):
        raise _err("manifest.runtime.deps 必须是 list")
    if "env" in runtime and not isinstance(runtime["env"], dict):
        raise _err("manifest.runtime.env 必须是 dict")

    # triggers
    triggers = manifest["triggers"]
    if not isinstance(triggers, list) or not triggers:
        raise _err("manifest.triggers 必须是非空 list")
    for i, t in enumerate(triggers):
        if not isinstance(t, str) or not t.strip():
            raise _err(f"manifest.triggers[{i}] 必须是非空 str")

    # inputs
    inputs = manifest["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise _err("manifest.inputs 必须是非空 list")
    input_names = set()
    for i, item in enumerate(inputs):
        if not isinstance(item, dict):
            raise _err(f"manifest.inputs[{i}] 必须是 dict")
        _check_required(item, ["name", "type", "required"], f"manifest.inputs[{i}]")
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise _err(f"manifest.inputs[{i}].name 必须是非空 str")
        if name in input_names:
            raise _err(f"manifest.inputs 存在同名字段：'{name}'")
        input_names.add(name)
        _check_enum(item["type"], _ALLOWED_INPUT_TYPES, f"manifest.inputs[{i}].type")
        if not isinstance(item["required"], bool):
            raise _err(f"manifest.inputs[{i}].required 必须是 bool")

    # outputs
    outputs = manifest["outputs"]
    if not isinstance(outputs, list) or not outputs:
        raise _err("manifest.outputs 必须是非空 list")
    for i, item in enumerate(outputs):
        if not isinstance(item, dict):
            raise _err(f"manifest.outputs[{i}] 必须是 dict")
        _check_required(
            item, ["name", "type", "path_pattern"], f"manifest.outputs[{i}]"
        )
        _check_enum(
            item["type"], _ALLOWED_OUTPUT_TYPES, f"manifest.outputs[{i}].type"
        )
        if not isinstance(item["path_pattern"], str) or not item["path_pattern"].strip():
            raise _err(f"manifest.outputs[{i}].path_pattern 必须是非空 str")

    # sandbox（选填但强建议）
    sandbox = manifest.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise _err("manifest.sandbox 必须是 dict")
    if "allowed_paths" in sandbox:
        ap = sandbox["allowed_paths"]
        if not isinstance(ap, list) or not all(isinstance(p, str) for p in ap):
            raise _err("manifest.sandbox.allowed_paths 必须是 list[str]")
    if "network" in sandbox:
        _check_enum(
            sandbox["network"], _ALLOWED_NETWORK, "manifest.sandbox.network"
        )

    # audit
    audit = manifest["audit"]
    if not isinstance(audit, dict):
        raise _err("manifest.audit 必须是 dict")
    if "log_to" not in audit or not isinstance(audit["log_to"], str):
        raise _err("manifest.audit.log_to 必须是 str（含 {ts} + {project} 占位符）")
    log_to = audit["log_to"]
    if "{ts}" not in log_to or "{project}" not in log_to:
        raise _err(
            f"manifest.audit.log_to 必须含 `{{ts}}` 和 `{{project}}` 占位符；实际：'{log_to}'"
        )

    # token_cost_estimate
    if "token_cost_estimate" in manifest:
        tce = manifest["token_cost_estimate"]
        if not isinstance(tce, int) or tce < 0:
            raise _err(f"manifest.token_cost_estimate '{tce}' 必须是非负 int")


def load_and_validate(capability_id_or_path: str) -> dict:
    """便捷入口：读文件 → schema 校验 → 返回 dict。invoke.py 首选入口。"""
    manifest = load_manifest(capability_id_or_path)
    validate_manifest(manifest)
    return manifest


def get_timeout_s(manifest: dict) -> int:
    """从 manifest 读 runtime.timeout_s，缺失回退默认 300s。依据：规范 §3.2。"""
    return int(manifest.get("runtime", {}).get("timeout_s", DEFAULT_TIMEOUT_S))

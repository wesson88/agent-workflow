"""
capability_executor/invoke.py — CLI 入口。

用法：
  python -m engine.capability_executor.invoke \
    --id web-scraper/crawl \
    --project pain-radar \
    --input url=https://example.com \
    --input max_pages=1

流程：manifest_loader → sandbox 校验 → executor 分派 → audit_writer

exit code：
  0 → success（exit_code == 0）
  1 → capability 执行失败（exit_code != 0）
  2 → manifest / sandbox / runtime 前置错误（fail-closed）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# 让 `python -m engine.capability_executor.invoke` 能正常解析 relative imports
_ENGINE_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_ENGINE_PARENT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_PARENT))
# skills 也放 path 便于 audit_writer 双写 append_audit
_SKILLS_DIR = _ENGINE_PARENT / "skills"
if _SKILLS_DIR.is_dir() and str(_SKILLS_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILLS_DIR))

# Windows utf-8 保底
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from engine.capability_executor.audit_writer import write_audit  # noqa: E402
from engine.capability_executor.base import (  # noqa: E402
    CapabilityExecutorError,
    ExecutorResult,
    ManifestValidationError,
    RuntimeMismatchError,
    SandboxViolationError,
)
from engine.capability_executor.executors import get_executor  # noqa: E402
from engine.capability_executor.manifest_loader import load_and_validate  # noqa: E402
from engine.capability_executor.sandbox import (  # noqa: E402
    assert_path_within,
    get_sandbox_allowed,
)


def _parse_input_kv(pairs: list[str]) -> dict[str, str]:
    """把 CLI --input key=val 列表解析成 dict。value 保留原字符串（executor 层处理类型转换）。"""
    out: dict[str, str] = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise SystemExit(f"--input '{kv}' 缺 '='；正确格式：key=val")
        k, v = kv.split("=", 1)
        k = k.strip()
        if not k:
            raise SystemExit(f"--input '{kv}' key 为空")
        out[k] = v
    return out


def _check_required_inputs(manifest: dict, inputs: dict[str, Any]) -> list[str]:
    """返回缺失的必填输入 name 列表。"""
    missing: list[str] = []
    for spec in manifest.get("inputs") or []:
        if spec.get("required") and spec["name"] not in inputs:
            missing.append(spec["name"])
    return missing


def _apply_input_defaults(manifest: dict, inputs: dict[str, Any]) -> dict[str, Any]:
    """给未传的 optional input 填 default（如果 manifest 里声明了）。"""
    filled = dict(inputs)
    for spec in manifest.get("inputs") or []:
        name = spec["name"]
        if name not in filled and "default" in spec:
            filled[name] = spec["default"]
    return filled


def _validate_input_paths(manifest: dict, inputs: dict[str, Any]) -> None:
    """对 type=file_ref 的输入做 sandbox 校验。"""
    allowed = get_sandbox_allowed(manifest)
    for spec in manifest.get("inputs") or []:
        if spec.get("type") == "file_ref":
            name = spec["name"]
            if name in inputs:
                assert_path_within(
                    inputs[name],
                    allowed,
                    label=f"inputs.{name}",
                )


def invoke_capability(
    capability_id: str,
    project: str,
    inputs: dict[str, Any],
    *,
    token_consumed: int | None = None,
) -> ExecutorResult:
    """程序化调用入口（tests / role_loader 摘要 / 未来 API 都从这里进）。

    流程等价于 CLI；raise CapabilityExecutorError 系列表示前置错误。
    """
    manifest = load_and_validate(capability_id)
    inputs_filled = _apply_input_defaults(manifest, inputs)
    missing = _check_required_inputs(manifest, inputs_filled)
    if missing:
        raise ManifestValidationError(
            f"必填输入缺失：{missing}（manifest.inputs 里 required=true 但 --input 未传）"
        )
    _validate_input_paths(manifest, inputs_filled)
    executor = get_executor(manifest["runtime"]["type"])
    result = executor.invoke(manifest, inputs_filled, project)
    write_audit(
        manifest, project, inputs_filled, result, token_consumed=token_consumed
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="调用一次 capability（读 vault manifest → executor 分派 → 落 audit）",
        prog="capability_executor.invoke",
    )
    parser.add_argument(
        "--id", required=True,
        help="capability id，格式 `<root>/<name>`（如 web-scraper/crawl）",
    )
    parser.add_argument(
        "--project", required=True,
        help="项目名，用于展开 path_pattern / audit.log_to 里的 `{project}` 占位符",
    )
    parser.add_argument(
        "--input", action="append", dest="inputs", default=[],
        help="能力输入，格式 key=val；可多次指定",
    )
    parser.add_argument(
        "--token-consumed", type=int, default=None, dest="token_consumed",
        help="（可选）本次调用 LLM 端 token 数，用于 audit.token_consumed",
    )
    args = parser.parse_args(argv)

    inputs_kv = _parse_input_kv(args.inputs)
    try:
        result = invoke_capability(
            args.id, args.project, inputs_kv,
            token_consumed=args.token_consumed,
        )
    except (ManifestValidationError, SandboxViolationError, RuntimeMismatchError) as e:
        print(f"❌ 前置错误：{e}", file=sys.stderr)
        return 2
    except CapabilityExecutorError as e:
        print(f"❌ capability_executor 错误：{e}", file=sys.stderr)
        return 2

    # 打印结果
    print(f"capability_id: {args.id}")
    print(f"project      : {args.project}")
    print(f"exit_code    : {result.exit_code}")
    print(f"duration_s   : {round(result.duration_s, 3)}")
    if result.artifact_paths:
        print("artifact_paths:")
        for p in result.artifact_paths:
            print(f"  - {p}")
    else:
        print("artifact_paths: (none)")
    if result.error:
        print(f"error        : {result.error}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

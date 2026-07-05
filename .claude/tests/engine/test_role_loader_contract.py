"""
test_role_loader_contract.py — P5a 契约解析（影子模式）单元测试

覆盖：
- `_resolve_contract` 按 fields.default 展开首个/legacy_directives template
- `_assert_contract_matches_declared` 用 T* 抽象化比对 outputs 与 template 展开
- `_build_role` 集成路径：
  - TL（真契约化角色）加载后 resolved_output_contract 非 None + assert 通过
  - PM / Architect / Backend / Frontend（未契约化）resolved_output_contract 保持 None
  - 破坏 contract schema（fields 空 / 占位符未 declared / template 不匹配）→ raise

依赖真实 vault 里的 TL / PM 角色（frontmatter 已 P4 契约化）。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from engine.role_loader import (
    ContractSchemaError,
    ResolvedContract,
    _abstract_module_id,
    _assert_contract_matches_declared,
    _build_role,
    _resolve_contract,
    invalidate_cache,
    load_role,
)


# ── 单元测试：_abstract_module_id ────────────────────────────
class TestAbstractModuleId:
    """T01 / T0N / T{n} / {module_id_template} 归一为 T*。"""

    @pytest.mark.parametrize("raw,expected", [
        ("10-项目/{project}/指令/给后端-T01.md", "10-项目/PROJECT/指令/给后端-T*.md"),
        ("10-项目/{project}/指令/给后端-T0N.md", "10-项目/PROJECT/指令/给后端-T*.md"),
        ("10-项目/{project}/指令/给后端-T{n}.md", "10-项目/PROJECT/指令/给后端-T*.md"),
        ("10-项目/{project}/指令/给后端-{module_id_template}.md",
         "10-项目/PROJECT/指令/给后端-T*.md"),
        # 索引 / 非模块化路径原样保留（除 {project} 归一）
        ("10-项目/{project}/指令/给后端-索引.md", "10-项目/PROJECT/指令/给后端-索引.md"),
        ("10-项目/{project}/模块清单.md", "10-项目/PROJECT/模块清单.md"),
    ])
    def test_abstract(self, raw, expected):
        assert _abstract_module_id(raw) == expected


# ── 单元测试：_resolve_contract ──────────────────────────────
class TestResolveContract:
    """契约解析：fields.default 展开 + 选 template + 校验 schema。"""

    def _base_contract(self) -> dict:
        return {
            "parameterizable": True,
            "fields": {
                "artifacts_pattern": {
                    "type": "enum",
                    "values": ["legacy_directives", "module_manifest"],
                    "default": "legacy_directives",
                },
                "module_id_template": {
                    "type": "string",
                    "default": "T{n}",
                },
            },
            "templates": {
                "legacy_directives": {
                    "outputs": [
                        "10-项目/{project}/指令/给后端-{module_id_template}.md",
                        "10-项目/{project}/指令/给后端-索引.md",
                    ]
                },
                "module_manifest": {
                    "outputs": [
                        "10-项目/{project}/模块清单.md",
                    ]
                },
            },
        }

    def test_legacy_directives_preferred(self):
        resolved = _resolve_contract(
            self._base_contract(), "output_contract", "测试角色"
        )
        assert resolved.template_name == "legacy_directives"
        # {module_id_template} 展开为 default T{n}
        assert "10-项目/{project}/指令/给后端-T{n}.md" in resolved.outputs
        assert "10-项目/{project}/指令/给后端-索引.md" in resolved.outputs

    def test_first_template_when_no_legacy_and_no_artifacts_field(self):
        """无 artifacts_pattern field + 无 legacy_directives → 走"首个 template"兜底。"""
        contract = self._base_contract()
        del contract["templates"]["legacy_directives"]
        del contract["fields"]["artifacts_pattern"]
        resolved = _resolve_contract(contract, "output_contract", "测试角色")
        assert resolved.template_name == "module_manifest"

    def test_artifacts_pattern_default_selects_template(self):
        """fields.artifacts_pattern.default = X → 选 template X（无 override 时）。"""
        contract = self._base_contract()
        contract["fields"]["artifacts_pattern"]["default"] = "module_manifest"
        resolved = _resolve_contract(contract, "output_contract", "测试角色")
        assert resolved.template_name == "module_manifest"

    def test_field_values_include_defaults(self):
        resolved = _resolve_contract(
            self._base_contract(), "output_contract", "测试角色"
        )
        assert resolved.field_values["artifacts_pattern"] == "legacy_directives"
        assert resolved.field_values["module_id_template"] == "T{n}"

    def test_missing_parameterizable_raises(self):
        contract = self._base_contract()
        contract["parameterizable"] = False
        with pytest.raises(ContractSchemaError, match="parameterizable"):
            _resolve_contract(contract, "output_contract", "测试角色")

    def test_empty_fields_raises(self):
        contract = self._base_contract()
        contract["fields"] = {}
        with pytest.raises(ContractSchemaError, match="fields 为空"):
            _resolve_contract(contract, "output_contract", "测试角色")

    def test_empty_templates_raises(self):
        contract = self._base_contract()
        contract["templates"] = {}
        with pytest.raises(ContractSchemaError, match="templates 为空"):
            _resolve_contract(contract, "output_contract", "测试角色")

    def test_undeclared_placeholder_raises(self):
        contract = self._base_contract()
        contract["templates"]["legacy_directives"]["outputs"].append(
            "10-项目/{project}/{unknown_field}.md"
        )
        with pytest.raises(ContractSchemaError, match=r"占位符 \{unknown_field\}"):
            _resolve_contract(contract, "output_contract", "测试角色")

    def test_builtin_placeholders_allowed(self):
        """{project} / {n} / {title_slug} / {ts} 等 builtin 不需 declared。"""
        contract = self._base_contract()
        contract["templates"]["module_manifest"]["outputs"] = [
            "10-项目/{project}/审计/{ts}-{title_slug}.md",
        ]
        # 不应抛异常
        resolved = _resolve_contract(contract, "output_contract", "测试角色")
        assert resolved.template_name == "legacy_directives"

    def test_input_contract_uses_inputs_key(self):
        contract = self._base_contract()
        # 换成 input_contract 语义：template 里应是 inputs
        contract["templates"] = {
            "legacy_directives": {
                "inputs": [
                    "10-项目/{project}/指令/给后端-{module_id_template}.md",
                ]
            }
        }
        resolved = _resolve_contract(contract, "input_contract", "测试角色")
        assert resolved.outputs == (
            "10-项目/{project}/指令/给后端-T{n}.md",
        )


# ── 单元测试：_assert_contract_matches_declared ──────────────
class TestAssertContractMatches:
    """T* 抽象化后集合比对：允许 T01/T0N 冗余、T{n} 参数化。"""

    def _resolved(self, outputs: tuple[str, ...]) -> ResolvedContract:
        return ResolvedContract(
            template_name="legacy_directives",
            field_values={},
            outputs=outputs,
        )

    def test_abstract_equivalent_passes(self):
        resolved = self._resolved((
            "10-项目/{project}/指令/给后端-T{n}.md",
            "10-项目/{project}/指令/给后端-索引.md",
        ))
        declared = (
            "10-项目/{project}/指令/给后端-T01.md",
            "10-项目/{project}/指令/给后端-T0N.md",
            "10-项目/{project}/指令/给后端-索引.md",
        )
        # 抽象化后集合都是 {给后端-T*.md, 给后端-索引.md} → 通过
        _assert_contract_matches_declared(
            "output_contract", resolved, declared, "测试角色"
        )

    def test_missing_declared_path_raises(self):
        resolved = self._resolved((
            "10-项目/{project}/指令/给后端-T{n}.md",
        ))
        declared = (
            "10-项目/{project}/指令/给后端-T01.md",
            "10-项目/{project}/指令/给前端-T01.md",  # template 未覆盖
        )
        with pytest.raises(
            ContractSchemaError, match="未被 template 展开覆盖"
        ):
            _assert_contract_matches_declared(
                "output_contract", resolved, declared, "测试角色"
            )

    def test_extra_template_path_raises(self):
        resolved = self._resolved((
            "10-项目/{project}/指令/给后端-T{n}.md",
            "10-项目/{project}/指令/给前端-T{n}.md",  # declared 中不存在
        ))
        declared = (
            "10-项目/{project}/指令/给后端-T01.md",
        )
        with pytest.raises(
            ContractSchemaError, match="template 展开出 declared 未声明"
        ):
            _assert_contract_matches_declared(
                "output_contract", resolved, declared, "测试角色"
            )

    def test_empty_declared_and_empty_resolved_passes(self):
        _assert_contract_matches_declared(
            "output_contract", self._resolved(()), (), "测试角色"
        )


# ── 集成测试：真实 vault 角色加载 ─────────────────────────────
class TestRoleLoaderIntegration:
    """P4 已契约化角色（TL）加载后必须带 resolved_output_contract。"""

    def setup_method(self):
        invalidate_cache()

    def test_tl_loaded_with_resolved_contract(self):
        role = load_role("技术主管")
        assert role.resolved_output_contract is not None
        rc = role.resolved_output_contract
        assert rc.template_name == "legacy_directives"
        # default 展开：{module_id_template} → T{n}
        assert any("给后端-T{n}.md" in p for p in rc.outputs)
        assert any("给后端-索引.md" in p for p in rc.outputs)
        assert any("给前端-T{n}.md" in p for p in rc.outputs)
        assert any("给前端-索引.md" in p for p in rc.outputs)
        # input_contract 未声明（TL 只写 output_contract）
        assert role.resolved_input_contract is None

    def test_backend_loaded_with_resolved_input_contract(self):
        """P7：Backend 加了 input_contract shadow declaration，加载后 resolved 非 None。"""
        role = load_role("后端工程师")
        assert role.resolved_input_contract is not None
        rc = role.resolved_input_contract
        assert rc.template_name == "legacy_directives"
        # default 展开：{module_id_template} → T{n}
        assert any("给后端-T{n}.md" in p for p in rc.outputs)
        assert any("给后端-索引.md" in p for p in rc.outputs)
        # 通用输入也在 legacy template 里
        assert any("系统设计.md" in p for p in rc.outputs)
        assert any("技术栈.md" in p for p in rc.outputs)
        # output_contract 未声明（Backend 只写 input_contract）
        assert role.resolved_output_contract is None

    def test_frontend_loaded_with_resolved_input_contract(self):
        """P7：Frontend 同款 shadow declaration。"""
        role = load_role("前端工程师")
        assert role.resolved_input_contract is not None
        rc = role.resolved_input_contract
        assert rc.template_name == "legacy_directives"
        assert any("给前端-T{n}.md" in p for p in rc.outputs)
        assert any("给前端-索引.md" in p for p in rc.outputs)
        assert any("API契约.md" in p for p in rc.outputs)
        assert role.resolved_output_contract is None

    @pytest.mark.parametrize("role_name", ["产品经理", "架构师"])
    def test_non_contract_roles_have_no_resolved(self, role_name):
        """P7 后 PM/Architect 仍未契约化（按 §11.9 判定豁免）。"""
        role = load_role(role_name)
        assert role.resolved_output_contract is None
        assert role.resolved_input_contract is None


# ── 集成测试：破坏 frontmatter 触发 raise ─────────────────────
class TestBrokenFrontmatterRaises:
    """构造 broken 角色 frontmatter，验证 _build_role 契约校验路径。

    monkeypatch role_loader.read_note 绕过 obsidian_io 的 VAULT_ROOT 边界校验，
    直接把合成的角色内容喂给 _build_role。
    """

    @staticmethod
    def _make_role_content(frontmatter_yaml: str) -> str:
        return textwrap.dedent(f"""\
            ---
            {frontmatter_yaml}
            ---

            # 角色：测试影子契约

            ## 1. 核心使命
            测试用。

            <!-- DYNAMIC_START -->
            <!-- DYNAMIC_END -->
            """)

    def _run_build_role_with_stub_content(
        self, monkeypatch, frontmatter_yaml: str,
    ) -> None:
        from engine import role_loader
        content = self._make_role_content(frontmatter_yaml.strip())
        monkeypatch.setattr(role_loader, "read_note", lambda _p: content)
        # note_path 只用作 dataclass 字段值，不影响加载路径
        _build_role(Path("角色-测试影子契约.md"))

    def test_contract_declared_but_outputs_missing_raises(self, monkeypatch):
        """契约声明 legacy 但 outputs 只声明前端 → 抽象化后集合不同 → raise。"""
        fm = """
            role: 测试影子契约
            aliases: []
            domain: 测试
            model: claude-sonnet-4-6
            max_tokens: 4096
            style: 测试
            outputs:
              - '10-项目/{project}/指令/给前端-T01.md'
            output_contract:
              parameterizable: true
              fields:
                module_id_template:
                  type: string
                  default: 'T{n}'
              templates:
                legacy_directives:
                  outputs:
                    - '10-项目/{project}/指令/给后端-{module_id_template}.md'
                    - '10-项目/{project}/指令/给后端-索引.md'
        """
        with pytest.raises(ContractSchemaError, match="不等价"):
            self._run_build_role_with_stub_content(monkeypatch, fm)

    def test_contract_missing_fields_raises(self, monkeypatch):
        fm = """
            role: 测试影子契约
            aliases: []
            domain: 测试
            model: claude-sonnet-4-6
            max_tokens: 4096
            style: 测试
            outputs: []
            output_contract:
              parameterizable: true
              fields: {}
              templates:
                legacy_directives:
                  outputs: ['10-项目/{project}/foo.md']
        """
        with pytest.raises(ContractSchemaError, match="fields 为空"):
            self._run_build_role_with_stub_content(monkeypatch, fm)

    def test_contract_undeclared_placeholder_raises(self, monkeypatch):
        fm = """
            role: 测试影子契约
            aliases: []
            domain: 测试
            model: claude-sonnet-4-6
            max_tokens: 4096
            style: 测试
            outputs: ['10-项目/{project}/foo.md']
            output_contract:
              parameterizable: true
              fields:
                dummy:
                  type: string
                  default: 'x'
              templates:
                legacy_directives:
                  outputs:
                    - '10-项目/{project}/{unknown_field}.md'
        """
        with pytest.raises(ContractSchemaError, match=r"占位符 \{unknown_field\}"):
            self._run_build_role_with_stub_content(monkeypatch, fm)


# ── P5b 写入模式：load_role(name, contract_overrides) ──────────
class TestLoadRoleWithOverrides:
    """P5b：workflow 层 opt-in 契约参数时 role_loader 用 overrides 替换 outputs。"""

    def setup_method(self):
        invalidate_cache()

    def test_no_overrides_uses_declared_outputs(self):
        """无 overrides → P5a 影子模式，role.outputs 保持硬编码字段值。"""
        role = load_role("技术主管")
        assert "10-项目/{project}/指令/给后端-T01.md" in role.outputs
        assert "10-项目/{project}/指令/给后端-T0N.md" in role.outputs
        assert "10-项目/{project}/指令/给前端-索引.md" in role.outputs
        # 未 opt-in 时不含 module_manifest 路径
        assert not any("模块清单.md" in p for p in role.outputs)

    def test_module_manifest_override_replaces_outputs(self):
        """artifacts_pattern=module_manifest → role.outputs 被 template 替换。"""
        role = load_role(
            "技术主管",
            contract_overrides={
                "output_contract": {"artifacts_pattern": "module_manifest"},
            },
        )
        # 新路径进入
        assert any("模块清单.md" in p for p in role.outputs)
        assert any("模块/T{n}-{title_slug}.md" in p for p in role.outputs)
        # 旧路径完全消失
        assert not any("给后端-T01.md" in p for p in role.outputs)
        assert not any("给前端-索引.md" in p for p in role.outputs)
        # resolved 契约字段也应带 module_manifest 语义
        assert role.resolved_output_contract is not None
        assert role.resolved_output_contract.template_name == "module_manifest"
        assert role.resolved_output_contract.field_values["artifacts_pattern"] == "module_manifest"

    def test_module_id_template_override(self):
        """module_id_template override 应影响路径展开。"""
        role = load_role(
            "技术主管",
            contract_overrides={
                "output_contract": {
                    "artifacts_pattern": "module_manifest",
                    "module_id_template": "T{n}-{title_slug}",
                },
            },
        )
        # module_id_template 从 "T{n}" 覆盖为 "T{n}-{title_slug}"
        # 因 module_manifest template 里已含 {module_id_template}-{title_slug}
        # 展开后 {title_slug} 出现两次（一次来自 template，一次来自 override）
        assert any("T{n}-{title_slug}-{title_slug}.md" in p for p in role.outputs)

    def test_unknown_field_override_raises(self):
        """override 中的 field 未在契约 fields 声明 → raise。"""
        with pytest.raises(
            ContractSchemaError, match="unknown_field.*未在契约 fields 声明"
        ):
            load_role(
                "技术主管",
                contract_overrides={
                    "output_contract": {"unknown_field": "x"},
                },
            )

    def test_unknown_template_override_raises(self):
        """artifacts_pattern 指向未在 templates 声明的 template → raise。"""
        with pytest.raises(
            ContractSchemaError, match="template 'nonexistent_template' 未在契约"
        ):
            load_role(
                "技术主管",
                contract_overrides={
                    "output_contract": {"artifacts_pattern": "nonexistent_template"},
                },
            )

    def test_overrides_on_non_contract_role_raises(self):
        """非契约化角色（PM）传 output_contract overrides → raise（防误用）。"""
        with pytest.raises(
            ContractSchemaError, match="未声明 output_contract"
        ):
            load_role(
                "产品经理",
                contract_overrides={
                    "output_contract": {"artifacts_pattern": "any"},
                },
            )

    def test_empty_overrides_dict_treated_as_no_overrides(self):
        """空 dict overrides 语义等同 None → 走影子模式。"""
        role = load_role(
            "技术主管",
            contract_overrides={},
        )
        # 走 P5a 影子模式，outputs 保持硬编码
        assert "10-项目/{project}/指令/给后端-T01.md" in role.outputs


# ── P5b workflow yaml step 解析 ────────────────────────────────
class TestWorkflowStepContractOverrides:
    """WorkflowStep.from_yaml 解析 contract_overrides 字段。"""

    def test_step_parses_contract_overrides(self):
        from engine.workflow import WorkflowStep
        step = WorkflowStep.from_yaml({
            "role": "技术主管",
            "contract_overrides": {
                "output_contract": {"artifacts_pattern": "module_manifest"},
            },
        })
        assert step.type == "linear"
        assert step.role == "技术主管"
        assert step.contract_overrides == {
            "output_contract": {"artifacts_pattern": "module_manifest"},
        }

    def test_step_default_no_contract_overrides(self):
        from engine.workflow import WorkflowStep
        step = WorkflowStep.from_yaml("技术主管")
        assert step.contract_overrides is None

    def test_step_dict_no_contract_overrides(self):
        from engine.workflow import WorkflowStep
        step = WorkflowStep.from_yaml({"role": "技术主管"})
        assert step.contract_overrides is None

    def test_step_invalid_contract_overrides_type_raises(self):
        from engine.workflow import WorkflowStep
        with pytest.raises(ValueError, match="contract_overrides 必须是 dict"):
            WorkflowStep.from_yaml({
                "role": "技术主管",
                "contract_overrides": "not a dict",
            })

    def test_step_end_to_end_with_load_role(self):
        """workflow yaml step → contract_overrides → load_role → 替换 outputs。"""
        from engine.workflow import WorkflowStep
        step = WorkflowStep.from_yaml({
            "role": "技术主管",
            "contract_overrides": {
                "output_contract": {"artifacts_pattern": "module_manifest"},
            },
        })
        role = load_role(step.role, contract_overrides=step.contract_overrides)
        assert any("模块清单.md" in p for p in role.outputs)
        assert not any("给后端-T01.md" in p for p in role.outputs)


# ── P7 端到端：Backend/Frontend input_contract 写入模式 ────────
class TestBackendFrontendInputContractOverrides:
    """P7：Backend/Frontend 的 input_contract 支持 P5b 写入模式。"""

    def setup_method(self):
        invalidate_cache()

    def test_backend_module_manifest_override_replaces_inputs(self):
        """artifacts_pattern=module_manifest → Backend inputs 走模块清单形态。"""
        role = load_role(
            "后端工程师",
            contract_overrides={
                "input_contract": {"task_source": "module_manifest"},
            },
        )
        assert any("模块清单.md" in p for p in role.inputs)
        assert any("模块/T{n}-{title_slug}.md" in p for p in role.inputs)
        # 通用输入保留
        assert any("系统设计.md" in p for p in role.inputs)
        assert any("PRD.md" in p for p in role.inputs)
        # legacy 输入完全消失
        assert not any("给后端-T01.md" in p for p in role.inputs)
        assert not any("给后端-索引.md" in p for p in role.inputs)

    def test_frontend_module_manifest_override_replaces_inputs(self):
        role = load_role(
            "前端工程师",
            contract_overrides={
                "input_contract": {"task_source": "module_manifest"},
            },
        )
        assert any("模块清单.md" in p for p in role.inputs)
        assert any("模块/T{n}-{title_slug}.md" in p for p in role.inputs)
        # API 契约保留
        assert any("API契约.md" in p for p in role.inputs)
        assert not any("给前端-T01.md" in p for p in role.inputs)

    def test_backend_no_overrides_baseline(self):
        """无 overrides → 影子模式，inputs 保持硬编码字段值。"""
        role = load_role("后端工程师")
        assert "10-项目/{project}/指令/给后端-T01.md" in role.inputs
        assert "10-项目/{project}/指令/给后端-索引.md" in role.inputs
        assert not any("模块清单.md" in p for p in role.inputs)

    def test_backend_unknown_field_raises(self):
        with pytest.raises(
            ContractSchemaError, match="unknown_field.*未在契约 fields 声明"
        ):
            load_role(
                "后端工程师",
                contract_overrides={
                    "input_contract": {"unknown_field": "x"},
                },
            )

    def test_backend_wrong_contract_type_raises(self):
        """Backend 只有 input_contract；传 output_contract overrides → raise。"""
        with pytest.raises(
            ContractSchemaError, match="未声明 output_contract"
        ):
            load_role(
                "后端工程师",
                contract_overrides={
                    "output_contract": {"artifacts_pattern": "module_manifest"},
                },
            )

    def test_workflow_step_end_to_end_backend(self):
        """workflow yaml step 传 input_contract overrides → Backend inputs 替换。"""
        from engine.workflow import WorkflowStep
        step = WorkflowStep.from_yaml({
            "role": "后端工程师",
            "contract_overrides": {
                "input_contract": {"task_source": "module_manifest"},
            },
        })
        role = load_role(step.role, contract_overrides=step.contract_overrides)
        assert any("模块清单.md" in p for p in role.inputs)

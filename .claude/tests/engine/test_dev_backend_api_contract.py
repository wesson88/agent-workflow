"""
test_dev_backend_api_contract.py — API契约 必产指令（使命-行为漂移修复）

背景（2026-07-19，[[产物注册表v0.4-fail全量化-2026-07-19]]）：API契约 曾放在
render_required_outputs 示例清单里，被"上面是路径**示例**"整体降级 →
6 次真实成功跑零产出。修法：独立 mandate 块，与示例清单分离。

覆盖：mandate 文本含 vault 路径 + "必产"强调 + 接口汇总要求。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
sys.path.insert(0, str(_SKILLS_DIR))


def _import_backend_main():
    path = _SKILLS_DIR / "dev_backend" / "main.py"
    spec = importlib.util.spec_from_file_location("dev_backend_main_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestApiContractMandate:
    def test_mandate_contains_vault_path_and_emphasis(self):
        mod = _import_backend_main()
        text = mod._render_api_contract_mandate("demo")
        assert "10-项目/demo/API契约.md" in text
        assert "必产" in text
        assert "不是示例" in text
        assert "全部" in text  # 汇总全部接口，不只本轮

    def test_mandate_mentions_contract_essentials(self):
        mod = _import_backend_main()
        text = mod._render_api_contract_mandate("p")
        for kw in ("方法", "路径", "错误码", "鉴权"):
            assert kw in text

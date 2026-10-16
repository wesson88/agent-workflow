"""
test_video_workflow.py — 视频域 W2 工作流编排（2026-10-17）

覆盖随各段落地递增：契约层（in_process 角色进图 / 视频产物注册表）→ 执行层 → 生成循环 → 批判者回路 → 模板端到端。
"""

from __future__ import annotations

import pytest


class TestContractLayer:
    def test_in_process_role_without_skill_dir_builds_node(self):
        """视频角色无 .claude/skills/ 目录，make_role_node 不再抛错（原来建图即失败）。"""
        from engine.graph.nodes import make_role_node

        node = make_role_node("分镜师", True, domain="video")
        assert node.__name__ == "node_分镜师"

    def test_subprocess_role_without_skill_dir_still_raises(self, monkeypatch):
        import engine.graph.nodes as nodes

        class R:
            executor = "subprocess"

        monkeypatch.setattr(nodes, "load_role", lambda n: R())
        monkeypatch.setattr(nodes, "role_to_skill_dir", lambda n: (_ for _ in ()).throw(ValueError("x")))
        with pytest.raises(ValueError):
            nodes.make_role_node("某角色", True)

    def test_video_registry_resolves_to_video_root(self):
        from engine.artifact_registry import load_config, load_registry

        reg, roots = load_registry(), load_config()
        assert reg["视频创作简报"].resolve(roots, "p") == "10-项目/video/p/inputs/创作简报.md"
        assert reg["创作简报"].domain == "music"          # 音乐域条目不受影响
        assert reg["样片审阅单"].resolve(roots, "p") == "10-项目/video/p/审阅/{n}.审阅.md"

    def test_video_genes_shadow_check_clean(self):
        from engine.artifact_registry import shadow_check_role
        from engine.role_loader import load_role

        for name in ("视频编剧", "分镜师", "美术指导", "配音导演", "prompt工程师", "样片审阅", "剪辑师"):
            r = load_role(name)
            assert shadow_check_role(r.name, r.produces, r.consumes, r.outputs, r.inputs) == [], name

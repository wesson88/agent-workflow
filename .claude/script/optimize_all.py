import subprocess
import os

skills_chain = [
    "product_manager",     # 业务需求 → requirements/PRD.md
    "chief_architect",     # PRD → 系统设计 + 技术主管任务
    "technical_lead",      # 拆分前后端任务
    "dev_backend",
    "dev_frontend",
]
for skill in skills_chain:
    env = os.environ.copy()
    env["TARGET_SKILL"] = skill
    result = subprocess.run(["python", "script/workflow.py"], env=env)
    if result.returncode != 0:
        print(f"优化 {skill} 失败，终止。")
        break

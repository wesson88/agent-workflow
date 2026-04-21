#!/usr/bin/env python3
"""
workflow.py - 元技能优化循环（增强版）

职责：
- 执行目标技能（或子技能）的测试任务
- 分析执行日志，若失败则生成补丁并更新目标技能的 skill.md
- 支持递归优化，直至成功或达到最大深度
- 提供原子写入、版本备份、审计日志等可靠性机制
- 状态管理：严格遵循状态机，blocked 技能跳过执行，联动全局状态
"""

import json
import shutil
import os
import re
import subprocess
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

# ================== 配置（可通过环境变量覆盖） ==================
BASE_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = BASE_DIR.parent
SKILLS_DIR = PROJECT_ROOT / "skills"

TARGET_SKILL = os.getenv("TARGET_SKILL", "chief_architect")
TARGET_SKILL_PATH = SKILLS_DIR / TARGET_SKILL / "skill.md"

LOG_PATH = BASE_DIR / "execution.log"
STATUS_PATH = PROJECT_ROOT / "status.json"
AUDIT_LOG_PATH = PROJECT_ROOT / "audit.jsonl"

BACKUP_DIR = Path.home() / ".claude/skill_versions"
BACKUP_RETENTION = 30

DYNAMIC_START = "<!-- DYNAMIC_START -->"
DYNAMIC_END = "<!-- DYNAMIC_END -->"

TEST_COMMAND = os.getenv("TEST_COMMAND", "").split()
# 全局阻塞联动开关：当技能 blocked 时是否将 system_state 设为 blocked
SET_GLOBAL_BLOCK = os.getenv("SET_GLOBAL_BLOCK", "false").lower() == "true"
# =============================================================

# 定义允许的状态转换（当前技能可主动触发的转换）
ALLOWED_TRANSITIONS = {
    "idle": ["busy"],
    "busy": ["success", "failed", "blocked"],
    "failed": ["busy", "idle"],          # busy：重试；idle：人工重置
    "blocked": [],                        # blocked 只能由上级/人工变为 idle
    "success": ["idle"],
    "monitoring": ["monitoring"]          # 监控角色保持不变
}

def utc_now():
    """返回 UTC 时间的 ISO 格式字符串（带 Z）"""
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def load_status():
    """加载系统状态，若文件不存在或损坏则返回符合新版 schema 的默认状态"""
    default_status = {
        "schema_version": "1.1",
        "project_name": "My-App-Dev",
        "system_state": "normal",
        "block_reason": None,
        "recursion_config": {
            "max_depth": 5,
            "current_depth": 0,
            "halt_on_failure": True,
            "consecutive_failures_limit": 2
        },
        "skill_registry": {
            "product_manager": {
                "status": "idle",
                "version": "1.0.0",
                "last_run": None,
                "last_patch_timestamp": None,
                "error_count": 0,
                "consecutive_failures": 0
            },
            "chief_architect": {
                "status": "monitoring",
                "version": "1.0.0",
                "last_run": None,
                "last_patch_timestamp": None,
                "error_count": 0
            },
            "technical_lead": {
                "status": "idle",
                "version": "1.0.0",
                "last_run": None,
                "error_count": 0,
                "consecutive_failures": 0
            },
            "dev_backend": {
                "version": "1.0.0",
                "status": "idle",
                "last_run": None,
                "error_count": 0,
                "consecutive_failures": 0,
                "last_output_path": "src/backend/"
            },
            "dev_frontend": {
                "version": "1.0.0",
                "status": "idle",
                "last_run": None,
                "error_count": 0,
                "consecutive_failures": 0,
                "last_output_path": "src/frontend/"
            }
        },
        "active_tasks": [],
        "audit_log": [],
        "status_enum": [
            "idle", "busy", "success", "failed", "blocked", "monitoring"
        ]
    }
    try:
        with open(STATUS_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_status

def save_status(status):
    """原子化写入 status.json"""
    with NamedTemporaryFile('w', dir=STATUS_PATH.parent, delete=False, encoding='utf-8') as tf:
        json.dump(status, tf, ensure_ascii=False, indent=2)
        temp_name = tf.name
    os.replace(temp_name, STATUS_PATH)

def append_audit_log(entry):
    """向审计日志文件追加一条记录（JSON Lines格式）"""
    with open(AUDIT_LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')

def backup_skill():
    """备份当前 skill.md 并清理旧备份"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now().replace(':', '-').replace('Z', '')  # 文件系统友好
    backup_path = BACKUP_DIR / f"{TARGET_SKILL}_{timestamp}.md"
    shutil.copy(TARGET_SKILL_PATH, backup_path)

    backups = sorted(BACKUP_DIR.glob(f"{TARGET_SKILL}_*.md"), key=os.path.getmtime, reverse=True)
    for old_backup in backups[BACKUP_RETENTION:]:
        old_backup.unlink()

    return str(backup_path)

def calculate_rule_hash(rule_text):
    return hashlib.sha256(rule_text.strip().encode('utf-8')).hexdigest()

def extract_existing_rules(content):
    """从 skill.md 内容中提取现有的动态区域规则，返回规则列表"""
    pattern = re.compile(f"{re.escape(DYNAMIC_START)}(.*?){re.escape(DYNAMIC_END)}", re.DOTALL)
    match = pattern.search(content)
    if not match:
        return []
    rules_section = match.group(1)
    lines = rules_section.split('\n')
    rules = []
    current_rule = []
    for line in lines:
        if line.strip().startswith('- ') or line.strip().startswith('# Patch'):
            if current_rule:
                rules.append('\n'.join(current_rule).strip())
                current_rule = []
            current_rule.append(line.rstrip())
        else:
            if current_rule:
                current_rule.append(line.rstrip())
    if current_rule:
        rules.append('\n'.join(current_rule).strip())
    return rules

def update_skill_with_patch(file_path, new_patch):
    """累加式更新 skill.md（去重、保留历史）"""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    existing_rules = extract_existing_rules(content)
    existing_hashes = {calculate_rule_hash(rule) for rule in existing_rules}

    new_patch_clean = new_patch.strip()
    new_hash = calculate_rule_hash(new_patch_clean)
    if new_hash in existing_hashes:
        print("ℹ️ 补丁规则已存在（基于哈希），跳过更新。")
        return False

    for rule in existing_rules:
        if new_patch_clean in rule:
            print("ℹ️ 补丁规则已存在（内容包含），跳过更新。")
            return False

    timestamp = utc_now()
    combined_rules = "\n\n".join(existing_rules + [f"# Patch [{timestamp}]:\n{new_patch_clean}"])
    new_section = f"{DYNAMIC_START}\n{combined_rules}\n{DYNAMIC_END}"

    pattern = re.compile(f"{re.escape(DYNAMIC_START)}.*?{re.escape(DYNAMIC_END)}", re.DOTALL)
    if pattern.search(content):
        new_content = pattern.sub(new_section, content)
    else:
        new_content = content + f"\n\n## 自动优化记录\n{new_section}\n"

    with NamedTemporaryFile('w', dir=file_path.parent, delete=False, encoding='utf-8') as tf:
        tf.write(new_content)
        temp_name = tf.name
    os.replace(temp_name, file_path)
    print(f"✅ 补丁已写入 {file_path}")
    return True

def generate_patch_from_log(log_msg):
    """根据日志内容生成补丁指令（示例规则）"""
    if "数学" in log_msg:
        return "- 强制规范：数学公式必须使用 $$ 符号包裹，并严格校验 LaTeX 语法。"
    elif "权限" in log_msg or "Permission" in log_msg:
        return "- 强制规范：所有文件写入前必须先检查目录权限并创建必要路径。"
    elif "超时" in log_msg:
        return "- 强制规范：增加操作超时处理，避免长时间阻塞。"
    return f"- 优化点：修复以下错误逻辑 - {log_msg[:100]}"

def update_skill_status(status, skill_name, updates):
    """
    安全更新技能状态字段，仅更新存在的字段，并检查状态转换合法性。
    返回 True 表示更新成功（含跳过），False 表示非法转换。
    """
    if skill_name not in status["skill_registry"]:
        print(f"⚠️ 技能 {skill_name} 不存在于注册表，无法更新")
        return False

    skill = status["skill_registry"][skill_name]

    # 状态转换合法性检查
    if "status" in updates:
        new_status = updates["status"]
        old_status = skill.get("status")
        if old_status and new_status not in ALLOWED_TRANSITIONS.get(old_status, []):
            print(f"⛔ 非法状态转换: {old_status} -> {new_status}，操作被拒绝")
            return False

    # 仅更新技能对象中已存在的字段
    for key, value in updates.items():
        if key in skill:
            skill[key] = value
        else:
            print(f"⚠️ 技能 {skill_name} 无字段 '{key}'，跳过更新")
    return True

def run_skill(task, sub_skill=None, use_test_cmd=False):
    """执行目标技能（或子技能）的测试"""
    if use_test_cmd and TEST_COMMAND:
        cmd = TEST_COMMAND.copy()
    else:
        cmd = ["python", str(SKILLS_DIR / TARGET_SKILL / "main.py"), "--task", task]
        if sub_skill:
            cmd.extend(["--sub-skill", sub_skill])

    print(f"🛠 正在运行: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=int(os.getenv("SKILL_TIMEOUT", "300")))
        success = (result.returncode == 0)
        output_log = result.stdout if success else result.stderr
    except subprocess.TimeoutExpired:
        success = False
        output_log = "进程执行超时（30秒）"
    except Exception as e:
        success = False
        output_log = f"进程异常终止: {str(e)}"

    execution_data = {"success": success, "log": output_log}
    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(execution_data, f, ensure_ascii=False)
    return execution_data

def main():
    task = os.getenv("TASK", "处理数学分析")
    max_iter = int(os.getenv("MAX_ITER", "3"))

    status = load_status()

    # 确保目标技能在注册表中存在，并填充基本字段
    if TARGET_SKILL not in status["skill_registry"]:
        print(f"➕ 技能 {TARGET_SKILL} 不存在，自动添加默认条目")
        # 从默认模板复制基础字段（若技能有特殊字段，需外部补充）
        status["skill_registry"][TARGET_SKILL] = {
            "status": "idle",
            "version": "1.0.0",
            "last_run": None,
            "error_count": 0,
            "consecutive_failures": 0
        }

    # 检查技能是否 blocked
    current_status = status["skill_registry"][TARGET_SKILL].get("status")
    if current_status == "blocked":
        print(f"⛔ 技能 {TARGET_SKILL} 当前为 blocked 状态，无法自动执行。请上级技能或人工介入解除阻塞。")
        return

    # 重置递归深度（新的一轮优化）
    status["recursion_config"]["current_depth"] = 0
    save_status(status)

    for i in range(max_iter):
        depth = i + 1
        status["recursion_config"]["current_depth"] = depth

        now = utc_now()
        # 尝试切换到 busy
        if not update_skill_status(status, TARGET_SKILL, {"status": "busy", "last_run": now}):
            # 如果转换非法（极少见），直接终止
            break
        save_status(status)

        print(f"\n🌀 递归轮次 {depth}/{max_iter} (目标技能: {TARGET_SKILL})")

        res = run_skill(task, sub_skill=None, use_test_cmd=True)

        if res["success"]:
            print("✅ 验证通过：任务成功！")
            # 成功：重置错误计数，状态置为 success 再转 idle
            update_skill_status(status, TARGET_SKILL, {
                "status": "success",
                "error_count": 0,
                "consecutive_failures": 0
            })
            save_status(status)
            update_skill_status(status, TARGET_SKILL, {"status": "idle"})
            save_status(status)
            break

        # 失败处理
        print(f"❌ 验证失败，开始反思并打补丁...")
        backup_path = backup_skill()
        patch = generate_patch_from_log(res["log"])
        patched = update_skill_with_patch(TARGET_SKILL_PATH, patch)

        # 如果成功打了补丁，更新 last_patch_timestamp（若字段存在）
        if patched:
            update_skill_status(status, TARGET_SKILL, {"last_patch_timestamp": now})

        # 更新错误计数
        skill = status["skill_registry"][TARGET_SKILL]
        error_count = skill.get("error_count", 0) + 1
        consecutive = skill.get("consecutive_failures", 0) + 1
        updates = {
            "status": "failed",
            "error_count": error_count,
            "consecutive_failures": consecutive,
            "last_run": now
        }
        update_skill_status(status, TARGET_SKILL, updates)

        # 检查是否达到连续失败限制，若达到则置为 blocked
        limit = status["recursion_config"].get("consecutive_failures_limit", 2)
        if consecutive >= limit:
            print(f"⚠️ 连续失败次数 {consecutive} 达到限制，技能状态置为 blocked")
            update_skill_status(status, TARGET_SKILL, {"status": "blocked"})
            if SET_GLOBAL_BLOCK:
                status["system_state"] = "blocked"
                status["block_reason"] = f"{TARGET_SKILL} 连续失败 {consecutive} 次"

        # 无论如何都保存状态
        save_status(status)

        # 记录审计日志（独立文件），使用 try-finally 确保尽量写入
        audit_entry = {
            "timestamp": now,
            "depth": depth,
            "skill": TARGET_SKILL,
            "action": "patch_applied" if patched else "patch_skipped",
            "reason": res["log"][:200],
            "backup": backup_path,
            "patch": patch
        }
        try:
            append_audit_log(audit_entry)
        except Exception as e:
            print(f"⚠️ 审计日志写入失败: {e}")

    else:
        # 循环自然结束（达到最大深度且未成功）
        print("🛑 达到最大深度，优化终止。")
        update_skill_status(status, TARGET_SKILL, {"status": "failed"})
        save_status(status)

if __name__ == "__main__":
    main()
"""
ab_gemini_music.py — Gemini flash vs pro 在 music 域作词/作曲上的 A/B 对比

目的：决定 music 域作词、作曲角色基因 model 字段换 gemini 时选哪档。
       gemini-2.5-flash (FREE key) vs gemini-2.5-pro (PAID key)
       人工对比 4 份输出后定。

测试项目：成为父亲那年（vault：10-项目/music/成为父亲那年/）

调用配对：
  作词 × flash, 作词 × pro
  作曲 × flash, 作曲 × pro

作曲固定喂现存词作.md（vault 既有），避免 "flash 作词差 → flash 作曲也差" 的传导污染。
                                  这样作曲的差距才纯粹反映作曲环节的模型差异。

输出：
  tmp/ab_gemini_music/词作-flash.md
  tmp/ab_gemini_music/词作-pro.md
  tmp/ab_gemini_music/曲作-flash.md
  tmp/ab_gemini_music/曲作-pro.md
  tmp/ab_gemini_music/_report.md      —— 字符 / tokens / 延迟 + 4 份输出文件清单
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
VAULT = Path(os.environ.get("VAULT_ROOT", r"D:\MarkDown\memory\adam"))
PROJECT = "成为父亲那年"

# 输出目录：vault 99-临时 下按日期分子目录，方便 Obsidian Graph View 关联
OUT_DIR = VAULT / "99-临时" / f"AB-gemini-music-{time.strftime('%Y-%m-%d')}"

ROLE_FILES = {
    "作词": VAULT / "00-系统/角色基因/music/角色-作词.md",
    "作曲": VAULT / "00-系统/角色基因/music/角色-作曲.md",
}

PROJECT_ROOT = VAULT / "10-项目/music" / PROJECT
BRIEF = PROJECT_ROOT / "inputs" / "创作简报.md"
VISION = PROJECT_ROOT / "创作 vision.md"
LYRIC_INSTR = PROJECT_ROOT / "指令" / "给作词.md"
COMPOSE_INSTR = PROJECT_ROOT / "指令" / "给作曲.md"
EXISTING_LYRIC = PROJECT_ROOT / "词作.md"

MODELS = {
    "flash": {"name": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY_FREE"},
    "pro":   {"name": "gemini-2.5-pro",   "key_env": "GEMINI_API_KEY_PAID"},
}

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_frontmatter(md: str) -> str:
    """去掉 YAML frontmatter，只留正文（system prompt 不需要 frontmatter）。"""
    if not md.startswith("---"):
        return md
    end = md.find("\n---", 4)
    return md[end + 4:].lstrip("\n") if end != -1 else md


def build_lyric_prompt() -> tuple[str, str]:
    """返回 (system_prompt, user_prompt) 给作词角色。"""
    system = strip_frontmatter(read(ROLE_FILES["作词"]))
    user = (
        f"# 项目：{PROJECT}\n\n"
        f"## 创作简报\n\n{read(BRIEF)}\n\n"
        f"## 创作 vision\n\n{read(VISION)}\n\n"
        f"## 指令：给作词\n\n{read(LYRIC_INSTR)}\n\n"
        "---\n\n请按角色基因 §6 工作流 + §9 输出格式，产出完整词作。"
    )
    return system, user


def build_compose_prompt() -> tuple[str, str]:
    """返回 (system_prompt, user_prompt) 给作曲角色。固定喂现存词作。"""
    system = strip_frontmatter(read(ROLE_FILES["作曲"]))
    user = (
        f"# 项目：{PROJECT}\n\n"
        f"## 创作简报\n\n{read(BRIEF)}\n\n"
        f"## 创作 vision\n\n{read(VISION)}\n\n"
        f"## 词作（已固定，不要重写）\n\n{read(EXISTING_LYRIC)}\n\n"
        f"## 指令：给作曲\n\n{read(COMPOSE_INSTR)}\n\n"
        "---\n\n请按角色基因 §6 工作流 + §9 输出格式，产出完整曲作 + Suno-prompt 两段。"
    )
    return system, user


def call(model_cfg: dict, system: str, user: str, max_tokens: int) -> dict:
    """调用 Gemini，返回 {text, prompt_tokens, completion_tokens, total_tokens, latency_ms}。"""
    from openai import OpenAI
    key = os.environ[model_cfg["key_env"]]
    client = OpenAI(api_key=key, base_url=BASE_URL)
    t0 = time.perf_counter()
    resp = client.chat.completions.create(
        model=model_cfg["name"],
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    choice = resp.choices[0]
    text = choice.message.content or ""
    usage = resp.usage
    return {
        "text": text,
        "finish_reason": choice.finish_reason,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "latency_ms": elapsed,
    }


def run_one(role: str, variant: str, system: str, user: str, max_tokens: int) -> dict:
    """跑一次，写到文件，返回 meta 给 report 汇总。"""
    print(f"  [{role}/{variant}] 调用中... ", end="", flush=True)
    try:
        r = call(MODELS[variant], system, user, max_tokens)
        out_path = OUT_DIR / f"{role}-{variant}.md"
        header = (
            f"<!-- model={MODELS[variant]['name']} | key={MODELS[variant]['key_env']} "
            f"| usage={r['prompt_tokens']}+{r['completion_tokens']}={r['total_tokens']} tokens "
            f"| latency={r['latency_ms']:.0f}ms | finish={r['finish_reason']} -->\n\n"
        )
        out_path.write_text(header + r["text"], encoding="utf-8")
        print(
            f"OK {r['latency_ms']:.0f}ms, "
            f"out_chars={len(r['text'])}, "
            f"out_tokens={r['completion_tokens']}, "
            f"finish={r['finish_reason']}"
        )
        r["role"] = role
        r["variant"] = variant
        r["out_path"] = out_path
        return r
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {type(e).__name__}: {str(e)[:200]}")
        return {
            "role": role, "variant": variant, "error": str(e)[:500],
            "latency_ms": 0, "out_path": None,
        }


def write_report(results: list[dict]) -> Path:
    lines = [
        f"# A/B 对比报告：Gemini flash vs pro on music 域\n",
        f"项目：{PROJECT}",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n",
        "## 调用汇总\n",
        "| 角色 | 变体 | 模型 | finish | prompt tok | output tok | total tok | 延迟 (s) | 输出字符 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        if "error" in r:
            lines.append(
                f"| {r['role']} | {r['variant']} | {MODELS[r['variant']]['name']} "
                f"| ERROR | - | - | - | - | {r['error'][:80]} |"
            )
            continue
        lines.append(
            f"| {r['role']} | {r['variant']} | {MODELS[r['variant']]['name']} "
            f"| {r['finish_reason']} "
            f"| {r['prompt_tokens']} | {r['completion_tokens']} | {r['total_tokens']} "
            f"| {r['latency_ms']/1000:.2f} | {len(r['text'])} |"
        )
    lines += [
        "\n## 输出文件\n",
        "对比时人工打开下列文件对照阅读：\n",
    ]
    for r in results:
        if r.get("out_path"):
            lines.append(f"- `{r['out_path'].relative_to(VAULT)}` (vault 内相对路径)")
    lines += [
        "\n## 决策口径\n",
        "- 看 4 份 md 是否符合 [[产物schema]] §3/§4/§5 结构（章节完整、字段齐）",
        "- 看作词：意象具体化 / 押韵服务情感 / 副歌钩子是否抽象空洞 / 是否说教",
        "- 看作曲：旋律段落对比 / 和弦走向是否套路化 / Suno-prompt 是否可执行",
        "- 同一角色 flash vs pro，若 pro 优势明显 → 该角色用 pro；若差不多 → 用 flash 省钱",
    ]
    p = OUT_DIR / "_report.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def main() -> int:
    load_dotenv(REPO / ".env")
    for k in ("GEMINI_API_KEY_FREE", "GEMINI_API_KEY_PAID"):
        if not os.environ.get(k):
            print(f"[error] .env 缺 {k}", file=sys.stderr)
            return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    lyric_sys, lyric_user = build_lyric_prompt()
    compose_sys, compose_user = build_compose_prompt()

    print(f"作词 prompt: system={len(lyric_sys)} chars, user={len(lyric_user)} chars")
    print(f"作曲 prompt: system={len(compose_sys)} chars, user={len(compose_user)} chars")
    print()

    # max_tokens 注意：Gemini OpenAI 兼容层里 max_tokens 把 reasoning token 也算入；
    # pro 思考更重，比 flash 更容易被卡。第一次跑 4096/8192 三处 finish=length，调大重跑。
    # 角色基因里 max_tokens 分别是 4096 / 8192（按 claude 设定），Gemini 需放宽 2-3x。
    results: list[dict] = []
    print("作词 A/B：")
    results.append(run_one("词作", "flash", lyric_sys, lyric_user, max_tokens=16384))
    results.append(run_one("词作", "pro",   lyric_sys, lyric_user, max_tokens=16384))
    print()
    print("作曲 A/B：")
    results.append(run_one("曲作", "flash", compose_sys, compose_user, max_tokens=16384))
    results.append(run_one("曲作", "pro",   compose_sys, compose_user, max_tokens=16384))
    print()

    report = write_report(results)
    print(f"汇总报告：vault:{report.relative_to(VAULT)}")

    any_fail = any("error" in r for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())

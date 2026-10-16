"""
video_gen — 视频域执行层（PRD §5.4 分层铁律 / §5.5 adapter 抽象）

角色基因只产契约（prompt 包、表单、时间线），二进制产物由执行层生成。本包是工作流调执行层的
唯一入口：`run_task(GenTask) -> GenResult`。

- 完成判据 = 期望文件在不在（ASSET_ROOT 下按 glob），与执行方式无关——所以人工 UI、模拟、
  真实 API 三种 adapter 可互换，工作流重跑时已完成的任务零成本跳过
- adapter 选择：环境变量 `VIDEO_GEN_ADAPTER_{KIND}` > `VIDEO_GEN_ADAPTER` > `human_ui`
  - human_ui：写任务卡 + 落人工闸，用户按卡在工具网页里生成、放到指定路径后 approve
  - mock：直接造占位素材（ffmpeg 测试图/静音），用于测试与整链空跑
  - 真实 API adapter（kling / seedance / tts …）开通 key 后在此注册，工作流不用改
"""

from .base import ADAPTERS, GenResult, GenTask, missing_outputs, register_adapter, run_task

__all__ = ["ADAPTERS", "GenResult", "GenTask", "missing_outputs", "register_adapter", "run_task"]

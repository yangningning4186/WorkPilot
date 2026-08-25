"""产品提示词的分层与证据契约。

这些断言不锁整段文案，只钉住从 OpenWorker / DeepTutor 借来的结构：有名分块、明确完成条件、
模式与 Persona 不扩大权限，以及所有后台 JSON 任务把输入当数据而不是指令。
"""

from app.cowork.environment import render_workspace_files_block
from app.cowork.memory_extraction import (
    CLASSIFICATION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
)
from app.cowork.prompt_blocks import PromptBlock, render_prompt_blocks
from app.cowork.runtime import COWORK_COMPACTION_PROMPTS, _system_prompt
from app.cowork.skills.distillation import DISTILLATION_SYSTEM_PROMPT
from app.cowork.subagent import READONLY_SUBAGENT_SYSTEM_PROMPT
from app.cowork.work_modes import render_work_mode_block
from app.docedit import _PROPOSAL_SYSTEM_PROMPT


def test_prompt_blocks_are_named_ordered_and_drop_empty_content() -> None:
    prompt = render_prompt_blocks(
        (
            PromptBlock("first", " one "),
            PromptBlock("empty", "   "),
            PromptBlock("second", "two"),
        )
    )

    assert prompt == "## first\n\none\n\n---\n\n## second\n\ntwo"


def test_main_agent_contract_separates_goal_workflow_authority_and_completion() -> None:
    prompt = _system_prompt(
        "tool contract",
        persona_block='<persona name="researcher">evidence</persona>',
        mode_block="<reading_mode>read first</reading_mode>",
    )

    headings = (
        "## 角色与完成标准",
        "## 指令层级与证据边界",
        "## 执行循环",
        "## 工作区与文件",
        "## Office、Shell 与远程资料",
        "## 安全与最终交付",
        "## 工具与扩展契约",
        "## Persona",
        "## WorkMode 与 Capability",
    )
    assert all(heading in prompt for heading in headings)
    assert list(map(prompt.index, headings)) == sorted(map(prompt.index, headings))
    assert "当前用户请求决定目标" in prompt
    assert "不能改写用户目标" in prompt
    assert "必要动作已有成功工具结果" in prompt
    assert "不得把计划中的动作写成已经完成" in prompt
    assert "多个都合理的可写目标" in prompt
    assert "必须先 ask_user" in prompt


def test_empty_optional_blocks_do_not_pollute_the_stable_prefix() -> None:
    prompt = _system_prompt("")

    assert "## Persona" not in prompt
    assert "## WorkMode 与 Capability" not in prompt
    assert "## 长期记忆" not in prompt
    assert "## 工具与扩展契约" not in prompt


def test_reading_mode_distinguishes_navigation_from_persistent_annotation() -> None:
    prompt = render_work_mode_block("reading", reading_path="paper.pdf")

    assert "reader_goto" in prompt
    assert "reader_annotate" in prompt
    assert "持久写盘" in prompt
    assert "用户明确要求" in prompt
    assert "逐字命中" in prompt


def test_subagent_contract_is_read_only_evidence_first_and_self_contained() -> None:
    prompt = READONLY_SUBAGENT_SYSTEM_PROMPT

    assert "隔离只读" in prompt
    assert "实际查看到的内容" in prompt
    assert "不向用户提问" in prompt
    assert "结论”“证据”“不确定项”" in prompt


def test_background_json_prompts_treat_payloads_as_untrusted_data() -> None:
    for prompt in (
        EXTRACTION_SYSTEM_PROMPT,
        CLASSIFICATION_SYSTEM_PROMPT,
        DISTILLATION_SYSTEM_PROMPT,
    ):
        assert "不可信" in prompt
        assert "只输出 JSON" in prompt

    assert "final_result 的口头总结本身不算证据" in DISTILLATION_SYSTEM_PROMPT
    assert "脱离当前对话仍是完整事实" in EXTRACTION_SYSTEM_PROMPT
    assert "同一主体、同一属性和同一适用范围" in CLASSIFICATION_SYSTEM_PROMPT


def test_office_prompt_routes_binary_files_through_skills_and_shell() -> None:
    prompt = _system_prompt("")

    assert "格式 Skill + Python/CLI + 工作区产物" in prompt
    assert "没有专用 inspect/edit" in prompt
    assert "run_in_background=true" in prompt
    assert "默认保留原件" in prompt
    assert "不得创建辅助脚本、备份或产物" in prompt
    assert "单次只读" in prompt
    assert "只重写 selected_text" in _PROPOSAL_SYSTEM_PROMPT
    assert "instruction 明确要求" in _PROPOSAL_SYSTEM_PROMPT
    assert "不得把上下文" in _PROPOSAL_SYSTEM_PROMPT and "内容复制进选区" in _PROPOSAL_SYSTEM_PROMPT


def test_text_deliverables_have_one_model_facing_write_route() -> None:
    prompt = _system_prompt("")

    assert "write_file 的 purpose=artifact" in prompt
    assert "purpose=workspace 只写辅助脚本" in prompt


def test_selected_workspace_files_are_stable_primary_targets_not_scan_permission() -> None:
    block = render_workspace_files_block(["/projects/quarterly/report.docx"])
    prompt = _system_prompt("", workspace_files_block=block)

    assert "<selected_workspace_files>" in prompt
    assert "/projects/quarterly/report.docx" in prompt
    assert "主要输入/编辑目标" in prompt
    assert "不要因为所在目录已授权就扫描无关的同级文件" in prompt


def test_compaction_preserves_continuation_state_not_unverified_completion_claims() -> None:
    prompt = COWORK_COMPACTION_PROMPTS.system_prompt

    assert "可直接续跑" in prompt
    assert "成功工具结果" in prompt
    assert "尚未获得的授权" in prompt
    assert "不要补写" in prompt

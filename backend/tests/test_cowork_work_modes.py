"""工作模式块的用例。

这里挡的是一个具体的产品故障：用户选了论文阅读但还没打开文档时，模型会凭它对同名论文
的印象作答，还附上一个像模像样的页码和一段像模像样的引文。对读者来说，那和从他本来要
打开的那份文件里取出来的完全无法区分。
"""

from __future__ import annotations

from app.cowork.runtime import _system_prompt
from app.cowork.work_modes import normalize_work_mode, render_work_mode_block


def test_office_mode_adds_nothing() -> None:
    """日常办公是默认玩法。多一段"你是通用助手"只是白烧稳定前缀。"""
    assert render_work_mode_block("office") == ""


def test_reading_mode_without_a_document_forbids_answering_from_memory() -> None:
    block = render_work_mode_block("reading", reading_path=None)

    assert "还没有打开任何文档" in block
    assert "不要" in block and "印象" in block


def test_reading_mode_with_a_document_pins_the_path() -> None:
    block = render_work_mode_block("reading", reading_path="papers/attention.pdf")

    assert "papers/attention.pdf" in block
    assert "还没有打开任何文档" not in block
    # 玩法和"读哪一份"是两段，换一份文档不该重写 playbook。
    assert "<reading_mode>" in block and "<reading_material>" in block


def test_blank_path_is_treated_as_no_document() -> None:
    assert "还没有打开任何文档" in render_work_mode_block("reading", reading_path="   ")


def test_reading_playbook_stays_out_of_ordinary_runs() -> None:
    """工具常驻是对的，玩法常驻不是——那是每次 run 都在为无关内容付前缀的钱。"""
    ordinary = _system_prompt("", mode_block=render_work_mode_block("office"))

    assert "reader_goto" not in ordinary
    assert "[p.12]" not in ordinary


def test_reading_block_reaches_the_system_prompt() -> None:
    prompt = _system_prompt("", mode_block=render_work_mode_block("reading"))

    assert "<reading_mode>" in prompt


def test_unknown_work_mode_falls_back_to_office() -> None:
    """老 checkpoint 没有这个字段；缺了只该少一段上下文，不该让 run 无法恢复。"""
    assert normalize_work_mode(None) == "office"
    assert normalize_work_mode("code") == "office"
    assert normalize_work_mode("reading") == "reading"


def test_retired_research_mode_resumes_as_office() -> None:
    """ "知识研究"这一档已经删了。

    部署时正在跑、或者已经存盘的 run，checkpoint 里仍然写着 research。恢复它们时不该因为
    一个档位改名而整批失败——退回日常办公只是少一段模式提示词。
    """
    assert normalize_work_mode("research") == "office"
    assert render_work_mode_block("office") == ""

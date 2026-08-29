import pytest

from app.agent_core.prompt_templates import (
    DuplicatePromptTemplateError,
    PromptTemplate,
    PromptTemplateError,
    PromptTemplateRegistry,
)


def test_prompt_template_is_strict_and_substitutes_once() -> None:
    template = PromptTemplate(
        template_id="review.file",
        source="Review {{path}} with {{mode}} mode. Again: {{path}}",
    )

    rendered = template.render({"path": "{{mode}}.docx", "mode": "strict"})

    assert template.parameters == ("path", "mode")
    assert rendered == "Review {{mode}}.docx with strict mode. Again: {{mode}}.docx"
    with pytest.raises(PromptTemplateError, match="缺少参数"):
        template.render({"path": "a.docx"})
    with pytest.raises(PromptTemplateError, match="未知参数"):
        template.render({"path": "a.docx", "mode": "strict", "extra": True})


def test_prompt_template_registry_rejects_duplicate_ids() -> None:
    registry = PromptTemplateRegistry()
    registry.register(PromptTemplate("review.file", "Review {{path}}"))

    assert registry.render("review.file", {"path": "a.docx"}) == "Review a.docx"
    with pytest.raises(DuplicatePromptTemplateError):
        registry.register(PromptTemplate("review.file", "Different"))


@pytest.mark.parametrize("source", ["bad {{", "bad }}", "bad {{not valid}}"])
def test_prompt_template_rejects_malformed_placeholders(source: str) -> None:
    with pytest.raises(PromptTemplateError, match="不完整占位符"):
        PromptTemplate("bad.template", source)

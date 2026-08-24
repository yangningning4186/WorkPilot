"""阅读三指标的 fixture 测试（eval/metrics/reading.py，口径见 docs/04 §5）。

材料是测试内构造的一份小 Markdown，不读跑批产物：真实报告随策略迭代变化，拿它当
fixture 会让这些测试在无关改动上红。
"""

from typing import Any

from eval.metrics.reading import material_from_text, merge_reading_scores, score_reading

# 每节都要撑过 `units_from_sections` 的定长阈值，否则整篇会被攒成一个 unit——
# 那样 locator_accuracy 就没有"引错节"这种情形可判了，测试会在一个假前提上通过。
_FILLER = "这一节讨论证据组织的工程细节，与前后两节的用词刻意不同。" * 45

SENTENCES = {
    1: "检索增强生成把外部语料接进生成流程。",
    2: "稠密检索把查询和文档映射到同一个向量空间，用内积排序。",
    3: "引用必须锚定在解析块上，因为分块策略一变，chunk 标识就会失效。",
}
MATERIAL = "# 证据组织综述\n\n" + "\n".join(
    f"## {index} 第{index}节\n\n{sentence}\n\n{_FILLER}\n" for index, sentence in SENTENCES.items()
)

FILES = {"papers/survey.md": MATERIAL}
WORKSPACE = "/tmp/case-1/workspace/papers/survey.md"


def test_the_fixture_really_splits_into_three_locators() -> None:
    """指标测试的前提：这份材料确实有三个 locator。

    切分阈值一变，下面几条测试会在"整篇只有一节"的假前提上继续通过——那时它们判的
    已经不是引对了页，而是根本没有第二页可以引错。
    """

    material = material_from_text("papers/survey.md", MATERIAL)

    assert material is not None
    assert material.unit_count == 3


def _material_locator(quote: str) -> int:
    material = material_from_text("papers/survey.md", MATERIAL)
    assert material is not None
    for unit in material.units:
        if quote in unit.text:
            return unit.locator
    raise AssertionError(f"fixture 里找不到这句话: {quote}")


def _call(name: str, arguments: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments, "status": "ok", "result": None, **extra}


def test_a_task_that_never_opened_a_material_is_not_scored() -> None:
    """没考不等于考砸了。

    办公任务不该把阅读指标的分母往下拉——否则一份 50 条的跑批里，46 条没碰材料的
    任务会把 read_before_claim 冲到接近 0，而那个数字什么也不说明。
    """

    scored = score_reading(
        response="已经写好报告 [p.2]。",
        trace=[_call("write_text_file", {"path": "/tmp/x.md"})],
        fixture_files=FILES,
    )

    assert scored is None


def test_read_before_claim_scores_each_citation_not_each_answer() -> None:
    quote = "稠密检索把查询和文档映射到同一个向量空间"
    read_locator = _material_locator(quote)
    trace = [
        _call(
            "read_material",
            {"path": WORKSPACE, "locators": str(read_locator)},
            result={"locators": [read_locator]},
        )
    ]

    scored = score_reading(
        # 一处读过、一处没有：这条指标要能把它们分开，而不是给整条回答一个"通过"。
        response=f"结论甲 [p.{read_locator}]，结论乙 [p.{read_locator + 1}]。",
        trace=trace,
        fixture_files=FILES,
    )

    assert scored is not None
    assert scored["read_before_claim"]["total"] == 2
    assert scored["read_before_claim"]["passed"] == 1
    assert scored["read_before_claim"]["ungrounded_citations"] == [read_locator + 1]


def test_a_range_citation_expands_into_one_claim_per_locator() -> None:
    """`[p.1-3]` 声称了三件事。只把它算成一处，模型多引两页就是白送的。"""

    scored = score_reading(
        response="综述覆盖了三节 [p.1-3]。",
        trace=[
            _call(
                "read_material",
                {"path": WORKSPACE, "locators": "1"},
                result={"locators": [1]},
            )
        ],
        fixture_files=FILES,
    )

    assert scored is not None
    assert scored["read_before_claim"]["total"] == 3
    assert scored["read_before_claim"]["passed"] == 1


def test_a_fabricated_quote_fails_verifiability_and_leaves_locator_unjudged() -> None:
    """引文根本不在文中时，"它在第几页"没有答案。

    把它算进 locator_accuracy 的分母，会让一次编造同时惩罚两条指标，从此再也分不清
    模型是引错了页还是压根编了一句。
    """

    scored = score_reading(
        response="见 [p.2]。",
        trace=[
            _call("read_material", {"path": WORKSPACE, "locators": "2"}, result={"locators": [2]}),
            _call(
                "reader_goto",
                {"path": WORKSPACE, "locator": 2, "quote": "本文在多语言检索上提升了 7 个点"},
            ),
        ],
        fixture_files=FILES,
    )

    assert scored is not None
    assert scored["quote_verifiability"] == {
        "total": 1,
        "passed": 0,
        "rate": 0.0,
        "by_script": {"zh": {"total": 1, "passed": 0, "rate": 0.0}},
        "cross_language": {"total": 0, "passed": 0, "rate": None},
        "unverified": [{"locator": 2, "quote": "本文在多语言检索上提升了 7 个点"}],
    }
    assert scored["locator_accuracy"]["total"] == 0
    assert scored["locator_accuracy"]["rate"] is None


def test_a_real_quote_on_the_wrong_locator_is_exactly_what_the_metric_is_for() -> None:
    quote = "引用必须锚定在解析块上"
    actual = _material_locator(quote)
    claimed = actual - 1
    assert claimed >= 1

    scored = score_reading(
        response=f"见 [p.{claimed}]。",
        trace=[
            _call(
                "read_material",
                {"path": WORKSPACE, "locators": f"{claimed},{actual}"},
                result={"locators": [claimed, actual]},
            ),
            _call("reader_goto", {"path": WORKSPACE, "locator": claimed, "quote": quote}),
        ],
        fixture_files=FILES,
    )

    assert scored is not None
    # 引文是真的——这一条过。
    assert scored["quote_verifiability"]["passed"] == 1
    # 但它不在模型声称的那一节上——这一条不过，且要说得出真身在哪。
    assert scored["locator_accuracy"] == {
        "total": 1,
        "passed": 0,
        "rate": 0.0,
        "misplaced": [{"claimed": claimed, "found": actual, "quote": quote}],
    }


def test_quotes_are_bucketed_by_script_because_cross_language_never_matches() -> None:
    """用中文问英文原文时，模型给的"引文"是它自己的译文，逐字校验必然落空。

    混在一起报，会把这份正常损耗说成引文能力缺陷；所以分语言是这条指标定义的一部分。
    """

    scored = score_reading(
        response="见 [p.3]。",
        trace=[
            _call("read_material", {"path": WORKSPACE, "locators": "3"}, result={"locators": [3]}),
            _call(
                "reader_goto",
                {"path": WORKSPACE, "locator": 3, "quote": "引用必须锚定在解析块上"},
            ),
            _call(
                "reader_goto",
                {
                    "path": WORKSPACE,
                    "locator": 3,
                    "quote": "citations must be anchored to parsed blocks",
                },
            ),
        ],
        fixture_files=FILES,
    )

    assert scored is not None
    assert scored["quote_verifiability"]["by_script"]["zh"]["passed"] == 1
    assert scored["quote_verifiability"]["by_script"]["en"]["passed"] == 0
    # 中文材料 + 英文引文 = 一条跨语言引用，单独计。
    assert scored["quote_verifiability"]["cross_language"] == {
        "total": 1,
        "passed": 0,
        "rate": 0.0,
    }


def test_a_material_edited_mid_run_is_reported_as_unscorable_not_as_a_wrong_quote() -> None:
    scored = score_reading(
        response="见 [p.1]。",
        trace=[
            _call("read_material", {"path": WORKSPACE, "locators": "1"}, result={"locators": [1]}),
            _call("reader_goto", {"path": WORKSPACE, "locator": 1, "quote": "检索增强生成"}),
        ],
        fixture_files=FILES,
        changed_files={"papers/survey.md"},
    )

    assert scored is not None
    assert scored["quote_verifiability"]["total"] == 0
    assert scored["unscorable"] == [
        {"path": "papers/survey.md", "reason": "material_changed_during_run"}
    ]


def test_merge_is_micro_averaged_over_claims_not_over_items() -> None:
    """先各自算比率再平均，会让只标了一处的样本和标了十处的样本一样重。"""

    left = score_reading(
        response="甲 [p.1]",
        trace=[
            _call("read_material", {"path": WORKSPACE, "locators": "1"}, result={"locators": [1]})
        ],
        fixture_files=FILES,
    )
    right = score_reading(
        response="乙 [p.2] 丙 [p.3] 丁 [p.1]",
        trace=[
            _call("read_material", {"path": WORKSPACE, "locators": "1"}, result={"locators": [1]})
        ],
        fixture_files=FILES,
    )
    assert left is not None and right is not None

    merged = merge_reading_scores([left, right])

    assert merged is not None
    assert merged["items"] == 2
    # 四处引用里只有两处读过 —— 宏平均会给出 (1.0 + 1/3) / 2 ≈ 0.67，那是错的。
    assert merged["read_before_claim"] == {"total": 4, "passed": 2, "rate": 0.5}

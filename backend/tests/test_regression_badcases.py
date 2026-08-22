"""badcase 回流的棘轮：修好的问题必须留下一条永久用例（docs/06-评测体系.md §6）。

分层收成两层之后（06 §4.1），**PR 层的质量兜底全在 pytest 上**。
所以"修复"不是终点，把修复固化成一条挡得住的用例才是——否则同一个坑会再踩一遍，
而夜间门禁最快也要等到第二天才告诉你。

## 怎么加一条

1. 在 `BADCASES` 里登记：症状、根因、修法、以及**哪条用例挡住它**
2. 如果还没有挡得住的用例，先把用例写出来——`covered_by` 指向不存在的测试会直接红
3. 可复现的线上 badcase 另外还要进 `regression` 评测集（`promoted_to_eval = true`），
   两条路径不互相替代：评测集量的是指标，pytest 挡的是回归

`test_every_registered_badcase_is_still_covered` 是这套棘轮的锁：
covering 测试被改名或删掉时它会失败，逼你要么恢复覆盖、要么显式改登记，
而不是让覆盖悄悄消失。
"""

import importlib
from dataclasses import dataclass

import pytest

from app.core.config import Settings


@dataclass(frozen=True)
class Badcase:
    case_id: str
    discovered: str
    source: str
    symptom: str
    fix: str
    covered_by: tuple[str, ...]


# 只登记**已经修好**的问题。仍在归因中的（D7 的精排 badcase、E7 的 retrieval miss）
# 不属于这里——棘轮锁的是"不许退回去"，不是"待办清单"。
#
# 2026-08-21 摘掉两条：E5（证据门控误拒）与 C（成本查询按 label 撞车）。它们锁的代码
# 分别随 pgvector 主答路径与 GPU 批次摊销一起退役了。登记一条没有活覆盖的 badcase，
# 棘轮就锁不住任何东西——那比不登记更糟，因为它看起来像被保护着。
BADCASES: tuple[Badcase, ...] = (
    Badcase(
        case_id="E2-ts-rank-cd-中文bigram",
        discovered="2026-08-14",
        source="docs/experiments/2026-08-14-E2-分语言tsvector与词法排序函数.md",
        symptom=(
            "词法臂在中文题上大幅劣化，留出集 span recall 直接掉到 0.0000："
            "cover density 奖励词间距离近的结果，而中文 bigram 天然分散在整个 chunk 里"
        ),
        fix="默认 lexical 排序函数从 ts_rank_cd 换成 ts_rank（纯词频，不看词间距离）",
        # 端到端那条覆盖随 pgvector 主答路径一起退役了（2026-08-21）。这里保留的是
        # 仍然成立的那一半：排序函数的默认值。E5（证据门控误拒）整条摘掉——它锁的是
        # grounded_answer 的证据打包顺序，那段代码已经不存在，留着只是让棘轮空转。
        covered_by=(
            "tests.test_regression_badcases::test_default_lexical_mode_stays_ts_rank",
        ),
    ),
    Badcase(
        case_id="G1-门禁拒判退出码撞车",
        discovered="2026-08-17",
        source="docs/experiments/2026-08-17-G1-首份baseline快照与门禁点亮.md",
        symptom=(
            "拿错数据集的跑批去过门禁，`eval.gate` 甩出 traceback 并以退出码 1 退出——"
            "和'判为不合格'同码。夜间自动化会把'跑批配错了'读成'质量回退'，"
            "而 gate.py 的文档明明写着这类前置条件不满足要退 2"
        ),
        fix=(
            "比较层（数据集不一致、受控配置漂移、item_id 配不上）抛的 ValueError "
            "在 gate 里统一转成 GateRefused，退出码 2 与判定不合格分开"
        ),
        covered_by=(
            "tests.test_eval_gate::test_cli_exit_codes_separate_failure_from_refusal",
            "tests.test_eval_gate::test_dataset_mismatch_is_rejected",
            "tests.test_eval_gate::test_controlled_config_drift_is_not_silently_allowed",
            "tests.test_eval_gate::test_item_id_mismatch_is_rejected",
        ),
    ),
)


def test_every_registered_badcase_is_still_covered() -> None:
    """covering 测试被改名或删掉 → 这里红。棘轮的锁就是这一条。"""
    missing: list[str] = []
    for badcase in BADCASES:
        for reference in badcase.covered_by:
            module_name, _, function_name = reference.partition("::")
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                missing.append(f"{badcase.case_id}: 模块不存在 {module_name}")
                continue
            if not callable(getattr(module, function_name, None)):
                missing.append(f"{badcase.case_id}: 用例不存在 {reference}")
    assert not missing, "badcase 的覆盖用例失联了，先恢复覆盖再改登记：\n" + "\n".join(missing)


def test_badcase_registry_is_well_formed() -> None:
    case_ids = [badcase.case_id for badcase in BADCASES]
    assert len(case_ids) == len(set(case_ids)), "badcase case_id 重复"
    for badcase in BADCASES:
        # 光写"修好了"没有价值，症状与根因才是下次判断"是不是同一个坑"的依据
        assert badcase.symptom.strip(), f"{badcase.case_id} 缺少症状描述"
        assert badcase.fix.strip(), f"{badcase.case_id} 缺少修法描述"
        assert badcase.source.strip(), f"{badcase.case_id} 缺少来源（台账或 feedback id）"
        assert badcase.covered_by, f"{badcase.case_id} 没有任何覆盖用例，棘轮锁不住"


@pytest.mark.parametrize("mode", ["ts_rank_cd", "coverage"])
def test_default_lexical_mode_stays_ts_rank(mode: str) -> None:
    """E2 的锁：默认排序函数不许退回 ts_rank_cd。

    这条是配置层面的钉子。cover density 在中文 bigram 上会把留出集打到 0.0000，
    而这个失效在全中文题集之外看不出来——所以默认值本身就值得钉死。
    """
    assert Settings().lexical_mode == "ts_rank"
    assert mode != Settings().lexical_mode

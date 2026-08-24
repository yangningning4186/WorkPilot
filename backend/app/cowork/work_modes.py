"""工作模式：用户在开场界面上选的那一档，决定这次 run 的玩法。

模式**不是** `CoworkState.mode`。那一个是审批档位（plan / execute），管的是"写工具放不
放行"；这一个管的是"这次要干哪一类活"，两者正交：论文阅读也可以先出计划。

模式块和 `environment_block` / `memory_block` 一样在 run 起始渲染一次就不再变，因此可以
安全地放进 system prompt——provider 的前缀缓存从 system 起算，每轮会变的内容放进去等于
每轮把整段前缀重新计费。

**为什么阅读模式的 playbook 不再由 `register_reading_tools` 无条件注入**：那样每一次
Cowork run 的 system prompt 里都挂着一段跟本次任务无关的阅读须知。现在由
`WorkCapability(name="reading")` 调这个纯渲染函数；工具仍注册在统一 registry，阅读档的
`owned_tools` 保证它们首轮可见，但**玩法**只在用户选了这一档时才该说。
"""

from __future__ import annotations

from typing import Any

from app.cowork_contracts import CoworkWorkMode

# 选中文本进提示词的上限。比 reader_annotate 的 quote 上限（2000）小一档：这一块每轮
# 都重发，而"用户指着哪里"只需要认得出是哪一段，不需要把整节正文抄进来。
_SELECTION_MAX_CHARS = 600

# `Material.unit` 的两个取值 → 提示词里的量词。写死成映射而不是让客户端直接给中文，
# 是因为这是视口里唯一会被插进提示词的自由字符串位。
_READING_UNITS = {"page": "页", "section": "节"}

_READING_PLAYBOOK = """<reading_mode>
用户选了论文阅读。目标是让回答中的每个文档结论都能在侧边阅读器里定位到原文。

工作方式：
1. 先用 material_outline 看结构，或用 search_material 定位；两者给的是 locator 和候选片段，
   不是可直接引用的完整原文。
2. 下任何关于这份文档的论断前，用 read_material 读对应 locator。搜索片段可能从句子中间
   截断，不能据此补全含义或引文。**没读过就是不知道。**
3. 每讲到一处具体内容就调用一次 reader_goto，把 read_material 中逐字出现的原文作为 quote；
   一处一次，不要攒到最后。中文解释英文原文时，quote 仍传英文原句。
4. 紧跟结论标出处，写成 [p.12]；几处写 [p.12,17]，连续几处写 [p.12-14]。不论材料的
   单位叫页还是节，一律写 [p.N]——阅读器把它渲染成可点的链接。
5. 只有用户明确要求“标注/高亮并留下备注”时才调用 reader_annotate。它会持久写盘，quote
   必须逐字命中；普通阅读定位用 reader_goto，不要用批注代替解释或引用。
6. 文档答不了的问题要直说，并说明已经查到什么、还缺什么；补充外部知识时显式标为文档之外。
</reading_mode>"""

_READING_OPEN = """<reading_material>
用户打开的文档是：{path}
所有阅读工具的 path 参数都传这个路径；他没有另外指名时不要去读别的文件。
</reading_material>"""

_READING_EMPTY = """<reading_material>
用户选了论文阅读，但还没有打开任何文档——阅读器面板正显示文件选择器。你手上**没有**
可读的文档。

他问的如果是关于某份文档的（章节、措辞、某句话在第几页），就直说还没有打开文件、请他
先选一份，**不要**凭你对同名论文或同题目书籍的印象作答：一个凭记忆想起来的页码或引文，
在读者眼里和从他本来要打开的那份文件里取出来的完全无法区分，而这正是这里最糟糕的失败。

不是关于文档的问题，正常回答。
</reading_material>"""


_READING_VIEWPORT = """<reading_viewport>
{lines}
</reading_viewport>"""


def normalize_reading_viewport(value: object) -> dict[str, Any] | None:
    """把客户端报上来的视口收成 state 里存得住的形状，非法就当没有。

    这是**用户可控输入**，而且会原样进提示词，所以每一项都得自己收敛：locator 必须是
    正整数（0 与负数表示"还没定位"，不是"第 0 页"），选中文本折成单行并截断，单位词只
    认封闭的两个值——它是唯一一个会被直接插进提示词的字符串，放开就等于给了一条往
    system 之外的块里写任意文字的路。全空返回 None 而不是 `{{}}`，因为下游只需要判一次
    "有没有"。

    刻意**不**在这里校验 locator 是否越界、选中文本是否真的出现在那一页：两者都要解析
    整份文档，而这个函数跑在创建 run 的 HTTP 请求里。越界的后果也有限——提示词里明说了
    这是阅读器报上来的位置，模型下论断前仍要 read_material。
    """

    if not isinstance(value, dict):
        return None
    raw_locator = value.get("locator")
    locator = (
        raw_locator if isinstance(raw_locator, int) and not isinstance(raw_locator, bool) else None
    )
    if locator is not None and locator < 1:
        locator = None
    raw_selection = value.get("selection")
    selection = " ".join(str(raw_selection).split()) if isinstance(raw_selection, str) else ""
    if len(selection) > _SELECTION_MAX_CHARS:
        selection = selection[:_SELECTION_MAX_CHARS] + "…"
    viewport: dict[str, Any] = {}
    if locator is not None:
        viewport["locator"] = locator
    if selection:
        viewport["selection"] = selection
    if value.get("unit") in _READING_UNITS:
        viewport["unit"] = value["unit"]
    # 只有 unit 而没有位置也没有选中：那不是一个视口，是一次空报告。
    if "locator" not in viewport and "selection" not in viewport:
        return None
    return viewport


def render_reading_viewport_block(viewport: object) -> str:
    """渲染"用户此刻在看哪里"。

    这是阅读器 → 模型的**唯一**反向通道。没有它，`reader_goto` 是单向的：模型能把视口
    推到某一页，却不知道用户正停在哪一页、手上划着哪一句——于是"这段是什么意思"这类
    问题在模型那里根本无法解析，它只能猜最近提过的那一处。

    **它属于末尾的临时块，不属于 system prompt。** 判据是 CLAUDE.md 那条：一次 run 内
    会不会变。视口按定义每一轮都可能不同，放进稳定前缀等于每轮把整段前缀作废。

    选中文本来自阅读器的文本层，不是解析口径的原文，所以块里必须明说这一点：PDF 的
    文本层带着硬换行、连字与分栏顺序，直接当引文抄进回答就会对不上 `verify_quote`。
    """

    normalized = normalize_reading_viewport(viewport)
    if normalized is None:
        return ""
    label = _READING_UNITS[normalized.get("unit", "page")]
    locator = normalized.get("locator")
    selection = normalized.get("selection", "")
    lines: list[str] = []
    if locator is not None:
        lines.append(f"阅读器现在显示第 {locator} {label}。")
    if selection:
        lines.append(f"用户在阅读器里选中了这段文字：“{selection}”")
        lines.append(
            "他说的“这段 / 这里 / 这句 / 这个公式”指的就是它，不要去猜最近提到过的那一处。"
        )
        where = f"第 {locator} {label}" if locator is not None else "对应 locator"
        lines.append(
            "这段文字取自阅读器的文本层，带着 PDF 的硬换行、连字与分栏顺序，**不是**可直接"
            f"引用的原文。要引用或下论断前先 read_material 读{where}，以那一份为准。"
        )
    elif locator is not None:
        lines.append("他没有选中任何文字；问题里的指代如果落不到具体位置，就直接问他指的是哪一处。")
    return _READING_VIEWPORT.format(lines="\n".join(lines))


def render_work_mode_block(
    work_mode: CoworkWorkMode,
    *,
    reading_path: str | None = None,
) -> str:
    """渲染这次 run 的模式块。日常办公是默认玩法，不需要额外说明。

    刻意不在这里去解析文档来报"共 12 页"：渲染发生在创建 run 的 HTTP 请求里，为了一句
    facts 去同步解析一份六百页 PDF 会把接口拖垮。模型第一次调 material_outline 就拿到了
    这些事实，早一步说没有价值。
    """
    if work_mode != "reading":
        return ""
    path = (reading_path or "").strip()
    material = _READING_OPEN.format(path=path) if path else _READING_EMPTY
    return f"{_READING_PLAYBOOK}\n\n{material}"


def normalize_work_mode(value: object) -> CoworkWorkMode:
    """老 checkpoint 没有这个字段、或带着已经删掉的档位；一律退回日常办公。

    退回而不是报错：恢复一个正在跑的 run 不该因为档位改名而失败，少一段模式提示词
    远好过整批 run 起不来。已经废弃的 "research" 正是走这条路。
    """
    return value if value in ("office", "reading") else "office"


__all__ = [
    "normalize_reading_viewport",
    "normalize_work_mode",
    "render_reading_viewport_block",
    "render_work_mode_block",
]

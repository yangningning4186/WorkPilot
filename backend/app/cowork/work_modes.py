"""工作模式：用户在开场界面上选的那一档，决定这次 run 的玩法。

模式**不是** `CoworkState.mode`。那一个是审批档位（plan / execute），管的是"写工具放不
放行"；这一个管的是"这次要干哪一类活"，两者正交：论文阅读也可以先出计划。

模式块和 `environment_block` / `memory_block` 一样在 run 起始渲染一次就不再变，因此可以
安全地放进 system prompt——provider 的前缀缓存从 system 起算，每轮会变的内容放进去等于
每轮把整段前缀重新计费。

**为什么阅读模式的 playbook 不再由 `register_reading_tools` 无条件注入**：那样每一次
Cowork run 的 system prompt 里都挂着一段跟本次任务无关的阅读须知。工具常驻是对的（模型
随时可能需要读一份文档），但**玩法**只在用户选了这一档时才该说。
"""

from __future__ import annotations

from app.cowork_contracts import CoworkWorkMode

_READING_PLAYBOOK = """<reading_mode>
用户选了论文阅读。他侧边有一个阅读器面板，你只能通过阅读工具看到那份文档。

工作方式：
1. 先用 material_outline 看结构、或用 search_material 全文定位，两者都返回 locator。
2. 下任何关于这份文档的论断之前，先用 read_material 读到原文。搜索返回的是开了窗口的
   片段，可能从句子中间断开，照抄一句会产生和原文对不上的引用。**没读过就是不知道。**
3. 每提到一处具体内容就调一次 reader_goto，把你即将引用的那句原话传进去，用户的阅读器
   会滚过去并高亮，他因此能边看你的回答边看到依据。一处一次，不要攒到最后调一次。
4. 紧跟结论标出处，写成 [p.12]；几处写 [p.12,17]，连续几处写 [p.12-14]。不论这份材料的
   单位叫页还是节，一律写 [p.N]——阅读器把它渲染成可点的链接。
5. 文档答不了的问题要直说答不了，并说清它讲了什么。补充文档之外的知识时必须标明那是
   文档之外的。
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


__all__ = ["normalize_work_mode", "render_work_mode_block"]

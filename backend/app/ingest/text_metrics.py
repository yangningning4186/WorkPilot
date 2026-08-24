"""切分用的文本度量。

放在共享的 `app/ingest/`：RAG 的 KB 节点切分和 Cowork 阅读引擎的分节都要用它，而这两个
包按 ADR-0011 互不 import。度量一段文字有多长跟任何产品语义都无关，属于解析这一层。
"""

from __future__ import annotations

import re

# 一个 CJK 字符按几个拉丁字符计。裸字符数在中英文之间差了一个量级：2800 个拉丁字符大约是
# 半页论文，2800 个汉字是两页多。按裸长度切，中文材料会被切成又长又粗的块，locator 精度和
# 检索粒度一起废掉——这是照搬英文语料上调出来的常数最容易踩的坑。
CJK_WEIGHT = 2.5

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")


def weighted_len(text: str) -> float:
    """按信息量而不是码点数衡量文本长度。

    CJK 字符按 `CJK_WEIGHT` 折算，于是同一个目标长度在中文材料和英文材料上表示的阅读量
    大致相当，切出来的粒度跨语言可比。
    """
    if not text:
        return 0.0
    cjk = len(_CJK.findall(text))
    return (len(text) - cjk) + cjk * CJK_WEIGHT


__all__ = ["CJK_WEIGHT", "weighted_len"]

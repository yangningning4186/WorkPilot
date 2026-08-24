"""本地知识库的磁盘布局与命名。

一个 KB 就是 `<root>/<slug>/` 一个目录：

    <root>/<slug>/
        manifest.json          # KbManifest：名字、文档清单、版本列表、当前激活版本
        sources/<sha256>.<ext> # 导入时复制的不可变原始字节快照
        versions/<version_id>/ # 一版索引：FAISS + BM25 + docstore

**一份文档集合上可以并存多个索引版本。** 版本 = (embedding 签名, 检索配置) 的一次具体
取值；换 embedding、换切分粒度、换融合方式各自建一版，同一批文档上直接对比。这正是
约束 10 想要的那个东西的正面形态：以前只能"签名不一致就拒绝检索"，现在可以"两版都在，
你说搜哪一版"。评测跑批因此可以把 baseline 与 candidate 指向同一个 KB 的两个版本，
而不必复制一遍语料。

**建新版本不动旧版本。** 构建先落到 ``versions/.<id>.<uuid>.staging``，完成后 rename
进正式目录，最后才原子更新 manifest/active。即使进程在任一步退出，旧 active 仍可用。
这和之前"重建就地覆盖 index/"是相反的取舍，理由也变了：
A/B 的前提就是两版同时活着。建到一半失败只会留下一个不完整的 `versions/<新 id>/`，
它不在 manifest 的 `versions` 里，因此对检索不可见——旧版本照常服务。

**旧的单索引布局（`index/`）不做静默兼容。** 读到它会显式报错并指向 `kb rebuild`：
KB 是派生数据，重建是秒级；而"猜一个版本 id 把旧目录认领进来"意味着那一版的检索配置
是编出来的，A/B 的两边从此不可比。无声的错配比一次显式重建糟得多。

`manifest.json` 仍然原子写：它是「这个库里有什么、哪一版在服役」的唯一事实来源，
写坏了连重建都不知道该重建哪些文件。

**为什么 slug 与显示名分开**：用户会把 KB 叫成「我的论文 / papers (2026)」，而这些字符
不能直接当目录名。slug 是路径，name 是给人看的，manifest 里两者都存。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

MANIFEST_NAME = "manifest.json"
#: 旧布局的单索引目录。只用来识别"这是个需要重建的老库"，不再往里写。
LEGACY_INDEX_DIR = "index"
VERSIONS_DIR = "versions"
SOURCES_DIR = "sources"
# 版本 id 和 slug 一样同时是目录名和路径穿越的防线。
_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_VERSION_LABEL_CHARS = 64
# 早期版本重建时留下的临时目录前缀。现在不再产生这种目录，但已有安装里可能还躺着，
# 所以列举时照旧跳过。
TMP_PREFIX = ".index."

# slug 同时是目录名和路径穿越的防线：只允许小写字母、数字和连字符，因此
# `../`、绝对路径、Windows 盘符都不可能通过。
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
MAX_NAME_CHARS = 64


class KbNameError(ValueError):
    """KB 名字不合法。消息按约束 4 写成可执行指令，直接回给模型或用户。"""


def slugify(name: str) -> str:
    """显示名 → 目录名。

    中文全部会被压成空，所以纯中文名字必须由调用方另给 slug——与其猜一个音译，不如让
    用户自己决定路径叫什么。返回空串表示「这个名字生成不出 slug」。
    """
    normalized = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return _SLUG_STRIP.sub("-", normalized.lower()).strip("-")[:63]


def validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug or ""):
        raise KbNameError(
            f"知识库标识 {slug!r} 不合法：只能用小写字母、数字和连字符，且以字母或数字开头。"
        )
    return slug


def validate_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise KbNameError("知识库名字不能为空。")
    if len(cleaned) > MAX_NAME_CHARS:
        raise KbNameError(f"知识库名字最长 {MAX_NAME_CHARS} 个字符。")
    return cleaned


def kb_dir(root: Path, slug: str) -> Path:
    return root / validate_slug(slug)


def validate_version_id(version_id: str) -> str:
    if not _VERSION_RE.match(version_id or ""):
        raise KbNameError(
            f"索引版本标识 {version_id!r} 不合法：只能用小写字母、数字和连字符，"
            "且以字母或数字开头。"
        )
    return version_id


def versions_dir(root: Path, slug: str) -> Path:
    return kb_dir(root, slug) / VERSIONS_DIR


def sources_dir(root: Path, slug: str) -> Path:
    """内容寻址的原始文件快照目录；不是用户源目录的软链接。"""

    return kb_dir(root, slug) / SOURCES_DIR


def version_dir(root: Path, slug: str, version_id: str) -> Path:
    return versions_dir(root, slug) / validate_version_id(version_id)


def legacy_index_dir(root: Path, slug: str) -> Path:
    return kb_dir(root, slug) / LEGACY_INDEX_DIR


def manifest_path(root: Path, slug: str) -> Path:
    return kb_dir(root, slug) / MANIFEST_NAME


def iter_slugs(root: Path) -> list[str]:
    """列出 root 下所有像 KB 的目录。

    跳过重建中途留下的 tmp 目录和任何不合法的目录名——用户往 data/kb 里随手放一个文件夹
    不该让整个列表接口炸掉。
    """
    if not root.is_dir():
        return []
    slugs: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(TMP_PREFIX):
            continue
        if _SLUG_RE.match(child.name) and (child / MANIFEST_NAME).is_file():
            slugs.append(child.name)
    return slugs


__all__ = [
    "LEGACY_INDEX_DIR",
    "MANIFEST_NAME",
    "MAX_VERSION_LABEL_CHARS",
    "SOURCES_DIR",
    "TMP_PREFIX",
    "VERSIONS_DIR",
    "KbNameError",
    "iter_slugs",
    "kb_dir",
    "legacy_index_dir",
    "manifest_path",
    "slugify",
    "sources_dir",
    "validate_name",
    "validate_slug",
    "validate_version_id",
    "version_dir",
    "versions_dir",
]

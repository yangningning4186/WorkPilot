"""注入给模型的运行环境事实。

分成两块，依据是**这条事实在一次 run 里会不会变**，而不是它属于哪个主题：

- `render_environment_block()`：日期、时区、操作系统。一次 run 内不变，进 system
  prompt，属于 provider prompt cache 的稳定前缀。
- `render_roots_block()` / `render_capabilities_block()`：当前已授权的目录与能力。
  `request_directory`、`request_capability` 获批都会在 run 中途改变它们，所以必须
  每轮重算，走 outbound 末尾的临时块。

不注入日期的代价是具体的：模型不知道"这周""上个季度"指哪一段，只能猜。不注入 OS
的代价同样具体：BSD 与 GNU 的 sed/date 参数不兼容，猜错就是一条失败的 shell 调用。
"""

from __future__ import annotations

import platform
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class WorkspaceRoot(Protocol):
    """只要求渲染用得上的两个字段，避免这个纯渲染模块反向依赖存储契约。"""

    @property
    def canonical_path(self) -> str: ...

    @property
    def access_mode(self) -> str: ...


def _operating_system() -> str:
    """给模型看的系统名。

    `platform.release()` 在 macOS 上返回 Darwin 内核号（25.4.0），模型对它没有先验；
    换成 macOS 15.4 才能正确推断可用的命令行工具。
    """

    system = platform.system()
    if system == "Darwin":
        version = platform.mac_ver()[0]
        return f"macOS {version}" if version else "macOS"
    return f"{system} {platform.release()}".strip()


def render_environment_block(now: datetime) -> str:
    """一次 run 内固定的环境事实。

    时间取自 run 起始时刻而不是每轮的墙钟：跨零点的 run 会让这一块变字，system
    prompt 一旦变字，整段前缀缓存就作废了。
    """

    local = now.astimezone()
    offset = local.strftime("%z")
    zone = local.tzname() or "local"
    return (
        "<environment>\n"
        f"当前日期：{local.strftime('%Y-%m-%d')}（{_WEEKDAYS[local.weekday()]}）\n"
        f"当前时间：{local.strftime('%H:%M')} {zone}（UTC{offset[:3]}:{offset[3:]}）\n"
        f"操作系统：{_operating_system()}（{platform.machine()}）\n"
        "涉及相对时间（本周、上个月、最近三天）时以上面的日期为准，不要另行假设。\n"
        "拼 shell 命令时按上面的系统选参数，BSD 与 GNU 的 sed/date/stat 并不通用。\n"
        "</environment>"
    )


def render_roots_block(roots: Sequence[WorkspaceRoot]) -> str:
    """当前已授权目录。每轮重算——目录可能刚刚被用户批准。"""

    if not roots:
        return ""
    lines = "\n".join(
        f"{index}. {root.canonical_path}"
        f"（{'读写' if root.access_mode == 'read_write' else '只读'}）"
        for index, root in enumerate(roots, start=1)
    )
    return (
        "<workspace_roots>\n"
        "本次会话已授权的目录，第一个是默认输出目录；用户只给文件名或相对路径时按它解析。\n"
        "需要这里没有的目录才调用 request_directory。\n"
        f"{lines}\n"
        "</workspace_roots>"
    )


def render_workspace_files_block(paths: Sequence[str]) -> str:
    """用户在系统选择器中点名的工作文件；一次 run 内保持不变。"""

    normalized = [path.strip() for path in paths if path.strip()]
    if not normalized:
        return ""
    lines = "\n".join(f"{index}. {path}" for index, path in enumerate(normalized, start=1))
    return (
        "<selected_workspace_files>\n"
        "用户明确选定下面这些原文件作为本轮主要输入/编辑目标。优先处理它们；除非任务确实需要，"
        "不要因为所在目录已授权就扫描无关的同级文件。\n"
        f"{lines}\n"
        "</selected_workspace_files>"
    )


def render_capabilities_block(granted: Sequence[str], available: Sequence[str]) -> str:
    """当前已授予与尚未授予的能力。每轮重算——用户可能刚批准了一项。

    不注入的代价在评测里量到过：网络能力已经授权，模型仍然先调
    `request_capability` 去要它，run 停在等人批准，任务就此失败。模型无从知道自己
    手上有什么，保守的猜法就是先要权限——它猜得没错，错的是我们没告诉它。
    """

    held = sorted(set(granted))
    held_capabilities = {item.split(" [", 1)[0] for item in held}
    missing = sorted(set(available) - held_capabilities)
    if not held and not missing:
        return ""
    lines = [
        "<capabilities>",
        "已授予（直接用，不要再为它们调用 request_capability）：" + ("、".join(held) or "无"),
        "未授予（确实需要时才调用 request_capability，并说明用途）："
        + ("、".join(missing) or "无"),
        "已授予不等于免审批：有副作用的动作仍可能逐次征求用户确认，那是另一道边界。",
        # 能力是按工具划的, 不是按后果划的。模型会自己推断"删文件属于写"从而去要
        # filesystem.write, 而 run_shell 实际校验 host.execute——评测里它就是这样
        # 停在一次多余的授权请求上的。这条边界只有我们知道, 不说它就只能靠猜。
        "能力按执行边界划分：run_shell 需要 host.execute，run_sandbox 需要 sandbox.execute，"
        "命令自身造成的读写不再单独要 filesystem.* ；"
        "filesystem.* 管的是 read_text_file / write_text_file 这类文件工具。",
        "</capabilities>",
    ]
    return "\n".join(lines)

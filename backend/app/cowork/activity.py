"""面向用户的 Cowork 工具活动摘要。

运行事件是会持久化并在桌面端回放的，因此不能把工具 arguments 原样塞进去：文件正文、
连接器请求体和凭据都可能在里面。这里仅从一小组明确允许展示的字段中提取目标，并对命令
做一次保守脱敏。界面拿到的是“做什么 / 为什么 / 对什么”，不是另一份工具调用日志。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit


class ToolActivity(TypedDict, total=False):
    title: str
    summary: str
    target: str
    target_kind: Literal["text", "code", "path", "url"]


_TITLES: dict[str, str] = {
    "ask_user": "等待你的答复",
    "request_directory": "申请工作目录",
    "request_capability": "申请运行能力",
    "propose_plan": "提交执行计划",
    "todo_write": "更新任务清单",
    "list_files": "列出文件",
    "read_file": "读取文件",
    "write_file": "写入文件",
    # 旧 checkpoint/cassette 回放仍可能产生这些名称。
    "read_text_file": "读取文本",
    "write_text_file": "写入文本",
    "replace_in_file": "修改文件",
    "search_files": "搜索文件",
    "git_status": "检查 Git 状态",
    "git_diff": "查看文件改动",
    "git_log": "查看提交历史",
    "read_pdf": "读取 PDF",
    "web_search": "搜索网页",
    "fetch_url": "读取网页",
    "run_shell": "执行 Shell 命令",
    "shell_task_output": "查看后台任务输出",
    "shell_task_kill": "停止后台任务",
    "wake_on": "等待后台任务",
    "sleep": "等待后继续",
    "create_artifact": "生成交付物",
    "load_tools": "加载扩展工具",
    "list_skills": "查看可用技能",
    "load_skill": "加载格式 Skill",
    "explore": "委派只读调查",
    "reader_goto": "定位文档原文",
    "reader_annotate": "批注文档原文",
    "search_knowledge": "检索知识库",
    "list_connectors": "查看已连接服务",
    "act_connector_api": "调用连接器",
    "browser_open": "打开浏览器",
    "browser_snapshot": "读取页面结构",
    "browser_click": "点击网页控件",
    "browser_back": "返回上一页",
    "browser_type": "填写网页输入",
    "browser_select": "选择网页选项",
    "browser_upload": "上传网页文件",
    "browser_download": "下载网页文件",
    "browser_screenshot": "保存网页截图",
    "browser_find": "查找页面内容",
    "browser_close": "关闭浏览器",
}

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)\s*=\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s;&|]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")


def _clip(value: object, limit: int = 180) -> str:
    text = " ".join(str(value).strip().split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def _path_target(arguments: Mapping[str, Any]) -> str:
    path = _string(arguments, "path") or _string(arguments, "suggested_path")
    return _clip(path, 220)


def _url_target(raw: str) -> str:
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return _clip(raw, 180)
    if not parts.netloc:
        return _clip(raw, 180)
    path = parts.path if parts.path not in {"", "/"} else ""
    return _clip(f"{parts.netloc}{path}", 180)


def _command_target(command: str) -> str:
    redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<已隐藏>", command)
    redacted = _BEARER.sub("Bearer <已隐藏>", redacted)
    return _clip(redacted, 220)


def _generic_title(name: str) -> str:
    readable = name.replace("_", " ").strip()
    return readable or "执行工具"


def _set_target(
    activity: ToolActivity,
    target: str,
    target_kind: Literal["text", "code", "path", "url"],
) -> None:
    """写入一组互相依赖的目标字段，并保留 TypedDict 的精确类型。"""

    activity["target"] = target
    activity["target_kind"] = target_kind


def describe_tool_activity(name: str, raw_arguments: object) -> ToolActivity:
    """把一次工具调用压成可安全持久化的用户可读摘要。"""

    arguments = raw_arguments if isinstance(raw_arguments, Mapping) else {}
    activity: ToolActivity = {"title": _TITLES.get(name, _generic_title(name))}

    reason = _string(arguments, "reason")
    if reason:
        activity["summary"] = _clip(reason)

    if name == "run_shell":
        command = _command_target(_string(arguments, "command"))
        if command:
            _set_target(activity, command, "code")
        if not reason:
            activity["summary"] = (
                "在后台启动命令"
                if arguments.get("run_in_background") is True
                else "在持久 Shell 会话中执行"
                if arguments.get("persistent_session") is True
                else "在工作目录中执行命令"
            )
        return activity

    if name in {
        "list_files",
        "read_file",
        "write_file",
        "read_text_file",
        "write_text_file",
        "replace_in_file",
        "search_files",
        "git_status",
        "git_diff",
        "git_log",
        "read_pdf",
        "create_artifact",
        "reader_goto",
        "reader_annotate",
        "browser_upload",
        "browser_download",
        "browser_screenshot",
    }:
        target = _path_target(arguments)
        if target:
            _set_target(activity, target, "path")

    if name == "list_files":
        pattern = _string(arguments, "pattern")
        activity["summary"] = (
            f"递归查找 {pattern or '*'}"
            if arguments.get("recursive") is True
            else f"查看 {pattern or '*'}"
        )
    elif name in {"read_file", "read_text_file"}:
        start = arguments.get("start_line")
        maximum = arguments.get("max_lines")
        if isinstance(start, int):
            activity["summary"] = (
                f"读取第 {start}–{start + maximum - 1} 行"
                if isinstance(maximum, int)
                else f"从第 {start} 行开始读取"
            )
    elif name in {"write_file", "write_text_file"}:
        activity["summary"] = (
            "创建或更新交付物" if arguments.get("purpose") == "artifact" else "创建或更新文本文件"
        )
    elif name == "replace_in_file":
        activity["summary"] = "替换文件中的指定内容"
    elif name == "search_files":
        query = _string(arguments, "query")
        if query:
            activity["summary"] = f"查找“{_clip(query, 90)}”"
    elif name == "create_artifact":
        title = _string(arguments, "title")
        if title:
            activity["summary"] = f"生成“{_clip(title, 100)}”"
    elif name in {"web_search", "search_knowledge"}:
        query = _string(arguments, "query")
        if query:
            _set_target(activity, _clip(query), "text")
        if name == "web_search":
            activity["summary"] = f"最多返回 {arguments.get('max_results', 8)} 条结果"
    elif name in {"fetch_url", "browser_open"}:
        url = _url_target(_string(arguments, "url"))
        if url:
            _set_target(activity, url, "url")
    elif name == "load_skill":
        skill = _string(arguments, "name")
        if skill:
            _set_target(activity, skill, "text")
            activity["summary"] = "读取这项 Skill 的执行规范"
    elif name == "load_tools":
        names = arguments.get("names")
        if isinstance(names, list):
            visible = [_clip(item, 60) for item in names[:8] if isinstance(item, str)]
            if visible:
                _set_target(activity, "、".join(visible), "text")
                activity["summary"] = f"加载 {len(names)} 项长尾能力"
    elif name == "todo_write":
        todos = arguments.get("todos")
        if isinstance(todos, list):
            activity["summary"] = f"同步 {len(todos)} 项任务进度"
    elif name == "explore":
        question = _string(arguments, "question")
        if question:
            _set_target(activity, _clip(question), "text")
            activity["summary"] = "在隔离的只读上下文中查证"
    elif name.startswith("browser_"):
        label = _string(arguments, "label") or _string(arguments, "text")
        if label:
            _set_target(activity, _clip(label, 120), "text")
    elif name == "act_connector_api":
        method = _string(arguments, "method").upper()
        path = _string(arguments, "path")
        target = " ".join(part for part in (method, path) if part)
        if target:
            _set_target(activity, _clip(target), "code")
        activity.setdefault("summary", "通过已授权账户调用官方 API")
    else:
        # 未知扩展工具只认这些低风险标识；绝不回退到 dump 整份 arguments。
        target_fields: tuple[tuple[str, Literal["text", "code", "path", "url"]], ...] = (
            ("query", "text"),
            ("path", "path"),
            ("url", "url"),
            ("name", "text"),
            ("action", "text"),
            ("task", "text"),
        )
        for key, kind in target_fields:
            value = _string(arguments, key)
            if value:
                shown = _url_target(value) if key == "url" else _clip(value)
                _set_target(activity, shown, kind)
                break

    return activity


def activity_description(activity: ToolActivity) -> str:
    """给 plan_steps 表留一条同样可读、无敏感参数的说明。"""

    parts = [activity.get("title", "执行工具")]
    if activity.get("summary"):
        parts.append(activity["summary"])
    if activity.get("target"):
        target = activity["target"]
        if activity.get("target_kind") == "path":
            target = PurePath(target).name or target
        parts.append(target)
    return " · ".join(parts)

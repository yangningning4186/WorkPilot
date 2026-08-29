"""Workspace 与 WorkPilot 控制面的不可静默修改边界。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

# 这些路径里的内容会在当前调用结束之后获得执行权或改变后续 Agent 行为。它们仍可由
# 用户逐次批准后修改，但 auto、workspace trust、常驻规则和 Team Worker 都不能放行。
_PROTECTED_WORKSPACE_MARKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?:^|[/=:])\.workpilot(?:/|$)", re.IGNORECASE),
        ".workpilot/**（Workspace policy、Skill 或 Persona）",
    ),
    (
        re.compile(r"(?:^|[/=:])\.git/hooks(?:/|$)", re.IGNORECASE),
        ".git/hooks/**（Git hook）",
    ),
    (
        re.compile(r"(?:^|[/=:])\.github/workflows(?:/|$)", re.IGNORECASE),
        ".github/workflows/**（CI workflow）",
    ),
    (
        re.compile(r"(?:^|[/=:])\.vscode/tasks\.json$", re.IGNORECASE),
        ".vscode/tasks.json（编辑器任务）",
    ),
)

# Prefix trust only attests to the argv prefix the owner reviewed.  Options that load a
# second argument/configuration file can smuggle an entirely different output path past
# that review, so they are never eligible for a silent waiver.  Keep this list about
# *argument indirection*, not ordinary input files: ``cat report.md`` remains usable, while
# ``curl --config request.conf`` needs a human because request.conf can itself declare an
# output path.
_OPAQUE_LONG_OPTIONS = frozenset(
    {
        "args-file",
        "command-file",
        "config",
        "config-env",
        "config-file",
        "configuration",
        "execute",
        "expand-output",
        "exclude-from",
        "files-from",
        "filter-from",
        "include-from",
        "options-file",
        "pathspec-from-file",
        "response-file",
        "rules-file",
        "to-command",
        "use-compress-program",
    }
)

# Short options are command-specific and may be combined (``curl -sSLoFILE``).  A value
# option consumes the rest of the cluster, which is important: characters in the filename
# must not be reinterpreted as more flags.  ``embedded`` means only @FILE/<FILE fragments
# are local paths; the rest of the value is data.
_PROGRAM_SHORT_VALUE_OPTIONS: dict[str, dict[str, str]] = {
    "curl": {
        "A": "value",
        "C": "value",
        "D": "path",
        "E": "path",
        "F": "embedded",
        "H": "embedded",
        "K": "opaque",
        "P": "value",
        "Q": "value",
        "T": "path",
        "U": "value",
        "X": "value",
        "Y": "value",
        "b": "path",
        "c": "path",
        "d": "embedded",
        "e": "value",
        "h": "value",
        "m": "value",
        "o": "path",
        "r": "value",
        "t": "value",
        "u": "value",
        "w": "embedded",
        "x": "value",
        "y": "value",
        "z": "path",
    },
    "git": {"C": "path", "c": "opaque"},
    "tar": {
        "C": "path",
        "H": "value",
        "I": "opaque",
        "K": "value",
        "L": "value",
        "N": "value",
        "T": "opaque",
        "V": "value",
        "X": "opaque",
        "b": "value",
        "f": "path",
    },
    "wget": {
        "A": "value",
        "B": "value",
        "D": "value",
        "I": "value",
        "O": "path",
        "P": "path",
        "R": "value",
        "T": "value",
        "U": "value",
        "W": "value",
        "X": "value",
        "a": "path",
        "e": "opaque",
        "i": "path",
        "l": "value",
        "o": "path",
        "t": "value",
        "w": "value",
    },
}

_PROGRAM_LONG_EMBEDDED_FILE_OPTIONS: dict[str, frozenset[str]] = {
    "curl": frozenset(
        {
            "data",
            "data-ascii",
            "data-binary",
            "data-urlencode",
            "form",
            "url-query",
            "variable",
            "write-out",
        }
    )
}

# These programs conventionally expand @response files into argv.  The response body is
# not available at the authorization seam, so accepting one under a trusted prefix would
# make the prefix meaningless.
_RESPONSE_FILE_PROGRAMS = frozenset(
    {
        "clang",
        "clang++",
        "gcc",
        "g++",
        "javac",
        "kotlinc",
        "rustc",
        "swiftc",
    }
)

_REMOTE_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def protected_workspace_path_reason(path: str | Path) -> str | None:
    """返回一个路径需要逐次人工批准的原因；大小写折叠以覆盖 Windows/macOS。"""

    normalized = str(path).strip().replace("\\", "/")
    if not normalized:
        return None
    for pattern, label in _PROTECTED_WORKSPACE_MARKERS:
        if pattern.search(normalized):
            return f"目标属于受保护路径 {label}"
    return None


def protected_shell_command_reason(
    *,
    argv: Sequence[str],
    cwd: Path,
    extra_protected_paths: Iterable[Path] = (),
) -> str | None:
    """保守识别 Shell 命令中明示的受保护路径。

    宿主 Shell 无法像文件工具一样完整解析任意脚本的真实写集合，因此这里只把可证明的
    cwd/argv 命中提升为 human-only；不把它描述成 OS sandbox。
    """

    opaque_reason = shell_argument_indirection_reason(argv)
    if opaque_reason is not None:
        return opaque_reason

    reason = protected_workspace_path_reason(cwd)
    if reason is not None:
        return reason
    protected = tuple(_resolved(path) for path in extra_protected_paths)
    program = _program_name(argv)
    for index, raw in enumerate(argv):
        # argv[0] is resolved through PATH unless it is explicitly path-shaped.  Treating a
        # same-named file in cwd as the executable would create noisy false positives.
        reason = _protected_argument_value_reason(
            raw,
            cwd=cwd,
            protected=protected,
            explicit=False,
            allow_plain_existing=index > 0,
        )
        if reason is not None:
            return reason
        for value, mode in _option_values(argv, index=index, program=program):
            reason = _protected_argument_value_reason(
                value,
                cwd=cwd,
                protected=protected,
                explicit=True,
                embedded_only=mode == "embedded",
            )
            if reason is not None:
                return reason
    return None


def shell_argument_indirection_reason(argv: Sequence[str]) -> str | None:
    """Return why an argv-prefix waiver cannot cover this command.

    Config/response files are nested argument sources.  We deliberately do not read and
    interpret them here: their contents can change after approval, parsers differ by tool,
    and some formats can include yet more files.  Requiring one human approval for the
    concrete invocation is the only honest boundary short of an OS sandbox.
    """

    program = _program_name(argv)
    short_options = _PROGRAM_SHORT_VALUE_OPTIONS.get(program, {})
    options_finished = False
    for raw in argv[1:]:
        if options_finished:
            if program in _RESPONSE_FILE_PROGRAMS and raw.startswith("@") and len(raw) > 1:
                return "命令通过 @response 文件加载未展开参数，不能使用静默前缀授权"
            continue
        if raw == "--":
            options_finished = True
            continue
        if raw.startswith("--"):
            name = _long_option_name(raw)
            if name in _OPAQUE_LONG_OPTIONS:
                return f"参数 --{name} 会加载或执行未展开配置，不能使用静默前缀授权"
            continue
        if raw.startswith("-") and raw != "-":
            cluster = raw[1:]
            for character in cluster:
                mode = short_options.get(character)
                if mode == "opaque":
                    return f"参数 -{character} 会加载或执行未展开配置，不能使用静默前缀授权"
                if mode in {"path", "embedded", "value"}:
                    # This option consumes the remainder (or the next argv item).
                    break
            continue
        if program in _RESPONSE_FILE_PROGRAMS and raw.startswith("@") and len(raw) > 1:
            return "命令通过 @response 文件加载未展开参数，不能使用静默前缀授权"
    return None


def protected_control_path_reason(
    path: str | Path,
    protected_paths: Iterable[Path],
) -> str | None:
    """识别 settings 定位的 WorkPilot DB、Skill、MCP 与 secret 控制面。"""

    candidate = _resolved(Path(path))
    for root in (_resolved(item) for item in protected_paths):
        if candidate == root or candidate.is_relative_to(root):
            return f"目标属于 WorkPilot 受保护控制面 {root}"
    return None


def _protected_argument_value_reason(
    raw: str,
    *,
    cwd: Path,
    protected: tuple[Path, ...],
    explicit: bool,
    allow_plain_existing: bool = False,
    embedded_only: bool = False,
) -> str | None:
    embedded_values = tuple((value, True) for value in _embedded_path_values(raw))
    values = embedded_values if embedded_only else ((raw, explicit), *embedded_values)
    for value, value_is_explicit in dict.fromkeys(values):
        normalized = value.strip()
        if not normalized or _is_remote_url(normalized):
            continue
        # An option wrapper is not itself a filesystem path.  Its value is handled by
        # _option_values below; skipping the wrapper avoids interpreting a remote URL such
        # as ``--url=https://host/.workpilot`` as a local protected path.
        if not value_is_explicit and normalized.startswith("-"):
            continue
        reason = protected_workspace_path_reason(normalized)
        if reason is not None:
            return reason
        candidate = _path_candidate(
            normalized,
            cwd=cwd,
            explicit=value_is_explicit,
            allow_plain_existing=allow_plain_existing,
        )
        if candidate is None:
            continue
        reason = protected_control_path_reason(candidate, protected)
        if reason is not None:
            return reason
    return None


def _option_values(argv: Sequence[str], *, index: int, program: str) -> tuple[tuple[str, str], ...]:
    """Extract path-bearing values, including attached/clustered short options."""

    raw = argv[index]
    if index == 0 or raw in {"", "-", "--"} or not raw.startswith("-"):
        return ()
    next_value = argv[index + 1] if index + 1 < len(argv) and argv[index + 1] != "--" else None
    if raw.startswith("--"):
        name = _long_option_name(raw)
        if "=" in raw:
            value = raw.split("=", 1)[1]
            long_mode = (
                "embedded"
                if name in _PROGRAM_LONG_EMBEDDED_FILE_OPTIONS.get(program, frozenset())
                else "path"
            )
            return ((value, long_mode),)
        if next_value is not None and not next_value.startswith("-"):
            if name in _PROGRAM_LONG_EMBEDDED_FILE_OPTIONS.get(program, frozenset()):
                return ((next_value, "embedded"),)
            # There is no portable registry of long options.  Treat every following token as
            # a possible value; this changes the decision only if it resolves to a protected
            # target, and closes unknown --destination/--chdir/--file spellings fail-closed.
            return ((next_value, "path"),)
        return ()

    cluster = raw[1:]
    candidates: list[tuple[str, str]] = []
    short_options = _PROGRAM_SHORT_VALUE_OPTIONS.get(program, {})
    for offset, character in enumerate(cluster):
        short_mode = short_options.get(character)
        if short_mode is None:
            continue
        if short_mode == "opaque":
            return ()  # shell_argument_indirection_reason already made it human-only.
        attached = cluster[offset + 1 :]
        if attached:
            candidates.append((attached, "embedded" if short_mode == "embedded" else "path"))
        elif next_value is not None:
            candidates.append((next_value, "embedded" if short_mode == "embedded" else "path"))
        break

    # Unknown short options still commonly use -oFILE.  Also consider the first path marker
    # after combined flags, but do not build every suffix of a 4 KiB argument: that would turn
    # an authorization check into quadratic work.
    if len(cluster) > 1:
        generic = cluster[1:]
        candidates.append((generic, "path"))
        if not _is_remote_url(generic):
            marker_offsets = [
                offset
                for marker in ("/", "\\", "~", ".workpilot", ".git", ".github", ".vscode")
                if (offset := generic.lower().find(marker)) > 0
            ]
            if marker_offsets:
                candidates.append((generic[min(marker_offsets) :], "path"))
    if next_value is not None:
        candidates.append((next_value, "path"))
    return tuple(dict.fromkeys(candidates))


def _embedded_path_values(raw: str) -> tuple[str, ...]:
    """Extract curl-style @FILE/<FILE and local file:// references from a value."""

    value = raw.strip()
    found: list[str] = []
    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.hostname in {None, "", "localhost"}:
            found.append(unquote(parsed.path))
    # form/data syntax: @path, <path, name=@path;type=..., or name=<path.
    for match in re.finditer(r"(?:^|=)(?:@|<)([^;,]+)", value):
        candidate = match.group(1).strip()
        if candidate and candidate != "-":
            found.append(candidate)
    # curl --variable name@FILE and --write-out "%output{FILE}" are path-bearing even
    # though the path is nested inside an otherwise non-path value.
    variable_file = re.fullmatch(r"%?[A-Za-z_][A-Za-z0-9_]*@(.+)", value)
    if variable_file is not None:
        found.append(variable_file.group(1).strip())
    for match in re.finditer(r"%output\{([^}]+)\}", value, re.IGNORECASE):
        candidate = match.group(1).strip()
        if candidate and candidate != "-":
            found.append(candidate)
    return tuple(found)


def _path_candidate(
    raw: str,
    *,
    cwd: Path,
    explicit: bool,
    allow_plain_existing: bool,
) -> Path | None:
    value = raw.strip()
    if not value or value.startswith("-") or _is_remote_url(value):
        return None
    path = Path(value).expanduser()
    candidate = path if path.is_absolute() else cwd / path
    path_shaped = any(marker in value for marker in ("/", "\\", "~")) or value.startswith(".")
    if not explicit and not path_shaped:
        if not allow_plain_existing or not (candidate.exists() or candidate.is_symlink()):
            return None
    return _resolved(candidate)


def _program_name(argv: Sequence[str]) -> str:
    if not argv:
        return ""
    name = Path(argv[0]).name.casefold()
    return name[:-4] if name.endswith(".exe") else name


def _long_option_name(raw: str) -> str:
    return raw[2:].split("=", 1)[0].strip().casefold().replace("_", "-")


def _is_remote_url(value: str) -> bool:
    return bool(_REMOTE_URL.match(value)) and not value.casefold().startswith("file://")


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)

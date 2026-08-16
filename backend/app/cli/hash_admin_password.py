"""交互式生成 DEMO_ADMIN_PASSWORD_HASH。"""

import sys
from getpass import getpass

from app.services.admin_sessions import hash_admin_password

# 没有 TTY 就没法关回显。与其让 getpass 退化成明文输入、或者抛一段
# termios + EOFError 的 traceback，不如直接说清楚该换到哪里去跑。
# 刻意不支持管道输入：`echo <口令> | ...` 会把口令留在 shell history 和 ps 里。
_NO_TTY = """需要一个真实终端才能安全输入口令（当前 stdin 不是 TTY，无法关闭回显）。

请在 Terminal / iTerm / IDE 内置终端里直接运行：

    cd backend && uv run python -m app.cli.hash_admin_password

不要用管道传入口令——那会把它留在 shell 历史和进程列表里。"""


def main() -> None:
    if not sys.stdin.isatty():
        raise SystemExit(_NO_TTY)
    password = getpass("Demo admin password: ")
    if password == "":
        raise SystemExit("口令不能为空")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("两次密码不一致")
    print(hash_admin_password(password))


if __name__ == "__main__":
    main()

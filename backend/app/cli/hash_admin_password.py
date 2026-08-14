"""交互式生成 DEMO_ADMIN_PASSWORD_HASH。"""

from getpass import getpass

from app.services.admin_sessions import hash_admin_password


def main() -> None:
    password = getpass("Demo admin password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("两次密码不一致")
    print(hash_admin_password(password))


if __name__ == "__main__":
    main()

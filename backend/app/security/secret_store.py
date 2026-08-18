"""本机密钥与 OAuth token 的加密存储。

数据库只保存 Fernet 密文；主密钥位于独立文件且权限固定为 0600。桌面应用是
单用户产品，因此这里不引入远程 KMS。备份数据库时若不同时备份主密钥，密文不可恢复。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(RuntimeError):
    pass


class LocalSecretStore:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path.expanduser().resolve()
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        path = self.key_path
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise SecretStoreError("SecretStore 主密钥必须是普通文件")
            try:
                key = path.read_bytes().strip()
                Fernet(key)
            except (OSError, ValueError) as error:
                raise SecretStoreError("SecretStore 主密钥无法读取或格式无效") from error
            self._restrict_permissions(path)
            return key
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise SecretStoreError("SecretStore 目录不能是符号链接")
        key = Fernet.generate_key()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return self._load_or_create_key()
        except OSError as error:
            raise SecretStoreError("无法创建 SecretStore 主密钥") from error
        self._restrict_permissions(path)
        return key

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError as error:
            raise SecretStoreError("无法限制 SecretStore 主密钥权限") from error

    def encrypt(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return "v1:" + self._fernet.encrypt(encoded).decode("ascii")

    def decrypt(self, ciphertext: str | None) -> dict[str, Any]:
        if not ciphertext:
            return {}
        if not ciphertext.startswith("v1:"):
            raise SecretStoreError("SecretStore 密文版本不受支持")
        try:
            decoded = self._fernet.decrypt(ciphertext[3:].encode("ascii"))
            payload = json.loads(decoded)
        except (InvalidToken, UnicodeError, json.JSONDecodeError) as error:
            raise SecretStoreError("SecretStore 密文损坏或主密钥不匹配") from error
        if not isinstance(payload, dict):
            raise SecretStoreError("SecretStore 密文内容不是 object")
        return {str(key): value for key, value in payload.items()}

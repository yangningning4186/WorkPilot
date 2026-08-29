"""本机密钥与 OAuth token 的加密存储。

数据库只保存 Fernet 密文；主密钥位于独立文件。POSIX 使用 0700 目录/0600 文件，
Windows 断开继承并只授权当前用户。桌面应用是单用户产品，因此这里不引入远程 KMS。
备份数据库时若不同时备份主密钥，密文不可恢复。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreError(RuntimeError):
    pass


_IS_WINDOWS = sys.platform == "win32"


def _windows_current_user_sid() -> str:
    """从当前进程 token 读取 SID，不信任可被覆盖的 USERNAME/USERDOMAIN 环境变量。"""

    import importlib
    from ctypes import wintypes

    ctypes: Any = importlib.import_module("ctypes")

    token_query = 0x0008
    token_user_class = 1

    class SidAndAttributes(ctypes.Structure):  # type: ignore[misc]
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("sid", wintypes.LPVOID),
            ("attributes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            token_user_class,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value == 0:
            raise OSError(ctypes.get_last_error(), "GetTokenInformation size failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        token_user = ctypes.cast(buffer, ctypes.POINTER(SidAndAttributes)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.sid, ctypes.byref(sid_text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            if not sid_text.value:
                raise OSError("current user SID is empty")
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _windows_dacl_sddl(sid: str, *, is_dir: bool) -> str:
    # D:P = protected DACL（不继承）；整个 DACL 只有当前 SID 的一个 allow ACE。
    inheritance = "OICI" if is_dir else ""
    return f"D:P(A;{inheritance};FA;;;{sid})"


def _apply_windows_dacl(path: Path, *, sddl: str) -> None:
    """用 Win32 API 整体替换 DACL；不会保留既有 Everyone/其他显式 ACE。"""

    import importlib
    from ctypes import wintypes

    ctypes: Any = importlib.import_module("ctypes")

    sddl_revision_1 = 1
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL

    descriptor = wintypes.LPVOID()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        sddl_revision_1,
        ctypes.byref(descriptor),
        None,
    ):
        raise OSError(ctypes.get_last_error(), "invalid SecretStore DACL")
    try:
        if not advapi32.SetFileSecurityW(
            str(path),
            dacl_security_information | protected_dacl_security_information,
            descriptor,
        ):
            raise OSError(ctypes.get_last_error(), "SetFileSecurityW failed")
    finally:
        kernel32.LocalFree(descriptor)


def _apply_windows_dacl_to_fd(descriptor: int, *, sddl: str) -> None:
    """在已打开的同一个 Windows file handle 上整体替换 DACL。"""

    import importlib
    from ctypes import wintypes

    ctypes: Any = importlib.import_module("ctypes")
    msvcrt: Any = importlib.import_module("msvcrt")

    sddl_revision_1 = 1
    dacl_security_information = 0x00000004
    protected_dacl_security_information = 0x80000000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    advapi32.SetSecurityInfo.restype = wintypes.DWORD

    security_descriptor = wintypes.LPVOID()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        sddl_revision_1,
        ctypes.byref(security_descriptor),
        None,
    ):
        raise OSError(ctypes.get_last_error(), "invalid SecretStore DACL")
    try:
        dacl_present = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        dacl_defaulted = wintypes.BOOL()
        if not advapi32.GetSecurityDescriptorDacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            raise OSError(ctypes.get_last_error(), "invalid SecretStore DACL")
        # NULL DACL 等价于向所有人开放，必须显式 fail closed。
        if not dacl_present.value or not dacl.value:
            raise OSError("SecretStore DACL is missing")
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        result = advapi32.SetSecurityInfo(
            handle,
            1,  # SE_FILE_OBJECT
            dacl_security_information | protected_dacl_security_information,
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(int(result), "SetSecurityInfo failed")
    finally:
        kernel32.LocalFree(security_descriptor)


def _open_windows_key_no_reparse(path: Path) -> int:
    """打开最终对象本身且禁止替换；reparse point 一律拒绝，返回 Python fd。"""

    import importlib
    from ctypes import wintypes

    ctypes: Any = importlib.import_module("ctypes")
    msvcrt: Any = importlib.import_module("msvcrt")

    generic_read = 0x80000000
    write_dac = 0x00040000
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_attribute_reparse_point = 0x00000400
    file_flag_open_reparse_point = 0x00200000

    class ByHandleFileInformation(ctypes.Structure):  # type: ignore[misc]
        _fields_: ClassVar[list[tuple[str, Any]]] = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        str(path),
        generic_read | write_dac,
        0,  # 不共享 read/write/delete；ACL 设置和读取期间路径不能被替换。
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        information = ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        if information.file_attributes & file_attribute_reparse_point:
            raise SecretStoreError("SecretStore 主密钥不能是 reparse point")
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0))
        handle = None
        return int(fd)
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def _create_windows_key_no_reparse(path: Path) -> int:
    """用 CREATE_NEW 建立 key，并保留 WRITE_DAC 供同一 handle 立即收窄权限。"""

    import importlib
    from ctypes import wintypes

    ctypes: Any = importlib.import_module("ctypes")
    msvcrt: Any = importlib.import_module("msvcrt")

    generic_write = 0x40000000
    write_dac = 0x00040000
    create_new = 1
    file_attribute_normal = 0x00000080
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        str(path),
        generic_write | write_dac,
        0,
        None,
        create_new,
        file_attribute_normal,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error_code = int(ctypes.get_last_error())
        if error_code in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
            raise FileExistsError(error_code, "SecretStore key already exists")
        raise OSError(error_code, "CreateFileW failed")
    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
        handle = None
        return int(descriptor)
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def _restrict_to_current_user(path: Path, *, is_dir: bool) -> None:
    """收窄 SecretStore 控制面权限；任何失败都阻止读取或写入 key。"""

    if not _IS_WINDOWS:
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
        except OSError:
            raise SecretStoreError("无法限制 SecretStore 权限") from None
        return

    try:
        sid = _windows_current_user_sid()
        _apply_windows_dacl(
            path,
            sddl=_windows_dacl_sddl(sid, is_dir=is_dir),
        )
    except (OSError, ValueError):
        # Win32 异常可能包含本机路径/账户；错误边界只给固定文案。
        raise SecretStoreError("无法限制 SecretStore Windows ACL") from None


def _restrict_open_windows_file(descriptor: int) -> None:
    """收窄已锁定 key handle 的 DACL，避免重新按路径打开造成 TOCTOU/共享冲突。"""

    try:
        sid = _windows_current_user_sid()
        _apply_windows_dacl_to_fd(
            descriptor,
            sddl=_windows_dacl_sddl(sid, is_dir=False),
        )
    except (OSError, ValueError):
        raise SecretStoreError("无法限制 SecretStore Windows ACL") from None


class LocalSecretStore:
    def __init__(self, key_path: Path) -> None:
        # absolute() 只规范化路径，不像 resolve() 那样先跟随最终 symlink；后续检查必须
        # 看见调用方给出的链接本身，才能维持“主密钥不能是符号链接”的边界。
        self.key_path = key_path.expanduser().absolute()
        encoded_key = self._load_or_create_key()
        self._fernet = Fernet(encoded_key)
        # Fernet key 是 urlsafe-base64 编码的 32 bytes。只在进程内保留原始材料，用带域
        # 分隔的 HMAC 派生控制面签名键；派生值和 checkpoint 分离，篡改数据库本身无法
        # 同时重签审批 receipt。
        self._key_material = base64.urlsafe_b64decode(encoded_key)

    def _load_or_create_key(self) -> bytes:
        path = self.key_path
        if path.is_symlink():
            raise SecretStoreError("SecretStore 主密钥不能是符号链接")
        self._prepare_directory(path.parent)
        if path.exists():
            return self._read_existing_key(path)
        key = Fernet.generate_key()
        descriptor: int | None = None
        created_identity: os.stat_result | None = None
        try:
            if _IS_WINDOWS:
                descriptor = _create_windows_key_no_reparse(path)
            else:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                descriptor = os.open(path, flags, 0o600)
            created_identity = os.fstat(descriptor)
            # Windows 的 mode 参数不建立 DACL。文件仍为空时先设 ACL，避免 secret bytes
            # 在“写完再设 DACL”的窗口继承宽权限。
            if _IS_WINDOWS:
                _restrict_open_windows_file(descriptor)
            else:
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(key + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return self._load_or_create_key()
        except SecretStoreError:
            if descriptor is not None:
                os.close(descriptor)
            # 这是本调用 O_EXCL 创建且尚未写入 secret 的文件。清掉它，让用户修复 ACL
            # 后可直接重试；清理失败也只留下空文件，不降级使用。
            self._unlink_created_file(path, created_identity)
            raise
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            # write/flush/fsync 失败时 fdopen 已接管并关闭 descriptor；仍按 O_EXCL
            # 创建时记录的 identity 清理同一对象，避免留下 partial/corrupt key。
            self._unlink_created_file(path, created_identity)
            raise SecretStoreError("无法创建 SecretStore 主密钥") from error
        return key

    def _read_existing_key(self, path: Path) -> bytes:
        descriptor: int | None = None
        try:
            if _IS_WINDOWS:
                descriptor = _open_windows_key_no_reparse(path)
                # share mode 0 的 handle 已锁定同一非-reparse 对象；在读 secret bytes
                # 之前在同一 handle 上整体替换 DACL。不能再用 path 重新打开，否则
                # SetFileSecurityW 会与独占 handle 产生 sharing violation。
                _restrict_open_windows_file(descriptor)
            else:
                flags = os.O_RDONLY
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                no_follow = getattr(os, "O_NOFOLLOW", None)
                if no_follow is None:
                    raise SecretStoreError("当前平台无法安全读取 SecretStore 主密钥")
                flags |= no_follow
                descriptor = os.open(path, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretStoreError("SecretStore 主密钥必须是普通文件")
            if not _IS_WINDOWS:
                # 对已经打开的同一 inode 收权限并读取，避免 is_file/chmod/read_bytes 间
                # 被换成 symlink 的 TOCTOU。
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                key = stream.read(4_097).strip()
            if len(key) > 4_096:
                raise SecretStoreError("SecretStore 主密钥无法读取或格式无效")
            Fernet(key)
            return key
        except SecretStoreError:
            raise
        except (OSError, ValueError):
            raise SecretStoreError("SecretStore 主密钥无法读取或格式无效") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        # 先检查再 mkdir，避免已知 symlink/junction 祖先把目录创建到意外位置；
        # mkdir 后复查，用 fail-closed 方式覆盖检查与创建之间的大多数替换竞争。
        LocalSecretStore._reject_symlink_components(path)
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise SecretStoreError("无法创建 SecretStore 目录") from error
        LocalSecretStore._reject_symlink_components(path)
        if not path.is_dir():
            raise SecretStoreError("SecretStore 目录必须是普通目录且不能是符号链接")
        _restrict_to_current_user(path, is_dir=True)

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = path
        while True:
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                pass
            except OSError:
                raise SecretStoreError("无法验证 SecretStore 目录路径") from None
            else:
                if stat.S_ISLNK(metadata.st_mode) or (
                    _IS_WINDOWS and hasattr(current, "is_junction") and current.is_junction()
                ):
                    raise SecretStoreError("SecretStore 目录路径不能包含符号链接或 junction")
            parent = current.parent
            if parent == current:
                return
            current = parent

    @staticmethod
    def _unlink_created_file(path: Path, identity: os.stat_result | None) -> None:
        if identity is None:
            return
        try:
            current = os.lstat(path)
            if current.st_dev == identity.st_dev and current.st_ino == identity.st_ino:
                path.unlink()
        except OSError:
            pass

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

    def derive_signing_key(self, purpose: str) -> str:
        """为内部协议派生独立 HMAC key，不复用或暴露 Fernet 主密钥。"""

        if not purpose or len(purpose) > 512 or "\x00" in purpose:
            raise SecretStoreError("SecretStore 签名用途无效")
        payload = b"workpilot-local-signing-key-v1\0" + purpose.encode("utf-8")
        return hmac.new(self._key_material, payload, hashlib.sha256).hexdigest()

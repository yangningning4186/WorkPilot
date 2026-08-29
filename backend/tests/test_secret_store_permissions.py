import os
import stat
from pathlib import Path

import pytest

import app.security.secret_store as secret_store_module
from app.security.secret_store import LocalSecretStore, SecretStoreError


def test_secret_store_keeps_posix_directory_and_key_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_store_module, "_IS_WINDOWS", False)
    key_path = tmp_path / "secrets" / "master.key"

    LocalSecretStore(key_path)

    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_secret_store_derives_domain_separated_signing_keys_without_persisting_them(
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "secrets" / "master.key"
    first = LocalSecretStore(key_path)
    approval_key = first.derive_signing_key("semantic-approval:v1:run-1")

    assert len(approval_key) == 64
    assert approval_key == LocalSecretStore(key_path).derive_signing_key(
        "semantic-approval:v1:run-1"
    )
    assert approval_key != first.derive_signing_key("semantic-approval:v1:run-2")
    assert approval_key.encode("ascii") not in key_path.read_bytes()


def test_secret_store_rejects_a_master_key_symlink_before_reading_it(tmp_path: Path) -> None:
    target = tmp_path / "real.key"
    target.write_text("not-a-key", encoding="utf-8")
    link = tmp_path / "master.key"
    link.symlink_to(target)

    with pytest.raises(SecretStoreError, match="符号链接"):
        LocalSecretStore(link)


def test_secret_store_rejects_a_symlinked_directory_ancestor(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    key_path = linked_parent / "secrets" / "master.key"

    with pytest.raises(SecretStoreError, match="符号链接"):
        LocalSecretStore(key_path)

    assert not key_path.exists()
    assert not (real_parent / "secrets").exists()


def test_posix_existing_key_is_hardened_and_read_through_the_same_nofollow_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_store_module, "_IS_WINDOWS", False)
    key_path = tmp_path / "secrets" / "master.key"
    LocalSecretStore(key_path)
    real_open = secret_store_module.os.open
    real_fchmod = secret_store_module.os.fchmod
    opened: list[tuple[int, int]] = []
    hardened: list[tuple[int, int]] = []

    def spy_open(path: Path, flags: int, *args: int) -> int:
        descriptor = real_open(path, flags, *args)
        opened.append((descriptor, flags))
        return descriptor

    def spy_fchmod(descriptor: int, mode: int) -> None:
        hardened.append((descriptor, mode))
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(secret_store_module.os, "open", spy_open)
    monkeypatch.setattr(secret_store_module.os, "fchmod", spy_fchmod)

    LocalSecretStore(key_path)

    assert len(opened) == 1
    assert opened[0][1] & os.O_NOFOLLOW
    assert hardened == [(opened[0][0], 0o600)]


def test_windows_acl_replaces_the_whole_dacl_before_writing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = "S-1-5-21-1000"
    applied: list[tuple[str, str]] = []

    def fake_apply(path: Path, *, sddl: str) -> None:
        applied.append((str(path), sddl))

    def fake_create(path: Path) -> int:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def fake_apply_to_fd(descriptor: int, *, sddl: str) -> None:
        assert os.fstat(descriptor).st_size == 0
        applied.append(("open-fd", sddl))

    monkeypatch.setattr(secret_store_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(secret_store_module, "_windows_current_user_sid", lambda: sid)
    monkeypatch.setattr(secret_store_module, "_apply_windows_dacl", fake_apply)
    monkeypatch.setattr(secret_store_module, "_create_windows_key_no_reparse", fake_create)
    monkeypatch.setattr(secret_store_module, "_apply_windows_dacl_to_fd", fake_apply_to_fd)
    key_path = tmp_path / "secrets" / "master.key"

    store = LocalSecretStore(key_path)
    assert store.decrypt(store.encrypt({"token": "private"})) == {"token": "private"}

    assert applied == [
        (str(key_path.parent), f"D:P(A;OICI;FA;;;{sid})"),
        ("open-fd", f"D:P(A;;FA;;;{sid})"),
    ]
    assert "WD" not in applied[1][1]  # Everyone SID alias 不在替换后的 DACL 中。


def test_windows_existing_broad_acl_is_replaced_before_same_handle_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "secrets" / "master.key"
    first = LocalSecretStore(key_path)
    ciphertext = first.encrypt({"token": "private"})
    events: list[str] = []

    def fake_apply(path: Path, *, sddl: str) -> None:
        events.append(f"dacl:{path.name}:{sddl}")

    def fake_safe_open(path: Path) -> int:
        events.append(f"open:{path.name}")
        return os.open(path, os.O_RDONLY)

    def fake_apply_to_fd(descriptor: int, *, sddl: str) -> None:
        os.fstat(descriptor)
        events.append(f"dacl:open-fd:{sddl}")

    monkeypatch.setattr(secret_store_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        secret_store_module,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-1000",
    )
    monkeypatch.setattr(secret_store_module, "_apply_windows_dacl", fake_apply)
    monkeypatch.setattr(secret_store_module, "_apply_windows_dacl_to_fd", fake_apply_to_fd)
    monkeypatch.setattr(secret_store_module, "_open_windows_key_no_reparse", fake_safe_open)

    loaded = LocalSecretStore(key_path)

    assert loaded.decrypt(ciphertext) == {"token": "private"}
    assert events[0].startswith("dacl:secrets:D:P(A;OICI;FA;;;")
    assert events[1] == "open:master.key"
    assert events[2].startswith("dacl:open-fd:D:P(A;;FA;;;")


def test_windows_existing_key_handle_acl_failure_closes_fd_and_hides_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "secrets" / "master.key"
    LocalSecretStore(key_path)
    leaked = "private-win32-diagnostic"
    opened: list[int] = []

    def fake_safe_open(path: Path) -> int:
        descriptor = os.open(path, os.O_RDONLY)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(secret_store_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        secret_store_module,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-1000",
    )
    monkeypatch.setattr(secret_store_module, "_apply_windows_dacl", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        secret_store_module,
        "_apply_windows_dacl_to_fd",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError(leaked)),
    )
    monkeypatch.setattr(secret_store_module, "_open_windows_key_no_reparse", fake_safe_open)

    with pytest.raises(SecretStoreError, match="Windows ACL") as raised:
        LocalSecretStore(key_path)

    assert leaked not in str(raised.value)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_windows_key_acl_failure_is_fail_closed_and_does_not_leak_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaked = "super-secret-from-win32"

    def fake_create(path: Path) -> int:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def fake_apply_to_fd(descriptor: int, *, sddl: str) -> None:
        assert os.fstat(descriptor).st_size == 0
        assert sddl.startswith("D:P")
        raise OSError(leaked)

    monkeypatch.setattr(secret_store_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        secret_store_module,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-1000",
    )
    monkeypatch.setattr(secret_store_module, "_apply_windows_dacl", lambda *_a, **_kw: None)
    monkeypatch.setattr(secret_store_module, "_create_windows_key_no_reparse", fake_create)
    monkeypatch.setattr(secret_store_module, "_apply_windows_dacl_to_fd", fake_apply_to_fd)
    key_path = tmp_path / "secrets" / "master.key"

    with pytest.raises(SecretStoreError, match="Windows ACL") as raised:
        LocalSecretStore(key_path)

    assert leaked not in str(raised.value)
    assert not key_path.exists()


def test_partial_key_is_removed_when_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "secrets" / "master.key"
    leaked = "filesystem-diagnostic"
    monkeypatch.setattr(
        secret_store_module.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError(leaked)),
    )

    with pytest.raises(SecretStoreError, match="无法创建") as raised:
        LocalSecretStore(key_path)

    assert leaked not in str(raised.value)
    assert not key_path.exists()


def test_windows_directory_acl_failure_prevents_key_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_store_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        secret_store_module,
        "_windows_current_user_sid",
        lambda: "S-1-5-21-1000",
    )
    monkeypatch.setattr(
        secret_store_module,
        "_apply_windows_dacl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("access denied")),
    )
    key_path = tmp_path / "secrets" / "master.key"

    with pytest.raises(SecretStoreError, match="Windows ACL"):
        LocalSecretStore(key_path)

    assert not key_path.exists()

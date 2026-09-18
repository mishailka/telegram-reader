"""Private account storage, outside the distributable plugin directory."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("TELEGRAM_READER_DATA_DIR")
    base = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "TelegramReader" / "account"
    base.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        base.chmod(0o700)
    return base.resolve()


def protect(raw: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Version 0.2 supports Windows DPAPI storage only.")
    import win32crypt
    return win32crypt.CryptProtectData(raw, "Telegram Reader", None, None, None, 1)


def unprotect(raw: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptUnprotectData(raw, None, None, None, 1)[1]


def load_account() -> dict | None:
    path = data_dir() / "account.dpapi"
    if not path.exists():
        return None
    return json.loads(unprotect(path.read_bytes()))


def save_account(account: dict) -> None:
    root = data_dir()
    encrypted = protect(json.dumps(account).encode("utf-8"))
    descriptor, temporary = tempfile.mkstemp(dir=root, prefix="account-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encrypted)
        os.replace(temporary, root / "account.dpapi")
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_account() -> None:
    (data_dir() / "account.dpapi").unlink(missing_ok=True)

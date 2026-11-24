"""Independent Butler pairing secrets, never read from legacy Hermex state."""

from __future__ import annotations

import base64
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from typing import Callable

from . import PLUGIN_NAME

PREVIEW_KEY_BYTES = 32
INSTALL_KEY_HEX_CHARS = 64


def hermes_root() -> Path:
    """One server install, not a separate key per agent profile."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except ImportError:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
        return home.parents[1] if home.parent.name == "profiles" else home


def key_dir(home: Path | None = None) -> Path:
    return (home or hermes_root()) / "plugin-data" / PLUGIN_NAME


@dataclass(frozen=True, repr=False)
class Keys:
    install_key: str
    preview_key: bytes

    @property
    def preview_key_b64(self) -> str:
        return base64.b64encode(self.preview_key).decode("ascii")


def _read_or_create(path: Path, make: Callable[[], bytes], max_bytes: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        data = make()
        fd, name = tempfile.mkstemp(prefix=".key-", dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return data
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Unexpected Butler pairing key file")
        os.fchmod(handle.fileno(), 0o600)
        return handle.read(max_bytes + 1)


def load_or_create_keys(home: Path | None = None) -> Keys:
    directory = key_dir(home)
    if directory.parent.is_symlink():
        raise ValueError("Unexpected Butler pairing state parent")
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("Unexpected Butler pairing state directory")
    directory.chmod(0o700)
    fd = os.open(directory / ".keys.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError("Unexpected Butler pairing lock file")
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        install = _read_or_create(directory / "install_key", lambda: secrets.token_hex(32).encode("ascii"), 64)
        preview = _read_or_create(directory / "preview_key", lambda: secrets.token_bytes(32), 32)
    if re.fullmatch(rb"[0-9a-f]{64}", install) is None or len(preview) != PREVIEW_KEY_BYTES:
        raise ValueError("Malformed Butler pairing state; no keys were rotated")
    return Keys(install_key=install.decode("ascii"), preview_key=preview)

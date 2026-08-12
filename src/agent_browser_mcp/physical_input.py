"""Cross-process lease and user-activity gate for physical input actions."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import math
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable, TypeVar


class PhysicalInputBusy(RuntimeError):
    """Raised when another process owns the physical-input lease."""


class InputActivityDetected(RuntimeError):
    """Raised when user input changes during the quiet window."""


_T = TypeVar("_T")
_Marker = tuple[int | None, int | None, int | None]


def _pid_alive(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        still_active = 259
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER is how OpenProcess reports a PID
                # that does not exist. Access-denied and unknown failures do
                # not prove death, so they must not authorize lock stealing.
                return kernel32.GetLastError() != 87
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _record_token(record: dict[str, Any] | None) -> str | None:
    if record is None:
        return None
    token = record.get("owner_token")
    return token if isinstance(token, str) and token else None


def _lock_guard_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise PhysicalInputBusy("physical-input arbitration is busy") from None


def _unlock_guard_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # Closing the fd below also releases an advisory lock owned by it.
        pass


class _ArbitrationGuard:
    """Non-blocking OS lock used for metadata changes and live ownership."""

    def __init__(self, lease_path: Path) -> None:
        self.path = Path(f"{lease_path}.guard")
        self._fd: int | None = None

    def __enter__(self) -> _ArbitrationGuard:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # Windows byte-range locking needs a persistent byte to lock.
            if os.fstat(fd).st_size < 1:
                os.ftruncate(fd, 1)
            _lock_guard_fd(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                _unlock_guard_fd(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _unlink_if_token(path: Path, owner_token: str) -> bool:
    """Unlink a matching record; caller must hold the sibling guard."""
    record = _read_record(path)
    if _record_token(record) != owner_token:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> None:
    """Unlink the created inode; caller must hold the sibling guard."""
    try:
        current = path.stat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except OSError:
        pass


def _record_is_stale(record: dict[str, Any], now: float) -> bool:
    expires_at = record.get("expires_at")
    expired = (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and math.isfinite(expires_at)
        and expires_at <= now
    )
    pid = record.get("pid")
    owner_dead = (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and not _pid_alive(pid)
    )
    return expired or owner_dead


class PhysicalInputLease:
    """A non-queued, process-safe lease represented by one JSON lock file."""

    def __init__(
        self,
        *,
        path: str | os.PathLike[str],
        ttl_seconds: float = 30.0,
        action_summary: str = "physical input",
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive finite number")
        if not isinstance(action_summary, str) or not action_summary:
            raise ValueError("action_summary must be a non-empty string")
        self.path = Path(path)
        self.ttl_seconds = float(ttl_seconds)
        self.action_summary = action_summary
        self.owner_token = secrets.token_hex(16)
        self._acquired = False
        self._guard: _ArbitrationGuard | None = None

    def _create(self) -> None:
        created_at = time.time()
        record = {
            "owner_token": self.owner_token,
            "pid": os.getpid(),
            "action_summary": self.action_summary,
            "created_at": created_at,
            "expires_at": created_at + self.ttl_seconds,
        }
        payload = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            opened = os.fstat(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            # _create always runs under the arbitration guard, so no compliant
            # contender can replace the empty pathname before this cleanup.
            try:
                self.path.unlink()
            except OSError:
                pass
            raise
        identity = (opened.st_dev, opened.st_ino)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("could not write physical-input lease")
                offset += written
            os.fsync(fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            _unlink_if_identity(self.path, identity)
            raise
        else:
            try:
                os.close(fd)
            except BaseException:
                _unlink_if_identity(self.path, identity)
                raise

    def _recover_stale(self) -> bool:
        record = _read_record(self.path)
        token = _record_token(record)
        if record is None or token is None or not _record_is_stale(record, time.time()):
            return False
        return _unlink_if_token(self.path, token)

    def __enter__(self) -> PhysicalInputLease:
        if self._acquired:
            raise RuntimeError("physical-input lease is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        guard = _ArbitrationGuard(self.path)
        guard.__enter__()
        try:
            try:
                self._create()
            except FileExistsError:
                if not self._recover_stale():
                    raise PhysicalInputBusy(f"physical input is busy: {self.path}") from None
                try:
                    self._create()
                except FileExistsError:
                    raise PhysicalInputBusy(f"physical input is busy: {self.path}") from None
        except BaseException:
            guard.__exit__(None, None, None)
            raise
        self._guard = guard
        self._acquired = True
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._acquired:
            guard = self._guard
            try:
                _unlink_if_token(self.path, self.owner_token)
            except OSError:
                # Leaving the tokenized record is safe. Once this guard is
                # released, expiry/dead-PID recovery can reclaim it.
                pass
            finally:
                self._guard = None
                self._acquired = False
                if guard is not None:
                    guard.__exit__(exc_type, exc_value, traceback)


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]


def _windows_pointer_position() -> tuple[int | None, int | None]:
    point = _Point()
    try:
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return int(point.x), int(point.y)
    except (AttributeError, OSError):
        pass
    return None, None


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _macos_pointer_position() -> tuple[int | None, int | None]:
    library_name = ctypes.util.find_library("ApplicationServices")
    if not library_name:
        return None, None
    event = None
    try:
        application_services = ctypes.CDLL(library_name)
        application_services.CGEventCreate.argtypes = [ctypes.c_void_p]
        application_services.CGEventCreate.restype = ctypes.c_void_p
        application_services.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        application_services.CGEventGetLocation.restype = _CGPoint
        application_services.CFRelease.argtypes = [ctypes.c_void_p]
        event = application_services.CGEventCreate(None)
        if not event:
            return None, None
        point = application_services.CGEventGetLocation(event)
        return int(point.x), int(point.y)
    except (AttributeError, OSError):
        return None, None
    finally:
        if event:
            application_services.CFRelease(event)


def _x11_pointer_position() -> tuple[int | None, int | None]:
    library_name = ctypes.util.find_library("X11")
    if not library_name:
        return None, None
    display = None
    try:
        x11 = ctypes.CDLL(library_name)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(None)
        if not display:
            return None, None
        root = x11.XDefaultRootWindow(display)
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        window_x = ctypes.c_int()
        window_y = ctypes.c_int()
        mask = ctypes.c_uint()
        available = x11.XQueryPointer(
            display,
            root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(window_x),
            ctypes.byref(window_y),
            ctypes.byref(mask),
        )
        if available:
            return int(root_x.value), int(root_y.value)
        return None, None
    except (AttributeError, OSError):
        return None, None
    finally:
        if display:
            x11.XCloseDisplay(display)


def _pointer_position() -> tuple[int | None, int | None]:
    if sys.platform == "win32":
        return _windows_pointer_position()
    if sys.platform == "darwin":
        return _macos_pointer_position()
    return _x11_pointer_position()


def last_input_marker() -> _Marker:
    """Return the available OS input timestamp and current pointer position."""
    last_input_time: int | None = None
    if sys.platform == "win32":
        info = _LastInputInfo(cbSize=ctypes.sizeof(_LastInputInfo))
        try:
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                last_input_time = int(info.dwTime)
        except (AttributeError, OSError):
            pass
    pointer_x, pointer_y = _pointer_position()
    return last_input_time, pointer_x, pointer_y


def wait_for_quiet(quiet_seconds: float = 0.75) -> None:
    """Raise if any marker available in both samples changes in the window."""
    if (
        isinstance(quiet_seconds, bool)
        or not isinstance(quiet_seconds, (int, float))
        or not math.isfinite(quiet_seconds)
        or quiet_seconds < 0
    ):
        raise ValueError("quiet_seconds must be a non-negative finite number")
    before = last_input_marker()
    time.sleep(quiet_seconds)
    after = last_input_marker()
    if any(
        old is not None and new is not None and old != new
        for old, new in zip(before, after)
    ):
        raise InputActivityDetected("physical input changed during the quiet window")


def _default_lock_path() -> Path:
    return Path.home() / ".agent-browser-mcp" / "physical-input.lock"


def run_physical_action(
    action_summary: str,
    action: Callable[[], _T],
    *,
    lock_path: str | os.PathLike[str] | None = None,
    quiet_seconds: float = 0.75,
    ttl_seconds: float = 30.0,
) -> _T:
    """Run one action closure after acquiring the lease and quiet gate."""
    path = _default_lock_path() if lock_path is None else Path(lock_path)
    with PhysicalInputLease(path=path, ttl_seconds=ttl_seconds, action_summary=action_summary):
        wait_for_quiet(quiet_seconds)
        return action()

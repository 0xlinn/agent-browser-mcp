"""Process-safe physical-input lease tests with no real OS input."""

from __future__ import annotations

import json
import multiprocessing
import multiprocessing.spawn
import os
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent_browser_mcp import physical_input as P
from agent_browser_mcp import server as S
from agent_browser_mcp.physical_input import (
    InputActivityDetected,
    PhysicalInputBusy,
    PhysicalInputLease,
)


def _hold_metadata_guard(path, ready, release):
    """Spawn-safe target that holds the real platform advisory guard."""
    from agent_browser_mcp.physical_input import _ArbitrationGuard

    with _ArbitrationGuard(Path(path)):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("parent did not release guard holder")


def _exit_while_holding_metadata_guard(path, ready, exit_now):
    """Abruptly end a holder so the OS must release its advisory lock."""
    from agent_browser_mcp.physical_input import _ArbitrationGuard

    with _ArbitrationGuard(Path(path)):
        ready.set()
        if not exit_now.wait(10):
            raise TimeoutError("parent did not terminate guard holder")
        os._exit(1)


def _try_metadata_guard(path, started, done, outcome):
    """Spawn-safe contender that reports immediate guard arbitration."""
    from agent_browser_mcp.physical_input import _ArbitrationGuard, PhysicalInputBusy

    started.set()
    try:
        with _ArbitrationGuard(Path(path)):
            outcome.value = 1
    except PhysicalInputBusy:
        outcome.value = 0
    finally:
        done.set()


def _try_lease(path, started, done, outcome):
    """Spawn-safe lease reacquisition probe with a bounded parent wait."""
    from agent_browser_mcp.physical_input import PhysicalInputBusy, PhysicalInputLease

    started.set()
    try:
        with PhysicalInputLease(path=Path(path), ttl_seconds=10):
            outcome.value = 1
    except PhysicalInputBusy:
        outcome.value = 0
    finally:
        done.set()


def _hold_lease(path, ttl_seconds, ready, release):
    """Hold the real lifetime lease until the parent releases this process."""
    from agent_browser_mcp.physical_input import PhysicalInputLease

    with PhysicalInputLease(path=Path(path), ttl_seconds=ttl_seconds, action_summary="holder"):
        ready.set()
        if not release.wait(10):
            raise TimeoutError("parent did not release lease holder")


def _exit_while_holding_lease(path, ready, exit_now):
    """Exit abruptly so the OS lock releases while the JSON record remains."""
    from agent_browser_mcp.physical_input import PhysicalInputLease

    lease = PhysicalInputLease(path=Path(path), ttl_seconds=10, action_summary="abrupt")
    lease.__enter__()
    ready.set()
    if not exit_now.wait(10):
        raise TimeoutError("parent did not terminate lease holder")
    os._exit(1)


def _cleanup_child(process, release):
    release.set()
    process.join(2)
    if process.is_alive():
        process.terminate()
        process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)


def _spawn_guard_holder(path):
    """Start a holder without the Windows venv launcher indirection."""
    previous_executable = multiprocessing.spawn.get_executable()
    multiprocessing.set_executable(sys._base_executable)
    try:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(target=_hold_metadata_guard, args=(str(path), ready, release))
        process.start()
    finally:
        multiprocessing.set_executable(os.fsdecode(previous_executable))
    return process, ready, release


def _spawn_exiting_guard_holder(path):
    """Start an abruptly exiting holder with the base CPython executable."""
    previous_executable = multiprocessing.spawn.get_executable()
    multiprocessing.set_executable(sys._base_executable)
    try:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        exit_now = context.Event()
        process = context.Process(
            target=_exit_while_holding_metadata_guard,
            args=(str(path), ready, exit_now),
        )
        process.start()
    finally:
        multiprocessing.set_executable(os.fsdecode(previous_executable))
    return process, ready, exit_now


def _spawn_lease_holder(path, ttl_seconds=10, abrupt=False):
    """Start an actual lease owner with the base CPython executable."""
    previous_executable = multiprocessing.spawn.get_executable()
    multiprocessing.set_executable(sys._base_executable)
    try:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        target = _exit_while_holding_lease if abrupt else _hold_lease
        args = (str(path), ready, release) if abrupt else (str(path), ttl_seconds, ready, release)
        process = context.Process(target=target, args=args)
        process.start()
    finally:
        multiprocessing.set_executable(os.fsdecode(previous_executable))
    return process, ready, release


def _record(*, token="other", pid=None, created_at=10, expires_at=20, action_summary="other"):
    return {
        "owner_token": token,
        "pid": os.getpid() if pid is None else pid,
        "action_summary": action_summary,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def test_second_lease_fails_immediately(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    monkeypatch.setattr(P.time, "sleep", lambda _seconds: pytest.fail("lease contention must not wait"))

    with PhysicalInputLease(path=path, ttl_seconds=10):
        with pytest.raises(PhysicalInputBusy):
            with PhysicalInputLease(path=path, ttl_seconds=10):
                pass


def test_process_guard_contention_is_visible_and_non_blocking(tmp_path):
    path = tmp_path / "input.lock"
    process, ready, release = _spawn_guard_holder(path)
    contender = None
    try:
        assert ready.wait(10), f"guard holder did not start (exitcode={process.exitcode})"
        context = multiprocessing.get_context("spawn")
        contender_started = context.Event()
        contender_done = context.Event()
        contender_outcome = context.Value("i", -1)
        previous_executable = multiprocessing.spawn.get_executable()
        multiprocessing.set_executable(sys._base_executable)
        try:
            contender = context.Process(
                target=_try_metadata_guard,
                args=(str(path), contender_started, contender_done, contender_outcome),
            )
            contender.start()
        finally:
            multiprocessing.set_executable(os.fsdecode(previous_executable))
        assert contender_started.wait(10), f"guard contender did not start (exitcode={contender.exitcode})"
        started_at = time.monotonic()
        assert contender_done.wait(5), f"guard contender did not finish (exitcode={contender.exitcode})"
        assert contender_outcome.value == 0
        elapsed = time.monotonic() - started_at
        assert elapsed < 1.0
        contender.join(10)
        assert not contender.is_alive()
        assert contender.exitcode == 0
        assert process.is_alive()

        release.set()
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        if contender is not None:
            _cleanup_child(contender, contender_started)
        _cleanup_child(process, release)

    with PhysicalInputLease(path=path, ttl_seconds=10):
        assert path.exists()


def test_process_death_releases_guard_without_file_cleanup(tmp_path):
    path = tmp_path / "input.lock"
    guard_path = path.parent / f"{path.name}.guard"
    process, ready, exit_now = _spawn_exiting_guard_holder(path)
    try:
        assert ready.wait(10), f"guard holder did not start (exitcode={process.exitcode})"
        assert guard_path.exists()
        exit_now.set()
        process.join(5)
        assert not process.is_alive()
        assert process.exitcode != 0

        context = multiprocessing.get_context("spawn")
        reacquire_started = context.Event()
        reacquire_done = context.Event()
        reacquire_outcome = context.Value("i", -1)
        reacquirer = None
        previous_executable = multiprocessing.spawn.get_executable()
        multiprocessing.set_executable(sys._base_executable)
        try:
            reacquirer = context.Process(
                target=_try_lease,
                args=(str(path), reacquire_started, reacquire_done, reacquire_outcome),
            )
            reacquirer.start()
        finally:
            multiprocessing.set_executable(os.fsdecode(previous_executable))
        assert reacquire_started.wait(10), f"reacquirer did not start (exitcode={reacquirer.exitcode})"
        probe_started_at = time.monotonic()
        assert reacquire_done.wait(5), f"reacquirer did not finish (exitcode={reacquirer.exitcode})"
        assert reacquire_outcome.value == 1
        assert time.monotonic() - probe_started_at < 1.0
        reacquirer.join(5)
        assert not reacquirer.is_alive()
        assert reacquirer.exitcode == 0
        assert guard_path.exists()
    finally:
        if 'reacquirer' in locals() and reacquirer is not None:
            _cleanup_child(reacquirer, reacquire_started)
        _cleanup_child(process, exit_now)


def test_live_owner_remains_busy_after_metadata_ttl(tmp_path):
    path = tmp_path / "input.lock"
    owner, ready, release = _spawn_lease_holder(path, ttl_seconds=0.2)
    contender = None
    contender_started = None
    try:
        assert ready.wait(10), f"lease holder did not start (exitcode={owner.exitcode})"
        record = json.loads(path.read_text(encoding="utf-8"))
        time.sleep(max(0.0, record["expires_at"] - time.time()) + 0.1)

        context = multiprocessing.get_context("spawn")
        contender_started = context.Event()
        contender_done = context.Event()
        contender_outcome = context.Value("i", -1)
        previous_executable = multiprocessing.spawn.get_executable()
        multiprocessing.set_executable(sys._base_executable)
        try:
            contender = context.Process(
                target=_try_lease,
                args=(str(path), contender_started, contender_done, contender_outcome),
            )
            contender.start()
        finally:
            multiprocessing.set_executable(os.fsdecode(previous_executable))
        assert contender_started.wait(10), f"contender did not start (exitcode={contender.exitcode})"
        started_at = time.monotonic()
        assert contender_done.wait(5), f"contender did not finish (exitcode={contender.exitcode})"
        assert contender_outcome.value == 0
        assert time.monotonic() - started_at < 1.0
        assert owner.is_alive()
    finally:
        if contender is not None and contender_started is not None:
            _cleanup_child(contender, contender_started)
        _cleanup_child(owner, release)

    with PhysicalInputLease(path=path, ttl_seconds=10):
        assert path.exists()


def test_abrupt_owner_death_releases_lifetime_lock_and_recovers_record(tmp_path):
    path = tmp_path / "input.lock"
    owner, ready, exit_now = _spawn_lease_holder(path, abrupt=True)
    try:
        assert ready.wait(10), f"lease holder did not start (exitcode={owner.exitcode})"
        assert path.exists()
        exit_now.set()
        owner.join(5)
        assert not owner.is_alive()
        assert owner.exitcode != 0

        with PhysicalInputLease(path=path, ttl_seconds=10, action_summary="recovered") as lease:
            assert json.loads(path.read_text(encoding="utf-8"))["owner_token"] == lease.owner_token
    finally:
        _cleanup_child(owner, exit_now)


def test_lease_record_contains_owner_pid_action_and_deadline(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    monkeypatch.setattr(P.time, "time", lambda: 100.0)

    with PhysicalInputLease(path=path, ttl_seconds=10, action_summary="click button") as lease:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record == {
            "owner_token": lease.owner_token,
            "pid": os.getpid(),
            "action_summary": "click button",
            "created_at": 100.0,
            "expires_at": 110.0,
        }
        assert lease.owner_token

    assert not path.exists()


def test_exit_does_not_remove_a_replacement_owned_by_another_token(tmp_path):
    path = tmp_path / "input.lock"
    replacement = _record(token="replacement", expires_at=10_000_000_000)

    with PhysicalInputLease(path=path, ttl_seconds=10):
        path.unlink()
        path.write_text(json.dumps(replacement), encoding="utf-8")

    assert json.loads(path.read_text(encoding="utf-8")) == replacement


def test_exception_from_body_still_releases_owned_lease(tmp_path):
    path = tmp_path / "input.lock"

    with pytest.raises(RuntimeError, match="boom"):
        with PhysicalInputLease(path=path, ttl_seconds=10):
            raise RuntimeError("boom")

    assert not path.exists()


def test_record_write_failure_does_not_leave_a_lock(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    monkeypatch.setattr(P.os, "write", lambda _fd, _payload: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        with PhysicalInputLease(path=path, ttl_seconds=10):
            pass

    assert not path.exists()


def test_fstat_failure_cleans_empty_lock_and_allows_reacquire(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    real_fstat = os.fstat
    calls = 0

    def fail_lease_fstat(fd):
        nonlocal calls
        calls += 1
        lease_fstat_call = 2 if hasattr(P, "_ArbitrationGuard") else 1
        if calls == lease_fstat_call:
            raise OSError("fstat failed")
        return real_fstat(fd)

    with monkeypatch.context() as patch:
        patch.setattr(P.os, "fstat", fail_lease_fstat)
        with pytest.raises(OSError, match="fstat failed"):
            with PhysicalInputLease(path=path, ttl_seconds=10):
                pass

    assert not path.exists()
    guard_path = path.parent / f"{path.name}.guard"
    assert guard_path.exists()
    assert guard_path.stat().st_size >= 1
    with PhysicalInputLease(path=path, ttl_seconds=10):
        assert path.exists()


def test_unexpired_live_owner_is_not_recovered(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    record = _record(pid=123, expires_at=200)
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(P.time, "time", lambda: 100)
    monkeypatch.setattr(P, "_pid_alive", lambda pid: pid == 123)

    with pytest.raises(PhysicalInputBusy):
        with PhysicalInputLease(path=path, ttl_seconds=10):
            pass

    assert json.loads(path.read_text(encoding="utf-8")) == record


@pytest.mark.parametrize("stale_reason", ["expired", "dead_pid"])
def test_stale_lease_is_recovered_once(monkeypatch, tmp_path, stale_reason):
    path = tmp_path / "input.lock"
    record = _record(pid=123, expires_at=50 if stale_reason == "expired" else 200)
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(P.time, "time", lambda: 100)
    monkeypatch.setattr(P, "_pid_alive", lambda _pid: stale_reason != "dead_pid")

    with PhysicalInputLease(path=path, ttl_seconds=10, action_summary="recovered") as lease:
        recovered = json.loads(path.read_text(encoding="utf-8"))
        assert recovered["owner_token"] == lease.owner_token
        assert recovered["action_summary"] == "recovered"

    assert not path.exists()


def test_malformed_existing_record_is_not_assumed_stale(tmp_path):
    path = tmp_path / "input.lock"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(PhysicalInputBusy):
        with PhysicalInputLease(path=path, ttl_seconds=10):
            pass

    assert path.read_text(encoding="utf-8") == "not-json"


def test_stale_recovery_retries_acquisition_only_once(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    stale = _record(token="stale", pid=123, expires_at=50)
    competitor = _record(token="competitor", pid=os.getpid(), expires_at=200)
    path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(P.time, "time", lambda: 100)
    real_open = os.open
    attempts = 0

    def competing_open(target, flags, mode=0o777):
        nonlocal attempts
        if os.fspath(target) == os.fspath(path):
            attempts += 1
            if attempts == 2:
                path.write_text(json.dumps(competitor), encoding="utf-8")
        return real_open(target, flags, mode)

    monkeypatch.setattr(P.os, "open", competing_open)

    with pytest.raises(PhysicalInputBusy):
        with PhysicalInputLease(path=path, ttl_seconds=10):
            pass

    assert attempts == 2
    assert json.loads(path.read_text(encoding="utf-8")) == competitor


def test_stale_validation_and_replacement_are_arbitrated(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    path.write_text(json.dumps(_record(token="stale", pid=123, expires_at=50)), encoding="utf-8")
    monkeypatch.setattr(P.time, "time", lambda: 100)
    real_read_record = P._read_record
    stale_validated = threading.Event()
    release_stale_owner = threading.Event()
    release_competitor = threading.Event()
    competitor_result_ready = threading.Event()
    stale_result = []
    competitor_result = []
    stale_reads = 0
    lease_a = PhysicalInputLease(path=path, ttl_seconds=10, action_summary="A")
    lease_b = PhysicalInputLease(path=path, ttl_seconds=10, action_summary="B")
    thread_a = None

    def controlled_read_record(target):
        nonlocal stale_reads
        record = real_read_record(target)
        if threading.current_thread() is thread_a and P._record_token(record) == "stale":
            stale_reads += 1
            if stale_reads == 2:
                stale_validated.set()
                assert release_stale_owner.wait(2)
        return record

    monkeypatch.setattr(P, "_read_record", controlled_read_record)

    def acquire_stale_owner():
        try:
            lease_a.__enter__()
            stale_result.append("acquired")
        except PhysicalInputBusy:
            stale_result.append("busy")

    def acquire_competitor():
        try:
            lease_b.__enter__()
            competitor_result.append("acquired")
        except PhysicalInputBusy:
            competitor_result.append("busy")
        finally:
            competitor_result_ready.set()
        if competitor_result == ["acquired"]:
            assert release_competitor.wait(2)

    thread_a = threading.Thread(target=acquire_stale_owner)
    thread_b = threading.Thread(target=acquire_competitor)
    try:
        thread_a.start()
        assert stale_validated.wait(2)
        thread_b.start()
        assert competitor_result_ready.wait(2)
        release_stale_owner.set()
        thread_a.join(2)

        assert stale_result == ["acquired"]
        assert competitor_result == ["busy"]
        assert json.loads(path.read_text(encoding="utf-8"))["owner_token"] == lease_a.owner_token
    finally:
        release_stale_owner.set()
        release_competitor.set()
        thread_a.join(2)
        thread_b.join(2)
        lease_b.__exit__(None, None, None)
        lease_a.__exit__(None, None, None)


def test_old_owner_cleanup_cannot_delete_replacement(monkeypatch, tmp_path):
    path = tmp_path / "input.lock"
    old_lease = PhysicalInputLease(path=path, ttl_seconds=10, action_summary="old")
    old_lease.__enter__()
    old_record = json.loads(path.read_text(encoding="utf-8"))
    old_record["expires_at"] = 0
    path.write_text(json.dumps(old_record), encoding="utf-8")
    real_read_record = P._read_record
    cleanup_validated = threading.Event()
    release_cleanup = threading.Event()
    release_replacement = threading.Event()
    replacement_result_ready = threading.Event()
    replacement_result = []
    replacement = PhysicalInputLease(path=path, ttl_seconds=10, action_summary="replacement")
    cleanup_thread = None

    def controlled_read_record(target):
        record = real_read_record(target)
        if threading.current_thread() is cleanup_thread and P._record_token(record) == old_lease.owner_token:
            cleanup_validated.set()
            assert release_cleanup.wait(2)
        return record

    monkeypatch.setattr(P, "_read_record", controlled_read_record)

    def cleanup_old_owner():
        old_lease.__exit__(None, None, None)

    def acquire_replacement():
        try:
            replacement.__enter__()
            replacement_result.append("acquired")
        except PhysicalInputBusy:
            replacement_result.append("busy")
        finally:
            replacement_result_ready.set()
        if replacement_result == ["acquired"]:
            assert release_replacement.wait(2)

    cleanup_thread = threading.Thread(target=cleanup_old_owner)
    replacement_thread = threading.Thread(target=acquire_replacement)
    try:
        cleanup_thread.start()
        assert cleanup_validated.wait(2)
        replacement_thread.start()
        assert replacement_result_ready.wait(2)
        release_cleanup.set()
        cleanup_thread.join(2)

        assert replacement_result == ["busy"]
        assert not path.exists()
    finally:
        release_cleanup.set()
        release_replacement.set()
        cleanup_thread.join(2)
        replacement_thread.join(2)
        replacement.__exit__(None, None, None)
        old_lease.__exit__(None, None, None)

    with PhysicalInputLease(path=path, ttl_seconds=10):
        assert path.exists()


def test_last_input_marker_posix_uses_pointer_only(monkeypatch):
    monkeypatch.setattr(P.sys, "platform", "linux")
    monkeypatch.setattr(P, "_pointer_position", lambda: (4, 5))

    assert P.last_input_marker() == (None, 4, 5)


def test_last_input_marker_windows_uses_fake_user32(monkeypatch):
    class FakeUser32:
        @staticmethod
        def GetLastInputInfo(info_pointer):
            info_pointer._obj.dwTime = 1234
            return 1

        @staticmethod
        def GetCursorPos(point_pointer):
            point_pointer._obj.x = 45
            point_pointer._obj.y = 67
            return 1

    monkeypatch.setattr(P.sys, "platform", "win32")
    monkeypatch.setattr(P.ctypes, "windll", types.SimpleNamespace(user32=FakeUser32()))

    assert P.last_input_marker() == (1234, 45, 67)


def test_wait_for_quiet_samples_then_waits_750ms_by_default(monkeypatch):
    markers = iter([(10, 1, 1), (10, 1, 1)])
    sleeps = []
    monkeypatch.setattr(P, "last_input_marker", lambda: next(markers))
    monkeypatch.setattr(P.time, "sleep", sleeps.append)

    P.wait_for_quiet()

    assert sleeps == [0.75]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ((10, 1, 1), (11, 1, 1)),
        ((10, 1, 1), (10, 2, 1)),
        ((None, 1, 1), (None, 1, 2)),
    ],
)
def test_wait_for_quiet_rejects_any_available_marker_change(monkeypatch, before, after):
    markers = iter([before, after])
    monkeypatch.setattr(P, "last_input_marker", lambda: next(markers))
    monkeypatch.setattr(P.time, "sleep", lambda _seconds: None)

    with pytest.raises(InputActivityDetected):
        P.wait_for_quiet(quiet_seconds=0)


def test_wait_for_quiet_ignores_unavailable_marker_components(monkeypatch):
    markers = iter([(None, 1, 1), (99, 1, 1)])
    monkeypatch.setattr(P, "last_input_marker", lambda: next(markers))
    monkeypatch.setattr(P.time, "sleep", lambda _seconds: None)

    P.wait_for_quiet(quiet_seconds=0)


def test_action_is_not_called_when_input_changes(monkeypatch, tmp_path):
    markers = iter([(10, 1, 1), (11, 1, 1)])
    monkeypatch.setattr(P, "last_input_marker", lambda: next(markers))
    called = False

    def action():
        nonlocal called
        called = True

    path = tmp_path / "l"
    with pytest.raises(InputActivityDetected):
        P.run_physical_action("click", action, lock_path=path, quiet_seconds=0)

    assert called is False
    assert not path.exists()


def test_run_physical_action_returns_value_and_releases(monkeypatch, tmp_path):
    path = tmp_path / "l"
    monkeypatch.setattr(P, "last_input_marker", lambda: (10, 1, 1))

    result = P.run_physical_action("click", lambda: "done", lock_path=path, quiet_seconds=0)

    assert result == "done"
    assert not path.exists()


def test_run_physical_action_releases_when_action_raises(monkeypatch, tmp_path):
    path = tmp_path / "l"
    monkeypatch.setattr(P, "last_input_marker", lambda: (10, 1, 1))

    def fail():
        raise RuntimeError("action failed")

    with pytest.raises(RuntimeError, match="action failed"):
        P.run_physical_action("click", fail, lock_path=path, quiet_seconds=0)

    assert not path.exists()


class _ApprovalContext:
    def __init__(self, action="accept", approve=True, error=None, events=None):
        self.action = action
        self.approve = approve
        self.error = error
        self.calls = []
        self.events = events if events is not None else []

    async def elicit(self, message, schema):
        self.calls.append((message, schema))
        self.events.append("elicit")
        if self.error is not None:
            raise self.error
        data = schema(approve=self.approve) if self.action == "accept" else None
        return SimpleNamespace(action=self.action, data=data)


class _FakePyAutoGUI:
    def __init__(self):
        self.calls = []

    def moveTo(self, x, y, duration=0.0):
        self.calls.append(("moveTo", x, y, duration))

    def click(self, *args, **kwargs):
        self.calls.append(("click", args, kwargs))

    def dragTo(self, x, y, duration=0.3, button="left"):
        self.calls.append(("dragTo", x, y, duration, button))

    def write(self, text, interval=0.01):
        self.calls.append(("write", text, interval))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))


_PHYSICAL_TOOL_CASES = [
    ("mouse_move", {"x": 1, "y": 2}, "moveTo"),
    ("mouse_click", {"x": 1, "y": 2, "session_id": "client:7"}, "click"),
    ("mouse_drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4}, "dragTo"),
    ("type_text", {"text": "hello", "session_id": "client:7"}, "write"),
    ("hotkey", {"keys_csv": "ctrl,l"}, "hotkey"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name, kwargs, gui_call", _PHYSICAL_TOOL_CASES)
async def test_accepted_physical_tool_runs_exactly_once(monkeypatch, tool_name, kwargs, gui_call):
    gui = _FakePyAutoGUI()
    ctx = _ApprovalContext()
    physical_calls = []
    activation_calls = []

    monkeypatch.setattr(S, "_pyautogui", lambda: gui)
    monkeypatch.setattr(
        S,
        "_maybe_activate",
        lambda mode, sid=None: activation_calls.append((mode, sid)) or {"on_screen": True},
    )

    def run(summary, action):
        physical_calls.append(summary)
        return action()

    monkeypatch.setattr(S.physical_input, "run_physical_action", run)

    result = await getattr(S, tool_name)(ctx=ctx, **kwargs)

    assert result["status"] == "ok"
    assert len(ctx.calls) == 1
    assert len(physical_calls) == 1
    assert sum(1 for call in gui.calls if call[0] == gui_call) == 1
    if tool_name in {"mouse_click", "type_text"}:
        assert activation_calls == [("current", "client:7")]
    else:
        assert activation_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name, kwargs, _", _PHYSICAL_TOOL_CASES)
@pytest.mark.parametrize("response", [("decline", False), ("cancel", False), ("accept", False)])
async def test_physical_tool_requires_approval_before_any_work(
    monkeypatch, tool_name, kwargs, _, response
):
    action, approve = response
    ctx = _ApprovalContext(action=action, approve=approve)
    events = []

    monkeypatch.setattr(S, "_pyautogui", lambda: pytest.fail("must not import pyautogui"))
    monkeypatch.setattr(S, "_maybe_activate", lambda *args: pytest.fail("must not activate"))
    monkeypatch.setattr(S.physical_input, "run_physical_action", lambda *args: pytest.fail("must not acquire lease"))

    async def fail_worker(*args, **kwargs):
        events.append("worker")
        pytest.fail("must not offload without approval")

    monkeypatch.setattr(S.anyio.to_thread, "run_sync", fail_worker)
    result = await getattr(S, tool_name)(ctx=ctx, **kwargs)

    assert result["status"] == "requires_user_action"
    assert events == []


@pytest.mark.anyio
@pytest.mark.parametrize("action,error", [("decline", None), ("cancel", None), ("accept", RuntimeError("unsupported"))])
async def test_elicitation_decline_cancel_and_failure_are_structured(monkeypatch, action, error):
    ctx = _ApprovalContext(action=action, approve=True, error=error)
    monkeypatch.setattr(S, "_pyautogui", lambda: pytest.fail("must not import pyautogui"))
    monkeypatch.setattr(S.physical_input, "run_physical_action", lambda *args: pytest.fail("must not acquire lease"))

    result = await S.mouse_click(ctx=ctx, x=1, y=2)

    assert result["status"] == "requires_user_action"


@pytest.mark.anyio
async def test_approval_requires_a_boolean_true(monkeypatch):
    class NonBooleanApproval:
        async def elicit(self, message, schema):
            return SimpleNamespace(action="accept", data=SimpleNamespace(approve=1))

    monkeypatch.setattr(S, "_pyautogui", lambda: pytest.fail("must not import pyautogui"))
    monkeypatch.setattr(S.physical_input, "run_physical_action", lambda *args: pytest.fail("must not acquire lease"))

    result = await S.mouse_click(ctx=NonBooleanApproval(), x=1, y=2)

    assert result["status"] == "requires_user_action"


@pytest.mark.parametrize("value", [1, "true", "yes"])
def test_approval_schema_rejects_coerced_truthy_values(value):
    with pytest.raises(ValidationError):
        S.PhysicalInputApproval(approve=value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error,status",
    [
        (P.PhysicalInputBusy("busy"), "busy"),
        (P.InputActivityDetected("changed"), "input_activity_detected"),
    ],
)
async def test_physical_gate_errors_are_structured(monkeypatch, error, status):
    ctx = _ApprovalContext()
    monkeypatch.setattr(S, "_maybe_activate", lambda *args: None)
    monkeypatch.setattr(S, "_pyautogui", lambda: pytest.fail("must not import after gate failure"))
    monkeypatch.setattr(S.physical_input, "run_physical_action", lambda *args: (_ for _ in ()).throw(error))

    result = await S.mouse_click(ctx=ctx, x=1, y=2)

    assert result["status"] == status


@pytest.mark.anyio
@pytest.mark.parametrize(
    "activation",
    [
        {"on_screen": False, "window_state": "minimized"},
        {"on_screen": None, "note": "reload extension"},
        {"activation_skipped": "dead session"},
    ],
)
async def test_unconfirmed_target_never_receives_physical_input(monkeypatch, activation):
    ctx = _ApprovalContext()

    monkeypatch.setattr(S, "_maybe_activate", lambda *args: activation)
    monkeypatch.setattr(S, "_pyautogui", lambda: pytest.fail("must not import pyautogui"))

    def run_physical_action(summary, action):
        return action()

    monkeypatch.setattr(S.physical_input, "run_physical_action", run_physical_action)

    result = await S.mouse_click(ctx=ctx, x=1, y=2, session_id="client:7")

    assert result["status"] == "activation_failed"
    assert result["activated"] == activation


@pytest.mark.anyio
@pytest.mark.parametrize("error", [P.PhysicalInputBusy("late busy"), P.InputActivityDetected("late activity")])
async def test_physical_gate_errors_after_action_started_are_not_hidden(monkeypatch, error):
    ctx = _ApprovalContext()
    monkeypatch.setattr(S, "_maybe_activate", lambda *args: {"on_screen": True})

    def run_physical_action(summary, action):
        return action()

    monkeypatch.setattr(S.physical_input, "run_physical_action", run_physical_action)
    monkeypatch.setattr(S, "_pyautogui", lambda: (_ for _ in ()).throw(error))

    with pytest.raises(type(error), match=str(error)):
        await S.mouse_click(ctx=ctx, x=1, y=2)


@pytest.mark.anyio
async def test_physical_action_is_offloaded_and_activation_follows_quiet_gate(monkeypatch):
    events = []
    ctx = _ApprovalContext(events=events)
    gui = _FakePyAutoGUI()

    class RecordingLock:
        def __enter__(self):
            events.append("tool_lock_enter")

        def __exit__(self, *args):
            events.append("tool_lock_exit")

    async def run_sync(worker, *args, **kwargs):
        events.append("worker")
        return worker()

    def run_physical_action(summary, action):
        events.append("lease_and_quiet_passed")
        return action()

    monkeypatch.setattr(S, "_TOOL_LOCK", RecordingLock())
    monkeypatch.setattr(S.anyio.to_thread, "run_sync", run_sync)
    monkeypatch.setattr(S.physical_input, "run_physical_action", run_physical_action)
    monkeypatch.setattr(
        S,
        "_maybe_activate",
        lambda mode, sid=None: events.append("activate") or {"on_screen": True},
    )
    monkeypatch.setattr(S, "_pyautogui", lambda: events.append("import_pyautogui") or gui)

    result = await S.mouse_click(ctx=ctx, x=1, y=2, session_id="client:7")

    assert result["status"] == "ok"
    assert events == [
        "elicit",
        "worker",
        "tool_lock_enter",
        "lease_and_quiet_passed",
        "activate",
        "import_pyautogui",
        "tool_lock_exit",
    ]
    assert len(gui.calls) == 1

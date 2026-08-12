from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import threading

import pytest

from agent_browser_mcp import server as S


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "src" / "agent_browser_mcp" / "chrome_extension" / "background.js"
MANIFEST = ROOT / "src" / "agent_browser_mcp" / "chrome_extension" / "manifest.json"


class _Driver:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.default_session_id = None

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        self.calls.append((payload, client_id, timeout))
        return self.response


def test_manifest_requests_downloads_permission():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "downloads" in manifest["permissions"]


def test_download_file_default_refuses_to_overwrite_requested_destination(
    monkeypatch, tmp_path
):
    source = tmp_path / "browser-download.tmp"
    source.write_bytes(b"new payload")
    destination_dir = tmp_path / "arbitrary" / "nested"
    destination_dir.mkdir(parents=True)
    destination = destination_dir / "report.bin"
    destination.write_bytes(b"old payload")
    driver = _Driver(
        {
            "data": {
                "ok": True,
                "data": {
                    "status": "completed",
                    "download_id": 17,
                    "path": str(source),
                    "bytes_received": len(b"new payload"),
                    "total_bytes": len(b"new payload"),
                },
            }
        }
    )
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda timeout=None, fresh=False: [{"id": "chrome:profile:42"}],
    )

    with pytest.raises(FileExistsError, match="overwrite=true"):
        S.download_file(
            "https://example.test/private/report",
            filename="report.bin",
            directory=str(destination_dir),
            timeout=12,
            session_id="chrome:profile:42",
        )

    assert destination.read_bytes() == b"old payload"
    assert source.read_bytes() == b"new payload"
    assert driver.calls == [
        (
            {
                "cmd": "downloads",
                "method": "download",
                "url": "https://example.test/private/report",
                "filename": "report.bin",
                "conflictAction": "uniquify",
                "wait": True,
                "timeoutMs": 12000,
            },
            "chrome:profile",
            13.0,
        )
    ]


def test_download_file_explicit_overwrite_replaces_requested_destination(
    monkeypatch, tmp_path
):
    source = tmp_path / "browser-download.tmp"
    source.write_bytes(b"new payload")
    destination_dir = tmp_path / "destination"
    destination_dir.mkdir()
    destination = destination_dir / "report.bin"
    destination.write_bytes(b"old payload")
    driver = _Driver({
        "data": {"ok": True, "data": {
            "status": "completed", "download_id": 17, "path": str(source),
        }}
    })
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S, "active_sessions",
        lambda timeout=None, fresh=False: [{"id": "chrome:profile:42"}],
    )

    result = S.download_file(
        "https://example.test/private/report",
        filename="report.bin",
        directory=str(destination_dir),
        overwrite=True,
        session_id="chrome:profile:42",
    )

    assert result["path"] == str(destination.resolve())
    assert destination.read_bytes() == b"new payload"
    assert not source.exists()


def test_download_file_default_destination_is_invisible_until_copy_completes(
    monkeypatch, tmp_path
):
    source = tmp_path / "browser-download.tmp"
    source.write_bytes(b"complete payload")
    destination = tmp_path / "destination" / "report.bin"
    copy_started = threading.Event()
    release_copy = threading.Event()
    real_copyfileobj = S.shutil.copyfileobj
    errors = []

    def blocking_copy(source_file, destination_file, length=0):
        first_chunk = source_file.read(7)
        destination_file.write(first_chunk)
        destination_file.flush()
        copy_started.set()
        assert release_copy.wait(timeout=5)
        return real_copyfileobj(source_file, destination_file, length)

    monkeypatch.setattr(S.shutil, "copyfileobj", blocking_copy)

    def move_download():
        try:
            S._move_download(source, destination, overwrite=False)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=move_download)
    worker.start()
    assert copy_started.wait(timeout=5)
    assert not destination.exists()
    release_copy.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert destination.read_bytes() == b"complete payload"
    assert not source.exists()
    assert list(destination.parent.glob(f".{destination.name}.*.download")) == []


def test_download_file_default_copy_failure_is_not_published(monkeypatch, tmp_path):
    source = tmp_path / "browser-download.tmp"
    source.write_bytes(b"complete payload")
    destination = tmp_path / "destination" / "report.bin"

    def failing_copy(source_file, destination_file, length=0):
        destination_file.write(b"partial")
        raise OSError("copy failed")

    monkeypatch.setattr(S.shutil, "copyfileobj", failing_copy)

    with pytest.raises(OSError, match="copy failed"):
        S._move_download(source, destination, overwrite=False)

    assert not destination.exists()
    assert source.read_bytes() == b"complete payload"
    assert list(destination.parent.glob(f".{destination.name}.*.download")) == []


def test_download_file_default_atomically_refuses_racing_destination(
    monkeypatch, tmp_path
):
    source = tmp_path / "browser-download.tmp"
    source.write_bytes(b"new payload")
    destination_dir = tmp_path / "destination"
    destination_dir.mkdir()
    destination = destination_dir / "report.bin"
    real_os_link = S.os.link

    def destination_appears_before_publish(source_path, destination_path):
        destination.write_bytes(b"racing payload")
        return real_os_link(source_path, destination_path)

    monkeypatch.setattr(S.os, "link", destination_appears_before_publish)

    with pytest.raises(FileExistsError, match="overwrite=true"):
        S._move_download(source, destination, overwrite=False)

    assert destination.read_bytes() == b"racing payload"
    assert source.read_bytes() == b"new payload"
    assert list(destination.parent.glob(f".{destination.name}.*.download")) == []


def test_download_file_directory_timeout_reports_destination_not_applied(
    monkeypatch, tmp_path
):
    driver = _Driver(
        {
            "data": {
                "ok": True,
                "data": {
                    "status": "in_progress",
                    "download_id": 18,
                    "bytes_received": 5,
                    "total_bytes": 100,
                },
            }
        }
    )
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    result = S.download_file(
        "https://example.test/large.bin",
        directory=str(tmp_path),
        timeout=2,
    )

    assert result["status"] == "in_progress"
    assert result["directory_applied"] is False
    assert result["requested_directory"] == str(tmp_path.resolve())
    assert "default download" in result["hint"].lower()
    assert "not" in result["hint"].lower()
    assert driver.calls[0][0]["conflictAction"] == "uniquify"


def test_download_file_preserves_in_progress_without_touching_disk(monkeypatch):
    driver = _Driver(
        {
            "data": {
                "ok": True,
                "data": {
                    "status": "in_progress",
                    "download_id": 18,
                    "bytes_received": 5,
                    "total_bytes": 100,
                },
            }
        }
    )
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    result = S.download_file(
        "https://example.test/large.bin", wait=False, timeout=2
    )

    assert result["type"] == "download"
    assert result["status"] == "in_progress"
    assert result["download_id"] == 18
    assert "path" not in result
    assert driver.calls[0][0]["wait"] is False
    assert driver.calls[0][0]["conflictAction"] == "uniquify"


def test_download_file_requires_explicit_session_to_be_live(monkeypatch):
    driver = _Driver({"data": {"ok": True}})
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda timeout=None, fresh=False: [{"id": "edge:profile:7"}],
    )

    with pytest.raises(RuntimeError, match="chrome:profile:42.*not found"):
        S.download_file(
            "https://example.test/private/report",
            session_id="chrome:profile:42",
        )

    assert driver.calls == []


def test_download_file_explicit_session_check_bypasses_stale_cache(monkeypatch):
    driver = _Driver({"data": {"ok": True}})
    fresh_flags = []

    def sessions(timeout=None, fresh=False):
        fresh_flags.append(fresh)
        return [] if fresh else [{"id": "chrome:profile:42"}]

    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "active_sessions", sessions)

    with pytest.raises(RuntimeError, match="chrome:profile:42.*not found"):
        S.download_file(
            "https://example.test/private/report",
            session_id="chrome:profile:42",
        )

    assert fresh_flags == [True]
    assert driver.calls == []


def test_download_file_rejects_malformed_explicit_session(monkeypatch):
    driver = _Driver({"data": {"ok": True}})
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    with pytest.raises(ValueError, match="invalid composite session id"):
        S.download_file(
            "https://example.test/private/report",
            session_id="chrome",
        )

    assert driver.calls == []


def test_registered_download_tool_only_locks_while_pinning_implicit_browser(monkeypatch):
    class RecordingLock:
        held = False
        entries = 0

        def __enter__(self):
            self.held = True
            self.entries += 1
            return self

        def __exit__(self, *args):
            self.held = False
            return False

    lock = RecordingLock()

    class Driver(_Driver):
        def ext_cmd(self, payload, client_id=None, timeout=15.0):
            assert lock.held is False
            return super().ext_cmd(payload, client_id=client_id, timeout=timeout)

    driver = Driver({
        "data": {
            "ok": True,
            "data": {"status": "in_progress", "download_id": 21},
        }
    })
    driver.default_session_id = "chrome:personal:42"
    monkeypatch.setattr(S, "_TOOL_LOCK", lock)
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    registered = S.mcp._tool_manager.get_tool("download_file").fn

    result = asyncio.run(
        registered("https://example.test/large.bin", wait=False, timeout=2)
    )

    assert result["status"] == "in_progress"
    assert lock.entries == 1
    assert driver.calls[0][1] == "chrome:personal"


def test_download_file_preserves_structured_extension_failure(monkeypatch):
    driver = _Driver(
        {
            "data": {
                "ok": True,
                "data": {
                    "status": "failed",
                    "download_id": 19,
                    "code": "download_failed",
                    "error": "NETWORK_FAILED",
                },
            }
        }
    )
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    result = S.download_file("https://example.test/broken.bin")

    assert result == {
        "type": "download",
        "status": "failed",
        "download_id": 19,
        "code": "download_failed",
        "error": "NETWORK_FAILED",
    }


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"url": "file:///tmp/a"}, "http"),
        ({"url": "https://example.test/a", "filename": "../escape.bin"}, "filename"),
        ({"url": "https://example.test/a", "filename": "\\escape.bin"}, "filename"),
        ({"url": "https://example.test/a", "filename": "C:escape.bin"}, "filename"),
        ({"url": "https://example.test/a", "filename": "."}, "filename"),
        ({"url": "https://example.test/a", "directory": "relative/path"}, "absolute"),
        (
            {
                "url": "https://example.test/a",
                "directory": "C:/downloads",
                "wait": False,
            },
            "wait=true",
        ),
        ({"url": "https://example.test/a", "timeout": 0}, "timeout"),
    ],
)
def test_download_file_rejects_invalid_inputs_before_bridge(monkeypatch, kwargs, match):
    driver = _Driver({"data": {"ok": True}})
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    with pytest.raises(ValueError, match=match):
        S.download_file(**kwargs)
    assert driver.calls == []


def test_open_url_classifies_extension_download_navigation(monkeypatch):
    driver = _Driver(
        {
            "data": {
                "status": "navigation_failed",
                "url": "https://example.test/current",
                "navigation": {
                    "frameId": "f1",
                    "isDownload": True,
                    "errorText": "net::ERR_ABORTED",
                },
                "navigation_error": "net::ERR_ABORTED",
            }
        }
    )
    driver.default_session_id = "chrome:profile:42"
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(
        S,
        "active_sessions",
        lambda timeout=None, fresh=False: [
            {"id": "chrome:profile:42", "url": "https://example.test/current"}
        ],
    )
    monkeypatch.setattr(S, "invalidate_sessions_cache", lambda: None)

    result = S.open_url(
        "https://example.test/attachment", session_id="chrome:profile:42"
    )

    assert result["type"] == "download"
    assert result["status"] == "triggered"
    assert result["is_download"] is True
    assert "ERR_ABORTED" in result["hint"]


def test_open_url_does_not_infer_download_from_err_aborted_alone():
    result = S._classify_navigation_result(
        {
            "status": "navigation_failed",
            "navigation_error": "net::ERR_ABORTED",
            "navigation": {"errorText": "net::ERR_ABORTED"},
        },
        requested_url="https://example.test/attachment",
    )
    assert result["status"] == "navigation_failed"
    assert result.get("type") != "download"


def test_extension_download_harness_reports_completed_interrupted_and_timeout():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function boundedDownloadTimeout")
    end = source.index("\n\nasync function handleExtMessage", start)
    helpers = source[start:end]
    script = f"""
const listeners = new Set();
const downloadOptions = [];
let nextId = 20;
let item = null;
const chrome = {{
  downloads: {{
    onChanged: {{
      addListener(fn) {{ listeners.add(fn); }},
      removeListener(fn) {{ listeners.delete(fn); }},
    }},
    async download(options) {{
      downloadOptions.push(options);
      const id = nextId++;
      item = {{ id, state: 'in_progress', filename: 'C:/Downloads/file.bin',
        bytesReceived: 0, totalBytes: 11, url: options.url }};
      return id;
    }},
    async search(query) {{ return item && item.id === query.id ? [item] : []; }},
  }},
}};
{helpers}
async function emit(state, error = null) {{
  item = {{ ...item, state, error, bytesReceived: state === 'complete' ? 11 : 3 }};
  for (const listener of [...listeners]) listener({{
    id: item.id, state: {{ current: state }},
    error: error ? {{ current: error }} : undefined,
  }});
  await new Promise(resolve => setTimeout(resolve, 0));
}}
(async () => {{
  const completedPromise = handleDownloadCommand({{
    method: 'download', url: 'https://example.test/file', wait: true, timeoutMs: 100,
    conflictAction: 'uniquify',
  }});
  await new Promise(resolve => setTimeout(resolve, 0));
  await emit('complete');
  const completed = await completedPromise;

  const interruptedPromise = handleDownloadCommand({{
    method: 'download', url: 'https://example.test/broken', wait: true, timeoutMs: 100,
  }});
  await new Promise(resolve => setTimeout(resolve, 0));
  await emit('interrupted', 'NETWORK_FAILED');
  const interrupted = await interruptedPromise;

  const timeout = await handleDownloadCommand({{
    method: 'download', url: 'https://example.test/slow', wait: true, timeoutMs: 5,
  }});
  process.stdout.write(JSON.stringify({{ completed, interrupted, timeout,
    downloadOptions, listenerCount: listeners.size }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["completed"]["data"]["status"] == "completed"
    assert outcome["completed"]["data"]["path"] == "C:/Downloads/file.bin"
    assert outcome["interrupted"]["data"]["status"] == "failed"
    assert outcome["interrupted"]["data"]["error"] == "NETWORK_FAILED"
    assert outcome["timeout"]["data"]["status"] == "in_progress"
    assert outcome["downloadOptions"][0]["conflictAction"] == "uniquify"
    assert outcome["downloadOptions"][1]["conflictAction"] == "overwrite"
    assert outcome["listenerCount"] == 0


def test_extension_download_terminal_event_wins_over_stale_search_and_start_failure_is_structured():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function boundedDownloadTimeout")
    end = source.index("\n\nasync function handleExtMessage", start)
    helpers = source[start:end]
    script = f"""
const listeners = new Set();
let failStart = false;
let hiddenFilenameSearches = 0;
const item = {{ id: 31, state: 'in_progress', filename: 'C:/Downloads/file.bin',
  bytesReceived: 7, totalBytes: 7 }};
const chrome = {{
  downloads: {{
    onChanged: {{
      addListener(fn) {{ listeners.add(fn); }},
      removeListener(fn) {{ listeners.delete(fn); }},
    }},
    async download() {{
      if (failStart) throw new Error('NETWORK_FAILED');
      return 31;
    }},
    async search() {{
      if (hiddenFilenameSearches > 0) {{
        hiddenFilenameSearches -= 1;
        return [{{ id: 31, state: 'in_progress', bytesReceived: 7, totalBytes: 7 }}];
      }}
      return [item];
    }},
  }},
}};
{helpers}
(async () => {{
  const completedPromise = handleDownloadCommand({{
    method: 'download', url: 'https://example.test/file', wait: true, timeoutMs: 100,
  }});
  await new Promise(resolve => setTimeout(resolve, 0));
  hiddenFilenameSearches = 2;
  for (const listener of [...listeners]) listener({{
    id: 31, state: {{ current: 'complete' }},
  }});
  const completed = await completedPromise;

  const interruptedPromise = handleDownloadCommand({{
    method: 'download', url: 'https://example.test/interrupted', wait: true, timeoutMs: 100,
  }});
  await new Promise(resolve => setTimeout(resolve, 0));
  for (const listener of [...listeners]) listener({{
    id: 31,
    state: {{ current: 'interrupted' }},
    error: {{ current: 'NETWORK_FAILED' }},
  }});
  const interrupted = await interruptedPromise;

  failStart = true;
  const failed = await handleDownloadCommand({{
    method: 'download', url: 'https://example.test/broken', wait: true, timeoutMs: 100,
  }});
  process.stdout.write(JSON.stringify({{ completed, interrupted, failed,
    listenerCount: listeners.size }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["completed"]["data"]["status"] == "completed"
    assert outcome["completed"]["data"]["path"] == "C:/Downloads/file.bin"
    assert outcome["interrupted"]["data"]["status"] == "failed"
    assert outcome["interrupted"]["data"]["error"] == "NETWORK_FAILED"
    assert outcome["failed"]["ok"] is True
    assert outcome["failed"]["data"] == {
        "status": "failed",
        "code": "download_failed",
        "error": "NETWORK_FAILED",
    }
    assert outcome["listenerCount"] == 0


def test_extension_download_complete_without_final_path_is_structured_failure():
    source = BACKGROUND.read_text(encoding="utf-8")
    start = source.index("function boundedDownloadTimeout")
    end = source.index("\n\nasync function handleExtMessage", start)
    helpers = source[start:end]
    script = f"""
const listeners = new Set();
let searches = 0;
const chrome = {{
  downloads: {{
    onChanged: {{
      addListener(fn) {{ listeners.add(fn); }},
      removeListener(fn) {{ listeners.delete(fn); }},
    }},
    async download() {{ return 41; }},
    async search() {{
      searches += 1;
      return [{{ id: 41, state: 'in_progress', bytesReceived: 7, totalBytes: 7 }}];
    }},
  }},
}};
{helpers}
(async () => {{
  const pending = handleDownloadCommand({{
    method: 'download', url: 'https://example.test/file', wait: true, timeoutMs: 1000,
  }});
  await new Promise(resolve => setTimeout(resolve, 0));
  for (const listener of [...listeners]) listener({{
    id: 41, state: {{ current: 'complete' }},
  }});
  const result = await pending;
  process.stdout.write(JSON.stringify({{ result, searches, listenerCount: listeners.size }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(
        ["node", "-"], input=script, text=True, capture_output=True, timeout=5
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["result"]["data"]["status"] == "failed"
    assert outcome["result"]["data"]["code"] == "download_status_failed"
    assert "final local path" in outcome["result"]["data"]["error"]
    assert outcome["searches"] >= 5
    assert outcome["listenerCount"] == 0

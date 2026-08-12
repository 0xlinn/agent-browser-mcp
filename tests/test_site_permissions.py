"""Offline contracts for temporary origin-scoped site permissions."""
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_browser_mcp import server as S


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "src" / "agent_browser_mcp" / "chrome_extension" / "background.js"
MANIFEST = ROOT / "src" / "agent_browser_mcp" / "chrome_extension" / "manifest.json"


class _ElicitationContext:
    def __init__(self, action="accept", approve=True, error=None):
        self.action = action
        self.approve = approve
        self.error = error
        self.calls = []

    async def elicit(self, message, schema):
        self.calls.append((message, schema))
        if self.error:
            raise self.error
        return SimpleNamespace(action=self.action, data=SimpleNamespace(approve=self.approve))


def test_manifest_declares_content_settings_permission():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "contentSettings" in manifest["permissions"]


def test_extension_has_no_self_reload_path():
    source = BACKGROUND.read_text(encoding="utf-8")
    assert "chrome.runtime.reload()" not in source
    assert "tmwd-self-reload" not in source


def test_site_permission_tools_and_schemas_are_registered():
    by_name = {tool.name: tool for tool in asyncio.run(S.mcp.list_tools())}
    assert {"set_site_permission", "reset_site_permissions"} <= set(by_name)
    assert {"permission", "setting", "origin", "duration_seconds", "session_id"} <= set(
        by_name["set_site_permission"].inputSchema["properties"]
    )
    assert {"origin", "permission", "session_id"} <= set(
        by_name["reset_site_permissions"].inputSchema["properties"]
    )


@pytest.mark.parametrize("origin", ["file:///tmp/a", "chrome://settings", "data:text/plain,x", "ftp://example.test"])
def test_only_http_and_https_origins_are_accepted(origin):
    with pytest.raises(ValueError, match="http or https"):
        S._normalize_site_permission_origin(origin)


def test_origin_is_reduced_to_its_normalized_origin():
    assert S._normalize_site_permission_origin("HTTPS://Example.TEST:443/path?q=1#part") == "https://example.test"
    assert S._normalize_site_permission_origin("http://Example.TEST:8080/a/b") == "http://example.test:8080"


@pytest.mark.parametrize("duration", [59, 601, True, "300"])
def test_duration_is_fail_closed(duration):
    with pytest.raises(ValueError, match="60 and 600"):
        S._validate_site_permission_duration(duration)


def test_permission_names_use_fixed_mapping():
    assert S._site_permission_spec("geolocation") == {"kind": "content", "setting": "location"}
    assert S._site_permission_spec("location") == {"kind": "content", "setting": "location"}
    with pytest.raises(ValueError, match="unsupported permission"):
        S._site_permission_spec("midi")


def test_setting_is_limited_to_browser_permission_states():
    assert S._validate_site_permission_setting("ask") == "ask"
    with pytest.raises(ValueError, match="allow, block, ask"):
        S._validate_site_permission_setting("session_only")


@pytest.mark.anyio
@pytest.mark.parametrize("action,error", [("decline", None), ("cancel", None), ("accept", RuntimeError("unsupported"))])
async def test_declined_or_unsupported_elicitation_never_sends_allow(monkeypatch, action, error):
    ctx = _ElicitationContext(action=action, error=error)
    calls = []

    def ext_cmd(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("extension must not receive an unapproved allow")

    monkeypatch.setattr(S, "switch_session", lambda session_id=None: "chrome:7")
    monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace(ext_cmd=ext_cmd, default_session_id="chrome:7"))

    result = await S.set_site_permission(ctx, "camera", "allow", "https://example.test/path", 300, "chrome:7")

    assert result["status"] == "requires_user_action"
    assert calls == []


@pytest.mark.anyio
async def test_allow_forwards_only_a_normalized_origin_after_approval(monkeypatch):
    ctx = _ElicitationContext()
    calls = []

    def ext_cmd(payload, **kwargs):
        calls.append((payload, kwargs))
        return {"data": {"ok": True, "lease_id": "lease-1"}}

    monkeypatch.setattr(S, "switch_session", lambda session_id=None: "chrome:7")
    monkeypatch.setattr(S, "require_driver", lambda: SimpleNamespace(ext_cmd=ext_cmd, default_session_id="chrome:7"))

    result = await S.set_site_permission(ctx, "geolocation", "allow", "https://EXAMPLE.test:443/path", 300, "chrome:7")

    assert result["status"] == "ok"
    assert calls == [(
        {"cmd": "site_permission", "action": "set", "tabId": 7, "permission": "location",
         "setting": "allow", "origin": "https://example.test", "durationSeconds": 300},
        {"client_id": "chrome", "timeout": 20.0},
    )]


@pytest.mark.anyio
async def test_server_preserves_permission_recovery_details(monkeypatch):
    ctx = _ElicitationContext()

    def ext_cmd(_payload, **_kwargs):
        return {"data": {
            "ok": False,
            "error": "storage unavailable",
            "recovery_pending": True,
            "recovery_error": "rollback record could not be saved",
            "lease_id": "lease-rollback",
            "origin": "https://example.test",
            "permission": "camera",
        }}

    driver = SimpleNamespace(ext_cmd=ext_cmd, default_session_id="chrome:7")
    monkeypatch.setattr(S, "switch_session", lambda session_id=None: "chrome:7")
    monkeypatch.setattr(S, "require_driver", lambda: driver)

    result = await S.set_site_permission(
        ctx, "camera", "allow", "https://example.test", 300, "chrome:7"
    )

    assert result["status"] == "error"
    assert result["message"] == "storage unavailable"
    assert result["recovery_pending"] is True
    assert result["recovery_error"] == "rollback record could not be saved"
    assert result["lease_id"] == "lease-rollback"
    assert result["origin"] == "https://example.test"
    assert result["permission"] == "camera"


def test_extension_permission_leases_are_executable_with_mocks():
    """A lease records/get-restores its prior setting and removes it only after success."""
    code = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('const PERMISSION_LEASES_KEY');
const end = source.indexOf('async function handleExtMessage', start);
if (start < 0 || end < 0) throw new Error('permission lease helpers not found');
const saved = {{}};
const alarms = [];
const activeAlarms = new Map();
const operations = [];
const events = [];
const debuggerCommands = [];
let failNextStageStorage = false;
let failNextContentSetting = '';
let effectiveCameraSetting = 'ask';
const chrome = {{
  storage: {{ local: {{
    async get(key) {{ return {{ [key]: saved[key] }}; }},
    async set(value) {{
      events.push(['storage-set', (value.tmwdPermissionLeases || []).length]);
      if (failNextStageStorage && (value.tmwdPermissionLeases || []).length > 0) {{
        failNextStageStorage = false;
        throw new Error('storage unavailable');
      }}
      Object.assign(saved, value);
    }},
  }} }},
  alarms: {{
    create(name, details) {{
      alarms.push({{ name, details }});
      activeAlarms.set(name, details);
      events.push(['alarm-create', name]);
    }},
    clear(name) {{ activeAlarms.delete(name); return Promise.resolve(true); }},
  }},
  contentSettings: {{
    camera: {{
      async get(details) {{
        operations.push(['get', details.primaryUrl]);
        return {{ setting: effectiveCameraSetting }};
      }},
      async set(details) {{
        operations.push(['set', details.primaryPattern, details.setting]);
        events.push(['content-set', details.setting]);
        if (failNextContentSetting === details.setting) {{
          failNextContentSetting = '';
          throw new Error('content setting unavailable');
        }}
        effectiveCameraSetting = details.setting;
      }},
    }},
  }},
  debugger: {{
    onEvent: {{ addListener() {{}} }},
    onDetach: {{ addListener() {{}} }},
    attach() {{ return Promise.resolve(); }},
    detach() {{ return Promise.resolve(); }},
    sendCommand(_target, method) {{
      debuggerCommands.push(method);
      return method === 'Browser.setPermission'
        ? Promise.reject(new Error('Browser domain unavailable'))
        : Promise.resolve();
    }},
  }},
  tabs: {{ onRemoved: {{ addListener() {{}} }} }},
}};
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const dialogAttachedTabs = new Set();
eval(source.slice(start, end));
(async () => {{
  const set = await setSitePermission({{ permission: 'camera', setting: 'allow', origin: 'https://example.test', durationSeconds: 60 }});
  const leasesAfterSet = saved.tmwdPermissionLeases;
  const priorLease = JSON.parse(JSON.stringify(leasesAfterSet[0]));
  const priorAlarm = JSON.parse(JSON.stringify(activeAlarms.get(priorLease.alarmName)));
  failNextContentSetting = 'block';
  const replacementFailure = await setSitePermission({{ permission: 'camera', setting: 'block', origin: 'https://example.test', durationSeconds: 120 }});
  const leaseAfterReplacementFailure = JSON.parse(JSON.stringify(saved.tmwdPermissionLeases[0]));
  const alarmAfterReplacementFailure = JSON.parse(JSON.stringify(activeAlarms.get(priorLease.alarmName)));
  const effectiveAfterReplacementFailure = effectiveCameraSetting;
  const reset = await resetSitePermissionLeases({{ origin: 'https://example.test', permission: 'camera' }});
  failNextStageStorage = true;
  const storageFailure = await setSitePermission({{ permission: 'camera', setting: 'allow', origin: 'https://storage-fail.test', durationSeconds: 60 }});
  const clipboard = await setSitePermission({{ permission: 'clipboard', setting: 'allow', origin: 'https://example.test', durationSeconds: 60, tabId: 7 }});
  console.log(JSON.stringify({{
    set, reset, storageFailure, clipboard, replacementFailure, operations, events,
    debuggerCommands, alarms, leasesAfterSet, priorLease, priorAlarm,
    leaseAfterReplacementFailure, alarmAfterReplacementFailure,
    effectiveAfterReplacementFailure, finalLeases: saved.tmwdPermissionLeases,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(["node", "-e", code], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["set"]["ok"] is True
    assert result["leasesAfterSet"][0]["previousSetting"] == "ask"
    assert result["alarms"][0]["name"].startswith("tmwd-permission:")
    assert result["replacementFailure"] == {
        "ok": False,
        "error": "content setting unavailable",
        "lease_id": result["priorLease"]["id"],
        "origin": "https://example.test",
        "permission": "camera",
        "recovery_pending": False,
    }
    assert result["effectiveAfterReplacementFailure"] == "allow"
    assert result["leaseAfterReplacementFailure"] == result["priorLease"]
    assert result["alarmAfterReplacementFailure"] == result["priorAlarm"]
    first_mutation = result["events"].index(["content-set", "allow"])
    assert result["events"][first_mutation - 2:first_mutation + 1] == [
        ["storage-set", 1],
        ["alarm-create", result["alarms"][0]["name"]],
        ["content-set", "allow"],
    ]
    assert result["operations"] == [
        ["get", "https://example.test"],
        ["set", "https://example.test/*", "allow"],
        ["get", "https://example.test"],
        ["set", "https://example.test/*", "block"],
        ["set", "https://example.test/*", "allow"],
        ["set", "https://example.test/*", "ask"],
        ["get", "https://storage-fail.test"],
    ]
    assert result["reset"]["ok"] is True
    assert result["finalLeases"] == []
    assert result["storageFailure"] == {
        "ok": False,
        "error": "storage unavailable",
        "lease_id": "https%3A%2F%2Fstorage-fail.test:camera",
        "origin": "https://storage-fail.test",
        "permission": "camera",
        "recovery_pending": False,
    }
    assert result["clipboard"] == {
        "ok": False,
        "unsupported": True,
        "error": "clipboard permission leases are unsupported because the exact prior state cannot be restored",
    }
    assert result["debuggerCommands"] == []


def test_extension_permission_alarm_startup_and_wake_recovery_are_executable():
    code = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('const PERMISSION_LEASES_KEY');
const end = source.indexOf('async function handleExtMessage', start);
if (start < 0 || end < 0) throw new Error('permission lease helpers not found');
let now = 1000000;
Date.now = () => now;
const saved = {{ tmwdPermissionLeases: [] }};
const activeAlarms = new Map();
const alarmListeners = [];
const startupListeners = [];
const installedListeners = [];
const effective = new Map();
let failNextRestore = false;
const originFromPattern = pattern => pattern.endsWith('/*') ? pattern.slice(0, -2) : pattern;
const chrome = {{
  storage: {{ local: {{
    async get(key) {{ return {{ [key]: saved[key] }}; }},
    async set(value) {{ Object.assign(saved, JSON.parse(JSON.stringify(value))); }},
  }} }},
  alarms: {{
    onAlarm: {{ addListener(listener) {{ alarmListeners.push(listener); }} }},
    async create(name, details) {{ activeAlarms.set(name, JSON.parse(JSON.stringify(details))); }},
    async clear(name) {{ return activeAlarms.delete(name); }},
  }},
  runtime: {{
    onStartup: {{ addListener(listener) {{ startupListeners.push(listener); }} }},
    onInstalled: {{ addListener(listener) {{ installedListeners.push(listener); }} }},
  }},
  debugger: {{
    onEvent: {{ addListener() {{}} }},
    onDetach: {{ addListener() {{}} }},
  }},
  tabs: {{ onRemoved: {{ addListener() {{}} }} }},
  contentSettings: {{ camera: {{
    async get(details) {{ return {{ setting: effective.get(details.primaryUrl) || 'ask' }}; }},
    async set(details) {{
      if (failNextRestore && details.setting === 'ask') {{
        failNextRestore = false;
        throw new Error('restore unavailable');
      }}
      effective.set(originFromPattern(details.primaryPattern), details.setting);
    }},
  }} }},
}};
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const dialogAttachedTabs = new Set();
eval(source.slice(start, end));
const makeLease = (origin, suffix) => {{
  const id = permissionLeaseId(origin, 'camera');
  return {{
    id, origin, permission: 'camera', kind: 'content', contentSetting: 'camera',
    previousSetting: 'ask', expiresAt: now - 1,
    alarmName: `tmwd-permission:${{id}}:${{suffix}}`, state: 'active',
  }};
}};
(async () => {{
  const wakeOrigin = 'https://wake.test';
  const wakeLease = makeLease(wakeOrigin, 'wake');
  saved.tmwdPermissionLeases = [wakeLease];
  effective.set(wakeOrigin, 'allow');
  activeAlarms.set(wakeLease.alarmName, {{ when: wakeLease.expiresAt }});
  const wakeResult = await installPermissionLeaseRecoveryHooks();

  const startupOrigin = 'https://startup.test';
  const startupLease = makeLease(startupOrigin, 'startup');
  saved.tmwdPermissionLeases = [startupLease];
  effective.set(startupOrigin, 'block');
  activeAlarms.set(startupLease.alarmName, {{ when: startupLease.expiresAt }});
  const startupResult = await startupListeners[0]();

  const expiryOrigin = 'https://expiry.test';
  effective.set(expiryOrigin, 'ask');
  const setForExpiry = await setSitePermission({{
    permission: 'camera', setting: 'allow', origin: expiryOrigin, durationSeconds: 60,
  }});
  const expiryLease = JSON.parse(JSON.stringify(saved.tmwdPermissionLeases[0]));
  now = expiryLease.expiresAt;
  const expiryResult = await alarmListeners[0]({{ name: expiryLease.alarmName }});

  const retryOrigin = 'https://retry.test';
  effective.set(retryOrigin, 'ask');
  now += 1000;
  await setSitePermission({{
    permission: 'camera', setting: 'allow', origin: retryOrigin, durationSeconds: 60,
  }});
  const retryLease = JSON.parse(JSON.stringify(saved.tmwdPermissionLeases[0]));
  now = retryLease.expiresAt;
  failNextRestore = true;
  const failedRestore = await alarmListeners[0]({{ name: retryLease.alarmName }});
  const retainedAfterFailure = JSON.parse(JSON.stringify(saved.tmwdPermissionLeases));
  const retryName = permissionRetryAlarmName(retryLease);
  const retryAlarm = JSON.parse(JSON.stringify(activeAlarms.get(retryName)));
  now = retryAlarm.when;
  const retryResult = await alarmListeners[0]({{ name: retryName }});

  console.log(JSON.stringify({{
    hookCounts: [alarmListeners.length, startupListeners.length, installedListeners.length],
    wakeResult, wakeEffective: effective.get(wakeOrigin), wakeRemaining: wakeResult.remaining,
    startupResult, startupEffective: effective.get(startupOrigin),
    setForExpiry, expiryResult, expiryEffective: effective.get(expiryOrigin),
    failedRestore, retainedAfterFailure, retryAlarm, retryResult,
    retryEffective: effective.get(retryOrigin), finalLeases: saved.tmwdPermissionLeases,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(["node", "-e", code], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["hookCounts"] == [1, 1, 1]
    assert result["wakeResult"]["ok"] is True
    assert result["wakeEffective"] == "ask"
    assert result["wakeRemaining"] == 0
    assert result["startupResult"]["ok"] is True
    assert result["startupEffective"] == "ask"
    assert result["setForExpiry"]["ok"] is True
    assert result["expiryResult"]["ok"] is True
    assert result["expiryEffective"] == "ask"
    assert result["failedRestore"]["ok"] is False
    assert result["failedRestore"]["recovery_pending"] is True
    assert len(result["retainedAfterFailure"]) == 1
    assert result["retryAlarm"]["when"] > result["retainedAfterFailure"][0]["expiresAt"]
    assert result["retryResult"]["ok"] is True
    assert result["retryEffective"] == "ask"
    assert result["finalLeases"] == []


def test_extension_replacement_failure_keeps_original_deadline_and_recovers():
    code = f"""
const fs = require('fs');
const source = fs.readFileSync({json.dumps(str(BACKGROUND))}, 'utf8');
const start = source.indexOf('const PERMISSION_LEASES_KEY');
const end = source.indexOf('async function handleExtMessage', start);
if (start < 0 || end < 0) throw new Error('permission lease helpers not found');
let now = 2000000;
Date.now = () => now;
const saved = {{ tmwdPermissionLeases: [] }};
const activeAlarms = new Map();
const alarmCreates = [];
const alarmListeners = [];
let failReplacementCommit = false;
let failRollbackAllow = false;
let effectiveSetting = 'ask';
const chrome = {{
  storage: {{ local: {{
    async get(key) {{ return {{ [key]: saved[key] }}; }},
    async set(value) {{
      const leases = value.tmwdPermissionLeases || [];
      if (failReplacementCommit && leases[0]?.state === 'active') {{
        failReplacementCommit = false;
        failRollbackAllow = true;
        throw new Error('replacement storage unavailable');
      }}
      Object.assign(saved, JSON.parse(JSON.stringify(value)));
    }},
  }} }},
  alarms: {{
    onAlarm: {{ addListener(listener) {{ alarmListeners.push(listener); }} }},
    async create(name, details) {{
      alarmCreates.push({{ name, details: JSON.parse(JSON.stringify(details)) }});
      activeAlarms.set(name, JSON.parse(JSON.stringify(details)));
    }},
    async clear(name) {{ return activeAlarms.delete(name); }},
  }},
  runtime: {{
    onStartup: {{ addListener() {{}} }},
    onInstalled: {{ addListener() {{}} }},
  }},
  debugger: {{
    onEvent: {{ addListener() {{}} }},
    onDetach: {{ addListener() {{}} }},
  }},
  tabs: {{ onRemoved: {{ addListener() {{}} }} }},
  contentSettings: {{ camera: {{
    async get() {{ return {{ setting: effectiveSetting }}; }},
    async set(details) {{
      if (failRollbackAllow && details.setting === 'allow') {{
        failRollbackAllow = false;
        throw new Error('rollback unavailable');
      }}
      effectiveSetting = details.setting;
    }},
  }} }},
}};
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const dialogAttachedTabs = new Set();
eval(source.slice(start, end));
(async () => {{
  await installPermissionLeaseRecoveryHooks();
  const initial = await setSitePermission({{
    permission: 'camera', setting: 'allow', origin: 'https://example.test', durationSeconds: 60,
  }});
  const priorLease = JSON.parse(JSON.stringify(saved.tmwdPermissionLeases[0]));
  const priorAlarm = JSON.parse(JSON.stringify(activeAlarms.get(priorLease.alarmName)));
  now += 10000;
  failReplacementCommit = true;
  const replacement = await setSitePermission({{
    permission: 'camera', setting: 'block', origin: 'https://example.test', durationSeconds: 120,
  }});
  const leaseAfterFailure = JSON.parse(JSON.stringify(saved.tmwdPermissionLeases[0]));
  const alarmAfterFailure = JSON.parse(JSON.stringify(activeAlarms.get(priorLease.alarmName)));
  const effectiveAfterFailure = effectiveSetting;
  const originalAlarmCreateCount = alarmCreates.filter(item => item.name === priorLease.alarmName).length;
  now = priorLease.expiresAt;
  const recovered = await alarmListeners[0]({{ name: priorLease.alarmName }});
  console.log(JSON.stringify({{
    initial, priorLease, priorAlarm, replacement, leaseAfterFailure, alarmAfterFailure,
    effectiveAfterFailure, originalAlarmCreateCount, recovered,
    effectiveAfterRecovery: effectiveSetting, finalLeases: saved.tmwdPermissionLeases,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    completed = subprocess.run(["node", "-e", code], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["initial"]["ok"] is True
    assert result["replacement"]["ok"] is False
    assert result["replacement"]["error"] == "replacement storage unavailable"
    assert result["replacement"]["recovery_pending"] is True
    assert result["replacement"]["recovery_error"] == "rollback unavailable"
    assert result["effectiveAfterFailure"] == "block"
    assert result["leaseAfterFailure"] == result["priorLease"]
    assert result["alarmAfterFailure"] == result["priorAlarm"]
    assert result["originalAlarmCreateCount"] == 1
    assert result["recovered"]["ok"] is True
    assert result["effectiveAfterRecovery"] == "ask"
    assert result["finalLeases"] == []

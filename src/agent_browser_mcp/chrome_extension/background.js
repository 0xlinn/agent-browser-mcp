// background.js - Cookie + CDP Bridge
chrome.runtime.onInstalled.addListener(() => {
  console.log('CDP Bridge installed');
  // Drop the old browser-wide CSP-stripping rule if this is an upgrade.
  // It used urlFilter:'*' as a DYNAMIC rule, so it survived restarts and
  // disabled CSP for every site in the profile — bank, mail, everything —
  // for as long as the extension stayed installed. CSP is now stripped
  // per-tab and only while a script is actually running (see withCspOff).
  chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [9999] });
  // Re-inject content scripts into already-open pages. After an extension
  // reload, pages opened under the OLD extension keep running orphaned
  // content.js whose chrome.runtime points at a dead context — their badge
  // sticks on "检测中" until a manual refresh. Re-injecting rebinds them to
  // this fresh SW so the user never has to reload each tab by hand. (WS tab
  // registration never depended on content.js; this only fixes the badge/UX.)
  reinjectAllTabs();
});

async function reinjectAllTabs() {
  let tabs;
  try { tabs = await chrome.tabs.query({}); } catch (_) { return; }
  for (const tab of tabs) {
    if (!tab.id || !isScriptable(tab.url)) continue;
    try {
      // config.js defines TID which content.js reads; must load in order.
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        files: ['config.js', 'content.js'],
      });
    } catch (_) {
      // Restricted pages (web store, chrome://, PDF viewer) reject injection;
      // skip them silently — they were never scriptable anyway.
    }
  }
}

// --- Bounded, tab-scoped JavaScript dialog state --------------------------
const DIALOG_STATE_TTL_MS = 30000;
const protocolDialogStates = new Map();
const dialogAttachedTabs = new Set();
const debuggerAttachments = new Map();
const debuggerRecoveryPromises = new Map();
const pendingNavigations = new Map();
const execDialogPolicies = new Map();
const pendingManualExecutions = new Map();
const runtimeExecutionContexts = new Map();
const runtimeContextWaiters = new Map();
const dialogEventSequences = new Map();
const manualExecutionGenerations = new Map();
const networkCaptures = new Map();
const consoleCaptures = new Map();
let nextDialogScope = 1;
let nextManualExecutionGeneration = 1;
const DEFAULT_CDP_TIMEOUT_MS = 20000;
const MAX_CDP_TIMEOUT_MS = 120000;

// --- Stable tab lifecycle generations ------------------------------------
// A native tab id can be reused after close/restart while the Python bridge
// still has an active-looking session under clientId:tabId. Pair every tab
// lifecycle with a generation that survives service-worker eviction (session
// storage lasts for the browser session) so open_new_tab never accepts a stale
// registration merely because the numeric id matches.
const TAB_GENERATIONS_KEY = 'tmwdTabGenerationsV1'; // gitleaks:allow - storage key, not a credential
const tabGenerations = new Map();
const tabGenerationAssignments = new Map();
let tabGenerationsLoadPromise = null;
let tabGenerationWriteQueue = Promise.resolve();
let nextTabGeneration = 1;

// Native tab creation is a two-step side effect (create, then record the
// result). Persist the operation before create so a service-worker restart
// can reconcile an in-flight operation without issuing a second create.
const CREATE_OPERATIONS_KEY = 'tmwdCreateOperationsV1'; // gitleaks:allow - storage key, not a credential
const CREATE_OPERATION_MAX_RECORDS = 256;
const CREATE_OPERATION_TTL_MS = 24 * 60 * 60 * 1000;
const createOperations = new Map();
const createOperationPromises = new Map();
let createOperationsLoadPromise = null;
let createOperationsWriteQueue = Promise.resolve();

function pruneCreateOperations() {
  const cutoff = Date.now() - CREATE_OPERATION_TTL_MS;
  for (const [operationId, record] of createOperations.entries()) {
    if (record?.status !== 'pending' && Number(record?.created_at || 0) < cutoff) {
      createOperations.delete(operationId);
    }
  }
  if (createOperations.size <= CREATE_OPERATION_MAX_RECORDS) return;
  const ordered = [...createOperations.entries()]
    .filter(([, record]) => record?.status !== 'pending')
    .sort((a, b) => Number(a[1]?.created_at || 0) - Number(b[1]?.created_at || 0));
  for (const [operationId] of ordered.slice(0, Math.max(0, createOperations.size - CREATE_OPERATION_MAX_RECORDS))) {
    createOperations.delete(operationId);
  }
}

function persistCreateOperations() {
  const area = chrome.storage?.session;
  if (!area) return Promise.reject(new Error('chrome.storage.session is unavailable'));
  pruneCreateOperations();
  const snapshot = Object.fromEntries(
    [...createOperations.entries()].map(([id, record]) => [id, {
      operation_id: record.operation_id,
      status: record.status,
      url: record.url,
      client_id: record.client_id || null,
      id: record.id ?? null,
      generation: record.generation || null,
      title: record.title || '',
      windowId: record.windowId ?? null,
      tab_status: record.tab_status || 'unknown',
      load_ready: Boolean(record.load_ready),
      created_at: record.created_at || Date.now(),
    }]),
  );
  const write = createOperationsWriteQueue
    .catch(() => {})
    .then(() => area.set({ [CREATE_OPERATIONS_KEY]: snapshot }));
  createOperationsWriteQueue = write;
  return write;
}

async function loadCreateOperations() {
  if (createOperationsLoadPromise) return await createOperationsLoadPromise;
  createOperationsLoadPromise = (async () => {
    try {
      const area = chrome.storage?.session;
      if (!area) throw new Error('chrome.storage.session is unavailable');
      const stored = (await area.get(CREATE_OPERATIONS_KEY))[CREATE_OPERATIONS_KEY] || {};
      for (const [operationId, record] of Object.entries(stored)) {
        if (record && typeof record === 'object') {
          createOperations.set(String(operationId), { ...record, operation_id: String(operationId) });
        }
      }
      pruneCreateOperations();
    } catch (error) {
      console.log('[TMWD-WS] create operation storage unavailable', error);
      throw error;
    }
    return createOperations;
  })();
  return await createOperationsLoadPromise;
}

function operationRecordData(record) {
  return {
    operation_id: record.operation_id,
    // Keep the historical status field as the native tab load status. The
    // operation state is explicit so old callers do not mistake a loading tab
    // for a missing operation.
    status: record.tab_status || 'unknown',
    operation_status: record.status || 'unknown',
    id: record.id,
    generation: record.generation,
    url: record.url,
    title: record.title || '',
    windowId: record.windowId,
    tab_status: record.tab_status || 'unknown',
    load_ready: Boolean(record.load_ready),
    client_id: record.client_id || null,
    may_have_created: record.status === 'pending',
    retry_safe: record.status === 'not_found',
  };
}

async function createTabStatus(msg) {
  const operationId = String(msg.operation_id || '');
  if (!operationId) return { ok: false, error: 'create_status requires operation_id' };
  try {
    await loadCreateOperations();
  } catch (error) {
    return { ok: true, data: {
      status: 'unknown', operation_status: 'unknown', operation_id: operationId,
      may_have_created: true, retry_safe: false,
      error: error.message || String(error),
    } };
  }
  const record = createOperations.get(operationId);
  if (!record) return { ok: true, data: {
    status: 'not_found', operation_status: 'not_found', operation_id: operationId,
    may_have_created: false, retry_safe: true,
  } };
  return { ok: true, data: operationRecordData(record) };
}

function newTabGeneration() {
  const random = globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2);
  return `${Date.now().toString(36)}-${nextTabGeneration++}-${random}`;
}

function persistTabGenerations() {
  const area = chrome.storage?.session;
  if (!area) return Promise.resolve();
  const snapshot = Object.fromEntries(tabGenerations);
  tabGenerationWriteQueue = tabGenerationWriteQueue
    .then(() => area.set({ [TAB_GENERATIONS_KEY]: snapshot }))
    .catch(error => console.log('[TMWD-WS] tab generation persistence unavailable', error));
  return tabGenerationWriteQueue;
}

async function loadTabGenerations() {
  if (tabGenerationsLoadPromise) return await tabGenerationsLoadPromise;
  tabGenerationsLoadPromise = (async () => {
    let stored = {};
    try {
      const area = chrome.storage?.session;
      if (area) stored = (await area.get(TAB_GENERATIONS_KEY))[TAB_GENERATIONS_KEY] || {};
    } catch (_) {}
    const tabs = await chrome.tabs.query({}).catch(() => []);
    const live = new Set(tabs.map(tab => String(tab.id)));
    for (const [rawId, generation] of Object.entries(stored)) {
      if (live.has(String(rawId)) && typeof generation === 'string') {
        tabGenerations.set(Number(rawId), generation);
      }
    }
    for (const tab of tabs) {
      if (!tabGenerations.has(tab.id)) tabGenerations.set(tab.id, newTabGeneration());
    }
    await persistTabGenerations();
  })();
  return await tabGenerationsLoadPromise;
}

function scheduleNewTabGeneration(tabId) {
  if (tabGenerationAssignments.has(tabId)) return tabGenerationAssignments.get(tabId);
  const assignment = (async () => {
    await loadTabGenerations();
    const generation = newTabGeneration();
    tabGenerations.set(tabId, generation);
    await persistTabGenerations();
    return generation;
  })().finally(() => tabGenerationAssignments.delete(tabId));
  tabGenerationAssignments.set(tabId, assignment);
  return assignment;
}

async function tabGenerationFor(tabId) {
  const pending = tabGenerationAssignments.get(tabId);
  if (pending) return await pending;
  await loadTabGenerations();
  if (!tabGenerations.has(tabId)) {
    tabGenerations.set(tabId, newTabGeneration());
    await persistTabGenerations();
  }
  return tabGenerations.get(tabId);
}

async function forgetTabGeneration(tabId) {
  const pending = tabGenerationAssignments.get(tabId);
  if (pending) await pending.catch(() => {});
  await loadTabGenerations();
  tabGenerations.delete(tabId);
  await persistTabGenerations();
}

async function validateTabCloseGenerations(tabIds, expected) {
  if (!expected || typeof expected !== 'object') return null;
  for (const tabId of tabIds) {
    const expectedGeneration = expected[String(tabId)];
    if (typeof expectedGeneration !== 'string') {
      return `missing expected generation for tab ${tabId}`;
    }
    const currentGeneration = await tabGenerationFor(tabId);
    if (currentGeneration !== expectedGeneration) {
      return `tab ${tabId} lifecycle generation changed; refusing close`;
    }
  }
  return null;
}

async function closeTabsWithGenerations(tabIds, expected) {
  const liveIds = [];
  const alreadyGone = [];
  for (const tabId of tabIds) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) {
      alreadyGone.push(tabId);
      continue;
    }
    if (expected && typeof expected === 'object') {
      const expectedGeneration = expected[String(tabId)];
      if (typeof expectedGeneration !== 'string') {
        throw new Error(`missing expected generation for tab ${tabId}`);
      }
      const currentGeneration = await tabGenerationFor(tabId);
      if (currentGeneration !== expectedGeneration) {
        throw new Error(`tab ${tabId} lifecycle generation changed; refusing close`);
      }
    }
    liveIds.push(tabId);
  }
  // Validate every live tab before removing any member of the batch.
  if (liveIds.length) await chrome.tabs.remove(liveIds);
  return { closed: liveIds, alreadyGone };
}

// --- Temporary, origin-scoped site-permission leases ----------------------
const PERMISSION_LEASES_KEY = 'tmwdPermissionLeases';
const PERMISSION_ALARM_PREFIX = 'tmwd-permission:';
const PERMISSION_LEASE_STAGED = 'staged';
const PERMISSION_LEASE_ACTIVE = 'active';
const SITE_PERMISSION_SETTINGS = new Set(['allow', 'block', 'ask']);
const SITE_PERMISSION_CONTENT_SETTINGS = {
  notifications: 'notifications',
  geolocation: 'location',
  location: 'location',
  camera: 'camera',
  microphone: 'microphone',
};
let permissionLeaseQueue = Promise.resolve();
let nextPermissionAlarmSequence = 1;

function withPermissionLeaseLock(work) {
  const next = permissionLeaseQueue.then(work, work);
  permissionLeaseQueue = next.catch(() => {});
  return next;
}

function normalizePermissionOrigin(raw) {
  try {
    const url = new URL(String(raw || ''));
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || !url.hostname ||
        url.username || url.password) {
      return null;
    }
    return url.origin;
  } catch (_) {
    return null;
  }
}

function permissionLeaseId(origin, permission) {
  return `${encodeURIComponent(origin)}:${permission}`;
}

function permissionAlarmName(lease) {
  return typeof lease.alarmName === 'string' && lease.alarmName.startsWith(PERMISSION_ALARM_PREFIX)
    ? lease.alarmName
    : `${PERMISSION_ALARM_PREFIX}${lease.id}`;
}

function permissionRetryAlarmName(lease) {
  return `${PERMISSION_ALARM_PREFIX}retry:${lease.id}`;
}

function newPermissionAlarmName(id) {
  return `${PERMISSION_ALARM_PREFIX}${id}:${Date.now()}:${nextPermissionAlarmSequence++}`;
}

async function clearPermissionAlarm(name) {
  try { await chrome.alarms.clear(name); } catch (_) {}
}

async function schedulePermissionRetry(lease) {
  await chrome.alarms.create(
    permissionRetryAlarmName(lease), { when: Date.now() + 60000 },
  );
}

async function loadPermissionLeases() {
  const stored = await chrome.storage.local.get(PERMISSION_LEASES_KEY);
  const leases = stored[PERMISSION_LEASES_KEY];
  return Array.isArray(leases) ? leases.filter(lease => lease && typeof lease === 'object') : [];
}

async function savePermissionLeases(leases) {
  await chrome.storage.local.set({ [PERMISSION_LEASES_KEY]: leases });
}

function contentSettingForPermission(permission) {
  return SITE_PERMISSION_CONTENT_SETTINGS[permission] || null;
}

async function setClipboardPermission(tabId, origin, setting) {
  if (!Number.isInteger(tabId)) {
    return { ok: false, unsupported: true, error: 'clipboard permission requires a tabId' };
  }
  let debuggerLease = null;
  try {
    debuggerLease = await attachAbmDebugger({ tabId });
    await sendDebuggerCommandWithTimeout(debuggerLease, 'Browser.setPermission', {
      permission: { name: 'clipboard-read' },
      setting: setting === 'allow' ? 'granted' : setting === 'block' ? 'denied' : 'prompt',
      origin,
    }, DEFAULT_CDP_TIMEOUT_MS);
    return { ok: true };
  } catch (error) {
    // The extension debugger commonly rejects Browser.*. It is never safe to
    // fall back to page or physical input for a browser permission operation.
    return { ok: false, unsupported: true, error: error.message || String(error) };
  } finally {
    if (debuggerLease) {
      try { await detachAbmDebugger(debuggerLease); } catch (_) {}
    }
  }
}

async function applyPermissionLease(lease, setting) {
  if (lease.kind === 'clipboard') {
    return await setClipboardPermission(lease.tabId, lease.origin, setting);
  }
  const contentSetting = chrome.contentSettings?.[lease.contentSetting];
  if (!contentSetting) {
    return { ok: false, unsupported: true, error: `content setting unavailable: ${lease.contentSetting}` };
  }
  try {
    await contentSetting.set({ primaryPattern: `${lease.origin}/*`, setting });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error.message || String(error) };
  }
}

async function restorePermissionLease(lease) {
  return await applyPermissionLease(lease, lease.previousSetting);
}

async function restorePermissionLeaseMatches(matches) {
  const leases = await loadPermissionLeases();
  const kept = [];
  const succeeded = [];
  const attempted = [];
  let restored = 0;
  const failures = [];
  for (const lease of leases) {
    if (!matches(lease)) {
      kept.push(lease);
      continue;
    }
    attempted.push(lease);
    const result = await restorePermissionLease(lease);
    if (result.ok) {
      restored += 1;
      succeeded.push(lease);
    } else {
      // A failed restore must survive worker eviction and retries.
      kept.push(lease);
      failures.push({ id: lease.id, error: result.error || 'restore failed', unsupported: !!result.unsupported });
    }
  }
  if (!attempted.length) {
    return { ok: true, restored: 0, failures: [], remaining: kept.length };
  }
  try {
    await savePermissionLeases(kept);
  } catch (error) {
    for (const lease of attempted) {
      try { await schedulePermissionRetry(lease); } catch (_) {}
    }
    return {
      ok: false,
      restored,
      failures: [...failures, { id: '', error: error.message || String(error), storage: true }],
      remaining: leases.length,
      recovery_pending: true,
      recovery_error: error.message || String(error),
    };
  }
  for (const lease of succeeded) {
    await clearPermissionAlarm(permissionAlarmName(lease));
    await clearPermissionAlarm(permissionRetryAlarmName(lease));
  }
  for (const failure of failures) {
    const lease = kept.find(item => item.id === failure.id);
    if (!lease) continue;
    try {
      await schedulePermissionRetry(lease);
    } catch (error) {
      failure.recovery_error = error.message || String(error);
    }
  }
  return {
    ok: failures.length === 0,
    restored,
    failures,
    remaining: kept.length,
    ...(failures.length ? { recovery_pending: true } : {}),
  };
}

async function restoreExpiredPermissionLeases() {
  return await withPermissionLeaseLock(async () => {
    const now = Date.now();
    return await restorePermissionLeaseMatches(lease =>
      lease.state === PERMISSION_LEASE_STAGED || Number(lease.expiresAt) <= now
    );
  });
}

function installPermissionLeaseRecoveryHooks() {
  chrome.alarms.onAlarm.addListener(async alarm => {
    if (alarm?.name?.startsWith(PERMISSION_ALARM_PREFIX)) {
      return await restoreExpiredPermissionLeases();
    }
  });
  chrome.runtime.onStartup.addListener(async () => await restoreExpiredPermissionLeases());
  chrome.runtime.onInstalled.addListener(async () => await restoreExpiredPermissionLeases());
  // A service-worker wake evaluates this file from the top, so the immediate
  // sweep also recovers interrupted staged mutations.
  return restoreExpiredPermissionLeases();
}

async function resetSitePermissionLeases({ origin = '', permission = '' } = {}) {
  const normalizedOrigin = origin ? normalizePermissionOrigin(origin) : '';
  if (origin && !normalizedOrigin) return { ok: false, error: 'origin must be an http or https origin' };
  const contentSetting = permission ? contentSettingForPermission(permission) : null;
  if (permission && permission !== 'clipboard' && !contentSetting) {
    return { ok: false, error: 'unsupported permission' };
  }
  const canonicalPermission = contentSetting || permission;
  return await withPermissionLeaseLock(async () => await restorePermissionLeaseMatches(lease =>
    (!normalizedOrigin || lease.origin === normalizedOrigin) &&
    (!canonicalPermission || lease.permission === canonicalPermission)
  ));
}

async function setSitePermission(msg) {
  const origin = normalizePermissionOrigin(msg.origin);
  const durationSeconds = msg.durationSeconds;
  const setting = msg.setting;
  const contentSetting = contentSettingForPermission(msg.permission);
  if (!origin) return { ok: false, error: 'origin must be an http or https origin' };
  if (!SITE_PERMISSION_SETTINGS.has(setting)) return { ok: false, error: 'invalid permission setting' };
  if (!Number.isInteger(durationSeconds) || durationSeconds < 60 || durationSeconds > 600) {
    return { ok: false, error: 'durationSeconds must be an integer from 60 to 600' };
  }
  if (msg.permission !== 'clipboard' && !contentSetting) {
    return { ok: false, error: 'unsupported permission' };
  }
  if (msg.permission === 'clipboard') {
    return {
      ok: false,
      unsupported: true,
      error: 'clipboard permission leases are unsupported because the exact prior state cannot be restored',
    };
  }

  return await withPermissionLeaseLock(async () => {
    const now = Date.now();
    const recovery = await restorePermissionLeaseMatches(lease =>
      lease.state === PERMISSION_LEASE_STAGED || Number(lease.expiresAt) <= now
    );
    if (!recovery.ok) {
      return {
        ok: false,
        error: 'an earlier site-permission lease is still awaiting recovery',
        recovery_pending: true,
        recovery_error: recovery.recovery_error || recovery.failures?.[0]?.error || 'restore failed',
        origin,
        permission: contentSetting,
      };
    }
    const permission = contentSetting || 'clipboard';
    const leases = await loadPermissionLeases();
    const id = permissionLeaseId(origin, permission);
    const existing = leases.find(lease => lease.id === id) || null;
    const api = chrome.contentSettings?.[contentSetting];
    if (!api) return { ok: false, unsupported: true, error: `content setting unavailable: ${contentSetting}` };
    let effectiveSetting;
    try {
      const current = await api.get({ primaryUrl: origin });
      effectiveSetting = current?.setting;
    } catch (error) {
      return { ok: false, error: error.message || String(error) };
    }
    if (typeof effectiveSetting !== 'string') return { ok: false, error: 'content setting has no restorable state' };
    const previousSetting = existing ? existing.previousSetting : effectiveSetting;
    const lease = {
      id,
      origin,
      permission,
      kind: permission === 'clipboard' ? 'clipboard' : 'content',
      contentSetting: permission === 'clipboard' ? undefined : contentSetting,
      previousSetting,
      tabId: permission === 'clipboard' ? msg.tabId : undefined,
      expiresAt: Date.now() + durationSeconds * 1000,
      alarmName: newPermissionAlarmName(id),
      state: PERMISSION_LEASE_ACTIVE,
    };
    const updated = leases.filter(item => item.id !== id);
    updated.push(lease);

    const failure = (result, recoveryPending, recoveryError = '') => ({
      ...result,
      lease_id: id,
      origin,
      permission,
      recovery_pending: !!recoveryPending,
      ...(recoveryError ? { recovery_error: recoveryError } : {}),
    });

    if (existing) {
      // The existing record and alarm stay untouched through mutation and
      // rollback. They remain the durable upper bound on the old lease.
      const applied = await applyPermissionLease(lease, setting);
      if (!applied.ok) {
        const rolledBack = await applyPermissionLease(lease, effectiveSetting);
        return failure(
          applied,
          !rolledBack.ok,
          rolledBack.ok ? '' : (rolledBack.error || 'rollback failed'),
        );
      }
      try {
        await chrome.alarms.create(lease.alarmName, { when: lease.expiresAt });
      } catch (error) {
        const rolledBack = await applyPermissionLease(lease, effectiveSetting);
        return failure(
          { ok: false, error: error.message || String(error) },
          !rolledBack.ok,
          rolledBack.ok ? '' : (rolledBack.error || 'rollback failed'),
        );
      }
      try {
        await savePermissionLeases(updated);
      } catch (error) {
        const rolledBack = await applyPermissionLease(lease, effectiveSetting);
        await clearPermissionAlarm(lease.alarmName);
        return failure(
          { ok: false, error: error.message || String(error) },
          !rolledBack.ok,
          rolledBack.ok ? '' : (rolledBack.error || 'rollback failed'),
        );
      }
      await clearPermissionAlarm(permissionAlarmName(existing));
      await clearPermissionAlarm(permissionRetryAlarmName(existing));
      return { ok: true, lease_id: id, origin, permission, expires_at: lease.expiresAt };
    }

    const stagedLease = { ...lease, state: PERMISSION_LEASE_STAGED };
    const staged = leases.filter(item => item.id !== id);
    staged.push(stagedLease);
    try {
      await savePermissionLeases(staged);
    } catch (error) {
      return failure({ ok: false, error: error.message || String(error) }, false);
    }
    try {
      await chrome.alarms.create(stagedLease.alarmName, { when: stagedLease.expiresAt });
    } catch (error) {
      try {
        await savePermissionLeases(leases);
        await clearPermissionAlarm(stagedLease.alarmName);
        return failure({ ok: false, error: error.message || String(error) }, false);
      } catch (cleanupError) {
        return failure(
          { ok: false, error: error.message || String(error) },
          true,
          cleanupError.message || String(cleanupError),
        );
      }
    }

    const applied = await applyPermissionLease(stagedLease, setting);
    if (!applied.ok) {
      const rolledBack = await applyPermissionLease(stagedLease, stagedLease.previousSetting);
      if (rolledBack.ok) {
        try {
          await savePermissionLeases(leases);
          await clearPermissionAlarm(stagedLease.alarmName);
          return failure(applied, false);
        } catch (error) {
          return failure(applied, true, error.message || String(error));
        }
      }
      return failure(applied, true, rolledBack.error || 'rollback failed');
    }
    try {
      await savePermissionLeases(updated);
    } catch (error) {
      const rolledBack = await applyPermissionLease(stagedLease, stagedLease.previousSetting);
      if (rolledBack.ok) {
        try {
          await savePermissionLeases(leases);
          await clearPermissionAlarm(stagedLease.alarmName);
          return failure({ ok: false, error: error.message || String(error) }, false);
        } catch (cleanupError) {
          return failure(
            { ok: false, error: error.message || String(error) },
            true,
            cleanupError.message || String(cleanupError),
          );
        }
      }
      return failure(
        { ok: false, error: error.message || String(error) },
        true,
        rolledBack.error || 'rollback failed',
      );
    }
    return { ok: true, lease_id: id, origin, permission, expires_at: lease.expiresAt };
  });
}

function validDialogPolicy(policy) {
  return policy === 'dismiss' || policy === 'accept' || policy === 'manual';
}

function currentProtocolDialog(tabId) {
  const state = protocolDialogStates.get(tabId);
  if (state && Date.now() - state.openedAt < DIALOG_STATE_TTL_MS) return state;
  if (state) protocolDialogStates.delete(tabId);
  return null;
}

function rememberProtocolDialog(tabId, params) {
  const state = {
    type: params.type,
    message: params.message || '',
    url: params.url || '',
    defaultPrompt: params.defaultPrompt || '',
    openedAt: Date.now(),
  };
  protocolDialogStates.set(tabId, state);
  setTimeout(() => {
    if (protocolDialogStates.get(tabId)?.openedAt === state.openedAt) {
      protocolDialogStates.delete(tabId);
    }
  }, DIALOG_STATE_TTL_MS);
  return state;
}

function rememberRuntimeExecutionContext(tabId, context) {
  if (!context || !Number.isInteger(context.id)) return;
  const contexts = runtimeExecutionContexts.get(tabId) || new Map();
  contexts.set(context.id, context);
  runtimeExecutionContexts.set(tabId, contexts);
  const waiters = runtimeContextWaiters.get(tabId);
  if (!waiters) return;
  for (const waiter of [...waiters]) {
    if (context.auxData?.isDefault && context.auxData?.frameId === waiter.frameId) {
      waiters.delete(waiter);
      clearTimeout(waiter.timer);
      waiter.resolve(context.id);
    }
  }
  if (!waiters.size) runtimeContextWaiters.delete(tabId);
}

function forgetRuntimeExecutionContext(tabId, executionContextId) {
  const contexts = runtimeExecutionContexts.get(tabId);
  if (!contexts) return;
  contexts.delete(executionContextId);
  if (!contexts.size) runtimeExecutionContexts.delete(tabId);
}

function defaultRuntimeExecutionContext(tabId, frameId) {
  const contexts = runtimeExecutionContexts.get(tabId);
  if (!contexts) return null;
  for (const context of contexts.values()) {
    if (context.auxData?.isDefault && context.auxData?.frameId === frameId) {
      return context.id;
    }
  }
  return null;
}

async function waitForDefaultRuntimeExecutionContext(tabId, frameId, timeoutMs = 1000) {
  const current = defaultRuntimeExecutionContext(tabId, frameId);
  if (current !== null) return current;
  return await new Promise((resolve, reject) => {
    const waiters = runtimeContextWaiters.get(tabId) || new Set();
    const waiter = { frameId, resolve, reject, timer: null };
    waiter.timer = setTimeout(() => {
      waiters.delete(waiter);
      if (!waiters.size) runtimeContextWaiters.delete(tabId);
      reject(new Error('manual execution could not resolve the MAIN execution context'));
    }, timeoutMs);
    waiters.add(waiter);
    runtimeContextWaiters.set(tabId, waiters);
  });
}

// --- Bounded, tab-scoped Network and Console capture ----------------------
function boundedCaptureInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? Math.max(minimum, Math.min(Math.floor(parsed), maximum))
    : fallback;
}

function truncateCaptureText(value, maxBytes) {
  const text = String(value ?? '');
  const bytes = new TextEncoder().encode(text);
  if (bytes.length <= maxBytes) {
    return { value: text, size: bytes.length, truncated: false };
  }
  return {
    value: new TextDecoder().decode(bytes.slice(0, maxBytes)),
    size: bytes.length,
    truncated: true,
  };
}

function truncateCaptureBody(body, base64Encoded, maxBytes) {
  if (base64Encoded) {
    const text = String(body ?? '');
    const maxChars = Math.max(4, Math.floor(maxBytes / 3) * 4);
    return {
      body: text.slice(0, maxChars),
      body_size: Math.floor(text.length * 3 / 4),
      body_truncated: text.length > maxChars,
      base64_encoded: true,
    };
  }
  const truncated = truncateCaptureText(body, maxBytes);
  return {
    body: truncated.value,
    body_size: truncated.size,
    body_truncated: truncated.truncated,
    base64_encoded: false,
  };
}

function pushCaptureEntry(capture, entry) {
  if (capture.entries.length >= capture.maxEntries) {
    const removed = capture.entries.shift();
    if (removed && capture.byRequestId?.get(removed.request_id) === removed) {
      capture.byRequestId.delete(removed.request_id);
    }
    capture.dropped += 1;
  }
  capture.entries.push(entry);
}

function networkCaptureSnapshot(capture, includeRequests = true) {
  const result = {
    status: capture.active ? 'capturing' : 'stopped',
    tab_id: capture.tabId,
    started_at: capture.startedAt,
    stopped_at: capture.stoppedAt,
    request_count: capture.entries.length,
    dropped: capture.dropped,
    body_errors: capture.bodyErrors,
    include_bodies: capture.includeBodies,
    max_entries: capture.maxEntries,
    max_body_bytes: capture.maxBodyBytes,
  };
  if (capture.lastError) result.error = capture.lastError;
  if (includeRequests) result.requests = capture.entries;
  return result;
}

function consoleCaptureSnapshot(capture, includeMessages = true) {
  const result = {
    status: capture.active ? 'capturing' : 'stopped',
    tab_id: capture.tabId,
    started_at: capture.startedAt,
    stopped_at: capture.stoppedAt,
    message_count: capture.messages.length,
    dropped: capture.dropped,
    max_entries: capture.maxEntries,
  };
  if (capture.lastError) result.error = capture.lastError;
  if (includeMessages) result.messages = capture.messages;
  return result;
}

function markCaptureTabInvalidated(tabId, reason) {
  const stoppedAt = Date.now();
  for (const capture of [networkCaptures.get(tabId), consoleCaptures.get(tabId)]) {
    if (!capture) continue;
    capture.active = false;
    capture.stoppedAt ||= stoppedAt;
    capture.lastError = reason || 'debugger detached';
    capture.debuggerLease = null;
  }
}

function handleNetworkCaptureEvent(tabId, method, params = {}) {
  const capture = networkCaptures.get(tabId);
  if (!capture?.active) return;
  if (method === 'Network.requestWillBeSent') {
    const postData = params.request?.postData === undefined
      ? null : truncateCaptureText(params.request.postData, Math.min(capture.maxBodyBytes, 65536));
    const entry = {
      request_id: params.requestId,
      url: params.request?.url || '',
      method: params.request?.method || '',
      type: params.type || '',
      timestamp: params.timestamp,
      wall_time: params.wallTime,
      document_url: params.documentURL || '',
      request_headers: params.request?.headers || {},
    };
    if (postData) {
      entry.post_data = postData.value;
      entry.post_data_size = postData.size;
      entry.post_data_truncated = postData.truncated;
    }
    pushCaptureEntry(capture, entry);
    capture.byRequestId.set(params.requestId, entry);
    return;
  }
  const entry = capture.byRequestId.get(params.requestId);
  if (!entry) return;
  if (method === 'Network.responseReceived') {
    entry.status = params.response?.status;
    entry.status_text = params.response?.statusText || '';
    entry.mime_type = params.response?.mimeType || '';
    entry.protocol = params.response?.protocol || '';
    entry.response_headers = params.response?.headers || {};
    entry.from_disk_cache = Boolean(params.response?.fromDiskCache);
    entry.from_service_worker = Boolean(params.response?.fromServiceWorker);
    return;
  }
  if (method === 'Network.loadingFailed') {
    entry.failed = true;
    entry.error_text = params.errorText || 'request failed';
    entry.canceled = Boolean(params.canceled);
    return;
  }
  if (method !== 'Network.loadingFinished') return;
  entry.encoded_data_length = params.encodedDataLength;
  entry.finished = true;
  if (!capture.includeBodies) return;
  const bodyPromise = (async () => {
    try {
      const result = await sendDebuggerCommandWithTimeout(
        capture.debuggerLease,
        'Network.getResponseBody',
        { requestId: params.requestId },
        capture.bodyTimeoutMs,
      );
      if (networkCaptures.get(tabId) !== capture) return;
      Object.assign(
        entry,
        truncateCaptureBody(result?.body, Boolean(result?.base64Encoded), capture.maxBodyBytes),
      );
    } catch (error) {
      entry.body_error = error.message || String(error);
      capture.bodyErrors += 1;
      if (debuggerFailureCode(error) === 'cdp_timeout') {
        capture.active = false;
        capture.stoppedAt ||= Date.now();
        capture.lastError = entry.body_error;
      }
    }
  })();
  capture.pendingBodies.add(bodyPromise);
  void bodyPromise.finally(() => capture.pendingBodies.delete(bodyPromise));
}

function captureRemoteObject(value) {
  if (!value || typeof value !== 'object') return value ?? null;
  if (Object.prototype.hasOwnProperty.call(value, 'value')) return value.value;
  if (value.unserializableValue !== undefined) return value.unserializableValue;
  const preview = truncateCaptureText(value.description || value.className || value.type || '', 4096);
  return preview.value;
}

function handleConsoleCaptureEvent(tabId, method, params = {}) {
  const capture = consoleCaptures.get(tabId);
  if (!capture?.active) return;
  let message = null;
  if (method === 'Runtime.consoleAPICalled') {
    const args = (params.args || []).map(captureRemoteObject);
    message = {
      type: params.type || 'log',
      timestamp: params.timestamp,
      args,
      text: truncateCaptureText(args.map(value => String(value)).join(' '), 8192).value,
      execution_context_id: params.executionContextId,
      stack_trace: params.stackTrace || null,
    };
  } else if (method === 'Runtime.exceptionThrown') {
    const details = params.exceptionDetails || {};
    message = {
      type: 'exception',
      timestamp: params.timestamp,
      text: truncateCaptureText(
        details.exception?.description || details.text || 'Uncaught exception', 8192,
      ).value,
      url: details.url || '',
      line_number: details.lineNumber,
      column_number: details.columnNumber,
      stack_trace: details.stackTrace || null,
    };
  }
  if (!message) return;
  if (capture.messages.length >= capture.maxEntries) {
    capture.messages.shift();
    capture.dropped += 1;
  }
  capture.messages.push(message);
}

async function handleNetworkCaptureCommand(msg, sender) {
  const tabId = Number(msg.tabId || sender.tab?.id);
  if (!Number.isInteger(tabId)) {
    return { ok: false, code: 'invalid_tab_id', error: 'network_capture requires tabId' };
  }
  if (msg.method === 'start') {
    const existing = networkCaptures.get(tabId);
    if (existing?.active) {
      return { ok: true, data: { ...networkCaptureSnapshot(existing, false), already_running: true } };
    }
    if (existing) networkCaptures.delete(tabId);
    let debuggerLease = null;
    const capture = {
      tabId,
      debuggerLease: null,
      active: true,
      startedAt: Date.now(),
      stoppedAt: null,
      maxEntries: boundedCaptureInteger(msg.maxEntries, 500, 10, 2000),
      maxBodyBytes: boundedCaptureInteger(msg.maxBodyBytes, 262144, 1024, 2097152),
      bodyTimeoutMs: boundedCaptureInteger(msg.bodyTimeoutMs, 5000, 100, 10000),
      includeBodies: msg.includeBodies !== false,
      entries: [],
      byRequestId: new Map(),
      pendingBodies: new Set(),
      dropped: 0,
      bodyErrors: 0,
      lastError: null,
    };
    try {
      debuggerLease = await attachAbmDebugger({ tabId });
      capture.debuggerLease = debuggerLease;
      networkCaptures.set(tabId, capture);
      await sendDebuggerCommandWithTimeout(debuggerLease, 'Network.enable', {
        maxTotalBufferSize: 10000000,
        maxResourceBufferSize: 2000000,
        maxPostDataSize: Math.min(capture.maxBodyBytes, 65536),
      }, boundedCdpTimeout(msg.timeoutMs, 10000));
      return { ok: true, data: networkCaptureSnapshot(capture, false) };
    } catch (error) {
      if (networkCaptures.get(tabId) === capture) networkCaptures.delete(tabId);
      if (debuggerLease) try { await detachAbmDebugger(debuggerLease); } catch (_) {}
      return {
        ok: false,
        code: debuggerFailureCode(error),
        error: error.message || String(error),
      };
    }
  }
  if (msg.method === 'stop') {
    const capture = networkCaptures.get(tabId);
    if (!capture) return { ok: true, data: { status: 'not_running', tab_id: tabId, requests: [] } };
    capture.active = false;
    capture.stoppedAt ||= Date.now();
    const lease = capture.debuggerLease;
    capture.debuggerLease = null;
    // Remove it before an intentional final detach can emit onDetach. That
    // event invalidates captures still present in the map; a normal stop must
    // not be mislabeled as an unexpected "debugger detached" failure.
    networkCaptures.delete(tabId);
    // Release this capture's lease before waiting for response bodies. That
    // synchronously rejects only commands owned by this lease and clears their
    // watchdogs, so a late body timeout cannot invalidate another capture that
    // is sharing the same debugger attachment (for example Console capture).
    if (lease) try { await detachAbmDebugger(lease); } catch (_) {}
    if (capture.pendingBodies.size) {
      await Promise.allSettled([...capture.pendingBodies]);
    }
    return { ok: true, data: networkCaptureSnapshot(capture, true) };
  }
  return { ok: false, code: 'unknown_method', error: `Unknown network_capture method: ${msg.method}` };
}

async function handleConsoleCaptureCommand(msg, sender) {
  const tabId = Number(msg.tabId || sender.tab?.id);
  if (!Number.isInteger(tabId)) {
    return { ok: false, code: 'invalid_tab_id', error: 'console requires tabId' };
  }
  if (msg.method === 'start') {
    const existing = consoleCaptures.get(tabId);
    if (existing?.active) {
      return { ok: true, data: { ...consoleCaptureSnapshot(existing, false), already_running: true } };
    }
    if (existing) consoleCaptures.delete(tabId);
    let debuggerLease = null;
    const capture = {
      tabId,
      debuggerLease: null,
      active: true,
      startedAt: Date.now(),
      stoppedAt: null,
      maxEntries: boundedCaptureInteger(msg.maxEntries, 500, 10, 5000),
      messages: [],
      dropped: 0,
      lastError: null,
    };
    try {
      debuggerLease = await attachAbmDebugger({ tabId });
      capture.debuggerLease = debuggerLease;
      consoleCaptures.set(tabId, capture);
      await sendDebuggerCommandWithTimeout(
        debuggerLease, 'Runtime.enable', {}, boundedCdpTimeout(msg.timeoutMs, 10000),
      );
      return { ok: true, data: consoleCaptureSnapshot(capture, false) };
    } catch (error) {
      if (consoleCaptures.get(tabId) === capture) consoleCaptures.delete(tabId);
      if (debuggerLease) try { await detachAbmDebugger(debuggerLease); } catch (_) {}
      return {
        ok: false,
        code: debuggerFailureCode(error),
        error: error.message || String(error),
      };
    }
  }
  const capture = consoleCaptures.get(tabId);
  if (msg.method === 'get') {
    if (!capture) {
      return { ok: true, data: { status: 'not_running', tab_id: tabId, messages: [] } };
    }
    const offset = boundedCaptureInteger(msg.offset, 0, 0, capture.messages.length);
    const maxItems = boundedCaptureInteger(msg.maxItems, 200, 1, 1000);
    const total = capture.messages.length;
    const messages = capture.messages.slice(offset, offset + maxItems);
    const data = {
      ...consoleCaptureSnapshot(capture, false),
      messages,
      offset,
      total,
      next_offset: offset + messages.length < total ? offset + messages.length : null,
      truncated: offset + messages.length < total,
    };
    if (msg.clear === true) {
      data.cleared = capture.messages.length;
      capture.messages.splice(0, capture.messages.length);
    }
    return { ok: true, data };
  }
  if (msg.method === 'stop') {
    if (!capture) return { ok: true, data: { status: 'not_running', tab_id: tabId, messages: [] } };
    capture.active = false;
    capture.stoppedAt ||= Date.now();
    consoleCaptures.delete(tabId);
    const lease = capture.debuggerLease;
    capture.debuggerLease = null;
    if (lease) try { await detachAbmDebugger(lease); } catch (_) {}
    return { ok: true, data: consoleCaptureSnapshot(capture, true) };
  }
  return { ok: false, code: 'unknown_method', error: `Unknown console method: ${msg.method}` };
}

function handleDebuggerEvent(source, method, params) {
  const tabId = source.tabId;
  if (!tabId || !dialogAttachedTabs.has(tabId)) return;
  if (typeof handleNetworkCaptureEvent === 'function') {
    handleNetworkCaptureEvent(tabId, method, params);
  }
  if (typeof handleConsoleCaptureEvent === 'function') {
    handleConsoleCaptureEvent(tabId, method, params);
  }
  if (method === 'Runtime.executionContextCreated') {
    rememberRuntimeExecutionContext(tabId, params?.context);
    return;
  }
  if (method === 'Runtime.executionContextDestroyed') {
    const pending = pendingManualExecutions.get(tabId);
    if (pending?.evaluationStarted &&
        pending.executionContextId === params?.executionContextId) {
      void cancelManualExecution(tabId, 'manual execution context was destroyed');
    }
    forgetRuntimeExecutionContext(tabId, params?.executionContextId);
    return;
  }
  if (method === 'Runtime.executionContextsCleared') {
    runtimeExecutionContexts.delete(tabId);
    const pending = pendingManualExecutions.get(tabId);
    if (pending?.evaluationStarted) {
      void cancelManualExecution(tabId, 'manual execution contexts were cleared');
    }
    return;
  }
  if (method === 'Page.frameNavigated') {
    if (params?.frame && !params.frame.parentId) {
      const pending = pendingManualExecutions.get(tabId);
      if (pending?.evaluationStarted) {
        void cancelManualExecution(tabId, 'manual tab navigated');
      }
    }
    return;
  }
  if (method === 'Page.javascriptDialogClosed') {
    protocolDialogStates.delete(tabId);
    return;
  }
  if (method !== 'Page.javascriptDialogOpening') return;
  const eventSequence = (dialogEventSequences.get(tabId) || 0) + 1;
  dialogEventSequences.set(tabId, eventSequence);
  const dialog = rememberProtocolDialog(tabId, params || {});
  const manualPending = pendingManualExecutions.get(tabId);
  if (manualPending &&
      pendingManualExecutions.get(tabId) === manualPending &&
      manualExecutionGenerations.get(tabId) === manualPending.generation &&
      manualPending.state === 'armed' && !manualPending.released &&
      !manualPending.dialog) {
    if (Date.now() > manualPending.deadline) {
      void cancelManualExecution(
        tabId, 'manual dialog ownership expired before opening', manualPending,
      );
    } else if (eventSequence >= manualPending.eventFloor) {
      manualPending.dialog = dialog;
      manualPending.state = 'dialog';
      if (manualPending.expiryTimer) clearTimeout(manualPending.expiryTimer);
      manualPending.resolveDialog({ kind: 'dialog', dialog });
    }
  }
  const navigationPending = pendingNavigations.get(tabId);
  if (!navigationPending) return;
  navigationPending.dialog = dialog;
  if (navigationPending.action === 'manual') {
    navigationPending.manualOwned = true;
    scheduleManualNavigationRelease(navigationPending);
  }
  if (navigationPending.action !== 'manual') {
    navigationPending.handlePromise = sendDebuggerCommandWithTimeout(
      navigationPending.debuggerLease,
      'Page.handleJavaScriptDialog',
      { accept: navigationPending.action === 'accept' },
      3000,
    ).then(() => { navigationPending.handled = true; }).catch((error) => {
      navigationPending.handleError = error.message || String(error);
    });
  }
  navigationPending.resolve(dialog);
}

chrome.debugger.onEvent.addListener(handleDebuggerEvent);

chrome.debugger.onDetach.addListener((source) => {
  if (!source.tabId) return;
  const key = debuggerTargetKey(source);
  const attachment = debuggerAttachments.get(key);
  if (attachment) {
    attachment.attached = false;
    attachment.invalidated = true;
    attachment.invalidationReason = 'debugger detached';
    attachment.refs = 0;
    rejectPendingDebuggerCommands(attachment, 'debugger detached');
    if (debuggerAttachments.get(key) === attachment) {
      debuggerAttachments.delete(key);
    }
  }
  dialogAttachedTabs.delete(source.tabId);
  protocolDialogStates.delete(source.tabId);
  const navigationPending = pendingNavigations.get(source.tabId) || null;
  dialogEventSequences.delete(source.tabId);
  manualExecutionGenerations.delete(source.tabId);
  runtimeExecutionContexts.delete(source.tabId);
  if (typeof markCaptureTabInvalidated === 'function') {
    markCaptureTabInvalidated(source.tabId, 'debugger detached');
  }
  if (navigationPending) {
    void cancelNavigationPending(
      source.tabId, 'debugger detached', navigationPending,
    );
  }
  void cancelManualExecution(source.tabId, 'debugger detached');
});

chrome.tabs.onRemoved.addListener((tabId) => {
  const attachment = debuggerAttachments.get(`tab:${tabId}`);
  if (attachment) {
    attachment.invalidationReason = 'tab removed';
    attachment.refs = 0;
    rejectPendingDebuggerCommands(attachment, 'tab removed');
    debuggerAttachments.delete(`tab:${tabId}`);
  }
  dialogAttachedTabs.delete(tabId);
  protocolDialogStates.delete(tabId);
  const navigationPending = pendingNavigations.get(tabId) || null;
  dialogEventSequences.delete(tabId);
  manualExecutionGenerations.delete(tabId);
  execDialogPolicies.delete(tabId);
  runtimeExecutionContexts.delete(tabId);
  if (typeof networkCaptures !== 'undefined') networkCaptures.delete(tabId);
  if (typeof consoleCaptures !== 'undefined') consoleCaptures.delete(tabId);
  if (navigationPending) {
    void cancelNavigationPending(tabId, 'tab removed', navigationPending);
  }
  void cancelManualExecution(tabId, 'tab removed');
});

function currentExecDialogPolicy(tabId, token) {
  const scopes = execDialogPolicies.get(tabId);
  if (!scopes) return null;
  const now = Date.now();
  for (const [token, entry] of scopes) {
    if (entry.expiresAt <= now) scopes.delete(token);
  }
  if (!scopes.size) {
    execDialogPolicies.delete(tabId);
    return null;
  }
  return token ? (scopes.get(token) || null) : null;
}

function claimManualExecDialogPolicy(tabId, source) {
  if (typeof source !== 'string') return null;
  const scopes = execDialogPolicies.get(tabId);
  if (!scopes) return null;
  const now = Date.now();
  for (const [token, entry] of scopes) {
    if (entry.expiresAt <= now) {
      scopes.delete(token);
      continue;
    }
    if (entry.policy === 'manual' && !entry.claimed && entry.source === source) {
      entry.claimed = true;
      return entry;
    }
  }
  if (!scopes.size) execDialogPolicies.delete(tabId);
  return null;
}

function debuggerTargetKey(target) {
  if (target.tabId) return `tab:${target.tabId}`;
  if (target.targetId) return `target:${target.targetId}`;
  if (target.extensionId) return `extension:${target.extensionId}`;
  throw new Error('debugger target is missing an identifier');
}

function boundedCdpTimeout(value, fallback = 20000, minimum = 100) {
  const requested = Number(value);
  const lowerBound = Math.max(1, Math.floor(Number(minimum) || 100));
  return Number.isFinite(requested) && requested > 0
    ? Math.max(lowerBound, Math.min(Math.floor(requested), 120000))
    : fallback;
}

function debuggerFailureCode(error) {
  const message = String(error?.message || error || '').toLowerCase();
  if (error?.code === 'cdp_timeout' || message.includes('cdp_timeout')) return 'cdp_timeout';
  if (message.includes('another debugger') || message.includes('already attached')) {
    return 'debugger_conflict';
  }
  if (message.includes('detached') || message.includes('not attached')) return 'debugger_detached';
  return 'cdp_error';
}

function clearDebuggerTabState(tabId, reason) {
  if (!tabId) return;
  dialogAttachedTabs.delete(tabId);
  protocolDialogStates.delete(tabId);
  dialogEventSequences.delete(tabId);
  runtimeExecutionContexts.delete(tabId);
  execDialogPolicies.delete(tabId);
  if (typeof markCaptureTabInvalidated === 'function') {
    markCaptureTabInvalidated(tabId, reason);
  }
  if (typeof cancelNavigationPending === 'function') {
    void cancelNavigationPending(tabId, reason);
  }
  if (typeof cancelManualExecution === 'function') {
    void cancelManualExecution(tabId, reason);
  }
}

function rejectPendingDebuggerCommands(attachment, reason, excludedCommand = null) {
  if (!attachment?.pendingCommands) return;
  for (const command of [...attachment.pendingCommands]) {
    if (command === excludedCommand || command.settled) continue;
    command.settled = true;
    if (command.timer !== null) {
      clearTimeout(command.timer);
      command.timer = null;
    }
    attachment.pendingCommands.delete(command);
    const error = new Error(`debugger_detached: ${reason || 'attachment released'}`);
    error.code = 'debugger_detached';
    command.reject(error);
  }
}

function rejectPendingDebuggerCommandsForLease(attachment, lease, reason) {
  if (!attachment?.pendingCommands || !lease) return;
  for (const command of [...attachment.pendingCommands]) {
    if (command.lease !== lease || command.settled) continue;
    command.settled = true;
    if (command.timer !== null) {
      clearTimeout(command.timer);
      command.timer = null;
    }
    attachment.pendingCommands.delete(command);
    const error = new Error(`debugger_detached: ${reason || 'lease released'}`);
    error.code = 'debugger_detached';
    command.reject(error);
  }
}

async function attachAbmDebugger(target) {
  const key = debuggerTargetKey(target);
  const activeRecovery = debuggerRecoveryPromises.get(key);
  if (activeRecovery) {
    await activeRecovery;
    return await attachAbmDebugger(target);
  }
  let attachment = debuggerAttachments.get(key);
  if (attachment?.detachingPromise) {
    try { await attachment.detachingPromise; } catch (_) {}
    return await attachAbmDebugger(target);
  }
  if (!attachment) {
    attachment = {
      key,
      target: { ...target },
      refs: 0,
      attached: false,
      invalidated: false,
      invalidationReason: null,
      generation: `${Date.now()}-${Math.random()}`,
      detachingPromise: null,
      attachPromise: null,
      pendingCommands: new Set(),
    };
    debuggerAttachments.set(key, attachment);
    const rawAttach = () => chrome.debugger.attach(attachment.target, '1.3');
    attachment.attachPromise = Promise.resolve()
      .then(rawAttach)
      .catch(async (error) => {
        if (debuggerFailureCode(error) !== 'debugger_conflict') throw error;
        // A service-worker restart can lose our in-memory lease map while
        // Chrome still considers this extension attached. Detach is scoped to
        // this extension; it cannot take ownership from a different extension.
        const waitingRefs = attachment.refs;
        const recovery = (async () => {
          attachment.attached = true;
          try {
            await detachAbmDebugger({ attachment, released: false }, true);
          } catch (_) {
            // If DevTools or another extension owns the target, our detach is
            // rejected as "not attached". Preserve the original conflict so
            // callers get the correct close-the-competing-debugger guidance.
            throw error;
          }
          attachment.invalidated = false;
          attachment.invalidationReason = null;
          attachment.detachingPromise = null;
          // Every caller awaiting this shared attachPromise already owns one
          // reference. force-detach clears refs while recovering Chrome's stale
          // attachment, so restore them before those leases begin issuing work.
          attachment.refs = waitingRefs;
          debuggerAttachments.set(key, attachment);
          await rawAttach();
        })();
        debuggerRecoveryPromises.set(key, recovery);
        try {
          await recovery;
        } finally {
          if (debuggerRecoveryPromises.get(key) === recovery) {
            debuggerRecoveryPromises.delete(key);
          }
        }
      })
      .then(() => {
        attachment.attached = true;
        if (attachment.target.tabId) dialogAttachedTabs.add(attachment.target.tabId);
      });
  }
  attachment.refs += 1;
  const lease = { attachment, generation: attachment.generation, released: false };
  try {
    await attachment.attachPromise;
    return lease;
  } catch (error) {
    lease.released = true;
    attachment.refs = Math.max(0, attachment.refs - 1);
    if (!attachment.refs && debuggerAttachments.get(key) === attachment) {
      debuggerAttachments.delete(key);
    }
    throw error;
  }
}

async function detachAbmDebugger(lease, force = false, excludedCommand = null) {
  if (!lease?.attachment || lease.released) return;
  lease.released = true;
  const attachment = lease.attachment;
  // Commands belong to the lease that started them. Releasing Network capture
  // must cancel only its getResponseBody calls, not invalidate a shared Console
  // lease later when an old watchdog fires.
  rejectPendingDebuggerCommandsForLease(attachment, lease, 'owning lease released');
  if (!force && (attachment.invalidated || lease.generation !== attachment.generation)) return;
  attachment.refs = force ? 0 : Math.max(0, attachment.refs - 1);
  if (attachment.refs || attachment.detachingPromise) return;
  rejectPendingDebuggerCommands(
    attachment,
    attachment.invalidationReason || 'attachment released',
    excludedCommand,
  );
  attachment.detachingPromise = (async () => {
    try {
      if (attachment.attached) {
        await Promise.race([
          chrome.debugger.detach(attachment.target),
          new Promise(resolve => setTimeout(resolve, 1000)),
        ]);
      }
    } finally {
      attachment.attached = false;
      if (debuggerAttachments.get(attachment.key) === attachment) {
        debuggerAttachments.delete(attachment.key);
        if (attachment.target.tabId) dialogAttachedTabs.delete(attachment.target.tabId);
      }
      if (attachment.target.tabId) {
        protocolDialogStates.delete(attachment.target.tabId);
      }
    }
  })();
  await attachment.detachingPromise;
}

async function forceInvalidateDebuggerAttachment(
  attachment, reason, triggeringCommand = null,
) {
  if (!attachment) return;
  if (attachment.invalidatingPromise) return await attachment.invalidatingPromise;
  attachment.invalidated = true;
  attachment.invalidationReason = reason;
  attachment.refs = 0;
  if (debuggerAttachments.get(attachment.key) === attachment) {
    debuggerAttachments.delete(attachment.key);
  }
  rejectPendingDebuggerCommands(attachment, reason, triggeringCommand);
  if (attachment.target.tabId) clearDebuggerTabState(attachment.target.tabId, reason);
  attachment.invalidatingPromise = detachAbmDebugger(
    { attachment, generation: attachment.generation, released: false },
    true,
    triggeringCommand,
  ).catch(() => {});
  await attachment.invalidatingPromise;
}

async function sendDebuggerCommandWithTimeout(
  lease, method, params = {}, timeoutMs = 20000, minimumTimeoutMs = 100,
) {
  if (!lease?.attachment || lease.released || lease.attachment.invalidated ||
      lease.generation !== lease.attachment.generation) {
    const error = new Error('debugger_detached: attachment lease is no longer valid');
    error.code = 'debugger_detached';
    throw error;
  }
  const attachment = lease.attachment;
  const bounded = boundedCdpTimeout(timeoutMs, 20000, minimumTimeoutMs);
  const commandState = {
    lease,
    timer: null,
    reject: null,
    settled: false,
  };
  attachment.pendingCommands ||= new Set();
  attachment.pendingCommands.add(commandState);
  const command = Promise.resolve().then(
    () => chrome.debugger.sendCommand(attachment.target, method, params || {}),
  );
  const watchdog = new Promise((_, reject) => {
    commandState.reject = reject;
    commandState.timer = setTimeout(async () => {
      if (commandState.settled) return;
      const reason = `cdp_timeout: ${method} exceeded ${bounded}ms`;
      await forceInvalidateDebuggerAttachment(attachment, reason, commandState);
      if (commandState.settled) return;
      const error = new Error(reason);
      error.code = 'cdp_timeout';
      error.method = method;
      error.timeoutMs = bounded;
      reject(error);
    }, bounded);
  });
  try {
    return await Promise.race([command, watchdog]);
  } catch (error) {
    if (debuggerFailureCode(error) === 'debugger_conflict') {
      await forceInvalidateDebuggerAttachment(
        attachment, `debugger_conflict: ${error.message || String(error)}`,
        commandState,
      );
    }
    throw error;
  } finally {
    commandState.settled = true;
    if (commandState.timer !== null) clearTimeout(commandState.timer);
    commandState.timer = null;
    attachment.pendingCommands.delete(commandState);
  }
}

async function handleProtocolDialog(msg) {
  const tabId = Number(msg.tabId);
  const action = msg.action;
  if (!Number.isInteger(tabId)) return { ok: false, error: 'handle_dialog requires tabId' };
  if (!validDialogPolicy(action)) return { ok: false, error: 'invalid dialog action' };
  let debuggerLease = null;
  let borrowedNavigationLease = false;
  let captured = null;
  const owningManual = pendingManualExecutions.get(tabId) || null;
  const owningNavigation = pendingNavigations.get(tabId) || null;
  try {
    // A manual navigation deliberately retains the exact lease that issued
    // Page.navigate while its beforeunload dialog is open.  Re-attaching a
    // second logical lease here works in the JS harness but real Chrome can
    // reject Page.handleJavaScriptDialog with "Detached while handling
    // command" while that navigation command is still paused.  Route the
    // explicit accept/dismiss through the owning lease instead; it already has
    // Page enabled and is released only after navigation + handledSignal settle.
    if (owningNavigation?.action === 'manual' && owningNavigation.dialog &&
        owningNavigation.debuggerLease && !owningNavigation.released) {
      debuggerLease = owningNavigation.debuggerLease;
      borrowedNavigationLease = true;
    } else {
      debuggerLease = await attachAbmDebugger({ tabId });
      await sendDebuggerCommandWithTimeout(
        debuggerLease, 'Page.enable', {}, boundedCdpTimeout(msg.timeoutMs, 2500),
      );
    }
    await new Promise(resolve => setTimeout(resolve, 50));
    captured = currentProtocolDialog(tabId) || owningManual?.dialog ||
      owningNavigation?.dialog || null;
    if (action === 'manual') {
      return { ok: true, data: {
        status: captured ? 'blocked_by_dialog' : 'no_dialog',
        dialog: captured,
        dialog_action: action,
        dialog_observed: Boolean(captured),
        handled: false,
        pending_execution: Boolean(owningManual || owningNavigation),
      } };
    }
    const params = {
      accept: action === 'accept',
      ...(msg.promptText !== undefined ? { promptText: msg.promptText } : {}),
    };
    await sendDebuggerCommandWithTimeout(
      debuggerLease, 'Page.handleJavaScriptDialog', params,
      boundedCdpTimeout(msg.timeoutMs, 2500),
    );
    if (owningNavigation?.action === 'manual' && !owningNavigation.handled &&
        owningNavigation.dialog) {
      owningNavigation.handled = true;
      if (owningNavigation.resolveHandled) owningNavigation.resolveHandled({
        kind: 'handled', action,
      });
      scheduleManualNavigationRelease(owningNavigation);
    }
    return { ok: true, data: {
      status: 'ok', handled: true, dialog: captured, dialog_action: action,
      dialog_observed: Boolean(captured),
      pending_execution: pendingManualExecutions.has(tabId) ||
        pendingNavigations.has(tabId),
    } };
  } catch (error) {
    return { ok: false, error: error.message || String(error), dialog: captured };
  } finally {
    if (debuggerLease && !borrowedNavigationLease) {
      try { await detachAbmDebugger(debuggerLease); } catch (_) {}
    }
  }
}

function classifyNavigationOutcome({
  firstKind, navigationKind, dialog, action, handleError,
}) {
  if (handleError) return 'dialog_handle_failed';
  if (dialog && action === 'manual') return 'blocked_by_dialog';
  if (dialog?.type === 'beforeunload' && action === 'dismiss') {
    return 'blocked_by_beforeunload';
  }
  if (firstKind === 'error' || navigationKind === 'error') return 'navigation_failed';
  if (firstKind === 'timeout' || navigationKind === 'timeout') return 'navigation_timeout';
  return 'ok';
}

async function releaseNavigationPending(pending) {
  if (!pending) return;
  if (pending.releasePromise) return await pending.releasePromise;
  pending.released = true;
  if (pending.releaseTimer) {
    clearTimeout(pending.releaseTimer);
    pending.releaseTimer = null;
  }
  if (pendingNavigations.get(pending.tabId) === pending) {
    pendingNavigations.delete(pending.tabId);
  }
  const lease = pending.debuggerLease;
  pending.debuggerLease = null;
  pending.releasePromise = (async () => {
    if (lease) {
      try { await detachAbmDebugger(lease); } catch (_) {}
    }
  })();
  await pending.releasePromise;
}

function scheduleManualNavigationRelease(pending) {
  if (!pending || pending.releasePromise || pending.cleanupPromise ||
      !pending.navigationPromise || !pending.handledSignal) return;
  pending.cleanupPromise = Promise.all([
    pending.navigationPromise,
    pending.handledSignal,
  ]).then(() => releaseNavigationPending(pending));
}

async function cancelNavigationPending(tabId, reason, expectedPending = null) {
  const pending = pendingNavigations.get(tabId);
  if (!pending || (expectedPending && pending !== expectedPending)) return false;
  pending.cancelReason = reason;
  if (pending.resolveCancel) pending.resolveCancel({ kind: 'cancelled', reason });
  await releaseNavigationPending(pending);
  return true;
}

async function navigateWithDialogPolicy(msg) {
  const tabId = Number(msg.tabId);
  const action = msg.beforeunload;
  if (!Number.isInteger(tabId)) return { ok: false, error: 'navigate requires tabId' };
  if (!validDialogPolicy(action)) return { ok: false, error: 'invalid beforeunload action' };
  let debuggerLease = null;
  let pending = null;
  try {
    const beforeTab = await chrome.tabs.get(tabId);
    debuggerLease = await attachAbmDebugger({ tabId });
    await sendDebuggerCommandWithTimeout(
      debuggerLease, 'Page.enable', {}, boundedCdpTimeout(msg.timeoutMs),
    );
    let resolveDialog;
    const dialogPromise = new Promise(resolve => { resolveDialog = resolve; });
    let resolveHandled;
    let resolveCancel;
    pending = {
      tabId,
      action,
      dialog: null,
      resolve: resolveDialog,
      handlePromise: null,
      handled: false,
      manualOwned: false,
      debuggerLease,
      released: false,
      resolveHandled,
      resolveCancel,
    };
    pending.handledSignal = new Promise(resolve => { resolveHandled = resolve; });
    pending.resolveHandled = resolveHandled;
    pending.cancelSignal = new Promise(resolve => { resolveCancel = resolve; });
    pending.resolveCancel = resolveCancel;
    pendingNavigations.set(tabId, pending);
    const navigationPromise = sendDebuggerCommandWithTimeout(
      debuggerLease, 'Page.navigate', { url: msg.url },
      boundedCdpTimeout(msg.timeoutMs),
    ).then(value => ({ kind: 'navigation', value })).catch(error => ({
      kind: 'error', error: error.message || String(error),
    }));
    pending.navigationPromise = navigationPromise;
    const waitMs = Math.max(100, Math.min(Number(msg.timeoutMs) || 15000, 30000));
    let first = await Promise.race([
      navigationPromise,
      dialogPromise.then(dialog => ({ kind: 'dialog', dialog })),
      pending.cancelSignal,
      new Promise(resolve => setTimeout(() => resolve({ kind: 'timeout' }), waitMs)),
    ]);
    // Page.navigate can resolve just before the dialog event is delivered.
    if (first.kind === 'navigation' && !pending.dialog) {
      first = await Promise.race([
        dialogPromise.then(dialog => ({ kind: 'dialog', dialog })),
        new Promise(resolve => setTimeout(() => resolve(first), 250)),
      ]);
    }
    const dialog = pending.dialog || (first.kind === 'dialog' ? first.dialog : null);
    if (dialog && action === 'manual') {
      pending.manualOwned = true;
      scheduleManualNavigationRelease(pending);
    }
    if (pending.handlePromise) await pending.handlePromise;
    let navigation = first.kind === 'navigation' ? first.value : null;
    let navigationKind = first.kind;
    let navigationError = first.kind === 'error' ? first.error : null;
    if (dialog && action === 'accept') {
      const completed = await Promise.race([
        navigationPromise,
        new Promise(resolve => setTimeout(() => resolve({ kind: 'timeout' }), 3000)),
      ]);
      navigationKind = completed.kind;
      navigationError = completed.kind === 'error' ? completed.error : null;
      if (completed.kind === 'navigation') navigation = completed.value;
    }
    let tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!dialog || action === 'accept') {
      const settleUntil = Date.now() + 3000;
      while (tab && Date.now() < settleUntil) {
        const currentUrl = tab.pendingUrl || tab.url || '';
        if (currentUrl && (currentUrl !== beforeTab.url || currentUrl === msg.url)) break;
        await new Promise(resolve => setTimeout(resolve, 100));
        tab = await chrome.tabs.get(tabId).catch(() => tab);
      }
    }
    const isDownload = Boolean(navigation?.isDownload);
    const status = isDownload ? 'triggered' : classifyNavigationOutcome({
      firstKind: first.kind,
      navigationKind,
      dialog,
      action,
      handleError: pending.handleError,
    });
    return {
      ok: true,
      data: {
        status,
        ...(isDownload ? {
          type: 'download',
          is_download: true,
          hint: 'The browser accepted this as a download. ERR_ABORTED is normal for a download navigation; use download_file when the final local path is required.',
        } : {}),
        dialog,
        dialog_action: dialog ? action : undefined,
        dialog_observed: Boolean(dialog),
        handled: Boolean(pending.handled),
        handle_error: pending.handleError,
        navigation,
        navigation_error: navigationError,
        url: status === 'blocked_by_beforeunload'
          ? (tab?.url || msg.url)
          : (tab?.pendingUrl || tab?.url || msg.url),
        title: tab?.title || '',
        pending_execution: Boolean(
          dialog && action === 'manual' && !pending.released,
        ),
      },
    };
  } catch (error) {
    return { ok: false, error: error.message || String(error), dialog: pending?.dialog || null };
  } finally {
    const retainManualOwner = Boolean(
      pending && pending.action === 'manual' && pending.manualOwned &&
      !pending.released && pendingNavigations.get(tabId) === pending
    );
    if (!retainManualOwner) {
      if (pending) await releaseNavigationPending(pending);
      else if (debuggerLease) {
        try { await detachAbmDebugger(debuggerLease); } catch (_) {}
      }
    } else {
      debuggerLease = null;
    }
  }
}

function boundedDownloadTimeout(value, fallback = 60000) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return fallback;
  return Math.max(1, Math.min(Math.floor(parsed), 30 * 60 * 1000));
}

function downloadItemResult(item, fallbackStatus = 'in_progress') {
  const terminalFallback = fallbackStatus === 'complete' || fallbackStatus === 'interrupted';
  // onChanged is authoritative for a terminal transition. downloads.search()
  // can briefly return the previous in_progress snapshot after that event.
  const state = terminalFallback ? fallbackStatus : (item?.state || fallbackStatus);
  const status = state === 'complete'
    ? 'completed' : state === 'interrupted' ? 'failed' : 'in_progress';
  const result = {
    status,
    download_id: item?.id,
    bytes_received: Number(item?.bytesReceived) || 0,
    total_bytes: Number(item?.totalBytes) || 0,
  };
  if (status === 'completed' && item?.filename) result.path = item.filename;
  if (status === 'failed') {
    result.error = item?.error || 'download interrupted';
    result.hint = 'The browser interrupted the download; inspect the error and retry download_file if appropriate.';
  }
  return result;
}

async function downloadSnapshot(downloadId, fallbackStatus = 'in_progress') {
  const matches = await chrome.downloads.search({ id: downloadId });
  const item = matches.find(candidate => candidate.id === downloadId) || { id: downloadId };
  return downloadItemResult(item, fallbackStatus);
}

async function settledDownloadSnapshot(downloadId, fallbackStatus) {
  const attempts = fallbackStatus === 'complete' ? 5 : 1;
  let snapshot;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    snapshot = await downloadSnapshot(downloadId, fallbackStatus);
    if (fallbackStatus !== 'complete' || snapshot.path) {
      return snapshot;
    }
    if (attempt < attempts - 1) {
      // onChanged can precede the search index's final filename by a few ticks.
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  throw new Error('download completed but Chrome did not expose the final local path');
}

function downloadFailureResult(code, error, downloadId) {
  return {
    ok: true,
    data: {
      status: 'failed',
      code,
      error,
      ...(Number.isInteger(downloadId) ? { download_id: downloadId } : {}),
    },
  };
}

async function handleDownloadCommand(msg) {
  if (msg.method !== 'download') {
    return { ok: false, code: 'unknown_download_method', error: `Unknown downloads method: ${msg.method}` };
  }
  if (typeof msg.url !== 'string' || !/^https?:\/\//i.test(msg.url)) {
    return { ok: false, code: 'invalid_download_url', error: 'downloads.download requires an http(s) URL' };
  }
  const options = {
    url: msg.url,
    conflictAction: msg.conflictAction === 'uniquify' ? 'uniquify' : 'overwrite',
    saveAs: false,
  };
  if (typeof msg.filename === 'string' && msg.filename) options.filename = msg.filename;
  let downloadId;
  try {
    downloadId = await chrome.downloads.download(options);
    if (!Number.isInteger(downloadId)) {
      return { ok: false, code: 'download_not_started', error: 'The browser did not return a download id' };
    }
    if (msg.wait === false) {
      return { ok: true, data: await downloadSnapshot(downloadId) };
    }
    const timeoutMs = boundedDownloadTimeout(msg.timeoutMs);
    return await new Promise(resolve => {
      let settled = false;
      let timer = null;
      const cleanup = () => {
        if (timer !== null) clearTimeout(timer);
        chrome.downloads.onChanged.removeListener(onChanged);
      };
      const finish = async (fallbackStatus, fallbackError = null) => {
        if (settled) return;
        settled = true;
        cleanup();
        try {
          const snapshot = await settledDownloadSnapshot(downloadId, fallbackStatus);
          if (fallbackStatus === 'interrupted' && fallbackError) snapshot.error = fallbackError;
          resolve({ ok: true, data: snapshot });
        } catch (error) {
          resolve(downloadFailureResult(
            'download_status_failed', error.message || String(error), downloadId,
          ));
        }
      };
      const onChanged = delta => {
        if (delta.id !== downloadId) return;
        const state = delta.state?.current;
        if (state === 'complete' || state === 'interrupted' || delta.error?.current) {
          void finish(
            state === 'complete' ? 'complete' : 'interrupted',
            delta.error?.current || null,
          );
        }
      };
      timer = setTimeout(() => void finish('in_progress'), timeoutMs);
      chrome.downloads.onChanged.addListener(onChanged);
      // Reconcile immediately after registering the listener. A tiny download
      // can finish between downloads.download() and listener installation;
      // onChanged covers what follows, while this search covers that gap.
      void downloadSnapshot(downloadId).then(snapshot => {
        if (snapshot.status === 'completed') void finish('complete');
        else if (snapshot.status === 'failed') void finish('interrupted');
      }).catch(error => {
        if (settled) return;
        settled = true;
        cleanup();
        resolve(downloadFailureResult(
          'download_status_failed', error.message || String(error), downloadId,
        ));
      });
    });
  } catch (error) {
    return downloadFailureResult(
      'download_failed', error.message || String(error), downloadId,
    );
  }
}

async function createTabAck(msg) {
  const operationId = String(msg.operation_id || '').trim();
  if (!operationId) {
    return { ok: false, error: 'tabs.create requires operation_id' };
  }
  try {
    await loadCreateOperations();
  } catch (error) {
    return { ok: true, data: {
      status: 'unknown', operation_status: 'unknown', operation_id: operationId,
      may_have_created: false, retry_safe: false,
      error: error.message || String(error),
    } };
  }
  const inFlight = createOperationPromises.get(operationId);
  if (inFlight) return await inFlight;
  const existing = createOperations.get(operationId);
  if (existing && existing.status === 'completed') {
    return { ok: true, data: operationRecordData(existing) };
  }
  if (existing && existing.status === 'pending') {
    return { ok: true, data: operationRecordData(existing) };
  }
  const run = (async () => {
    const clientId = msg.client_id || msg.clientId
      || (typeof getClientId === 'function' ? await getClientId() : null);
    const pending = {
      operation_id: operationId,
      status: 'pending',
      url: msg.url || 'about:blank',
      client_id: clientId,
      created_at: Date.now(),
      tab_status: 'pending',
      load_ready: false,
    };
    createOperations.set(operationId, pending);
    // This write intentionally precedes chrome.tabs.create. If the worker is
    // evicted after create but before completion is saved, reconciliation sees
    // pending and returns unknown rather than opening a second tab.
    try {
      await persistCreateOperations();
    } catch (error) {
      createOperations.delete(operationId);
      return { ok: true, data: {
        status: 'unknown', operation_status: 'unknown', operation_id: operationId,
        may_have_created: false, retry_safe: false,
        error: error.message || String(error),
      } };
    }
    let tab = null;
    let generation = null;
    try {
      // Creation acknowledgement must not wait for the destination document.
      tab = await chrome.tabs.create({
        url: pending.url,
        active: msg.active !== false,
      });
      try { generation = await tabGenerationFor(tab.id); } catch (_) {}
      if (!generation) generation = await scheduleNewTabGeneration(tab.id);
      const currentUrl = tab.pendingUrl || tab.url || pending.url;
      const completed = {
        ...pending,
        status: 'completed',
        id: tab.id,
        generation,
        url: currentUrl,
        title: tab.title || '',
        windowId: tab.windowId,
        tab_status: tab.status || 'unknown',
        load_ready: Boolean(tab.status === 'complete' && currentUrl),
      };
      createOperations.set(operationId, completed);
      await persistCreateOperations();
      void sendTabsUpdate();
      return { ok: true, data: operationRecordData(completed) };
    } catch (error) {
      if (tab && tab.id !== undefined && tab.id !== null) {
        // The native side effect already happened. Any failure while
        // assigning generation or persisting the completion is ambiguous;
        // preserve pending so reconciliation can never issue a second create.
        const uncertain = {
          ...pending,
          id: tab.id,
          generation: generation || pending.generation || null,
          url: tab.pendingUrl || tab.url || pending.url,
          title: tab.title || '',
          windowId: tab.windowId,
          tab_status: tab.status || 'unknown',
          error: error.message || String(error),
        };
        createOperations.set(operationId, uncertain);
        try { await persistCreateOperations(); } catch (_) {}
        return { ok: true, data: operationRecordData(uncertain) };
      }
      const failed = {
        ...pending,
        status: 'not_found',
        tab_status: 'unknown',
        error: error.message || String(error),
      };
      createOperations.set(operationId, failed);
      try { await persistCreateOperations(); } catch (_) {
        return { ok: true, data: {
          status: 'unknown', operation_status: 'unknown', operation_id: operationId,
          may_have_created: false, retry_safe: false,
          error: failed.error,
        } };
      }
      return { ok: false, error: failed.error, data: {
        status: 'not_found', operation_status: 'not_found',
        operation_id: operationId, retry_safe: true,
      } };
    }
  })();
  createOperationPromises.set(operationId, run);
  try { return await run; }
  finally { createOperationPromises.delete(operationId); }
}

async function handleExtMessage(msg, sender) {
  if (msg.cmd === 'site_permission') {
    // Keep operation failures in a successful bridge envelope so the server
    // can return structured unsupported/error results instead of losing them
    // in TMWebDriver's generic extension exception path.
    if (msg.action === 'set') return { ok: true, data: await setSitePermission(msg) };
    if (msg.action === 'reset') return { ok: true, data: await resetSitePermissionLeases(msg) };
    return { ok: true, data: { ok: false, error: 'invalid site permission action' } };
  }
  if (msg.cmd === 'dialog_state') {
    const tabId = Number(msg.tabId);
    if (!Number.isInteger(tabId)) return { ok: false, error: 'dialog_state requires tabId' };
    return { ok: true, data: { dialog: currentProtocolDialog(tabId) } };
  }
  if (msg.cmd === 'handle_dialog') return await handleProtocolDialog(msg);
  if (msg.cmd === 'navigate') return await navigateWithDialogPolicy(msg);
  if (msg.cmd === 'set_dialog_policy') {
    const tabId = Number(msg.tabId);
    if (!Number.isInteger(tabId)) return { ok: false, error: 'set_dialog_policy requires tabId' };
    if (!validDialogPolicy(msg.policy)) return { ok: false, error: 'invalid dialog policy' };
    const token = `${Date.now()}-${nextDialogScope++}`;
    const requestedTimeout = Number(msg.timeoutMs);
    const timeoutMs = Number.isFinite(requestedTimeout) && requestedTimeout > 0
      ? Math.min(Math.floor(requestedTimeout), 120000) : 15000;
    const scopes = execDialogPolicies.get(tabId) || new Map();
    scopes.set(token, {
      token,
      policy: msg.policy,
      timeoutMs,
      expiresAt: Date.now() + 120000,
      source: msg.policy === 'manual' && typeof msg.source === 'string'
        ? msg.source : null,
      claimed: false,
    });
    execDialogPolicies.set(tabId, scopes);
    return { ok: true, data: { token } };
  }
  if (msg.cmd === 'clear_dialog_policy') {
    const tabId = Number(msg.tabId);
    const scopes = execDialogPolicies.get(tabId);
    if (scopes) {
      if (msg.token !== undefined) scopes.delete(String(msg.token));
      else scopes.clear();
      if (!scopes.size) execDialogPolicies.delete(tabId);
    }
    return { ok: true, data: { cleared: true } };
  }
  if (msg.cmd === 'cookies') return await handleCookies(msg, sender);
  if (msg.cmd === 'downloads') return await handleDownloadCommand(msg);
  if (msg.cmd === 'cdp') return await handleCDP(msg, sender);
  if (msg.cmd === 'batch') return await handleBatch(msg, sender);
  if (msg.cmd === 'tabs') {
    try {
      if (msg.method === 'switch') {
        const tab = await chrome.tabs.update(msg.tabId, { active: true });
        // A minimized window swallows the raise: the tab goes active, focused:true
        // reports success, and the page still isn't on screen — so screen-coordinate
        // clicks land on whatever is. Un-minimize first, then report what the
        // window actually ended up as so the caller can tell.
        const before = await chrome.windows.get(tab.windowId);
        const update = { focused: true };
        if (before.state === 'minimized') update.state = 'normal';
        await chrome.windows.update(tab.windowId, update);
        const after = await chrome.windows.get(tab.windowId);
        return {
          ok: true,
          windowId: tab.windowId,
          wasMinimized: before.state === 'minimized',
          windowState: after.state,
          onScreen: after.state !== 'minimized',
        };
      } else if (msg.method === 'close') {
        // Works on chrome-extension:// pages too, which never become sessions
        // and so can't be closed through any session-scoped path.
        const tabIds = Array.isArray(msg.tabId) ? msg.tabId : [msg.tabId];
        // chrome.tabs is the existence truth even for PDF/restricted pages
        // that never register a scriptable ABM session. Validate the whole
        // batch before removing anything so native-id reuse cannot misclose.
        return { ok: true, data: await closeTabsWithGenerations(
          tabIds, msg.expectedGenerations,
        ) };
      } else if (msg.method === 'create_status') {
        return await createTabStatus(msg);
      } else if (msg.method === 'create') {
        // Native tab creation — runs in the SW, so it needs NO existing tab.
        // This replaces the old GM_openInTab path (a Tampermonkey API that
        // does not exist in a plain extension, so open_new_tab always threw).
        return await createTabAck(msg);
      } else {
        // all:true also reveals chrome-extension:// pages. They never become
        // sessions (content scripts can't run there), but chrome.debugger CAN
        // attach to them, so CDP is the only way to drive an extension's own UI.
        const tabs = (await chrome.tabs.query({}))
          .filter(t => msg.all ? true : isScriptable(t.url));
        const data = tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId }));
        return { ok: true, data };
      }
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'debugger_targets') {
    // Ground truth for what CDP can actually attach to, including
    // service_worker/other targets that chrome.tabs.query never reports.
    try {
      const t = await chrome.debugger.getTargets();
      return { ok: true, data: t.map(x => ({ type: x.type, title: (x.title || '').slice(0, 40),
        url: (x.url || '').slice(0, 80), attached: x.attached, id: x.id, tabId: x.tabId,
        extensionId: x.extensionId })) };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'call_extension') {
    try {
      if (!msg.extId || typeof msg.extId !== 'string') {
        return { ok: false, code: 'invalid_extension_id', error: 'call_extension requires extId' };
      }
      if (msg.extId === chrome.runtime.id) {
        return {
          ok: false,
          code: 'self_message_unsupported',
          error: 'call_extension targets another installed extension, not the ABM bridge itself',
        };
      }
      const response = await chrome.runtime.sendMessage(msg.extId, msg.message);
      return { ok: true, data: response ?? null };
    } catch (e) {
      return {
        ok: false,
        code: 'extension_message_failed',
        error: e.message,
        hint: 'The target extension must be enabled and allow this ABM extension in externally_connectable.',
      };
    }
  }
  if (msg.cmd === 'management') {
    try {
      if (msg.method === 'list') {
        const all = await chrome.management.getAll();
        return { ok: true, data: all.map(e => ({ id: e.id, name: e.name, enabled: e.enabled, type: e.type, version: e.version })) };
      }
      if (msg.method === 'disable') {
        await chrome.management.setEnabled(msg.extId, false);
        return { ok: true };
      }
      if (msg.method === 'enable') {
        await chrome.management.setEnabled(msg.extId, true);
        return { ok: true };
      }
      if (msg.method === 'uninstall') {
        if (msg.extId === chrome.runtime.id) {
          return {
            ok: false,
            code: 'self_uninstall_unsupported',
            error: 'The ABM bridge cannot uninstall itself through its active response channel.',
          };
        }
        await chrome.management.uninstall(msg.extId, { showConfirmDialog: msg.showConfirmDialog !== false });
        return { ok: true };
      }
      return { ok: false, error: 'Unknown method: ' + msg.method };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'bookmarks') {
    try {
      if (msg.method === 'tree') {
        return { ok: true, data: await chrome.bookmarks.getTree() };
      }
      if (msg.method === 'create') {
        return { ok: true, data: await chrome.bookmarks.create(msg.node) };
      }
      if (msg.method === 'move') {
        return { ok: true, data: await chrome.bookmarks.move(msg.id, msg.destination) };
      }
      if (msg.method === 'update') {
        return { ok: true, data: await chrome.bookmarks.update(msg.id, msg.changes) };
      }
      if (msg.method === 'bulkUpdateTitles') {
        const updates = msg.updates || [];
        let updated = 0;
        const errors = [];
        for (const item of updates) {
          try {
            await chrome.bookmarks.update(item.id, { title: item.title });
            updated++;
          } catch (e) {
            errors.push({ id: item.id, error: e.message });
          }
        }
        return { ok: errors.length === 0, updated, errors };
      }
      if (msg.method === 'removeTree') {
        await chrome.bookmarks.removeTree(msg.id);
        return { ok: true };
      }
      if (msg.method === 'remove') {
        await chrome.bookmarks.remove(msg.id);
        return { ok: true };
      }
      if (msg.method === 'bulkRemove') {
        const ids = msg.ids || [];
        let removed = 0;
        const errors = [];
        for (const id of ids) {
          try {
            await chrome.bookmarks.remove(id);
            removed++;
          } catch (e) {
            errors.push({ id, error: e.message });
          }
        }
        return { ok: errors.length === 0, removed, errors };
      }
      if (msg.method === 'bulkOrganize') {
        const rootId = msg.rootId;
        const folderPaths = msg.folderPaths || [];
        const assignments = msg.assignments || [];
        const removeFolderIds = msg.removeFolderIds || [];
        const pathIds = {};

        async function childrenOf(parentId) {
          const nodes = await chrome.bookmarks.getChildren(parentId);
          return nodes.filter(n => !n.url);
        }

        async function ensureFolder(parentId, title) {
          const existing = (await childrenOf(parentId)).find(n => n.title === title);
          if (existing) return existing.id;
          const created = await chrome.bookmarks.create({ parentId, title });
          return created.id;
        }

        async function ensurePath(parts) {
          let parentId = rootId;
          const walked = [];
          for (const part of parts) {
            walked.push(part);
            const key = walked.join('\u0001');
            if (!pathIds[key]) pathIds[key] = await ensureFolder(parentId, part);
            parentId = pathIds[key];
          }
          return parentId;
        }

        for (const parts of folderPaths) await ensurePath(parts);

        let moved = 0;
        for (const item of assignments) {
          const parentId = await ensurePath(item.path);
          await chrome.bookmarks.move(item.id, { parentId });
          moved++;
        }

        function countUrls(node) {
          if (node.url) return 1;
          return (node.children || []).reduce((sum, child) => sum + countUrls(child), 0);
        }

        let removedFolders = 0;
        for (const id of removeFolderIds) {
          const nodes = await chrome.bookmarks.getSubTree(id).catch(() => []);
          if (!nodes.length) continue;
          if (countUrls(nodes[0]) === 0) {
            await chrome.bookmarks.removeTree(id);
            removedFolders++;
          }
        }
        return { ok: true, moved, removedFolders };
      }
      if (msg.method === 'bulkCreate') {
        const rootId = msg.rootId;
        const folderPaths = msg.folderPaths || [];
        const creates = msg.creates || [];
        const pathIds = {};

        async function childrenOf(parentId) {
          const nodes = await chrome.bookmarks.getChildren(parentId);
          return nodes.filter(n => !n.url);
        }

        async function ensureFolder(parentId, title) {
          const existing = (await childrenOf(parentId)).find(n => n.title === title);
          if (existing) return existing.id;
          const created = await chrome.bookmarks.create({ parentId, title });
          return created.id;
        }

        async function ensurePath(parts) {
          let parentId = rootId;
          const walked = [];
          for (const part of parts) {
            walked.push(part);
            const key = walked.join('\u0001');
            if (!pathIds[key]) pathIds[key] = await ensureFolder(parentId, part);
            parentId = pathIds[key];
          }
          return parentId;
        }

        for (const parts of folderPaths) await ensurePath(parts);

        const existingUrls = new Set();
        function normalizeUrl(raw) {
          try {
            const u = new URL(raw);
            u.hash = '';
            for (const key of Array.from(u.searchParams.keys())) {
              const lower = key.toLowerCase();
              if (lower.startsWith('utm_') || lower === 'fbclid' || lower === 'gclid') {
                u.searchParams.delete(key);
              }
            }
            u.hostname = u.hostname.replace(/^www\./, '').toLowerCase();
            u.pathname = u.pathname.replace(/\/+$/, '') || '/';
            return u.toString();
          } catch (_) {
            return String(raw || '').trim().toLowerCase();
          }
        }

        async function collectUrls(parentId) {
          const children = await chrome.bookmarks.getChildren(parentId);
          for (const child of children) {
            if (child.url) existingUrls.add(normalizeUrl(child.url));
            else await collectUrls(child.id);
          }
        }
        await collectUrls(rootId);

        let created = 0;
        let skipped = 0;
        for (const item of creates) {
          const key = normalizeUrl(item.url);
          if (existingUrls.has(key)) {
            skipped++;
            continue;
          }
          const parentId = await ensurePath(item.path);
          await chrome.bookmarks.create({ parentId, title: item.title || item.url, url: item.url });
          existingUrls.add(key);
          created++;
        }
        return { ok: true, created, skipped };
      }
      return { ok: false, error: 'Unknown method: ' + msg.method };
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'network_capture') return await handleNetworkCaptureCommand(msg, sender);
  if (msg.cmd === 'console') return await handleConsoleCaptureCommand(msg, sender);
  const knownCommands = [
    'site_permission', 'dialog_state', 'handle_dialog', 'navigate',
    'set_dialog_policy', 'clear_dialog_policy', 'cookies', 'cdp', 'batch',
    'tabs', 'debugger_targets', 'management', 'bookmarks', 'call_extension',
    'network_capture', 'console', 'downloads', 'bridge_status',
  ];
  if (msg.cmd === 'bridge_status') {
    return { ok: true, data: { version: '2026.08.12-pass2-final', knownCommands } };
  }
  return {
    ok: false,
    code: 'unknown_cmd',
    error: `Unknown extension cmd: ${msg.cmd}`,
    version: '2026.08.12-pass2-final',
    knownCommands,
    hint: 'The extension command router is alive; compare knownCommands and reload the unpacked extension if this server expects a newer build.',
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // tmwd_ping has its own dedicated listener below that answers synchronously;
  // letting it fall through to handleExtMessage would race two sendResponse
  // calls on the same message and log a noisy "Unknown cmd". Skip it here.
  if (msg && msg.cmd === 'tmwd_ping') return false;
  handleExtMessage(msg, sender).then(sendResponse);
  return true;
});

async function handleCookies(msg, sender) {
  try {
    // A page-driven request (via the TID channel) must not name an arbitrary
    // site: the page only gets to read cookies for its own origin, not
    // whatever URL it stuffed into msg.url. WS-bridge calls have no sender.tab
    // and may name any tab. This is what stops a compromised page from reading
    // the user's mail/bank cookies through this extension.
    let url = (sender && sender.tab) ? sender.tab.url : (msg.url || null);
    if (!url && msg.tabId) {
      const tab = await chrome.tabs.get(msg.tabId);
      url = tab.url;
    }
    const origin = url.match(/^https?:\/\/[^\/]+/)[0];
    const all = await chrome.cookies.getAll({ url });
    const part = await chrome.cookies.getAll({ url, partitionKey: { topLevelSite: origin } }).catch(() => []);
    const merged = [...all];
    for (const c of part) {
      if (!merged.some(x => x.name === c.name && x.domain === c.domain)) merged.push(c);
    }
    return { ok: true, data: merged };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function batchDeadlineRemainingMs(msg, stage) {
  const deadlineEpochMs = Number(msg.deadlineEpochMs);
  if (!Number.isFinite(deadlineEpochMs) || deadlineEpochMs <= 0) return null;
  const remaining = Math.floor(deadlineEpochMs - Date.now());
  if (remaining > 0) return remaining;
  const error = new Error(`cdp_timeout: batch deadline exhausted ${stage}`);
  error.code = 'cdp_timeout';
  throw error;
}

async function handleBatch(msg, sender) {
  const R = [];
  let attachedTabId = null;
  let debuggerLease = null;
  const resolve$N = (params) => JSON.parse(JSON.stringify(params || {}).replace(/"\$(\d+)\.([^"]+)"/g,
    (_, i, path) => { let v = R[+i]; for (const k of path.split('.')) v = v[k]; return JSON.stringify(v); }));
  try {
    batchDeadlineRemainingMs(msg, 'before batch start');
    for (const c of msg.commands) {
      batchDeadlineRemainingMs(msg, `before ${c.method || c.cmd || 'command'}`);
      if (c.tabId === undefined && msg.tabId !== undefined) c.tabId = msg.tabId;
      if (c.cmd === 'cookies') {
        R.push(await handleCookies(c, sender));
      } else if (c.cmd === 'tabs') {
        const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
        R.push({ ok: true, data: tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId })) });
      } else if (c.cmd === 'cdp') {
        const tabId = c.tabId || msg.tabId || sender.tab?.id;
        if (attachedTabId !== tabId) {
          if (debuggerLease) {
            const previousLease = debuggerLease;
            debuggerLease = null;
            attachedTabId = null;
            await detachAbmDebugger(previousLease);
          }
          debuggerLease = await attachAbmDebugger({ tabId });
          attachedTabId = tabId;
          batchDeadlineRemainingMs(msg, `after attaching for ${c.method}`);
        }
        const remaining = batchDeadlineRemainingMs(msg, `before dispatching ${c.method}`);
        const requestedTimeout = boundedCdpTimeout(c.timeoutMs ?? msg.timeoutMs);
        R.push(await sendDebuggerCommandWithTimeout(
          debuggerLease, c.method, resolve$N(c.params),
          remaining === null ? requestedTimeout : Math.min(requestedTimeout, remaining),
          remaining === null ? 100 : 1,
        ));
      } else {
        R.push({ ok: false, error: 'unknown cmd: ' + c.cmd });
      }
    }
    return { ok: true, results: R };
  } catch (e) {
    const code = debuggerFailureCode(e);
    return {
      ok: false, code, error: e.message || String(e), results: R,
      retryable: code === 'cdp_timeout' || code === 'debugger_detached',
      hint: code === 'debugger_conflict'
        ? 'Close DevTools or the competing debugger on this tab, then retry.'
        : 'ABM invalidated and detached the timed-out debugger lease; retry once on the same tab.',
    };
  } finally {
    if (debuggerLease) try { await detachAbmDebugger(debuggerLease); } catch (_) {}
  }
}

async function handleCDP(msg, sender) {
  // Debuggee accepts tabId, extensionId or targetId. All three were measured
  // against another extension; none can reach one:
  //   tabId    -> "Cannot access a chrome-extension:// URL of different extension"
  //   extensionId -> means the legacy MV2 background page only; every MV3
  //                  extension lacks one, so "No background page with given id"
  //   targetId -> same refusal as tabId, even for a target getTargets() lists
  //               as attached=false. The check is at attach, not at lookup.
  // Kept because targetId still addresses OUR own offscreen/worker targets.
  const target = msg.targetId ? { targetId: msg.targetId }
               : msg.extensionId ? { extensionId: msg.extensionId }
                                 : { tabId: msg.tabId || sender.tab?.id };
  if (!target.targetId && !target.extensionId && !target.tabId) {
    return { ok: false, error: 'no tabId, extensionId or targetId' };
  }
  let debuggerLease = null;
  try {
    debuggerLease = await attachAbmDebugger(target);
    const result = await sendDebuggerCommandWithTimeout(
      debuggerLease, msg.method, msg.params || {}, boundedCdpTimeout(msg.timeoutMs),
    );
    return { ok: true, data: result };
  } catch (e) {
    const code = debuggerFailureCode(e);
    return {
      ok: false,
      code,
      error: e.message || String(e),
      method: msg.method,
      timeout_ms: boundedCdpTimeout(msg.timeoutMs),
      retryable: code === 'cdp_timeout' || code === 'debugger_detached',
      hint: code === 'debugger_conflict'
        ? 'Close DevTools or the competing debugger on this tab, then retry.'
        : 'ABM invalidated and detached the timed-out debugger lease; retry once after list_tabs.',
    };
  } finally {
    if (debuggerLease) try { await detachAbmDebugger(debuggerLease); } catch (_) {}
  }
}
// Filter out chrome:// and other internal tabs that can't be scripted
const isScriptable = url => url && /^https?:/.test(url);

// --- Shared page/CDP script builder core ---
function buildExecScript(code, errorHandler, dialogScope = null, sourceUrl = null) {
  const hasDialogScope = Boolean(
    dialogScope?.token &&
    (dialogScope.policy === 'dismiss' || dialogScope.policy === 'accept')
  );
  const dialogToken = hasDialogScope ? dialogScope.token : null;
  const dialogPolicy = hasDialogScope ? dialogScope.policy : null;
  const execSourceUrl = typeof sourceUrl === 'string' && sourceUrl
    ? sourceUrl.replace(/[\r\n]/g, '') : null;
  return `(async () => {
    function smartProcessResult(result) {
      if (result === null || result === undefined || typeof result !== 'object') return result;
      try { if (result.window === result && result.document) return '[Window: ' + (result.location?.href || 'about:blank') + ']'; } catch(_){}
      if (typeof jQuery !== 'undefined' && result instanceof jQuery) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result instanceof NodeList || result instanceof HTMLCollection) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result.nodeType === 1) return result.outerHTML;
      if (!Array.isArray(result) && typeof result === 'object' && 'length' in result && typeof result.length === 'number') {
        const firstElement = result[0];
        if (firstElement && firstElement.nodeType === 1) {
          const elements = []; const length = Math.min(result.length, 100);
          for (let i = 0; i < length; i++) { const elem = result[i]; if (elem && elem.nodeType === 1) elements.push(elem.outerHTML); } return elements;
        }
      }
      try { return JSON.parse(JSON.stringify(result, function(key, value) { if (typeof value === 'object' && value !== null) { if (value.nodeType === 1) return value.outerHTML; if (value === window || value === document) return '[Object]'; try { if (value.window === value && value.document) return '[Window]'; } catch(_){} } return value; })); } catch (e) { return '[无法序列化: ' + e.message + ']'; }
    }
    // Dialog suppression is scoped to this command: disable_dialogs.js defers
    // to the native alert/confirm/prompt unless this deadline is in the future,
    // so the user's own confirmations keep working during normal browsing.
    //
    // A DEADLINE rather than a boolean, because a boolean stays stuck on if the
    // script throws past the finally, the tab navigates mid-command, or the
    // worker is evicted — and a stuck flag silently eats the user's own
    // confirm() dialogs, which is the very bug this was meant to fix.
    //
    // Each command captures its OWN deadline. Two concurrent commands used to
    // share one window.__tmwd_suppress_until slot: whichever finished first
    // zeroed it, un-suppressing the dialog while the other command's script
    // was still mid-flight. Only the command that set the CURRENT value may
    // clear it.
    const _tmwdHasDialogScope = ${JSON.stringify(hasDialogScope)};
    const _tmwdDeadline = _tmwdHasDialogScope ? Date.now() + 120000 : 0;
    const _tmwdDialogToken = ${JSON.stringify(dialogToken)};
    if (_tmwdHasDialogScope) {
      const _tmwdDialogScope = {
        token: _tmwdDialogToken,
        policy: ${JSON.stringify(dialogPolicy)},
        deadline: _tmwdDeadline,
      };
      const _tmwdScopes = Array.isArray(window.__tmwd_dialog_scopes)
        ? window.__tmwd_dialog_scopes : [];
      _tmwdScopes.push(_tmwdDialogScope);
      window.__tmwd_dialog_scopes = _tmwdScopes;
      window.__tmwd_suppress_until = Math.max(
        _tmwdDeadline,
        ..._tmwdScopes.map(scope => Number(scope.deadline) || 0),
      );
    }
    const _tmwdRecords = () => (Array.isArray(window.__tmwd_dialog_records)
      ? window.__tmwd_dialog_records : [])
      .filter(record => record.token === _tmwdDialogToken);
    try {
      const rawJsCode = ${JSON.stringify(code)}.trim();
      const _tmwdExecSourceUrl = ${JSON.stringify(execSourceUrl)};
      const _tmwdWithSourceUrl = c => _tmwdExecSourceUrl
        ? c + '\\n//# sourceURL=' + _tmwdExecSourceUrl : c;
      const jsCode = _tmwdWithSourceUrl(rawJsCode);
      const lines = rawJsCode.split(/\\r?\\n/).filter(l => l.trim());
      const lastLine = lines.length > 0 ? lines[lines.length - 1].trim() : '';
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      let r;
      function _air(c) { const ls = c.split(/\\r?\\n/); let i = ls.length - 1; while (i >= 0 && !ls[i].trim()) i--; if (i < 0) return c; const t = ls[i].trim(); if (/^(return |return;|return$|let |const |var |if |if\\(|for |for\\(|while |while\\(|switch|try |throw |class |function |async |import |export |\\/\\/|})/.test(t)) return c; ls[i] = ls[i].match(/^(\\s*)/)[1] + 'return ' + t; return ls.join('\\n'); }
      if (lastLine.startsWith('return')) {
        r = await (new AsyncFunction(jsCode))();
      } else {
        try { r = eval(jsCode); if (r instanceof Promise) r = await r; } catch (e) {
          if (e instanceof SyntaxError && /return/i.test(e.message)) {
            // A single-line script can contain several statements followed by
            // an explicit return. Auto-returning that whole line would return
            // after its first expression and silently skip the later work.
            r = await (new AsyncFunction(jsCode))();
          } else if (e instanceof SyntaxError && /await/i.test(e.message)) {
            r = await (new AsyncFunction(_tmwdWithSourceUrl(_air(rawJsCode))))();
          } else throw e;
        }
      }
      const processed = smartProcessResult(r);
      if (_tmwdHasDialogScope) {
        return { ok: true, data: {
          __tmwd_dialog_result: true,
          value: processed,
          dialogs: _tmwdRecords(),
        } };
      }
      return { ok: true, data: processed };
    } catch (e) {
      ${errorHandler}
    } finally {
      if (_tmwdHasDialogScope && Array.isArray(window.__tmwd_dialog_scopes)) {
        window.__tmwd_dialog_scopes = window.__tmwd_dialog_scopes
          .filter(scope => scope.token !== _tmwdDialogToken && Date.now() < scope.deadline);
        window.__tmwd_suppress_until = window.__tmwd_dialog_scopes.reduce(
          (latest, scope) => Math.max(latest, Number(scope.deadline) || 0), 0
        );
      } else if (_tmwdHasDialogScope && window.__tmwd_suppress_until === _tmwdDeadline) {
        window.__tmwd_suppress_until = 0;
      }
    }
  })()`;
}

function buildPageScript(code, dialogScope = null) {
  return buildExecScript(code, `
      const errMsg = e.message || String(e);
      return { ok: false, error: { name: e.name || 'Error', message: errMsg, stack: e.stack || '' },
        csp: errMsg.includes('Refused to evaluate') || errMsg.includes('unsafe-eval') || errMsg.includes('Content Security Policy') };
  `, dialogScope);
}

function buildCdpScript(code, dialogScope = null, sourceUrl = null) {
  return buildExecScript(code, `
      return { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } };
  `, dialogScope, sourceUrl);
}

function manualExecutionResult(status, dialog = null) {
  const blocked = status === 'blocked_by_dialog';
  return { ok: true, data: {
    __tmwd_dialog_result: true,
    value: null,
    dialogs: dialog ? [dialog] : [],
    dialog,
    status,
    dialog_action: 'manual',
    dialog_observed: Boolean(dialog),
    handled: false,
    pending_execution: true,
    manual_blocked: blocked,
  } };
}

function manualRawSyntaxError(error) {
  const message = error?.message || String(error);
  return { ok: true, data: {
    __tmwd_dialog_result: true,
    value: null,
    dialogs: [],
    dialog: null,
    status: 'manual_raw_syntax_error',
    dialog_action: 'manual',
    dialog_observed: false,
    handled: false,
    pending_execution: false,
    manual_blocked: false,
    error: {
      code: 'manual_raw_syntax_error',
      name: 'SyntaxError',
      message,
      suggestion: 'Use a raw expression (remove `return expr`) or write an authored IIFE explicitly.',
    },
  } };
}

function manualExecutionError(error, prefix = 'manual CDP execution failed: ') {
  const message = error?.message || String(error);
  return { ok: false, error: {
    name: error?.name || 'Error',
    message: prefix + message,
    stack: error?.stack || '',
  } };
}

function normalizeManualEvaluation(evaluationOutcome) {
  if (evaluationOutcome.kind === 'evaluation_error') {
    return manualExecutionError(evaluationOutcome.error, '');
  }
  const evaluation = evaluationOutcome.evaluation;
  if (evaluation?.exceptionDetails) {
    const exception = evaluation.exceptionDetails.exception;
    if (exception?.className === 'SyntaxError' ||
        /SyntaxError/i.test(evaluation.exceptionDetails.text || '') ||
        /SyntaxError/i.test(exception?.description || '')) {
      return manualRawSyntaxError({
        message: exception?.description || evaluation.exceptionDetails.text || 'SyntaxError',
      });
    }
    const description = evaluation.exceptionDetails.exception?.description ||
      evaluation.exceptionDetails.text || 'CDP Error';
    return { ok: false, error: {
      name: 'Error', message: description, stack: description,
    } };
  }
  const value = evaluation?.result?.value;
  return { ok: true, data: value };
}

async function releaseManualExecution(pending) {
  if (!pending) return;
  if (pending.releasePromise) return await pending.releasePromise;
  pending.released = true;
  pending.state = 'released';
  if (pending.expiryTimer) {
    clearTimeout(pending.expiryTimer);
    pending.expiryTimer = null;
  }
  if (pendingManualExecutions.get(pending.tabId) === pending) {
    pendingManualExecutions.delete(pending.tabId);
  }
  const debuggerLease = pending.debuggerLease;
  pending.debuggerLease = null;
  pending.releasePromise = (async () => {
    if (debuggerLease) {
      try { await detachAbmDebugger(debuggerLease); } catch (_) {}
    }
  })();
  await pending.releasePromise;
}

async function cancelManualExecution(tabId, reason, expectedPending = null) {
  const pending = pendingManualExecutions.get(tabId);
  if (!pending || (expectedPending && pending !== expectedPending)) return false;
  pending.cancelReason = reason;
  pending.state = 'cancelled';
  if (!pending.cancelResolved) {
    pending.cancelResolved = true;
    pending.resolveCancel({ kind: 'cancelled', reason });
  }
  await releaseManualExecution(pending);
  return true;
}

async function executeManualScript(tabId, code, dialogScope) {
  const existing = pendingManualExecutions.get(tabId);
  if (existing) return manualExecutionResult('busy', existing.dialog || null);

  const target = { tabId };
  const pending = {
    tabId,
    token: dialogScope.token,
    generation: nextManualExecutionGeneration++,
    debuggerLease: null,
    mainFrameId: null,
    executionContextId: null,
    evaluationStarted: false,
    evaluationSettled: false,
    state: 'preparing',
    eventFloor: Number.POSITIVE_INFINITY,
    deadline: 0,
    expiryTimer: null,
    dialog: null,
    released: false,
    releasePromise: null,
    cancelResolved: false,
    resolveDialog: null,
    resolveCancel: null,
  };
  pending.dialogSignal = new Promise(resolve => { pending.resolveDialog = resolve; });
  pending.cancelSignal = new Promise(resolve => { pending.resolveCancel = resolve; });
  pendingManualExecutions.set(tabId, pending);
  manualExecutionGenerations.set(tabId, pending.generation);

  try {
    pending.debuggerLease = await attachAbmDebugger(target);
    await sendDebuggerCommandWithTimeout(pending.debuggerLease, 'Page.enable', {}, 5000);
    await sendDebuggerCommandWithTimeout(pending.debuggerLease, 'Runtime.enable', {}, 5000);
    const frameTree = await sendDebuggerCommandWithTimeout(
      pending.debuggerLease, 'Page.getFrameTree', {}, 5000,
    );
    pending.mainFrameId = frameTree?.frameTree?.frame?.id || null;
    if (!pending.mainFrameId) {
      throw new Error('manual execution could not resolve the main frame');
    }
    pending.executionContextId = await waitForDefaultRuntimeExecutionContext(
      tabId, pending.mainFrameId,
    );

    await sendDebuggerCommandWithTimeout(pending.debuggerLease, 'Runtime.evaluate', {
      expression: 'void 0',
      contextId: pending.executionContextId,
      returnByValue: true,
    }, 5000);
    if (pendingManualExecutions.get(tabId) !== pending || pending.released) {
      throw new Error('manual execution ownership changed before evaluation');
    }
    pending.eventFloor = (dialogEventSequences.get(tabId) || 0) + 1;
    pending.deadline = Date.now() + Math.max(1, Number(dialogScope.timeoutMs) || 15000);
    pending.state = 'armed';
    pending.evaluationStarted = true;
    pending.expiryTimer = setTimeout(() => {
      if (pendingManualExecutions.get(tabId) === pending &&
          pending.state === 'armed' && !pending.dialog) {
        void cancelManualExecution(
          tabId, 'manual dialog ownership expired while waiting for an outcome', pending,
        );
      }
    }, Math.max(1, Number(dialogScope.timeoutMs) || 15000));
    let evaluationCommand;
    try {
      evaluationCommand = sendDebuggerCommandWithTimeout(
        pending.debuggerLease, 'Runtime.evaluate', {
        expression: code,
        contextId: pending.executionContextId,
        awaitPromise: true,
        returnByValue: true,
        replMode: true,
      }, boundedCdpTimeout(dialogScope.timeoutMs, 15000));
    } catch (error) {
      evaluationCommand = Promise.reject(error);
    }
    pending.evaluationPromise = Promise.resolve(evaluationCommand).then(
      evaluation => {
        pending.evaluationSettled = true;
        pending.state = 'settled';
        return { kind: 'evaluation', evaluation };
      },
      error => {
        pending.evaluationSettled = true;
        pending.state = 'settled';
        return { kind: 'evaluation_error', error };
      },
    );
    pending.settlementPromise = (async () => {
      const outcome = await pending.evaluationPromise;
      await releaseManualExecution(pending);
      return normalizeManualEvaluation(outcome);
    })();

    const first = await Promise.race([
      pending.settlementPromise.then(result => ({ kind: 'evaluation', result })),
      pending.dialogSignal,
      pending.cancelSignal,
    ]);
    if (first.kind === 'dialog') {
      return manualExecutionResult('blocked_by_dialog', first.dialog);
    }
    if (first.kind === 'cancelled') {
      await releaseManualExecution(pending);
      return manualExecutionError(
        new Error(first.reason || 'manual execution cancelled'), ''
      );
    }
    return first.result;
  } catch (error) {
    await cancelManualExecution(tabId, error.message || String(error));
    return manualExecutionError(error);
  }
}

// --- Scoped, temporary CSP removal -----------------------------------------
// Stripping CSP is necessary for eval-based injection, but it must not leak
// beyond the tab being automated or outlive the command. Session rules are
// never persisted to disk, so even a crash can't leave the profile exposed
// past a browser restart. Rule ids are namespaced per tab to allow parallel
// commands on different tabs; a refcount keeps nested calls on the SAME tab
// from tearing the rule down early.
const CSP_RULE_BASE = 90000;
// tabId -> {depth, ruleId}. A single tab can have nested withCspOff calls
// (e.g. a retry inside the original); the depth refcount keeps the rule alive
// until the LAST nested call exits. ruleId is allocated once per tab so two
// different tabs never share an id (tabId % 9000 collided once tabId >= 9000).
const cspRefs = new Map();
let cspNextRuleId = CSP_RULE_BASE;
function cspRuleIdFor(tabId) {
  let entry = cspRefs.get(tabId);
  if (!entry) {
    entry = { depth: 0, ruleId: cspNextRuleId++ };
    cspRefs.set(tabId, entry);
  }
  return entry;
}

// A crash or eviction mid-command could leave a rule behind, and a leftover
// rule means CSP stays off for that tab. Session rules die with the browser,
// but the worker restarts far more often than the browser does — so sweep a
// generous id range on every startup. cspNextRuleId resets on SW restart, so
// the sweep range must not depend on the in-memory counter.
(async () => {
  try {
    const rules = await chrome.declarativeNetRequest.getSessionRules();
    // Sweep every rule in our namespace. tabId is unbounded and ruleId grows
    // with it, so a fixed cap would miss rules from large tabIds.
    const ours = rules.filter(r => r.id >= CSP_RULE_BASE).map(r => r.id);
    if (ours.length) {
      await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: ours });
      console.log('[TMWD] cleared', ours.length, 'stale CSP rule(s)');
    }
  } catch (_) { /* API unavailable: nothing to clean */ }
})();

async function withCspOff(tabId, fn) {
  const entry = cspRuleIdFor(tabId);
  const ruleId = entry.ruleId;
  // Increment the refcount BEFORE the await. Doing it after let two concurrent
  // calls both read depth=0, both add the rule, and both tear it down in their
  // finally — even while the other call's fn() was still relying on it.
  entry.depth += 1;
  if (entry.depth === 1) {
    try {
      await chrome.declarativeNetRequest.updateSessionRules({
        removeRuleIds: [ruleId],
        addRules: [{
          id: ruleId, priority: 1,
          action: { type: 'modifyHeaders', responseHeaders: [
            { header: 'content-security-policy', operation: 'remove' },
            { header: 'content-security-policy-report-only', operation: 'remove' }
          ]},
          // tabIds confines this to the automated tab only.
          condition: { urlFilter: '*', resourceTypes: ['main_frame', 'sub_frame'], tabIds: [tabId] }
        }]
      });
    } catch (e) {
      // Not fatal: injection may still succeed on pages without CSP, and the
      // CDP fallback bypasses CSP entirely.
      console.log('[TMWD] CSP rule add failed:', e.message);
    }
  }
  try {
    // Never let a hung injection keep CSP off: race the work against a cap so
    // `finally` always runs and the rule always comes back.
    return await Promise.race([
      fn(),
      new Promise((_, rej) => setTimeout(
        () => rej(new Error('CSP-relaxed injection timed out')), 30000)),
    ]);
  } finally {
    entry.depth -= 1;
    if (entry.depth <= 0) {
      cspRefs.delete(tabId);
      try {
        await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [ruleId] });
      } catch (_) { /* rule may already be gone with the tab */ }
    }
  }
}

// --- WebSocket Client for TMWebDriver ---
let ws = null;
let connectInFlight = false;
// Last time the bridge answered our ping with a pong. A half-open zombie
// socket stays readyState===OPEN and accepts send() without error, so pong
// silence is the only reliable "this TCP connection is actually dead" signal.
let lastPongAt = 0;
const WS_URL = 'ws://127.0.0.1:18765';
const HTTP_PROBE = 'http://127.0.0.1:18766/link';

// --- Browser identity: distinguishes Chrome vs Edge vs multiple profiles ---
// Without this, Chrome tab 456 and Edge tab 456 collide into one server session.
let CLIENT_ID = null;
function getBrowserType() {
  const ua = navigator.userAgent;
  if (ua.includes('Edg/')) return 'edge';       // must test before Chrome; Edge UA also has 'Chrome/'
  if (ua.includes('OPR/')) return 'opera';
  if (ua.includes('Chrome/')) return 'chrome';
  return 'unknown';
}
async function getClientId() {
  if (CLIENT_ID) return CLIENT_ID;
  // storage MUST NOT be able to sink the whole registration path: if the
  // permission is missing or the API throws, a missing clientId only costs us
  // cross-restart id stability, whereas an exception here kills ext_ready /
  // tabs_update entirely (server registers 0 sessions). Degrade, don't crash.
  try {
    const s = await chrome.storage.local.get('tmwd_client_id');
    if (s.tmwd_client_id) { CLIENT_ID = s.tmwd_client_id; return CLIENT_ID; }
    // Per-profile: chrome.storage.local is isolated per profile, so two profiles of the
    // same browser get distinct ids too.
    CLIENT_ID = getBrowserType() + '_' + Math.random().toString(36).slice(2, 8);
    await chrome.storage.local.set({ tmwd_client_id: CLIENT_ID });
  } catch (e) {
    console.error('[TMWD-WS] storage unavailable, using ephemeral clientId', e);
    if (!CLIENT_ID) CLIENT_ID = getBrowserType() + '_' + Math.random().toString(36).slice(2, 8);
  }
  return CLIENT_ID;
}

function scheduleProbe() {
  // MV3 clamps sub-minute alarms aggressively; also wake via tabs/events below.
  chrome.alarms.create('tmwd-ws-probe', { delayInMinutes: 0.5, periodInMinutes: 1 });
}

// Keep the SW alive while the WS is connected.
//
// This used to rely on chrome.alarms.create({delayInMinutes: 0.4}) with the
// comment "~24s, under the 30s SW timeout". Chrome's minimum alarm delay is
// 30s, so 0.4 was clamped UP to at least the timeout it was supposed to beat —
// the alarm could only re-wake the worker after an eviction, never prevent it.
// It also left the no-tab case unprotected: with zero scriptable tabs there is
// no content.js tick, which is exactly the state the "works with no tabs open"
// tools exist to serve.
//
// Activity on an extension API / message port DOES reset the idle timer, so a
// self-driven ping over the socket is what actually holds the worker up. 20s
// stays clear of the 30s limit, and the ping doubles as the liveness probe the
// pong watchdog already consumes.
let keepaliveTimer = null;
const KEEPALIVE_MS = 20000;

function scheduleKeepalive() {
  if (keepaliveTimer !== null) return; // already ticking
  keepaliveTimer = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      stopKeepalive();
      ws = null;
      ensureConnected('keepalive-lost');
      scheduleProbe(); // alarm keeps retrying even if this worker is evicted
      return;
    }
    // Zombie check: on a half-open socket send() succeeds silently and the OS
    // won't surface the dead peer for minutes, so we'd keep pushing tabs into a
    // black hole. No pong across ~3 ticks => treat the socket as dead.
    if (lastPongAt && Date.now() - lastPongAt > 55000) {
      console.log('[TMWD-WS] pong timeout, forcing reconnect');
      try { ws.close(); } catch (_) {}
      ws = null;
      lastPongAt = 0;
      stopKeepalive();
      ensureConnected('keepalive-pong-timeout');
      scheduleProbe();
      return;
    }
    try {
      ws.send(JSON.stringify({ type: 'ping' }));
      // After a bridge restart the socket can stay OPEN client-side while the
      // new daemon has an empty session table — re-push tabs every tick so MCP
      // recovers without user action.
      sendTabsUpdate();
      // Touching an extension API resets the idle timer even if the socket
      // write alone were not enough.
      chrome.runtime.getPlatformInfo(() => { void chrome.runtime.lastError; });
    } catch (e) {
      console.log('[TMWD-WS] keepalive ping failed:', e.message);
      ws = null;
      stopKeepalive();
      ensureConnected('keepalive-send-failed');
      scheduleProbe();
    }
  }, KEEPALIVE_MS);
}

function stopKeepalive() {
  if (keepaliveTimer !== null) { clearInterval(keepaliveTimer); keepaliveTimer = null; }
}

async function isServerAlive() {
  // Probe HTTP side (18766), not the WS port (18765 returns 426 and may confuse fetch).
  try {
    const ctrl = new AbortController();
    setTimeout(() => ctrl.abort(), 1500);
    await fetch(HTTP_PROBE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd: 'get_all_sessions' }),
      signal: ctrl.signal,
    });
    return true;
  } catch (_) {
    // Network refused / timeout. Still try WS: some stacks answer WS but not this route.
    try {
      const ctrl2 = new AbortController();
      setTimeout(() => ctrl2.abort(), 800);
      await fetch('http://127.0.0.1:18765/', { signal: ctrl2.signal });
      return true; // any response including 426 means port is open
    } catch (e2) {
      return false;
    }
  }
}

function ensureConnected(reason) {
  if (ws && ws.readyState <= 1) return;
  console.log('[TMWD-WS] ensureConnected:', reason || '');
  connectWS();
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'tmwd-ws-keepalive') {
    // Legacy alarm from older installs. The ping/zombie/re-register logic now
    // lives in the setInterval keepalive (alarms can't fire faster than 60s,
    // so they could never hold the worker up). Keep this as a recovery path:
    // if the worker was evicted anyway, this re-arms everything.
    if (ws && ws.readyState === WebSocket.OPEN) {
      scheduleKeepalive();
    } else {
      ws = null;
      ensureConnected('keepalive-lost');
      scheduleProbe();
    }
    chrome.alarms.clear('tmwd-ws-keepalive');
  }
  if (alarm.name === 'tmwd-ws-probe') {
    if (ws && ws.readyState <= 1) return; // Already connected/connecting
    // Always attempt WS; isServerAlive is only a log hint (HTTP probe is unreliable).
    const alive = await isServerAlive();
    console.log('[TMWD-WS] probe tick, httpAlive=', alive);
    ensureConnected('alarm-probe');
  }
});

async function handleWsExec(data) {
  const tabId = data.tabId;
  const dialogScopeMatch = typeof data.code === 'string'
    ? data.code.match(/\/\*__tmwd_dialog_scope:([A-Za-z0-9._-]+)\*\//)
    : null;
  const dialogScopeToken = dialogScopeMatch ? dialogScopeMatch[1] : null;
  const dialogScope = currentExecDialogPolicy(tabId, dialogScopeToken) ||
    (!dialogScopeToken ? claimManualExecDialogPolicy(tabId, data.code) : null);
  console.log('[TMWD-WS] Exec request', data.id, 'on tab', tabId);
  // Same dead-socket hazard as onmessage, with a wider window: script exec plus
  // the CDP fallback can run for seconds while the bridge restarts under us.
  const sock = ws;
  const send = (obj) => {
    if (sock && sock.readyState === WebSocket.OPEN) {
      try { sock.send(JSON.stringify(obj)); return true; } catch (_) { return false; }
    }
    return false;
  };
  // The ACK is the bridge's ONLY signal that the script was delivered: no ACK
  // means it classifies the command "undelivered" and retries it as
  // side-effect-free. So if the ACK can't go out, we must NOT run the script —
  // otherwise a form submit / purchase executes here, the retry runs it again,
  // and the agent sees a clean success on a double execution.
  if (!send({ type: 'ack', id: data.id })) {
    console.log('[TMWD-WS] ACK undeliverable, skipping exec so the retry stays safe', data.id);
    return;
  }
  if (!tabId) {
    send({ type: 'error', id: data.id, error: 'No tabId provided' });
    return;
  }
  // Use onCreated listener to reliably capture new tabs (avoids race condition with query-diff).
  // Only count tabs THIS script opened (openerTabId === tabId): without the
  // filter every new tab in the browser — including ones the user opens by
  // hand or other extensions create — gets reported as a script side-effect.
  const newTabIds = new Set();
  const onCreated = (tab) => { if (tab.openerTabId === tabId) newTabIds.add(tab.id); };
  chrome.tabs.onCreated.addListener(onCreated);
  try {
    let res;
    if (dialogScope?.policy === 'manual') {
      // Manual must be stopped by the debugger outside user-controlled
      // JavaScript. A thrown sentinel can always be caught by indirect eval or
      // a Function constructor, so manual evaluations never use executeScript.
      res = await executeManualScript(tabId, data.code, dialogScope);
    } else {
      const inject = async () => {
        const result = await chrome.scripting.executeScript({
          target: { tabId },
          world: 'MAIN',
          func: async (s) => await eval(s),
          args: [buildPageScript(data.code, dialogScope)]
        });
        let r = result[0]?.result;
        if (r === null || r === undefined) {
          console.log('[TMWD-WS] executeScript returned null/undefined, treating as CSP issue');
          r = { ok: false, error: { name: 'Error', message: 'executeScript returned null (possible CSP or context issue)', stack: '' }, csp: true };
        }
        return r;
      };
      try {
        // First attempt WITHOUT touching CSP. Most pages inject fine, and a
        // declarativeNetRequest rule change makes Chrome re-evaluate the tab's
        // requests — doing that in the same await chain as the injection made
        // executeScript hang indefinitely (ACK sent, result never returned).
        res = await inject();
        // Only a CSP-flavoured failure justifies relaxing the header, and then
        // only for this tab, for this one retry.
        if (res && !res.ok && res.csp) {
          console.log('[TMWD-WS] retrying injection with CSP relaxed for tab', tabId);
          try { res = await withCspOff(tabId, inject); } catch (e) {
            console.log('[TMWD-WS] CSP-relaxed retry failed:', e.message);
          }
        }
      } catch (e) {
        console.log('[TMWD-WS] scripting.executeScript failed:', e.message);
        res = { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' }, csp: true };
      }
      // CDP fallback for CSP-restricted pages
      if (res && !res.ok && res.csp) {
        console.log('[TMWD-WS] CDP fallback for tab', tabId);
        const wrappedCode = buildCdpScript(data.code, dialogScope);
        let cdpLease = null;
        try {
          cdpLease = await attachAbmDebugger({ tabId });
          const cdpRes = await sendDebuggerCommandWithTimeout(cdpLease, 'Runtime.evaluate', {
            expression: wrappedCode, awaitPromise: true, returnByValue: true
          }, DEFAULT_CDP_TIMEOUT_MS);
          if (cdpRes.exceptionDetails) {
            const desc = cdpRes.exceptionDetails.exception?.description || 'CDP Error';
            res = { ok: false, error: { name: 'Error', message: desc, stack: desc } };
          } else {
            res = cdpRes.result.value;
          }
        } catch (cdpErr) {
          res = { ok: false, error: { name: 'Error', message: 'CDP fallback failed: ' + cdpErr.message, stack: '' } };
        } finally {
          if (cdpLease) try { await detachAbmDebugger(cdpLease); } catch (_) {}
        }
      }
    }
    // Grace period for async tab creation (e.g. link click with target=_blank)
    if (newTabIds.size === 0) await new Promise(r => setTimeout(r, 200));
    chrome.tabs.onCreated.removeListener(onCreated);
    // Get full info for captured new tabs
    const newTabs = [];
    for (const id of newTabIds) {
      try {
        const t = await chrome.tabs.get(id);
        newTabs.push({
          id: t.id,
          url: t.url,
          title: t.title,
          generation: await tabGenerationFor(t.id),
        });
      } catch (_) {}
    }
    if (res?.ok) {
      // Echo back the tab we actually ran on. The Python side reports this
      // instead of guessing from its remembered default, which can be stale
      // (or None) while the script ran on a real tab here.
      send({ type: 'result', id: data.id, result: res.data, newTabs, tabId });
    } else {
      console.log(res);
      send({ type: 'error', id: data.id, error: res?.error || 'Unknown error', newTabs, tabId });
    }
  } catch (e) {
    send({ type: 'error', id: data.id, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' }, tabId });
  } finally {
    chrome.tabs.onCreated.removeListener(onCreated);
  }
}

function connectWS() {
  if (ws && ws.readyState <= 1) return; // CONNECTING or OPEN
  if (connectInFlight) return;
  connectInFlight = true;
  ws = null;
  console.log('[TMWD-WS] Connecting to', WS_URL);
  // Capture the socket we are about to create. Every handler below closes over
  // `self`, not the global `ws`: a reconnect may have replaced `ws` with a new
  // socket by the time this one's onclose/onerror fires, and the old handler
  // must NOT null out the new connection or tear down its keepalive.
  let self;
  try {
    self = new WebSocket(WS_URL);
    ws = self;
  } catch (e) {
    console.error('[TMWD-WS] Constructor error:', e);
    ws = null;
    connectInFlight = false;
    scheduleProbe();
    return;
  }
  // A TCP connect can hang silently (VPN up, bridge half-dead): the socket
  // sits in CONNECTING forever and every ensureConnected() call bails on
  // readyState<=1, so nothing ever retries. Cap the attempt; onclose will
  // route back through the probe alarm.
  const connectTimer = setTimeout(() => {
    if (self.readyState === WebSocket.CONNECTING) {
      try { self.close(); } catch (_) {}
    }
  }, 5000);
  ws.onopen = async () => {
    clearTimeout(connectTimer);
    if (ws !== self) return; // a newer socket owns the slot
    connectInFlight = false;
    console.log('[TMWD-WS] Connected!');
    // Seed the pong clock so the zombie watchdog has a baseline; the bridge's
    // first pong (within a keepalive tick) refreshes it. Without a seed the
    // watchdog's `lastPongAt &&` guard would never arm.
    lastPongAt = Date.now();
    scheduleKeepalive(); // Keep SW alive while connected
    // Arm the probe alarm even on a healthy connection: if the SW is evicted
    // while the socket is OPEN, only an alarm can wake us back up, and a
    // healthy probe tick is a cheap no-op.
    scheduleProbe();
    try {
      const clientId = await getClientId();
      const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
      if (self !== ws || self.readyState !== WebSocket.OPEN) return; // died during await
      self.send(JSON.stringify({
        type: 'ext_ready',
        clientId,
        browser: getBrowserType(),
        tabs: await Promise.all(tabs.map(async t => ({
          id: t.id,
          url: t.url,
          title: t.title,
          generation: await tabGenerationFor(t.id),
        })))
      }));
      console.log('[TMWD-WS] Sent ext_ready with', tabs.length, 'tabs as', clientId);
    } catch (e) {
      console.error('[TMWD-WS] ext_ready failed', e);
    }
  };
  ws.onmessage = async (event) => {
    if (ws !== self) return; // a newer socket owns the slot
    // This handler awaits, and the bridge can die mid-await: onclose nulls the
    // global `ws`, so a later send() would throw DOMException on a dead socket.
    // Capture the socket we arrived on and only answer if it's still open --
    // the reply is worthless anyway once the bridge is gone.
    const sock = self;
    const reply = (obj) => {
      if (sock && sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(obj));
    };
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'pong') { lastPongAt = Date.now(); return; }
      if (data.id && data.code) {
        let code = data.code;
        // If code is a JSON string representing an object, parse it
        if (typeof code === 'string') {
          try { const p = JSON.parse(code); if (p && typeof p === 'object') code = p; } catch (_) {}
        }
        if (typeof code === 'object' && code !== null && code.cmd) {
          // Custom protocol message → route to handleExtMessage
          if (code.tabId === undefined && data.tabId !== undefined) code.tabId = data.tabId;
          const res = await handleExtMessage(code, {});
          reply({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error });
        } else if (typeof code === 'string') {
          // Plain JS code
          await handleWsExec(data);
        } else if (typeof code === 'object' && code !== null) {
          // Object without cmd → legacy extension message
          const msg = code.tabId === undefined && data.tabId !== undefined ? { ...code, tabId: data.tabId } : code;
          const res = await handleExtMessage(msg, {});
          reply({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error });
        }
      }
    } catch (e) {
      // Name the two cases apart: a dead socket is benign (bridge restarted),
      // a real parse failure is not. The old wording blamed parsing for both.
      const dead = !sock || sock.readyState !== WebSocket.OPEN;
      console[dead ? 'log' : 'error'](
        dead ? '[TMWD-WS] bridge went away mid-request, reply dropped'
             : '[TMWD-WS] message handling error', e);
    }
  };
  ws.onclose = () => {
    clearTimeout(connectTimer);
    if (ws !== self) return; // a newer connection owns the slot; leave it be
    console.log('[TMWD-WS] Disconnected');
    ws = null;
    connectInFlight = false;
    // Stop holding the worker alive for a socket that's gone; the probe alarm
    // takes over reconnect duty and survives eviction.
    stopKeepalive();
    scheduleProbe();
  };
  ws.onerror = (e) => {
    console.error('[TMWD-WS] Error:', e);
    // onclose will fire after this, which triggers reconnect
  };
}

// Initial connect + wake-up hooks
void installPermissionLeaseRecoveryHooks();
ensureConnected('boot');
chrome.runtime.onStartup.addListener(() => {
  ensureConnected('onStartup');
});
chrome.runtime.onInstalled.addListener(() => {
  ensureConnected('onInstalled');
});
// Long-lived port from content scripts. Establishing a port is a stronger SW
// wake signal than sendMessage — and merely re-instantiating the dead SW runs
// this file top-to-bottom, hitting ensureConnected('boot') above. We also kick
// a connect here so a freshly-woken SW re-registers tabs without waiting for
// the next 5s poll.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'tmwd_keepalive') return;
  ensureConnected('port-connect');
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { sendTabsUpdate(); } catch (_) {}
  }
  // Consume lastError: when the content page enters bfcache Chrome closes this
  // port and sets lastError ("moved into back/forward cache"). Not reading it
  // logs an "Unchecked runtime.lastError" warning every navigation. The content
  // side re-opens the port on its next poll / pageshow, so nothing to do here.
  port.onDisconnect.addListener(() => { void chrome.runtime.lastError; });
});
// Popup / content can poke SW awake
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.cmd === 'tmwd_ping') {
    ensureConnected('runtime-message');
    // Badge click / content-script poll is a free chance to re-register tabs
    // against a bridge that restarted while our socket looked healthy.
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { sendTabsUpdate(); } catch (_) {}
    }
    sendResponse({
      ok: true,
      ws: !!(ws && ws.readyState === WebSocket.OPEN),
      readyState: ws ? ws.readyState : -1,
    });
    return true;
  }
  return false;
});

// Sync tab list on changes — also re-open WS if SW slept
async function sendTabsUpdate() {
  ensureConnected('tabs-event');
  // The check above goes stale across these awaits, so re-check the SAME socket
  // right before sending rather than trusting the earlier readyState.
  const sock = ws;
  try {
    const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url) && !/streamlit/i.test(t.title));
    if (!sock || sock.readyState !== WebSocket.OPEN) return;
    const clientId = await getClientId();
    if (!sock || sock.readyState !== WebSocket.OPEN) return;
    sock.send(JSON.stringify({
      type: 'tabs_update',
      clientId,
      browser: getBrowserType(),
      tabs: await Promise.all(tabs.map(async t => ({
        id: t.id,
        url: t.url,
        title: t.title,
        generation: await tabGenerationFor(t.id),
      })))
    }));
  } catch (e) {
    console.error('[TMWD-WS] tabs_update failed', e);
  }
}
chrome.tabs.onUpdated.addListener((_, changeInfo) => {
  if (changeInfo.status === 'complete' || changeInfo.url) sendTabsUpdate();
});
chrome.tabs.onRemoved.addListener((tabId) => {
  void forgetTabGeneration(tabId).finally(() => sendTabsUpdate());
});
chrome.tabs.onCreated.addListener((tab) => {
  void (async () => {
    await loadTabGenerations();
    if (!tabGenerations.has(tab.id)) await scheduleNewTabGeneration(tab.id);
  })().finally(() => sendTabsUpdate());
});
chrome.tabs.onActivated.addListener(() => ensureConnected('tab-activated'));

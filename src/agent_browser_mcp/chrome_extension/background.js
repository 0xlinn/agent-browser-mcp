// background.js - Cookie + CDP Bridge
chrome.runtime.onInstalled.addListener(() => {
  console.log('CDP Bridge installed');
  // Strip CSP headers to allow eval/inline scripts
  chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: [9999],
    addRules: [{
      id: 9999, priority: 1,
      action: { type: 'modifyHeaders', responseHeaders: [
        { header: 'content-security-policy', operation: 'remove' },
        { header: 'content-security-policy-report-only', operation: 'remove' }
      ]},
      condition: { urlFilter: '*', resourceTypes: ['main_frame', 'sub_frame'] }
    }]
  });
});

async function handleExtMessage(msg, sender) {
  if (msg.cmd === 'cookies') return await handleCookies(msg, sender);
  if (msg.cmd === 'cdp') return await handleCDP(msg, sender);
  if (msg.cmd === 'batch') return await handleBatch(msg, sender);
  if (msg.cmd === 'tabs') {
    try {
      if (msg.method === 'switch') {
        const tab = await chrome.tabs.update(msg.tabId, { active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        return { ok: true };
      } else {
        const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
        const data = tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId }));
        return { ok: true, data };
      }
    } catch (e) { return { ok: false, error: e.message }; }
  }
  if (msg.cmd === 'management') {
    try {
      if (msg.method === 'list') {
        const all = await chrome.management.getAll();
        return { ok: true, data: all.map(e => ({ id: e.id, name: e.name, enabled: e.enabled, type: e.type, version: e.version })) };
      }
      if (msg.method === 'reload') {
        chrome.alarms.create('tmwd-self-reload', { when: Date.now() + 200 });
        return { ok: true };
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
  return { ok: false, error: 'Unknown cmd: ' + msg.cmd };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleExtMessage(msg, sender).then(sendResponse);
  return true;
});

async function handleCookies(msg, sender) {
  try {
    let url = msg.url || sender.tab?.url;
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

async function handleBatch(msg, sender) {
  const R = [];
  let attached = null;
  const resolve$N = (params) => JSON.parse(JSON.stringify(params || {}).replace(/"\$(\d+)\.([^"]+)"/g,
    (_, i, path) => { let v = R[+i]; for (const k of path.split('.')) v = v[k]; return JSON.stringify(v); }));
  try {
    for (const c of msg.commands) {
      if (c.tabId === undefined && msg.tabId !== undefined) c.tabId = msg.tabId;
      if (c.cmd === 'cookies') {
        R.push(await handleCookies(c, sender));
      } else if (c.cmd === 'tabs') {
        const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
        R.push({ ok: true, data: tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId })) });
      } else if (c.cmd === 'cdp') {
        const tabId = c.tabId || msg.tabId || sender.tab?.id;
        if (attached !== tabId) {
          if (attached) { await chrome.debugger.detach({ tabId: attached }); attached = null; }
          await chrome.debugger.attach({ tabId }, '1.3');
          attached = tabId;
        }
        R.push(await chrome.debugger.sendCommand({ tabId }, c.method, resolve$N(c.params)));
      } else {
        R.push({ ok: false, error: 'unknown cmd: ' + c.cmd });
      }
    }
    if (attached) await chrome.debugger.detach({ tabId: attached });
    return { ok: true, results: R };
  } catch (e) {
    if (attached) try { await chrome.debugger.detach({ tabId: attached }); } catch (_) {}
    return { ok: false, error: e.message, results: R };
  }
}

async function handleCDP(msg, sender) {
  const tabId = msg.tabId || sender.tab?.id;
  if (!tabId) return { ok: false, error: 'no tabId' };
  try {
    await chrome.debugger.attach({ tabId }, '1.3');
    const result = await chrome.debugger.sendCommand({ tabId }, msg.method, msg.params || {});
    await chrome.debugger.detach({ tabId });
    return { ok: true, data: result };
  } catch (e) {
    try { await chrome.debugger.detach({ tabId }); } catch (_) {}
    return { ok: false, error: e.message };
  }
}
// Filter out chrome:// and other internal tabs that can't be scripted
const isScriptable = url => url && /^https?:/.test(url);

// --- Shared page/CDP script builder core ---
function buildExecScript(code, errorHandler) {
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
    try {
      const jsCode = ${JSON.stringify(code)}.trim();
      const lines = jsCode.split(/\\r?\\n/).filter(l => l.trim());
      const lastLine = lines.length > 0 ? lines[lines.length - 1].trim() : '';
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      let r;
      function _air(c) { const ls = c.split(/\\r?\\n/); let i = ls.length - 1; while (i >= 0 && !ls[i].trim()) i--; if (i < 0) return c; const t = ls[i].trim(); if (/^(return |return;|return$|let |const |var |if |if\\(|for |for\\(|while |while\\(|switch|try |throw |class |function |async |import |export |\\/\\/|})/.test(t)) return c; ls[i] = ls[i].match(/^(\\s*)/)[1] + 'return ' + t; return ls.join('\\n'); }
      if (lastLine.startsWith('return')) {
        r = await (new AsyncFunction(jsCode))();
      } else {
        try { r = eval(jsCode); if (r instanceof Promise) r = await r; } catch (e) {
          if (e instanceof SyntaxError && (/return/i.test(e.message) || /await/i.test(e.message))) { r = await (new AsyncFunction(_air(jsCode)))(); } else throw e;
        }
      }
      return { ok: true, data: smartProcessResult(r) };
    } catch (e) {
      ${errorHandler}
    }
  })()`;
}

function buildPageScript(code) {
  return buildExecScript(code, `
      const errMsg = e.message || String(e);
      return { ok: false, error: { name: e.name || 'Error', message: errMsg, stack: e.stack || '' },
        csp: errMsg.includes('Refused to evaluate') || errMsg.includes('unsafe-eval') || errMsg.includes('Content Security Policy') };
  `);
}

function buildCdpScript(code) {
  return buildExecScript(code, `
      return { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } };
  `);
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

function scheduleKeepalive() {
  // Keep SW alive while WS is connected (~25s, under 30s SW timeout)
  chrome.alarms.create('tmwd-ws-keepalive', { delayInMinutes: 0.4 }); // ~24s
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
  if (alarm.name === 'tmwd-self-reload') {
    chrome.runtime.reload();
    return;
  }
  if (alarm.name === 'tmwd-ws-keepalive') {
    // Keepalive: ping to keep SW alive + detect dead / half-open connections.
    // After bridge restart the socket can stay OPEN client-side while the new
    // daemon has an empty session table — re-push tabs every tick so MCP recovers.
    if (ws && ws.readyState === WebSocket.OPEN) {
      // Zombie check: on a half-open socket send() succeeds silently and the
      // OS won't surface the dead peer for minutes, so we'd keep pushing tabs
      // into a black hole. If the bridge hasn't pong'd across two full ticks
      // (~48s), treat the socket as dead and force a fresh connect.
      if (lastPongAt && Date.now() - lastPongAt > 55000) {
        console.log('[TMWD-WS] pong timeout, forcing reconnect');
        try { ws.close(); } catch (_) {}
        ws = null;
        lastPongAt = 0;
        ensureConnected('keepalive-pong-timeout');
        scheduleProbe();
        return;
      }
      try {
        ws.send('{"type":"ping"}');
        // fire-and-forget re-register; must not block the alarm handler
        sendTabsUpdate();
      } catch (_) {
        ws = null;
        ensureConnected('keepalive-send-failed');
        scheduleProbe();
        return;
      }
      scheduleKeepalive();
    } else {
      // Connection lost, switch to probe mode
      ws = null;
      ensureConnected('keepalive-lost');
      scheduleProbe();
    }
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
  console.log('[TMWD-WS] Exec request', data.id, 'on tab', tabId);
  ws.send(JSON.stringify({ type: 'ack', id: data.id }));
  if (!tabId) {
    ws.send(JSON.stringify({ type: 'error', id: data.id, error: 'No tabId provided' }));
    return;
  }
  // Use onCreated listener to reliably capture new tabs (avoids race condition with query-diff)
  const newTabIds = new Set();
  const onCreated = (tab) => { newTabIds.add(tab.id); };
  chrome.tabs.onCreated.addListener(onCreated);
  try {
    let res;
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId },
        world: 'MAIN',
        func: async (s) => await eval(s),
        args: [buildPageScript(data.code)]
      });
      res = result[0]?.result;
      if (res === null || res === undefined) {
        console.log('[TMWD-WS] executeScript returned null/undefined, treating as CSP issue');
        res = { ok: false, error: { name: 'Error', message: 'executeScript returned null (possible CSP or context issue)', stack: '' }, csp: true };
      }
    } catch (e) {
      console.log('[TMWD-WS] scripting.executeScript failed:', e.message);
      res = { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' }, csp: true };
    }
    // CDP fallback for CSP-restricted pages
    if (res && !res.ok && res.csp) {
      console.log('[TMWD-WS] CDP fallback for tab', tabId);
      const wrappedCode = buildCdpScript(data.code);
      try {
        await chrome.debugger.attach({ tabId }, '1.3');
        const cdpRes = await chrome.debugger.sendCommand({ tabId }, 'Runtime.evaluate', {
          expression: wrappedCode, awaitPromise: true, returnByValue: true
        });
        await chrome.debugger.detach({ tabId });
        if (cdpRes.exceptionDetails) {
          const desc = cdpRes.exceptionDetails.exception?.description || 'CDP Error';
          res = { ok: false, error: { name: 'Error', message: desc, stack: desc } };
        } else {
          res = cdpRes.result.value;
        }
      } catch (cdpErr) {
        try { await chrome.debugger.detach({ tabId }); } catch (_) {}
        res = { ok: false, error: { name: 'Error', message: 'CDP fallback failed: ' + cdpErr.message, stack: '' } };
      }
    }
    // Grace period for async tab creation (e.g. link click with target=_blank)
    if (newTabIds.size === 0) await new Promise(r => setTimeout(r, 200));
    chrome.tabs.onCreated.removeListener(onCreated);
    // Get full info for captured new tabs
    const newTabs = [];
    for (const id of newTabIds) {
      try { const t = await chrome.tabs.get(id); newTabs.push({id: t.id, url: t.url, title: t.title}); } catch (_) {}
    }
    if (res?.ok) {
      ws.send(JSON.stringify({ type: 'result', id: data.id, result: res.data, newTabs }));
    } else {
      console.log(res);
      ws.send(JSON.stringify({ type: 'error', id: data.id, error: res?.error || 'Unknown error', newTabs }));
    }
  } catch (e) {
    ws.send(JSON.stringify({ type: 'error', id: data.id, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } }));
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
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    console.error('[TMWD-WS] Constructor error:', e);
    ws = null;
    connectInFlight = false;
    scheduleProbe();
    return;
  }
  ws.onopen = async () => {
    connectInFlight = false;
    console.log('[TMWD-WS] Connected!');
    // Seed the pong clock so the zombie watchdog has a baseline; the bridge's
    // first pong (within a keepalive tick) refreshes it. Without a seed the
    // watchdog's `lastPongAt &&` guard would never arm.
    lastPongAt = Date.now();
    scheduleKeepalive(); // Keep SW alive while connected
    try {
      const clientId = await getClientId();
      const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url));
      ws.send(JSON.stringify({
        type: 'ext_ready',
        clientId,
        browser: getBrowserType(),
        tabs: tabs.map(t => ({ id: t.id, url: t.url, title: t.title }))
      }));
      console.log('[TMWD-WS] Sent ext_ready with', tabs.length, 'tabs as', clientId);
    } catch (e) {
      console.error('[TMWD-WS] ext_ready failed', e);
    }
  };
  ws.onmessage = async (event) => {
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
          ws.send(JSON.stringify({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error }));
        } else if (typeof code === 'string') {
          // Plain JS code
          await handleWsExec(data);
        } else if (typeof code === 'object' && code !== null) {
          // Object without cmd → legacy extension message
          const msg = code.tabId === undefined && data.tabId !== undefined ? { ...code, tabId: data.tabId } : code;
          const res = await handleExtMessage(msg, {});
          ws.send(JSON.stringify({ type: res.ok ? 'result' : 'error', id: data.id, result: res.data ?? res.results ?? res, error: res.error }));
        }
      }
    } catch (e) {
      console.error('[TMWD-WS] message parse error', e);
    }
  };
  ws.onclose = () => {
    console.log('[TMWD-WS] Disconnected');
    ws = null;
    connectInFlight = false;
    scheduleProbe();
  };
  ws.onerror = (e) => {
    console.error('[TMWD-WS] Error:', e);
    // onclose will fire after this, which triggers reconnect
  };
}

// Initial connect + wake-up hooks
ensureConnected('boot');
chrome.runtime.onStartup.addListener(() => ensureConnected('onStartup'));
chrome.runtime.onInstalled.addListener(() => ensureConnected('onInstalled'));
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
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const tabs = (await chrome.tabs.query({})).filter(t => isScriptable(t.url) && !/streamlit/i.test(t.title));
  try {
    const clientId = await getClientId();
    ws.send(JSON.stringify({
      type: 'tabs_update',
      clientId,
      browser: getBrowserType(),
      tabs: tabs.map(t => ({ id: t.id, url: t.url, title: t.title }))
    }));
  } catch (e) {
    console.error('[TMWD-WS] tabs_update send failed', e);
  }
}
chrome.tabs.onUpdated.addListener((_, changeInfo) => {
  if (changeInfo.status === 'complete' || changeInfo.url) sendTabsUpdate();
});
chrome.tabs.onRemoved.addListener(() => sendTabsUpdate());
chrome.tabs.onCreated.addListener(() => sendTabsUpdate());
chrome.tabs.onActivated.addListener(() => ensureConnected('tab-activated'));

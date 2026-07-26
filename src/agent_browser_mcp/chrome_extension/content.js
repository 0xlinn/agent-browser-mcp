;(function(){ if (/streamlit/i.test(document.title)) return;

// Idempotency guard: onInstalled re-injects this script into already-open
// tabs (to rebind orphaned content scripts after an extension reload). Without
// this, each reload would stack another badge + MutationObserver + 5s interval
// on top of the dead ones. A window flag is per-execution-world, so a fresh
// injection into the same page still sees the previous run's flag and bails.
if (window.__tmwd_content_loaded) return;
window.__tmwd_content_loaded = true;

// Remove meta CSP tags
document.querySelectorAll('meta[http-equiv="Content-Security-Policy"]').forEach(e => e.remove());

// Indicator badge — reflects real WS status via background (green=bridge, orange=inject-only)
(function(){
  if(window.self!==window.top)return;
  // Drop any badge left by an orphaned older injection so we don't double up.
  const stale = document.getElementById('ljq-ind');
  if (stale) stale.remove();
  const d=document.createElement('div');
  d.id='ljq-ind';
  d.innerText='ljq_driver: 检测中';
  d.style.cssText='position:fixed;bottom:8px;right:8px;background:#888;color:white;padding:4px 7px;border-radius:4px;font-size:11px;font-weight:bold;z-index:99999;cursor:pointer;box-shadow:0 2px 4px rgba(0,0,0,0.2);opacity:0.85;';
  function paint(ok){
    d.innerText = ok ? 'ljq_driver: 已连接' : 'ljq_driver: 桥未连';
    d.style.background = ok ? '#4CAF50' : '#e67e22';
  }
  // An orphaned content script (extension reloaded under it) throws
  // "Extension context invalidated" on any chrome.* call. Tell the user to
  // refresh instead of silently sticking on "检测中" forever.
  function orphaned(){
    d.innerText = 'ljq_driver: 请刷新页面';
    d.style.background = '#c0392b';
  }
  function poll(withAlert){
    try {
      chrome.runtime.sendMessage({cmd:'tmwd_ping'}, (r)=>{
        if (chrome.runtime.lastError) { orphaned(); return; }
        paint(!!(r && r.ws));
        if (withAlert) alert((r && r.ws ? '桥已连接' : '桥未连接') + '\nURL: '+location.href+'\nreadyState='+(r?r.readyState:'?'));
      });
    } catch(e) { orphaned(); }
  }
  d.addEventListener('click',()=>poll(true));
  (document.body||document.documentElement).appendChild(d);
  // Long-lived port: re-opening a port is a stronger, cheaper SW wake signal
  // than sendMessage, and it lets a dead SW re-instantiate + reconnect the
  // bridge on its own — this is what turns "SW slept, must reload extension"
  // into "self-heals within 5s". The ping below just reads status for the badge.
  function wakeSW(){
    try {
      const p = chrome.runtime.connect({ name: 'tmwd_keepalive' });
      // Swallow the disconnect; the next poll re-opens. Reading lastError here
      // avoids an unchecked-error console warning when the SW is mid-restart.
      p.onDisconnect.addListener(()=>{ void chrome.runtime.lastError; });
    } catch(_) { /* orphaned; poll() will paint the refresh hint */ }
  }
  wakeSW();
  poll(false);
  setInterval(()=>{ wakeSW(); poll(false); }, 5000);
})();

new MutationObserver(muts => {
  for (const m of muts) for (const n of m.addedNodes) {
    if (n.id === TID || (n.querySelector && n.querySelector('#' + TID))) {
      const el = n.id === TID ? n : n.querySelector('#' + TID);
      handle(el);
    }
  }
}).observe(document.documentElement, { childList: true, subtree: true });

async function handle(el) {
  try {
    const req = el.textContent.trim() ? JSON.parse(el.textContent) : { cmd: 'cookies' };
    const cmd = req.cmd || 'cookies';
    let resp;
    if (cmd === 'cookies') {
      resp = await chrome.runtime.sendMessage({ cmd: 'cookies', url: req.url || location.href });
    } else if (cmd === 'cdp') {
      resp = await chrome.runtime.sendMessage({ cmd: 'cdp', method: req.method, params: req.params || {}, tabId: req.tabId });
    } else if (cmd === 'batch') {
      resp = await chrome.runtime.sendMessage({ cmd: 'batch', commands: req.commands, tabId: req.tabId });
    } else if (cmd === 'tabs') {
      resp = await chrome.runtime.sendMessage({ cmd: 'tabs', method: req.method, tabId: req.tabId });
    } else if (cmd === 'bookmarks') {
      resp = await chrome.runtime.sendMessage({
        cmd: 'bookmarks',
        method: req.method,
        id: req.id,
        node: req.node,
        destination: req.destination,
        changes: req.changes,
        rootId: req.rootId,
        folderPaths: req.folderPaths,
        assignments: req.assignments,
        removeFolderIds: req.removeFolderIds,
        creates: req.creates,
        updates: req.updates,
        ids: req.ids
      });
    } else if (cmd === 'management') {
      resp = await chrome.runtime.sendMessage({ cmd: 'management', method: req.method, extId: req.extId, showConfirmDialog: req.showConfirmDialog });
    } else {
      resp = { ok: false, error: 'unknown cmd: ' + cmd };
    }
    el.textContent = JSON.stringify(resp);
  } catch (e) {
    el.textContent = JSON.stringify({ ok: false, error: e.message });
  }
}
})();

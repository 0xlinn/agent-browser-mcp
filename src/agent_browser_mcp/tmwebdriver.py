import json, threading, time, uuid, queue, socket, requests, traceback
from typing import Dict, Any, Optional, List  
from simple_websocket_server import WebSocketServer, WebSocket  
from bs4 import BeautifulSoup  
import bottle, random
from bottle import route, template, request, response

class Session:
    def __init__(self, session_id, info, client=None):
        self.id = session_id
        self.info = info
        self.connect_at = time.time()
        self.disconnect_at = None
        self.type = info.get('type', 'ws')
        self.ws_client = client if self.type in ('ws', 'ext_ws') else None
        self.http_queue = client if self.type == 'http' else None
    @property
    def url(self): return self.info.get('url', '')
    def is_active(self):
        if self.type == 'http' and time.time() - self.connect_at > 60: self.mark_disconnected()
        return self.disconnect_at is None
    def reconnect(self, client, info):
        self.info = info
        self.type = info.get('type', 'ws')
        if self.type in ('ws', 'ext_ws'):
            self.ws_client = client
            self.http_queue = None
        elif self.type == 'http':
            self.http_queue = client
        self.connect_at = time.time()
        self.disconnect_at = None
    def mark_disconnected(self):
        print(f"Tab disconnected: {self.url} (Session: {self.id})")
        self.disconnect_at = time.time()


class TMWebDriver:  
    def __init__(self, host: str = '127.0.0.1', port: int = 18765):  
        self.host, self.port = host, port
        self.sessions, self.results, self.acks = {}, {}, {}
        self.default_session_id = None
        self.latest_session_id = None
        # Last time ANY extension pushed ext_ready/tabs_update. Lets `doctor`
        # tell "extension never registered" (0 or stale) from "registered but
        # just dropped" — the single most useful signal for classifying why
        # /link is empty. None until the first registration this daemon sees.
        self.last_ext_seen = None
        # Per-client last-seen: which browser is stale vs which just dropped.
        self.client_last_seen = {}
        # client_id -> {'ws','browser','ts'}: the extension's own socket, one per
        # browser, independent of how many tabs exist. Addresses the service
        # worker directly for tab-less commands; see ext_cmd().
        self.ext_clients = {}
        with socket.socket() as _probe:
            _probe.settimeout(1)
            self.is_remote = _probe.connect_ex((host, port+1)) == 0
        if not self.is_remote:
            # Both bundled servers set SO_REUSEADDR, which on Windows lets a
            # second process bind the very same ports and steal a share of the
            # connections (extension on one host, /link on the other -> lost
            # results). An exclusive lock socket on port+2 guarantees exactly
            # one host; losers wait for the winner and go remote.
            self._host_lock = self._acquire_host_lock()
            if self._host_lock is None:
                for _ in range(20):
                    time.sleep(0.25)
                    with socket.socket() as s:
                        s.settimeout(1)
                        if s.connect_ex((host, port + 1)) == 0: break
                self.is_remote = True
        if not self.is_remote:
            self.start_ws_server()
            self.start_http_server()
        else:
            self.remote = f'http://{self.host}:{self.port+1}/link'
            # trust_env=False: HTTP(S)_PROXY env vars (no NO_PROXY) would route
            # loopback bridge calls through the system proxy — a restart there
            # surfaces as 502/refused, i.e. phantom bridge outages. Session
            # also gives us connection pooling for repeated calls.
            self._http = requests.Session()
            self._http.trust_env = False

    def _acquire_host_lock(self):
        s = socket.socket()
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((self.host, self.port + 2))
            s.listen(1)
            return s  # held open for the process lifetime
        except OSError:
            s.close()
            return None

    def start_http_server(self):
        self.app = app = bottle.Bottle()

        @app.route('/api/longpoll', method=['GET', 'POST'])
        def long_poll():
            data = request.json
            session_id = data.get('sessionId')  
            session_info = {'url': data.get('url'), 'title': data.get('title', ''), 'type': 'http'}  
            if session_id not in self.sessions: 
                session = Session(session_id, session_info, queue.Queue())
                print(f"Browser http connected: {session.url} (Session: {session_id})")  
                self.sessions[session_id] = session
            session = self.sessions[session_id]
            if session.disconnect_at is not None and session.type != 'http': session.reconnect(queue.Queue(), session_info)
            session.disconnect_at = None
            if session.type == 'http': msgQ = session.http_queue
            else: return json.dumps({"id": "", "ret": "use ws"})
            session.connect_at = start_time = time.time()
            while time.time() - start_time < 5:
                try:
                    msg = msgQ.get(timeout=0.2)
                    try: self.acks[json.loads(msg).get('id','')] = time.time()
                    except: traceback.print_exc()
                    return msg
                except queue.Empty: continue
            return json.dumps({"id": "", "ret": "next long-poll"})

        @app.route('/api/result', method=['GET','POST'])
        def result():
            data = request.json
            if data.get('type') == 'result':
                self.results[data.get('id')] = {'success': True, 'data': data.get('result'), 'newTabs': data.get('newTabs', []), 'ts': time.time()}
            elif data.get('type') == 'error':
                self.results[data.get('id')] = {'success': False, 'data': data.get('error'), 'newTabs': data.get('newTabs', []), 'ts': time.time()}
            return 'ok'

        @app.route('/link', method=['GET','POST'])
        def link():
            data = request.json
            if data.get('cmd') == 'get_all_sessions': return json.dumps({'r': self.get_all_sessions()}, ensure_ascii=False)
            if data.get('cmd') == 'diagnose': return json.dumps({'r': self.diagnose()}, ensure_ascii=False)
            if data.get('cmd') == 'find_session': 
                url_pattern = data.get('url_pattern', '')
                return json.dumps({'r': self.find_session(url_pattern)}, ensure_ascii=False)
            if data.get('cmd') == 'ext_cmd':
                try:
                    result = self.ext_cmd(data.get('payload') or {},
                                          client_id=data.get('clientId'),
                                          timeout=float(data.get('timeout', 15.0)))
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': str(e)}}, ensure_ascii=False)
            if data.get('cmd') == 'execute_js':
                session_id = data.get('sessionId')
                code = data.get('code')
                timeout = float(data.get('timeout', 10.0))
                try:
                    result = self.execute_js(code, timeout=timeout, session_id=session_id)
                    print('[remote result]', (str(code)[:50] + ' RESULT:' +str(result)[:50]).replace('\n', ' '))
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': str(e)}}, ensure_ascii=False)
            return 'ok'
        def run():
            from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler
            from socketserver import ThreadingMixIn
            class _T(ThreadingMixIn, WSGIServer): pass
            class _H(WSGIRequestHandler):
                def log_request(self, *a): pass
            make_server(self.host, self.port+1, app, server_class=_T, handler_class=_H).serve_forever()
        http_thread = threading.Thread(target=run, daemon=True)
        http_thread.start()  

    def clean_sessions(self):
        now = time.time()
        # Snapshot keys, then re-fetch with .get: another thread (WS handler or a
        # concurrent clean_sessions from a parallel /link call) may delete a
        # session between the snapshot and access, so both the read and the
        # delete must tolerate a missing key instead of raising KeyError.
        for sid in list(self.sessions.keys()):
            session = self.sessions.get(sid)
            if session is None: continue
            if not session.is_active() and session.disconnect_at is not None \
                    and now - session.disconnect_at > 600:
                self.sessions.pop(sid, None)
        # Results/acks that arrive after their caller already timed out would
        # otherwise accumulate forever in a long-lived daemon.
        for r_id in list(self.results.keys()):
            entry = self.results.get(r_id)
            if isinstance(entry, dict) and now - entry.get('ts', now) > 600:
                self.results.pop(r_id, None)
        for a_id in list(self.acks.keys()):
            ts = self.acks.get(a_id)
            if not isinstance(ts, float) or now - ts > 600:
                self.acks.pop(a_id, None)
        # Per-client heartbeat stamps leak otherwise: client_id is random per
        # extension install/profile, so every reinstall adds a new key that
        # never goes away. Drop stamps for clients with no active session and
        # no heartbeat in 10 min.
        live_clients = {s.info.get('client_id') for s in self.sessions.values()
                        if s.is_active() and s.info.get('client_id')}
        for cid in list(self.client_last_seen.keys()):
            entry = self.client_last_seen.get(cid)
            if cid not in live_clients and (not isinstance(entry, dict)
                    or now - entry.get('ts', now) > 600):
                self.client_last_seen.pop(cid, None)

    def start_ws_server(self) -> None:  
        driver = self  
        class JSExecutor(WebSocket):  
            def handle(self) -> None:  
                try:  
                    data = json.loads(self.data)  
                    if data.get('type') == 'ready':  
                        session_id = data.get('sessionId')  
                        session_info = {'url': data.get('url'), 'title': data.get('title', ''),
                            'connected_at': time.time(), 'type': 'ws'}  
                        driver._register_client(session_id, self, session_info)  
                    elif data.get('type') in ['ext_ready', 'tabs_update']:
                        tabs = data.get('tabs', [])
                        # Namespace sessions per browser instance so Chrome/Edge (and
                        # multiple profiles) don't collide on identical small tab ids.
                        # Fall back to a per-connection uuid if an older extension
                        # doesn't send clientId — id(self) is unusable here because
                        # addresses get reused after GC, colliding namespaces.
                        client_id = data.get('clientId')
                        if not client_id:
                            if not hasattr(self, '_fallback_cid'):
                                self._fallback_cid = f"conn_{uuid.uuid4().hex[:10]}"
                            client_id = self._fallback_cid
                        browser = data.get('browser', '')
                        driver.last_ext_seen = time.time()
                        if client_id: driver.client_last_seen[client_id] = {'ts': time.time(), 'browser': browser}
                        # Keep the CLIENT-level socket. It is one per browser and
                        # exists regardless of tab count, so SW-side commands
                        # (chrome.tabs.create) still work with zero open tabs —
                        # per-tab sessions can't express that.
                        if client_id:
                            driver.ext_clients[client_id] = {'ws': self, 'browser': browser, 'ts': time.time()}
                        def _sid(tab_id): return f"{client_id}:{tab_id}"
                        current_tab_ids = {_sid(tab['id']) for tab in tabs}
                        print(f"Received tabs update from {client_id} ({browser}): {current_tab_ids}")
                        # Only sweep sessions belonging to THIS client; another
                        # browser's update must not disconnect this browser's tabs.
                        for sid in list(driver.sessions.keys()):
                            sess = driver.sessions[sid]
                            if sess.type == 'ext_ws' and sess.info.get('client_id') == client_id and sid not in current_tab_ids:
                                sess.mark_disconnected()
                        for tab in tabs:
                            session_id = _sid(tab['id'])
                            session_info = {'url': tab.get('url'), 'title': tab.get('title', ''),
                                'connected_at': time.time(), 'type': 'ext_ws',
                                'client_id': client_id, 'browser': browser, 'tab_id': tab['id']}
                            sess = driver.sessions.get(session_id)
                            if sess and sess.is_active(): sess.info = session_info
                            else: driver._register_client(session_id, self, session_info)
                    elif data.get('type') == 'ping':
                        # Liveness reply so the extension can tell a live socket
                        # from a half-open zombie (TCP ESTABLISHED but dead). No
                        # pong within a couple keepalive ticks => extension force-
                        # reconnects instead of pushing tabs into a black hole.
                        try: self.send_message(json.dumps({'type': 'pong'}))
                        except Exception: pass
                    elif data.get('type') == 'ack': driver.acks[data.get('id','')] = time.time()
                    elif data.get('type') == 'result':
                        driver.results[data.get('id')] = {'success': True, 'data': data.get('result'), 'newTabs': data.get('newTabs', []), 'ts': time.time()}
                    elif data.get('type') == 'error':
                        driver.results[data.get('id')] = {'success': False, 'data': data.get('error'), 'newTabs': data.get('newTabs', []), 'ts': time.time()}
                except Exception as e:  
                    print(f"Error handling message: {e}")  
                    if hasattr(self, 'data'): print(self.data)  
            def connected(self): (f"New connection from {self.address}")  
            def handle_close(self): 
                print(f"WS Connection closed: {self.address}")
                driver._unregister_client(self)  
        
        # First bind stays in the caller's thread so startup failures are loud.
        self.server = WebSocketServer(self.host, self.port, JSExecutor)

        def run():
            # serve_forever has no internal guard: any exception that escapes
            # the poll loop silently kills the thread while the LISTEN socket
            # keeps accepting into the kernel backlog - clients show
            # ESTABLISHED but no handshake ever completes. Rebuild and go on.
            srv = self.server
            while True:
                try:
                    srv.serve_forever()
                except Exception:
                    print("WS server loop crashed; rebuilding in 1s")
                    traceback.print_exc()
                try:
                    srv.close()
                except Exception:
                    pass
                time.sleep(1)
                try:
                    srv = self.server = WebSocketServer(self.host, self.port, JSExecutor)
                    print(f"WS server rebuilt on ws://{self.host}:{self.port}")
                except Exception:
                    traceback.print_exc()
                    time.sleep(2)

        server_thread = threading.Thread(target=run)
        server_thread.daemon = True
        server_thread.start()
        print(f"WebSocket server running on ws://{self.host}:{self.port}")
    
    def _register_client(self, session_id: str, client: WebSocket, session_info) -> None:  
        is_new_session = session_id not in self.sessions

        if is_new_session:
            session = Session(session_id, session_info, client)
            self.sessions[session_id] = session            
            print(f"New tab connected: {session.url} (Session: {session_id})")  
        else:
            session = self.sessions[session_id]
            session.reconnect(client, session_info)
            print(f"Tab reconnected: {session.url} (Session: {session_id})")  

        self.latest_session_id = session_id
        if self.default_session_id is None: self.default_session_id = session_id 
    
    def _unregister_client(self, client: WebSocket) -> None:
        for session in self.sessions.values():
            if session.ws_client == client: session.mark_disconnected()
        # Drop the client-level socket too, else ext_cmd would keep writing into
        # a closed connection instead of failing over to a live browser.
        for cid in [c for c, v in self.ext_clients.items() if v.get('ws') is client]:
            self.ext_clients.pop(cid, None)
    
    def execute_js(self, code, timeout=15, session_id=None) -> Any:  
        if session_id is None: session_id = self.default_session_id  
        if self.is_remote:
            print('remote_execute_js')
            # HTTP timeout must outlast the JS timeout or long scripts die at
            # the transport layer before the bridge can answer.
            response = self._remote_cmd({"cmd": "execute_js", "sessionId": session_id,
                                         "code": code, "timeout": str(timeout)},
                                        timeout=float(timeout) + 10).get('r', {})
            if response.get('error'): raise Exception(response['error'])
            return response
 
        switched_from = None
        session = self.sessions.get(session_id)
        if not session or not session.is_active():
            time.sleep(3)
            session = self.sessions.get(session_id)
            if not session or not session.is_active():
                alive_sessions = [s for s in self.sessions.values() if s.is_active()]
                if alive_sessions:
                    # Failover must not silently jump browsers: prefer a tab from
                    # the same client namespace (client_id:tab_id) as the dead one.
                    want_client = str(session_id).rsplit(':', 1)[0] if session_id and ':' in str(session_id) else None
                    same_client = [s for s in alive_sessions if want_client and str(s.id).startswith(want_client + ':')]
                    pool = same_client or alive_sessions
                    latest = self.sessions.get(self.latest_session_id)
                    session = latest if latest in pool else pool[-1]
                    print(f"会话 {session_id} 未连接，自动切换到活动会话: {session.id}")
                    switched_from = session_id
                    session_id = self.default_session_id = session.id
                if not session or not session.is_active():
                    raise ValueError(f"会话ID {session_id} 未连接")
        # Callers (and the AI driving them) must learn their target changed.
        extra = {'switched_session': session_id, 'switched_from': switched_from} if switched_from else {}

        tp = session.type
        assert tp in ['ws', 'http', 'ext_ws'], f"Unsupported session type: {tp}"
        exec_id = str(uuid.uuid4())  
        payload_dict = {'id': exec_id, 'code': code}
        if tp == 'ext_ws':
            # session.id is now "client_id:tab_id"; use the raw browser tab id.
            payload_dict['tabId'] = int(session.info.get('tab_id', str(session.id).rsplit(':', 1)[-1]))
        payload = json.dumps(payload_dict)

        if tp in ['ws', 'ext_ws']:
            try: session.ws_client.send_message(payload)
            except Exception as e:
                # Half-open socket (e.g. after system sleep): mark it dead so
                # the next call fails over instead of hitting it again.
                session.mark_disconnected()
                raise ValueError(f"会话 {session_id} 连接已失效({e})，已标记断开，请重试")
        elif tp == 'http': session.http_queue.put(payload)

        start_time = time.time()  
        self.clean_sessions() 
        hasjump = acked = False

        while exec_id not in self.results:  
            time.sleep(0.2)  
            if not acked and exec_id in self.acks:
                acked = True; start_time = time.time()
            if tp in ['ws', 'ext_ws']:
                if not session.is_active(): hasjump = True
                if hasjump and session.is_active():
                    return {'result': f"Session {session_id} reloaded.", "closed":1, **extra}
            if time.time() - start_time > timeout:
                if tp in ['ws', 'ext_ws']:
                    if hasjump: return {'result': f"Session {session_id} reloaded and new page is loading...", 'closed':1, **extra}
                    if acked: return {"result": f"No response data in {timeout}s (ACK received, script may still be running)", **extra}
                    return {"result": f"No response data in {timeout}s (no ACK, script may not have been delivered)", **extra}
                elif tp == 'http':
                    if acked: return {"result": f"Session {session_id} no response in {timeout}s (delivered but no result)", **extra}
                    return {"result": f"Session {session_id} no response in {timeout}s (script not polled)", **extra}
        
        result = self.results.pop(exec_id)  
        if exec_id in self.acks: self.acks.pop(exec_id)
        if not result['success']: raise Exception(result['data'])  
        rr = {'data': result['data'], **extra}
        newtabs = result.get('newTabs', []); [x.pop('ts', None) for x in newtabs]
        if newtabs: rr['newTabs'] = newtabs
        return rr
    
    def ext_cmd(self, cmd: dict, client_id=None, timeout=15):
        """Send a command straight to an extension service worker.

        The extension's socket is per-browser, so this works with ZERO open
        tabs: background.js routes any payload carrying `.cmd` to
        handleExtMessage, which runs in the SW. Plain JS still needs a tab
        (handleWsExec requires tabId) — that asymmetry is the whole point.
        """
        if self.is_remote:
            response = self._remote_cmd({"cmd": "ext_cmd", "payload": cmd,
                                         "clientId": client_id, "timeout": str(timeout)},
                                        timeout=float(timeout) + 10).get('r', {})
            if response.get('error'): raise Exception(response['error'])
            return response

        if client_id is None:
            # Prefer the browser the caller already works in, so a stray command
            # can't open tabs in the other browser.
            want = (str(self.default_session_id).rsplit(':', 1)[0]
                    if self.default_session_id and ':' in str(self.default_session_id) else None)
            if want and want in self.ext_clients: client_id = want
            elif self.ext_clients:
                client_id = max(self.ext_clients.items(), key=lambda kv: kv[1].get('ts', 0))[0]
        entry = self.ext_clients.get(client_id)
        if not entry:
            raise ValueError("没有已连接的扩展(ext_clients 为空):检查扩展是否加载、桥是否在运行")

        exec_id = str(uuid.uuid4())
        try:
            entry['ws'].send_message(json.dumps({'id': exec_id, 'code': cmd}))
        except Exception as e:
            self.ext_clients.pop(client_id, None)
            raise ValueError(f"扩展 {client_id} 连接已失效({e})，已丢弃,请重试")

        start_time = time.time()
        while exec_id not in self.results:
            time.sleep(0.2)
            if time.time() - start_time > timeout:
                return {'result': f"扩展 {client_id} 在 {timeout}s 内无响应"}
        result = self.results.pop(exec_id)
        self.acks.pop(exec_id, None)
        if not result['success']: raise Exception(result['data'])
        return {'data': result['data']}

    def _remote_cmd(self, cmd, timeout=30):
        return self._http.post(self.remote, headers={"Content-Type": "application/json"}, json=cmd, timeout=timeout).json()

    def get_all_sessions(self, timeout=None):
        if self.is_remote:
            return self._remote_cmd({"cmd": "get_all_sessions"}, timeout=timeout or 30).get('r', [])
        self.clean_sessions()
        return [{'id': session.id, **session.info} for session in self.sessions.values()
                if session.is_active()]

    def get_session_dict(self, timeout=None):
        return {session['id']: session['url'] for session in self.get_all_sessions(timeout=timeout)}

    def diagnose(self, timeout=None):
        """Classify why the bridge might be unusable, mapping to SKILL.md causes.

        Returns a dict the CLI/MCP `doctor` prints so the user never has to run
        netstat + curl + read the badge by hand. `cause` is the machine-readable
        verdict; `advice` is the one-line human fix.
        """
        if self.is_remote:
            # We're a thin client; ask the real host for its self-diagnosis.
            try:
                return self._remote_cmd({"cmd": "diagnose"}, timeout=timeout or 6).get('r', {})
            except Exception as e:
                return {"cause": "bridge_unreachable", "ok": False,
                        "advice": "桥守护无响应/端口拒绝 → 通常自动拉起；否则手动跑 python -m agent_browser_mcp.bridge (SKILL 成因1)",
                        "error": str(e)}
        self.clean_sessions()
        now = time.time()
        active = [s for s in self.sessions.values() if s.is_active()]
        ever = self.last_ext_seen is not None
        stale = ever and (now - self.last_ext_seen) > 90
        per_client = {cid: {"browser": v.get("browser", ""), "seconds_ago": round(now - v["ts"], 1)}
                      for cid, v in self.client_last_seen.items()}
        if active:
            cause, ok, advice = "healthy", True, f"{len(active)} 个 tab 已注册，桥+扩展都通。"
        elif not ever:
            cause, ok, advice = "ext_never_registered", False, (
                "扩展从没连过桥：① chrome://extensions 看有无红色 Errors(成因2a storage权限) "
                "② 扩展是否被禁用(2b) ③ 是否没开任何 http(s) 页面。")
        elif stale:
            cause, ok, advice = "sw_slept_or_dropped", False, (
                f"扩展曾注册但已 {round(now - self.last_ext_seen)}s 无心跳：SW 睡死(2d)或半开僵尸(2c)，"
                "两者修复动作相同——开着的正常网页应 5s 内自愈；否则 chrome://extensions 点 ↻ reload。")
        else:
            cause, ok, advice = "registering", False, (
                "扩展刚有心跳但当前 0 活跃 tab：正在注册/所有 tab 都是 chrome:// 或空白页，稍等或打开一个 http(s) 页面。")
        return {
            "cause": cause, "ok": ok, "advice": advice,
            "active_tabs": len(active),
            "ever_registered": ever,
            "last_ext_seen_seconds_ago": round(now - self.last_ext_seen, 1) if ever else None,
            "clients": per_client,
        }

    def find_session(self, url_pattern: str):
        if url_pattern == '':
            session = self.sessions.get(self.latest_session_id)
            return [(session.id, session.info)] if session and session.is_active() else []
        matching_sessions = []  
        for session in self.sessions.values():
            if not session.is_active(): continue
            if 'url' in session.info and url_pattern in session.info['url']:  
                matching_sessions.append((session.id, session.info))  
        return matching_sessions

    def set_session(self, url_pattern: str) -> bool:  
        if self.is_remote:
            matched = self._remote_cmd({"cmd": "find_session", "url_pattern": url_pattern}).get('r', [])
        else:
            matched = self.find_session(url_pattern)
        if not matched: return print(f"警告: 未找到URL包含 '{url_pattern}' 的会话")  
        if len(matched) > 1: print(f"警告: 找到多个URL包含 '{url_pattern}' 的会话，选择第一个")  
        self.default_session_id, info = matched[0]
        print(f"成功设置默认会话: {self.default_session_id}: {info['url']}")  
        return self.default_session_id  
    
    def jump(self, url, timeout=10): self.execute_js(f"window.location.href={json.dumps(url)}", timeout=timeout)
    def newtab(self, url=None):
        if url is None: url = "http://www.baidu.com/robots.txt"
        # Native chrome.tabs.create, addressed to the extension itself so it
        # works with no tabs open. (The old GM_openInTab was a Tampermonkey-only
        # API absent from a plain extension, so it always threw ReferenceError.)
        payload = {"cmd": "tabs", "method": "create", "url": url}
        try:
            return self.ext_cmd(payload)
        except Exception as e:
            # Older bridge daemon without /link ext_cmd: fall back to the tab
            # session route, which still works whenever at least one tab lives.
            print(f"ext_cmd 不可用({e})，回退到 session 通道")
            return self.execute_js(json.dumps(payload))
    
if __name__ == "__main__":
    driver = TMWebDriver(host='127.0.0.1', port=18765)
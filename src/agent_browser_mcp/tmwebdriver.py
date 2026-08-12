import json, os, threading, time, uuid, queue, socket, requests, traceback, hmac, secrets
from pathlib import Path
from typing import Dict, Any, Optional, List
from simple_websocket_server import WebSocketServer, WebSocket
from bs4 import BeautifulSoup
import bottle, random
from bottle import route, template, request, response

# --- /link 鉴权 --------------------------------------------------------------
# WS 口靠 origin 前缀挡住网页（扩展读不到磁盘上的密钥，只能这么做）；但 /link 是
# 命令通道，本机任意进程都能 POST 一条 execute_js 在用户登录态的 Chrome 里跑任意
# JS。ABM 默认用用户目录中的持久 token 鉴权；旧环境变量只用于一次迁移。
TOKEN_ENV = 'AGENT_BROWSER_BRIDGE_TOKEN'
TOKEN_FILE_ENV = 'AGENT_BROWSER_BRIDGE_TOKEN_FILE'
TOKEN_AUTH_ENV = 'AGENT_BROWSER_BRIDGE_AUTH'


def bridge_token_path() -> Path:
    """Return the one persistent token location shared by all ABM processes."""
    configured = (os.environ.get(TOKEN_FILE_ENV) or '').strip()
    return Path(configured).expanduser() if configured else Path.home() / '.agent-browser-mcp' / 'bridge-token'


def _read_token_file(path: Path) -> str:
    try:
        value = path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return ''
    return value


def _persist_token(path: Path, token: str) -> str:
    """Create the token file once; concurrent starters converge on its value."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (token + '\n').encode('utf-8'))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return token
    except FileExistsError:
        # O_EXCL exposes the file just before the winning process writes its
        # contents. Give that tiny window time to close instead of inventing a
        # different in-memory token that would immediately split the clients.
        for _ in range(20):
            stored = _read_token_file(path)
            if stored:
                return stored
            time.sleep(0.01)
        raise RuntimeError(f'ABM bridge token file is empty: {path}')
    except OSError as exc:
        raise RuntimeError(f'ABM cannot persist its bridge token at {path}: {exc}') from exc


def bridge_token() -> str:
    """Read or create the stable per-user token shared by every ABM process.

    The old env var is a one-time bootstrap for existing installs. Once the file
    exists, later editor environments cannot rotate or replace it. Authentication
    can only be disabled deliberately via ``AGENT_BROWSER_BRIDGE_AUTH=off``.
    """
    auth_mode = (os.environ.get(TOKEN_AUTH_ENV) or '').strip().lower()
    if auth_mode in {'0', 'false', 'off', 'disabled'}:
        return ''
    path = bridge_token_path()
    stored = _read_token_file(path)
    if stored:
        return stored
    legacy = (os.environ.get(TOKEN_ENV) or '').strip()
    return _persist_token(path, legacy or secrets.token_urlsafe(32))


def header_token(headers) -> str:
    """从 Authorization: Bearer <t> 或 X-Bridge-Token 取 token，取不到返回 ''。

    两个头都收：Authorization 是常规写法，X-Bridge-Token 留给设不了
    Authorization 的调用方（某些 userscript / 反代）。
    """
    auth = (headers.get('Authorization', '') or '').strip()
    if auth[:7].lower() == 'bearer ':
        return auth[7:].strip()
    return (headers.get('X-Bridge-Token', '') or '').strip()


def check_link_token(headers, want: str) -> None:
    """token 不对就抛 401。want 为空仅表示鉴权被显式关闭。"""
    if not want:
        return
    got = header_token(headers)
    # compare_digest 而非 ==：避免按字节提前返回泄露前缀。
    if not got or not hmac.compare_digest(got, want):
        raise bottle.HTTPResponse(
            status=401, body='unauthorized: missing or bad bridge token')


# 单参数版：want 从共享文件现读。测试与外部调用方按此名引用；桥内部用
# check_link_token(headers, want) 传已锁定的 token。
def require_link_token(headers) -> None:
    check_link_token(headers, bridge_token())


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
        # 启动时锁定一次。所有进程从同一个持久文件取 token；运行期间若有人做完整
        # 用户数据清理，旧 daemon 仍保持原值，必须在清理前先停止它。
        self.link_token = bridge_token()
        if self.link_token:
            print(f"/link 已启用 token 鉴权（{bridge_token_path()}）")

        @app.hook('before_request')
        def _reject_cross_origin():
            # These routes execute JS in the user's logged-in tabs, so nothing
            # that carries a web Origin may reach them. In practice bottle's
            # request.json requires Content-Type: application/json, which forces
            # a CORS preflight this server never answers — but that is a
            # side effect of the parser, not a decision. Make it explicit, and
            # deliberately send no Access-Control-* headers so no browser ever
            # gets permission to read a response.
            origin = request.headers.get('Origin', '') or ''
            if origin and not origin.startswith(
                    ('chrome-extension://', 'moz-extension://',
                     'safari-web-extension://', 'extension://')):
                extra = os.environ.get('AGENT_BROWSER_WS_ALLOWED_ORIGINS', '')
                if not any(origin == o.strip() for o in extra.split(',') if o.strip()):
                    raise bottle.HTTPResponse(status=403, body='forbidden origin')

        @app.route('/api/longpoll', method=['GET', 'POST'])
        def long_poll():
            # 结果回传通道与 /link 同源：token 已配置时必须同样鉴权，否则本机
            # 任意进程可在无 Origin 头的情况下注册伪造会话（本地纵深防御）。
            # 显式关闭鉴权时保持旧版 userscript 轮询客户端兼容。
            check_link_token(request.headers, self.link_token)
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
            # 与 /api/longpoll 同理：token 已配置时伪造结果注入同样需要鉴权。
            check_link_token(request.headers, self.link_token)
            data = request.json
            if data.get('type') == 'result':
                self.results[data.get('id')] = {'success': True, 'data': data.get('result'), 'newTabs': data.get('newTabs', []), 'tabId': data.get('tabId'), 'ts': time.time()}
            elif data.get('type') == 'error':
                self.results[data.get('id')] = {'success': False, 'data': data.get('error'), 'newTabs': data.get('newTabs', []), 'tabId': data.get('tabId'), 'ts': time.time()}
            return 'ok'

        @app.route('/link', method=['GET','POST'])
        def link():
            # 命令通道，先鉴权再解析 body；仅显式关闭鉴权时是 no-op。
            check_link_token(request.headers, self.link_token)
            data = request.json
            if not isinstance(data, dict):
                return json.dumps({'r': {'error': 'body 必须是带 "cmd" 的 JSON 对象'}},
                                  ensure_ascii=False)
            cmd = data.get('cmd')
            if cmd == 'get_all_sessions':
                try:
                    return json.dumps({'r': self.get_all_sessions()}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': f'get_all_sessions 内部错误: {e}'}},
                                      ensure_ascii=False)
            if cmd == 'diagnose':
                try:
                    return json.dumps({'r': self.diagnose()}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': f'diagnose 内部错误: {e}'}},
                                      ensure_ascii=False)
            if cmd == 'find_session':
                url_pattern = data.get('url_pattern', '')
                try:
                    return json.dumps({'r': self.find_session(url_pattern)}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': f'find_session 内部错误: {e}'}},
                                      ensure_ascii=False)
            if cmd == 'ext_cmd':
                try:
                    result = self.ext_cmd(data.get('payload') or {},
                                          client_id=data.get('clientId'),
                                          timeout=float(data.get('timeout', 15.0)))
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': str(e)}}, ensure_ascii=False)
            if cmd == 'execute_js':
                session_id = data.get('sessionId')
                code = data.get('code')
                timeout = float(data.get('timeout', 10.0))
                # Absent means False: an older client that doesn't send the flag
                # gets the safe behaviour (no substitute tab), not the old one.
                allow_failover = str(data.get('allowFailover', '0')) == '1'
                try:
                    result = self.execute_js(code, timeout=timeout, session_id=session_id,
                                             allow_failover=allow_failover)
                    print('[remote result]', (str(code)[:50] + ' RESULT:' +str(result)[:50]).replace('\n', ' '))
                    return json.dumps({'r': result}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({'r': {'error': str(e)}}, ensure_ascii=False)
            # 未知 cmd 必须报错而不是含糊地回 "ok"：调用方会把裸 "ok" 当成功，
            # 而实际上什么都没执行（例如把 ext_cmd 的 payload 直接当顶层 cmd 发）。
            return json.dumps(
                {'r': {'error': f'unknown cmd: {cmd!r}（扩展命令请用 cmd=ext_cmd + payload）'}},
                ensure_ascii=False)
        def run():
            from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler
            from socketserver import ThreadingMixIn
            class _T(ThreadingMixIn, WSGIServer): pass
            class _H(WSGIRequestHandler):
                def log_request(self, *a): pass
            make_server(self.host, self.port+1, app, server_class=_T, handler_class=_H).serve_forever()
        http_thread = threading.Thread(target=run, daemon=True)
        http_thread.start()  

    @staticmethod
    def _ws_origin(sock) -> str:
        """Origin header from the WS handshake, '' if absent."""
        try:
            return sock.request.headers.get('Origin', '') or ''
        except Exception:
            return ''

    def _origin_allowed(self, sock) -> bool:
        """Only the extension service worker may drive the bridge.

        Its handshake Origin is chrome-extension://<id> (also
        moz-extension:// / safari-web-extension:// on other browsers). Web
        pages always send https?://..., so a prefix allowlist keeps every
        site out while needing no shared secret — which matters because an
        extension cannot read a token file off disk.

        A non-browser local client (curl, a test script) sends no Origin at
        all. That is allowed only when AGENT_BROWSER_WS_ALLOW_NO_ORIGIN=1,
        so the default posture stays closed.
        """
        origin = self._ws_origin(sock)
        if not origin:
            return os.environ.get('AGENT_BROWSER_WS_ALLOW_NO_ORIGIN', '') == '1'
        allowed = ('chrome-extension://', 'moz-extension://',
                   'safari-web-extension://', 'extension://')
        if origin.startswith(allowed):
            return True
        extra = os.environ.get('AGENT_BROWSER_WS_ALLOWED_ORIGINS', '')
        return any(origin == o.strip() for o in extra.split(',') if o.strip())

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
                # close() in connected() only queues the close frame, so a
                # rejected peer can still get a data frame in before the socket
                # actually goes away. Re-check here so nothing it sends is ever
                # interpreted.
                if not driver._origin_allowed(self):
                    return
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
                        driver._apply_extension_tabs(client_id, browser, tabs, self)
                    elif data.get('type') == 'ping':
                        # Liveness reply so the extension can tell a live socket
                        # from a half-open zombie (TCP ESTABLISHED but dead). No
                        # pong within a couple keepalive ticks => extension force-
                        # reconnects instead of pushing tabs into a black hole.
                        try: self.send_message(json.dumps({'type': 'pong'}))
                        except Exception: pass
                    elif data.get('type') == 'ack': driver.acks[data.get('id','')] = time.time()
                    elif data.get('type') == 'result':
                        driver.results[data.get('id')] = {'success': True, 'data': data.get('result'), 'newTabs': data.get('newTabs', []), 'tabId': data.get('tabId'), 'ts': time.time()}
                    elif data.get('type') == 'error':
                        driver.results[data.get('id')] = {'success': False, 'data': data.get('error'), 'newTabs': data.get('newTabs', []), 'tabId': data.get('tabId'), 'ts': time.time()}
                except Exception as e:  
                    print(f"Error handling message: {e}")  
                    if hasattr(self, 'data'): print(self.data)  
            def connected(self):
                # WebSocket is exempt from the same-origin policy and needs no
                # CORS preflight, so without this check ANY page the user visits
                # could open ws://127.0.0.1:18765 and speak the protocol — most
                # damagingly send ext_ready with a chosen clientId, hijacking
                # ext_clients so every later ext_cmd goes to the attacker's
                # socket. The only legitimate client is the extension service
                # worker, whose handshake Origin is chrome-extension://<id>.
                if not driver._origin_allowed(self):
                    origin = driver._ws_origin(self)
                    print(f"Rejected WS connection from {self.address}, origin={origin!r}")
                    try: self.close()
                    except Exception: pass
                    return
                print(f"New connection from {self.address}")
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
    
    def _apply_extension_tabs(self, client_id: str, browser: str,
                              tabs: list[dict], client: WebSocket) -> None:
        """Apply one extension tab snapshot, respecting tab lifecycle generations."""
        def _sid(tab_id): return f"{client_id}:{tab_id}"

        current_tab_ids = {_sid(tab['id']) for tab in tabs}
        print(f"Received tabs update from {client_id} ({browser}): {current_tab_ids}")
        # Only sweep sessions belonging to THIS client; another browser's
        # update must not disconnect this browser's tabs.
        for sid in list(self.sessions.keys()):
            sess = self.sessions[sid]
            if (sess.type == 'ext_ws'
                    and sess.info.get('client_id') == client_id
                    and sid not in current_tab_ids):
                sess.mark_disconnected()
        for tab in tabs:
            session_id = _sid(tab['id'])
            session_info = {
                'url': tab.get('url'),
                'title': tab.get('title', ''),
                'connected_at': time.time(),
                'type': 'ext_ws',
                'client_id': client_id,
                'browser': browser,
                'tab_id': tab['id'],
            }
            if tab.get('generation') is not None:
                session_info['generation'] = str(tab['generation'])
            sess = self.sessions.get(session_id)
            old_generation = sess.info.get('generation') if sess else None
            new_generation = session_info.get('generation')
            if (sess and sess.is_active() and old_generation is not None
                    and new_generation is not None
                    and str(old_generation) != str(new_generation)):
                # Same client:tabId, different native tab lifetime. Replace the
                # session object so waiters cannot mistake the old registration
                # for the tab chrome.tabs.create just returned.
                print(
                    f"Tab generation changed for {session_id}: "
                    f"{old_generation} -> {new_generation}"
                )
                sess.mark_disconnected()
                self.sessions.pop(session_id, None)
                sess = None
            if sess and sess.is_active():
                sess.info = session_info
                sess.ws_client = client
            else:
                self._register_client(session_id, client, session_info)

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
    
    def _live_default_session_id(self) -> Optional[str]:
        """The remembered default tab, or a fresh pick if it has died.

        Tab ids churn — closing a tab, restarting the browser or reloading the
        extension all invalidate them — and this default is set once, when the
        first tab ever registers. Keeping a dead one poisons every call that
        does not name a tab: the caller gets "run switch_tab first" about a tab
        it never picked, so agents end up doing list_tabs + switch_tab before
        every single action. Re-picking is only safe on this implicit path.
        """
        cur = self.default_session_id
        if cur:
            session = self.sessions.get(cur)
            if session and session.is_active():
                return cur
        # Snapshot before iterating: the WS thread may insert/remove sessions
        # (tabs_update) while this runs — clean_sessions already snapshots for
        # exactly that reason; missing the snapshot here raises RuntimeError.
        alive = [s for s in list(self.sessions.values()) if s.is_active()]
        if not alive:
            return cur  # nothing to pick; let the caller's error path report it
        latest = self.sessions.get(self.latest_session_id)
        chosen = latest if latest in alive else alive[-1]
        if cur:
            print(f"默认会话 {cur} 已失效，自动改用 {chosen.id}")
        self.default_session_id = chosen.id
        return chosen.id

    def execute_js(self, code, timeout=15, session_id=None, allow_failover=False) -> Any:
        """Run JS in a tab.

        allow_failover=False by default: if the target session is gone we raise
        instead of running the script on some other live tab, because the script
        may have side effects the caller only meant for the tab it named.
        """
        timeout = float(timeout)
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        deadline = time.monotonic() + timeout

        def remaining():
            return max(0.0, deadline - time.monotonic())

        # Whether the caller actually named a tab. An explicitly named dead tab
        # must still be refused below; an implicit one the driver supplied from
        # its own memory must not be, since the caller never chose it.
        caller_named_target = session_id is not None
        if session_id is None:
            session_id = self._live_default_session_id()
        if self.is_remote:
            print('remote_execute_js')
            # HTTP timeout must outlast the JS timeout or long scripts die at
            # the transport layer before the bridge can answer.
            response = self._remote_cmd({"cmd": "execute_js", "sessionId": session_id,
                                         "code": code, "timeout": str(timeout),
                                         "allowFailover": "1" if allow_failover else "0"},
                                        timeout=max(0.001, remaining())).get('r', {})
            if response.get('error'):
                err = response['error']
                # Keep the exception type the same across local and remote: the
                # bridge flattens everything to a string over HTTP, so a caller
                # catching ValueError locally would miss it in remote mode.
                if '未连接' in err or 'not found' in err:
                    raise ValueError(err)
                raise Exception(err)
            if isinstance(response, dict) and response.get('tabId') is not None:
                # The executor (extension/bridge) names the tab it used; surface
                # it under the same key the local path uses so callers read one
                # field regardless of transport.
                response = dict(response)
                response['executed_tab_id'] = int(response['tabId'])
            return response
 
        switched_from = None
        session = self.sessions.get(session_id)
        if not session or not session.is_active():
            time.sleep(min(3.0, remaining()))
            session = self.sessions.get(session_id)
            if not session or not session.is_active():
                alive_sessions = [s for s in list(self.sessions.values()) if s.is_active()]
                # Do NOT execute on a substitute tab. This used to pick a live
                # session and run the script there anyway, so "click checkout"
                # or "delete this row" landed on whatever tab happened to be
                # alive, with only a switched_session field to say so after the
                # side effect had already happened. Refuse and name the
                # candidates; the caller re-targets explicitly.
                # An implicit target that died mid-call is the driver's own
                # bookkeeping problem, not a caller mistake, so recover from it
                # like _live_default_session_id would rather than refusing.
                if (allow_failover or not caller_named_target) and alive_sessions:
                    want_client = str(session_id).rsplit(':', 1)[0] if session_id and ':' in str(session_id) else None
                    same_client = [s for s in alive_sessions if want_client and str(s.id).startswith(want_client + ':')]
                    pool = same_client or alive_sessions
                    latest = self.sessions.get(self.latest_session_id)
                    session = latest if latest in pool else pool[-1]
                    print(f"会话 {session_id} 未连接，自动切换到活动会话: {session.id}")
                    switched_from = session_id
                    session_id = self.default_session_id = session.id
                if not session or not session.is_active():
                    if alive_sessions:
                        cands = ', '.join(str(s.id) for s in alive_sessions[:8])
                        raise ValueError(
                            f"会话ID {session_id} 未连接，已拒绝在其他标签页执行（避免副作用落到非目标页面）。"
                            f"当前活动会话: {cands}。请 switch_tab 选定目标后重试。")
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
        # Which tab this command names, derived from the session the driver
        # resolved — so every return path (success, reload, timeout) reports
        # the executor's target instead of a later guess from driver memory.
        exec_tab_id = int(session.info.get('tab_id', str(session.id).rsplit(':', 1)[-1])) if tp == 'ext_ws' else None

        # A disconnected tab may recover during the bounded grace sleep above.
        # Recovery does not renew the caller's budget: once the deadline is
        # spent, do not dispatch a side-effecting script merely because the
        # socket came back at the last instant.
        if remaining() <= 0:
            if tp in ['ws', 'ext_ws']:
                return {
                    'result': (
                        f"No response data in {timeout}s "
                        "(no ACK, script may not have been delivered)"
                    ),
                    'executed_tab_id': exec_tab_id,
                    **extra,
                }
            return {
                'result': (
                    f"Session {session_id} no response in {timeout}s "
                    "(script not polled)"
                ),
                'executed_tab_id': exec_tab_id,
                **extra,
            }

        if tp in ['ws', 'ext_ws']:
            try: session.ws_client.send_message(payload)
            except Exception as e:
                # Half-open socket (e.g. after system sleep): mark it dead so
                # the next call fails over instead of hitting it again.
                session.mark_disconnected()
                raise ValueError(f"会话 {session_id} 连接已失效({e})，已标记断开，请重试")
        elif tp == 'http': session.http_queue.put(payload)

        self.clean_sessions() 
        hasjump = acked = False
        missing_result = object()
        result = missing_result

        while result is missing_result:
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            wait_for = min(0.05, remaining())
            if wait_for > 0:
                time.sleep(wait_for)
            # A result can arrive during the final polling sleep exactly as the
            # deadline expires. Consume that real reply instead of dropping it
            # below and reporting an ambiguous timeout for a completed script.
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            if not acked and exec_id in self.acks:
                acked = True
            if tp in ['ws', 'ext_ws']:
                if not session.is_active(): hasjump = True
                if hasjump and session.is_active():
                    # 脚本随 reload 作废：晚到的结果不应再被任何人读到，直接丢弃。
                    self.results.pop(exec_id, None)
                    self.acks.pop(exec_id, None)
                    return {'result': f"Session {session_id} reloaded.", "closed":1,
                            'executed_tab_id': exec_tab_id, **extra}
            if remaining() <= 0:
                # Make the deadline decision with one atomic dict operation.
                # A result present now is consumed; one arriving after this
                # point is genuinely late and is cleaned up below.
                result = self.results.pop(exec_id, missing_result)
                if result is not missing_result:
                    break
                # 清理本次 exec_id 的残留键：超时返回后 results/acks 里不会
                # 再有调用方来取，留着只会累积到 clean_sessions 的 600s TTL——
                # 高频超时场景下等于是长期泄漏（与 ext_cmd 超时路径一致）。
                self.results.pop(exec_id, None)
                self.acks.pop(exec_id, None)
                if tp in ['ws', 'ext_ws']:
                    if hasjump: return {'result': f"Session {session_id} reloaded and new page is loading...", 'closed':1,
                                        'executed_tab_id': exec_tab_id, **extra}
                    if acked: return {"result": f"No response data in {timeout}s (ACK received, script may still be running)",
                                      'executed_tab_id': exec_tab_id, **extra}
                    return {"result": f"No response data in {timeout}s (no ACK, script may not have been delivered)",
                            'executed_tab_id': exec_tab_id, **extra}
                elif tp == 'http':
                    if acked: return {"result": f"Session {session_id} no response in {timeout}s (delivered but no result)",
                                      'executed_tab_id': exec_tab_id, **extra}
                    return {"result": f"Session {session_id} no response in {timeout}s (script not polled)",
                            'executed_tab_id': exec_tab_id, **extra}
        
        assert result is not missing_result
        if exec_id in self.acks: self.acks.pop(exec_id)
        if not result['success']: raise Exception(result['data'])  
        rr = {'data': result['data'], **extra}
        # Prefer the tab the executor echoed back (covers failover and remote
        # transports); fall back to the session we resolved locally.
        rtab = result.get('tabId')
        rr['executed_tab_id'] = int(rtab) if rtab is not None else exec_tab_id
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
        timeout = float(timeout)
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        deadline = time.monotonic() + timeout

        def remaining():
            return max(0.0, deadline - time.monotonic())

        if self.is_remote:
            response = self._remote_cmd({"cmd": "ext_cmd", "payload": cmd,
                                         "clientId": client_id, "timeout": str(timeout)},
                                        timeout=max(0.001, remaining())).get('r', {})
            if response.get('error'):
                err = str(response['error'])
                # Keep the timeout distinguishable across the HTTP hop, so
                # callers like newtab() can tell "never answered" (do not
                # retry, it may have happened) from "no such route".
                if 'did not respond within' in err:
                    raise TimeoutError(err)
                raise Exception(err)
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

        missing_result = object()
        result = missing_result
        while result is missing_result:
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            wait_for = min(0.05, remaining())
            if wait_for > 0:
                time.sleep(wait_for)
            result = self.results.pop(exec_id, missing_result)
            if result is not missing_result:
                break
            if remaining() <= 0:
                result = self.results.pop(exec_id, missing_result)
                if result is not missing_result:
                    break
                # MUST raise, not return. Returning a dict here made callers
                # like set_extension_enabled / close_tabs / open_new_tab report
                # status:"ok" for a mutation that never reached the browser —
                # the agent then acts on a success that did not happen.
                self.results.pop(exec_id, None)
                self.acks.pop(exec_id, None)
                raise TimeoutError(
                    f"extension {client_id} did not respond within {timeout}s; "
                    f"the command may not have reached the browser (service worker "
                    f"asleep, or no scriptable tab to wake it)")
        assert result is not missing_result
        self.acks.pop(exec_id, None)
        if not result['success']: raise Exception(result['data'])
        return {'data': result['data'], 'client_id': client_id}

    def _remote_cmd(self, cmd, timeout=30):
        headers = {"Content-Type": "application/json"}
        # Every client reads the same persistent per-user token file. This is
        # independent of which editor launched the MCP process.
        token = bridge_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Bridge-Token"] = token
        resp = self._http.post(self.remote, headers=headers, json=cmd, timeout=timeout)
        if resp.status_code == 401:
            # 别把 401 的 HTML/文本喂给 .json()（会变成一句看不懂的 JSONDecodeError）。
            raise PermissionError(
                "桥拒绝了本次请求(401)：MCP 与常驻桥读取的 ABM token 不一致。"
                f"确认双方使用同一个 {bridge_token_path()}，然后重启旧桥；"
                f"{TOKEN_ENV} 仅用于旧安装的一次迁移。")
        if resp.status_code >= 400:
            # 桥端未捕获的异常会变成 500 HTML 页；同样不能喂给 .json()，
            # 否则调用方看到的是 JSONDecodeError 而不是真实成因。
            body = resp.text if hasattr(resp, 'text') else ''
            snippet = (body[:300] if body else '(空响应)').replace('\n', ' ')
            raise RuntimeError(f"桥返回 HTTP {resp.status_code}: {snippet}")
        return resp.json()

    def get_all_sessions(self, timeout=None):
        if self.is_remote:
            return self._remote_cmd({"cmd": "get_all_sessions"}, timeout=timeout or 30).get('r', [])
        self.clean_sessions()
        return [{'id': session.id, **session.info} for session in list(self.sessions.values())
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
        active = [s for s in list(self.sessions.values()) if s.is_active()]
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

    def set_session(self, url_pattern: str) -> Optional[str]:
        if self.is_remote:
            matched = self._remote_cmd({"cmd": "find_session", "url_pattern": url_pattern}).get('r', [])
        else:
            matched = self.find_session(url_pattern)
        if not matched:
            print(f"警告: 未找到URL包含 '{url_pattern}' 的会话")
            return None
        if len(matched) > 1: print(f"警告: 找到多个URL包含 '{url_pattern}' 的会话，选择第一个")  
        self.default_session_id, info = matched[0]
        print(f"成功设置默认会话: {self.default_session_id}: {info['url']}")  
        return self.default_session_id  
    
    def jump(self, url, timeout=10): self.execute_js(f"window.location.href={json.dumps(url)}", timeout=timeout)
    def newtab(self, url=None, client_id=None, timeout=15, active=True):
        if url is None: url = "http://www.baidu.com/robots.txt"
        # Native chrome.tabs.create, addressed to the extension itself so it
        # works with no tabs open. (The old GM_openInTab was a Tampermonkey-only
        # API absent from a plain extension, so it always threw ReferenceError.)
        payload = {
            "cmd": "tabs",
            "method": "create",
            "url": url,
            "active": bool(active),
        }
        try:
            return self.ext_cmd(payload, client_id=client_id, timeout=timeout)
        except TimeoutError:
            # The extension got the command but never answered. The session
            # route would talk to the same asleep service worker, so retrying
            # there just burns another timeout — and if the tab DID open, the
            # fallback would open a second one. Surface it instead.
            raise
        except Exception as e:
            # Older bridge daemon without /link ext_cmd: fall back to the tab
            # session route, which still works whenever at least one tab lives.
            print(f"ext_cmd 不可用({e})，回退到 session 通道")
            return self.execute_js(json.dumps(payload))
    
if __name__ == "__main__":
    driver = TMWebDriver(host='127.0.0.1', port=18765)

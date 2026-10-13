import sys, types, json, threading, time, socket, base64, hashlib, struct

# ===== stub mcdreforged =====
class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()

class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        v = _Any()
        setattr(self, name, v)
        return v

import importlib.abc, importlib.machinery

class _Loader(importlib.abc.Loader):
    def create_module(self, spec):
        return _StubModule(spec.name)
    def exec_module(self, module):
        pass

class _StubFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("mcdreforged") or fullname.startswith("packaging"):
            return importlib.machinery.ModuleSpec(fullname, _Loader(), is_package=True)
        return None

sys.meta_path.insert(0, _StubFinder())
sys.path.insert(0, r'F:\vibe coding工程\gugubot-qq-ws\plugin')

# ===== 最小 WS 服务器（标准库实现）=====
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RECEIVED = []
IDENTIFY_OK = threading.Event()

def handle_client(conn):
    # 1) HTTP 升级握手
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return
        data += chunk
    key = None
    for line in data.decode("latin-1").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    conn.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())

    def send_text(payload):
        msg = payload.encode()
        header = bytearray([0x81])
        n = len(msg)
        if n < 126:
            header.append(0x00 | n)
        elif n < 65536:
            header.append(0x00 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x00 | 127)
            header += struct.pack(">Q", n)
        conn.sendall(bytes(header) + msg)

    def recv_text():
        # 简化：不处理分片/大帧
        hdr = conn.recv(2)
        if len(hdr) < 2:
            return None
        b1, b2 = hdr
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", conn.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", conn.recv(8))[0]
        mask = conn.recv(4) if masked else None
        payload = b""
        while len(payload) < length:
            payload += conn.recv(length - len(payload))
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 8:  # close
            return None
        return payload.decode()

    # 2) 发 Hello (op:2)
    send_text(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))

    while True:
        raw = recv_text()
        if raw is None:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        op = data.get("op")
        RECEIVED.append(data)
        if op == 2:  # Identify
            d = data.get("d", {})
            assert d.get("token", "").startswith("QQBot "), "token 必须带 QQBot 前缀"
            assert d.get("intents") == 1 | (1 << 4), f"intents 错误: {d.get('intents')}"
            assert d.get("shard") == [0, 1]
            send_text(json.dumps({
                "op": 0, "s": 1, "t": "READY",
                "d": {"session_id": "SESS123", "user": {"id": "BOT_OFFICIAL_ID"}},
            }))
            IDENTIFY_OK.set()
        elif op == 1:  # Heartbeat
            send_text(json.dumps({"op": 11}))

def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 19877))
    srv.listen(1)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle_client, args=(c,), daemon=True).start()

server_thread = threading.Thread(target=serve, daemon=True)
server_thread.start()
time.sleep(0.3)

# ===== 客户端 =====
from gugubot.connector.qq_official.token_manager import QQTokenManager
from gugubot.connector.qq_official.api_client import QQOfficialClient
from gugubot.connector.qq_official.reply_pool import QQReplyPool

class FakeLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): print("[info]", *a)
    def warning(self, *a, **k): print("[warn]", *a)
    def error(self, *a, **k): print("[err]", *a)

tm = QQTokenManager("123456789", "SECRET", logger=FakeLogger())
tm._token = "FAKE_TOKEN"
tm._expires_at = time.time() + 3600

pool = QQReplyPool()
api = QQOfficialClient(tm, pool, logger=FakeLogger())
api.get_wss_url = lambda: {"url": "ws://127.0.0.1:19877"}

dispatches = []
from gugubot.connector.qq_official.ws_client import QQOfficialWSClient
ws = QQOfficialWSClient(
    api_client=api,
    token_manager=tm,
    intents=1 | (1 << 4),
    logger=FakeLogger(),
    on_dispatch=lambda t, s, d: dispatches.append((t, s, d)),
)
ws.connect()

assert IDENTIFY_OK.wait(timeout=10), "服务器未收到合法 Identify"
for i in range(50):
    if ws.is_connected():
        break
    time.sleep(0.1)
assert ws.is_connected(), "READY 后应视为已连接"
assert ws.session_id == "SESS123"

time.sleep(2.5)
heartbeats = [r for r in RECEIVED if r.get("op") == 1]
assert len(heartbeats) >= 1, f"应有心跳: {RECEIVED}"

ws.disconnect()
print()
print("=" * 40)
print("WS PROTOCOL TEST PASSED (Hello/Identify/READY/Heartbeat)")


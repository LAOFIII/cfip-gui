# -*- coding: utf-8 -*-
"""取 IP 列表专用的代理支持（HTTP / SOCKS4 / SOCKS5，含账号密码）。

只用标准库实现，避免引入 PySocks 之类的第三方依赖。
注意：代理**只用于「取 IP 列表」这一步**，测速流量始终直连目标 IP。
"""

import base64
import re
import socket
import ssl
import struct
import urllib.parse
import urllib.request

MODE_NONE = "不使用代理"
MODE_HTTP = "HTTP代理"
MODE_SOCKS4 = "SOCKS4代理"
MODE_SOCKS5 = "SOCKS5代理"
MODES = (MODE_NONE, MODE_HTTP, MODE_SOCKS4, MODE_SOCKS5)


class ProxyConfig:
    """代理设置。mode 为 MODES 之一；host/port 为空表示未配置。"""

    __slots__ = ("mode", "host", "port", "user", "password")

    def __init__(self, mode=MODE_NONE, host="", port=0, user="", password=""):
        self.mode = mode if mode in MODES else MODE_NONE
        self.host = (host or "").strip()
        self.port = int(port or 0)
        self.user = (user or "").strip()
        self.password = password or ""

    @property
    def enabled(self):
        return self.mode != MODE_NONE and bool(self.host) and 0 < self.port < 65536

    def describe(self):
        if self.mode == MODE_NONE:
            return "直连"
        if not self.host:
            return f"{self.mode}（未填服务器）"
        auth = "，带认证" if self.user else ""
        return f"{self.mode} {self.host}:{self.port}{auth}"

    def validate(self):
        """返回错误描述，None 表示可用。"""
        if self.mode == MODE_NONE:
            return None
        if not self.host:
            return "已选择代理模式，但没填服务器地址"
        if not (0 < self.port < 65536):
            return "代理端口不合法（应为 1~65535）"
        return None


# ---------------------------------------------------------------- SOCKS 握手

def _connect_via_socks(proxy: ProxyConfig, host, port, timeout):
    """建立到 (host, port) 的 SOCKS 隧道，返回已连通的 socket。"""
    fam = socket.AF_INET
    try:
        infos = socket.getaddrinfo(proxy.host, proxy.port, 0, socket.SOCK_STREAM)
        if infos:
            fam = infos[0][0]
    except OSError as exc:
        raise OSError(f"代理服务器域名解析失败：{exc}") from exc

    sock = socket.socket(fam, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((proxy.host, proxy.port))
    except OSError as exc:
        sock.close()
        raise OSError(f"连不上代理 {proxy.host}:{proxy.port}（{exc}）") from exc

    try:
        if proxy.mode == MODE_SOCKS5:
            _socks5_handshake(sock, proxy, host, port)
        else:
            _socks4_handshake(sock, proxy, host, port)
    except Exception:
        sock.close()
        raise
    return sock


def _socks5_handshake(sock, proxy, host, port):
    methods = [0x00, 0x02] if proxy.user else [0x00]
    sock.sendall(bytes([0x05, len(methods), *methods]))
    resp = _recv_exact(sock, 2)
    if resp[0] != 0x05:
        raise OSError("SOCKS5 握手失败：服务器返回版本异常")
    if resp[1] == 0xFF:
        raise OSError("SOCKS5 握手失败：代理不接受任何认证方式")
    if resp[1] == 0x02:
        if not proxy.user:
            raise OSError("SOCKS5 代理要求账号密码，但未填写")
        u = proxy.user.encode("utf-8")[:255]
        p = proxy.password.encode("utf-8")[:255]
        sock.sendall(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
        auth = _recv_exact(sock, 2)
        if auth[1] != 0x00:
            raise OSError("SOCKS5 认证失败：用户名或密码错误")

    try:
        addr = socket.inet_aton(host)
        req = bytes([0x05, 0x01, 0x00, 0x01]) + addr
    except OSError:
        try:
            packed = socket.inet_pton(socket.AF_INET6, host)
            req = bytes([0x05, 0x01, 0x00, 0x04]) + packed
        except OSError:
            name = host.encode("idna")[:255]
            req = bytes([0x05, 0x01, 0x00, 0x03, len(name)]) + name
    sock.sendall(req + struct.pack(">H", port))

    head = _recv_exact(sock, 4)
    if head[0] != 0x05:
        raise OSError("SOCKS5 响应异常")
    if head[1] != 0x00:
        codes = {1: "一般性失败", 2: "规则不允许", 3: "网络不可达", 4: "主机不可达",
                 5: "连接被拒绝", 6: "TTL 过期", 7: "不支持的命令", 8: "不支持的地址类型"}
        raise OSError(f"SOCKS5 连接目标失败：{codes.get(head[1], '未知错误码 %d' % head[1])}")
    atyp = head[3]
    if atyp == 0x01:
        _recv_exact(sock, 4)
    elif atyp == 0x04:
        _recv_exact(sock, 16)
    elif atyp == 0x03:
        ln = _recv_exact(sock, 1)[0]
        _recv_exact(sock, ln)
    _recv_exact(sock, 2)


def _socks4_handshake(sock, proxy, host, port):
    try:
        addr = socket.inet_aton(host)
    except OSError:
        # SOCKS4 不支持域名和 IPv6：交给代理侧解析域名不保证，这里直接报错更清楚
        raise OSError("SOCKS4 只支持 IPv4 目标，当前目标需要 SOCKS5") from None
    userid = proxy.user.encode("utf-8") if proxy.user else b""
    sock.sendall(bytes([0x04, 0x01]) + struct.pack(">H", port) + addr + userid + b"\x00")
    resp = _recv_exact(sock, 8)
    if resp[1] != 0x5A:
        raise OSError(f"SOCKS4 连接目标失败：错误码 0x{resp[1]:02X}")


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("代理连接被对端关闭")
        buf += chunk
    return buf


# ---------------------------------------------------------------- HTTP 请求

def _http_via_socks(proxy: ProxyConfig, url, timeout, headers):
    """经 SOCKS 代理完成一次 HTTPS/HTTP GET，返回 (status, body_bytes)。"""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))

    sock = _connect_via_socks(proxy, host, port, timeout)
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            + "Connection: close\r\n\r\n"
        )
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        m = re.match(r"HTTP/\d\.\d\s+(\d{3})", status_line)
        status = int(m.group(1)) if m else 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
            if len(body) > 8 * 1024 * 1024:
                break
        return status, body
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _http_via_http_proxy(proxy: ProxyConfig, url, timeout, headers):
    """经 HTTP 代理：HTTPS 走 CONNECT 隧道，HTTP 走绝对 URI。"""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)

    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        if scheme == "https":
            line = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if proxy.user:
                token = base64.b64encode(f"{proxy.user}:{proxy.password}".encode()).decode()
                line += f"Proxy-Authorization: Basic {token}\r\n"
            sock.sendall((line + "\r\n").encode())
            resp = b""
            while b"\r\n\r\n" not in resp and len(resp) < 8192:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                resp += chunk
            status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
            if " 200" not in status_line:
                raise OSError(f"HTTP 代理 CONNECT 失败：{status_line}")
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
            target = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        else:
            target = url

        req = (
            f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
            + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            + "Connection: close\r\n\r\n"
        )
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        m = re.match(r"HTTP/\d\.\d\s+(\d{3})", status_line)
        status = int(m.group(1)) if m else 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
            if len(body) > 8 * 1024 * 1024:
                break
        return status, body
    finally:
        try:
            sock.close()
        except OSError:
            pass


def fetch_url(url, proxy: ProxyConfig, timeout=20, headers=None):
    """按代理配置取 URL 内容。返回 (status, body_bytes)。

    proxy 为 None 或 mode=无代理 时走直连（空 ProxyHandler，忽略系统代理）。
    """
    headers = headers or {}
    if proxy is None or not proxy.enabled:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(8 * 1024 * 1024)
    if proxy.mode in (MODE_SOCKS5, MODE_SOCKS4):
        return _http_via_socks(proxy, url, timeout, headers)
    return _http_via_http_proxy(proxy, url, timeout, headers)


def probe_proxy(proxy: ProxyConfig, timeout=8):
    """检测代理是否可用：能连上并且能穿过它拿到公网响应。返回 (ok, message)。"""
    err = proxy.validate()
    if err:
        return False, err
    try:
        sock = _connect_via_socks(proxy, "www.cloudflare.com", 443, timeout) \
            if proxy.mode in (MODE_SOCKS5, MODE_SOCKS4) else None
        if sock is not None:
            sock.close()
            return True, f"{proxy.mode} 可用（{proxy.host}:{proxy.port} 已穿透到目标 443）"
        status, _ = _http_via_http_proxy(proxy, "https://www.cloudflare.com/cdn-cgi/trace",
                                         timeout, {"User-Agent": "cfip-gui"})
        if status == 200:
            return True, f"HTTP 代理可用（{proxy.host}:{proxy.port}，CONNECT 隧道正常）"
        return False, f"HTTP 代理返回 HTTP {status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

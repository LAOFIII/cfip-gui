# -*- coding: utf-8 -*-
"""
cfip-gui —— 自选 IP 测速工具（GUI 版，参考 XIU2/CloudflareSpeedTest 的测速逻辑改造）

核心逻辑（与 cfst 一致）：
  1. TCPing 延迟测速（多线程，每 IP 多次取平均、统计丢包）
  2. 延迟排序 + 过滤（延迟上下限、丢包上限）
  3. 从延迟最低的 IP 起，依次做下载测速（单线程排队，限时）
  4. 按下载速度排序，输出 Top N + CSV

与 cfst 的差异（本次改造点）：
  - 测速目标完全由用户指定：手动粘贴 IP / 从 API 网址拉取（不再读 ip.txt 网段文件）
  - 全流程强制直连：清空代理环境变量、所有 socket 直连目标 IP、启动前做直连自检
  - 提供 tkinter GUI；同一文件带 --cli 命令行模式，便于脚本/无界面环境验证

只依赖 Python 3.9+ 标准库（tkinter / ssl / socket / urllib），无需 pip 安装。
"""

import argparse
import csv
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

APP_NAME = "cfip-gui"
APP_VER = "1.0.0"

# ---------------------------------------------------------------- 直连基础

PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "NODE_USE_ENV_PROXY", "GIT_PROXY_COMMAND",
)


def clear_proxy_env():
    """进程启动即清空代理相关环境变量，避免任何库偷偷走代理。"""
    removed = []
    for k in PROXY_ENV_KEYS:
        if k in os.environ:
            removed.append(f"{k}={os.environ.pop(k)}")
    return removed


def direct_opener():
    """强制不走代理的 urllib opener（空 ProxyHandler 会忽略环境变量）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_reg_proxy():
    """读取 Windows 系统代理（WinINET）设置。返回 (enabled, server)。"""
    try:
        import winreg
    except Exception:
        return None, ""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        except OSError:
            enable = 0
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        except OSError:
            server = ""
        winreg.CloseKey(key)
        return bool(enable), str(server or "")
    except OSError:
        return None, ""


def _netsh_winhttp_proxy():
    """WinHTTP 代理（服务级），返回描述字符串。"""
    try:
        out = subprocess.run(
            ["netsh", "winhttp", "show", "proxy"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = out.stdout or b""
        text = raw.decode("utf-8", "ignore") if raw else ""
        if "代理" not in text and "Proxy" not in text:
            text = raw.decode("gbk", "ignore")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # 直接访问 → 无代理
        if any(("直接访问" in ln) or ("Direct access" in ln) for ln in lines):
            return "直接访问（未配置代理）"
        body = " ".join(lines[1:]) if len(lines) > 1 else (lines[0] if lines else "未知")
        return body or "未知"
    except Exception as exc:  # noqa: BLE001
        return f"检测失败: {exc}"


def _vpn_adapters():
    """检测可能劫持流量的虚拟网卡（TUN/TAP/VPN）。"""
    hits = []
    try:
        out = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = out.stdout or b""
        for enc in ("utf-8", "gbk"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                text = raw.decode(enc, "ignore")
        keys = ("tun", "tap", "wg", "wireguard", "openvpn", "tailscale", "zerotier",
                "clash", "mihomo", "singbox", "sing-box", "vpn", "proxy")
        for line in text.splitlines():
            low = line.lower()
            if any(k in low for k in keys):
                hits.append(line.strip())
    except Exception:
        pass
    return hits


def egress_trace(timeout=8.0):
    """直连 Cloudflare trace，看出口 IP 与 warp 状态（用于佐证是否被中转）。"""
    ctx = ssl.create_default_context()
    host = "www.cloudflare.com"
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                tls.settimeout(timeout)
                req = (
                    f"GET /cdn-cgi/trace HTTP/1.1\r\nHost: {host}\r\n"
                    "User-Agent: cfip-gui\r\nConnection: close\r\n\r\n"
                )
                tls.sendall(req.encode())
                buf = b""
                while b"\r\n\r\n" not in buf and len(buf) < 65536:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                head, _, body = buf.partition(b"\r\n\r\n")
                while len(body) < 4096:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    body += chunk
                data = {}
                for line in body.decode("utf-8", "ignore").splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        data[k.strip()] = v.strip()
                return {"ok": True, "data": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def direct_selftest():
    """直连自检。返回 (ok, lines)。ok=False 表示判定为“非直连”，默认阻断测速。"""
    lines, ok = [], True

    removed = clear_proxy_env()
    if removed:
        lines.append(("WARN", "已清除代理环境变量：" + ", ".join(removed)))
    else:
        lines.append(("OK", "无代理环境变量"))

    enabled, server = _read_reg_proxy()
    if enabled is None:
        lines.append(("WARN", "无法读取 Windows 系统代理设置"))
    elif enabled:
        # 系统代理只影响 WinINET/浏览器等，本程序 socket 直连不受其影响 → 提示而非阻断
        lines.append(("WARN", f"Windows 系统代理已开启：{server}（本程序所有 socket 直连目标，"
                              f"不走该代理；如需 100% 干净可临时关闭）"))
    else:
        lines.append(("OK", "Windows 系统代理未开启"))

    winhttp = _netsh_winhttp_proxy()
    if "直接访问" in winhttp or "Direct access" in winhttp or "no proxy" in winhttp.lower():
        lines.append(("OK", f"WinHTTP 代理：{winhttp}"))
    else:
        ok = False
        lines.append(("FAIL", f"WinHTTP 代理已配置：{winhttp}"))

    adapters = _vpn_adapters()
    if adapters:
        ok = False
        lines.append(("FAIL", "检测到可能接管流量的虚拟网卡/VPN：" + "; ".join(adapters)))
    else:
        lines.append(("OK", "未发现 TUN/TAP/VPN 虚拟网卡"))

    trace = egress_trace()
    if trace["ok"]:
        d = trace["data"]
        info = f"直连出口 IP={d.get('ip', '?')} 地区={d.get('colo', '?')} warp={d.get('warp', '?')}"
        if d.get("warp", "off") not in ("off", "", "?"):
            ok = False
            lines.append(("FAIL", info + " —— 出口疑似经过 WARP/中转"))
        else:
            lines.append(("OK", info))
    else:
        lines.append(("WARN", "直连连通性探测失败（不影响测速，但无法佐证出口）：" + trace["error"]))

    return ok, lines


# ---------------------------------------------------------------- IP 输入解析

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"(?<![:.\w])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![:.\w])")


def _norm(token):
    """规范化单个 IP 文本（去方括号、去端口）。非法返回 None。"""
    t = token.strip().strip("[]").strip()
    if not t:
        return None
    # [v6]:443 / v6 带端口（仅 IPv4 才安全地剥端口）
    if t.count(":") == 1 and "." in t:
        t = t.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(t))
    except ValueError:
        return None


def parse_ip_text(text, max_n=500):
    """解析用户输入：每行一个 / 逗号 / 分号 / 空格分隔；支持 CIDR（按 /24 随机取一个）。"""
    tokens, cidrs = [], []
    for raw in re.split(r"[\s,;，；]+", text or ""):
        tok = raw.strip()
        if not tok or tok.startswith("#"):
            continue
        if "/" in tok:
            cidrs.append(tok)
        else:
            tokens.append(tok)

    ips, bad = [], []
    for tok in tokens:
        ip = _norm(tok)
        if ip:
            ips.append(ip)
        else:
            bad.append(tok)

    import random
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            bad.append(c)
            continue
        if net.version == 4 and net.prefixlen < 24:
            # 与 cfst 默认行为一致：每个 /24 段随机取一个
            subnets = [s for s in net.subnets(new_prefix=24)]
            pick = random.choice(subnets)
            ips.append(str(pick.network_address + random.randint(1, 254)))
        else:
            hosts = list(net.hosts()) if net.num_addresses <= 1024 else None
            if hosts is None:
                bad.append(c + "(网段过大)")
            else:
                ips.extend(str(h) for h in hosts)

    seen, uniq = set(), []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            uniq.append(ip)
    truncated = False
    if len(uniq) > max_n:
        uniq = uniq[:max_n]
        truncated = True
    return uniq, bad, truncated


# ---------------------------------------------------------------- API 取列表

def fetch_ip_list(url, timeout=20, retries=2, allow_proxy=False, log=None):
    """请求 API 并解析出 IP 列表。返回 (ips, err)。

    兼容返回格式：纯文本每行一个 IP、逗号/空格分隔、JSON 数组、JSON 对象内的字符串数组。
    """
    log = log or (lambda *_: None)
    if not re.match(r"^https?://", url or "", re.I):
        return [], "网址必须以 http:// 或 https:// 开头"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({} if not allow_proxy else urllib.request.getproxies())
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{APP_NAME}/{APP_VER}",
        "Accept": "text/plain, application/json, */*",
    })
    last = ""
    for attempt in range(1, retries + 1):
        try:
            log(f"请求 API（第 {attempt} 次，{'经系统代理' if allow_proxy else '直连'}）：{url}")
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read(2 * 1024 * 1024)
                text = raw.decode("utf-8", "replace")
                log(f"HTTP {resp.status}，{len(raw)} 字节")
            ips = _extract_ips_from_payload(text)
            if ips:
                return ips, None
            return [], "接口返回成功但未解析到任何 IP（内容前 200 字：%r）" % text[:200]
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code} {exc.reason}"
            log("失败：" + last)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            log("失败：" + last)
        if attempt < retries:
            time.sleep(1.0)
    hint = ""
    if not allow_proxy:
        hint = "（若该域名直连被重置/超时，可勾选「API 取列表允许走系统代理」——仅取列表，测速流量仍强制直连）"
    return [], f"API 请求失败：{last}{hint}"


def _extract_ips_from_payload(text):
    body = (text or "").strip()
    if not body:
        return []
    # JSON 优先
    if body[0] in "[{":
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            data = None
        if data is not None:
            found = []

            def walk(node):
                if isinstance(node, str):
                    ip = _norm(node)
                    if ip:
                        found.append(ip)
                    else:
                        for m in IPV4_RE.findall(node) + IPV6_RE.findall(node):
                            ip = _norm(m)
                            if ip:
                                found.append(ip)
                elif isinstance(node, list):
                    for x in node:
                        walk(x)
                elif isinstance(node, dict):
                    for v in node.values():
                        walk(v)

            walk(data)
            return _dedupe(found)
    found = []
    for line in body.splitlines():
        for m in IPV4_RE.findall(line) + IPV6_RE.findall(line):
            ip = _norm(m)
            if ip:
                found.append(ip)
    return _dedupe(found)


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------- 测速核心

@dataclass
class IpResult:
    ip: str
    sent: int = 0
    recv: int = 0
    latency: float = 0.0
    speed: float = 0.0
    colo: str = "N/A"
    error: str = ""

    @property
    def loss(self):
        return 0.0 if self.sent == 0 else round((self.sent - self.recv) / self.sent, 2)


def tcp_ping(ip, port, timeout):
    """单次 TCP 握手耗时（ms），失败返回 None。"""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect((ip, port))
        return (time.perf_counter() - t0) * 1000.0
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 目标是裸 IP，证书必然不匹配；只测速不校验
    try:
        ctx.set_alpn_protocols(["http/1.1"])  # 强制 HTTP/1.1，避免协商到 h2 后手写请求解析失败
    except Exception:
        pass
    return ctx


def http_ping(ip, port, host, path, timeout):
    """HTTPing：TCP+TLS+首个响应字节耗时（ms），返回 (ms, colo) 或 (None, "")。"""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.connect((ip, port))
        with _ssl_ctx().wrap_socket(s, server_hostname=host) as tls:
            tls.settimeout(timeout)
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {APP_NAME}/{APP_VER}\r\n"
                "Accept: */*\r\nConnection: close\r\n\r\n"
            )
            tls.sendall(req.encode())
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = tls.recv(2048)
                if not chunk:
                    break
                head += chunk
            ms = (time.perf_counter() - t0) * 1000.0
            return ms, _colo_from_headers(head)
    except Exception:  # noqa: BLE001
        return None, ""
    finally:
        try:
            s.close()
        except Exception:
            pass


def _colo_from_headers(head_bytes):
    text = head_bytes.decode("latin-1", "ignore")
    m = re.search(r"(?im)^cf-ray:\s*[0-9a-f]+-([A-Za-z]{3})\s*$", text)
    return m.group(1).upper() if m else ""


def download_test(ip, port, host, path, duration, timeout=10.0):
    """从指定 IP 拉流，限时 duration 秒。返回 (MB/s, colo, error)。"""
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        with _ssl_ctx().wrap_socket(s, server_hostname=host) as tls:
            tls.settimeout(timeout)
            req = (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {APP_NAME}/{APP_VER}\r\n"
                "Accept: */*\r\nConnection: keep-alive\r\n\r\n"
            )
            tls.sendall(req.encode())
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 65536:
                chunk = tls.recv(4096)
                if not chunk:
                    return 0.0, "", "无响应"
                head += chunk
            colo = _colo_from_headers(head)
            status = head.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
            if " 200" not in status and " 206" not in status:
                return 0.0, colo, f"状态码异常：{status}"
            length = 0
            m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
            if m:
                length = int(m.group(1))
            start = time.perf_counter()
            total = len(head.split(b"\r\n\r\n", 1)[1])
            deadline = start + duration
            while True:
                remain = deadline - time.perf_counter()
                if remain <= 0:
                    break
                if length and total >= length:
                    break
                tls.settimeout(max(0.5, min(timeout, remain)))
                try:
                    chunk = tls.recv(65536)
                except (TimeoutError, socket.timeout):
                    # 服务端不主动关连接是常态；已拿到数据即视为正常结束
                    break
                if not chunk:
                    break
                total += len(chunk)
            elapsed = max(time.perf_counter() - start, 1e-6)
            speed = (total / 1024 / 1024) / elapsed
            if total <= 0:
                return 0.0, colo, "未收到数据"
            return speed, colo, ""
    except Exception as exc:  # noqa: BLE001
        return 0.0, "", f"{type(exc).__name__}: {exc}"
    finally:
        try:
            s.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 任务编排

@dataclass
class Options:
    port: int = 443
    threads: int = 100
    pings: int = 4
    timeout: float = 4.0
    dl_count: int = 10
    dl_time: float = 8.0
    lat_min: float = 0.0
    lat_max: float = 9999.0
    loss_max: float = 1.0
    speed_url: str = "https://speed.cloudflare.com/__down?bytes=50000000"
    top_n: int = 20
    max_ips: int = 500
    httping: bool = False
    csv_path: str = ""


def split_speed_url(url):
    m = re.match(r"^(https?)://([^/]+)(/.*)?$", url.strip())
    if not m:
        raise ValueError("测速地址格式错误：" + url)
    scheme, hostport, path = m.group(1), m.group(2), m.group(3) or "/"
    host = hostport.split(":")[0]
    return host, path


class Runner:
    """跑一轮完整测速；通过 emit(type, payload) 把事件推给界面/命令行。"""

    def __init__(self, ips, opts: Options, emit, stop_event=None):
        self.ips = ips
        self.o = opts
        self.emit = emit
        self.stop = stop_event or threading.Event()
        self.results = []

    def run(self):
        o = self.o
        try:
            host, path = split_speed_url(o.speed_url)
        except ValueError as exc:
            self.emit("error", str(exc))
            return []
        self.emit("log", f"开始延迟测速（模式：{'HTTP' if o.httping else 'TCP'}, 端口：{o.port}, "
                         f"线程：{o.threads}, 次数：{o.pings}, 目标：{len(self.ips)} 个 IP）")
        results = self._latency_phase(host, path)
        if self.stop.is_set():
            self.emit("stopped", None)
            return results

        passed = [r for r in results
                  if r.recv > 0 and r.latency <= o.lat_max and r.latency >= o.lat_min
                  and r.loss <= o.loss_max]
        passed.sort(key=lambda r: (r.loss, r.latency))
        self.emit("log", f"延迟测速完成：可用 {sum(1 for r in results if r.recv > 0)} 个，"
                         f"过滤后队列 {len(passed)} 个")
        if not passed:
            self.emit("error", "没有 IP 通过延迟/丢包过滤，请放宽延迟上下限或丢包上限后重试")
            self.emit("done", results)
            return results

        queue_ips = passed[: max(o.dl_count, 1)] if o.dl_count > 0 else passed
        self.emit("log", f"开始下载测速（队列：{len(queue_ips)}，单 IP 限时 {o.dl_time}s，"
                         f"地址：{o.speed_url}）")
        for idx, r in enumerate(queue_ips, 1):
            if self.stop.is_set():
                self.emit("stopped", None)
                break
            speed, colo, err = download_test(r.ip, o.port, host, path, o.dl_time)
            r.speed = round(speed, 2)
            if colo:
                r.colo = colo
            if err:
                r.error = err
                self.emit("log", f"[{idx}/{len(queue_ips)}] {r.ip} 下载测速失败：{err}")
            else:
                self.emit("log", f"[{idx}/{len(queue_ips)}] {r.ip} 下载速度 {r.speed} MB/s"
                                 + (f"（{r.colo}）" if colo else ""))
            self.emit("progress", (idx, len(queue_ips), f"下载测速 {idx}/{len(queue_ips)}"))

        results.sort(key=lambda r: (-r.speed, r.loss, r.latency))
        self.emit("done", results)
        return results

    def _latency_phase(self, host, path):
        o = self.o
        results = {ip: IpResult(ip) for ip in self.ips}
        done = 0
        total = len(self.ips)
        lock = threading.Lock()

        def work(ip):
            nonlocal done
            r = results[ip]
            ok_ms = []
            for _ in range(max(o.pings, 1)):
                if self.stop.is_set():
                    break
                if o.httping:
                    ms, colo = http_ping(ip, o.port, host, path, o.timeout)
                else:
                    ms = tcp_ping(ip, o.port, o.timeout)
                    colo = ""
                r.sent += 1
                if ms is not None:
                    r.recv += 1
                    ok_ms.append(ms)
                    if colo:
                        r.colo = colo
            if ok_ms:
                r.latency = round(sum(ok_ms) / len(ok_ms), 2)
            with lock:
                done += 1
                if done % max(1, total // 100) == 0 or done == total:
                    self.emit("progress", (done, total, f"延迟测速 {done}/{total}"))
            return r

        with ThreadPoolExecutor(max_workers=max(1, min(o.threads, max(total, 1)))) as pool:
            list(pool.map(work, self.ips))
        return [results[ip] for ip in self.ips]


# ---------------------------------------------------------------- CLI

def run_cli(argv):
    p = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VER} —— 自选 IP 测速（直连）")
    p.add_argument("--ips", default="", help="手动输入 IP，逗号/换行/空格分隔")
    p.add_argument("--api", default="", help="从网址取 IP 列表")
    p.add_argument("--selftest", action="store_true", help="只做直连自检")
    p.add_argument("--force", action="store_true", help="自检不通过也继续")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--n", type=int, default=100, help="延迟测速线程")
    p.add_argument("--t", type=int, default=4, help="每 IP 测速次数")
    p.add_argument("--dn", type=int, default=10, help="下载测速数量")
    p.add_argument("--dt", type=float, default=8.0, help="单 IP 下载测速秒数")
    p.add_argument("--url", default=Options.speed_url, help="下载测速地址")
    p.add_argument("--httping", action="store_true")
    p.add_argument("--csv", default="result.csv")
    p.add_argument("--max", type=int, default=500, help="单次 IP 上限")
    a = p.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ok, lines = direct_selftest()
    for lvl, text in lines:
        print(f"[{lvl}] {text}")
    print(f"直连自检：{'通过' if ok else '未通过'}")
    if not ok and not a.force:
        print("已阻断测速（加 --force 可强制继续）。")
        return 2
    if a.selftest:
        return 0 if ok else 2

    ips, bad, trunc = [], [], False
    if a.ips:
        ips, bad, trunc = parse_ip_text(a.ips, a.max)
    elif a.api:
        ips, err = fetch_ip_list(a.api, allow_proxy=False, log=lambda m: print("  " + m))
        if err:
            print("[错误] " + err)
            return 1
        ips = ips[: a.max]
    else:
        print("请用 --ips 或 --api 指定目标")
        return 1
    if bad:
        print(f"[提示] 忽略非法输入 {len(bad)} 项：{', '.join(bad[:10])}")
    if trunc:
        print(f"[提示] IP 超过上限，已截断为 {a.max} 个")
    if not ips:
        print("[错误] 没有可测的 IP")
        return 1

    opts = Options(port=a.port, threads=a.n, pings=a.t, dl_count=a.dn, dl_time=a.dt,
                   speed_url=a.url, httping=a.httping, max_ips=a.max, csv_path=a.csv)
    runner = Runner(ips, opts, emit=lambda t, p: _cli_emit(t, p, opts))
    results = runner.run()
    _print_table(results, opts.top_n)
    if a.csv:
        save_csv(results, a.csv)
        print(f"结果已写入 {a.csv}")
    return 0


def _cli_emit(kind, payload, opts):
    if kind == "log":
        print("· " + str(payload))
    elif kind == "error":
        print("[错误] " + str(payload))
    elif kind == "progress":
        cur, total, label = payload
        if total and (cur == total or cur % max(1, total // 20) == 0):
            print(f"  {label}")


def _print_table(results, top_n):
    rows = [r for r in results if r.recv > 0]
    rows.sort(key=lambda r: (-r.speed, r.loss, r.latency))
    print(f"{'IP 地址':<40}{'已发送':>6}{'已接收':>6}{'丢包率':>8}{'平均延迟':>10}{'下载速度(MB/s)':>16}{'地区码':>8}")
    for r in rows[:top_n]:
        print(f"{r.ip:<40}{r.sent:>6}{r.recv:>6}{r.loss:>8.2f}{r.latency:>10.2f}{r.speed:>16.2f}{r.colo:>8}")


def save_csv(results, path):
    rows = [r for r in results if r.recv > 0]
    rows.sort(key=lambda r: (-r.speed, r.loss, r.latency))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["IP 地址", "已发送", "已接收", "丢包率", "平均延迟", "下载速度(MB/s)", "地区码"])
        for r in rows:
            w.writerow([r.ip, r.sent, r.recv, f"{r.loss:.2f}", f"{r.latency:.2f}", f"{r.speed:.2f}", r.colo])


# ---------------------------------------------------------------- GUI

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"{APP_NAME} v{APP_VER} —— 自选 IP 测速（强制直连）")
            self.geometry("1180x820")
            self.minsize(980, 700)
            self.q = queue.Queue()
            self.worker = None
            self.stop_event = threading.Event()
            self.results = []
            self.self_test_ok = None
            self._build()
            self.after(120, self._drain)
            self.after(200, self.do_selftest)

        # ---- UI
        def _build(self):
            pad = dict(padx=8, pady=4)
            root = ttk.Frame(self)
            root.pack(fill="both", expand=True)

            # 输入区
            box = ttk.LabelFrame(root, text="1) 测速目标（二选一）")
            box.pack(fill="x", **pad)
            nb = ttk.Notebook(box)
            nb.pack(fill="both", expand=True, padx=6, pady=6)
            self.nb = nb

            f1 = ttk.Frame(nb)
            nb.add(f1, text="手动输入 IP")
            ttk.Label(f1, text="每行一个，也可用逗号/空格分隔；支持 CIDR（如 104.16.0.0/16 每 /24 随机取一个）").pack(anchor="w", padx=4)
            self.txt_ips = tk.Text(f1, height=6, wrap="none", font=("Consolas", 10))
            self.txt_ips.pack(fill="both", expand=True, padx=4, pady=4)

            f2 = ttk.Frame(nb)
            nb.add(f2, text="从 API 网址获取")
            row = ttk.Frame(f2)
            row.pack(fill="x", padx=4, pady=6)
            ttk.Label(row, text="网址：").pack(side="left")
            self.var_api = tk.StringVar(value="https://ip.ehb.cc.cd/v6-cm?ips=100")
            ttk.Entry(row, textvariable=self.var_api).pack(side="left", fill="x", expand=True, padx=4)
            ttk.Button(row, text="只取列表（不测速）", command=self.fetch_only).pack(side="left")
            self.var_api_proxy = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                f2, text="API 取列表允许走系统代理（仅取列表；测速流量仍强制直连）",
                variable=self.var_api_proxy,
            ).pack(anchor="w", padx=4)
            ttk.Label(f2, text="取回的 IP 会填入「手动输入 IP」页，可先核对再开始测速。",
                      foreground="#666").pack(anchor="w", padx=4, pady=2)

            # 参数区
            pbox = ttk.LabelFrame(root, text="2) 参数")
            pbox.pack(fill="x", **pad)
            grid = ttk.Frame(pbox)
            grid.pack(fill="x", padx=6, pady=6)

            def spin(row_, col, label, var, frm, to, inc=1, width=8):
                ttk.Label(grid, text=label).grid(row=row_, column=col * 2, sticky="e", padx=(6, 2), pady=3)
                sp = ttk.Spinbox(grid, from_=frm, to=to, increment=inc, textvariable=var, width=width)
                sp.grid(row=row_, column=col * 2 + 1, sticky="w", pady=3)
                return sp

            self.var_port = tk.IntVar(value=443)
            self.var_n = tk.IntVar(value=100)
            self.var_t = tk.IntVar(value=4)
            self.var_timeout = tk.DoubleVar(value=4.0)
            self.var_dn = tk.IntVar(value=10)
            self.var_dt = tk.DoubleVar(value=8.0)
            self.var_lmin = tk.DoubleVar(value=0.0)
            self.var_lmax = tk.DoubleVar(value=9999.0)
            self.var_loss = tk.DoubleVar(value=1.0)
            self.var_top = tk.IntVar(value=20)
            self.var_max = tk.IntVar(value=500)
            self.var_httping = tk.BooleanVar(value=False)

            spin(0, 0, "端口", self.var_port, 1, 65535)
            spin(0, 1, "延迟线程", self.var_n, 1, 1000, 10)
            spin(0, 2, "每 IP 次数", self.var_t, 1, 10)
            spin(1, 0, "超时(s)", self.var_timeout, 1, 30, 1)
            spin(1, 1, "下载测速数量", self.var_dn, 0, 500, 1)
            spin(1, 2, "下载限时(s)", self.var_dt, 1, 60, 1)
            spin(2, 0, "延迟下限(ms)", self.var_lmin, 0, 9999, 10)
            spin(2, 1, "延迟上限(ms)", self.var_lmax, 0, 9999, 10)
            spin(2, 2, "丢包上限", self.var_loss, 0, 1, 0.05)
            spin(3, 0, "显示条数", self.var_top, 1, 500, 5)
            spin(3, 1, "单次 IP 上限", self.var_max, 1, 20000, 100)

            ttk.Checkbutton(grid, text="HTTPing 模式（延迟用 HTTP 而非 TCP 握手）",
                            variable=self.var_httping).grid(row=3, column=4, columnspan=2, sticky="w", padx=6)

            row2 = ttk.Frame(pbox)
            row2.pack(fill="x", padx=6, pady=(0, 6))
            ttk.Label(row2, text="下载测速地址：").pack(side="left")
            self.var_url = tk.StringVar(value=Options.speed_url)
            ttk.Entry(row2, textvariable=self.var_url).pack(side="left", fill="x", expand=True, padx=4)
            ttk.Button(row2, text="恢复默认", command=lambda: self.var_url.set(Options.speed_url)).pack(side="left")

            # 直连自检
            sbox = ttk.LabelFrame(root, text="3) 直连自检")
            sbox.pack(fill="x", **pad)
            srow = ttk.Frame(sbox)
            srow.pack(fill="x", padx=6, pady=4)
            self.lbl_direct = ttk.Label(srow, text="检测中…", foreground="#b36b00")
            self.lbl_direct.pack(side="left")
            ttk.Button(srow, text="重新检测", command=self.do_selftest).pack(side="right")
            self.var_force = tk.BooleanVar(value=False)
            ttk.Checkbutton(srow, text="自检不通过也继续（不建议）",
                            variable=self.var_force).pack(side="right", padx=8)

            # 操作
            abox = ttk.Frame(root)
            abox.pack(fill="x", **pad)
            self.btn_start = ttk.Button(abox, text="开始测速", command=self.start)
            self.btn_start.pack(side="left")
            self.btn_stop = ttk.Button(abox, text="停止", command=self.stop_run, state="disabled")
            self.btn_stop.pack(side="left", padx=6)
            ttk.Button(abox, text="导出 CSV", command=self.export_csv).pack(side="left", padx=6)
            ttk.Button(abox, text="清空日志", command=lambda: self._clear(self.txt_log)).pack(side="left", padx=6)
            self.var_status = tk.StringVar(value="就绪")
            ttk.Label(abox, textvariable=self.var_status).pack(side="left", padx=12)
            self.pbar = ttk.Progressbar(abox, mode="determinate", length=280)
            self.pbar.pack(side="right")

            # 结果 + 日志
            paned = ttk.Panedwindow(root, orient="vertical")
            paned.pack(fill="both", expand=True, **pad)
            rbox = ttk.LabelFrame(paned, text="4) 结果（按下载速度排序）")
            cols = ("ip", "sent", "recv", "loss", "lat", "speed", "colo")
            self.tree = ttk.Treeview(rbox, columns=cols, show="headings", height=10)
            for c, t, w, anchor in (
                ("ip", "IP 地址", 320, "w"), ("sent", "已发送", 70, "center"),
                ("recv", "已接收", 70, "center"), ("loss", "丢包率", 80, "center"),
                ("lat", "平均延迟(ms)", 110, "center"), ("speed", "下载速度(MB/s)", 130, "center"),
                ("colo", "地区码", 80, "center"),
            ):
                self.tree.heading(c, text=t)
                self.tree.column(c, width=w, anchor=anchor)
            vsb = ttk.Scrollbar(rbox, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=vsb.set)
            self.tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
            vsb.pack(side="right", fill="y", pady=4)
            paned.add(rbox, weight=3)

            lbox = ttk.LabelFrame(paned, text="5) 运行日志")
            self.txt_log = tk.Text(lbox, height=8, wrap="word", font=("Consolas", 9))
            lvsb = ttk.Scrollbar(lbox, orient="vertical", command=self.txt_log.yview)
            self.txt_log.configure(yscrollcommand=lvsb.set)
            self.txt_log.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
            lvsb.pack(side="right", fill="y", pady=4)
            paned.add(lbox, weight=2)

        # ---- 工具
        def _clear(self, widget):
            widget.delete("1.0", "end")

        def log(self, msg):
            self.txt_log.insert("end", time.strftime("[%H:%M:%S] ") + str(msg) + "\n")
            self.txt_log.see("end")

        def _drain(self):
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "log":
                        self.log(payload)
                    elif kind == "error":
                        self.log("错误：" + str(payload))
                    elif kind == "progress":
                        cur, total, label = payload
                        self.pbar["maximum"] = max(total, 1)
                        self.pbar["value"] = cur
                        self.var_status.set(label)
                    elif kind == "done":
                        self._on_done(payload)
                    elif kind == "stopped":
                        self.log("已停止")
                        self._reset_buttons()
                    elif kind == "selftest":
                        self._on_selftest(payload)
            except queue.Empty:
                pass
            self.after(120, self._drain)

        def _reset_buttons(self):
            self.btn_start["state"] = "normal"
            self.btn_stop["state"] = "disabled"
            self.var_status.set("就绪")

        # ---- 自检
        def do_selftest(self):
            self.lbl_direct.config(text="直连自检中…", foreground="#b36b00")

            def work():
                ok, lines = direct_selftest()
                self.q.put(("selftest", (ok, lines)))

            threading.Thread(target=work, daemon=True).start()

        def _on_selftest(self, payload):
            ok, lines = payload
            self.self_test_ok = ok
            for lvl, text in lines:
                self.log(f"[{lvl}] {text}")
            if ok:
                self.lbl_direct.config(text="直连自检通过：无系统代理 / 无 VPN 网卡，出口为本地直连", foreground="#1a7f37")
            else:
                self.lbl_direct.config(text="直连自检未通过：存在代理或 VPN，测速结果不可信（详见日志）", foreground="#c62828")

        # ---- 取列表
        def fetch_only(self):
            url = self.var_api.get().strip()
            allow_proxy = self.var_api_proxy.get()
            self.log(f"开始从 API 取 IP 列表：{url}")

            def work():
                ips, err = fetch_ip_list(url, allow_proxy=allow_proxy, log=lambda m: self.q.put(("log", m)))
                self.q.put(("apiresult", (ips, err)))

            threading.Thread(target=work, daemon=True).start()

        # ---- 开始 / 停止
        def collect_ips(self):
            tab = self.nb.index(self.nb.select())
            if tab == 0:
                text = self.txt_ips.get("1.0", "end")
                return parse_ip_text(text, self.var_max.get())
            url = self.var_api.get().strip()
            if not url:
                return [], ["（未填写 API 网址）"], False
            ips, err = fetch_ip_list(url, allow_proxy=self.var_api_proxy.get(), log=self.log)
            if err:
                messagebox.showerror("API 取列表失败", err)
                return [], [], False
            if len(ips) > self.var_max.get():
                return ips[: self.var_max.get()], [], True
            return ips, [], False

        def start(self):
            if self.worker and self.worker.is_alive():
                return
            if self.self_test_ok is False and not self.var_force.get():
                messagebox.showerror(
                    "直连自检未通过",
                    "检测到系统代理或 VPN/TUN 网卡，测速流量可能被中转，结果不可信。\n\n"
                    "请先关闭代理软件与 VPN（或在系统代理设置里关闭代理）后点「重新检测」。\n"
                    "如确认要冒险测试，勾选「自检不通过也继续」。",
                )
                return
            ips, bad, trunc = self.collect_ips()
            if bad:
                self.log("忽略非法输入：" + ", ".join(bad[:10]))
            if trunc:
                self.log(f"IP 数量超过上限，已截断为 {self.var_max.get()} 个")
            if not ips:
                messagebox.showwarning("没有可测的 IP", "请输入至少一个合法 IP，或填写可用的 API 网址。")
                return

            opts = Options(
                port=int(self.var_port.get()), threads=int(self.var_n.get()),
                pings=int(self.var_t.get()), timeout=float(self.var_timeout.get()),
                dl_count=int(self.var_dn.get()), dl_time=float(self.var_dt.get()),
                lat_min=float(self.var_lmin.get()), lat_max=float(self.var_lmax.get()),
                loss_max=float(self.var_loss.get()), speed_url=self.var_url.get().strip(),
                top_n=int(self.var_top.get()), max_ips=int(self.var_max.get()),
                httping=bool(self.var_httping.get()),
            )
            self.tree.delete(*self.tree.get_children())
            self.pbar["value"] = 0
            self.btn_start["state"] = "disabled"
            self.btn_stop["state"] = "normal"
            self.var_status.set("运行中…")
            self.stop_event = threading.Event()
            self.log(f"待测 IP {len(ips)} 个：" + ", ".join(ips[:8]) + (" …" if len(ips) > 8 else ""))

            def work():
                runner = Runner(ips, opts, emit=lambda t, p: self.q.put((t, p)), stop_event=self.stop_event)
                try:
                    res = runner.run()
                    self.q.put(("results", res))
                except Exception as exc:  # noqa: BLE001
                    self.q.put(("error", f"运行异常：{type(exc).__name__}: {exc}"))
                    self.q.put(("stopped", None))

            self.worker = threading.Thread(target=work, daemon=True)
            self.worker.start()

        def stop_run(self):
            self.stop_event.set()
            self.var_status.set("正在停止…")

        def _on_done(self, results):
            self.results = results or []
            rows = [r for r in self.results if r.recv > 0]
            rows.sort(key=lambda r: (-r.speed, r.loss, r.latency))
            self.tree.delete(*self.tree.get_children())
            for r in rows[: self.var_top.get()]:
                self.tree.insert("", "end", values=(
                    r.ip, r.sent, r.recv, f"{r.loss:.2f}", f"{r.latency:.2f}",
                    f"{r.speed:.2f}", r.colo,
                ))
            ok_n = len(rows)
            self.log(f"测速结束：可用 {ok_n} 个 IP，结果表显示前 {min(len(rows), self.var_top.get())} 个")
            if not ok_n:
                messagebox.showwarning("没有结果", "所有 IP 均未通过测试，请检查端口、测速地址或网络直连状态。")
            self.pbar["value"] = self.pbar["maximum"]
            self._reset_buttons()

        def export_csv(self):
            if not self.results:
                messagebox.showinfo("无结果", "请先完成一次测速。")
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".csv", initialfile="result.csv",
                filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            )
            if not path:
                return
            save_csv(self.results, path)
            self.log("已导出：" + path)
            messagebox.showinfo("已导出", path)

    app = App()
    app.mainloop()
    return 0


def _frozen():
    return bool(getattr(sys, "frozen", False))


def _ensure_console():
    """打包成无控制台 exe 后，--cli 模式需要自己挂一个控制台窗口，否则看不到输出。"""
    if not _frozen() or sys.stdout is not None:
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AllocConsole():
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:
        pass


def _fatal_gui_error(exc):
    """无控制台时，把启动失败原因弹窗告诉用户，而不是静默退出。"""
    msg = (
        f"程序启动失败：\n\n{type(exc).__name__}: {exc}\n\n"
        "如果是缺少 tkinter，请安装完整版 Python 后重试；\n"
        "也可以改用命令行模式运行。"
    )
    try:
        import tkinter.messagebox as mb
        mb.showerror(f"{APP_NAME} 启动失败", msg)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, f"{APP_NAME} 启动失败", 0x10)
        except Exception:
            pass


def main():
    argv = sys.argv[1:]
    clear_proxy_env()
    if argv and argv[0] in ("--cli",):
        _ensure_console()
        return run_cli(argv[1:])
    if argv and argv[0] in ("--selftest",):
        _ensure_console()
        return run_cli(argv)
    try:
        return run_gui()
    except Exception as exc:  # noqa: BLE001
        _fatal_gui_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""
多功能终端框架 - 非阻塞 HTTP GET 状态机
闲沐工坊 · 框架服务

供功能模式在 loop() 里逐帧 tick 推进的 HTTP GET 客户端,
根治"等待网络时 UI 动画冻结"的问题:
  - 纯 http:// 走 socket(非阻塞) + uselect.poll 状态机,
    DNS → 连接 → 发送 → 接收 → 解析, 单帧只推进一小步
  - https:// 无法分步 (TLS 握手阻塞), start() 直接标记 BLOCK,
    由调用方用 get_json_blocking() 走旧 urequests 路径
  - 响应头解析 Content-Length 与 chunked 两种长度语义,
    Connection: close 时以 EOF 收尾; 响应体上限保护防撑爆内存
  - 全程不抛异常, 失败统一转化为 FAIL 状态 + error 字符串

用法 (模式内):
    req = http_nb.Request(url, timeout_ms=8000)
    req.start()
    # loop 每帧:
    st = req.tick()
    if st == http_nb.DONE:
        body = req.body()      # bytes
        status = req.status    # HTTP 状态码 (仅 DONE 后有效)
        req.close()
    elif st == http_nb.FAIL:
        print(req.error)
        req.close()
"""

import time

try:
    import usocket as socket
except ImportError:
    import socket
try:
    import uselect
except ImportError:
    import select as uselect
try:
    import uerrno as errno
except ImportError:
    import errno

# tick() 返回状态
RUN = "run"        # 仍在推进, 下一帧继续 tick
DONE = "done"      # 响应完成, body()/status 可用
FAIL = "fail"      # 失败, error 字符串可读
BLOCK = "block"    # https 等无法分步的 URL, 需调用方走阻塞路径

_MAX_HEADER = 16 * 1024    # 响应头上限
_MAX_BODY = 96 * 1024      # 响应体上限 (K线 JSON 峰值约 30KB)
_RECV_PER_TICK = 4         # 单 tick 最多 recv 次数
_RECV_SIZE = 512           # 单次 recv 字节 (控制单帧耗时)

# errno/socket 常量因子移植而异: ESP32 的 uerrno 精简掉 EWOULDBLOCK
# (语义与 EAGAIN 相同), 部分固件 socket 也缺 SO_ERROR — 一律防御性取值
_EAGAIN      = getattr(errno, "EAGAIN", 11)
_EWOULDBLOCK = getattr(errno, "EWOULDBLOCK", _EAGAIN)
_EINPROGRESS = getattr(errno, "EINPROGRESS", 115)
_RETRY_ERR = (_EAGAIN, _EWOULDBLOCK)
_SO_ERROR   = getattr(socket, "SO_ERROR", None)  # None=无法查询, 交给后续 send/recv 暴露


def _parse_url(url):
    """http://host[:port]/path → (host, port, path); 不支持返回 None"""
    if url.startswith("http://"):
        rest = url[7:]
        port = 80
    elif url.startswith("https://"):
        rest = url[8:]
        port = 443
    else:
        return None
    slash = rest.find("/")
    if slash < 0:
        hostport, path = rest, "/"
    else:
        hostport, path = rest[:slash], rest[slash:]
    colon = hostport.rfind(":")
    if colon > 0:
        try:
            port = int(hostport[colon + 1:])
            hostport = hostport[:colon]
        except ValueError:
            pass
    if not hostport:
        return None
    return hostport, port, path


class Request(object):
    """单次非阻塞 HTTP GET. 生命周期 idle → conn/send/recv → done/fail"""

    def __init__(self, url, headers=None, timeout_ms=8000):
        self.url = url
        self.status = 0        # HTTP 状态码 (DONE 后有效)
        self.error = None      # FAIL 原因
        self._timeout_ms = timeout_ms
        self._headers = dict(headers or {})
        self._sock = None
        self._state = "idle"
        self._deadline = 0
        self._req = b""        # 待发送请求报文
        self._req_off = 0
        self._rx = bytearray() # 原始接收缓冲
        self._hdr_ok = False
        self._body = bytearray()
        self._clen = -1        # Content-Length, -1 = 到 EOF 结束
        self._chunked = False
        self._ch_remain = 0         # chunked: 当前 chunk 剩余字节
        self._ch_done = False       # chunked: 已收到终止块
        self._ch_need_crlf = False  # chunked: 数据块后待跳过 CRLF
        self._clean = bytearray()   # chunked: 解析出的净数据

    # ---------- 对外 ----------

    def start(self):
        """发起请求 (DNS 一次性解析, 常规 <100ms; https 直接判 BLOCK)"""
        parsed = _parse_url(self.url)
        if parsed is None:
            return self._fail("bad url")
        host, port, path = parsed
        if self.url.startswith("https://"):
            self._state = "block"
            return BLOCK
        self._deadline = time.ticks_add(time.ticks_ms(), self._timeout_ms)
        try:
            ai = socket.getaddrinfo(host, port)
            sa = ai[0][4]
        except Exception as e:
            return self._fail("dns:" + str(e).split("'")[0])
        try:
            s = socket.socket()
            s.setblocking(False)
            s.connect(sa)
            self._sock = s
            self._enter_send()
        except OSError as e:
            if e.args and e.args[0] == _EINPROGRESS:
                self._sock = s
                self._state = "conn"
            else:
                try:
                    s.close()
                except Exception:
                    pass
                return self._fail("conn:" + str(e.args))
        except Exception as e:
            return self._fail("conn:" + str(e))
        return RUN

    def tick(self):
        """每帧推进一小步, 返回 RUN/DONE/FAIL (start 未调用时自动 start)"""
        if self._state == "idle":
            return self.start()
        if self._state == "block":
            return BLOCK
        if self._state == "done":
            return DONE
        if self._state == "fail":
            return FAIL
        if time.ticks_diff(self._deadline, time.ticks_ms()) <= 0:
            return self._fail("timeout")
        if self._state == "conn":
            self._tick_conn()
        elif self._state == "send":
            self._tick_send()
        elif self._state == "recv":
            self._tick_recv()
        if self._state == "done":
            return DONE
        if self._state == "fail":
            return FAIL
        return RUN

    def body(self):
        return bytes(self._clean if self._chunked else self._body)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._rx = bytearray()
        if self._state not in ("done", "fail", "block", "idle"):
            self._state = "fail"

    # ---------- 内部状态 ----------

    def _fail(self, msg):
        self.error = msg
        self._state = "fail"
        self.close()
        return FAIL

    def _enter_send(self):
        host, port, path = _parse_url(self.url)
        lines = ["GET " + path + " HTTP/1.1",
                 "Host: " + host,
                 "User-Agent: MicroPython/XianMuBox",
                 "Connection: close",
                 "Accept: */*"]
        for k, v in self._headers.items():
            lines.append(str(k) + ": " + str(v))
        self._req = ("\r\n".join(lines) + "\r\n\r\n").encode()
        self._req_off = 0
        self._state = "send"

    def _tick_conn(self):
        """connect 进行中: 等 POLLOUT 后查 SO_ERROR (固件缺失时跳过查询)"""
        try:
            p = uselect.poll()
            p.register(self._sock, uselect.POLLOUT)
            ev = p.poll(0)
            p.unregister(self._sock)
            if not ev:
                return
            if _SO_ERROR is not None:
                err = self._sock.getsockopt(socket.SOL_SOCKET, _SO_ERROR)
                if err != 0:
                    self._fail("conn:" + str(err))
                    return
            self._enter_send()
        except Exception as e:
            self._fail("conn:" + str(e))

    def _tick_send(self):
        try:
            n = self._sock.send(self._req[self._req_off:self._req_off + 512])
            if n:
                self._req_off += n
        except OSError as e:
            if e.args and e.args[0] not in _RETRY_ERR:
                self._fail("send:" + str(e.args))
                return
        except Exception as e:
            self._fail("send:" + str(e))
            return
        if self._req_off >= len(self._req):
            self._state = "recv"

    def _tick_recv(self):
        # 数据可用即连续排空 (15ms 单帧时间预算, 不限 recv 次数):
        # 40ms 帧间隔的慢读会触发部分服务器在 Connection: close 下
        # 提前关连, 未读走的缓冲数据被丢弃 → truncated
        deadline = time.ticks_add(time.ticks_ms(), 15)
        while True:
            try:
                chunk = self._sock.recv(_RECV_SIZE)
            except OSError as e:
                if e.args and e.args[0] in _RETRY_ERR:
                    return  # 本帧内核缓冲已排空
                self._fail("recv:" + str(e.args))
                return
            except Exception as e:
                self._fail("recv:" + str(e))
                return
            if not chunk:
                self._finish(eof=True)
                return
            self._rx.extend(chunk)
            if len(self._rx) > _MAX_HEADER + _MAX_BODY:
                self._fail("too large")
                return
            if not self._hdr_ok:
                self._split_header()
                if self._state != "recv":
                    return
            if self._body_done():
                self._finish(eof=False)
                return
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return  # 单帧时间预算用尽, 余量下帧继续

    def _split_header(self):
        """从 _rx 切出响应头并解析长度语义; 头不完整时返回 False"""
        i = self._rx.find(b"\r\n\r\n")
        if i < 0:
            if len(self._rx) > _MAX_HEADER:
                self._fail("hdr too large")
            return False
        try:
            head = bytes(self._rx[:i]).decode()
        except Exception:
            return self._fail("bad hdr")
        rows = head.split("\r\n")
        try:
            self.status = int(rows[0].split(" ")[1])
        except Exception:
            return self._fail("bad status")
        low = head.lower()
        self._chunked = "transfer-encoding:" in low and \
            "chunked" in low.split("transfer-encoding:")[1].split("\r\n")[0]
        self._clen = -1
        if not self._chunked:
            for r in rows[1:]:
                if r.lower().startswith("content-length:"):
                    try:
                        self._clen = int(r.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break
        self._hdr_ok = True
        rest = self._rx[i + 4:]
        self._rx = bytearray()
        if rest:
            self._body.extend(rest)
        return True

    def _body_done(self):
        if self._chunked:
            self._feed_chunked()
            return self._ch_done
        if self._clen >= 0:
            return len(self._body) >= self._clen
        return False  # 无长度语义, 等 EOF

    def _feed_chunked(self):
        """增量解析 chunked 流: _body=未消费原始流, _clean=净数据"""
        while not self._ch_done:
            if self._ch_need_crlf:
                if len(self._body) < 2:
                    return
                self._body = self._body[2:]
                self._ch_need_crlf = False
                continue
            if self._ch_remain > 0:
                take = len(self._body)
                if take == 0:
                    return
                take = min(take, self._ch_remain)
                self._clean.extend(self._body[:take])
                self._body = self._body[take:]
                self._ch_remain -= take
                if self._ch_remain == 0:
                    self._ch_need_crlf = True
                continue
            nl = self._body.find(b"\r\n")
            if nl < 0:
                if len(self._body) > 32:
                    self._fail("bad chunk")
                return
            try:
                line = bytes(self._body[:nl]).decode()
                size = int(line.split(";")[0].strip(), 16)
            except Exception:
                self._fail("bad chunk")
                return
            self._body = self._body[nl + 2:]
            if size == 0:
                self._ch_done = True
                return
            self._ch_remain = size

    def _finish(self, eof):
        # 截断一律硬失败: 部分 JSON 交给上层解析只会得到 syntax error,
        # 不如诚实上报 (错误含 已收/应到 字节数) 由周期刷新自愈
        if self._chunked:
            self._feed_chunked()
            if not self._ch_done:
                return self._fail("truncated(chunked)")
            self._body = self._clean
        elif self._clen >= 0 and len(self._body) < self._clen:
            return self._fail("truncated %d/%d" % (len(self._body),
                                                   self._clen))
        self._state = "done"


def get_json_blocking(url, headers=None, timeout_ms=8000):
    """阻塞 GET JSON (https 回退路径) → (status, dict|None), 不抛异常"""
    try:
        import urequests as requests
    except ImportError:
        import requests
    try:
        r = requests.get(url, headers=dict(headers or {}),
                         timeout=max(1, timeout_ms // 1000))
        try:
            data = r.json() if r.status_code == 200 else None
        except Exception:
            data = None
        finally:
            try:
                r.close()
            except Exception:
                pass
        return r.status_code, data
    except Exception:
        return 0, None


def get_text_blocking(url, headers=None, timeout_ms=6000):
    """阻塞 GET 原始字节 (非 JSON 响应, 如腾讯行情 v_xxx="...") →
    (status, bytes|None), 不抛异常。

    与 get_json_blocking 同源, 供行情链路在非阻塞读取失败时兜底;
    GBK 响应不解码 (调用方只消费纯 ASCII 字段)。
    """
    try:
        import urequests as requests
    except ImportError:
        import requests
    try:
        r = requests.get(url, headers=dict(headers or {}),
                         timeout=max(1, timeout_ms // 1000))
        try:
            body = getattr(r, "content", None)
            if body is None:
                raw = getattr(r, "raw", None)
                body = raw.read() if raw is not None else None
        finally:
            try:
                r.close()
            except Exception:
                pass
        return r.status_code, body
    except Exception:
        return 0, None

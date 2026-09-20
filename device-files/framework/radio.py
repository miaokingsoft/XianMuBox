"""
XianMuBox - 网络电台服务层 (radio_relay, v2.6 纯实时流式直连)

闲沐盒子数据服务平台 radio_relay 电台转播 (2026-09-18 起 M6 移除
分段下载, 唯一交付路径 = 流式端点):
  GET /api/v1/stream/radio_relay?mode=mp3&station=<code>
  HTTP/1.1 chunked 长连接, audio/mpeg 连续透传, 即时起播零转码;
  首个听众到达服务端才连上游, 无人听 30 分钟回收; 同一上游多听众
  共享广播; 听众上限 10/台 (总 30), 超限 42901; 上游静默 >90s 结束
  听众流 (重播即恢复)。上游真实地址只存服务端内存, 目录/日志不回源。

直连直播 (v2.6): stream_url 格式稳定, 由台代码直接构造
(stream_url()), 收听路径零取数 —— 直播即时起播, 无需先查状态;
电台目录用内置快照 (纯内存, 与平台 2026-09-19 目录一致, 滞后新台
时流端点报 40401 -> 「电台暂不可用」, 无害)。

盒子端 chunked 支持: 平台文档标注「chunked 对现役固件不可用」指
urequests 不解 chunked 帧; 本层用裸 socket + ssl/ussl 自实现流式
客户端 (手工解析响应头 + chunk-size 行), 供 audio.play_mp3 的
read_fn 逐块供数 —— 不改固件即接新契约, 与 http_nb 的 chunked
解析经验同源。真机坑: v1.20 TLS 模块名为 ssl 无 ussl 别名;
SSLSocket 无 settimeout (超时须 wrap 前设在原生 socket 上)。

播放失败/断流语义:
  "ok"(流结束)   上游静默 >90s / 连接被断 —— 直播无「播完」概念,
                 ok 即断流, 调用方短退避后用原 stream_url 重连
  "cancelled"    按键打断
  "limit"        42901 听众已满 (60s 后重试)
  "noent"        40401/40001 无此台/参数错 (不自动重试)
  "stop"         50301/50302 服务停用 (不自动重试)
  "noauth"       40302/40101/40102 未订阅/未绑定 (不自动重试)
  "fail"         连接失败/非音频响应/解码/I2S

MicroPython 兼容: 无 f-string / time.ticks_* / usocket+ssl/ussl 双名
惰性 import
"""

import time

_STREAM_TIMEOUT_S = 30          # 流 socket 统一超时 (秒; 建连+读同档——
                                # v1.20 SSLSocket 无 settimeout, 须 wrap
                                # 前设在原生 socket 上; 服务端静默 >90s
                                # 断流, 客户端 30s 无数据即断开重连)
_STREAM_HEAD_MAX = 4096         # 响应头最多读取字节
_STREAM_PORT_HTTPS = 443

# 电台目录内置快照 (平台 2026-09-19 目录; 平台加新台时此处同步更新,
# 未更新前新台选到显「电台暂不可用」)
_FALLBACK_STATIONS = [
    {"code": "cnr1", "name": "中国之声"},
    {"code": "gd_music", "name": "广东音乐之声"},
    {"code": "gd_traffic", "name": "羊城交通电台"},
    {"code": "js_music", "name": "江苏经典流行音乐"},
    {"code": "ah_story", "name": "安徽小说评书广播"},
    {"code": "lg_voice", "name": "两广之声音乐台"},
    {"code": "intl_news", "name": "国际新闻"},
    {"code": "classic500", "name": "500首华语经典"},
    {"code": "hj_music", "name": "怀集音乐之声"},
    {"code": "rthk1", "name": "RTHK Radio-1"},
]


def media_url(path):
    """站内相对路径 -> 完整 URL (拼平台 endpoint), 与 news 同款"""
    p = str(path or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    try:
        from framework.settings import get_cloud
        base = str(get_cloud().get("endpoint", "")).rstrip("/")
    except Exception:
        base = ""
    if not base:
        return ""
    return base + (p if p.startswith("/") else "/" + p)


def stream_url(code):
    """拼直播流端点完整 URL (stream_url 格式稳定, 由台代码直接构造,
    收听路径零取数 —— 直播即时起播, 无需先查状态)"""
    return media_url("/api/v1/stream/radio_relay?mode=mp3&station=%s"
                     % str(code or "").strip())


def stations(force=False):
    """电台目录 [{"code","name"}...]: 纯内置快照 (纯内存秒出, 不发
    网络请求)。目录以平台取数 payload.stations 为准, 快照可能滞后
    (滞后台选到时流端点报 40401 -> 「电台暂不可用」, 无害)。"""
    return [dict(s) for s in _FALLBACK_STATIONS]


def _parse_url(url):
    """拆 URL -> (host, port, path, is_https); 解析失败返回 None"""
    p = str(url or "").strip()
    if p.startswith("https://"):
        rest, is_https = p[8:], True
    elif p.startswith("http://"):
        rest, is_https = p[7:], False
    else:
        return None
    slash = rest.find("/")
    if slash < 0:
        host, path = rest, "/"
    else:
        host, path = rest[:slash], rest[slash:]
    port = _STREAM_PORT_HTTPS if is_https else 80
    if ":" in host:
        host, _, ps = host.partition(":")
        try:
            port = int(ps)
        except ValueError:
            return None
    return host, port, path, is_https


def _parse_head(head):
    """解析 HTTP 响应头 -> (status, ctype, transfer_encoding)。

    坑: MicroPython 的 str(bytes) 得到 "b'...'" repr 字面量而非文本,
    字节切片必须用 bytes 的切片语法 str(b, "utf-8") 显式解码 —— 真机
    曾因 str(k) 致 content-type 键名永远匹配不上 (ctype='') 而静默
    拒流 (2026-09-19)。
    """
    lines = head.split(b"\r\n")
    status_line = lines[0] if lines else b""
    try:
        status = int(status_line.split(b" ", 2)[1])
    except (IndexError, ValueError):
        raise OSError("bad status line")
    ctype = ""
    te = ""
    for ln in lines[1:]:
        if b":" not in ln:
            continue
        k, _, v = ln.partition(b":")
        key = str(k, "utf-8").strip().lower()
        if key == "content-type":
            ctype = str(v, "utf-8").strip().lower()
        elif key == "transfer-encoding":
            te = str(v, "utf-8").strip().lower()
    return status, ctype, te


def _open_stream(url):
    """建流式连接: 裸 socket (+ usssl) -> 手工 HTTP 响应头解析。

    返回 (sock, chunked, err_code): sock 供 _chunked_read 使用,
    err_code 非空表示失败 (sock 为 None):
      "limit"/"noent"/"stop"/...  错误信封业务码透传
      "fail"                      连接失败/非音频响应/协议异常
    """
    import gc
    gc.collect()
    parts = _parse_url(url)
    if parts is None:
        return None, False, "fail"
    host, port, path, is_https = parts
    sock = None
    try:
        import socket as usocket
        ai = usocket.getaddrinfo(host, port, 0, usocket.SOCK_STREAM)[0]
        sock = usocket.socket(ai[0], usocket.SOCK_STREAM, ai[2])
        # 超时必须在 wrap 前设在原生 socket 上: v1.20 SSLSocket 无
        # settimeout (仅 read/write/close), wrap 后读阻塞继承底层;
        # 新固件 SSLSocket 若支持, wrap 后 try 覆盖同值
        sock.settimeout(_STREAM_TIMEOUT_S)
        print("[RADIO] connecting %s:%s ..." % (host, port))
        sock.connect(ai[-1])
        if is_https:
            # TLS 模块名随固件而异 (v1.20 ESP32 注册为 ssl, urequests
            # 同款用法; 老 port 为 ussl), 双名兼容
            try:
                import ussl
            except ImportError:
                import ssl as ussl
            print("[RADIO] tls handshake (%s) ..." % ussl.__name__)
            try:
                sock = ussl.wrap_socket(sock, server_hostname=host)
            except TypeError:
                sock = ussl.wrap_socket(sock)
            try:
                sock.settimeout(_STREAM_TIMEOUT_S)
            except AttributeError:
                pass
            print("[RADIO] tls ok")
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: XianMuBox\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n"
               % (path, host))
        try:
            n_sent = sock.write(req.encode())
            if n_sent is not None and n_sent < len(req):
                raise OSError("req partial write %d/%d"
                              % (n_sent, len(req)))
        except TypeError:
            sock.write(req.encode())      # 旧固件 write 无返回值
        # 手工读响应头 (chunked 流无法用 urequests: 其不解 chunk 帧)
        buf = b""
        while b"\r\n\r\n" not in buf:
            piece = sock.read(512)
            if piece is None:
                raise OSError("head read None (timeout/nonblock)")
            if not piece:
                raise OSError("head eof (peer closed)")
            buf += piece
            if len(buf) > _STREAM_HEAD_MAX:
                raise OSError("head too large")
        head, _, buf = buf.partition(b"\r\n\r\n")
        status, ctype, te = _parse_head(head)
        print("[RADIO] resp: status=%s ctype=%s te=%s"
              % (status, ctype or "?", te or "?"))
        if status != 200 or "json" in ctype:
            # 错误路径为统一 JSON 信封 (校验顺序: 50301/40401/40102/
            # 40101/40302/40001/42901)
            code = "fail"
            try:
                if "json" in ctype:
                    body = sock.read(1024)
                    print("[RADIO] err body:", body[:200])
                    try:
                        import ujson as json
                    except ImportError:
                        import json
                    env = json.loads(body)
                    code = str(env.get("code") or "fail")
            except Exception as e:
                print("[RADIO] err body parse fail:", repr(e))
            try:
                sock.close()
            except Exception:
                pass
            print("[RADIO] stream http:", status, ctype)
            return None, False, code
        if "audio" not in ctype:
            # 上游非 MP3: 服务端保证一字节不透传, 防御性拒绝
            print("[RADIO] stream non-audio ctype:", repr(ctype))
            try:
                sock.close()
            except Exception:
                pass
            return None, False, "fail"
        chunked = "chunked" in te
        print("[RADIO] stream open: chunked=%s" % chunked)
        return sock, chunked, ""
    except Exception as e:
        print("[RADIO] stream connect error:", repr(e))
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        return None, False, "fail"


def _make_chunked_read(sock, chunked):
    """chunked / 裸流供数函数 (play_mp3 的 read_fn)。

    手工解 chunk 帧 (十六进制长度行 + 数据 + 尾 CRLF); 任何读超时/
    断流一律关闭并返回 b"" —— play_mp3 以 EOF 正常收尾, 上层把
    直播的「流结束」当断流短退避重连。
    """
    state = {"remain": 0, "buf": b"", "done": False, "got": 0,
             "raw": False}

    def _fill(n):
        # 确保缓冲至少 n 字节, 失败返回 False (失败原因打日志)
        while len(state["buf"]) < n:
            try:
                piece = sock.read(min(4096, n - len(state["buf"])))
            except Exception as e:
                print("[RADIO] stream read error:", repr(e),
                      "got=%d" % state["got"])
                return False
            if piece is None:
                print("[RADIO] stream read None (timeout/nonblock),"
                      " got=%d" % state["got"])
                return False
            if not piece:
                print("[RADIO] stream eof (peer closed), got=%d"
                      % state["got"])
                return False
            state["buf"] += piece
        return True

    def _read(n):
        if state["done"]:
            return b""
        try:
            if chunked and not state["raw"]:
                if state["remain"] == 0:
                    # 读下一 chunk 的十六进制长度行
                    if not _fill_head():
                        state["done"] = True
                        print("[RADIO] stream end at chunk-line,"
                              " got=%d" % state["got"])
                        return b""
                    line, _, state["buf"] = state["buf"].partition(b"\r\n")
                    size_ok = True
                    try:
                        state["remain"] = int(line.strip() or b"0", 16)
                    except ValueError:
                        # 部分服务端节点声明 chunked 实际发裸流 (真机
                        # 实测): 非法 size 行 = 数据已开始, 还原该行与
                        # 其行尾后切裸流透传, 音频字节不丢
                        print("[RADIO] chunked frame invalid,"
                              " fallback raw passthrough")
                        state["raw"] = True
                        state["buf"] = line + b"\r\n" + state["buf"]
                        size_ok = False
                    if size_ok and state["remain"] == 0:
                        state["done"] = True       # 终止 chunk
                        print("[RADIO] stream end (chunk 0), got=%d"
                              % state["got"])
                        return b""
                if not state["raw"]:
                    need = min(n, state["remain"])
                    if not _fill(need):
                        state["done"] = True
                        return b""
                    data, state["buf"] = state["buf"][:need], \
                        state["buf"][need:]
                    state["remain"] -= len(data)
                    state["got"] += len(data)
                    if state["remain"] == 0 and not _eat_crlf():
                        state["done"] = True
                    return data
            # 裸流 (声明非 chunked, 或声明 chunked 实际裸流的降级):
            # 有数据即返回 (play_mp3 的 read_fn 允许短于 n)
            if not state["buf"]:
                try:
                    piece = sock.read(n)
                except Exception as e:
                    print("[RADIO] stream read error:", repr(e),
                          "got=%d" % state["got"])
                    state["done"] = True
                    try:
                        sock.close()
                    except Exception:
                        pass
                    return b""
                if piece is None:
                    print("[RADIO] stream read None (timeout/nonblock),"
                          " got=%d" % state["got"])
                    state["done"] = True
                    return b""
                if not piece:
                    print("[RADIO] stream raw eof, got=%d" % state["got"])
                    state["done"] = True
                    return b""
                state["buf"] = piece
            data, state["buf"] = state["buf"], b""
            state["got"] += len(data)
            return data
        except Exception as e:
            print("[RADIO] stream read exception:", repr(e),
                  "got=%d" % state["got"])
            state["done"] = True
            try:
                sock.close()
            except Exception:
                pass
            return b""

    def _fill_head():
        # 读到缓冲出现 "\r\n" (chunk 长度行结束) 或失败
        while b"\r\n" not in state["buf"]:
            try:
                piece = sock.read(256)
            except Exception as e:
                print("[RADIO] stream read error:", repr(e),
                      "got=%d" % state["got"])
                return False
            if piece is None:
                print("[RADIO] stream read None (timeout/nonblock),"
                      " got=%d" % state["got"])
                return False
            if not piece:
                print("[RADIO] stream eof (peer closed), got=%d"
                      % state["got"])
                return False
            state["buf"] += piece
        return True

    def _eat_crlf():
        # chunk 数据后的分隔 CRLF (remain 耗尽时缓冲头应恰为 \r\n)
        if not _fill(2):
            return False
        if state["buf"][:2] == b"\r\n":
            state["buf"] = state["buf"][2:]
        return True

    return _read


def play_stream(url, volume=80, on_progress=None, cancelled=None):
    """收听直播流: 建流式连接 -> audio.play_mp3 边下边播 (可打断)。

    返回 (state, played_sec): state 为
      "ok"        流结束 (服务端静默 >90s / 连接被断 —— 直播无播完
                  概念, 调用方应短退避后用原 url 重连)
      "cancelled" 播放中被按键打断
      "limit"     听众已满 (42901, 信封 retry_after 为重试秒)
      "noent"/"stop"/其他业务码  流端点错误信封 (40401 无此台/
                  50301 停用等), 不自动重试
      "badmp3"    解码器解不出帧
      "noauth"    40302/40101/40102 未订阅/未绑定 (不自动重试)
      "fail"      连接失败/非音频响应/解码/I2S 异常
    """
    import gc
    if not url:
        return ("fail", 0)
    try:
        from framework import audio
        if not audio.available():
            print("[RADIO] audio unavailable: i2s not available")
            return ("fail", 0)
        if not audio.mp3_supported():
            print("[RADIO] mp3dec missing")
            return ("fail", 0)
    except Exception as e:
        print("[RADIO] audio import error:", e)
        return ("fail", 0)
    gc.collect()
    sock, chunked, code = _open_stream(url)
    if sock is None:
        if code == "42901":
            return ("limit", 0)
        if code in ("40401", "40001"):
            return ("noent", 0)
        if code in ("50301", "50302"):
            return ("stop", 0)
        if code in ("40302", "40101", "40102"):
            # 未订阅/未绑定/Token 失效 (stream_require_auth=true 时)
            return ("noauth", 0)
        return ("fail", 0)
    holder = {"sec": 0}

    def _prog(sec):
        holder["sec"] = sec
        if on_progress:
            on_progress(sec)

    try:
        read_fn = _make_chunked_read(sock, chunked)
        ok, err = audio.play_mp3(read_fn, volume=volume,
                                 is_cancelled=cancelled,
                                 on_progress=_prog)
        print("[RADIO] play_mp3 end: ok=%s err=%s played=%ds"
              % (ok, err or "-", holder["sec"]))
        if err == "cancelled":
            return ("cancelled", holder["sec"])
        if ok:
            return ("ok", holder["sec"])
        if err == "bad mp3":
            return ("badmp3", holder["sec"])
        return ("fail", holder["sec"])
    except Exception as e:
        print("[RADIO] stream error:", repr(e))
        return ("fail", holder["sec"])
    finally:
        try:
            sock.close()
        except Exception:
            pass
        gc.collect()

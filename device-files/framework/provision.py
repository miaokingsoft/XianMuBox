"""
XianMuBox - WiFi 配网服务 (SoftAP + Captive Portal)
闲沐工坊 · 插件式架构

解决"新设备/密码失效时无任何配网手段"的开箱问题:
设备开启热点 XianMuBox-XXXX (开放网络), 手机连接后由 DNS 劫持自动弹出
配网页, 选择家中 WiFi 并输入密码, 设备保存凭据并试连, 成功后自动收摊。

组成:
  - AP 热点       network.WLAN(AP_IF), SSID 取设备 SN 末 4 位
  - DNS 劫持      UDP 53 端口, 所有域名解析到 AP IP (Captive Portal 探测)
  - 配网 HTTP     TCP 80 端口, 独立轻量实现 (不复用 http_server, 后者
                  专职 STA 局域网 Web 配置, 职责单一且生命周期不同)
  - 设备端配网页  run_provision_page(): 二维码 + 状态展示 (阻塞式子页)
  - 开机引导      boot_provision_check(): main.py 开头调用, 未联网即进配网页

HTTP 接口 (仅配网期间在 AP 网段可用):
  GET  /                      配网页面 (手机浏览器)
  GET  /api/provision/scan    扫描周围热点列表 (JSON)
  POST /api/provision/save    提交 SSID/密码, 持久化并试连 (JSON)
  GET  /api/provision/status  查询连接状态 (JSON)
  其余 GET                    302 重定向到配网页 (兼容各系统探测路径)

使用方 (settings 模式 / main.py) 只需:
  - run_provision_page(display, buttons, buzzer)  阻塞配网页
  - boot_provision_check(display, buttons, buzzer) 开机检查+引导
"""

import time
import gc
import json

from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_CYAN,
    CLR_DIM, CLR_GREEN, CLR_RED, CLR_AMBER, CLR_BLACK,
)

# STA 连接复用 framework.wifi 的非阻塞连接器与单例 WLAN
try:
    from framework.wifi import WiFiConnector, _get_wlan
except ImportError:
    WiFiConnector = None
    _get_wlan = None

try:
    from framework import settings as _cfg
except ImportError:
    _cfg = None

# 最多保留的 WiFi 凭据条数 (与 settings.json wifi 节点约定一致)
_MAX_WIFI_ENTRIES = 5

# ===================== 模块级状态 =====================
_ap = None            # AP 接口 (network.WLAN)
_ap_ip = "192.168.4.1"
_ap_name = ""
_dns_sock = None      # UDP 53 劫持 socket
_http_sock = None     # TCP 80 配网服务 socket
_connector = None     # WiFiConnector 实例 (试连中)
_state = "idle"       # idle / connecting / success / failed
_conn_ip = None       # 试连成功后的 STA IP


def _sn_suffix():
    """取设备 SN 末 4 位作热点后缀 (如 SN-AABBCCDDEEFF -> EEFF)"""
    try:
        sn = str(_cfg.get_device_sn())
        return sn[-4:]
    except Exception:
        return "0000"


def ap_ssid():
    """配网热点名: XianMuBox-<SN末4位>"""
    return "XianMuBox-" + _sn_suffix()


# ===================== AP / DNS / HTTP 生命周期 =====================

def start_provisioning():
    """启动配网: AP 热点 + DNS 劫持 + 配网 HTTP。

    返回 (ap_ssid, ap_ip)；失败返回 (None, None)。
    """
    global _ap, _ap_ip, _ap_name, _dns_sock, _http_sock

    if _ap is not None:
        return (_ap_name, _ap_ip)

    try:
        import network
        import socket

        _ap = network.WLAN(network.AP_IF)
        _ap_name = ap_ssid()
        # 顺序强制: esp_wifi 协议栈在任意接口首次 active(True) 时才初始化。
        # 新设备开机 STA 从未激活过, 此时对未激活的 AP 接口 config(essid=...)
        # 会被驱动以 NOT INIT 拒绝 (真机报 AP START FAIL 的根因), 必须先
        # 启动 AP 再配置名称/认证方式
        _ap.active(True)

        # 等 AP 就绪 (最多 2s)
        start = time.ticks_ms()
        while not _ap.active():
            if time.ticks_diff(time.ticks_ms(), start) > 2000:
                raise OSError("AP not active")
            time.sleep_ms(50)

        # 开放网络: 配网页面是配网期间的临时通道, 免输热点密码
        # (行业常见做法; 成功收摊后热点即关闭)
        try:
            _ap.config(essid=_ap_name, authmode=network.AUTH_OPEN)
        except Exception:
            _ap.config(essid=_ap_name)

        try:
            _ap_ip = _ap.ifconfig()[0]
        except Exception:
            _ap_ip = "192.168.4.1"

        # DNS 劫持: UDP 53, 所有查询应答 A 记录指向 AP IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 53))
        s.setblocking(False)
        _dns_sock = s

        # 配网 HTTP: TCP 80
        for _attempt in range(3):
            try:
                addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
                h = socket.socket()
                h.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                h.bind(addr)
                h.listen(2)
                h.setblocking(False)
                _http_sock = h
                break
            except OSError:
                try:
                    h.close()
                except Exception:
                    pass
                time.sleep_ms(200)

        print("[PROV] AP:", _ap_name, "ip:", _ap_ip,
              "dns:", _dns_sock is not None, "http:", _http_sock is not None)
        return (_ap_name, _ap_ip)
    except Exception as e:
        print("[PROV] start error:", e)
        stop_provisioning()
        return (None, None)


def stop_provisioning():
    """关闭配网: 释放 DNS/HTTP socket 与 AP 热点, 回收内存"""
    global _ap, _dns_sock, _http_sock, _connector, _state
    for s in (_dns_sock, _http_sock):
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    _dns_sock = None
    _http_sock = None
    _connector = None
    _state = "idle"
    if _ap is not None:
        try:
            _ap.active(False)
        except Exception:
            pass
        _ap = None
    gc.collect()


def is_provisioning():
    return _ap is not None


def provision_state():
    """返回配网状态摘要 dict (设备端页面与 HTTP 接口共用)"""
    ssid = None
    if _connector is not None:
        try:
            ssid = _connector.current_ssid
        except Exception:
            ssid = None
    return {
        "state": _state,
        "ssid": ssid,
        "ip": _conn_ip,
        "ap": _ap_name,
        "ap_ip": _ap_ip,
    }


# ===================== DNS 劫持 =====================

def _poll_dns():
    """处理一个待应答的 DNS 查询 (非阻塞, 无查询立即返回)。

    仅应答单问题标准查询: 回 A 记录指向 AP IP, 其余丢弃。
    手机连上热点后, 系统连通性探测域名被劫持到本机, 触发
    Captive Portal 弹窗/通知, 用户点开即进配网页。
    """
    if _dns_sock is None:
        return
    try:
        req, addr = _dns_sock.recvfrom(256)
    except OSError:
        return
    except Exception:
        return
    if len(req) < 17:
        return
    if req[4] != 0 or req[5] != 1:  # 只应答 QDCOUNT=1
        return
    try:
        ip_parts = _ap_ip.split(".")
        rdata = bytes(int(p) for p in ip_parts)
        # 头部: 事务ID回显 | 0x8180 标准响应 | QD=1 AN=1 NS=0 AR=0
        resp = (req[0:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
                + req[12:]            # 原样回显 Question 段
                + b"\xc0\x0c"         # Name 指针指向 Question
                + b"\x00\x01\x00\x01"  # Type A, Class IN
                + b"\x00\x00\x00\x0a"  # TTL 10s (换网后探测能及时刷新)
                + b"\x00\x04" + rdata)
        _dns_sock.sendto(resp, addr)
    except Exception as e:
        print("[PROV] dns resp error:", e)


# ===================== 配网 HTTP =====================

# 各系统 Captive Portal 探测路径 (统一 302 到配网页)
_PROBE_PATHS = (
    "/generate_204", "/hotspot-detect.html", "/connecttest.txt",
    "/redirect", "/success.txt", "/ncsi.txt", "/fwlink",
    "/connectivitycheck", "/kindle-wifi/wifistub.html",
)


def _send_response(cl, code, ctype, body, location=None):
    try:
        if isinstance(body, str):
            body = body.encode("utf-8")
        head = ("HTTP/1.1 %d OK\r\n" % code).encode("ascii")
        if location:
            head += ("Location: " + location + "\r\n").encode("ascii")
        head += (("Content-Type: " + ctype + "\r\n"
                  "Content-Length: " + str(len(body)) + "\r\n"
                  "Cache-Control: no-store\r\n"
                  "Connection: close\r\n\r\n").encode("ascii"))
        cl.send(head)
        if body:
            cl.send(body)
    except Exception:
        pass


def _handle_client(cl):
    """处理一个配网 HTTP 连接 (阻塞解析, 单请求即关闭)"""
    try:
        cl.settimeout(3)
        req = cl.recv(1024)
    except Exception:
        return
    if not req:
        return
    try:
        lines = req.split(b"\r\n")
        parts = lines[0].decode().split(" ")
        method = parts[0]
        path = parts[1].split("?")[0]

        # 正文 (POST JSON)
        body = b""
        clen = 0
        for ln in lines[1:]:
            if ln.lower().startswith(b"content-length:"):
                try:
                    clen = int(ln.split(b":")[1])
                except Exception:
                    clen = 0
                break
        if clen > 0:
            # 正文可能随首包到达, 不足时继续收
            idx = req.find(b"\r\n\r\n")
            body = req[idx + 4:] if idx >= 0 else b""
            guard = 0
            while len(body) < clen and guard < 20:
                guard += 1
                try:
                    chunk = cl.recv(1024)
                except Exception:
                    break
                if not chunk:
                    break
                body += chunk

        _route(cl, method, path, body)
    except Exception as e:
        print("[PROV] handle error:", e)
        _send_response(cl, 500, "text/plain", b"err")
    finally:
        try:
            cl.close()
        except Exception:
            pass
        gc.collect()


def _route(cl, method, path, body):
    """配网服务路由分发"""
    if method == "GET" and (path == "/" or path == "/index.html"):
        _send_response(cl, 200, "text/html", _PAGE_HTML)
    elif method == "GET" and path == "/api/provision/scan":
        _send_response(cl, 200, "application/json",
                       json.dumps(_api_scan()))
    elif method == "POST" and path == "/api/provision/save":
        _send_response(cl, 200, "application/json",
                       json.dumps(_api_save(body)))
    elif method == "GET" and path == "/api/provision/status":
        _send_response(cl, 200, "application/json",
                       json.dumps(_api_status()))
    elif method == "GET":
        # 其余路径 (含各系统探测路径) 重定向到配网页
        loc = "http://" + _ap_ip + "/"
        _send_response(cl, 302, "text/plain", b"", location=loc)
    else:
        _send_response(cl, 404, "application/json",
                       json.dumps({"ok": False, "msg": "not found"}))


def _api_scan():
    """扫描周围热点 (扫描会短暂打断 STA 试连, 连接进行中拒绝)"""
    if _state == "connecting":
        return {"ok": False, "msg": "connecting"}
    if _get_wlan is None:
        return {"ok": False, "msg": "no wifi"}
    try:
        sta = _get_wlan()
        sta.active(True)
        res = sta.scan()
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    # 按 RSSI 降序, 同名去重, 开放/加密都列出
    seen = []
    aps = []
    for entry in res:
        ssid = ""
        try:
            ssid = str(entry[0], "utf-8").strip()
        except Exception:
            continue
        if not ssid or ssid in seen:
            continue
        seen.append(ssid)
        rssi = int(entry[3])
        auth = int(entry[4])
        aps.append({"ssid": ssid[:32], "rssi": rssi,
                    "secure": 1 if auth != 0 else 0})
        if len(aps) >= 15:
            break
    aps.sort(key=lambda a: a["rssi"], reverse=True)
    return {"ok": True, "aps": aps}


def _api_save(body):
    """保存凭据并试连: 新 SSID 插到列表首位, 同名去重, 上限 5 条"""
    global _connector, _state, _conn_ip
    try:
        d = json.loads(body)
    except Exception:
        return {"ok": False, "msg": "bad json"}
    ssid = str(d.get("ssid", "")).strip()
    password = str(d.get("password", ""))
    if not ssid:
        return {"ok": False, "msg": "empty ssid"}
    if len(password) > 64:
        return {"ok": False, "msg": "password too long"}

    if _cfg is not None:
        old = _cfg.get_wifi_configs()
        merged = [{"ssid": ssid, "password": password}]
        for e in old:
            if e.get("ssid") != ssid:
                merged.append({"ssid": e["ssid"], "password": e["password"]})
        merged = merged[:_MAX_WIFI_ENTRIES]
        try:
            r = _cfg.update({"wifi": merged})
            if not r.get("ok"):
                return {"ok": False, "msg": "save failed"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    return _start_connect()


def _start_connect():
    """用 settings.json 现有凭据发起非阻塞试连"""
    global _connector, _state, _conn_ip
    if WiFiConnector is None or _cfg is None:
        return {"ok": False, "msg": "no wifi"}
    configs = _cfg.get_wifi_configs()
    if not configs:
        return {"ok": False, "msg": "no config"}
    _conn_ip = None
    _connector = WiFiConnector(configs)
    _connector.start()
    _state = "connecting"
    return {"ok": True, "state": "connecting", "ssid": configs[0]["ssid"]}


def _api_status():
    """连接状态查询 (附带推进一步, 兜底页面轮询)"""
    _tick_connect()
    return {"ok": True, "state": _state,
            "ssid": provision_state()["ssid"], "ip": _conn_ip}


def _tick_connect():
    """推进试连状态机 (设备页面循环与 status 接口共用)"""
    global _state, _conn_ip
    if _state != "connecting" or _connector is None:
        return
    try:
        _connector.tick()
        if _connector.state == "success":
            _state = "success"
            _conn_ip = _connector.result[1]
        elif _connector.state == "failed":
            _state = "failed"
    except Exception as e:
        print("[PROV] tick error:", e)
        _state = "failed"


def poll_provision():
    """非阻塞轮询: DNS + HTTP + 试连状态机。

    设备端配网页主循环每帧调用; 各环节异常互不影响。
    """
    _poll_dns()
    if _http_sock is not None:
        try:
            cl, _addr = _http_sock.accept()
            _handle_client(cl)
        except OSError as e:
            err = e.args[0] if e.args else -1
            if err not in (11, 110, 9):
                print("[PROV] poll error:", e)
        except Exception as e:
            print("[PROV] poll error:", e)
    _tick_connect()


# ===================== 设备端配网页 =====================

# 页面布局 (240x240):
#   y=4    标题「配网模式」(24px)
#   y=32   分隔线
#   y=40~  二维码 (V2 矩阵 33 模块 x4 = 132px) / 帮助页文案
#   y=180  提示行 (24px 中文)
#   y=206  热点名 (8px ASCII)
#   y=224  状态行 (8px: 连接中/成功/失败 + 按键提示)

def _draw_provision_page(d, sub, big, ap_name, ap_ip,
                         state, ssid, ip):
    """整页绘制二维码配网页 (ready/connecting/success/failed 状态行不同)"""
    d.fill(CLR_BG)
    title = "配网模式"
    d.write(big, title, (240 - 4 * 24) // 2, 4, CLR_WHITE, CLR_BG)
    d.hline(8, 32, 224, CLR_DIM)

    # 二维码: http://<ap_ip>/
    try:
        from framework.qrcode import get_matrix
        n, rows = get_matrix("http://" + ap_ip + "/")
    except ImportError:
        n, rows = None, None
    if n is not None:
        scale = 4
        qs = n * scale
        qx = (240 - qs) // 2
        qy = 40
        d.fill_rect(qx, qy, qs, qs, CLR_WHITE)
        for y in range(n):
            row = rows[y]
            for x in range(n):
                if row[x]:
                    d.fill_rect(qx + x * scale, qy + y * scale,
                                scale, scale, CLR_BLACK)
    else:
        if sub:
            d.text(sub, "QR ERROR", 240 // 2 - 32, 100, CLR_RED, CLR_BG)

    # 提示行 (24px 中文, 全量字库)
    d.write(big, "手机扫码自动配置", 24, 182, CLR_CYAN, CLR_BG)

    # 热点名 (ASCII 8px, 居中)
    if sub:
        line = "AP: " + ap_name
        d.text(sub, line, (240 - len(line) * 8) // 2, 210, CLR_WHITE, CLR_BG)
        _draw_state_line(d, sub, state, ssid, ip)


def _draw_state_line(d, sub, state, ssid, ip):
    """底部状态行: 连接中/成功/失败/按键提示"""
    if not sub:
        return
    d.fill_rect(0, 224, 240, 16, CLR_BG)
    if state == "connecting":
        line = "Connecting: " + (ssid or "")[:12]
        d.text(sub, line, (240 - min(len(line) * 8, 240)) // 2, 224,
               CLR_AMBER, CLR_BG)
    elif state == "success":
        line = "OK! IP:" + (ip or "")
        d.text(sub, line, (240 - min(len(line) * 8, 240)) // 2, 224,
               CLR_GREEN, CLR_BG)
    elif state == "failed":
        line = "Failed! B:RETRY"
        d.text(sub, line, (240 - len(line) * 8) // 2, 224, CLR_RED, CLR_BG)
    else:
        line = "A:HELP C:EXIT"
        d.text(sub, line, (240 - len(line) * 8) // 2, 224, CLR_GRAY, CLR_BG)


def _draw_help_page(d, sub, big, ap_name, ap_ip):
    """帮助页: 三步文字指引 (B 键返回二维码页)"""
    d.fill(CLR_BG)
    d.write(big, "配网帮助", (240 - 4 * 24) // 2, 4, CLR_WHITE, CLR_BG)
    d.hline(8, 32, 224, CLR_DIM)
    steps = ("1 连接热点", "2 自动弹出配置页", "3 选择WiFi输密码")
    y = 56
    for s in steps:
        d.write(big, s, 16, y, CLR_CYAN, CLR_BG)
        y += 40
    if sub:
        line = "AP:" + ap_name
        d.text(sub, line, (240 - len(line) * 8) // 2, y + 4,
               CLR_WHITE, CLR_BG)
        line2 = "http://" + ap_ip + "/"
        d.text(sub, line2, (240 - len(line2) * 8) // 2, y + 18,
               CLR_GRAY, CLR_BG)
    d.write(big, "B 返回", 84, 210, CLR_GRAY, CLR_BG)


def _load_fonts():
    """惰性加载子字体 (8px ASCII) 与大字体 (hanzi_24), 返回 (sub, big)"""
    sub = None
    big = None
    try:
        from font import vga1_8x8 as sub
    except ImportError:
        pass
    try:
        from font import hanzi_24 as big
    except ImportError:
        pass
    return (sub, big)


def run_provision_page(display, buttons, buzzer=None):
    """设备端配网页 (阻塞式子页)。

    先启动配网服务 (若未启动), 再循环: 按键处理 + poll_provision()
    + 状态渲染。成功后展示 IP 约 2.5s 自动退出并收摊; C 键随时
    退出 (保留配网服务时同时收摊)。

    按键: A=帮助页/二维码页切换, B=用已存凭据重试连接, C=退出。

    返回 (connected: bool, ip: str or None)。
    """
    sub, big = _load_fonts()
    d = display

    if not is_provisioning():
        ap_name, ap_ip = start_provisioning()
        if ap_name is None:
            if sub:
                d.fill(CLR_BG)
                d.text(sub, "AP START FAIL", 240 // 2 - 52, 116,
                       CLR_RED, CLR_BG)
                time.sleep_ms(1500)
            return (False, None)
    else:
        ap_name = _ap_name
        ap_ip = _ap_ip

    # 若已有可用连接 (如从设置菜单进入且已联网), 直接返回
    try:
        from framework.wifi import check_wifi
        ok, ip = check_wifi()
        if ok:
            stop_provisioning()
            return (True, ip)
    except Exception:
        pass

    state = _state
    ssid = None
    ip = _conn_ip
    page = "qr"            # qr / help
    done_ts = None

    _draw_provision_page(d, sub, big, ap_name, ap_ip, state, ssid, ip)
    if buzzer:
        buzzer.tone(1000, 30)

    while True:
        up, sel, down, _a, _b, _c = buttons.scan_timed()

        if down:
            # C 短按: 退出配网 (规范: C=退出)
            if buzzer:
                buzzer.tone(600, 30)
            stop_provisioning()
            return (False, None)
        if up:
            # A 短按: 二维码页 / 帮助页切换
            page = "help" if page == "qr" else "qr"
            if page == "help":
                _draw_help_page(d, sub, big, ap_name, ap_ip)
            else:
                _draw_provision_page(d, sub, big, ap_name, ap_ip,
                                     state, ssid, ip)
            if buzzer:
                buzzer.tone(900, 15)
        if sel:
            # B 短按: 重试连接 (用 settings.json 现有凭据)
            r = _start_connect()
            if r.get("ok"):
                state = "connecting"
                ssid = r.get("ssid")
            else:
                state = "failed"
            if page == "help":
                page = "qr"
                last_draw_state = None
                _draw_provision_page(d, sub, big, ap_name, ap_ip,
                                     state, ssid, ip)
            else:
                _draw_state_line(d, sub, state, ssid, ip)
            if buzzer:
                buzzer.tone(800, 20)

        # 推进配网服务 (DNS/HTTP/试连)
        poll_provision()

        # 状态变化 -> 局部重绘状态行 + 音效; 成功后倒计时自动退出
        if _state != state or _conn_ip != ip:
            state = _state
            ip = _conn_ip
            if _connector is not None:
                ssid = _connector.current_ssid
            if page == "qr":
                _draw_state_line(d, sub, state, ssid, ip)
            if state == "success":
                if buzzer:
                    buzzer.tone(1200, 30)
                    time.sleep_ms(60)
                    buzzer.tone(1600, 40)
                done_ts = time.ticks_ms()
            elif state == "failed":
                if buzzer:
                    buzzer.tone(300, 60)

        if state == "success" and done_ts is not None:
            if time.ticks_diff(time.ticks_ms(), done_ts) > 2500:
                stop_provisioning()
                return (True, ip)

        time.sleep_ms(30)


def boot_provision_check(display, buttons, buzzer=None):
    """开机配网引导 (main.py 进入菜单前调用一次)。

    WiFi 已连接 -> 直接返回 (原流程零改动);
    未连接     -> 进入配网页 (用户 C 键可跳过进离线菜单)。
    """
    try:
        from framework.wifi import check_wifi
        ok, _ip = check_wifi()
    except Exception as e:
        print("[PROV] check_wifi error:", e)
        return
    if ok:
        return
    try:
        run_provision_page(display, buttons, buzzer)
    except Exception as e:
        print("[PROV] page error:", e)
    finally:
        gc.collect()


# ===================== 配网页 HTML (手机浏览器) =====================
# 深色科技风, 与既有 Web 配置页观感一致。纯静态字符串 (不含 % 格式化,
# JS 里的取模运算不会踩占位符坑), 全部请求走相对路径。

_PAGE_HTML_STR = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>闲沐盒子 WiFi 配网</title><style>"
    "body{background:#0d1117;color:#e6edf3;font-family:sans-serif;"
    "margin:0;padding:16px;max-width:420px;margin:0 auto}"
    "h1{font-size:20px;text-align:center;color:#58d6d0}"
    ".card{background:#161b22;border:1px solid #30363d;border-radius:10px;"
    "padding:12px;margin-bottom:12px}"
    ".ap{padding:10px 8px;border-bottom:1px solid #21262d;cursor:pointer;"
    "display:flex;justify-content:space-between;align-items:center}"
    ".ap:last-child{border-bottom:none}"
    ".ap:active{background:#1f6feb33}"
    ".sig{color:#7ee787;font-size:13px;min-width:44px;text-align:right}"
    ".lock{color:#d29922;margin-right:6px}"
    "input{width:100%;box-sizing:border-box;background:#0d1117;"
    "color:#e6edf3;border:1px solid #30363d;border-radius:8px;"
    "padding:10px;font-size:15px;margin-top:6px}"
    "button{width:100%;background:#1f6feb;color:#fff;border:none;"
    "border-radius:8px;padding:12px;font-size:16px;margin-top:10px}"
    "button.sec{background:#21262d;color:#8b949e}"
    "#msg{text-align:center;min-height:20px;color:#7ee787;font-size:14px}"
    "#msg.err{color:#f85149}"
    ".hint{font-size:12px;color:#8b949e;text-align:center;margin-top:8px}"
    "</style></head><body>"
    "<h1>闲沐盒子 &middot; WiFi 配网</h1>"
    "<div class='card'><div style='font-size:14px;margin-bottom:4px'>"
    "附近 WiFi（点击选择）</div><div id='aps'>扫描中...</div>"
    "<button class='sec' onclick='doscan()'>重新扫描</button></div>"
    "<div class='card'>"
    "<div style='font-size:14px'>WiFi 名称（SSID）</div>"
    "<input id='ssid' placeholder='也可手动输入'/>"
    "<div style='font-size:14px;margin-top:8px'>密码（无密码可留空）</div>"
    "<input id='pwd' type='password' placeholder='WiFi 密码'/>"
    "<button onclick='save()'>连接</button>"
    "<div class='hint'>提交后设备自动保存并试连，成功即进入主菜单</div>"
    "</div><div id='msg'></div>"
    "<script>"
    "var busy=false;"
    "function esc(s){var d=document.createElement('div');"
    "d.textContent=s;return d.innerHTML;}"
    "function doscan(){var el=document.getElementById('aps');"
    "el.textContent='扫描中...';"
    "fetch('/api/provision/scan').then(function(r){return r.json()})"
    ".then(function(j){if(!j.ok){el.textContent=j.msg||'扫描失败';return}"
    "el.innerHTML='';"
    "if(!j.aps.length){el.textContent='未发现热点';return}"
    "j.aps.forEach(function(a){var dv=document.createElement('div');"
    "dv.className='ap';"
    "var l=document.createElement('span');"
    "l.innerHTML=(a.secure?'<span class=lock>&#128274;</span>':'')"
    "+esc(a.ssid);"
    "var r=document.createElement('span');r.className='sig';"
    "r.textContent=(a.rssi>-60?'强':a.rssi>-75?'中':'弱');"
    "dv.appendChild(l);dv.appendChild(r);"
    "dv.onclick=function(){document.getElementById('ssid').value=a.ssid;"
    "document.getElementById('pwd').focus();};"
    "el.appendChild(dv);});})"
    ".catch(function(){el.textContent='扫描失败,请重试';});}"
    "function save(){if(busy)return;"
    "var s=document.getElementById('ssid').value.trim();"
    "var p=document.getElementById('pwd').value;"
    "if(!s){msg('请填写 WiFi 名称',1);return}"
    "busy=true;msg('提交中...',0);"
    "fetch('/api/provision/save',{method:'POST',"
    "headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({ssid:s,password:p})})"
    ".then(function(r){return r.json()}).then(function(j){"
    "if(!j.ok){busy=false;msg(j.msg||'提交失败',1);return}"
    "msg('连接中: '+s+' ...',0);poll();})"
    ".catch(function(){busy=false;msg('网络错误,请重试',1);});}"
    "function poll(){fetch('/api/provision/status')"
    ".then(function(r){return r.json()}).then(function(j){"
    "if(j.state=='success'){msg('连接成功! IP:'+j.ip,0);"
    "setTimeout(function(){location.reload()},3000);busy=false;return}"
    "if(j.state=='failed'){busy=false;"
    "msg('连接失败,请检查密码后重试',1);doscan();return}"
    "setTimeout(poll,1000);})"
    ".catch(function(){setTimeout(poll,2000);});}"
    "function msg(t,err){var m=document.getElementById('msg');"
    "m.textContent=t;m.className=err?'err':'';}"
    "doscan();</script></body></html>"
)

_PAGE_HTML = _PAGE_HTML_STR.encode("utf-8")

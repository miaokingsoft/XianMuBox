"""
XianMuBox - 全局 HTTP 配置服务
闲沐工坊 · 插件式架构

提供局域网内远程配置设备参数的能力：
  - GET /             - Web 配置页面
  - GET /api/config   - 获取当前配置 (JSON)
  - POST /api/config  - 更新配置并持久化
  - GET /api/wifi     - 获取 WiFi 状态
  - POST /api/wifi    - 触发 WiFi 重连

可配置项（全部通过 framework/settings.py 统一读写）：
  - wifi        WiFi SSID / 密码列表
  - api_keys    各服务商 API 密钥
  - brightness  屏幕亮度
  - http_port   HTTP 端口
  - swipe       滑动参数 / 设备列表

注: 时区固定为北京时间 UTC+8, 不再作为配置项。
"""

import socket
import json
import gc

from framework import settings as cfg
from framework import http_server_state as _state

# ===== 服务器实例 =====
# socket 引用持久化到 http_server_state 模块，避免 framework.http_server
# 被 boot.py 回收后丢失运行状态导致 EADDRINUSE


# ==================== 配置访问（统一委托给 framework/settings.py）====================

def get_all_config():
    """获取当前完整配置（含 wifi / api_keys 等敏感字段）"""
    return cfg.load_settings()


def _enrich_weather(weather):
    """心知天气仅需 city 字段, 无需额外补全 (保留扩展点)"""
    if not isinstance(weather, dict):
        return weather
    return weather


def apply_config(d, persist=True):
    """
    应用配置变更并可选持久化.

    d: 配置字典（仅含要修改的键）
    persist: 是否持久化到文件

    返回: {"ok": bool, "changed": bool, "msg": str}
    """
    # 天气变更时按 city 查表补全 lat/lon/name/city_id, 避免后续保存丢字段
    if isinstance(d, dict) and "weather" in d and isinstance(d["weather"], dict):
        _enrich_weather(d["weather"])
    result = cfg.update(d, persist=persist)
    if result["ok"] and result["changed"]:
        # 持久化成功后刷新内存视图
        try:
            cfg.reload()
        except Exception as e:
            print("[HTTP] reload error:", e)
        # 天气配置变更后清缓存, 下次 get_weather() 会重新拉取
        if "weather" in d:
            try:
                from framework import weather as _wx
                _wx.invalidate_cache()
            except Exception:
                pass
    return result


def sync_device_time():
    """通过 NTP 同步设备 RTC 时间, 并修正为北京时间 (UTC+8).

    时区固定北京时间, 不再从 settings.json 读取.
    返回: {"ok": bool, "msg": str}
    """
    try:
        timezone = 8

        from machine import RTC
        import ntptime
        import time as _time

        rtc = RTC()
        ntptime.host = "ntp.aliyun.com"  # 平台文档建议, 国内可达性
        ntptime.settime()   # 同步 UTC 到 RTC
        # ntptime 把 UTC 写入 RTC, 这里按时区偏移修正为本地时间
        t = _time.time() + timezone * 3600
        tm = _time.localtime(t)
        # RTC.datetime: (year, month, day, weekday, hours, mins, secs, subsecs)
        # localtime weekday: Monday=0; RTC weekday: Sunday=0 -> (tm[6]+1)%7
        rtc.datetime((tm[0], tm[1], tm[2], (tm[6] + 1) % 7,
                      tm[3], tm[4], tm[5], 0))
        return {"ok": True, "msg": "Time synced (UTC%+d)" % timezone}
    except Exception as e:
        print("[HTTP] sync time error:", e)
        return {"ok": False, "msg": "Sync failed: " + str(e)}


# ==================== Web 页面模板 ====================

# 模板文件路径（设备上需部署到 /www/index.html, 由 tools/build.py 打包并 deploy.py 上传）
_HTML_PATH = "/www/index.html"
# 模板缓存: 首次请求时从 VFS 加载, 后续复用 (避免每次请求都读 flash)
_HTML_TEMPLATE = None


def _load_template():
    """从 VFS 加载 HTML 模板. 仅首次调用, 之后返回缓存."""
    global _HTML_TEMPLATE
    if _HTML_TEMPLATE is None:
        with open(_HTML_PATH, "r") as f:
            _HTML_TEMPLATE = f.read()
    return _HTML_TEMPLATE


def _js_string(s):
    """转义字符串用于嵌入 JS 字符串字面量 (被双引号包围).

    处理: 反斜杠, 双引号, 换行/回车, 防止字面量被破坏.
    注: 防 </script> 边界突破需要在拼接后另行处理 (见 _build_html).
    """
    if s is None:
        s = ""
    s = str(s)
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r"))


def _html_attr(s):
    """转义字符串用于嵌入 HTML 属性值 (被双引号包围).

    处理: &, ", < , 防止属性被闭合或注入 HTML 标签.
    """
    if s is None:
        s = ""
    s = str(s)
    return (s.replace("&", "&amp;")
             .replace('"', "&quot;")
             .replace("<", "&lt;"))


def _safe_for_script(s):
    """防 </script> 边界突破: 将 </ 替换为 <\\/ (JSON 嵌入场景)."""
    return s.replace("</", "<\\/")


def _build_html(config):
    """构建 HTML 页面，填入当前配置值 (v1.2: api_keys 多 key schema)."""
    api = config.get("api_keys", {}) or {}
    swipe = config.get("swipe", {}) or {}
    project = config.get("project", {}) or {}
    if not isinstance(project, dict):
        project = {}

    # WiFi 列表 -> JS 数组初始化（页面加载后再 addWifi 渲染）
    # 双重转义: 字符串字面量转义 + </ 防 script 闭合
    wifi_list = config.get("wifi", []) or []
    wifi_js_items = []
    for entry in wifi_list:
        if isinstance(entry, dict) and entry.get("ssid"):
            wifi_js_items.append(
                '["%s","%s"]' % (_js_string(entry.get("ssid", "")),
                                 _js_string(entry.get("password", ""))))
    wifi_js = _safe_for_script("[" + ",".join(wifi_js_items) + "]")

    # API Keys -> v1.2 KEYS 结构 (排除 ai_token 单值字段)
    keys_js = _safe_for_script(_serialize_api_keys_for_js(api))

    # 服务商显示开关 -> 停用代号列表 (设备端 _load_cards 同源过滤)
    hidden_raw = config.get("api_keys_hidden", [])
    hidden_list = []
    if isinstance(hidden_raw, list):
        for _p in hidden_raw:
            _s = str(_p).strip()
            if _s:
                hidden_list.append(_s)
    keys_hidden_js = _safe_for_script(json.dumps(hidden_list))

    # ai_token 嵌入 HTML 属性 value="..." 走 _html_attr (双引号/尖括号安全)
    # 优先从 project.ai_token 读取，回落到 api_keys.ai_token
    project_ai_token = project.get("ai_token", "")
    if not project_ai_token:
        project_ai_token = api.get("ai_token", "")
    ai_token_html = _html_attr(project_ai_token)

    try:
        brightness_v = int(config.get("brightness", 100))
    except (TypeError, ValueError):
        brightness_v = 100
    try:
        http_port_v = int(config.get("http_port", 80))
    except (TypeError, ValueError):
        http_port_v = 80

    # 滑动配置 v2.0 (modes + devices)
    def _swipe_int(k, default, lo=None, hi=None):
        try:
            v = int(swipe.get(k, default))
        except (TypeError, ValueError):
            v = default
        if lo is not None and v < lo:
            v = default
        if hi is not None and v > hi:
            v = default
        return v

    swipe_mode_idx_v = _swipe_int("mode_idx", 0, 0, 3)
    swipe_active_device_idx_v = _swipe_int("active_device_idx", 0, 0, 100)
    swipe_modes_js = _safe_for_script(
        _serialize_swipe_modes_for_js(swipe.get("modes", [])))
    swipe_devices_js = _safe_for_script(
        _serialize_swipe_devices_for_js(swipe.get("devices", [])))

    # project 节点：AI Token + 设备 SN + 自媒体 + 自有网站
    project_ai_token_html = _html_attr(project_ai_token)
    project_self_media_js = _safe_for_script(
        _serialize_project_self_media_for_js(project.get("self_media", [])))
    project_own_sites_js = _safe_for_script(
        _serialize_project_own_sites_for_js(project.get("own_sites", [])))

    # 设备 SN (设备端首次启动自动生成, Web 端只读)
    try:
        device_sn_v = str(project.get("device_sn", "")).strip()
        if not device_sn_v:
            # 兜底: 现场生成 (例如首次访问 Web 时尚未 boot 完)
            try:
                from framework.settings import ensure_device_sn
                device_sn_v = ensure_device_sn()
            except Exception:
                device_sn_v = ""
        device_sn_html = _html_attr(device_sn_v)
    except Exception:
        device_sn_html = ""

    # 股票子节点 (闲沐数观用户级 API)
    stock_cfg = config.get("stock", {}) or {}
    if not isinstance(stock_cfg, dict):
        stock_cfg = {}
    try:
        stock_endpoint_v = _html_attr(
            str(stock_cfg.get("endpoint", "http://www.mooview.cn/api/")).strip()
            or "http://www.mooview.cn/api/")
    except Exception:
        stock_endpoint_v = "http://www.mooview.cn/api/"
    try:
        stock_api_key_v = _html_attr(str(stock_cfg.get("api_key", "")).strip())
    except Exception:
        stock_api_key_v = ""

    # 云平台子节点 (v1.4: 闲沐盒子云平台; 激活密钥出厂固化,
    # v1.4.1 起不经 Web 展示/填写, 仅注入开关状态)
    cloud_cfg = config.get("cloud", {}) or {}
    if not isinstance(cloud_cfg, dict):
        cloud_cfg = {}
    cloud_enabled_v = "true" if cloud_cfg.get("enabled") else "false"

    # 天气/城市配置 (心知天气 v3)
    weather = config.get("weather", {}) or {}
    if not isinstance(weather, dict):
        weather = {}
    try:
        weather_api_key = str(weather.get("api_key", ""))
    except Exception:
        weather_api_key = ""
    weather_city = str(weather.get("city", "深圳"))
    weather_cities_js = _safe_for_script(_serialize_cities_for_js())

    # 闹钟 (v1.3: 网络时钟闹钟, 恒 3 组; 纯 bool/int/list, json.dumps 即可)
    alarms_raw = config.get("alarms", []) or []
    alarms_norm = []
    for _i in range(3):
        _a = alarms_raw[_i] if _i < len(alarms_raw) and isinstance(alarms_raw[_i], dict) else {}
        try:
            _ah = int(_a.get("h", 0))
        except (TypeError, ValueError):
            _ah = 0
        try:
            _am = int(_a.get("m", 0))
        except (TypeError, ValueError):
            _am = 0
        if _ah < 0 or _ah > 23:
            _ah = 0
        if _am < 0 or _am > 59:
            _am = 0
        _days = []
        if isinstance(_a.get("days"), list):
            for _d in _a["days"]:
                try:
                    _d = int(_d)
                except (TypeError, ValueError):
                    continue
                if 0 <= _d <= 6 and _d not in _days:
                    _days.append(_d)
        _days.sort()
        _msg = str(_a.get("msg", "") or "").strip()[:16]
        alarms_norm.append({"en": bool(_a.get("en", False)),
                            "h": _ah, "m": _am, "days": _days,
                            "msg": _msg})
    alarms_js = _safe_for_script(json.dumps(alarms_norm))

    # 新闻播报 (v1.5: 整点新闻播报; enabled + hours 列表 + volume)
    news_cfg = config.get("news", {}) or {}
    if not isinstance(news_cfg, dict):
        news_cfg = {}
    news_enabled_v = "true" if news_cfg.get("enabled") else "false"
    try:
        news_volume_v = int(news_cfg.get("volume", 80))
    except (TypeError, ValueError):
        news_volume_v = 80
    news_hours = []
    if isinstance(news_cfg.get("hours"), list):
        for _h in news_cfg["hours"]:
            try:
                _h = int(_h)
            except (TypeError, ValueError):
                continue
            if 0 <= _h <= 23 and _h not in news_hours:
                news_hours.append(_h)
    news_hours.sort()
    news_hours_js = _safe_for_script(json.dumps(news_hours))

    template = _load_template()

    # 构建格式化字典
    fmt_dict = {
        "brightness": brightness_v,
        "http_port": http_port_v,
        "wifi": wifi_js,
        "keys": keys_js,
        "keys_hidden": keys_hidden_js,
        "swipe_mode_idx": swipe_mode_idx_v,
        "swipe_active_device_idx": swipe_active_device_idx_v,
        "swipe_modes": swipe_modes_js,
        "swipe_devices": swipe_devices_js,
        "project_self_media": project_self_media_js,
        "project_own_sites": project_own_sites_js,
        "device_sn": device_sn_html,
        "stock_endpoint": stock_endpoint_v,
        "stock_api_key": stock_api_key_v,
        "cloud_enabled": cloud_enabled_v,
        "weather_api_key": _html_attr(weather_api_key),
        "weather_city": _html_attr(weather_city),
        "weather_cities": weather_cities_js,
        "alarms": alarms_js,
        "news_enabled": news_enabled_v,
        "news_hours": news_hours_js,
        "news_volume": news_volume_v,
    }

    # 提取模板中所有 %(key)s / %(key)d 占位符，诊断 dict 不匹配
    tmpl_keys = set()
    i = 0
    while True:
        i = template.find("%(", i)
        if i == -1:
            break
        end = template.find(")", i + 2)
        if end != -1 and end + 1 < len(template) and template[end + 1] in "sd":
            tmpl_keys.add(template[i + 2:end])
        i += 2

    missing = tmpl_keys - set(fmt_dict.keys())
    extra = set(fmt_dict.keys()) - tmpl_keys

    # 检测裸 % 字符（既不是 %% 转义，也不是 %(key) 占位符）
    bare_positions = []
    j = 0
    while True:
        j = template.find("%", j)
        if j == -1:
            break
        if j + 1 < len(template) and template[j + 1] == "%":
            j += 2
            continue
        if j + 1 < len(template) and template[j + 1] == "(":
            j += 2
            continue
        bare_positions.append(j)
        j += 1

    if missing:
        print("[HTTP] Template needs keys not in dict:", sorted(missing))
    if extra:
        print("[HTTP] Dict has extra keys not in template:", sorted(extra))
    if bare_positions:
        contexts = []
        for pos in bare_positions[:5]:
            start = max(0, pos - 10)
            end = min(len(template), pos + 10)
            snippet = template[start:end].replace("\n", "\\n").replace("\r", "\\r")
            contexts.append(snippet)
        print("[HTTP] Template has %d bare %% (not %%%% or %%(key)):" % len(bare_positions))
        for ctx in contexts:
            print("      ...%s..." % ctx)

    try:
        return template % fmt_dict
    except Exception as e:
        print("[HTTP] HTML format error:", e)
        if missing:
            print("[HTTP] Missing keys:", sorted(missing))
        if bare_positions:
            print("[HTTP] Bare %% count:", len(bare_positions))
        raise


def _serialize_api_keys_for_js(api):
    """把 settings.json 的 api_keys 序列化为 KEYS JSON 字符串.

    输入 (settings 任意 schema) -> {"deepseek":[{"label":"...","key":"..."}], ...}
    排除 ai_token 单值字段, 按存储顺序保留 provider 列表.
    注意: json.dumps 不转义 <, 调用方需配合 _safe_for_script() 防 </script> 突破.
    """
    out = {}
    for prov, val in (api or {}).items():
        if prov == "ai_token":
            continue
        if isinstance(val, list):
            items = []
            for it in val:
                if not isinstance(it, dict):
                    continue
                k = str(it.get("key", "")).strip()
                if not k:
                    continue
                lbl = str(it.get("label", "")).strip()
                items.append({"label": lbl[:24], "key": k})
            out[prov] = items
        elif isinstance(val, str) and val:
            out[prov] = [{"label": "%s-1" % prov, "key": val}]
        # 空字符串/None -> 不输出该 provider
    return json.dumps(out)


def _serialize_swipe_modes_for_js(modes):
    """把 swipe.modes 序列化为 JS 数组字符串.

    保留 name 与全部参数字段, 确保返回 4 个模式, 缺失项补默认值.
    调用方需配合 _safe_for_script().
    """
    mode_names = ["刷新视频-上滑", "刷新视频-下滑", "看书-左滑", "看书-右滑"]
    defaults = {
        "interval_min": 5, "interval_max": 15,
        "swipe_offset_ratio": 6, "swipe_steps": 5, "swipe_delay_ms": 20,
    }
    out = []
    for i in range(4):
        m = modes[i] if isinstance(modes, list) and i < len(modes) else {}
        if not isinstance(m, dict):
            m = {}
        d = {"name": str(m.get("name", mode_names[i]))[:32]}
        for k, default in defaults.items():
            try:
                v = int(m.get(k, default))
            except (TypeError, ValueError):
                v = default
            d[k] = v
        out.append(d)
    return json.dumps(out)


def _serialize_swipe_devices_for_js(devices):
    """把 swipe.devices 序列化为 JS 数组字符串.

    v2.0 只保留 name / screen_w / screen_h, 调用方需配合 _safe_for_script().
    """
    out = []
    for dev in (devices or []):
        if not isinstance(dev, dict):
            continue
        d = {"name": str(dev.get("name", "Phone"))[:32]}
        for k in ("screen_w", "screen_h"):
            try:
                d[k] = int(dev.get(k, 0))
            except (TypeError, ValueError):
                d[k] = 0
        if d["screen_w"] <= 0:
            d["screen_w"] = 1080
        if d["screen_h"] <= 0:
            d["screen_h"] = 1920
        out.append(d)
    return json.dumps(out)


def _serialize_cities_for_js():
    """把 weather.CITIES 序列化为 JS 数组字符串, 用于前端下拉框.

    调用方需配合 _safe_for_script().
    """
    try:
        from framework.weather import CITIES
        out = []
        for c in CITIES:
            out.append({
                "id": str(c.get("id", ""))[:16],
                "name": str(c.get("name", ""))[:32],
                "province": str(c.get("province", ""))[:16],
            })
        return json.dumps(out)
    except Exception:
        return "[]"


def _serialize_project_self_media_for_js(items):
    """把 project.self_media 序列化为 JS 数组字符串"""
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        d = {}
        cat = str(it.get("category", "")).strip().lower()
        if cat:
            d["category"] = cat
        if "name" in it:
            d["name"] = str(it["name"])[:64]
        if "url" in it:
            d["url"] = str(it["url"])[:512]
        if d:
            out.append(d)
    return json.dumps(out)


def _serialize_project_own_sites_for_js(items):
    """把 project.own_sites 序列化为 JS 数组字符串"""
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        d = {}
        if "name" in it:
            d["name"] = str(it["name"])[:64]
        if "api" in it:
            d["api"] = str(it["api"])[:512]
        if "key" in it:
            d["key"] = str(it["key"])[:512]
        if d:
            out.append(d)
    return json.dumps(out)


# ==================== HTTP 请求处理 ====================

def _parse_request(data):
    """极简 HTTP 请求解析 (data 应包含完整 header + body)"""
    try:
        text = data.decode("utf-8", "ignore")
        lines = text.split("\r\n")
        if not lines:
            return None, None, None

        parts = lines[0].split(" ")
        if len(parts) < 2:
            return None, None, None

        method = parts[0]
        path = parts[1]
        body = ""

        if "\r\n\r\n" in text:
            body = text.split("\r\n\r\n", 1)[1]

        return method, path, body
    except Exception:
        return None, None, None


def _get_content_length(data):
    """从 HTTP header 中解析 Content-Length (仅处理第一个匹配)."""
    try:
        text = data.decode("utf-8", "ignore")
        for line in text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return 0


_REASON = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    500: "Internal Server Error",
}


def _safe_send(cl_sock, data):
    """可靠发送: 循环 send 直到全部发完或连接断开.
    解决 MicroPython socket.send() 不保证一次发完的问题。"""
    try:
        total = 0
        n = len(data)
        while total < n:
            sent = cl_sock.send(data[total:])
            if not sent:
                break
            total += sent
        return total == n
    except OSError:
        return False


def _send_response(cl_sock, code, content_type, body):
    """分片发送 HTTP 响应, 避免 header+body 拼接的大内存分配。

    与 _response() 不同, 此函数直接发送而非返回 bytes,
    header 和 body 分别编码发送, 无需额外拼接空间。
    对于 HTML 页面等大响应, 可减少约 8KB 的峰值内存占用。
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    reason = _REASON.get(code, "OK")
    header = "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (
        code, reason, content_type, len(body))
    _safe_send(cl_sock, header.encode("utf-8"))
    _safe_send(cl_sock, body)


def _handle_client(cl_sock):
    """处理单个 HTTP 请求"""
    try:
        cl_sock.settimeout(3)
        data = b""

        # 1. 读取完整 HTTP header (以 \r\n\r\n 结束)
        while True:
            try:
                chunk = cl_sock.recv(1024)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data:
                    break
            except OSError:
                break

        if b"\r\n\r\n" not in data:
            return

        # 2. 如果 header 声明了 Content-Length, 继续读取 body
        content_length = _get_content_length(data)
        header_end = data.find(b"\r\n\r\n") + 4
        body_len = len(data) - header_end
        while body_len < content_length:
            try:
                chunk = cl_sock.recv(1024)
                if not chunk:
                    break
                data += chunk
                body_len += len(chunk)
            except OSError:
                break

        method, path, body = _parse_request(data)
        if method is None:
            _send_response(cl_sock, 400, "text/plain", "Bad Request")
            return

        config = get_all_config()

        # ========== GET / ==========
        if path == "/" and method == "GET":
            gc.collect()  # 回收碎片, 为 HTML 构建腾出连续空间
            html = _build_html(config)
            _send_response(cl_sock, 200, "text/html; charset=utf-8", html)
            del html  # 发送完立即释放, 降低后续内存压力

        # ========== GET /api/config ==========
        elif path == "/api/config" and method == "GET":
            j = json.dumps(config)
            _send_response(cl_sock, 200, "application/json", j)

        # ========== POST /api/config ==========
        elif path == "/api/config" and method == "POST":
            if body:
                try:
                    d = json.loads(body)
                    # 调试: 打印天气字段 (如存在)
                    if "weather" in d:
                        print("[HTTP] /api/config weather input:", d.get("weather"))
                    # 闹钟保存回执: 串口立即可见 Web 保存的各组内容与结果
                    if "alarms" in d and isinstance(d["alarms"], list):
                        parts = []
                        for _i in range(3):
                            _a = d["alarms"][_i] if _i < len(d["alarms"]) and isinstance(d["alarms"][_i], dict) else {}
                            parts.append("AL%d{en:%d,%02d:%02d,%s}" % (
                                _i + 1,
                                1 if _a.get("en") else 0,
                                _a.get("h", 0) if isinstance(_a.get("h"), int) else 0,
                                _a.get("m", 0) if isinstance(_a.get("m"), int) else 0,
                                (",".join(str(x) for x in _a.get("days", []))) or "once"))
                        print("[ALARM] web save:", " ".join(parts))
                    result = apply_config(d)
                    if "alarms" in d:
                        print("[ALARM] web save result: ok=%d changed=%d msg=%s" % (
                            1 if result.get("ok") else 0,
                            1 if result.get("changed") else 0,
                            result.get("msg", "")))
                    # 调试: 打印保存结果
                    if "weather" in d:
                        from framework import settings as _cfg_dbg
                        print("[HTTP] /api/config weather saved:", _cfg_dbg.get("weather", {}))
                    # 检测 http_port 变化: 当前 server 仍绑定旧端口, 需用户手动重启
                    if (result.get("ok") and result.get("changed")
                            and "http_port" in d):
                        old_port = config.get("http_port", 80)
                        new_port = d.get("http_port", old_port)
                        if int(new_port) != int(old_port):
                            result["msg"] = (result.get("msg", "Saved")
                                             + " | HTTP port changed: restart server to take effect (B-button in Settings)")
                    resp = json.dumps(result)
                    _send_response(cl_sock, 200, "application/json", resp)
                except Exception as e:
                    resp = json.dumps({"ok": False, "error": str(e)})
                    _send_response(cl_sock, 400, "application/json", resp)
            else:
                _send_response(cl_sock, 400, "text/plain", "no body")

        # ========== POST /api/time (NTP 同步设备时间, 固定北京时间) ==========
        elif path == "/api/time" and method == "POST":
            result = sync_device_time()
            resp = json.dumps(result)
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== GET /api/weather (天气配置 / 城市列表 / 当前天气) ==========
        elif path == "/api/weather" and method == "GET":
            try:
                from framework.weather import CITIES, get_weather, get_current_city
                # 透传所有配置 (前端用于填充表单)
                weather_cfg = config.get("weather", {}) or {}
                cur = get_weather()
                resp = json.dumps({
                    "ok": True,
                    "config": weather_cfg,
                    "current": cur,
                    "current_city": get_current_city(),
                    "cities": [
                        {"id": c["id"], "name": c["name"], "province": c["province"]}
                        for c in CITIES
                    ],
                })
                _send_response(cl_sock, 200, "application/json", resp)
            except Exception as e:
                resp = json.dumps({"ok": False, "error": str(e)})
                _send_response(cl_sock, 200, "application/json", resp)

        # ========== POST /api/weather (更新城市/API Key) ==========
        elif path == "/api/weather" and method == "POST":
            if body:
                try:
                    d = json.loads(body)
                    # 心知天气 v3 配置字段:
                    #   city     中文城市名 (例如 "武汉")
                    #   api_key  心知天气 API 密钥
                    new_weather = {}
                    if "city" in d:
                        new_weather["city"] = str(d["city"])[:16]
                    if "api_key" in d:
                        new_weather["api_key"] = str(d["api_key"])[:128]

                    if not new_weather:
                        # 纯刷新请求: 只清缓存, 不更新配置
                        if d.get("refresh"):
                            try:
                                from framework import weather as _wx
                                _wx.invalidate_cache()
                            except Exception:
                                pass
                            resp = json.dumps({"ok": True, "msg": "Cache cleared"})
                            _send_response(cl_sock, 200, "application/json", resp)
                        else:
                            resp = json.dumps({"ok": False, "msg": "No fields to update"})
                            _send_response(cl_sock, 400, "application/json", resp)
                    else:
                        # 合并到现有配置
                        existing = config.get("weather", {}) or {}
                        if not isinstance(existing, dict):
                            existing = {}
                        existing.update(new_weather)

                        result = apply_config({"weather": existing})
                        # 强制清缓存, 让下次读取立即生效
                        try:
                            from framework import weather as _wx
                            _wx.invalidate_cache()
                        except Exception:
                            pass
                        resp = json.dumps({
                            "ok": result.get("ok", False),
                            "changed": result.get("changed", False),
                            "msg": "天气配置已保存" if result.get("ok") else result.get("msg", ""),
                            "weather": existing,
                        })
                        _send_response(cl_sock, 200, "application/json", resp)
                except Exception as e:
                    resp = json.dumps({"ok": False, "error": str(e)})
                    _send_response(cl_sock, 400, "application/json", resp)
            else:
                _send_response(cl_sock, 400, "text/plain", "no body")

        # ========== GET /api/wifi ==========
        elif path == "/api/wifi" and method == "GET":
            try:
                from framework.wifi import _get_wlan
                wlan = _get_wlan()
                connected = wlan.isconnected()
                ip = wlan.ifconfig()[0] if connected else None
                resp = json.dumps({
                    "connected": connected,
                    "ip": ip
                })
                _send_response(cl_sock, 200, "application/json", resp)
            except Exception as e:
                resp = json.dumps({"connected": False, "error": str(e)})
                _send_response(cl_sock, 200, "application/json", resp)

        # ========== GET /api/balance (实时余额查询, v1.2) ==========
        elif path == "/api/balance" and method == "GET":
            try:
                from modes.ai_balance import _fetch_balances_now
                items = _fetch_balances_now()
                resp = json.dumps({"ok": True, "items": items})
                _send_response(cl_sock, 200, "application/json", resp)
            except ImportError as e:
                resp = json.dumps({"ok": False, "items": [],
                                   "error": "ai_balance module unavailable"})
                _send_response(cl_sock, 200, "application/json", resp)
            except Exception as e:
                resp = json.dumps({"ok": False, "items": [],
                                   "error": str(e)})
                _send_response(cl_sock, 200, "application/json", resp)

        # ========== GET /api/stock (闲沐数观股票 API 透传, v1.1) ==========
        # _parse_request 返回完整 path (含 query string), 用 startswith 匹配
        elif path.startswith("/api/stock") and method == "GET":
            # _parse_request 不解析 query, 这里手动提取
            qmark = path.find("?")
            query = {}
            if qmark >= 0:
                qs = path[qmark + 1:]
                for kv in qs.split("&"):
                    if not kv:
                        continue
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                    else:
                        k, v = kv, ""
                    try:
                        from urllib.parse import unquote  # type: ignore
                        k = unquote(k.replace("+", " "))
                        v = unquote(v.replace("+", " "))
                    except Exception:
                        pass
                    query[k] = v
            try:
                from modes.stock_api import handle_stock_request
                result = handle_stock_request(query)
                resp = json.dumps(result)
                _send_response(cl_sock, 200, "application/json", resp)
            except ImportError:
                _send_response(cl_sock, 200, "application/json",
                               json.dumps({"ok": False, "message": "stock_api module unavailable"}))
            except Exception as e:
                _send_response(cl_sock, 200, "application/json",
                               json.dumps({"ok": False, "message": str(e)}))

        # ========== GET /api/cloud (云平台状态, v1.4) ==========
        elif path == "/api/cloud" and method == "GET":
            try:
                from framework import cloud as _cloud
                st = _cloud.status()
            except Exception as e:
                st = {"state": "error", "err_msg": str(e)}
            _send_response(cl_sock, 200, "application/json",
                           json.dumps(st))

        # ========== POST /api/cloud/activate (手动激活, v1.4) ==========
        # 阻塞请求 (约 1~10s), 与 /api/balance 同类
        elif path == "/api/cloud/activate" and method == "POST":
            try:
                from framework import cloud as _cloud
                ok, err = _cloud.activate_now()
            except Exception as e:
                ok, err = False, str(e)
            resp = json.dumps({"ok": bool(ok), "err": str(err or "")})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== POST /api/cloud/heartbeat (手动心跳, v1.4) ==========
        elif path == "/api/cloud/heartbeat" and method == "POST":
            try:
                from framework import cloud as _cloud
                ok, err = _cloud.heartbeat_now()
            except Exception as e:
                ok, err = False, str(e)
            resp = json.dumps({"ok": bool(ok), "err": str(err or "")})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== GET /api/cloud/sources (数据源清单, v1.4.2) ==========
        # 缺省纯内存 (平台缓存 > 固件登记回落); ?refresh=1 阻塞拉取平台
        # 清单并刷新缓存 (约 1~10s), 与 /api/cloud/activate 同类
        elif path.startswith("/api/cloud/sources") and method == "GET":
            refresh = ("refresh=1" in path) or ("refresh=true" in path)
            try:
                from framework import cloud as _cloud
                if refresh:
                    info = _cloud.fetch_sources(force=True)
                    resp = json.dumps({"ok": True,
                                       "origin": info["origin"],
                                       "sources": info["sources"]})
                else:
                    resp = json.dumps({"ok": True,
                                       "sources": _cloud.list_sources()})
            except Exception as e:
                resp = json.dumps({"ok": False, "msg": str(e),
                                   "sources": []})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== POST /api/cloud/probe (探测单个数据源, v1.4.2) ==========
        # body {"code": "lottery_ssq"}; 阻塞请求 (约 1~10s),
        # 与 /api/cloud/activate 同类
        elif path == "/api/cloud/probe" and method == "POST":
            src = ""
            try:
                d = json.loads(body) if body else {}
                src = str(d.get("code", "")).strip()
            except Exception:
                pass
            if not src:
                resp = json.dumps({"ok": False, "msg": "missing code"})
            else:
                try:
                    from framework import cloud as _cloud
                    r = _cloud.probe_source(src)
                except Exception as e:
                    r = {"code": src, "name": "", "state": "error",
                         "state_text": "探测异常", "detail": str(e)}
                resp = json.dumps({"ok": True, "result": r})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== POST /api/news/play (手动触发整点新闻播报, v1.5) ==========
        # 非阻塞: 仅置播报请求标志, 由主循环下一帧 framework/news.check()
        # 拉起播报接管 (播报本身阻塞 1~3 分钟, 不能在 HTTP 处理线程内做)
        elif path == "/api/news/play" and method == "POST":
            try:
                from framework import news as _news
                if _news.is_playing():
                    resp = json.dumps({"ok": False, "msg": "正在播报中"})
                elif _news.request_play():
                    resp = json.dumps({"ok": True,
                                       "msg": "已加入播报队列"})
                else:
                    resp = json.dumps({"ok": False, "msg": "触发失败"})
            except Exception as e:
                resp = json.dumps({"ok": False, "msg": str(e)})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== GET /api/news/status (新闻播报状态, v1.5) ==========
        # 返回最近一次播报结果 (不发起网络请求)
        elif path == "/api/news/status" and method == "GET":
            try:
                from framework import news as _news
                resp = json.dumps({"ok": True, "result": _news.status()})
            except Exception as e:
                resp = json.dumps({"ok": False, "msg": str(e)})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== GET /api/news/preview (拉取当档新闻预览, v1.5) ==========
        # API 体检: 取数并返回标题/时长/来源/媒体完整 URL (浏览器可
        # 直接试听平台 WAV)。阻塞请求 (约 1~10s), 与 /api/cloud/probe 同类
        elif path == "/api/news/preview" and method == "GET":
            try:
                from framework import news as _news
                resp = json.dumps({"ok": True, "result": _news.preview()})
            except Exception as e:
                resp = json.dumps({"ok": False, "msg": str(e)})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== POST /api/wifi (触发重连) ==========
        elif path == "/api/wifi" and method == "POST":
            try:
                from framework.wifi import reconnect_wifi
                ok = reconnect_wifi()
                if ok:
                    resp = json.dumps({"ok": True, "msg": "WiFi reconnected"})
                else:
                    resp = json.dumps({"ok": False, "msg": "Reconnect failed"})
            except ImportError:
                resp = json.dumps({"ok": False, "msg": "reconnect not available"})
            except Exception as e:
                resp = json.dumps({"ok": False, "msg": str(e)})
            _send_response(cl_sock, 200, "application/json", resp)

        # ========== POST /api/test_wwwsite (测试自有网站连通性) ==========
        elif path == "/api/test_wwwsite" and method == "POST":
            if not body:
                resp = json.dumps({"ok": False, "msg": "empty body"})
            else:
                try:
                    d = json.loads(body)
                    api_url = str(d.get("api", "")).strip()
                    api_key = str(d.get("key", "")).strip()
                    if not api_url:
                        resp = json.dumps({"ok": False, "msg": "missing api url"})
                    else:
                        import urequests
                        hdrs = {}
                        if api_key:
                            hdrs["X-API-Key"] = api_key
                        print("[HTTP] test_wwwsite:", api_url)
                        r = urequests.get(api_url, headers=hdrs, timeout=10)
                        sc = r.status_code
                        txt = r.text
                        r.close()
                        # 截断避免过大响应撑爆内存
                        if len(txt) > 600:
                            txt = txt[:600] + "..."
                        resp = json.dumps({"ok": 200 <= sc < 400,
                                           "status": sc,
                                           "body": txt})
                except Exception as e:
                    resp = json.dumps({"ok": False, "msg": str(e)})
            _send_response(cl_sock, 200, "application/json", resp)

        else:
            _send_response(cl_sock, 404, "text/plain", "Not Found")

    except Exception as e:
        import sys
        print("[HTTP] Handle error:", e)
        try:
            _send_response(cl_sock, 500, "text/plain", "Internal Server Error")
        except Exception:
            pass
    finally:
        try:
            cl_sock.close()
        except Exception:
            pass
        gc.collect()  # 请求完毕回收临时对象, 降低后续轮询的内存压力


# ==================== 服务器生命周期 ====================

def poll_http_server():
    """
    非阻塞轮询，处理一个待处理的 HTTP 请求（若有）。
    应在主循环或 boot.py 中定期调用。
    """
    if _state._http_server_sock is None:
        return

    try:
        cl, addr = _state._http_server_sock.accept()
        _handle_client(cl)
    except OSError as e:
        err = e.args[0] if e.args else -1
        # MicroPython ESP32: EAGAIN (11) / EWOULDBLOCK: 非阻塞, 无待处理连接 (正常)
        #                    ETIMEDOUT (110): 旧 settimeout 版遗留, 兼容保留
        # 两者均表示"当前无连接", 不应触发 socket 重建
        if err not in (11, 110):
            print("[HTTP] poll error:", e)
            # socket 可能已损坏, 尝试重建
            port = None
            if _state._http_server_sock is not None:
                try:
                    _state._http_server_sock.close()
                except Exception:
                    pass
                _state._http_server_sock = None
            start_http_server(port)


def start_http_server(port=None):
    """启动 HTTP 服务器（非阻塞, 含重试）"""

    if _state._http_server_sock is not None:
        print("[HTTP] Server already running")
        return True

    if port is None:
        config = get_all_config()
        port = config.get("http_port", 80)

    # 最多重试 3 次, 间隔 200ms (应对 SO_REUSEADDR 不完全生效)
    for attempt in range(3):
        try:
            addr = socket.getaddrinfo("0.0.0.0", port)[0][-1]
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(addr)
            s.listen(2)
            s.setblocking(False)  # 非阻塞: accept 无连接立即返回, 不吞主循环帧预算

            _state._http_server_sock = s
            print("[HTTP] Server started on port", port)
            return True

        except OSError as e:
            print("[HTTP] Start attempt %d failed:" % (attempt + 1), e)
            try:
                s.close()
            except Exception:
                pass
            if attempt < 2:
                time_ms = __import__("time").sleep_ms
                time_ms(200)
            continue

        except Exception as e:
            print("[HTTP] Start failed:", e)
            break

    _state._http_server_sock = None
    return False


def stop_http_server():
    """停止 HTTP 服务器"""

    if _state._http_server_sock is not None:
        try:
            _state._http_server_sock.close()
        except Exception:
            pass
        _state._http_server_sock = None
        print("[HTTP] Server stopped")
        return True

    return False


def is_running():
    """检查 HTTP 服务器是否正在运行"""
    return _state._http_server_sock is not None

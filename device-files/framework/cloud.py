"""
XianMuBox - 闲沐盒子云平台客户端 (v1.4.1)

对接闲沐盒子 API 服务平台 (默认 https://api.wwzu.com), 设备侧统一接入层:
  - 激活: POST /api/v1/device/activate (出厂 activation_key 换 device_token)
  - 心跳: POST /api/v1/device/heartbeat (按服务端 next_interval 调度)
  - 取数: GET  /api/v1/data/{source_code} (任意数据源统一入口)
  - 数据源登记: _SOURCES 清单供 Web「云端数据源」卡片展示与逐源探测
    (list_sources / probe_source, v1.4.2)
  - 绑定: 心跳/兜底 pending 指令中的 bind_confirm 激活码存入 _pending,
    由本机设置「云端激活」子页轮询 poll_pending() 取出大字展示,
    用户在平台网页回填完成绑定 (v1.4.1 起不再全屏接管)

设计要点:
  - Token 仅存 RAM (_token), 不落盘不进日志; 重启后自动重新激活
    (平台侧合法重复激活 = Token 轮换, 幂等安全)
  - tick() 非阻塞调度, 供 BaseMode.run()/主菜单每帧调用:
    未启用或未配置密钥时零开销直接返回
  - 请求前置: RTC 年份 >= 2026 (NTP 已校时, 平台要求 TLS 正确时钟)
    且 WiFi 已连接; 不满足不发请求, 原因记入 status()
  - 失败指数退避 30s 起倍增封顶 30min; 激活/心跳异常均不向上抛

后续新增功能接入平台 API 只需:
    from framework.cloud import fetch_json
    code, data = fetch_json("hot_weibo")                       # 无参源
    code, data = fetch_json("weather_basic", {"city": "深圳"})  # 带参源
    code == 0 时渲染 data["payload"]; 错误码语义透传见平台文档

MicroPython 兼容: 无 f-string / time.ticks_* / urequests 惰性 import
"""

import time
import gc

# ===== 平台交互常量 =====
_HB_DEFAULT_S = 600         # 心跳默认间隔 (服务端未下发时)
_BACKOFF_START_MS = 30000   # 指数退避起点
_BACKOFF_MAX_MS = 1800000   # 退避封顶 30min
_FIRST_DELAY_MS = 8000      # 首次上报延迟 (避开开机联网风暴)
_OFFLINE_RETRY_MS = 30000   # 离线期重试节奏
_IDLE_RETRY_MS = 3600000    # 未校时等闲置态复查节奏
_REQ_TIMEOUT = 5            # 单请求超时 (秒)。心跳/激活在每帧主循环内同步执行,
                            # 超时越长该帧按键越迟钝; TLS 握手典型 1~3s, 5s 为安全下限
_MIN_YEAR = 2026            # RTC 明显未校时的判定年份
_PENDING_TTL_MS = 300000    # 激活码本地有效期 (平台 5 分钟, 不解析时间串)
_URL_SAFE = ("abcdefghijklmnopqrstuvwxyz"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~")

# ===== 运行状态 (仅 RAM; Token 绝不落盘/不进日志) =====
_token = None               # device_token (64 位 hex)
_hb_interval = _HB_DEFAULT_S
_next_ms = 0                # 下次动作时刻 (ticks)
_backoff_ms = _BACKOFF_START_MS
_last_err = None            # 最近业务错误 (code, msg)
_last_hb_str = ""           # 最近成功心跳的本地时刻 "HH:MM:SS"
_pending = None             # 待显示绑定码 {"code","expire_at"}
_offline_seen = False       # 离线标记 (重连后补拉 pending)
_model_override = None
_fw_override = None


# ===================== 基础工具 =====================

def _get_cloud_cfg():
    from framework.settings import get_cloud
    return get_cloud()


def ready():
    """云服务是否启用且已配置激活密钥 (无网络检查, 供调用方快速分流)。

    密钥判定走 get_cloud() (含出厂固化默认值回退), 与请求前置一致。
    """
    try:
        cfg = _get_cloud_cfg()
    except Exception:
        return False
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return False
    return bool(str(cfg.get("activation_key", "")).strip())


def configure(model="", fw_version=""):
    """覆盖型号/固件版本 (默认读 config.py 的 CHIP/FW_VERSION; 仿真测试用)"""
    global _model_override, _fw_override
    if model:
        _model_override = str(model)
    if fw_version:
        _fw_override = str(fw_version)


def _device_sn():
    try:
        from framework.settings import get_device_sn
        return get_device_sn()
    except Exception:
        return ""


def _device_model():
    """上报型号: settings.cloud.model > config.CHIP (framework 可合法读 config)"""
    if _model_override:
        return _model_override
    try:
        m = str(_get_cloud_cfg().get("model", "")).strip()
        if m:
            return m
    except Exception:
        pass
    try:
        from config import CHIP
        return str(CHIP)
    except Exception:
        return "esp32"


def _fw_version():
    if _fw_override:
        return _fw_override
    try:
        from config import FW_VERSION
        return str(FW_VERSION)
    except Exception:
        return "0.0.0"


def _time_ok():
    """NTP 前置: RTC 年份明显异常时不得发起 HTTPS (平台文档强制)"""
    try:
        return time.localtime()[0] >= _MIN_YEAR
    except Exception:
        return False


def _precheck():
    """请求前置检查。返回 (ok, 原因): ""/"unconfigured"/"no_time"/"no_wifi\""""
    try:
        cfg = _get_cloud_cfg()
    except Exception:
        return False, "unconfigured"
    if not cfg.get("enabled") or not str(cfg.get("activation_key", "")).strip():
        return False, "unconfigured"
    if not _time_ok():
        return False, "no_time"
    try:
        from framework.wifi import check_wifi
        ok, _ip = check_wifi()
    except Exception:
        ok = False
    if not ok:
        return False, "no_wifi"
    return True, ""


def _quote(s):
    """最小 URL 编码: 保留字母数字与 -_.~, 其余按 UTF-8 转 %XX"""
    out = []
    for ch in s:
        if ch in _URL_SAFE:
            out.append(ch)
        else:
            try:
                buf = ch.encode("utf-8")
            except Exception:
                continue
            for b in buf:
                out.append("%%%02X" % b)
    return "".join(out)


def _urlencode(params):
    parts = []
    for k in params:
        v = params[k]
        if v is None:
            continue
        parts.append(_quote(str(k)) + "=" + _quote(str(v)))
    return "&".join(parts)


# ===================== HTTP 底层 =====================

def _request(method, path, body=None, params=None, auth=True):
    """平台请求底层。返回 (code, data, msg):
    code=None 表示网络失败/响应不可解析; 否则为平台业务码 (0=成功)。
    """
    try:
        base = str(_get_cloud_cfg().get("endpoint", "")).rstrip("/")
    except Exception:
        base = ""
    url = base + path
    if params:
        qs = _urlencode(params)
        if qs:
            url = url + ("&" if "?" in url else "?") + qs

    headers = {}
    if auth:
        headers["X-Device-SN"] = _device_sn()
        if _token:
            headers["X-Device-Token"] = _token
    payload = None
    if body is not None:
        import json
        payload = json.dumps(body)
        headers["Content-Type"] = "application/json"

    gc.collect()
    try:
        import urequests as requests
    except ImportError:
        import requests
    r = None
    try:
        if method == "POST":
            r = requests.post(url, data=payload, headers=headers,
                              timeout=_REQ_TIMEOUT)
        else:
            r = requests.get(url, headers=headers, timeout=_REQ_TIMEOUT)
        try:
            resp = r.json()
        except Exception:
            resp = None
        if not isinstance(resp, dict):
            print("[CLOUD] bad body, http status:",
                  getattr(r, "status_code", "?"))
            return None, None, "bad response"
        code = resp.get("code")
        data = resp.get("data")
        if not isinstance(data, dict):
            data = {}
        return code, data, str(resp.get("msg", ""))
    except Exception as e:
        print("[CLOUD] request error:", e)
        return None, None, "network"
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        gc.collect()


def _schedule_backoff(now):
    """网络/未知业务失败: 指数退避后重试"""
    global _backoff_ms, _next_ms
    _backoff_ms = min(_backoff_ms * 2, _BACKOFF_MAX_MS)
    _next_ms = time.ticks_add(now, _backoff_ms)


# ===================== 激活 / 心跳 =====================

def ensure_active(force=False):
    """确保持有有效 Token (无 Token 或 force=True 时重新激活)。

    返回 (ok, err): err 为 "" / "unconfigured" / "no_time" / "no_wifi" /
    "network" / "bad response" 或平台 msg。
    """
    global _token, _hb_interval, _backoff_ms, _last_err, _next_ms
    if _token and not force:
        return True, ""
    ok, pre = _precheck()
    if not ok:
        return False, pre
    body = {
        "sn": _device_sn(),
        "model": _device_model(),
        "fw_version": _fw_version(),
        "activation_key": str(_get_cloud_cfg().get("activation_key", "")).strip(),
    }
    code, data, msg = _request("POST", "/api/v1/device/activate",
                               body=body, auth=False)
    now = time.ticks_ms()
    if code is None:
        _schedule_backoff(now)
        return False, "network"
    if code == 0:
        tok = str(data.get("device_token", "")).strip()
        if not tok:
            _schedule_backoff(now)
            return False, "bad response"
        _token = tok
        try:
            iv = int(data.get("heartbeat_interval", _HB_DEFAULT_S))
        except (ValueError, TypeError):
            iv = _HB_DEFAULT_S
        _hb_interval = iv if iv > 0 else _HB_DEFAULT_S
        _backoff_ms = _BACKOFF_START_MS
        _last_err = None
        _next_ms = time.ticks_add(now, _hb_interval * 1000)
        print("[CLOUD] activated, hb every %ds" % _hb_interval)
        return True, ""
    # 业务失败: 40104 SN 未登记/密钥错, 40301 被禁用 -> 配置类问题长退避
    _last_err = (code, msg)
    if code in (40104, 40301):
        _backoff_ms = _BACKOFF_MAX_MS
        _next_ms = time.ticks_add(now, _backoff_ms)
    else:
        _schedule_backoff(now)
    print("[CLOUD] activate failed code=%s" % code)
    return False, msg or ("code %s" % code)


def _handle_ops(ops):
    """处理心跳/兜底返回的待执行指令; 只认 bind_confirm, 未知 type 忽略。

    服务端返回空指令列表 = 无待确认绑定 (已确认/已过期), 清空本地信箱。
    """
    global _pending
    if not isinstance(ops, list):
        return
    if not ops:
        _pending = None
        return
    for op in ops:
        if not isinstance(op, dict):
            continue
        if op.get("type") == "bind_confirm":
            c = str(op.get("code", "")).strip()
            if c:
                _pending = {"code": c,
                            "expire_at": str(op.get("expire_at", "")),
                            "ts": time.ticks_ms()}
                print("[CLOUD] bind_confirm received")


def heartbeat():
    """发送一次心跳, 按响应 next_interval 重新调度。返回 (ok, err)"""
    global _token, _hb_interval, _backoff_ms, _last_err, _next_ms, _last_hb_str
    a_ok, a_err = ensure_active()
    if not a_ok:
        return False, a_err
    code, data, msg = _request("POST", "/api/v1/device/heartbeat",
                               body={"fw_version": _fw_version()})
    now = time.ticks_ms()
    if code is None:
        _schedule_backoff(now)
        return False, "network"
    if code == 0:
        _backoff_ms = _BACKOFF_START_MS
        _last_err = None
        try:
            iv = int(data.get("next_interval", _hb_interval))
        except (ValueError, TypeError):
            iv = _hb_interval
        _hb_interval = iv if iv > 0 else _HB_DEFAULT_S
        _next_ms = time.ticks_add(now, _hb_interval * 1000)
        try:
            t = time.localtime()
            _last_hb_str = "%02d:%02d:%02d" % (t[3], t[4], t[5])
        except Exception:
            pass
        _handle_ops(data.get("pending_ops"))
        return True, ""
    if code == 40102:
        # Token 吊销/失效: 清除后短延迟重激活 (回退出厂密钥路径)
        print("[CLOUD] token revoked, will re-activate")
        _token = None
        _backoff_ms = _BACKOFF_START_MS
        _next_ms = time.ticks_add(now, 3000)
        return False, "token revoked"
    _last_err = (code, msg)
    _schedule_backoff(now)
    return False, msg or ("code %s" % code)


def _extract_ops(data):
    """从 pending 响应提取指令列表。

    服务端 GET /device/pending 返回键为 "ops"; 兼容 "pending_ops"
    (与心跳响应同键), 异常结构一律按无指令处理。
    """
    if not isinstance(data, dict):
        return None
    ops = data.get("ops")
    if not isinstance(ops, list):
        ops = data.get("pending_ops")
    return ops if isinstance(ops, list) else None


def pending_fallback():
    """离线重连兜底: GET /device/pending 拉取错过的待执行指令"""
    _code, data, _msg = _request("GET", "/api/v1/device/pending")
    if _code == 0:
        _handle_ops(_extract_ops(data))


def poll_pending():
    """「云端激活」子页轮询: 确保持 Token 后拉取绑定码等待执行指令。

    与 tick() 互斥使用 (子页为阻塞循环, 全局 tick 停摆, 由本函数接管);
    收到 bind_confirm 存入 _pending, 由 get_pending() 读取。
    返回 (ok, err): ok=False 时 err 为激活前置/网络原因。
    """
    global _token
    a_ok, a_err = ensure_active()
    if not a_ok:
        return False, a_err
    code, data, _msg = _request("GET", "/api/v1/device/pending")
    if code == 40102:
        # Token 被吊销: 重激活后重试一次
        _token = None
        a_ok, a_err = ensure_active()
        if not a_ok:
            return False, a_err
        code, data, _msg = _request("GET", "/api/v1/device/pending")
    if code is None:
        return False, "network"
    if code == 0:
        _handle_ops(_extract_ops(data))
    return True, ""


def tick():
    """全局空闲调度 (BaseMode.run/主菜单每帧调用)。

    未启用/未配置时零开销返回; 到期后补激活或心跳。
    WiFi 断开期间顺延, 重连后先补拉 pending 再恢复心跳节奏。
    """
    global _next_ms, _offline_seen
    if not ready():
        return
    now = time.ticks_ms()
    if _next_ms == 0:
        _next_ms = time.ticks_add(now, _FIRST_DELAY_MS)
        return
    if time.ticks_diff(now, _next_ms) < 0:
        return
    ok, pre = _precheck()
    if not ok:
        if pre == "no_wifi":
            _offline_seen = True
            _next_ms = time.ticks_add(now, _OFFLINE_RETRY_MS)
        else:
            _next_ms = time.ticks_add(now, _IDLE_RETRY_MS)
        return
    if _offline_seen:
        _offline_seen = False
        pending_fallback()
    heartbeat()


# ===================== 统一取数 (任意数据源) =====================

def fetch_json(source_code, params=None):
    """统一取数: GET /api/v1/data/{source_code}。

    返回 (code, data):
      code == 0     -> data = {"source","version","updated_at","stale",
                              "payload", ...}, 渲染 data["payload"]
      code == 42901 -> data = {"retry_after": 秒} (限流, 建议按其退避)
      code == 40102 -> Token 失效且重激活未成功
      其他          -> 平台错误码原样透传 (40101 未绑定/40302 未订阅/
                       40401 源不存在/50301 停用/50401 暂无数据 ...)
      code is None  -> 网络失败
    """
    global _token
    a_ok, _a_err = ensure_active()
    if not a_ok:
        return None, None
    path = "/api/v1/data/" + str(source_code).strip()
    code, data, msg = _request("GET", path, params=params)
    if code is None:
        return None, None
    if code == 40102:
        # Token 被吊销: 重新激活后重试一次
        _token = None
        a_ok, _a_err = ensure_active()
        if not a_ok:
            return 40102, None
        code, data, msg = _request("GET", path, params=params)
        if code is None:
            return None, None
    if code == 0:
        return 0, data
    if code == 42901:
        try:
            wait = int(data.get("retry_after", 60))
        except (ValueError, TypeError):
            wait = 60
        return 42901, {"retry_after": wait if wait > 0 else 60}
    global _last_err
    _last_err = (code, msg)
    return code, None


# ===================== 数据源注册表 (Web「云端数据源」卡片) =====================

# 固件登记清单: 平台清单不可用时兜底展示 (code 与平台一致);
# 正常情况以平台 GET /api/v1/data/sources 返回为准 (fetch_sources 缓存)
_SOURCES = [
    {"code": "lottery_ssq", "name": "双色球开奖", "used_by": "天天中奖"},
    {"code": "lottery_dlt", "name": "大乐透开奖", "used_by": "天天中奖"},
]

_sources_cache = None       # 平台数据源清单缓存 (RAM, 重启丢失重拉即得)

# 取数错误码 -> (状态字, 展示文本); 未覆盖的码按 "错误码 N" 透传
_SOURCE_ERR_STATES = {
    40101: ("unbound", "设备未绑定"),
    40302: ("unsubscribed", "未订阅"),
    40401: ("missing", "源不存在"),
    50301: ("disabled", "源已停用"),
    50401: ("nodata", "暂无数据"),
    42901: ("ratelimited", "限流中"),
}


def list_sources():
    """当前生效数据源清单 (纯内存, 不发网络请求):
    平台缓存优先, 无缓存回落固件登记清单"""
    if _sources_cache is not None:
        return [dict(s) for s in _sources_cache]
    return [{"code": s["code"], "name": s["name"],
             "used_by": s.get("used_by", "")} for s in _SOURCES]


def fetch_sources(force=False):
    """平台数据源清单 GET /api/v1/data/sources (v1.4.2)。

    force=False: 纯内存 (有缓存返回缓存, 否则固件登记清单);
    force=True: 阻塞拉取平台清单并刷新缓存 (约 1~10s),
    云未启用/网络/业务失败一律回落固件登记清单。
    返回 {"origin": "cloud"/"local", "sources": [...]}。
    """
    global _sources_cache
    if not force and _sources_cache is not None:
        return {"origin": "cloud", "sources": _sources_cache}
    local = {"origin": "local",
             "sources": [{"code": s["code"], "name": s["name"],
                          "used_by": s.get("used_by", "")}
                         for s in _SOURCES]}
    if not force or not ready():
        return local
    a_ok, _a_err = ensure_active()
    if not a_ok:
        return local
    code, data, _msg = _request("GET", "/api/v1/data/sources")
    if code != 0 or not isinstance(data, dict):
        return local
    rows = data.get("sources")
    if not isinstance(rows, list) or not rows:
        return local
    lst = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        c = str(r.get("source", "")).strip()
        if not c:
            continue
        lst.append({"code": c,
                    "name": str(r.get("name", c)),
                    "category": str(r.get("category", "")),
                    "poll_interval": r.get("poll_interval", 0),
                    "updated_at": str(r.get("updated_at", "")),
                    "source_name": str(r.get("source_name", ""))})
    if not lst:
        return local
    _sources_cache = lst
    return {"origin": "cloud", "sources": lst}


def probe_source(source_code):
    """探测单个数据源可用性 (阻塞, 单请求约 1~10s), 供 Web /api/cloud/probe。

    返回 {code, name, state, state_text, detail}, state 语义:
    ok / stale(数据过期) / network / unconfigured / unbound / unsubscribed /
    missing / disabled / nodata / ratelimited / unknown(未登记) / error
    """
    src = str(source_code).strip()
    out = {"code": src, "name": "", "state": "error",
           "state_text": "探测失败", "detail": ""}
    for s in list_sources():
        if s.get("code") == src:
            out["name"] = str(s.get("name", ""))
            break
    else:
        out["state"] = "unknown"
        out["state_text"] = "未登记"
        return out
    if not ready():
        out["state"] = "unconfigured"
        out["state_text"] = "云服务未启用"
        return out
    code, data = fetch_json(src)
    if code is None:
        out["state"] = "network"
        out["state_text"] = "网络失败"
    elif code == 0:
        out["state"] = "ok"
        out["state_text"] = "正常"
        parts = []
        sn = str(data.get("source_name", "")).strip()
        if sn:
            parts.append(sn)
        ua = str(data.get("updated_at", "")).strip()
        if ua:
            parts.append("更新于 " + ua)
        out["detail"] = " / ".join(parts)
        if data.get("stale"):
            out["state"] = "stale"
            out["state_text"] = "数据过期"
    elif code == 42901:
        out["state"] = "ratelimited"
        out["state_text"] = "限流中"
        try:
            wait = int(data.get("retry_after", 0))
        except (ValueError, TypeError):
            wait = 0
        if wait > 0:
            out["detail"] = "%ds 后可重试" % wait
    elif code in _SOURCE_ERR_STATES:
        out["state"], out["state_text"] = _SOURCE_ERR_STATES[code]
    else:
        out["state_text"] = "错误码 %s" % code
    return out


# ===================== 绑定码信箱 =====================

def get_pending():
    """待显示激活码 {"code","expire_at"} (不消费)。

    超过平台 5 分钟有效期按过期处理返回 None (expire_at 时间串不作
    解析, 按平台约定用本地 5 分钟计时)。
    """
    global _pending
    if not _pending:
        return None
    try:
        if time.ticks_diff(time.ticks_ms(), _pending.get("ts", 0)) > _PENDING_TTL_MS:
            _pending = None
            return None
    except Exception:
        pass
    return _pending


# ===================== 状态查询 / 手动触发 =====================

def status():
    """当前云服务状态 (不发起网络请求), 供 Web /api/cloud 与设置页展示"""
    try:
        cfg = _get_cloud_cfg()
    except Exception:
        cfg = {}
    code = _last_err[0] if _last_err else None
    pend = get_pending()
    st = {
        "endpoint": str(cfg.get("endpoint", "")),
        "enabled": bool(cfg.get("enabled")),
        "configured": ready(),
        "activated": _token is not None,
        "state": _state_key(cfg),
        "sn": _device_sn(),
        "model": _device_model(),
        "fw_version": _fw_version(),
        "interval": _hb_interval,
        "err_code": code,
        "err_msg": _last_err[1] if _last_err else "",
        "last_hb": _last_hb_str,
        "pending_code": pend["code"] if pend else "",
    }
    return st


def _state_key(cfg):
    """状态机关键字: disabled/unconfigured/no_time/binding/active/inactive"""
    if not cfg.get("enabled"):
        return "disabled"
    if not str(cfg.get("activation_key", "")).strip():
        return "unconfigured"
    if get_pending():
        return "binding"
    if _token:
        return "active"
    if not _time_ok():
        return "no_time"
    return "inactive"


def activate_now():
    """手动立即激活 (Web/设置页触发); force 会轮换 Token (平台幂等)"""
    return ensure_active(force=True)


def heartbeat_now():
    """手动立即心跳 (Web/设置页触发)"""
    return heartbeat()

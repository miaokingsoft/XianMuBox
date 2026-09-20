"""
XianMuBox - 统一配置访问层
闲沐工坊 · 插件式架构

settings.json 是设备唯一的应用级与敏感配置来源，取代旧的
secrets.py（WiFi/API_KEY）与 swipe_settings.json（滑动参数）。

本模块是 settings.json 的唯一读写入口：
  - load_settings()       加载完整配置（文件缺失时写默认值并返回）
  - get(key, default)     读取单个顶层字段
  - get_wifi_configs()    读取 WiFi 列表（供 framework.wifi 使用）
  - get_api_keys()        读取 API 密钥字典（供 ai_balance 使用）
  - get_swipe_config()    读取 swipe 子节点（供 modes.swipe 使用）
  - update(partial)       验证 + 合并 + 持久化（HTTP / 设置菜单复用）
  - save_settings()       全量回写（兼容 http_server 的框架钩子）

配置结构 (v1.2)::

    {
      "wifi": [{"ssid": "...", "password": "..."}, ...],
      "api_keys": {
          # 共享代理 token (单值, 不列表化)
          "ai_token": "sk-xxx",
          # 每个服务商一个列表, 每一项是一个独立的 API key
          # 旧格式 {"deepseek": "sk-..."} 会在首次 load_settings 时自动迁移
          "deepseek":    [{"label": "DeepSeek-1",    "key": "sk-..."}],
          "siliconflow": [{"label": "SiliconFlow-1", "key": "sk-..."}],
          "zhipu":       [{"label": "Zhipu-1",       "key": "sk-..."}]
      },
      "brightness": 100,     # 屏幕亮度百分比 25/50/75/100
      "http_port": 80,
      "cloud": {             # v1.4 闲沐盒子云平台 (激活/心跳/统一取数)
        "endpoint": "https://api.wwzu.com",
        "activation_key": "",  # 型号激活密钥 (Web 云服务页签填写)
        "model": "",           # 设备型号代号 (空 = 自动取 config.CHIP)
        "enabled": True
      },
      "alarms": [            # v1.3 网络时钟闹钟 (恒 3 组, days 空 = 单次)
        {"en": False, "h": 7, "m": 30, "days": [1, 2, 3, 4, 5],
         "msg": ""}, ...],
      "news": {              # v1.5 整点新闻播报
        "enabled": True,     # 全局自动播报总开关
        "hours": [9..22],    # 允许自动播报的整点档位 (0-23 升序)
        "volume": 80         # 播报音量 0-100
      },
      "swipe": {             # 原 swipe_settings.json 全部字段
        "interval_min": 5, "interval_max": 15,
        "screen_w": 1080, "screen_h": 1920,
        "mode_idx": 0, "swipe_offset_ratio": 6,
        "swipe_steps": 5, "swipe_delay_ms": 20,
        "active_device_idx": 0, "devices": [...]
      }
    }
"""

import json

_SETTINGS_FILE = "settings.json"
_LEGACY_SWIPE_FILE = "swipe_settings.json"

# ===================== 服务商元数据 (v1.2 多 key 支持) =====================
# provider_key (settings.json 中使用的代号) → {"label": 显示名, "api_provider": 余额代理识别的代号}
_PROVIDER_META = {
    "deepseek":    {"label": "DeepSeek",    "api_provider": "DEEPSEEK"},
    "siliconflow": {"label": "SiliconFlow", "api_provider": "SiliconFlow"},
    "zhipu":       {"label": "Zhipu",       "api_provider": "ZHIPU"},
    "kimi":        {"label": "Kimi",        "api_provider": "kimi"},
}
# 单值 key (不走列表化, 仍以字符串存储)
_SINGLE_KEY_PROVIDERS = {"ai_token"}

# ===== 默认配置（首次启动 / 文件缺失时使用）=====
# swipe 配置 v2.0：
#   - modes: 4 种滑动模式各自独立的间隔与滑动参数
#   - devices: 仅存储不同手机的分辨率（name/screen_w/screen_h）
#   - active_device_idx: 当前使用哪台设备的分辨率
#   - mode_idx: 当前活跃的滑动模式
_DEFAULT_MODE = {
    "interval_min": 5,
    "interval_max": 15,
    "swipe_offset_ratio": 6,
    "swipe_steps": 5,
    "swipe_delay_ms": 20,
}
_DEFAULT_SWIPE = {
    "mode_idx": 0,
    "active_device_idx": 0,
    "modes": [
        {"name": "刷新视频-上滑", "interval_min": 5, "interval_max": 15,
         "swipe_offset_ratio": 6, "swipe_steps": 5, "swipe_delay_ms": 20},
        {"name": "刷新视频-下滑", "interval_min": 5, "interval_max": 15,
         "swipe_offset_ratio": 6, "swipe_steps": 5, "swipe_delay_ms": 20},
        {"name": "看书-左滑", "interval_min": 5, "interval_max": 15,
         "swipe_offset_ratio": 6, "swipe_steps": 5, "swipe_delay_ms": 20},
        {"name": "看书-右滑", "interval_min": 5, "interval_max": 15,
         "swipe_offset_ratio": 6, "swipe_steps": 5, "swipe_delay_ms": 20},
    ],
    "devices": [],
}

# ===== project 子节点默认（v1.0：AI Token + 自媒体 + 自有网站） =====
# v1.1 新增 device_sn 字段: 设备端以 machine.unique_id() 派生值为唯一权威,
# 本字段仅为只读展示缓存 (Web/文件篡改均无效, 读取时自动按硬件纠偏).
_DEFAULT_PROJECT = {
    "ai_token": "",
    "device_sn": "",
    "self_media": [],
    "own_sites": [],
}

# ===== weather 子节点默认 =====
_DEFAULT_WEATHER = {
    "api_key": "",
    "city": "深圳",
}

# ===== alarms 子节点默认 (v1.3: 网络时钟闹钟) =====
# 固定 3 组, 每组: en 开关 / h 时(0-23) / m 分(0-59) / days 重复星期 /
# msg 自定义提醒文字(≤16 字, 响铃时替换 ALARM 大字显示, 空=显示 ALARM)
# days: 0=周日..6=周六 (与 RTC.datetime weekday 一致), 空列表 = 单次(触发后自动关闭)
_ALARM_COUNT = 3
_ALARM_MSG_MAX = 16


def _default_alarms():
    return [{"en": False, "h": 7, "m": 30, "days": [1, 2, 3, 4, 5], "msg": ""},
            {"en": False, "h": 12, "m": 0, "days": [], "msg": ""},
            {"en": False, "h": 22, "m": 0, "days": [0, 6], "msg": ""}]


# ===== stock 子节点默认 (v1.1: 闲沐数观股票 API 配置) =====
# endpoint: 用户级 API 基础地址
# api_key:  用户级 key (从 own_sites 拆分出来独立维护)
_DEFAULT_STOCK = {
    "endpoint": "http://www.mooview.cn/api/",
    "api_key": "",
}

# ===== cloud 子节点默认 (v1.4: 闲沐盒子云平台) =====
# endpoint:       平台基础地址 (默认 https://api.wwzu.com)
# activation_key: 型号激活密钥 (出厂/私有构建注入, 与 model 在平台侧配对登记;
#                 不经 Web 展示/填写, 用户绑定走 SN + 激活码流程;
#                 公开产物默认留空 = 云平台不可激活)
# model:          设备型号代号 (固化默认, 空 = 运行时自动取 config.CHIP)
# enabled:        云服务总开关 (关闭后不发任何平台请求)
_DEFAULT_CLOUD = {
    "endpoint": "https://api.wwzu.com",
    "activation_key": "",
    "model": "XianMu_Esp32S3",
    "enabled": True,
}

# ===== news 子节点默认 (v1.5: 整点新闻播报) =====
# enabled: 全局自动播报总开关 (关闭后整点不播, 新闻模式内手动播报不受限)
# hours:   允许自动播报的整点档位 (0-23 去重升序; 平台档期 9~22 共 14 档)
# volume:  播报音量 0-100 (framework/audio 移位缩放, 约 6dB 步进)
_DEFAULT_NEWS = {
    "enabled": True,
    "hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
    "volume": 80,
}

# ===== radio 子节点默认 (v2.4: 网络电台 radio_relay) =====
# volume:       收听音量 0-100 (framework/audio 移位缩放)
# last_station: 上次收听的电台代码 (台单页光标定位用)
_DEFAULT_RADIO = {
    "volume": 80,
    "last_station": "",
}

# ===== story 子节点默认 (v1.0: 听老故事 comic_audio) =====
# volume:     播放音量 0-100 (framework/audio 移位缩放)
# last_tag:   上次收听的标签名 (标签页光标定位用)
# last_story: 上次收听的连环画 htmlname (故事页光标定位用;
#             2026-09-19 平台契约改版后为字符串, 旧数字 id 值失配
#             自动回到从头播, 无需迁移)
# last_track: 上次听到的话序号 (清单下标, 0=从头; 整本播完归零)
_DEFAULT_STORY = {
    "volume": 80,
    "last_tag": "",
    "last_story": "",
    "last_track": 0,
}

DEFAULT_SETTINGS = {
    "wifi": [],
    "api_keys": {},
    "api_keys_hidden": [],   # 停用显示的服务商代号列表 (设备不读取/不显示)
    "brightness": 100,
    "http_port": 80,
    "swipe": dict(_DEFAULT_SWIPE),
    "project": dict(_DEFAULT_PROJECT),
    "weather": dict(_DEFAULT_WEATHER),
    "stock": dict(_DEFAULT_STOCK),
    "cloud": dict(_DEFAULT_CLOUD),
    "news": dict(_DEFAULT_NEWS),
    "radio": dict(_DEFAULT_RADIO),
    "story": dict(_DEFAULT_STORY),
    "alarms": _default_alarms(),
}

# ===== 亮度档位（百分比 -> PWM duty），与 modes.settings 保持一致 =====
BRIGHTNESS_LEVELS = (
    (25,  256),
    (50,  512),
    (75,  768),
    (100, 1023),
)

# ===== 各字段校验范围（update 时使用）=====
# 注: 时区已固定北京时间 UTC+8, 不再作为配置字段
_INT_RANGES = {
    "brightness": (1, 100),
    "http_port": (1, 65535),
}

# swipe v2.0 顶层标量字段范围
_SWIPE_INT_RANGES = {
    "mode_idx": (0, 3),
    "active_device_idx": (0, 100),
}

# swipe v2.0 模式参数字段范围（modes 数组内使用）
_SWIPE_MODE_INT_RANGES = {
    "interval_min": (1, 120),
    "interval_max": (5, 300),
    "swipe_offset_ratio": (1, 100),
    "swipe_steps": (1, 100),
    "swipe_delay_ms": (1, 1000),
}

# swipe v2.0 设备分辨率字段范围（devices 数组内使用）
_SWIPE_DEVICE_INT_RANGES = {
    "screen_w": (100, 4000),
    "screen_h": (100, 6000),
}

# 旧版 swipe schema 字段范围（仅用于一次性迁移校验）
_LEGACY_SWIPE_INT_RANGES = {
    "interval_min": (1, 120),
    "interval_max": (5, 300),
    "screen_w": (100, 4000),
    "screen_h": (100, 6000),
    "mode_idx": (0, 100),
    "swipe_offset_ratio": (1, 100),
    "swipe_steps": (1, 100),
    "swipe_delay_ms": (1, 1000),
    "active_device_idx": (0, 100),
}

# ===== 进程内缓存，避免每次读盘 =====
_cache = None


# ==================== MicroPython 兼容工具 ====================

def _title_case(s):
    """MicroPython str 没有 .title() 方法；用此函数生成显示标签 (首字母大写)。

    仅用于 _PROVIDER_META 里未收录的 provider 的 fallback 显示名，
    已知服务商 (deepseek/siliconflow/zhipu) 走 meta["label"]，不会走到这里。
    """
    if not s:
        return s
    return s[0].upper() + s[1:] if len(s) > 1 else s.upper()


# ==================== 底层 IO ====================

def _read_file():
    """读原始 JSON dict，失败返回 None"""
    try:
        with open(_SETTINGS_FILE, "r") as f:
            return json.load(f)
    except OSError:
        return None
    except Exception as e:
        print("[SETTINGS] read error:", e)
        return None


def _write_file(d):
    """全量写入 settings.json"""
    try:
        with open(_SETTINGS_FILE, "w") as f:
            json.dump(d, f)
        return True
    except Exception as e:
        print("[SETTINGS] write error:", e)
        return False


def _migrate_legacy_swipe(merged):
    """一次性迁移：若存在旧 swipe_settings.json，并入 swipe 子节点后删除。"""
    try:
        with open(_LEGACY_SWIPE_FILE, "r") as f:
            legacy = json.load(f)
    except Exception:
        return merged

    swipe = dict(_DEFAULT_SWIPE)
    # 把旧文件里的标量字段搬过来
    for k in _LEGACY_SWIPE_INT_RANGES:
        if k in legacy:
            try:
                swipe[k] = int(legacy[k])
            except (ValueError, TypeError):
                pass
    if "devices" in legacy and isinstance(legacy["devices"], list):
        swipe["devices"] = legacy["devices"]

    merged["swipe"] = swipe
    print("[SETTINGS] Migrated legacy", _LEGACY_SWIPE_FILE)
    try:
        import os
        os.remove(_LEGACY_SWIPE_FILE)
    except Exception:
        pass
    return merged


def _migrate_swipe_schema(merged):
    """一次性迁移：旧版 swipe schema（全局参数 + 设备里含滑动参数）升级到 v2.0。

    触发条件：swipe 节点里存在旧版全局字段（interval_min / swipe_offset_ratio / screen_w 等）。
    迁移规则：
      - 把全局间隔与滑动参数填充到 4 个模式（初始值相同，用户后续可在 UI 分别调整）
      - 把旧设备中的 swipe_* 参数剥离，只保留 name / screen_w / screen_h
      - 如果旧设备为空，则根据旧全局分辨率生成一个默认设备
    返回 (merged, migrated_bool)
    """
    swipe = merged.get("swipe")
    if not isinstance(swipe, dict):
        return merged, False

    # 新版 schema 特征：存在 modes 数组且顶层没有旧全局参数字段
    has_old_global = any(k in swipe for k in (
        "interval_min", "interval_max", "screen_w", "screen_h",
        "swipe_offset_ratio", "swipe_steps", "swipe_delay_ms"))
    if not has_old_global:
        return merged, False

    new_swipe = dict(_DEFAULT_SWIPE)
    new_swipe["mode_idx"] = int(swipe.get("mode_idx", 0))
    new_swipe["active_device_idx"] = int(swipe.get("active_device_idx", 0))

    # 收集旧全局参数作为 4 个模式的初始值
    base_mode = {
        "interval_min": 5,
        "interval_max": 15,
        "swipe_offset_ratio": 6,
        "swipe_steps": 5,
        "swipe_delay_ms": 20,
    }
    for k in base_mode:
        if k in swipe:
            try:
                v = int(swipe[k])
                if _SWIPE_MODE_INT_RANGES[k][0] <= v <= _SWIPE_MODE_INT_RANGES[k][1]:
                    base_mode[k] = v
            except (ValueError, TypeError):
                pass

    # 应用默认名称
    mode_names = ["刷新视频-上滑", "刷新视频-下滑", "看书-左滑", "看书-右滑"]
    new_swipe["modes"] = []
    for name in mode_names:
        m = dict(base_mode)
        m["name"] = name
        new_swipe["modes"].append(m)

    # 迁移设备列表：只保留 name / screen_w / screen_h
    old_devices = swipe.get("devices", [])
    new_devices = []
    if isinstance(old_devices, list):
        for dev in old_devices:
            if not isinstance(dev, dict):
                continue
            d = {}
            if "name" in dev:
                d["name"] = str(dev["name"])
            for k in ("screen_w", "screen_h"):
                if k in dev:
                    try:
                        v = int(dev[k])
                        if _SWIPE_DEVICE_INT_RANGES[k][0] <= v <= _SWIPE_DEVICE_INT_RANGES[k][1]:
                            d[k] = v
                    except (ValueError, TypeError):
                        pass
            if d:
                if "name" not in d:
                    d["name"] = "Phone"
                new_devices.append(d)

    # 没有设备时，用旧全局分辨率生成一个默认设备
    if not new_devices:
        default_dev = {"name": "Phone"}
        for k in ("screen_w", "screen_h"):
            if k in swipe:
                try:
                    v = int(swipe[k])
                    if _SWIPE_DEVICE_INT_RANGES[k][0] <= v <= _SWIPE_DEVICE_INT_RANGES[k][1]:
                        default_dev[k] = v
                except (ValueError, TypeError):
                    pass
        if "screen_w" not in default_dev:
            default_dev["screen_w"] = 1080
        if "screen_h" not in default_dev:
            default_dev["screen_h"] = 1920
        new_devices.append(default_dev)

    new_swipe["devices"] = new_devices
    merged["swipe"] = new_swipe
    print("[SETTINGS] Migrated swipe to v2.0 schema")
    return merged, True


def _migrate_project(merged):
    """一次性迁移：把 api_keys.ai_token 移到 project.ai_token（v1.0 项目页签拆分）。

    仅当 project 节点不存在或缺少 ai_token 时触发；迁移完成后从 api_keys 移除 ai_token。
    返回 (merged, migrated_bool)
    """
    api = merged.get("api_keys")
    if not isinstance(api, dict) or "ai_token" not in api:
        return merged, False

    token = api.get("ai_token", "")
    proj = merged.get("project")
    if not isinstance(proj, dict):
        proj = dict(_DEFAULT_PROJECT)
        merged["project"] = proj

    if not proj.get("ai_token") and token:
        proj["ai_token"] = str(token)

    # 从 api_keys 中移除 ai_token（避免与新位置冲突）
    try:
        del api["ai_token"]
    except Exception:
        pass

    print("[SETTINGS] Migrated api_keys.ai_token -> project.ai_token")
    return merged, True


def _migrate_stock(merged):
    """一次性迁移: 把 own_sites 里 name=="stock" 的条目独立到 stock 子节点.

    旧版 (v1.0) 股票 API 借用 own_sites 维护, 现拆分为独立 stock 节点.
    - own_sites 中的 stock 条目保留 (避免影响其他模块), 但迁移其 api/key 到 stock
    - 已存在 stock.api_key 时跳过迁移
    返回 (merged, migrated_bool)
    """
    stock = merged.get("stock")
    if not isinstance(stock, dict):
        stock = dict(_DEFAULT_STOCK)
        merged["stock"] = stock

    # 已配置 api_key 则不再迁移
    if stock.get("api_key"):
        return merged, False

    own_sites = merged.get("project", {}).get("own_sites") if isinstance(merged.get("project"), dict) else None
    if not isinstance(own_sites, list):
        return merged, False

    for it in own_sites:
        if isinstance(it, dict) and str(it.get("name", "")).strip().lower() == "stock":
            api = str(it.get("api", "")).strip()
            key = str(it.get("key", "")).strip()
            if api and not stock.get("endpoint"):
                stock["endpoint"] = api
            if key and not stock.get("api_key"):
                stock["api_key"] = key
            if stock.get("api_key"):
                print("[SETTINGS] Migrated own_sites.stock -> stock node")
                return merged, True

    return merged, False


def _migrate_api_keys_v12(merged):
    """一次性迁移: api_keys 从 v1.1 扁平 {provider: "sk-..."} 升级到 v1.2 分组列表。

    只对以下情况触发:
      1. key 不在 _SINGLE_KEY_PROVIDERS 里 (如 deepseek / siliconflow / zhipu)
      2. value 是字符串 (旧 schema)
      3. value 非空 (空字符串直接当 list 处理)

    升级后写入 {"label": "{显示名}-1", "key": "..."} 单元素列表, 并落盘。
    返回 (merged, migrated_bool)
    """
    api = merged.get("api_keys")
    if not isinstance(api, dict):
        return merged, False

    migrated = False
    out = {}
    for prov, val in api.items():
        out[prov] = val
        if prov in _SINGLE_KEY_PROVIDERS:
            continue
        if not isinstance(val, str):
            continue  # 已是 list 或其他结构, 不动
        if not val:
            # 空字符串 -> 空列表 (新 schema 下 "未配置" 用 [] 表示)
            out[prov] = []
            migrated = True
            continue
        meta = _PROVIDER_META.get(prov, {})
        label = meta.get("label", _title_case(prov))
        out[prov] = [{"label": label, "key": val}]
        migrated = True

    if migrated:
        merged["api_keys"] = out
        print("[SETTINGS] Migrated api_keys to v1.2 (multi-key)")
    return merged, migrated


def _validate_alarms(alarms):
    """校验 alarms 节点 (v1.3: 网络时钟闹钟).

    固定 3 组, 每组:
      en:   bool 开关
      h/m:  时(0-23) / 分(0-59), 越界 clamp
      days: 星期白名单 {0..6} 去重排序, 空列表 = 单次
      msg:  自定义提醒文字, strip 后截断到 _ALARM_MSG_MAX 字符
    返回 (None, 规范化列表) 或 (错误信息, None)。
    """
    if not isinstance(alarms, list):
        return ("alarms must be array", None)
    out = []
    for i in range(_ALARM_COUNT):
        a = alarms[i] if i < len(alarms) and isinstance(alarms[i], dict) else {}
        try:
            h = int(a.get("h", 0))
        except (ValueError, TypeError):
            h = 0
        try:
            m = int(a.get("m", 0))
        except (ValueError, TypeError):
            m = 0
        if h < 0:
            h = 0
        elif h > 23:
            h = 23
        if m < 0:
            m = 0
        elif m > 59:
            m = 59
        days = a.get("days")
        day_set = set()
        if isinstance(days, list):
            for d in days:
                try:
                    d = int(d)
                except (ValueError, TypeError):
                    continue
                if 0 <= d <= 6:
                    day_set.add(d)
        try:
            msg = str(a.get("msg", "") or "").strip()[:_ALARM_MSG_MAX]
        except Exception:
            msg = ""
        out.append({"en": bool(a.get("en", False)), "h": h, "m": m,
                    "days": sorted(day_set), "msg": msg})
    return (None, out)


def _migrate_alarms(merged):
    """一次性迁移: alarms 节点缺失或结构非法时补默认值 (v1.3 新增)。

    返回 (merged, migrated_bool)
    """
    al = merged.get("alarms")
    if isinstance(al, list) and len(al) == _ALARM_COUNT and all(
            isinstance(x, dict) and "h" in x and "m" in x for x in al):
        return merged, False
    _, norm = _validate_alarms(al)
    merged["alarms"] = norm
    print("[SETTINGS] Initialized alarms node")
    return merged, True


def _validate_news(n):
    """校验 news 子节点 (v1.5: 整点新闻播报).

    字段:
      enabled: bool 自动播报总开关
      hours:   允许自动播报的整点档位列表 (0-23 去重升序, 整体替换式)
      volume:  音量 0-100
    返回 (None, 规范化 dict) 或 (错误信息, None)。
    """
    if not isinstance(n, dict):
        return None
    out = {}
    if "enabled" in n:
        out["enabled"] = bool(n["enabled"])
    if "hours" in n:
        if not isinstance(n["hours"], list):
            return ("news.hours must be array", None)
        hs = set()
        for h in n["hours"]:
            try:
                h = int(h)
            except (ValueError, TypeError):
                continue
            if 0 <= h <= 23:
                hs.add(h)
        out["hours"] = sorted(hs)
    if "volume" in n:
        try:
            v = int(n["volume"])
        except (ValueError, TypeError):
            return ("news.volume must be integer", None)
        if not (0 <= v <= 100):
            return ("news.volume must be 0-100", None)
        out["volume"] = v
    return (None, out)


def _migrate_news(merged):
    """一次性迁移: news 节点缺失或结构非法时补默认值 (v1.5 新增)。

    返回 (merged, migrated_bool)
    """
    n = merged.get("news")
    if isinstance(n, dict) and isinstance(n.get("hours"), list) \
            and "enabled" in n and "volume" in n:
        return merged, False
    _, norm = _validate_news(n)
    if norm is None:
        norm = dict(_DEFAULT_NEWS)
    merged["news"] = norm
    print("[SETTINGS] Initialized news node")
    return merged, True


def _validate_radio(n):
    """校验 radio 子节点 (v2.4: 网络电台 radio_relay).

    字段:
      volume:       音量 0-100
      last_station: 上次收听电台代码 (<=16 字符, 台单光标定位用)
    返回 (None, 规范化 dict) 或 (错误信息, None)。
    """
    if not isinstance(n, dict):
        return (None, None)
    out = {}
    if "volume" in n:
        try:
            v = int(n["volume"])
        except (ValueError, TypeError):
            return ("radio.volume must be integer", None)
        if not (0 <= v <= 100):
            return ("radio.volume must be 0-100", None)
        out["volume"] = v
    if "last_station" in n:
        out["last_station"] = str(n["last_station"]).strip()[:16]
    return (None, out)


def _migrate_radio(merged):
    """一次性迁移: radio 节点缺失或结构非法时补默认值 (v2.4 新增)。

    返回 (merged, migrated_bool)
    """
    n = merged.get("radio")
    if isinstance(n, dict) and "volume" in n and "last_station" in n:
        return merged, False
    _, norm = _validate_radio(n if isinstance(n, dict) else {})
    if norm is None:
        norm = dict(_DEFAULT_RADIO)
    merged["radio"] = norm
    print("[SETTINGS] Initialized radio node")
    return merged, True


def _validate_story(n):
    """校验 story 子节点 (v1.0: 听老故事 comic_audio).

    字段:
      volume:     音量 0-100
      last_tag:   上次收听标签名 (<=16 字符, 光标定位用)
      last_story: 上次收听连环画 htmlname (<=32 字符; v1.1 契约改版
                  后为字符串, 存量数字 id 值转字符串无害失配)
      last_track: 上次听到的话序号 (>=0, 清单下标)
    返回 (None, 规范化 dict) 或 (错误信息, None)。
    """
    if not isinstance(n, dict):
        return (None, None)
    out = {}
    if "volume" in n:
        try:
            v = int(n["volume"])
        except (ValueError, TypeError):
            return ("story.volume must be integer", None)
        if not (0 <= v <= 100):
            return ("story.volume must be 0-100", None)
        out["volume"] = v
    if "last_tag" in n:
        out["last_tag"] = str(n["last_tag"]).strip()[:16]
    if "last_story" in n:
        out["last_story"] = str(n["last_story"]).strip()[:32]
    if "last_track" in n:
        try:
            out["last_track"] = max(0, int(n["last_track"]))
        except (ValueError, TypeError):
            return ("story.last_track must be integer", None)
    return (None, out)


def _migrate_story(merged):
    """一次性迁移: story 节点缺失或结构非法时补默认值 (v1.0 新增)。

    返回 (merged, migrated_bool)
    """
    n = merged.get("story")
    if isinstance(n, dict) and "volume" in n and "last_story" in n \
            and "last_track" in n and "last_tag" in n:
        return merged, False
    _, norm = _validate_story(n if isinstance(n, dict) else {})
    if norm is None:
        norm = dict(_DEFAULT_STORY)
    merged["story"] = norm
    print("[SETTINGS] Initialized story node")
    return merged, True


def _merge_defaults(raw):
    """把读到的 raw dict 与默认值合并，补齐缺失字段"""
    if not isinstance(raw, dict):
        raw = {}
    merged = {}
    for k, v in DEFAULT_SETTINGS.items():
        # swipe / api_keys 这类可变容器需要深拷贝默认值，避免污染
        if isinstance(v, dict):
            merged[k] = dict(v)
            if isinstance(raw.get(k), dict):
                merged[k].update(raw[k])
        elif isinstance(v, list):
            merged[k] = list(raw[k]) if isinstance(raw.get(k), list) else []
        else:
            merged[k] = raw.get(k, v)
    # 兜底: raw 中存在但 DEFAULT_SETTINGS 没有声明的 key, 也要保留
    # (例如后续新增的字段, 旧版本读不到默认时仍能透传)
    for k in raw.keys():
        if k not in merged:
            merged[k] = raw[k]
    return merged


# ==================== 对外 API ====================

def load_settings(force=False):
    """
    加载完整配置 dict。
    - 文件缺失时：写入默认值并返回。
    - 命中缓存且未强制刷新时直接返回缓存。

    force=True 时忽略缓存重新读盘。
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    raw = _read_file()
    if raw is None:
        # 首次启动：生成默认文件
        merged = _merge_defaults({})
        merged = _migrate_legacy_swipe(merged)
        merged, _ = _migrate_swipe_schema(merged)
        merged, _ = _migrate_api_keys_v12(merged)
        merged, _ = _migrate_project(merged)
        merged, _ = _migrate_stock(merged)
        merged, _ = _migrate_alarms(merged)
        merged, _ = _migrate_news(merged)
        merged, _ = _migrate_radio(merged)
        merged, _ = _migrate_story(merged)
        _write_file(merged)
        _cache = merged
        print("[SETTINGS] Created default", _SETTINGS_FILE)
        return merged

    merged = _merge_defaults(raw)
    merged = _migrate_legacy_swipe(merged)
    merged, swipe_migrated = _migrate_swipe_schema(merged)
    merged, api_migrated = _migrate_api_keys_v12(merged)
    merged, project_migrated = _migrate_project(merged)
    merged, stock_migrated = _migrate_stock(merged)
    merged, alarms_migrated = _migrate_alarms(merged)
    merged, news_migrated = _migrate_news(merged)
    merged, radio_migrated = _migrate_radio(merged)
    merged, story_migrated = _migrate_story(merged)
    # 迁移可能改动了内容，落盘保证一致
    if (swipe_migrated or api_migrated or project_migrated
            or stock_migrated or alarms_migrated or news_migrated
            or radio_migrated or story_migrated):
        _write_file(merged)
    _cache = merged
    return merged


def get(key, default=None):
    """读取单个顶层字段"""
    return load_settings().get(key, default)


def get_wifi_configs():
    """读取 WiFi 配置列表，供 framework.wifi 使用。
    返回 [{"ssid": str, "password": str}, ...]"""
    wifi = load_settings().get("wifi", [])
    out = []
    if isinstance(wifi, list):
        for entry in wifi:
            if not isinstance(entry, dict):
                continue
            ssid = entry.get("ssid", "")
            pwd = entry.get("password", "")
            if ssid:
                out.append({"ssid": str(ssid), "password": str(pwd)})
    return out


def get_api_keys():
    """读取 API 密钥字典，供 ai_balance 使用。
    返回 dict（即使缺失也返回 {}，调用方用 .get() 兜底）。"""
    keys = load_settings().get("api_keys", {})
    return keys if isinstance(keys, dict) else {}


def get_swipe_config():
    """读取 swipe 子节点（已合并默认值）"""
    swipe = load_settings().get("swipe", {})
    if not isinstance(swipe, dict):
        swipe = {}
    base = dict(_DEFAULT_SWIPE)
    base.update(swipe)
    return base


def _validate_wifi(wifi):
    """校验 wifi 列表"""
    if not isinstance(wifi, list):
        return None
    out = []
    for entry in wifi:
        if not isinstance(entry, dict):
            continue
        ssid = str(entry.get("ssid", "")).strip()
        pwd = str(entry.get("password", ""))
        if ssid:
            out.append({"ssid": ssid, "password": pwd})
    return out


def _validate_api_keys(keys):
    """校验 api_keys 字典 (v1.2 多 key schema).

    每项 schema:
      - ai_token 等单值 key: 必须是 str (空字符串允许)
      - 其他服务商: 可以是
          str (旧 schema, 自动升级为单元素列表) 或
          list[{"label": str, "key": str}] (新 schema)
      - 空字符串 / 空列表 都视为 "未配置", 保留该 key 但 value 为空
    """
    if not isinstance(keys, dict):
        return None
    out = {}
    for prov, v in keys.items():
        prov = str(prov)
        if prov in _SINGLE_KEY_PROVIDERS:
            # 单值 key, 始终是字符串 (空字符串允许)
            out[prov] = str(v) if v is not None else ""
            continue
        if isinstance(v, str):
            # 旧 schema -> 升级为单元素列表 (空串 -> 空列表)
            if v:
                meta = _PROVIDER_META.get(prov, {})
                label = meta.get("label", _title_case(prov))
                out[prov] = [{"label": label[:24], "key": str(v)}]
            else:
                out[prov] = []
            continue
        if isinstance(v, list):
            items = []
            for it in v:
                if not isinstance(it, dict):
                    continue
                k = str(it.get("key", "")).strip()
                if not k:
                    continue
                meta = _PROVIDER_META.get(prov, {"label": _title_case(prov)})
                default_lbl = meta.get("label", _title_case(prov))
                lbl = str(it.get("label", "")).strip() or default_lbl
                items.append({"label": lbl[:24], "key": k})
            out[prov] = items
            continue
        # 未知类型 -> 跳过
    return out


def _validate_api_keys_hidden(v):
    """校验 api_keys_hidden (停用显示的服务商代号列表).

    list[str] -> 去空/去重保序; 其他类型返回 None.
    """
    if not isinstance(v, list):
        return None
    out = []
    for item in v:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def get_provider_keys(provider):
    """读取指定服务商的所有 key (v1.2).

    返回 [{label, key}, ...] 列表 (空列表表示未配置).
    自动兼容旧 schema (扁平字符串), 旧值会被就地看作 [{label: '<Provider>-1', key: '...'}].
    历史数据修正: 已迁移到 list 但 label 仍带 "-<数字>" 序号 (如 "DeepSeek-1") 且与
    默认显示名匹配时, 自动剥掉 "-<数字>" 后缀, 统一显示为 "DeepSeek".
    """
    api = load_settings().get("api_keys", {})
    if not isinstance(api, dict):
        return []
    meta = _PROVIDER_META.get(provider, {"label": _title_case(provider)})
    default_lbl = meta.get("label", _title_case(provider))

    def _strip_suffix(name):
        """剥掉 '-<数字>' 后缀 (如 'DeepSeek-1' -> 'DeepSeek')"""
        if not name or "-" not in name:
            return name
        head, _, tail = name.rpartition("-")
        if tail.isdigit() and head == default_lbl:
            return head
        return name

    raw = api.get(provider)
    if isinstance(raw, list):
        out = []
        for it in raw:
            if not isinstance(it, dict):
                continue
            k = str(it.get("key", "")).strip()
            if not k:
                continue
            lbl = str(it.get("label", "")).strip()
            if not lbl:
                lbl = default_lbl
            else:
                lbl = _strip_suffix(lbl)
            out.append({"label": lbl[:24], "key": k})
        return out
    if isinstance(raw, str) and raw:
        return [{"label": default_lbl, "key": raw}]
    return []


def get_token():
    """读取共享代理 token (优先 project.ai_token，回落 api_keys.ai_token)."""
    cfg = load_settings()
    proj = cfg.get("project")
    if isinstance(proj, dict):
        t = proj.get("ai_token", "")
        if t:
            return str(t)
    api = cfg.get("api_keys", {})
    if isinstance(api, dict):
        t = api.get("ai_token", "")
        if t:
            return str(t)
    return ""


def _derive_sn_from_hw():
    """从 machine.unique_id() 派生 SN; 非设备端返回 None.

    返回 'SN-AABBCCDDEEFF' (取 unique_id 前 6 字节) 或 None
    (PC/仿真器无 machine 模块, 或 uid 缺失/异常).
    """
    try:
        import machine  # type: ignore
        uid = machine.unique_id()
    except Exception:
        return None
    if not isinstance(uid, (bytes, bytearray)) or len(uid) < 6:
        return None
    return "SN-" + "".join("%02X" % b for b in uid[:6])


def get_device_sn():
    """读取设备唯一序列号, 返回值始终非空.

    设备端以 machine.unique_id() 派生值为唯一权威 (芯片 UUID 出厂固化,
    物理只读): project.device_sn 落盘值仅为只读展示缓存, 手改无效,
    与硬件派生值不一致时下次读取自动纠偏回写.
    PC/仿真器无 machine 模块, 沿用缓存值, 无缓存才生成一次.
    """
    sn = _derive_sn_from_hw()
    if sn is not None:
        _cache_device_sn(sn)
        return sn
    return ensure_device_sn()


def ensure_device_sn():
    """确保存在设备序列号并落盘, 返回值始终非空 (开机自检/兼容入口).

    - 设备端: 与 get_device_sn() 同源, 一律硬件派生并纠偏缓存
    - 桌面/PC 调试环境: 缓存值优先, 无缓存用随机 6 字节生成一次
      (保证同一调试环境内 SN 稳定)
    """
    sn = _derive_sn_from_hw()
    if sn is None:
        cfg = load_settings()
        proj = cfg.get("project")
        if isinstance(proj, dict):
            cached = str(proj.get("device_sn", "")).strip()
            if cached:
                return cached
        try:
            import secrets  # type: ignore
            uid = secrets.token_bytes(6)
        except Exception:
            uid = b"\x00\x00\x00\x00\x00\x01"
        if not isinstance(uid, (bytes, bytearray)) or len(uid) < 6:
            uid = b"\x00\x00\x00\x00\x00\x01"
        sn = "SN-" + "".join("%02X" % b for b in uid[:6])
        _cache_device_sn(sn, "pc")
        return sn
    _cache_device_sn(sn)
    return sn


def _cache_device_sn(sn, src="hw"):
    """把权威 SN 写回 project.device_sn 展示缓存 (不一致才落盘).

    src: 'hw' = 硬件派生纠偏 / 'pc' = PC 端首次随机生成.
    """
    cfg = load_settings()
    proj = cfg.get("project")
    if not isinstance(proj, dict):
        proj = dict(_DEFAULT_PROJECT)
        cfg["project"] = proj
    if str(proj.get("device_sn", "")).strip() != sn:
        proj["device_sn"] = sn
        save_settings()
        if src == "hw":
            print("[SETTINGS] device_sn synced from hardware:", sn)
        else:
            print("[SETTINGS] Generated PC-side device_sn:", sn)


def get_stock_config():
    """读取股票子节点 (闲沐数观 API).

    返回 {"endpoint": str, "api_key": str}, 缺省回落到 own_sites 中
    name=="stock" 的条目 (历史兼容).
    """
    cfg = load_settings()
    out = {
        "endpoint": _DEFAULT_STOCK["endpoint"],
        "api_key":  "",
    }
    st = cfg.get("stock")
    if isinstance(st, dict):
        if st.get("endpoint"):
            out["endpoint"] = str(st["endpoint"])
        if st.get("api_key"):
            out["api_key"] = str(st["api_key"])
    # 兜底: own_sites 中 name=="stock" 的条目
    if not out["api_key"]:
        proj = cfg.get("project")
        if isinstance(proj, dict):
            for it in proj.get("own_sites", []) or []:
                if isinstance(it, dict) and str(it.get("name", "")).strip().lower() == "stock":
                    api = str(it.get("api", "")).strip()
                    key = str(it.get("key", "")).strip()
                    if api:
                        out["endpoint"] = api
                    if key:
                        out["api_key"] = key
                    break
    return out


def get_cloud():
    """读取 cloud 子节点 (闲沐盒子云平台, 已合并默认值).

    返回 {"endpoint": str, "activation_key": str, "model": str,
          "enabled": bool}
    activation_key/model 留空时回退出厂固化默认值 (关闭云服务
    用 enabled=False, 清空密钥不能作为关闭手段)。
    """
    cfg = load_settings().get("cloud", {})
    if not isinstance(cfg, dict):
        cfg = {}
    base = dict(_DEFAULT_CLOUD)
    base.update(cfg)
    if not str(base.get("activation_key", "")).strip():
        base["activation_key"] = _DEFAULT_CLOUD["activation_key"]
    if not str(base.get("model", "")).strip():
        base["model"] = _DEFAULT_CLOUD["model"]
    return base


def get_alarms():
    """读取 alarms 子节点 (网络时钟闹钟, 已合并默认值, 恒 3 组)。

    返回 [{"en": bool, "h": int, "m": int, "days": [int...],
           "msg": str}, ...]
    days 为空列表表示单次闹钟 (触发后自动关闭);
    msg 为响铃时显示的自定义提醒文字 (空 = 显示默认 ALARM)。
    """
    _, out = _validate_alarms(load_settings().get("alarms"))
    return out


def get_news():
    """读取 news 子节点 (整点新闻播报, 已合并默认值)。

    返回 {"enabled": bool, "hours": [int...], "volume": int}
    hours 为允许自动播报的整点档位 (0-23 升序);
    volume 为播报音量 0-100。
    """
    base = dict(_DEFAULT_NEWS)
    cfg = load_settings().get("news", {})
    if isinstance(cfg, dict):
        base.update(cfg)
    _, out = _validate_news(base)
    if out is None:
        out = dict(_DEFAULT_NEWS)
    return out


def get_radio():
    """读取 radio 子节点 (网络电台, 已合并默认值)。

    返回 {"volume": int 0-100, "last_station": str}
    """
    base = dict(_DEFAULT_RADIO)
    cfg = load_settings().get("radio", {})
    if isinstance(cfg, dict):
        base.update(cfg)
    _, out = _validate_radio(base)
    if out is None:
        out = dict(_DEFAULT_RADIO)
    return out


def get_story():
    """读取 story 子节点 (听老故事, 已合并默认值)。

    返回 {"volume": int 0-100, "last_tag": str, "last_story": str,
          "last_track": int}
    """
    base = dict(_DEFAULT_STORY)
    cfg = load_settings().get("story", {})
    if isinstance(cfg, dict):
        base.update(cfg)
    _, out = _validate_story(base)
    if out is None:
        out = dict(_DEFAULT_STORY)
    return out


def get_project():
    """读取 project 子节点（已合并默认值）"""
    proj = load_settings().get("project", {})
    if not isinstance(proj, dict):
        proj = {}
    base = dict(_DEFAULT_PROJECT)
    base.update(proj)
    return base


def get_all_providers():
    """返回当前 settings.json 里所有非单值的服务商代号 (按定义顺序)."""
    api = load_settings().get("api_keys", {})
    if not isinstance(api, dict):
        return []
    return [k for k in api.keys() if k not in _SINGLE_KEY_PROVIDERS]  # noqa: PERF401


def _validate_swipe_modes(modes):
    """校验 swipe.modes 数组，返回规范化后的列表"""
    if not isinstance(modes, list):
        return None
    out = []
    for m in modes:
        if not isinstance(m, dict):
            continue
        d = {}
        if "name" in m:
            d["name"] = str(m["name"])
        for k, (lo, hi) in _SWIPE_MODE_INT_RANGES.items():
            if k not in m:
                continue
            try:
                v = int(m[k])
            except (ValueError, TypeError):
                continue
            if lo <= v <= hi:
                d[k] = v
        # 只有至少包含一个有效参数字段才保留
        if d:
            out.append(d)
    return out


def _validate_swipe_devices(devices):
    """校验 swipe.devices 数组，只保留 name / screen_w / screen_h"""
    if not isinstance(devices, list):
        return None
    out = []
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        d = {}
        if "name" in dev:
            d["name"] = str(dev["name"])
        for k, (lo, hi) in _SWIPE_DEVICE_INT_RANGES.items():
            if k not in dev:
                continue
            try:
                v = int(dev[k])
            except (ValueError, TypeError):
                continue
            if lo <= v <= hi:
                d[k] = v
        if d:
            out.append(d)
    return out


def _validate_swipe(swipe):
    """校验 swipe 子节点（v2.0 schema）"""
    if not isinstance(swipe, dict):
        return None
    out = {}
    for k, (lo, hi) in _SWIPE_INT_RANGES.items():
        if k not in swipe:
            continue
        try:
            v = int(swipe[k])
        except (ValueError, TypeError):
            return ("swipe.%s must be integer" % k, None)
        if not (lo <= v <= hi):
            return ("swipe.%s must be %d-%d" % (k, lo, hi), None)
        out[k] = v
    if "modes" in swipe:
        modes = _validate_swipe_modes(swipe["modes"])
        if modes is not None:
            out["modes"] = modes
    if "devices" in swipe:
        devs = _validate_swipe_devices(swipe["devices"])
        if devs is not None:
            out["devices"] = devs
    return (None, out)


# ===== project 子节点校验（v1.0） =====
# 自媒体类别白名单
_SELF_MEDIA_CATEGORIES = {"douyin", "kuaishou", "xiaohongshu", "bilibili"}


def _validate_project(p):
    """校验 project 子节点.

    device_sn 字段: 设备端由 machine.unique_id() 派生 (硬件权威),
    校验函数显式拒绝 Web 端写入修改 (忽略该字段).
    """
    if not isinstance(p, dict):
        return None
    out = {}
    if "ai_token" in p:
        out["ai_token"] = str(p["ai_token"]) if p["ai_token"] is not None else ""
    # device_sn 仅设备端可写, Web 端尝试 POST 写入时静默忽略
    if "self_media" in p and isinstance(p["self_media"], list):
        items = []
        for it in p["self_media"]:
            if not isinstance(it, dict):
                continue
            d = {}
            cat = str(it.get("category", "")).strip().lower()
            if cat and cat in _SELF_MEDIA_CATEGORIES:
                d["category"] = cat
            if "name" in it:
                d["name"] = str(it["name"])[:64]
            if "url" in it:
                d["url"] = str(it["url"])[:512]
            if d:
                items.append(d)
        out["self_media"] = items
    if "own_sites" in p and isinstance(p["own_sites"], list):
        items = []
        for it in p["own_sites"]:
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
                items.append(d)
        out["own_sites"] = items
    return (None, out)


# ===== stock 子节点校验 (v1.1) =====
def _validate_stock(s):
    """校验 stock 子节点 (闲沐数观用户级 API).

    字段:
      endpoint: API 基础地址 (默认 http://www.mooview.cn/api/)
      api_key:  用户级 key
    """
    if not isinstance(s, dict):
        return None
    out = {}
    if "endpoint" in s:
        ep = str(s["endpoint"] or "").strip()
        if ep:
            out["endpoint"] = ep[:512]
    if "api_key" in s:
        out["api_key"] = str(s["api_key"] or "")[:128]
    return (None, out)


# ===== cloud 子节点校验 (v1.4: 闲沐盒子云平台) =====
def _validate_cloud(c):
    """校验 cloud 子节点.

    字段:
      endpoint:       平台基础地址 (http(s):// 开头, 推荐 HTTPS)
      activation_key: 型号激活密钥
      model:          设备型号代号 (空 = 自动取 config.CHIP)
      enabled:        云服务总开关
    """
    if not isinstance(c, dict):
        return None
    out = {}
    if "endpoint" in c:
        ep = str(c["endpoint"] or "").strip()
        if ep:
            if not ep.startswith("http://") and not ep.startswith("https://"):
                return ("cloud.endpoint must start with http(s)://", None)
            # 去掉末尾 '/' 统一拼接形式
            out["endpoint"] = ep.rstrip("/")[:256]
    if "activation_key" in c:
        out["activation_key"] = str(c["activation_key"] or "").strip()[:128]
    if "model" in c:
        out["model"] = str(c["model"] or "").strip()[:64]
    if "enabled" in c:
        out["enabled"] = bool(c["enabled"])
    return (None, out)


def _validate_weather(w):
    """校验 weather 子节点 (心知天气 v3)

    配置字段:
      api_key: 心知天气 API Key
      city:    中文城市名 (如 "武汉")
      fallback: 兜底数据 (网络失败时使用)
    """
    if not isinstance(w, dict):
        return None
    out = {}
    if "api_key" in w:
        out["api_key"] = str(w["api_key"])[:128]
    if "city" in w:
        out["city"] = str(w["city"])[:16]
    # 兜底字段透传 (不严格校验, 失败时使用)
    if "fallback" in w and isinstance(w["fallback"], dict):
        fb = w["fallback"]
        out["fallback"] = {
            "city":       str(fb.get("city", ""))[:16],
            "text":       str(fb.get("text", "多云"))[:32],
            "code":       str(fb.get("code", "cloudy"))[:32],
            "temp":       int(fb["temp"]) if isinstance(fb.get("temp"), (int, float)) else 25,
        }
    return (None, out)


def update(partial, persist=True):
    """
    验证并合并部分配置，可选持久化。

    partial: 仅含待修改字段的 dict，支持顶层 wifi / api_keys /
             brightness / http_port / swipe。
    persist: 是否写盘。

    返回 {"ok": bool, "changed": bool, "msg": str}
    """
    if not isinstance(partial, dict):
        return {"ok": False, "msg": "body must be object"}

    current = load_settings()
    result = {}
    changed = False

    # ----- wifi -----
    if "wifi" in partial:
        v = _validate_wifi(partial["wifi"])
        if v is None:
            return {"ok": False, "msg": "wifi must be array"}
        if v != current.get("wifi"):
            result["wifi"] = v
            changed = True

    # ----- api_keys -----
    if "api_keys" in partial:
        v = _validate_api_keys(partial["api_keys"])
        if v is None:
            return {"ok": False, "msg": "api_keys must be object"}
        if v != current.get("api_keys"):
            result["api_keys"] = v
            changed = True

    # ----- api_keys_hidden (服务商显示开关, 整体替换式) -----
    if "api_keys_hidden" in partial:
        v = _validate_api_keys_hidden(partial["api_keys_hidden"])
        if v is None:
            return {"ok": False, "msg": "api_keys_hidden must be array"}
        if v != current.get("api_keys_hidden", []):
            result["api_keys_hidden"] = v
            changed = True

    # ----- 简单整数字段 -----
    for key, (lo, hi) in _INT_RANGES.items():
        if key not in partial:
            continue
        try:
            v = int(partial[key])
        except (ValueError, TypeError):
            return {"ok": False, "msg": "%s must be integer" % key}
        if not (lo <= v <= hi):
            return {"ok": False, "msg": "%s must be %d-%d" % (key, lo, hi)}
        if v != current.get(key):
            result[key] = v
            changed = True

    # ----- swipe 子节点 -----
    if "swipe" in partial:
        err, sw = _validate_swipe(partial["swipe"])
        if err:
            return {"ok": False, "msg": err}
        old_sw = current.get("swipe", {})
        merged_sw = dict(old_sw)
        merged_sw.update(sw)
        if merged_sw != old_sw:
            result["swipe"] = merged_sw
            changed = True

    # ----- project 子节点 -----
    if "project" in partial:
        err, pr = _validate_project(partial["project"])
        if err:
            return {"ok": False, "msg": err}
        old_pr = current.get("project", {})
        if not isinstance(old_pr, dict):
            old_pr = dict(_DEFAULT_PROJECT)
        merged_pr = dict(old_pr)
        merged_pr.update(pr)
        if merged_pr != old_pr:
            result["project"] = merged_pr
            changed = True

    # ----- weather 子节点 -----
    if "weather" in partial:
        err, wx = _validate_weather(partial["weather"])
        if err:
            return {"ok": False, "msg": err}
        old_wx = current.get("weather", {})
        if not isinstance(old_wx, dict):
            old_wx = {}
        merged_wx = dict(old_wx)
        merged_wx.update(wx)
        if merged_wx != old_wx:
            result["weather"] = merged_wx
            changed = True

    # ----- stock 子节点 (v1.1: 闲沐数观用户级 API) -----
    if "stock" in partial:
        err, st = _validate_stock(partial["stock"])
        if err:
            return {"ok": False, "msg": err}
        old_st = current.get("stock", {})
        if not isinstance(old_st, dict):
            old_st = dict(_DEFAULT_STOCK)
        merged_st = dict(old_st)
        merged_st.update(st)
        if merged_st != old_st:
            result["stock"] = merged_st
            changed = True

    # ----- cloud 子节点 (v1.4: 闲沐盒子云平台) -----
    if "cloud" in partial:
        err, cl = _validate_cloud(partial["cloud"])
        if err:
            return {"ok": False, "msg": err}
        old_cl = current.get("cloud", {})
        if not isinstance(old_cl, dict):
            old_cl = dict(_DEFAULT_CLOUD)
        merged_cl = dict(old_cl)
        merged_cl.update(cl)
        if merged_cl != old_cl:
            result["cloud"] = merged_cl
            changed = True

    # ----- alarms 节点 (v1.3: 网络时钟闹钟, 整体替换式) -----
    if "alarms" in partial:
        err, al = _validate_alarms(partial["alarms"])
        if err:
            return {"ok": False, "msg": err}
        if al != current.get("alarms"):
            result["alarms"] = al
            changed = True

    # ----- news 子节点 (v1.5: 整点新闻播报, 合并式) -----
    if "news" in partial:
        err, nw = _validate_news(partial["news"])
        if err:
            return {"ok": False, "msg": err}
        old_nw = current.get("news", {})
        if not isinstance(old_nw, dict):
            old_nw = dict(_DEFAULT_NEWS)
        merged_nw = dict(old_nw)
        merged_nw.update(nw)
        if merged_nw != old_nw:
            result["news"] = merged_nw
            changed = True

    # ----- radio 子节点 (v2.4: 网络电台, 合并式) -----
    if "radio" in partial:
        err, rd = _validate_radio(partial["radio"])
        if err:
            return {"ok": False, "msg": err}
        old_rd = current.get("radio", {})
        if not isinstance(old_rd, dict):
            old_rd = dict(_DEFAULT_RADIO)
        merged_rd = dict(old_rd)
        merged_rd.update(rd)
        if merged_rd != old_rd:
            result["radio"] = merged_rd
            changed = True

    # ----- story 子节点 (v1.0: 听老故事, 合并式) -----
    if "story" in partial:
        err, sy = _validate_story(partial["story"])
        if err:
            return {"ok": False, "msg": err}
        old_sy = current.get("story", {})
        if not isinstance(old_sy, dict):
            old_sy = dict(_DEFAULT_STORY)
        merged_sy = dict(old_sy)
        merged_sy.update(sy)
        if merged_sy != old_sy:
            result["story"] = merged_sy
            changed = True

    if persist and changed:
        merged = dict(current)
        merged.update(result)
        if _write_file(merged):
            global _cache
            _cache = merged

    return {"ok": True, "changed": changed,
            "msg": "Saved!" if changed else "No change"}


def save_settings():
    """全量回写当前缓存配置（兼容 http_server 的框架钩子）。"""
    global _cache
    if _cache is None:
        _cache = load_settings()
    return _write_file(_cache)


def reload():
    """强制重新读盘并刷新缓存"""
    return load_settings(force=True)

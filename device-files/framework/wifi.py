"""
XianMuBox - WiFi 服务
闲沐工坊 · 插件式架构

统一 WiFi 接口，提供两个函数：
  - connect_wifi()  开机阶段阻塞式连接（带 SSID 回调）
  - check_wifi()    菜单入口快速查询（不发起连接）
"""

import time

# WiFi 配置来源已迁移到 settings.json，通过 framework/settings 读取
try:
    from framework.settings import get_wifi_configs as _load_wifi_configs
except ImportError:
    _load_wifi_configs = None

_CONN_TIMEOUT_MS = 5000

# 模块级 WLAN 缓存，避免重复创建
_wlan = None


def _get_wlan():
    """获取或创建 WLAN STA 接口（单例）"""
    global _wlan
    if _wlan is None:
        import network
        _wlan = network.WLAN(network.STA_IF)
    return _wlan


def _pm_off(wlan):
    """关闭 STA 省电 (0xA11140 = PM_NONE)。

    modem sleep 在 DTIM 间隔休眠, 延迟尖峰可达数百 ms, 是整点新闻流播
    卡顿源之一。必须在 active(True) 之后调用: esp_wifi 未初始化时
    esp_wifi_set_pm 会被驱动以 NOT INIT 拒绝, 放在创建对象时静默吞掉
    等于从未生效。
    """
    try:
        wlan.config(pm=0xA11140)
    except Exception:
        pass


def _disconnect_for_reconnect(wlan):
    """重连前安全断开: disconnect 后等待真正去关联完成（最多 1.5s）。

    软复位/重启后残留关联状态下, disconnect 后立刻 connect 会被驱动拒绝
    (OSError: Wifi Invalid Mode) 或静默忽略, 导致所有 SSID 瞬间失败。
    必须等 isconnected() 翻 False (去关联完成) 再发起新连接。
    """
    try:
        wlan.disconnect()
    except Exception:
        pass
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < 1500:
        if not wlan.isconnected():
            break
        time.sleep_ms(50)
    time.sleep_ms(100)


def connect_wifi(on_ssid=None):
    """
    阻塞式 WiFi 连接（开机阶段调用）。
    逐个遍历 wifi_config，每个 SSID 等待 _CONN_TIMEOUT_MS。

    on_ssid: 可选回调 on_ssid(ssid_str)，每次尝试新 SSID 时触发

    返回 (connected: bool, ip: str or None)
    """
    wifi_configs = _load_wifi_configs() if _load_wifi_configs else []
    if not wifi_configs:
        return (False, None)

    wlan = _get_wlan()
    try:
        wlan.active(True)
        _pm_off(wlan)
        time.sleep_ms(200)

        # 与 WiFiConnector.start 同理: 重启后 isconnected 可能为 True 但
        # DHCP/DNS 已丢, 必须重连让 DHCP 完整重跑
        _disconnect_for_reconnect(wlan)

        for entry in wifi_configs:
            ssid = entry["ssid"]
            if on_ssid:
                on_ssid(ssid)
            try:
                wlan.connect(ssid, entry["password"])
            except Exception:
                continue

            start = time.ticks_ms()
            while time.ticks_diff(time.ticks_ms(), start) < _CONN_TIMEOUT_MS:
                if wlan.isconnected():
                    return (True, wlan.ifconfig()[0])
                time.sleep_ms(100)

            try:
                wlan.disconnect()
            except Exception:
                pass
            time.sleep_ms(50)

        return (False, None)
    except Exception:
        try:
            wlan.active(False)
        except Exception:
            pass
        return (False, None)


def check_wifi():
    """
    快速查询 WiFi 状态（菜单入口调用一次）。
    不发起新连接，仅检查当前是否已连接。

    返回 (connected: bool, ip: str or None)
    """
    try:
        wlan = _get_wlan()
        if wlan.isconnected():
            return (True, wlan.ifconfig()[0])
        return (False, None)
    except Exception:
        return (False, None)


def reconnect_wifi():
    """
    断开当前 WiFi 并重新连接 (阻塞式, 由 HTTP API 触发)。
    遍历 wifi_config 中的所有 SSID, 超时 5s/个。

    返回 (connected: bool, ip: str or None)
    """
    wlan = _get_wlan()
    try:
        wlan.disconnect()
        time.sleep_ms(500)
        wlan.active(False)
        time.sleep_ms(200)
        wlan.active(True)
        _pm_off(wlan)
        time.sleep_ms(200)
    except Exception:
        pass

    # 复用 connect_wifi 的连接逻辑
    return connect_wifi()


# ===================== 非阻塞 WiFi 连接器 =====================

class WiFiConnector:
    """非阻塞 WiFi 连接状态机。

    用于开机动画等场景，在保持界面动画运行的同时进行 WiFi 连接。

    用法::
        conn = WiFiConnector()
        conn.start()
        while conn.state == 'connecting':
            conn.tick()
            # 渲染动画、更新进度条、显示 SSID ...
            time.sleep_ms(30)
        ok, ip = conn.result

    属性:
        state       - 'idle' | 'connecting' | 'success' | 'failed'
        progress    - 0~100 连接进度百分比
        current_ssid- 当前正在尝试的 SSID（字符串或 None）
        result      - (connected: bool, ip: str or None)
    """

    def __init__(self, configs=None):
        if configs is None:
            if _load_wifi_configs:
                configs = _load_wifi_configs()
            else:
                configs = []
        self._configs = configs if configs else []
        self._wlan = None
        self._state = 'idle'
        self._config_idx = 0
        self._start_ticks = 0
        self._result = (False, None)
        self._current_ssid = None
        self._progress = 0

    @property
    def state(self):
        return self._state

    @property
    def progress(self):
        return self._progress

    @property
    def current_ssid(self):
        return self._current_ssid

    @property
    def result(self):
        return self._result

    def start(self):
        """初始化 WLAN 并开始连接尝试。"""
        self._wlan = _get_wlan()
        try:
            self._wlan.active(True)
            _pm_off(self._wlan)
        except Exception:
            self._state = 'failed'
            self._result = (False, None)
            return
        time.sleep_ms(200)

        # 关键: 不能信任 isconnected() 的"已连接"。软复位/重启后 WLAN 驱动层
        # 关联保留、isconnected 为 True, 但 LwIP 重新初始化后 DHCP/DNS 配置
        # 已丢失, 直接使用会导致 NTP/HTTP 全部 DNS 失败(-202), 开机时间停在
        # 旧值。必须 disconnect 后重新连接, 让 DHCP 完整重跑。冷启动时
        # isconnected 本来就是 False, 不受影响; 重启场景多花几秒重连。
        # 同理: WLAN 接口处于残留连接状态时, 新的 wlan.connect() 会被静默
        # 忽略导致超时失败, 也必须先 disconnect (参考 reconnect_wifi)。
        _disconnect_for_reconnect(self._wlan)

        if not self._configs:
            self._state = 'failed'
            self._result = (False, None)
            return

        self._config_idx = 0
        self._try_connect_current()

    def _try_connect_current(self):
        """发起对当前配置条目的连接。"""
        entry = self._configs[self._config_idx]
        self._current_ssid = entry["ssid"]
        self._start_ticks = time.ticks_ms()
        self._state = 'connecting'
        try:
            self._wlan.connect(self._current_ssid, entry["password"])
        except Exception:
            self._next_config()

    def _next_config(self):
        """切换到下一个 WiFi 配置条目。"""
        # 等待去关联完成再连下一个, 否则 connect 可能被驱动拒绝/忽略
        _disconnect_for_reconnect(self._wlan)

        self._config_idx += 1
        if self._config_idx >= len(self._configs):
            self._state = 'failed'
            self._result = (False, None)
            self._current_ssid = None
            return

        self._try_connect_current()

    def tick(self):
        """执行一次非阻塞步骤。在 state=='connecting' 时循环调用。"""
        if self._state != 'connecting':
            return

        if self._wlan.isconnected():
            self._state = 'success'
            self._result = (True, self._wlan.ifconfig()[0])
            self._progress = 100
            return

        elapsed = time.ticks_diff(time.ticks_ms(), self._start_ticks)
        total = len(self._configs)
        share = 100 // total

        in_config = min(_CONN_TIMEOUT_MS, elapsed)
        config_progress = share * in_config // _CONN_TIMEOUT_MS
        self._progress = min(99, self._config_idx * share + config_progress)

        if elapsed >= _CONN_TIMEOUT_MS:
            self._next_config()

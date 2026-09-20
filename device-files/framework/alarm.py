"""
XianMuBox - 全局闹钟服务 (v1.3)

由 BaseMode.run() 主循环逐帧调用 check(), 设备开机后任意功能模式
运行中闹钟到点即接管屏幕进入响铃页; 任意按键停止, 60s 自动静默。

配置来源: settings.json 的 alarms 节点 (恒 3 组):
  {"en": bool, "h": 0-23, "m": 0-59, "days": [0=周日..6=周六], "msg": str}
days 为空列表 = 单次 (触发后自动关闭并落盘)。
星期匹配用日期计算 (Zeller), 不依赖 RTC weekday 字段
(其基准随写入路径变化, 约定不一致曾致按星期闹钟永不匹配)。

设计约束:
  - check() 常驻每帧: 只做 RTC 分钟变化比较, 开销极小
  - 分钟沿匹配 + 5 分钟回看补漏 (秒沿会被阻塞操作跳过漏响)
  - 配置热加载: 每秒沿直接读 settings 模块缓存 (Web 保存即更新
    该缓存, ≤1s 生效), 不依赖文件 mtime (flash 时间戳粒度粗,
    同秒写入无法检出)
  - 触发后的响铃交互是阻塞式接管 (宿主模式 loop 暂停),
    停止后由宿主模式 enter() 整页重绘恢复

MicroPython 兼容: 无 f-string / time.ticks_*
"""

import time
from machine import RTC

try:
    from config import W, H
except ImportError:
    W, H = 240, 240

from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_GREEN, CLR_RED,
)

ALARM_COUNT = 3
ALARM_RING_MS = 60000           # 响铃自动停止时长
ALARM_BEEP_GAP_MS = 1000        # 响铃间歇周期
# 响铃音量三参数 (嫌小可调):
#   频率取接近廉价无源磁式蜂鸣器共振峰 (MLT-8530 类 ~2.7kHz),
#   比原 1000/1300Hz 响度明显更高; 若为有源蜂鸣器, 音量由
#   ALARM_BEEP_DUTY 决定, 可调到 900~950。
ALARM_BEEP_HZ = (2900, 2400)    # 双音交替频率
ALARM_BEEP_MS = 150             # 单次鸣响时长
ALARM_BEEP_DUTY = 512           # PWM 占空比 (有源蜂鸣器可调高增响)
ALARM_MSG_MAX = 16              # 提醒文字上限 (2 行 x 8 字, 与 settings 一致)
ALARM_MSG_LINE = 8              # 每行字数 (24px 中文, 240px 屏宽安全排版)
NEON_GLOW = 0x03EF              # 暗青辉光 (与 net_clock 一致)

_RTC = None
_LAST_MIN = -1                 # 上次检查过的当日分钟序号 (hh*60+mm)
_FIRED_KEY = ""                 # "idx-hhmm" 同分钟防重指纹
_ARMED_SIG = ""                 # 上次打印的启用闹钟签名 (变化时打印)
_SY32 = None
_HZ24 = None


def _get_rtc():
    global _RTC
    if _RTC is None:
        _RTC = RTC()
    return _RTC


def _get_sy32():
    global _SY32
    if _SY32 is None:
        try:
            from font import sy_32x32 as f
            _SY32 = f
        except ImportError:
            _SY32 = False
    return _SY32


def _get_hz24():
    global _HZ24
    if _HZ24 is None:
        try:
            from font import hanzi_24 as f
            _HZ24 = f
        except ImportError:
            _HZ24 = False
    return _HZ24


def _get_sub_font():
    try:
        from font import vga1_8x8 as f
        return f
    except ImportError:
        return None


def _read_time():
    """(hh, mm, ss, y, mo, dy), 异常时 ss=-1 保证不会误触发"""
    try:
        dt = _get_rtc().datetime()
        return dt[4], dt[5], dt[6], dt[0], dt[1], dt[2]
    except Exception:
        return 0, 0, -1, 2026, 1, 1


def _calc_weekday(y, m, d):
    """从日期计算星期 (0=周日..6=周六, 与 Web days 语义一致)。

    不依赖 RTC 的 weekday 字段: 不同写入路径/固件对 weekday 基准
    的约定不一致 (0=周日 或 0=周一), 曾导致按星期闹钟永不匹配。
    Zeller 公式: h 0=周六..6=周五, 转为 0=周日。
    """
    if m < 3:
        m += 12
        y -= 1
    j = y // 100
    k = y % 100
    h = (d + (13 * (m + 1)) // 5 + k + k // 4 + j // 4 + 5 * j) % 7
    return (h + 6) % 7


def _write_glow(d, font, ch, x, y, fg, glow):
    """霓虹辉光字: 四方向暗色偏移光晕 + 亮色主体"""
    d.write(font, ch, x - 1, y, glow, CLR_BG)
    d.write(font, ch, x + 1, y, glow, CLR_BG)
    d.write(font, ch, x, y - 1, glow, CLR_BG)
    d.write(font, ch, x, y + 1, glow, CLR_BG)
    d.write(font, ch, x, y, fg, CLR_BG)


def _msg_lines(msg):
    """提醒文字拆成最多 2 行, 每行 ≤ALARM_MSG_LINE 字"""
    out = []
    i = 0
    n = min(len(msg), ALARM_MSG_MAX)
    while i < n and len(out) < 2:
        out.append(msg[i:i + ALARM_MSG_LINE])
        i += ALARM_MSG_LINE
    return out


def _hanzi_width(font, text):
    """按字库逐字宽度求和, 失败退化为 MAX_WIDTH * len"""
    try:
        w = 0
        for ch in text:
            w += font._WIDTHS[font.MAP.index(ch)]
        return w
    except Exception:
        return len(text) * font.MAX_WIDTH


def _ring(display, buttons, buzzer, hh, mm, msg=""):
    """阻塞式响铃接管: 红框闪烁 + ALARM/自定义文字 + 时间。

    msg 非空时以 24px 中文最多 2 行居中显示 (替换 ALARM 大字),
    否则显示默认 ALARM。任意按键停止 (返回 True) 或 60s 超时
    静默 (返回 False)。期间持续 update_last, 避免按键泄漏为
    宿主模式的幻影按键事件。
    """
    import gc
    gc.collect()
    d = display
    sy32 = _get_sy32()
    sf = _get_sub_font()

    # ---- 静态层 ----
    d.fill(CLR_BG)
    if sf:
        d.fill_rect(0, 0, W, 14, CLR_BG)
        d.text(sf, "ALARM", 6, 4, CLR_GRAY, CLR_BG)
        d.hline(0, 14, W, CLR_DIM)
    hz = _get_hz24()
    lines = _msg_lines(msg) if msg else []
    if lines and hz:
        # 自定义提醒文字: 24px 中文, 最多 2 行居中
        y = 46
        for ln in lines:
            lw = _hanzi_width(hz, ln)
            d.write(hz, ln, (W - lw) // 2, y, CLR_RED, CLR_BG)
            y += 32
    elif sy32:
        msg_txt = "ALARM"
        mw = len(msg_txt) * sy32.MAX_WIDTH
        d.write(sy32, msg_txt, (W - mw) // 2, 56, CLR_RED, CLR_BG)
    elif sf:
        d.text(sf, "ALARM", (W - 5 * 16) // 2, 64, CLR_RED, CLR_BG)
    if sy32:
        tm = "%02d:%02d" % (hh, mm)
        x0 = (W - 164) // 2
        for ch_str in tm:
            if ch_str == ':':
                d.fill_rect(x0 + 6, 120, 6, 6, CLR_CYAN)
                d.fill_rect(x0 + 6, 134, 6, 6, CLR_CYAN)
                x0 += 16 + 4
            else:
                _write_glow(d, sy32, ch_str, x0, 112, CLR_WHITE, NEON_GLOW)
                x0 += 33 + 4
    elif sf:
        d.text(sf, "%02d:%02d" % (hh, mm), (W - 5 * 16) // 2, 110,
               CLR_WHITE, CLR_BG)
    if sf:
        d.text(sf, "Press any key", (W - 13 * 8) // 2, 168, CLR_GRAY, CLR_BG)
        d.text(sf, "A:STOP", (W - 6 * 8) // 2, H - 19, CLR_GREEN, CLR_BG)

    # ---- 交互循环 (非阻塞蜂鸣 + 按键扫描) ----
    end = time.ticks_add(time.ticks_ms(), ALARM_RING_MS)
    next_beep = 0
    beep_hi = False
    blink = None
    # 丢弃响铃前的残留按键事件/边沿 (如进入瞬间正在按的键不算停止)
    buttons.clear_events()
    while True:
        now = time.ticks_ms()
        up, sel, down, _a, _b, _c = buttons.scan_timed()
        if up or sel or down:
            if buzzer:
                try:
                    buzzer.tone(600, 30)
                except Exception:
                    pass
            return True
        if time.ticks_diff(end, now) <= 0:
            return False
        # 红色警示边框闪烁 (250ms)
        on = (now // 250) % 2 == 0
        if on != blink:
            blink = on
            d.rect(6, 20, W - 12, H - 64, CLR_RED if on else CLR_DIM)
        # 间歇短鸣 (tone 仅阻塞 ALARM_BEEP_MS, 按键扫描不受影响)
        if time.ticks_diff(now, next_beep) >= 0:
            next_beep = time.ticks_add(now, ALARM_BEEP_GAP_MS)
            beep_hi = not beep_hi
            if buzzer:
                try:
                    buzzer.tone(ALARM_BEEP_HZ[1] if beep_hi else ALARM_BEEP_HZ[0],
                                ALARM_BEEP_MS, ALARM_BEEP_DUTY)
                except Exception:
                    pass
        time.sleep_ms(30)


def check(display, buttons, buzzer):
    """每帧调用 (BaseMode.run 主循环): 分钟沿检查闹钟。

    匹配按"分钟变化"触发而非 ss==0 秒沿: 天气拉取/NTP 等阻塞操作
    可能让 loop 跳过整秒, 秒沿法会永久错过闹钟 (真机复现过)。
    分钟变化时回看最多 5 分钟补漏 (短暂阻塞后醒来仍会补响)。
    到点即接管屏幕响铃 (阻塞至停止/超时), 返回 True 表示触发过,
    宿主模式应自行 enter() 整页重绘恢复。
    排障日志 (串口可见):
      [ALARM] armed wd=N: ...  启用中的闹钟列表 (签名变化时打印一次)
      [ALARM] ring ...         实际触发响铃
    """
    global _LAST_MIN, _FIRED_KEY, _ARMED_SIG
    hh, mm, ss, y, mo, dy = _read_time()
    if ss < 0:
        return False            # RTC 异常, 不误触发
    wd = _calc_weekday(y, mo, dy)
    cur = hh * 60 + mm
    if cur == _LAST_MIN:
        return False
    prev = _LAST_MIN
    _LAST_MIN = cur
    try:
        from framework import settings as st
        alarms = st.get_alarms()
    except Exception as e:
        print("[ALARM] load error:", e)
        return False
    # 启用闹钟签名 (变化时打印一次, 便于串口排查; 组号对应 Web 闹钟 1/2/3)
    armed = []

    def _days_txt(days):
        if not days:
            return "单次"
        if len(days) == 7:
            return "每天"
        if days == [1, 2, 3, 4, 5]:
            return "1-5"
        if days == [0, 6]:
            return "周末"
        return ",".join(str(d) for d in days)

    for _i in range(len(alarms)):
        _a = alarms[_i]
        if _a.get("en"):
            armed.append("AL%d %02d:%02d(%s)" % (
                _i + 1, _a.get("h", 0), _a.get("m", 0),
                _days_txt(_a.get("days", []))))
        else:
            armed.append("AL%d --" % (_i + 1))
    sig = ",".join(armed)
    if sig != _ARMED_SIG:
        _ARMED_SIG = sig
        if sig:
            print("[ALARM] armed wd=%d:" % wd, " | ".join(armed))
    # 待匹配分钟区间: 当前分钟 + 回看补漏 (跨零点/首帧不回看)
    if prev < 0 or cur <= prev:
        span = [cur]
    else:
        lo = prev + 1
        if lo < cur - 5:
            lo = cur - 5
        span = list(range(lo, cur + 1))
    i = 0
    n = len(alarms)
    while i < n:
        a = alarms[i]
        if a.get("en"):
            days = a.get("days")
            if not days or wd in days:
                ah = a.get("h", 0)
                am = a.get("m", 0)
                if ah * 60 + am in span:
                    key = "%d-%02d%02d" % (i, ah, am)
                    if key != _FIRED_KEY:
                        _FIRED_KEY = key
                        print("[ALARM] ring AL%d %02d:%02d msg=%s" % (
                            i + 1, ah, am, a.get("msg", "")))
                        if not days:
                            # 单次闹钟: 触发后自动关闭并落盘
                            a["en"] = False
                            try:
                                st.update({"alarms": alarms})
                            except Exception:
                                pass
                        _ring(display, buttons, buzzer, ah, am,
                              str(a.get("msg", "") or ""))
                        return True
        i += 1
    return False

"""
XianMuBox - 系统启动入口（由 build.py 自动生成）
闲沐工坊 · 插件式架构

职责：硬件初始化 + 开机动画（NTP 同步后固定修正为北京时间 UTC+8），
完成后正常返回（应用主循环在 main.py）。
"""

import gc

from framework.hal import init_display, Buzzer, Buttons
from config import BUZZER_PIN

# ===== 1. 硬件初始化 =====
display = init_display()
buzzer = Buzzer(BUZZER_PIN)
buttons = Buttons()
gc.collect()

# ===== 2. 开机动画 =====
# tz_apply: 开机动画 NTP 同步成功后回调，把 RTC 从 UTC 修正为北京时间。
# RTC 无掉电保持，这次修正若失败，由网络时钟模式的周期重同步兜底。


def _apply_timezone():
    try:
        import time as _time
        t = _time.time() + 8 * 3600
        tm = _time.localtime(t)
        from machine import RTC
        # RTC.datetime: (year, month, day, weekday, hours, mins, secs, subsecs)
        # localtime weekday: Monday=0; RTC weekday: Sunday=0 -> (tm[6]+1)%7
        RTC().datetime((tm[0], tm[1], tm[2], (tm[6] + 1) % 7,
                        tm[3], tm[4], tm[5], 0))
    except Exception as e:
        print("[BOOT] Timezone adjust error:", e)


from boot_animations.matrix import play as play_boot
play_boot(display, buzzer, tz_apply=_apply_timezone)

gc.collect()
# 正常返回：固件随后初始化 USB CDC，并执行 main.py 进入应用主循环

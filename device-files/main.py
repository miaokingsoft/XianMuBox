"""
XianMuBox - 应用主循环（由 build.py 自动生成）
闲沐工坊 · 插件式架构

从 boot.py 接手硬件并进入主循环。
（MicroPython v1.27+ 要求 boot.py 尽快返回以激活 USB CDC，主循环移至此处）
"""

import gc

from framework.hal import init_display, Buzzer, Buttons
from config import BUZZER_PIN

# 重新装配硬件（boot.py 中的对象随其作用域释放）
display = init_display()
buzzer = Buzzer(BUZZER_PIN)
buttons = Buttons()

from menus.list import run_menu
from modes.registry import dispatch

gc.collect()

# ===== SD 卡挂载（失败静默跳过，不影响任何既有功能）=====
try:
    from framework.sdcard import mount as sd_mount
    sd_mount()
    gc.collect()
except Exception as e:
    print("[BOOT] sdcard mount error:", e)

# ===== 开机配网引导 =====
# WiFi 未连接（新设备 / 密码失效）时进入 SoftAP 配网页，C 键可跳过
# 进离线菜单；已连接则原流程零改动。
try:
    from framework.provision import boot_provision_check
    boot_provision_check(display, buttons, buzzer)
    gc.collect()
except Exception as e:
    print("[BOOT] provision check error:", e)

# ===== 主循环 =====
while True:
    try:
        mode_id = run_menu(display, buttons, buzzer)
        print("[BOOT] Selected:", mode_id)
        if mode_id:
            dispatch(display, buttons, buzzer, mode_id)
            gc.collect()
    except KeyboardInterrupt:
        print("[BOOT] Interrupted")
        break
    except Exception as e:
        print("[BOOT] Error:", e)
        display.fill(0x0000)
        import time
        time.sleep_ms(2000)

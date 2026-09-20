"""
XianMuBox - 模式注册表与调度（由 build.py 自动生成）
"""

_MODULE_MAP = {
    "net_clock": ("modes.net_clock", "NetClockMode"),
    "swipe": ("modes.swipe", "SwipeMode"),
    "self_media": ("modes.self_media", "SelfMediaMode"),
    "dashboard": ("modes.dashboard", "DashboardMode"),
    "ai_balance": ("modes.ai_balance", "AIBalanceMode"),
    "stock": ("modes.stock", "StockMode"),
    "lottery": ("modes.lottery", "LotteryMode"),
    "news": ("modes.news", "NewsMode"),
    "radio": ("modes.radio", "RadioMode"),
    "story": ("modes.story", "StoryMode"),
    "morse_code": ("modes.morse_code", "MorseCodeMode"),
    "vocab": ("modes.vocab", "VocabMode"),
    "settings": ("modes.settings", "SettingsMode"),
}


def get_available_ids():
    """返回所有已注册的 MODE_ID 列表"""
    return list(_MODULE_MAP.keys())


def dispatch(display, buttons, buzzer, mode_id):
    """
    调度到指定模式。
    异常保护：单个模式崩溃不扩散，错误信息打印到 REPL。
    """
    entry = _MODULE_MAP.get(mode_id)
    if entry is None:
        print("[DISPATCH] Unknown mode_id:", mode_id)
        return

    path, cls_name = entry
    try:
        import gc as _gc
        _gc.collect()
        print("[DISPATCH] Loading %s, free: %d" % (path, _gc.mem_free()))

        parts = path.split(".")
        mod = __import__(path)
        for part in parts[1:]:
            mod = getattr(mod, part)

        cls = getattr(mod, cls_name)
        mode = cls(display, buttons, buzzer)
        print("[DISPATCH] Running %s, free: %d" % (cls_name, _gc.mem_free()))
        mode.run()
    except Exception as e:
        print("[DISPATCH] Mode '%s' crashed:" % mode_id, e)
        try:
            import sys
            sys.print_exception(e)
            display.fill(0x0000)
            try:
                from font import vga1_8x8 as FONT
                display.text(FONT, "MODE ERROR", 60, 110, 0xF800, 0x0000)
            except Exception:
                pass
        except Exception:
            pass
        import time
        time.sleep_ms(2000)

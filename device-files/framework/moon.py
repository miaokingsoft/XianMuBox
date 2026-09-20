"""
多功能终端框架 - 月相计算
闲沐工坊 · 天文工具

提供基于日期的月相计算 (新月/上弦月/满月/下弦月)
不需要联网, 纯本地计算

算法参考: Conway's approximation (simplified)
精确度约 1-2 天, 对UI显示足够
"""

import math
import time

# 月相名称 (中文)
_PHASE_NAMES = (
    "新月",    # 0
    "蛾眉月",  # 1
    "上弦月",  # 2
    "盈凸月",  # 3
    "满月",    # 4
    "亏凸月",  # 5
    "下弦月",  # 6
    "残月",    # 7
)


def moon_phase(year=None, month=None, day=None):
    """
    返回月相信息

    参数: 年/月/日 (默认当前RTC时间)

    返回值:
      {
        "phase": 0-7,           # 月相索引 (0=新月, 4=满月)
        "name": "新月",          # 中文名
        "age": 0.0-29.5,        # 月龄(天)
        "illumination": 0-1.0,  # 月面照亮比例
        "is_waxing": True/False # 是否盈月
      }
    """
    if year is None or month is None or day is None:
        try:
            from machine import RTC
            rtc = RTC()
            dt = rtc.datetime()
            year, month, day = dt[0], dt[1], dt[2]
        except Exception:
            year, month, day = 2026, 1, 1

    # 已知新月日期作为参考: 2000-01-06 18:14 UTC
    # 朔望月长度 ~ 29.530588853 天
    if year < 2000:
        ref_year = 2000
        diff_days = (year - ref_year) * 365.25
    else:
        ref_year = 2000
        ref_month = 1
        ref_day = 6
        # 简化的日期差 (忽略闰秒等细节)
        diff_days = _days_since_2000(year, month, day) - _days_since_2000(ref_year, ref_month, ref_day)

    SYNODIC = 29.530588853
    cycles = diff_days / SYNODIC
    age = (cycles - math.floor(cycles)) * SYNODIC  # 月龄 0-29.53

    # 月相索引 (8等分)
    phase_idx = int((age / SYNODIC) * 8 + 0.5) % 8

    # 照亮比例 (粗略)
    illumination = (1 - math.cos(2 * math.pi * age / SYNODIC)) / 2

    return {
        "phase": phase_idx,
        "name": _PHASE_NAMES[phase_idx],
        "age": age,
        "illumination": illumination,
        "is_waxing": age < SYNODIC / 2,
    }


def _days_since_2000(year, month, day):
    """简化版: 计算自 2000-01-01 起的天数"""
    if month <= 2:
        year -= 1
        month += 12
    # 儒略日公式 (简化)
    a = year // 100
    b = 2 - a + a // 400
    jd = int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + b - 1524
    # 2000-01-01 的儒略日 = 2451545
    return jd - 2451545
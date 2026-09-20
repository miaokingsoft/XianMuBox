"""
多功能终端框架 - 开机画面 Matrix Edition
闲沐工坊 · st7789_mpy C 驱动 · 黑客帝国雨 + 进度条

接口：play(display, buzzer, preload_fn=None, tz_apply=None)
  display     已初始化的 ST7789 对象（由 framework/hal.py 创建）
  buzzer      Buzzer 实例（由 framework/hal.py 创建）
  preload_fn  可选回调，阶段5中调用（用于预加载菜单模块等）
  tz_apply    可选回调，阶段4b NTP 成功后调用（应用时区偏移）

动画流程 (~5s + WiFi + NTP):
  1. 蜂鸣 + 背光渐入
  2. Matrix 数字雨 (纯雨景)
  3. 雨中浮现 "闲沐工坊" + "XianMu Work"
  4. 标题框3色轮转展示
  5. WiFi 连接 (进度条 0-70%)
  6. NTP 同步 + 时区 (进度条 70-90%)
  7. 系统就绪 + 预加载 (进度条 90-100%)
  8. 快速过渡到菜单 (无黑屏)
"""

import time, gc, random
from machine import Pin, PWM

try:
    from font import vga1_8x8 as SUB_FONT
except ImportError:
    SUB_FONT = None

from font import hanzi32
from config import W, H, TFT_BLK
from framework.colors import (
    CLR_BG as BLACK,
    CLR_WHITE as WHITE,
    CLR_GREEN as GREEN,
    CLR_CYAN as CYAN,
    CLR_MAGENTA as MAGENTA,
    CLR_RED as RED,
)
from framework.wifi import connect_wifi, WiFiConnector

# ===== Matrix 专属配色（不导入全局调色板） =====
DIM_G   = 0x03E0   # 暗绿（雨滴尾迹中段）
DARK_G  = 0x0180   # 深暗绿（雨滴尾迹末端）

# -- 赛博边框色 --
CYBER_BORDER = 0x07FF   # 青色主边框
CYBER_CORNER = 0xF81F   # 品红角标
CYBER_GLOW   = 0x03FF   # 暗青辉光

# -- 赛博边框色轮转表 --
_BORDER_COLORS = (CYBER_BORDER, CYAN, GREEN, MAGENTA)

# -- 标题框背景渐变色 (3色循环) --
BG_COLORS = (
    0xF16F,  # 日落珊瑚
    0xE2EF,  # 熔岩紫红
    0xFF80,  # 纯金黄
)

# -- 标题文字前景色 (与背景一一对应) --
TITLE_TEXT_COLORS = (
    WHITE,   # 配日落珊瑚
    WHITE,   # 配熔岩紫红
    BLACK,   # 配纯金黄
)

# -- 进度条分段 (WiFi 0-70%, NTP 70-90%, 就绪 90-100%) --
PROG_WIFI_END = 70
PROG_NTP_END = 90
PROG_READY_END = 100

# -- Matrix 雨滴配置 --
CHAR_W = 8
CHAR_H = 8
COLS = W // CHAR_W
ROWS = H // CHAR_H
_SPEED_POOL = (1, 1, 1, 2)
_SPEED_POOL2 = (1, 1, 2)

# -- 中文字模配置 --
CN_SIZE = hanzi32.HEIGHT
TITLE = "闲沐工坊"
TITLE_CHAR_W = CN_SIZE
TITLE_W = len(TITLE) * TITLE_CHAR_W
TITLE_H = CN_SIZE
TITLE_X = (W - TITLE_W) // 2
TITLE_Y = H // 2 - 36

# -- 标题赛博边框区域 --
TB_PAD = 10
TITLE_BOX_X = TITLE_X - TB_PAD
TITLE_BOX_Y = TITLE_Y - TB_PAD
TITLE_BOX_W = TITLE_W + TB_PAD * 2
TITLE_BOX_H = TITLE_H + TB_PAD * 2

# -- 布局关键 Y 坐标 --
LINE_Y    = TITLE_Y + TITLE_H + 16
SUB_Y     = LINE_Y + 10
BAR_Y     = H - 30
BAR_LABEL_Y = BAR_Y - 14
WIFI_LABEL_Y = BAR_Y - 20
READY_Y   = BAR_Y + 12

# -- 中央面板区域 --
PANEL_X = 20
PANEL_Y = TITLE_Y - 10
PANEL_W = W - 40
PANEL_H = READY_Y - PANEL_Y + 20

# -- 雨滴被面板遮挡的列范围 --
MASK_COL_START = PANEL_X // CHAR_W
MASK_COL_END = (PANEL_X + PANEL_W) // CHAR_W
MASK_ROW_START = PANEL_Y // CHAR_H
MASK_ROW_END = (PANEL_Y + PANEL_H) // CHAR_H + 1


# ===================== 雨滴状态管理 =====================

def _init_drops():
    drops = []
    for _ in range(COLS):
        drops.append([
            random.randint(-20, -1),
            random.choice(_SPEED_POOL),
            random.randint(4, 10),
            random.random() < 0.6,
        ])
    return drops


def _step_rain(display, drops, mask=False):
    for col_idx in range(COLS):
        row, speed, trail, active = drops[col_idx]
        if not active:
            if random.random() < 0.05:
                drops[col_idx][0] = 0
                drops[col_idx][1] = random.choice(_SPEED_POOL2)
                drops[col_idx][2] = random.randint(4, 10)
                drops[col_idx][3] = True
            continue

        cx = col_idx * CHAR_W
        in_mask_col = mask and (MASK_COL_START <= col_idx < MASK_COL_END)

        if 0 <= row < ROWS:
            if not (in_mask_col and MASK_ROW_START <= row < MASK_ROW_END):
                cy = row * CHAR_H
                display.fill_rect(cx, cy, CHAR_W, CHAR_H, GREEN)
                display.pixel(cx + 3, cy + 3, WHITE)

        for t in range(1, trail + 1):
            tr = row - t
            if 0 <= tr < ROWS:
                if not (in_mask_col and MASK_ROW_START <= tr < MASK_ROW_END):
                    ty = tr * CHAR_H
                    if t <= trail // 2:
                        display.fill_rect(cx, ty, CHAR_W, CHAR_H, DIM_G)
                    else:
                        display.fill_rect(cx, ty, CHAR_W, CHAR_H, DARK_G)

        erase_row = row - trail - 1
        if 0 <= erase_row < ROWS:
            if not (in_mask_col and MASK_ROW_START <= erase_row < MASK_ROW_END):
                ey = erase_row * CHAR_H
                display.fill_rect(cx, ey, CHAR_W, CHAR_H, BLACK)

        row += speed
        drops[col_idx][0] = row

        if row - trail > ROWS:
            drops[col_idx][0] = random.randint(-5, -1)
            drops[col_idx][1] = random.choice(_SPEED_POOL)
            drops[col_idx][2] = random.randint(4, 10)
            drops[col_idx][3] = random.random() < 0.7


# ===================== 赛博边框 =====================

def _draw_cyber_border(display, x, y, w, h, bg_color=BLACK, border_idx=0):
    main_c = _BORDER_COLORS[border_idx % 4]
    corner_c = CYBER_CORNER if border_idx % 2 == 0 else CYAN

    display.fill_rect(x, y, w, h, bg_color)
    display.rect(x, y, w, h, main_c)
    display.rect(x - 1, y - 1, w + 2, h + 2, CYBER_GLOW)

    arm = 8
    display.hline(x, y, arm, corner_c)
    display.vline(x, y, arm, corner_c)
    display.hline(x + w - arm, y, arm, corner_c)
    display.vline(x + w - 1, y, arm, corner_c)
    display.hline(x, y + h - 1, arm, corner_c)
    display.vline(x, y + h - arm, arm, corner_c)
    display.hline(x + w - arm, y + h - 1, arm, corner_c)
    display.vline(x + w - 1, y + h - arm, arm, corner_c)

    mid_x = x + w // 2
    mid_y = y + h // 2
    display.fill_rect(mid_x - 3, y - 1, 6, 3, corner_c)
    display.fill_rect(mid_x - 3, y + h - 2, 6, 3, corner_c)
    display.fill_rect(x - 1, mid_y - 3, 3, 6, corner_c)
    display.fill_rect(x + w - 2, mid_y - 3, 3, 6, corner_c)


# ===================== 进度条 =====================

def _draw_progress_bar(display, x, y, w, h, pct, border_color, fill_color):
    display.rect(x, y, w, h, border_color)
    if pct > 0 and w > 4 and h > 2:
        fill_w = max(1, (w - 4) * pct // 100)
        display.fill_rect(x + 2, y + 1, fill_w, h - 2, fill_color)


# ===================== 开机动画主入口 =====================

def play(display, buzzer, preload_fn=None, tz_apply=None):
    """
    播放 Matrix 风格开机动画。

    参数由 boot.py 传入（即 framework/hal.py 创建的 display 和 buzzer）。

    preload_fn: 可选回调，阶段5中调用（预加载菜单模块等）
    tz_apply:   可选回调，NTP 同步成功后调用（应用时区偏移）

    动画流程 (~5s + WiFi + NTP):
      1. 蜂鸣 + 背光渐入
      2. 纯 Matrix 雨 (25帧)
      3. 雨中浮现标题 + 副标题
      4. WiFi 连接 (进度条 0-70%)
      4b. NTP 同步 + 时区 (进度条 70-90%)
      5. 系统就绪 + 预加载 (进度条 90-100%)
      6. 快速过渡到菜单 (无黑屏)
    """
    # ---- 初始化背光 ----
    blk = PWM(Pin(TFT_BLK), freq=1000, duty=0)

    def bright(d):
        blk.duty(max(0, min(1023, int(d))))

    drops = _init_drops()

    # ===== 阶段 1: 蜂鸣 + 背光渐入 =====
    display.fill(BLACK)
    buzzer.beep(30)
    for d in range(0, 1024, 25):
        bright(d)
        time.sleep_ms(6)
    bright(1023)
    time.sleep_ms(50)

    # ===== 阶段 2: 纯 Matrix 雨 (25帧 ~0.75s) =====
    for _ in range(25):
        _step_rain(display, drops, mask=False)
        time.sleep_ms(30)
    gc.collect()

    # ===== 阶段 3: 雨中浮现标题 =====
    display.fill_rect(PANEL_X, PANEL_Y, PANEL_W, PANEL_H, BLACK)

    bg_idx = 0
    border_idx = 0
    _draw_cyber_border(display, TITLE_BOX_X, TITLE_BOX_Y,
                       TITLE_BOX_W, TITLE_BOX_H,
                       BG_COLORS[bg_idx], border_idx)

    display.write(hanzi32, TITLE, TITLE_X, TITLE_Y, GREEN, BG_COLORS[bg_idx])
    buzzer.tone(600, 20)
    time.sleep_ms(150)

    for i, ch in enumerate(TITLE):
        _step_rain(display, drops, mask=True)
        cx = TITLE_X + i * TITLE_CHAR_W
        display.fill_rect(cx, TITLE_Y, TITLE_CHAR_W, TITLE_H, BG_COLORS[bg_idx])
        display.write(hanzi32, ch, cx, TITLE_Y,
                      TITLE_TEXT_COLORS[bg_idx], BG_COLORS[bg_idx])
        buzzer.tone(800 + i * 200, 15)
        time.sleep_ms(60)

    for cycle in range(3):
        bg_idx = (bg_idx + 1) % len(BG_COLORS)
        border_idx += 1
        _step_rain(display, drops, mask=True)
        _draw_cyber_border(display, TITLE_BOX_X, TITLE_BOX_Y,
                           TITLE_BOX_W, TITLE_BOX_H,
                           BG_COLORS[bg_idx], border_idx)
        display.write(hanzi32, TITLE, TITLE_X, TITLE_Y,
                      TITLE_TEXT_COLORS[bg_idx], BG_COLORS[bg_idx])
        buzzer.tone(600 + cycle * 100, 15)
        time.sleep_ms(200)

    line_w = 120
    line_x = (W - line_w) // 2
    display.hline(line_x, LINE_Y, line_w, MAGENTA)
    display.hline(line_x - 14, LINE_Y, 13, DIM_G)
    display.hline(line_x + line_w + 1, LINE_Y, 13, DIM_G)

    sub_text = "XianMu Work"
    if SUB_FONT:
        tw = len(sub_text) * 8
        display.text(SUB_FONT, sub_text,
                     (W - tw) // 2, SUB_Y, GREEN, BLACK)
    else:
        display.hline((W - 80) // 2, SUB_Y + 4, 80, GREEN)

    for _ in range(2):
        _step_rain(display, drops, mask=True)
        time.sleep_ms(30)

    # ===== 阶段 4: WiFi 连接 + 进度条动画 (0-70%) =====
    bar_w = 160
    bar_h = 6
    bar_x = (W - bar_w) // 2

    display.fill_rect(0, WIFI_LABEL_Y - 2, W, 14, BLACK)

    _wf_ok = False
    _wf_ip = None

    if True:  # use wifi_config (always present)
        if SUB_FONT:
            t = "WiFi: Connecting..."
            display.text(SUB_FONT, t, (W - len(t) * 8) // 2,
                         WIFI_LABEL_Y, DIM_G, BLACK)

        connector = WiFiConnector()
        connector.start()
        last_ssid = None

        while connector.state == 'connecting':
            _step_rain(display, drops, mask=True)

            # Update SSID display when SSID changes
            ssid = connector.current_ssid
            if ssid != last_ssid:
                display.fill_rect(0, WIFI_LABEL_Y - 2, W, 14, BLACK)
                if SUB_FONT:
                    t = ("WiFi: " + ssid) if ssid else "WiFi: Connecting..."
                    display.text(SUB_FONT, t, (W - len(t) * 8) // 2,
                                 WIFI_LABEL_Y, GREEN, BLACK)
                last_ssid = ssid

            # Update progress bar (0-70% range for WiFi)
            pct = connector.progress * PROG_WIFI_END // 100
            if bar_w > 4 and bar_h > 2:
                display.fill_rect(bar_x + 2, BAR_Y + 1, bar_w - 4, bar_h - 2, BLACK)
            _draw_progress_bar(display, bar_x, BAR_Y, bar_w, bar_h,
                               pct, GREEN, CYAN)

            connector.tick()
            time.sleep_ms(30)

        # Show final result
        _step_rain(display, drops, mask=True)
        display.fill_rect(0, WIFI_LABEL_Y - 2, W, 14, BLACK)

        _wf_ok, _wf_ip = connector.result

        if SUB_FONT:
            if _wf_ok:
                t = "OK! IP:" + _wf_ip
                display.text(SUB_FONT, t, (W - len(t) * 8) // 2,
                             WIFI_LABEL_Y, GREEN, BLACK)
                buzzer.tone(1000, 20)
            else:
                t = "WiFi: Failed"
                display.text(SUB_FONT, t, (W - len(t) * 8) // 2,
                             WIFI_LABEL_Y, RED, BLACK)
                buzzer.tone(300, 40)

        # Final WiFi progress bar state (70%)
        bar_color = GREEN if _wf_ok else RED
        if bar_w > 4 and bar_h > 2:
            display.fill_rect(bar_x + 2, BAR_Y + 1, bar_w - 4, bar_h - 2, BLACK)
        _draw_progress_bar(display, bar_x, BAR_Y, bar_w, bar_h,
                           PROG_WIFI_END, bar_color, bar_color)

        time.sleep_ms(600)

    buzzer.tone(1800, 30)
    time.sleep_ms(30)
    buzzer.tone(2400, 30)

    # ===== 阶段 4b: NTP 同步 + 时区 (70-90%) =====
    ntp_ok = False
    if _wf_ok:
        if SUB_FONT:
            display.fill_rect(0, WIFI_LABEL_Y - 2, W, 14, BLACK)
            t = "NTP: Sync..."
            display.text(SUB_FONT, t, (W - len(t) * 8) // 2,
                         WIFI_LABEL_Y, CYAN, BLACK)

        # NTP 前先播一段雨动画 + 进度条缓进 (70%→85%)
        # 这样用户看到进度在走，而非停顿等待
        ntp_pre_frames = 20
        for fi in range(ntp_pre_frames):
            _step_rain(display, drops, mask=True)
            pct = PROG_WIFI_END + int(15 * (fi + 1) / ntp_pre_frames)
            if bar_w > 4 and bar_h > 2:
                display.fill_rect(bar_x + 2, BAR_Y + 1, bar_w - 4, bar_h - 2, BLACK)
            _draw_progress_bar(display, bar_x, BAR_Y, bar_w, bar_h,
                               pct, GREEN, CYAN)
            time.sleep_ms(30)

        # 尝试 NTP 同步 (最多 3 次: WiFi 刚就绪时 DNS/UDP 可能瞬时失败,
        # RTC 无掉电保持, 开机这次失败又无兜底会导致整个开机时间错误)
        try:
            import ntptime
            ntptime.host = "ntp.aliyun.com"  # 平台文档建议, 国内可达性
            for _try in range(3):
                try:
                    ntptime.settime()  # 阻塞调用，但进度条已到 85%，用户感知在推进中
                    ntp_ok = True
                    break
                except Exception as e:
                    print("[BOOT] NTP sync try %d failed:" % (_try + 1), e)
                    time.sleep_ms(500)
        except Exception as e:
            print("[BOOT] NTP module error:", e)

        # 应用时区 (NTP 成功后立即修正 RTC)
        if ntp_ok and tz_apply:
            try:
                tz_apply()
            except Exception as e:
                print("[BOOT] Timezone apply error in animation:", e)

        # NTP 完成后补播雨动画 + 进度条推到 90% (5帧过渡)
        for fi in range(5):
            _step_rain(display, drops, mask=True)
            pct = 85 + int((PROG_NTP_END - 85) * (fi + 1) / 5)
            if bar_w > 4 and bar_h > 2:
                display.fill_rect(bar_x + 2, BAR_Y + 1, bar_w - 4, bar_h - 2, BLACK)
            bar_c = GREEN if ntp_ok else MAGENTA
            _draw_progress_bar(display, bar_x, BAR_Y, bar_w, bar_h,
                               pct, bar_c, bar_c)
            time.sleep_ms(30)

        # 显示 NTP 结果
        if SUB_FONT:
            display.fill_rect(0, WIFI_LABEL_Y - 2, W, 14, BLACK)
            t = "NTP: OK" if ntp_ok else "NTP: Skip"
            ntp_color = GREEN if ntp_ok else MAGENTA
            display.text(SUB_FONT, t, (W - len(t) * 8) // 2,
                         WIFI_LABEL_Y, ntp_color, BLACK)

        # 进度条到 90%
        if bar_w > 4 and bar_h > 2:
            display.fill_rect(bar_x + 2, BAR_Y + 1, bar_w - 4, bar_h - 2, BLACK)
        bar_c = GREEN if ntp_ok else MAGENTA
        _draw_progress_bar(display, bar_x, BAR_Y, bar_w, bar_h,
                           PROG_NTP_END, bar_c, bar_c)

        time.sleep_ms(400)
    else:
        # WiFi 未连接，跳过 NTP，进度条直接到 90%
        if SUB_FONT:
            display.fill_rect(0, WIFI_LABEL_Y - 2, W, 14, BLACK)
            display.text(SUB_FONT, "NTP: Skip", (W - len("NTP: Skip") * 8) // 2,
                         WIFI_LABEL_Y, MAGENTA, BLACK)
        if bar_w > 4 and bar_h > 2:
            display.fill_rect(bar_x + 2, BAR_Y + 1, bar_w - 4, bar_h - 2, BLACK)
        _draw_progress_bar(display, bar_x, BAR_Y, bar_w, bar_h,
                           PROG_NTP_END, MAGENTA, MAGENTA)
        time.sleep_ms(300)

    # ===== 阶段 5: 系统就绪 + 预加载 (90-100%) =====
    if SUB_FONT:
        display.fill_rect(0, WIFI_LABEL_Y - 2, W, 14, BLACK)
        display.text(SUB_FONT, "Ready!", (W - len("Ready!") * 8) // 2,
                     WIFI_LABEL_Y, WHITE, BLACK)

    # 预加载（回调由 boot.py 注入，预导入菜单模块等）
    if preload_fn:
        try:
            preload_fn()
        except Exception as e:
            print("[BOOT] Preload error:", e)

    # 进度条推到 100% + 雨动画
    for i in range(10):
        _step_rain(display, drops, mask=True)
        pct = PROG_NTP_END + int((PROG_READY_END - PROG_NTP_END) * (i + 1) / 10)
        if bar_w > 4 and bar_h > 2:
            display.fill_rect(bar_x + 2, BAR_Y + 1, bar_w - 4, bar_h - 2, BLACK)
        _draw_progress_bar(display, bar_x, BAR_Y, bar_w, bar_h,
                           pct, GREEN, CYAN)
        time.sleep_ms(30)

    # ===== 阶段 6: 快速过渡到菜单 (无黑屏) =====
    # 优化：先播放旋律（屏幕仍显示 Ready! 画面），最后清屏，
    # 使清屏到菜单渲染的黑屏间隔最短（<50ms）
    drops = None
    gc.collect()

    # 在 Ready! 画面下播放完成旋律，用户不感知黑屏
    buzzer.melody([
        (523, 45, 25),
        (784, 45, 25),
        (1046, 45, 25),
        (1318, 70, 0),
    ])

    gc.collect()
    print("[BOOT] 闲沐工坊 Matrix Edition - Ready!")
    print("[BOOT] free:", gc.mem_free())

    # 清屏作为最后一步 → 紧接着 boot.py 的菜单渲染，黑屏间隔极短
    # 不做背光淡出，直接清屏为菜单背景色 → 背光常亮 → 用户零黑屏感知
    display.fill(BLACK)

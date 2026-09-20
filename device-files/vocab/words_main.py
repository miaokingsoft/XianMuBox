"""
背单词模式 - 移植自源项目 lib/words/main.py

UI 1:1 复刻源项目（黄色标题栏 + 图标 + 绿色释义 + 英文单词 + 页码 + 底部按钮 + 自动播放进度条）。
唯一改动：源 4 按键 (A前翻 / B后翻 / C自动 / D退出) -> 本项目 3 按键 (A翻页 / B自动 / C退出)。
  A 翻页：短按下一个（仅前进，到末尾循环回开头）
  B 自动：短按切换自动播放开/关（自动播放进度条走完约 12 秒后随机跳下一个）
  C 退出：短按退出回主菜单
自动播放期间：A 忽略、B 关闭自动、C 退出（与源项目一致）。

适配说明：
  - 源 IRQ 回调置标志 -> 本项目 buttons.scan() 轮询短按边沿
  - 源阻塞内循环进度条 -> 非阻塞 ticks 进度条（保持按键响应）
  - 源硬编码 982 -> len(DanCi) 动态；源 random.randrange(0,900) bug 已修为 (0, len)
  - 英文单词字体 italict（font 包内，italict 为 INDEX/FONT 位图格式）；
    源矢量 draw 路径保留，真机 draw 异常时自动永久降级 text 位图路径
  - 中文字体 hanzi1k -> hanzi_24 全量字库（接口 MAP/WIDTHS/HEIGHT 兼容，v1.8.x）
  - 各字体导入带 hasattr FONT 校验，防字体模块损坏致驱动 KeyError: FONT
"""

import time
import random
import gc

from font import hanzi_24 as font
from vocab.words_data import DanCi
from framework.colors import (
    CLR_WHITE,
    CLR_VOCAB_HDR,
    CLR_VOCAB_BTN_ACTIVE,
    CLR_VOCAB_DEF,
    CLR_VOCAB_COUNTER,
    CLR_VOCAB_PROGBG,
)


def _load_bitmap_font(*names):
    """按序尝试导入 font/ 位图字体，返回第一个具备 FONT 导出的模块（全失败返回 None）。

    只判 ImportError 防不住"模块存在但缺 FONT 导出"（驱动 text() 会 KeyError），
    故再校验 hasattr(mod, 'FONT')。
    """
    for name in names:
        try:
            mod = getattr(__import__('font.' + name), name)
            if hasattr(mod, 'FONT'):
                return mod
        except ImportError:
            continue
    return None


# ----- 英文单词字体：优先 italict，缺失/损坏则按 16x32 -> 8x16 -> 8x8 降级 -----
# 位图降级统一置于进度条下方 (y=56)，避免与 y=51..55 的自动播放进度条重叠
_USE_VECTOR_EN = False
font_En = None
try:
    from font import italict as _italict
    if hasattr(_italict, 'FONT'):
        font_En = _italict
        _USE_VECTOR_EN = True
except ImportError:
    pass
if font_En is None:
    font_En = _load_bitmap_font('vga1_16x32', 'vga1_8x16', 'vga1_8x8')

# ----- 页码字体：位图字体（用于 "当前 / 总数"） -----
_vga_counter = _load_bitmap_font('vga1_8x16', 'vga1_16x16', 'vga1_8x8')

gc.collect()

# ----- 布局常量（与源项目一致，240x240） -----
HEADER_H = 50
CONTENT_Y = 51
CONTENT_H = 154
BTN_Y = 206
BTN_H = 32
# 3 按钮布局：x=4/83/162, w=74（4+74+5+74+5+74+4 = 240）
BTN_X = (4, 83, 162)
BTN_W = 74
AUTO_MS = 12000             # 自动播放每词停留约 12 秒（源 240*50ms）
_A_LABEL = '翻页'           # hanzi_24 为全量字库，无需再按字库覆盖回退 '后翻'


def _line_auto(font_, word_, w_, h_, x_, y_):
    """移植自源 tft_config.LineAuto：按字体实际宽度自动换行，返回 [(x, y, seg), ...]"""
    worddata = []
    char_hight = font_.HEIGHT + 6
    x, y = x_, y_
    char_ = ''
    for char in word_:
        char_width = font_.WIDTHS[font_.MAP.index(char)] if char in font_.MAP else 0
        x += char_width + 2
        char_ += char
        if x > w_:
            x = 0
            worddata.append((x_, y, char_))
            y += char_hight
            char_ = ''
        if y > y_ + h_ - char_hight:
            break
    if char_ != '':
        worddata.append((x_, y, char_))
    return worddata


class Words:
    """背单词主逻辑。生命周期：enter() -> loop()（返回 False 退出）-> exit()。"""

    def __init__(self, display, buttons, buzzer=None):
        self.tft = display
        self.buttons = buttons
        self.buzzer = buzzer
        self.abc = DanCi
        self._total = len(DanCi)
        self.id = random.randrange(0, self._total)
        self._mode = "manual"         # "manual" | "auto"
        self._auto_start_ms = 0
        self._auto_progress_px = -1   # 上次绘制的进度像素，避免重复绘制
        self._frames = 0              # 帧计数（gc 节流用）

    # ---------- 蜂鸣反馈 ----------
    def _beep(self, freq=900, ms=15):
        if self.buzzer:
            try:
                self.buzzer.tone(freq, ms)
            except Exception:
                pass

    # ---------- UI 绘制（1:1 复刻源项目）----------
    def HomeUi(self, title):
        tft = self.tft
        tft.fill(0)
        # 顶部标题栏
        tft.fill_rect(0, 0, 240, HEADER_H, CLR_VOCAB_HDR)
        # 标题
        tft.write(font, title, 10, 14, CLR_WHITE, CLR_VOCAB_HDR)
        # 标题栏图标（缺失则跳过，不崩）
        try:
            tft.png("/pic/words_0.png", 200, 15, True)
        except Exception:
            pass
        self.ButA()
        self.ButB()
        self.ButC()

    def _draw_btn(self, idx, label, isok=False):
        bg_ = CLR_VOCAB_BTN_ACTIVE if isok else CLR_WHITE
        tft = self.tft
        tft.fill_rect(BTN_X[idx], BTN_Y, BTN_W, BTN_H, bg_)
        # 标签居中：write_len 实测宽度水平居中（不可用时按 MAX_WIDTH 估算），垂直按字高居中
        try:
            w = tft.write_len(font, label)
        except Exception:
            w = len(label) * getattr(font, 'MAX_WIDTH', 24)
        x = BTN_X[idx] + max(0, (BTN_W - w) // 2)
        y = BTN_Y + max(0, (BTN_H - font.HEIGHT) // 2)
        tft.write(font, label, x, y, 0, bg_)

    def ButA(self, isok=False):
        self._draw_btn(0, _A_LABEL, isok)

    def ButB(self, isok=False):
        self._draw_btn(1, '自动', isok)

    def ButC(self, isok=False):
        self._draw_btn(2, '退出', isok)

    def MainUi(self):
        tft = self.tft
        # 内容区
        tft.fill_rect(0, CONTENT_Y, 240, CONTENT_H, 0)
        # 页码：当前 / 总数（与源布局一致：当前 x=110, '/' x=160, 总数 x=180）
        if _vga_counter is not None:
            tft.text(_vga_counter, str(self.id + 1), 110, 178, CLR_VOCAB_COUNTER, 0)
            tft.text(_vga_counter, '/', 160, 178, CLR_VOCAB_COUNTER, 0)
            tft.text(_vga_counter, str(self._total), 180, 178, CLR_VOCAB_COUNTER, 0)
        # 英文单词
        en_word = self.abc[self.id][0]
        if font_En is None:
            pass
        elif _USE_VECTOR_EN and hasattr(tft, 'draw'):
            # 矢量字体 draw：坐标为文字左下角，与源一致 (10, 76)。
            # 真机驱动 draw 与字体格式不兼容时（抛异常）永久降级位图 text 路径
            try:
                tft.draw(font_En, en_word, 10, 76, CLR_WHITE)
            except Exception:
                globals()['_USE_VECTOR_EN'] = False
                tft.text(font_En, en_word, 10, 56, CLR_WHITE, 0)
        else:
            # 位图字体 text：坐标为左上角，置于进度条下方
            tft.text(font_En, en_word, 10, 56, CLR_WHITE, 0)
        # 中文释义：自动换行后绿色绘制
        title_xy = _line_auto(font, self.abc[self.id][1], 230, 70, 5, 105)
        for x, y, txt in title_xy:
            tft.write(font, txt, x, y, CLR_VOCAB_DEF, 0)

    def _draw_auto_progress(self, px):
        """绘制自动播放进度条（0..240），仅当像素变化时重绘。"""
        if px == self._auto_progress_px:
            return
        tft = self.tft
        tft.fill_rect(0, CONTENT_Y, 240, 4, CLR_VOCAB_PROGBG)
        if px > 0:
            tft.fill_rect(0, CONTENT_Y, px, 4, CLR_VOCAB_BTN_ACTIVE)
        self._auto_progress_px = px

    def _clear_auto_progress(self):
        """清除进度条（切回手动时）。"""
        self.tft.fill_rect(0, CONTENT_Y, 240, 4, 0)
        self._auto_progress_px = -1

    # ---------- 生命周期 ----------
    def enter(self):
        self.HomeUi('背单词')
        self.MainUi()
        # 丢弃进入前可能残留的按键事件/边沿，避免误触发
        self.buttons.clear_events()

    def loop(self):
        up, sel, down = self.buttons.scan()

        # C 退出：始终生效
        if down:
            self._beep(1000, 20)
            return False

        if self._mode == "manual":
            if up:
                # A 翻页：下一个（仅前进，循环）
                self.id = (self.id + 1) % self._total
                self.ButA(True)
                self.MainUi()
                self.ButA()
                self._beep()
            elif sel:
                # B 自动：开启自动播放
                self._mode = "auto"
                self._auto_start_ms = time.ticks_ms()
                self._auto_progress_px = -1
                self.ButB(True)
                self._beep(1200, 15)
        else:  # auto
            if sel:
                # B 自动：关闭自动播放
                self._mode = "manual"
                self._clear_auto_progress()
                self.ButB()
                self._beep(800, 15)
            else:
                # 进度推进
                now = time.ticks_ms()
                elapsed = time.ticks_diff(now, self._auto_start_ms)
                if elapsed >= AUTO_MS:
                    # 随机跳下一个词（修复源 random.randrange(0,900) bug）
                    self.id = random.randrange(0, self._total)
                    self.MainUi()
                    self._auto_start_ms = now
                    self._auto_progress_px = -1
                else:
                    px = (elapsed * 240) // AUTO_MS
                    if px > 240:
                        px = 240
                    self._draw_auto_progress(px)

        # gc 节流：每 100 帧一次（约 5s），避免高频 collect
        self._frames += 1
        if self._frames % 100 == 0:
            gc.collect()
        time.sleep_ms(50)
        return True

    def exit(self):
        # 释放大对象引用（词库由 vocab.py 包装器卸载 sys.modules 真正回收）
        self.abc = None

"""
电码概述 - 摩斯电码介绍（硬件垂直滚动展示）
移植自源项目 morse/gaishu.py

适配点：
  - 4 键 IRQ -> 3 键轮询（任意键返回）
  - 图片路径 sd/pic/ -> /pic/
  - config.hex2rgb -> hex2rgb, config.TFA/BFA -> TFA/BFA
  - run() 改为 loop() 逐帧返回
"""
import utime
from font import hanzi1k as font
from morse.tft_helpers import hex2rgb, TFA, BFA, ButtonAdapter


class GaiShu:
    def __init__(self, tft_, buttons, morse):
        self.tft = tft_
        self.btn = ButtonAdapter(buttons)
        self.Morse = morse
        self.Morse.Ui('电码概述')
        self.Exit_ = False

        # 滚动初始化
        # 背景图：jpg 为全项目仅此一处使用的接口，部分固件驱动可能不支持，
        # 缺失/不支持时跳过背景图，文字滚动仍可正常工作（不致子功能加载崩溃）
        try:
            self.tft.jpg("/pic/morse/Morsetaste.jpg", 0, 60, True)
        except Exception:
            pass
        self._height = self.tft.height()
        self._width = self.tft.width()
        self._last_line = self._height - 30
        self._tfa = TFA
        self._bfa = BFA
        self.tft.vscrdef(self._tfa, self._height, self._bfa)
        self._scroll = 0
        self._character = 0

        self._xian = [
            '摩尔斯电码',
            'Morse code',
            '也被称作摩斯密码,是',
            '一种时通时断的信号',
            '代码, 通过不同的排列',
            '顺序来表达不同的英文',
            '字母,数字和标点符号.',
            '它发明于1837年,',
            '是一种早期的数字化',
            '通信形式。',
            ' ',
            '++++重点一++++',
            '它的组成是由',
            '点dot(.),划dash(-)',
            '这两种符号所组成的。',
            ' ',
            '++++重点二++++',
            '一点作为一个基本的信',
            '号单位(t),一划的长度',
            '就是3个基本单位(3t);',
            ' ',
            '++++重点三++++',
            '点和划之间的停顿间隔',
            '是1个时间长度(t);',
            ' ',
            '++++重点四++++',
            '字母(数字)与字母(数',
            '字)之间的停顿间隔就是7个',
            '基本单位长度(7t).',
            ' ',
            ' '
        ]

    def loop(self):
        """每帧滚动 1 像素，任意键返回"""
        a_short, b_short, c_short, b_long = self.btn.scan()
        if a_short or b_short or c_short or b_long:
            self.tft.init()
            return False

        self.tft.fill_rect(0, self._scroll, self._width, 1, 0)
        if self._scroll % 30 == 0:
            line = (self._scroll + self._last_line) % self._height
            fg = hex2rgb('#FF0000') if (self._character in [1, 11, 16, 21, 25]) else hex2rgb('#FFFFFF')
            self.tft.write(font, self._xian[self._character], 5, line, fg, 0)
            self._character += 1
            if self._character >= len(self._xian):
                self._character = 0
        self.tft.vscsad(self._scroll + self._tfa)
        self._scroll += 1
        self._scroll %= self._height
        utime.sleep(0.05)
        return True

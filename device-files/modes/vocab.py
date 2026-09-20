"""
背单词模式 - BaseMode 包装器
将 vocab/ 包的背单词功能接入框架的生命周期（结构同 modes/morse_code.py）。

生命周期：
  enter() -> 创建 Words 并绘制首屏
  loop()  -> 委托给 Words.loop()，返回 False 退出模式
  exit()  -> 释放 Words 并卸载 vocab 包所有子模块释放内存
"""

from modes.base import BaseMode


class VocabMode(BaseMode):
    MODE_ID = "vocab"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._words = None

    def enter(self):
        self.display.fill(0x0000)
        from vocab.words_main import Words
        self._words = Words(self.display, self.buttons, self.buzzer)
        self._words.enter()

    def loop(self):
        if self._words is None:
            return False
        return self._words.loop()

    def exit(self):
        if self._words is not None:
            self._words.exit()
            self._words = None
        # 卸载 vocab 包所有子模块释放内存
        import sys
        import gc
        for k in list(sys.modules):
            if k == "vocab" or k.startswith("vocab."):
                del sys.modules[k]
        gc.collect()

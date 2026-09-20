"""
摩斯电码模式 - BaseMode 包装器
将 morse/ 包的 4 功能摩斯电码套件接入框架的生命周期。

生命周期：
  enter() -> 创建 MorseMain 路由器并绘制子菜单
  loop()  -> 委托给路由器 loop()，返回 False 退出模式
  exit()  -> 关闭路由器并卸载 morse 包所有子模块释放内存
"""

from modes.base import BaseMode


class MorseCodeMode(BaseMode):
    MODE_ID = "morse_code"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._router = None

    def enter(self):
        self.display.fill(0x0000)
        from morse.morse_main import MorseMain
        self._router = MorseMain(self.display, self.buttons, self.buzzer)
        self._router.enter()

    def loop(self):
        if self._router is None:
            return False
        return self._router.loop()

    def exit(self):
        if self._router is not None:
            self._router.exit()
            self._router = None
        # 卸载 morse 包所有子模块释放内存
        import sys
        import gc
        for k in list(sys.modules):
            if k == "morse" or k.startswith("morse."):
                del sys.modules[k]
        gc.collect()

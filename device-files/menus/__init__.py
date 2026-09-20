"""
多功能终端框架 - 菜单插件集
闲沐工坊 · 插件式架构

统一接口：
    def run_menu(display, buttons, buzzer=None) -> mode_id:
        "显示菜单，用户选择后返回 MODE_ID 字符串"
        ...

当前可用：
    list.py      iOS Settings 风格纵向列表菜单
    carousel.py  横向轮播图标菜单
"""

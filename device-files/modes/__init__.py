"""
多功能终端框架 - 模式层入口
闲沐工坊 · 插件式架构

导出 BaseMode 基类、HeaderButtonMode UI 模板和 dispatch 调度器。
模式注册表已迁移至 modes/registry.py。
"""

from .base import BaseMode, get_sub_font, get_big_font
from .ui_template import HeaderButtonMode
from .registry import dispatch, get_available_ids

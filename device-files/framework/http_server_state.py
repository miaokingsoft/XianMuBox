"""
XianMuBox - HTTP 服务器 socket 持久化状态
闲沐工坊 · 插件式架构

本模块刻意保持极简，仅保存服务器 socket 对象引用。

背景：framework.http_server 体积较大（socket+json），boot.py 会在每次
模式退出后将其从 sys.modules 中移除以释放内存。若把 socket 直接存于
http_server 模块内部，模块卸载后 Python 层面的引用会丢失，但底层 OS socket
仍可能占用端口，导致再次进入设置时无法重新 bind，报 EADDRINUSE。

把 socket 引用单独放在本模块并不回收，即可在 http_server 被卸载/重载后
仍保持运行状态，同时保留内存优化意图。
"""

_http_server_sock = None

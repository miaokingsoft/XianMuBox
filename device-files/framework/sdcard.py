"""
XianMuBox - SD 卡挂载层 (v1.9 SD 卡接入)

驱动 TF 卡 (SPI 模式) 挂载到 /sd, 供动画素材/音频/数据记录等用途:

  - mount(): machine.SDCard(slot=2) + os.mount('/sd');
    20MHz 失败自动降 10MHz 重试一次 (杜邦线过长最常见问题)
  - umount(): 卸载并释放 SPI 外设 (设置页/调试用, 幂等)
  - mounted(): 当前挂载状态查询

设计约束:
  - 挂载失败绝不抛异常、绝不阻塞启动 (main.py 启动序列静默调用),
    无卡/坏卡/接线异常时所有接口安全返回 False
  - SD 走独立 SPI(2) 总线, 与屏幕 SPI(1) 完全隔离 (见 doc/SD卡模块实施方案.md)
  - 模块无卡检测 (CD) 脚, 不支持热插拔, 插拔卡须断电
  - PC 仿真 (无 machine.SDCard / 设备档案无 SD_* 引脚) 安全降级

MicroPython 兼容: 无 f-string / machine 惰性 import
"""

import os

try:
    import machine
    _HAS_SD = hasattr(machine, "SDCard")
except ImportError:
    machine = None
    _HAS_SD = False

try:
    from config import SD_SCK, SD_MOSI, SD_MISO, SD_CS
except ImportError:
    SD_SCK, SD_MOSI, SD_MISO, SD_CS = 18, 17, 13, 1

_MOUNT = "/sd"

_sd = None


def mounted():
    """SD 卡当前是否已挂载"""
    return _sd is not None


def mount_point():
    """挂载点路径 (供模式层拼接 /sd/xxx 路径)"""
    return _MOUNT


def mount():
    """挂载 SD 卡, 成功返回 True; 失败/无卡/不支持返回 False (不抛异常)。

    频率先 20MHz, 失败降 10MHz 重试一次; 仍失败则放弃 (启动流程照常)。
    """
    global _sd
    if _sd is not None:
        return True
    if not _HAS_SD or SD_SCK is None or SD_MOSI is None or \
       SD_MISO is None or SD_CS is None:
        return False
    for freq in (20000000, 10000000):
        try:
            sd = machine.SDCard(slot=2, sck=machine.Pin(SD_SCK),
                                mosi=machine.Pin(SD_MOSI),
                                miso=machine.Pin(SD_MISO),
                                cs=machine.Pin(SD_CS), freq=freq)
            os.mount(sd, _MOUNT)
            _sd = sd
            print("[SD] mounted at %s (%d Hz)" % (_MOUNT, freq))
            return True
        except Exception as e:
            print("[SD] mount fail @%d Hz:" % freq, e)
    return False


def umount():
    """卸载 SD 卡并释放 SPI 外设 (幂等; 未挂载时为空操作)"""
    global _sd
    if _sd is None:
        return
    try:
        os.umount(_MOUNT)
    except Exception:
        pass
    try:
        _sd.deinit()
    except Exception:
        pass
    _sd = None

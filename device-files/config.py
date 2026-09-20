"""
XianMuBox - 硬件配置（由 build.py 自动生成）
目标设备: esp32s3_n8r8
"""

DEVICE_MODULE = "devices.esp32s3_n8r8"

_dev = __import__(DEVICE_MODULE)
for _part in DEVICE_MODULE.split(".")[1:]:
    _dev = getattr(_dev, _part)

TFT_SCL = _dev.TFT_SCL
TFT_SDA = _dev.TFT_SDA
TFT_RES = _dev.TFT_RES
TFT_DC  = _dev.TFT_DC
TFT_CS  = _dev.TFT_CS
TFT_BLK = _dev.TFT_BLK
BTN_A   = _dev.BTN_A
BTN_B   = _dev.BTN_B
BTN_C   = _dev.BTN_C
BUZZER_PIN = _dev.BUZZER_PIN
LED_PIN = getattr(_dev, "LED_PIN", None)
I2S_BCLK = getattr(_dev, "I2S_BCLK", None)
I2S_LRC  = getattr(_dev, "I2S_LRC", None)
I2S_DIN  = getattr(_dev, "I2S_DIN", None)
SD_SCK  = getattr(_dev, "SD_SCK", None)
SD_MOSI = getattr(_dev, "SD_MOSI", None)
SD_MISO = getattr(_dev, "SD_MISO", None)
SD_CS   = getattr(_dev, "SD_CS", None)

W = _dev.W
H = _dev.H
ROTATION    = _dev.ROTATION
BUFFER_SIZE = _dev.BUFFER_SIZE

CHIP          = _dev.CHIP
BOARD_NAME    = _dev.BOARD_NAME
HAS_BLE       = _dev.HAS_BLE
HAS_PSRAM     = _dev.HAS_PSRAM
MAX_FONT_SIZE = _dev.MAX_FONT_SIZE

# 固件版本 (云平台激活/心跳上报)
FW_VERSION = "1.3.0"

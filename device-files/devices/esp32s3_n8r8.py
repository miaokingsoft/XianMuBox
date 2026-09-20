"""XianMuBox - ESP32-S3 N16R8 设备档案"""

# ===== 芯片信息 =====
CHIP = "esp32s3"
BOARD_NAME = "ESP32-S3 N8R8"

# ===== 引脚映射 =====
TFT_SCL, TFT_SDA, TFT_RES = 12, 11, 8
TFT_DC, TFT_CS, TFT_BLK   = 9, 10, 7
BTN_A, BTN_B, BTN_C       = 5, 4, 3
BUZZER_PIN = 6
LED_PIN = 21  # WS2812 RGB LED
# I2S 音频 (MAX98357A 功放, v1.5 整点新闻播报; 2026-09-13 由 15/16/14 调整, 避开底排拥挤并集中走线)
I2S_BCLK = 38  # 位时钟 (底边焊盘, 与 LRC/DIN 相邻)
I2S_LRC  = 39  # 左右声道字时钟 (WS)
I2S_DIN  = 40  # 串行数据 → MAX98357A DIN
# SD 卡 (独立 SPI(2) 总线, 与屏幕 SPI(1) 隔离; 见 doc/SD卡模块实施方案.md v3)
SD_SCK  = 18   # SPI 时钟
SD_MOSI = 17   # 主出从入
SD_MISO = 13   # 主入从出
SD_CS   = 1    # 片选

# ===== 屏幕参数 =====
W, H = 240, 240
ROTATION = 2  # ST7789: 0=0°, 1=90°, 2=180°, 3=270°  (180° 翻转)
BUFFER_SIZE = 320 * 40 * 2   # S3 有 PSRAM，buffer 可更大

# ===== 硬件能力标记 =====
HAS_BLE = True
HAS_PSRAM = True
MAX_FONT_SIZE = 32

# ESP32 系列多功能终端框架 - 项目约定

> 闲沐工坊 · 插件式框架 v1.1  
> 适用所有 `.py` 文件修改与新增  
> 本规则与 `doc/框架架构设计文档.md` + `doc/功能开发SOP.md` 互为补充  
> 最近更新：2026-06-26

---

## 1. 项目身份

- **项目名**：XianMuBox（闲沐盒子）
- **品牌名**：闲沐工坊 / XianMu Work
- **硬件**：ESP32 系列开发板（S3/S2 等），当前参考硬件为 ESP32-S3 N8R8
- **屏幕**：ST7789（分辨率及 rotation 由 `devices/<device>.py` 定义，当前 240×240）
- **固件**：MicroPython v1.29+（定制构建：`st7789_mpy` C 驱动 + `romfont` 字体内嵌 C 模块 + `mp3dec` MP3 软解码 C 模块 + USB CDC 提前初始化补丁 + 3.5MB app 自定义分区，构建见 `doc/固件构建指南.md`，版本履历见 `doc/固件版本履历.md`）
- **框架定位**：基于 MicroPython 的 ESP32 系列多功能桌面终端开发框架，通过插件式架构实现硬件无关的功能组合
- **UI 风格**：iOS Settings.app 暗色模式（纯黑底 + 卡片式选中态）
- **远程配置**：局域网 HTTP 服务（默认端口 80）

---

## 2. 三层插件架构

```
boot.py (装配层)
  framework/hal.py → boot_animations/ → menus/ → modes/
```

- **framework/** — 平台层：颜色调色板（单一事实源）、硬件抽象（Buttons/Buzzer/display）、WiFi 服务、settings.json 统一读写、HTTP 配置服务
- **boot_animations/** — 开机动画插件：`play(display, buzzer)` 接口
- **menus/** — 菜单插件：`run_menu(display, buttons, buzzer) -> mode_id` 接口
- **modes/** — 功能模式插件：BaseMode 子类，通过 MODE_ID 懒加载调度

### 三层解耦原则

- 层之间**仅通过 MODE_ID 字符串**关联，禁止对象引用或索引号耦合
- boot.py 是**唯一知道三层存在的装配点**
- 每层内多个实现可**任意替换**（改 boot.py 一行 import，或改 profile 的 `boot_animation` / `menu_style`）

---

## 3. MicroPython 编码约束

### 3.1 语法限制
- **禁止 f-string**：MicroPython 全系固件不支持，用 `"text:" + var` 或 `"%s: %d" % (s, n)`
- **避免 `with` 语句**：旧版固件可能不支持 context manager
- **时间统一用 `time.ticks_ms()` / `ticks_diff()`**：避免 `time.time()` 溢出
- **除法统一用 `//` 整数除**：像素坐标必须是整数
- **字体 import 约定**：`from font import hanzi_24 as hanzi24`（全量汉字，数据嵌固件 romfont，见架构文档第 10 节），不加 `.py`；旧 `hz_24x24`/`hanzi24` 已退役，仿真器 spec 脚本由 `render_spec._load_font` 做旧名兼容

### 3.2 内存管理（重要）
```
✅ 在以下节点调用 gc.collect()：
  - 开机动画/菜单初始化后
  - 模式退出后（boot.py 主循环）
  - 批量绘制完成后
  - png 图片解码后
  - 循环中每 100 帧一次（(loop_cnt & 0x63) == 0）

✅ 使用 tuple 替代 list 作为模块级常量（MicroPython ROM 化优化）
✅ 动态数组采用原地赋值（arr[i][j]=v）而非整体替换
✅ 退出时置空大对象引用（self._data = None）以辅助 gc
❌ 禁止在循环内频繁创建临时 list/对象
❌ 禁止 WiFi 连接期间调用 gc.collect()（会导致连接中断）
```

### 3.3 display 操作规范
- `display.write()` 的 bg/fg 参数**不能传 None 或 0**，必须用颜色常量
- `display.png()` 加载 PNG 图标时，需显式传入 `transparency=True`（透明图）
- ST7789 初始化 `buffer_size` 需根据芯片内存调整（内存受限的低端芯片 ≤4096，S3 可更大）
- `display.circle()` 在部分固件中不可用，用 `hline` 逐行填充替代

---

## 4. UI 视觉规范（iOS 暗色模式）

### 4.1 颜色调色板（来源：`framework/colors.py`，**绝对不允许在别处重复定义**）

| 常量 | 值 | 用途 |
|------|-----|------|
| `CLR_BG` | 0x0000 | 纯黑背景 |
| `CLR_CARD` | 0x18C3 | 卡片底色 |
| `CLR_CARD_HI` | 0x294B | 卡片顶部 1px 高光 |
| `CLR_CARD_SH` | 0x1082 | 卡片底部 1px 阴影 |
| `CLR_WHITE` | 0xFFFF | 白·文字/描边 |
| `CLR_GRAY` | 0x8410 | 中性灰·未选中文字 |
| `CLR_DIM` | 0x4208 | 极暗·分隔线/装饰 |
| `CLR_CYAN` | 0x07FF | 青·激活态 |
| `CLR_GREEN` | 0x07E0 | 绿·盈利/已连接 |
| `CLR_RED` | 0xF800 | 红·亏损/警告 |
| `CLR_AMBER` | 0xFD20 | 琥珀·低额 |
| `CLR_MAGENTA` | 0xF81F | 品红·强调 |
| `CLR_ORANGE` | 0xFD00 | 橙·新增/提示（v1.1 起） |

### 4.2 iOS 卡片选中态
```
选中项绘制顺序（不可颠倒）：
1. fill_rect(x, y, w, h, CLR_CARD)         # 铺底
2. hline(x, y, w, CLR_CARD_HI)             # 顶部 1px 高光
3. hline(x, y+h-1, w, CLR_CARD_SH)         # 底部 1px 阴影
4. rect(x, y, w, h, CLR_WHITE)             # 白色 1px 描边
```
使用 `BaseMode.draw_card()` 统一实现。

### 4.3 布局常量
- 屏幕：240×240（部分功能如 swipe 扩展到顶部栏+底部按钮区）
- 顶部 WiFi 状态条：y=0~16
- 菜单项间距：45px 等距（非 40px）
- 底部页码点：y=226，4×4 像素
- 操作提示：底部 y210~235 区域

### 4.4 禁止的 UI 元素
❌ 边框/角标/双层辉光/雷达/扫描线/脉冲/英文副标题/网格背景
❌ TFT 屏上极暗色（0x4208 以下）单独作为文字颜色（人眼不可见）

---

## 5. 依赖规则（严格执行）

### 5.1 允许的导入
```
✅ modes/xxx.py → framework/colors.py, framework/hal.py, framework/wifi.py
✅ modes/xxx.py → framework/settings.py                  (持久化配置)
✅ modes/xxx.py → config.py (W, H, ROTATION 等硬件常量)
✅ modes/xxx.py → modes/base.py (BaseMode, SUB_FONT)
✅ menus/*.py   → framework/colors, framework/hal, framework/wifi, app_config.py
✅ boot_animations/* → framework/colors, framework/wifi, config.py, font/
✅ framework/http_server.py → framework/settings.py     (统一读写)
```

### 5.2 禁止的导入
```
❌ modes/xxx.py → modes/yyy.py           （模式间禁止互相依赖）
❌ menus/*.py   → modes/xxx.py           （菜单不感知模式实现）
❌ app_config.py → modes/*               （配置不依赖模式层）
❌ framework/*  → app_config.py          （平台层不依赖应用层）
❌ framework/*  → modes/*                （平台层不依赖模式层）
❌ modes/xxx.py → menus/*                （模式不依赖菜单）
```

### 5.3 配置分工
| 文件 | 职责 | 修改时机 | 是否纳入版本控制 |
|------|------|---------|------------------|
| `devices/<X>.py` | 引脚、屏幕尺寸、硬件能力 | 换硬件时 | ✅ |
| `config.py` | 指向目标设备档案（build.py 生成） | 自动生成 | ✅ |
| `framework/colors.py` | 颜色调色板（唯一来源） | 主题改版时 | ✅ |
| `app_config.py` | 菜单项编排（build.py 按 profile 生成） | 功能编排时 | ✅ |
| `settings.json` | WiFi / API Key / 亮度 / HTTP / 滑动（时区固定北京时间 UTC+8，不落配置） | 换网络/服务时 | ❌ |

> ⚠️ `secrets.py` 与 `swipe_settings.json` 已废弃。所有持久化配置统一写入 `settings.json`，由 `framework/settings.py` 负责读写/校验/迁移。

---

## 6. 按钮交互约定

| 按钮 | 短按 | 长按 (>800ms) |
|------|------|---------------|
| A | 上移/上一项 | 模式特定（如 swipe 切换活跃设备） |
| B | 确认/选择 | **统一返回主菜单**（先 `flash_exit()` 后 `return False`） |
| C | 下移/下一项 | 模式特定 |

> 按钮 GPIO 引脚由 `devices/<X>.py` 定义（`BTN_A` / `BTN_B` / `BTN_C`），不同开发板可重新映射。

- 三按钮均为**内部上拉**，按下 = LOW，边沿检测
- 长按阈值：`LONG_PRESS_MS = 800`（来自 `framework/hal.py`）
- 消抖窗口：`DEBOUNCE_MS = 30` + 有效按压最短时长 `MIN_PRESS_MS = 15`（来自 `framework/hal.py`，IRQ 锁存 + 时间戳消抖 + 毛刺/余颤时长校验）
- **边沿信号统一取自 `buttons.scan()` / `buttons.scan_timed()`**（IRQ 锁存 + 消抖唯一入口）；禁止在模式里自行 `read_raw()` 比较边沿，长按计时用 `scan_timed()` 返回的电平；模式交接处（enter/reset/响铃接管）用 `buttons.clear_events()` 丢弃残留事件
- 操作必须有**蜂鸣反馈**（`beep()` 或 `buzzer.tone()`）和视觉反馈

---

## 7. 新增功能标准流程（5 步）

### 7.1 源码模式（直接编辑 `src/`）

```
1. 创建 modes/xxx.py → 继承 BaseMode，实现 enter()/loop()/exit()
2. app_config.py → MENU_ITEMS 追加一行 {"id":"xxx", "name":"中文", "color":CLR_XXX, "icon":"/icons/xxx.png"}
3. modes/registry.py → _MODULE_MAP 追加 "xxx":("modes.xxx","XxxMode")
4. profiles/*.json → features 数组追加条目（含 id/name/color/icon/module/class）
5. 运行 python tools/test.py → 上传测试
```

### 7.2 Profile 模式（推荐用于功能编排）

直接修改 `profiles/<name>.json` 的 `features` 数组。`tools/build.py` 会按 profile 自动生成 `app_config.py` 和 `modes/registry.py`。开发者只需提供对应的模式源码文件。

### 7.3 删除功能

从 `app_config.py`、`modes/registry.py` 和所有相关 profile 中各删对应行即可，不影响其他功能。

---

## 8. 命名与格式约定

- 文件名：小写 + 下划线（`menu.py`, `ai_balance.py`）
- 类名：PascalCase（`ClockMode`, `AIBalanceMode`）
- 模块级常量：UPPER_SNAKE（`CLR_BG`, `LONG_PRESS_MS`, `BRIGHTNESS_LEVELS`）
- 私有方法/属性：下划线前缀（`_draw_card`, `_idx`）
- 类属性 `MODE_ID`：小写 + 下划线，与文件名一致
- 注释语言：中文（所有模块文件头部 docstring 用中文描述功能）
- 每文件必须以模块级 docstring 开头，包含功能描述和操作说明

---

## 9. 测试检查清单

新增/修改模式时必须验证：
- [ ] `enter()` 首行 `display.fill(CLR_BG)` 清屏
- [ ] C 短按闪白返回主菜单（内容浏览类）或"退出"菜单项可用（菜单/列表类）
- [ ] 删除该模式注册后，菜单正常显示其余项
- [ ] 进入→退出→再进入 5 次无内存泄漏
- [ ] 模式内异常不扩散到 boot.py 主循环
- [ ] 操作时有蜂鸣反馈

部署前必须运行：
```bash
python tools/test.py        # 必须 ALL PASSED
python tools/build.py --profile <X>  # 必须 BUILD SUCCESS
```

---

## 10. 禁止事项汇总

| # | 禁止 | 替代方案 |
|---|------|---------|
| 1 | 在模块内定义颜色常量 | 从 `framework/colors.py` 导入 |
| 2 | 在模块内定义 `Buttons` 类 | 使用 `framework/hal.py` 中的 |
| 3 | 模式间互相 import | 通过 MODE_ID + registry 调度 |
| 4 | 使用 f-string | `"t:" + str(x)` 或 `"%d" % x` |
| 5 | 直接操作 GPIO | 通过 `self.display` / `self.buttons` |
| 6 | 全屏 `fill()` 替代局部重绘 | 使用 `fill_rect()` 局部清除 |
| 7 | 循环内高频 `gc.collect()` | 控制在每 100 帧或关键节点 |
| 8 | WiFi 连接中 `gc.collect()` | 等连接结束后再回收 |
| 9 | `display.write()` 传 None/0 | 必须传合法颜色常量 |
| 10 | 菜单感知模式细节 | 只返回 MODE_ID，不读取模式文件 |
| 11 | 在 `secrets.py` 中写 WiFi/API Key | 通过 `settings.json` + `framework/settings.update()` |
| 12 | 自行读写 `swipe_settings.json` | `framework/settings.get_swipe_config()` 统一入口 |
| 13 | 直接 `open("settings.json", "w")` | 走 `framework/settings.update()` 自动校验 |

---

## 11. settings.json 字段规范（v1.2 api_keys 多 key；v1.3 扩展节点；v1.4 云平台；v1.5 新闻播报；v2.4 网络电台；v1.0 听老故事）

```json
{
  "wifi": [{"ssid": "...", "password": "..."}],
  "api_keys": {
    "ai_token": "sk-shared-proxy-token",          // 单值字段: 共享代理 token, 字符串
    "deepseek":    [{"label": "DeepSeek-主",   "key": "sk-..."}],   // 列表: 一个服务商多个 key
    "siliconflow": [{"label": "默认",          "key": "sk-..."}],
    "zhipu":       [{"label": "Zhipu-1",       "key": "sk-..."}]
  },
  "brightness": 100,
  "http_port": 80,
  "swipe": {
    "interval_min": 5, "interval_max": 15,
    "screen_w": 1080, "screen_h": 1920,
    "mode_idx": 0, "swipe_offset_ratio": 6,
    "swipe_steps": 5, "swipe_delay_ms": 20,
    "active_device_idx": 0, "devices": [...]
  },

  // ===== v1.3 新增节点 =====
  "alarms": [                                     // 全局闹钟 3 组 (framework/alarm.py)
    {"en": true, "h": 7, "m": 30, "days": [1,2,3,4,5], "msg": "起床"}
  ],                                              // days: 0=周日..6=周六, 空列表=单次
  "api_keys_hidden": ["siliconflow"],             // Web 页停用显示的服务商
  "weather": {"city": "武汉", "api_key": "..."},  // 心知天气 (framework/weather.py)
  "project": {                                    // 项目仪表动态站点 + 配套
    "own_sites": [{"name": "...", "api": "https://...", "key": "..."}],
    "ai_token": "...", "self_media": [],
    "device_sn": "SN-..."                         // 设备序列号: machine.unique_id() 派生
                                                  //  (硬件权威可查不可改; 本字段仅展示
                                                  //   缓存, 篡改读取时自动纠偏, Web 不可写)
  },
  "stock": {"endpoint": "https://...", "api_key": "..."},   // 股票数据服务

  // ===== v1.4 新增节点 =====
  "cloud": {                                      // 闲沐盒子云平台 (framework/cloud.py)
    "endpoint": "https://api.wwzu.com",           // 平台基础地址 (http(s):// 开头, 保存去尾 /)
    "activation_key": "...",                      // 型号激活密钥 (出厂/私有构建注入, 文档中隐去;
                                                  //  存空 = 云平台不可激活, 不经 Web 展示/填写)
    "model": "XianMu_Esp32S3",                    // 设备型号代号 (出厂固化; 空 = 自动取 config.CHIP)
    "enabled": true                               // 云服务总开关 (关闭云服务用本开关)
  }

  // ===== v1.5 新增节点 =====
  ,"news": {                                      // 整点新闻播报 (framework/news.py)
    "enabled": true,                              // 自动播报总开关 (手动播报不受限)
    "hours": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22],
                                                  // 允许自动播报的整点档位 (0-23 去重升序;
                                                  //  平台档期 9:00~22:00 共 14 档)
    "volume": 80                                  // 播报音量 0-100 (约 6dB 步进软件音量)
  }

  // ===== v2.4 新增节点 =====
  ,"radio": {                                     // 网络电台 (framework/radio.py)
    "volume": 80,                                 // 收听音量 0-100
    "last_station": "cnr1"                        // 上次收听电台代码 (台单页光标定位用)
  }

  // ===== v1.0 新增节点 =====
  ,"story": {                                     // 听老故事 (framework/story.py)
    "volume": 80,                                 // 播放音量 0-100
    "last_tag": "英雄模范",                        // 上次收听标签名 (标签页光标定位用)
    "last_story": "book01",                       // 上次收听连环画 htmlname (v1.1 契约改版
                                                  //  后为字符串; 旧数字 id 值失配自动从头)
    "last_track": 3                               // 上次听到的话序号 (清单下标; 整本播完归零)
  }
}
```

**cloud 固化约定（v1.4.1）**：`activation_key` 与 `model` 为出厂固化默认值（`framework/settings.py → _DEFAULT_CLOUD`），`get_cloud()` 对存储中的空串自动回退默认值；设备绑定用户账号走 SN + 激活码流程（本机设置「云端激活」子页），密钥不出现在任何 UI 与日志中。

**v1.1 → v1.2 迁移**：`framework/settings.py` 在 `load_settings()` 启动时自动检测旧 schema（api_keys 中非 `ai_token` 字段为扁平字符串），整体升级为单元素列表并落盘。HTTP POST 提交旧 schema 时，`_validate_api_keys` 同样会自动升级。无需用户手动迁移。

访问方式：

| 用途 | API |
|------|-----|
| 读完整配置 | `from framework.settings import load_settings; load_settings()` |
| 读顶层字段 | `from framework.settings import get; get("brightness", 100)` |
| 读 WiFi 列表 | `from framework.wifi import get_wifi_configs`（间接通过 settings） |
| 读 API Keys（兼容旧） | `from framework.settings import get_api_keys` |
| **读某服务商所有 key（v1.2 推荐）** | `from framework.settings import get_provider_keys; get_provider_keys("deepseek")` |
| **读共享 token（v1.2 推荐）** | `from framework.settings import get_token` |
| 读 swipe 配置 | `from framework.settings import get_swipe_config` |
| 读闹钟（v1.3） | `from framework.settings import get_alarms` |
| 读项目仪表配置（v1.3） | `from framework.settings import get_project` |
| 读股票配置（v1.3） | `from framework.settings import get_stock_config` |
| 读云平台配置（v1.4） | `from framework.settings import get_cloud` |
| 读新闻播报配置（v1.5） | `from framework.settings import get_news` |
| 读网络电台配置（v2.4） | `from framework.settings import get_radio` |
| 读听老故事配置（v1.0） | `from framework.settings import get_story` |
| 读设备 SN（云平台上报用） | `from framework.settings import get_device_sn`（返回 `machine.unique_id()` 派生值，唯一权威不可篡改；PC 端无 machine 模块时沿用缓存值） |
| 更新并持久化 | `from framework.settings import update; update({"brightness": 75})` |
| 远程 HTTP 改 | `POST /api/config` 走 `framework/http_server.apply_config` |
| 本机设置改 | `modes/settings.py`（菜单入口）走 `framework/settings.update` |

字段校验范围由 `framework/settings.py` 内 `_INT_RANGES` / `_SWIPE_INT_RANGES` / `_validate_api_keys` 定义，update 时自动拒绝越界与非法 schema。

---

## 12. HTTP 服务规范

设备启动后由 `modes/settings.py` 或 `boot.py` 调用 `framework.http_server.start_http_server(port)` 启动。

| 端点 | 方法 | 用途 |
|------|------|------|
| `/` | GET | HTML 配置页（深色科技风多页签：WiFi/API Key/亮度/HTTP/滑动/天气/闹钟/项目（股票/自有网站/自媒体）/云服务/新闻；时间卡片固定北京时间 UTC+8） |
| `/api/config` | GET/POST | 读/写完整或部分配置 |
| `/api/time` | POST | 手动触发 NTP 对时（固定北京时间 UTC+8，无需参数） |
| `/api/wifi` | GET/POST | 状态查询/触发重连 |
| `/api/weather` | GET/POST | 天气配置/城市列表/当前天气；更新城市与 Key/强制刷新 |
| `/api/balance` | GET | AI 服务商余额实时查询（代理） |
| `/api/stock` | GET | 股票数据 API 透传（`modes/stock_api.py`） |
| `/api/cloud` | GET | 云平台服务状态（state/SN/型号/固件/心跳/错误码，v1.4） |
| `/api/cloud/activate` | POST | 手动立即激活（阻塞约 1~10s，Token 轮换幂等） |
| `/api/cloud/heartbeat` | POST | 手动立即心跳（阻塞） |
| `/api/news/play` | POST | 手动触发整点新闻播报（非阻塞置请求标志，v1.5） |
| `/api/news/status` | GET | 最近一次新闻播报结果（状态/档期/标题/来源/时刻，v1.5） |
| `/api/news/preview` | GET | 拉取当档新闻预览（标题+媒体完整 URL，阻塞约 1~10s，API 体检用，v1.5） |
| `/api/test_wwwsite` | POST | 自有网站连通性自检 |

- 服务端口由 `settings.json.http_port` 决定（默认 80）
- 主循环需周期性 `poll_http_server()`，每次最多处理一个请求（50 ms 超时）
- 所有写操作最终委托给 `framework/settings.update()`，自动校验+持久化
- 服务器 socket 引用保存在 `framework/http_server_state.py`（模块卸载重载后端口不丢）

---

## 13. 文档同步约定（强制 · Definition of Done）

> 2026-09-01 用户确立的全局约定：**任何对项目功能 / 接口 / 结构的更新，必须在同一批改动中同步更新对应文档；文档未同步 = 任务未完成。**

"改什么 → 同步哪些文档"对照表（与项目根 `AGENTS.md` 保持一致）：

| 改动内容 | 必须同步的文档 |
|----------|----------------|
| 新增功能模式 | 复制 `doc/模式功能文档模板.md` 新建功能文档；登记 `doc/功能文档总览.md`、README、`doc/项目目录结构说明.md` |
| 修改某模式行为（按键/页面/数据源/交互） | 该模式功能文档对应章节（按键表逐项对照源码核实） |
| `framework/` 新增或修改模块 | `doc/框架架构设计文档.md` + README 对应段 |
| `settings.json` 结构变更 | 框架架构设计文档 4.x、本文档第 11 节、受影响模式文档 |
| HTTP 接口变更 | 框架架构设计文档 9 节、本文档第 12 节、README HTTP 表 |
| 全局约定（按键/UI/编码约束） | 框架架构设计文档对应节、本文档对应节、受影响模式文档 |
| `profiles/` / 构建工具 / 测试 | `doc/项目目录结构说明.md` + README |
| 目录 / 文件增删 | `doc/项目目录结构说明.md` + README 目录树 |

执行要点：

1. 文档更新与代码改动**同批提交**，不留"下次补"；
2. 按键表、数据来源、接口参数必须**逐项对照源码**核实，不凭记忆写；
3. 提交前 `python tools/test.py` 全绿（`secrets.py` 1 项为已知预期失败）；
4. 提交走**命令行**（`git add -A` → `git commit`），不要用编辑器 / GUI 的「版本提交」按钮，原因见下方提交环境注意。

> **提交环境注意（2026-09-16 实测，ZCode Desktop 3.11.2）**
> 本仓库有 31 个中文名文件，用 GUI「版本提交」会报 `提交失败：git add failed: fatal: pathspec 'doc/需<U+FFFD><U+FFFD>文档.md' did not match any files`。
> **该失败与 `core.quotepath` 无关**，排查时不要动这个配置：GUI 取列表用的是 `git status --porcelain=v2 --branch -z`，`-z` 输出是裸 UTF-8、不受 quotepath 影响；本仓库 quotepath 在 system / global / local 三级均未设置（走默认 true），属正常状态。
> 真实原因在客户端侧：它读取子进程 stdout 时按**每个数据块单独**做 UTF-8 解码，没有缓存跨块的不完整多字节序列；git 输出工作区状态是分多次 `write` 的，某次块边界落在多字节字符中间时，该字符的两半各自被解码成 `U+FFFD`，损坏后的路径被原样传给 `git add -- <path>`，git 因 pathspec 不匹配而失败。特征有三：损坏路径形如 `doc/需<U+FFFD><U+FFFD>文档.md`（首尾字符完好、中间一字变成两个替换符）；同一批文件里通常只有个别中文名出错；因块边界随 git 输出节奏浮动，GUI 重试可能偶然成功但不可靠。
> **补充（2026-09-19 实测）**：搬移/改名记录同样中招——`git status` 里的 `R  旧路径 -> 新路径` 条目被解析器取箭头左侧**旧路径**传给 `git add`，而旧路径在工作区已不存在，报 `pathspec 'tools/_spk_svg/volume-2.svg' did not match any files`（纯 ASCII 路径也失败，与中文名/编码无关，是同一解析缺陷的另一表现）。凡工作区存在 `git mv` 产生的改名记录，GUI 提交必失败；规避相同——命令行操作，改名用 `git mv`（直接进暂存区）或 `git add -A` 整仓暂存。
> 规避：命令行整仓暂存（`git add -A`，不逐个传中文路径）不受影响；提交信息建议写入 UTF-8 文件后用 `git commit -F <file>`，避免经由控制台代码页传递中文。仓库内无其他修法，客户端 `app.asar` 为打包产物且其更新源未指向可用发布通道。

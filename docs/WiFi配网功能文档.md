# WiFi 配网（Provision）功能文档

> 模块路径：`src/framework/provision.py`（框架级服务 + 设备端配网页，非独立模式）
> 入口：① `main.py` 开机引导 `boot_provision_check()`（WiFi 未连接自动进配网页，C 键跳过）；② 本机设置「WiFi配网」项（`modes/settings.py → _action_provision()`）
> 关联资源：`framework/wifi.py`（STA 单例 + WiFiConnector 复用）、`framework/settings.py`（凭据读写 `settings.json → wifi`）、`framework/qrcode.py`（配网 URL 二维码，延迟导入）、`font/hanzi_24`（全量字库）、`font/vga1_8x8`（ASCII 小字）
> 配置文件：`settings.json → wifi`（结构不变，新凭据插首 + 同名去重，上限 5 条）
> 构建绑定：`tools/build.py → FRAMEWORK_FILES` 白名单 + `gen_main_py` 模板（开机引导注入）
> 最近更新：2026-09-19（v1.8.8 修复真机新设备 "AP START FAIL"：先 `active(True)` 后 `config()`；v1.8.7 首次交付：SoftAP + Captive Portal 开箱配网）

---

## 一、功能概述

解决**开箱/换网场景无任何配网手段**的问题：此前全新设备（`wifi: []`）或密码失效时，开机只显示 "WiFi: Failed" 后进离线菜单，设备端没有扫描/输密码 UI，Web 配置页又依赖"已联网"，形成死锁。

方案采用带屏无输入设备的行业主流做法 **SoftAP + Captive Portal 网页配网**（SmartConfig/Airkiss 已被业界淘汰、BLE 配网需专用 App，均不适合本项目零 App 依赖的定位）：

1. 设备开启开放热点 `XianMuBox-<SN末4位>`；
2. 手机扫码（二维码编码 `http://192.168.4.1/`）或连接热点后由 **DNS 劫持**自动弹出配网页；
3. 网页列出扫描到的周围 WiFi，点选并输入密码提交；
4. 设备持久化凭据并试连，屏幕与网页同步显示连接中/成功/失败；
5. 成功后展示 IP 约 2.5s 自动退出配网页进主菜单，热点/DNS/HTTP 全部收摊释放。

全程无需 USB、串口或专用 App。

## 二、组成与流程

```
开机 (main.py)
  └─ boot_provision_check(): check_wifi() 已连接 → 原流程零改动
       └─ 未连接 → run_provision_page() 设备端配网页(阻塞式子页, C 键跳过)
start_provisioning()
  ├─ AP 热点     network.WLAN(AP_IF) 开放网络, SSID=XianMuBox-<SN末4位>
  ├─ DNS 劫持    UDP 53, 所有域名 A 记录 → AP IP (Captive Portal 探测弹页)
  ├─ 配网 HTTP   TCP 80 (独立轻量实现, 不复用 http_server)
  └─ 试连状态机  复用 WiFiConnector (非阻塞, 5s/SSID)
手机浏览器
  ├─ GET  /                      配网页 (深色风格, 扫描列表/手输 SSID/密码)
  ├─ GET  /api/provision/scan    周围热点列表 (RSSI 降序去重, 上限 15)
  ├─ POST /api/provision/save    提交凭据 (插首/去重/上限5 → settings.json → 试连)
  ├─ GET  /api/provision/status  连接状态轮询 (idle/connecting/success/failed)
  └─ 其余 GET                    302 → 配网页 (兼容 generate_204 等探测路径)
```

**AP 启动时序（强制，v1.8.8）**：`start_provisioning()` 内必须**先 `_ap.active(True)` 并等待就绪，再 `config(essid/authmode)`**。原因：MicroPython ESP32 端口的 `esp_wifi` 协议栈在任意接口首次 `active(True)` 时才初始化；新设备开机 STA 从未激活过，对未激活的 AP 接口 `config()` 会被驱动以 `WIFI NOT INIT` 拒绝（首版把 config 放在 active 之前，真机新设备报 "AP START FAIL" 的根因；老设备因 STA 曾激活过而侥幸正常）。`framework/wifi.py` 的省电设置 `_pm_off()` 同理必须在激活后调用。

## 三、设备端配网页

布局（240×240）：

- 标题「配网模式」+ 分隔线；中区二维码（`http://<AP IP>/`，V2 矩阵 33 模块 × scale 4 = 132px 白底黑码）；青色提示行「手机扫码自动配置」；`AP: XianMuBox-XXXX` 热点名行（8px）；底部状态行。
- 帮助页：三步指引「1 连接热点 / 2 自动弹出配置页 / 3 选择WiFi输密码」+ 热点名与 URL。
- 状态行随连接状态机局部重绘：`A:HELP C:EXIT`（待机）/ `Connecting: <SSID>`（琥珀）/ `OK! IP:x.x.x.x`（绿，双音提示）/ `Failed! B:RETRY`（红，低音）。

## 四、按键表（对照源码 `run_provision_page()`）

| 页面 | A 短按 | B 短按 | C 短按 |
|------|--------|--------|--------|
| 配网页（二维码页） | 切换到帮助页 | 用 `settings.json` 现有凭据重试连接 | 退出配网（收摊全部服务，返回调用方） |
| 帮助页 | 返回二维码页 | 重试连接（自动切回二维码页） | 退出配网 |
| 连接成功 | —（2.5s 后自动退出） | — | 退出配网 |

- 遵循按键统一规范：C=退出、A=页面切换（翻页语义）、B=主操作（重试）；无长按手势。
- 本机设置列表内的「WiFi配网」项：A 上移 / B 进入 / C 下移（列表类通用规范）。

## 五、HTTP 接口明细（仅配网期间、AP 网段 192.168.4.1 可用）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 配网页 HTML（纯静态字符串，深色科技风，全部请求走相对路径；含扫描列表点选、手输 SSID、密码提交、状态轮询 JS） |
| GET | `/api/provision/scan` | STA 接口 `scan()`：RSSI 降序、同名去重、上限 15，返回 `{ok, aps:[{ssid,rssi,secure}]}`；试连进行中拒绝（扫描会打断连接）返回 `{ok:false,msg:"connecting"}` |
| POST | `/api/provision/save` | body `{ssid,password}`：ssid 必填、密码 ≤64；凭据**插首 + 同名去重 + 上限 5** 经 `settings.update()` 落盘后发起非阻塞试连，返回 `{ok,state:"connecting",ssid}` |
| GET | `/api/provision/status` | `{ok,state,ssid,ip}`（附带推进一步状态机，兜底页面轮询） |
| GET | 其余（含 `/generate_204`、`/hotspot-detect.html` 等） | 302 重定向到 `http://<AP IP>/`，触发手机 Captive Portal 弹页 |

> 与既有 STA 局域网 Web 配置服务（`framework/http_server.py`，9 节）**相互独立**：端口同为 80 但生命周期/网段不重叠——配网服务仅 AP 模式期间存在，收摊即关；`http_server` 专职已联网后的局域网配置。未复用前者是为保持其职责单一并避免 AP/STA 双态路由耦合。

## 六、DNS 劫持（Captive Portal 原理）

- UDP 53 端口非阻塞 socket，仅应答 QDCOUNT=1 的标准查询：事务 ID 回显 + 标志 0x8180 + Question 段原样回显 + 指针式 A 记录（0xC00C，TTL 10s）指向 AP IP；
- 手机连上热点后，系统连通性探测域名被劫持到本机 → Android/iOS 判定"需登录"自动弹出配网页（或通知栏入口），无需手动打开浏览器；
- TTL 取 10s：换网后探测能及时刷新，不留陈旧缓存。

## 七、数据依赖与降级

| 依赖 | 缺失表现 |
|------|----------|
| `network` AP/STA（真机固件） | `start_provisioning()` 失败 → 屏显 "AP START FAIL" 1.5s 返回 (False, None)。v1.8.8 已修常见诱因（未激活接口 `config()` 被 NOT INIT 拒绝，见第二节时序约束），若再遇此提示多为驱动层异常，可查串口 `[PROV] start error:` 日志 |
| `framework/wifi.py`（WiFiConnector/单例 WLAN） | `_api_save` 返回 `{ok:false,msg:"no wifi"}`，不落盘 |
| `framework/settings.py` | 凭据不落盘；热点名后缀回退 "0000" |
| `framework/qrcode.py` | 二维码区显示 "QR ERROR"（帮助页仍可用） |
| 蜂鸣器 | 静音，不影响流程 |
| SN（`project.device_sn`） | 开机 `boot.py` 已 `ensure_device_sn()`，正常必有值 |

## 八、验证与回归

- `python tools/_sim_provision.py`：桌面仿真回归 24 项断言——设备端五态页面截图（`tools/shots/sim_provision_{qr,help,connecting,success,failed}.png`）、HTTP 路由（scan/save/status/探测 302/404/连接中拒扫）、凭据合并（插首/去重/上限 5）、DNS 应答（事务 ID/标志/A 记录 IP/畸形丢弃）、**AP 启动生命周期（含建模「未激活接口 config 抛 NOT INIT」约束的 WLAN 桩，锁死先 active 后 config 顺序——旧顺序下该组 3 项断言 FAIL 并复现 `WIFI NOT INIT`）**；
- PC 无法仿真 AP/DNS/STA 实连，**真机验收清单**：① 删空 settings.json wifi 后上电 → 自动进配网页，**热点正常出现（不得再报 AP START FAIL，v1.8.8 回归点）**；② 手机连 `XianMuBox-XXXX` 是否自动弹页（不弹则扫二维码）；③ 选网提交 → 设备屏与网页同步状态 → 成功自动进菜单；④ 密码错误 → Failed → B 重试；⑤ C 退出后热点消失、离线菜单正常；⑥ 设置菜单「WiFi配网」入口随时可用。

## 九、已知限制

- 开放热点为行业常见做法（配网期短暂存在、收摊即关）；如需防蹭网可后续加固定密码（屏显密码方案已预留）；
- 凭据经 AP 明文 HTTP 提交，与主流配网方案一致（配网页内容不含敏感回显）；
- 部分 Android 机型对开放热点的 Captive Portal 探测激进度不同，弹页不保证 100% 触发——二维码扫码为兜底入口（帮助页同时给出 URL）；
- 隐藏 SSID 热点不会出现在扫描列表，需在配网页手动输入 SSID（已支持）。

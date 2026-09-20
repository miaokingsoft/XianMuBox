"""
多功能终端框架 - 颜色调色板
闲沐工坊 · 插件式架构

单一事实源：整个项目的颜色常量统一在此定义。
所有模块（menu / modes / boot_animations）均从此文件导入颜色值，
严禁在各模块内重复定义颜色常量。
"""

# ===== iOS 暗色模式调色板 =====
CLR_BG      = 0x0000   # 纯黑背景
CLR_DASH_BG = 0x39C7   # 仪表盘内容区背景 (#383838)
CLR_CARD    = 0x18C3   # 卡片底色（深蓝灰）
CLR_CARD_HI = 0x294B   # 卡片顶部高光（1px 提亮）
CLR_CARD_SH = 0x1082   # 卡片底部阴影（1px 压暗）
CLR_WHITE   = 0xFFFF   # 白色文字 / 描边
CLR_GRAY    = 0x8410   # 中性灰（未选中文字）
CLR_DIM     = 0x4208   # 极暗（装饰 / 未激活元素）
CLR_CYAN    = 0x07FF   # 青色（激活态 / 装饰）
CLR_BLUE    = 0x033F   # 蓝色（标题栏）
CLR_GREEN   = 0x07E0   # 纯绿（盈利 / 已连接）
CLR_RED     = 0xF800   # 纯红（亏损 / 未连接 / 警告）
CLR_AMBER   = 0xFD20   # 琥珀（低额警告）
CLR_ORANGE  = 0xFBE0   # 橙黄
CLR_MAGENTA = 0xF81F   # 品红
CLR_BLACK   = 0x0000   # 纯黑（二维码深色模块，与 CLR_BG 同值但语义独立）

# ===== Carousel 轮播菜单专属配色 =====
CLR_CAROUSEL_BG      = 0x18E3   # 轮播背景
CLR_CAROUSEL_HEADER  = 0x0A75   # 轮播头部栏
CLR_CAROUSEL_DIVIDER = 0x1996   # 轮播分隔线
CLR_CAROUSEL_ACCENT  = 0xB5FC   # 轮播强调色
CLR_CAROUSEL_CHEV    = 0xC618   # 轮播箭头色

# ===== AI 余额模式专属配色 (按 photo/Deepseek.svg) =====
CLR_LIME   = 0xA6A7   # 柠檬绿头部栏 (#A5D63F)
CLR_DARK   = 0x3186   # 深灰背景    (#333333)
CLR_CHEV   = 0xFFFF   # 箭头白色    (#FFFFFF) - 高对比度，清晰可见
CLR_DOT_OFF = 0xCE59  # 未选分页方块 (#CCCCCC)

# ===== 持仓股票模式专属配色（金融终端深色版）=====
CLR_STK_BG          = 0x0000   # 主背景（纯黑）
CLR_STK_HDR         = 0xF800   # 红色标题栏（= CLR_RED）
CLR_STK_UP          = 0xF800   # 涨：红色柱 / 红字价格（= CLR_RED）
CLR_STK_DN          = 0x033F   # 跌：深蓝柱 / 蓝字价格（= CLR_BLUE）
CLR_STK_GAIN        = 0xF800   # 盈利（红字）(= CLR_RED)
CLR_STK_LOSS        = 0x07E0   # 亏损（绿字）(= CLR_GREEN)
CLR_STK_TAG         = 0xFFE0   # 当前价 ▼ 标记（柠檬黄）
CLR_STK_FCAST       = 0xF81F   # AI 预测 K 线颜色（品红 = CLR_MAGENTA）
CLR_STK_PAGEDOT     = 0xFFE0   # 分页指示点：当前=黄
CLR_STK_PAGEDOT_OFF = 0x4208   # 分页指示点：其它=暗灰
CLR_STK_BTN_BG      = 0xFFFF   # 按钮底色（白）
CLR_STK_BTN_FG      = 0xF800   # 按钮文字（红）
CLR_STK_BTN_BD      = 0xF800   # 按钮描边（红）
CLR_STK_BRACKET     = 0xFFFF   # 「」括号（白）
CLR_STK_TXT_DIM     = 0x8410   # 暗文字（Pos: / P&L: 标签 / 日期）(= CLR_GRAY)
CLR_STK_TXT_HI      = 0xFFFF   # 高亮文字（数值 / 标题）(= CLR_WHITE)
CLR_STK_GRID        = 0x2104   # K 线网格线 / 对比条轨道（暗灰）
CLR_STK_UP_DIM      = 0x7800   # 成交量涨柱（暗红, r=15=半亮 CLR_STK_UP）
CLR_STK_DN_DIM      = 0x019F   # 成交量跌柱（暗蓝, 半亮 CLR_STK_DN）

# ===== 背单词模式专属配色（移植自源项目 lib/words）=====
CLR_VOCAB_HDR        = 0xFE00   # 标题栏黄 (#FFC300)
CLR_VOCAB_BTN_ACTIVE = 0xFF47   # 按钮激活黄 (#FFEB3B)
CLR_VOCAB_DEF        = 0xA6A7   # 中文释义绿 (#A5D63F) = CLR_LIME
CLR_VOCAB_COUNTER    = 0x73AE   # 页码灰 (#777777)
CLR_VOCAB_PROGBG     = 0x1082   # 进度条底 (#111111) = CLR_CARD_SH

# ===== 自媒统计模式专属配色 =====
CLR_SM_BG      = 0x0000   # 主背景（纯黑，两页一致）
CLR_SM_HDR_BG  = 0x07E0   # 详情页绿色顶栏 (= CLR_GREEN)
CLR_SM_HDR_FG  = 0x07FF   # 详情页顶栏标题青色 (= CLR_CYAN)
CLR_SM_ACCENT  = 0x07FF   # 轮播页分页点当前色 / 装饰 (= CLR_CYAN)
CLR_SM_DIV     = 0x07FF   # 轮播页顶栏分隔线 (= CLR_CYAN)
CLR_SM_CHEV    = 0xC618   # 左/右 chevron 浅白
CLR_SM_DIM     = 0x4208   # 分页点未选中 (= CLR_DIM)
CLR_SM_TXT_LBL = 0x8410   # 数据标签灰字 (= CLR_GRAY)
CLR_SM_TXT_VAL = 0xFFFF   # 数据数值白字 (= CLR_WHITE)

# ===== 天天中奖模式专属配色 =====
CLR_LOT_BLUE = 0x1BBA   # 蓝球 / 后区球 (#1976D2)
CLR_LOT_GOLD = 0xFFE0   # 中奖金额 / 强调 (柠檬黄)

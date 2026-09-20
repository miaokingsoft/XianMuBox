"""
多功能终端框架 - 天气服务
闲沐工坊 · 心知天气 v3 API 抽象层

数据源:
  - 心知天气 v3    https://www.seniverse.com
  - 本地缓存兜底   settings.json fallback

注意: 心知天气免费版 weather/now 接口仅返回 text/code/temperature
      付费版才会返回 humidity/pressure/wind_speed 等字段
      此模块在免费版下 humidity/aqi/uv 均为 None

配置 (settings.json):
  weather:
    city: "武汉"             # 中文城市名
    api_key: "YOUR_KEY"     # 心知天气 API Key
    # 兜底(网络失败时使用)
    fallback:
      city: "武汉"
      text: "多云"
      code: "cloudy"
      temp: 25

返回值 (字典):
  {
    "ok": True/False,
    "city": "武汉",
    "text": "多云",
    "code": "cloudy",       # 天气代码: sunny/cloudy/rain/snow/fog/thunder
    "temp": 25,             # 摄氏度
    "humidity": None,       # 免费版无此字段
    "aqi": None,            # 免费版无此字段
    "aqi_label": "--",
    "uv": None,             # 免费版无此字段
    "uv_label": "--",
    "last_update": "2026-06-27T23:30:08+08:00",  # 心知API返回的本地时间字符串
    "ts": 1690000000,       # 数据时间戳(ms)
    "source": "seniverse",  # 数据来源
  }
"""

import time
import gc
try:
    import ujson as json
except ImportError:
    import json

try:
    import urequests
except ImportError:
    urequests = None


# ===== 天气代码映射 =====
# 心知天气 code_name -> 内部代码
_CODE_MAP = {
    "sunny":         "sunny",
    "cloudy":        "cloudy",
    "partly_cloudy": "cloudy",
    "overcast":      "cloudy",
    "light_rain":    "rain",
    "moderate_rain": "rain",
    "heavy_rain":    "rain",
    "storm_rain":    "rain",
    "thunder_shower":"thunder",
    "light_snow":    "snow",
    "moderate_snow": "snow",
    "heavy_snow":    "snow",
    "fog":           "fog",
    "haze":          "fog",
}

# 心知天气数字 code 0-30 -> 内部代码
_CODE_DIGIT_MAP = {
    "0": "sunny", "1": "sunny", "2": "sunny", "3": "sunny",
    "4": "cloudy", "5": "cloudy", "6": "cloudy", "7": "cloudy",
    "8": "cloudy", "9": "cloudy",
    "10": "rain", "11": "rain", "12": "rain", "13": "rain",
    "14": "rain", "15": "rain", "16": "rain",
    "17": "thunder", "18": "thunder",
    "19": "fog", "20": "fog", "21": "fog", "22": "fog",
    "23": "fog", "24": "fog", "25": "fog",
    "26": "snow", "27": "snow", "28": "snow", "29": "snow",
}


def _normalize_code(code):
    """根据心知天气的 code 字符串/数字返回内部代码"""
    if not code:
        return "cloudy"
    code_str = str(code)
    if code_str in _CODE_DIGIT_MAP:
        return _CODE_DIGIT_MAP[code_str]
    return _CODE_MAP.get(code_str, "cloudy")


# ===== 紫外线指数评级 =====
def _uv_label(uv):
    if uv is None:
        return None, "--"
    if uv <= 2:
        return uv, "弱"
    elif uv <= 5:
        return uv, "中等"
    elif uv <= 7:
        return uv, "强"
    elif uv <= 10:
        return uv, "很强"
    else:
        return uv, "极强"


def _aqi_label(aqi):
    if aqi is None:
        return None, "--"
    if aqi <= 50:
        return aqi, "优"
    elif aqi <= 100:
        return aqi, "良"
    elif aqi <= 150:
        return aqi, "差"
    elif aqi <= 200:
        return aqi, "差"
    elif aqi <= 300:
        return aqi, "极差"
    else:
        return aqi, "爆表"


# ===== 默认兜底数据 =====
def _fallback_data(cfg):
    """网络失败时使用本地配置"""
    fb = cfg.get("fallback", {}) if isinstance(cfg, dict) else {}
    cfg_city = cfg.get("city", "深圳") if isinstance(cfg, dict) else "深圳"
    return {
        "ok": False,
        "city": fb.get("city", cfg_city),
        "text": fb.get("text", "多云"),
        "code": fb.get("code", "cloudy"),
        "code_num": "1",
        "temp": fb.get("temp", 25),
        "humidity": None,
        "aqi": None,
        "aqi_label": "--",
        "uv": None,
        "uv_label": "--",
        "ts": time.time() * 1000,
        "last_update": None,
        "source": "fallback",
    }


# ===== 心知天气 v3 =====
def fetch_seniverse(cfg):
    """
    心知天气 v3 API (实况天气)
    文档: https://docs.seniverse.com

    URL: https://api.seniverse.com/v3/weather/now.json
    参数: key=API_KEY, location=city(支持中文/拼音), language=zh-Hans, unit=c

    响应:
    {
      "results": [{
        "location": {"id": "...", "name": "武汉"},
        "now": {
          "text": "多云",
          "code": "4",
          "temperature": "23"
          # 付费版才会有: humidity, pressure, wind_speed, feels_like ...
        },
        "last_update": "2026-06-27T23:30:08+08:00"
      }]
    }

    注: 免费版 now 仅返回 text/code/temperature
    """
    if urequests is None:
        print("[WEATHER] urequests not available")
        return None
    api_key = cfg.get("api_key", "")
    if not api_key:
        print("[WEATHER] API key missing")
        return None

    # 直接使用中文城市名（心知 API 原生支持中文/拼音/英文）
    location = cfg.get("city", "深圳")
    if not location:
        print("[WEATHER] city not configured")
        return None

    # URL 编码城市名（中文需编码）
    try:
        import urllib.parse as _urlparse
        location_enc = _urlparse.quote(location)
    except Exception:
        # MicroPython 无 urllib.parse 时, 中文URL编码处理
        location_enc = location

    url = "https://api.seniverse.com/v3/weather/now.json?key=%s&location=%s&language=zh-Hans&unit=c" % (
        api_key, location_enc)

    try:
        gc.collect()
        print("[WEATHER] GET", location)
        resp = urequests.get(url, timeout=5)
        status = resp.status_code
        print("[WEATHER] status=", status)
        if status != 200:
            try:
                resp.close()
            except Exception:
                pass
            return None
        data = resp.json()
        try:
            resp.close()
        except Exception:
            pass

        results = data.get("results", [])
        if not results:
            print("[WEATHER] no results in response")
            return None

        result = results[0]
        loc = result.get("location", {})
        now = result.get("now", {})

        text = now.get("text", "晴")
        code = now.get("code", "0")
        norm_code = _normalize_code(code)

        # 温度(必返回)
        try:
            temp = int(float(now.get("temperature", "25")))
        except Exception:
            temp = 25

        # 湿度(免费版无此字段, 付费版才返回)
        humidity = None
        humidity_raw = now.get("humidity")
        if humidity_raw is not None:
            try:
                humidity = int(float(str(humidity_raw).rstrip("%")))
            except Exception:
                humidity = None

        # AQI/UV: weather/now 不提供, 保持 None
        uv_n, uv_l = _uv_label(None)
        aqi_n, aqi_l = _aqi_label(None)

        # 心知API返回的本地时间字符串
        last_update = result.get("last_update")

        return {
            "ok": True,
            "city": loc.get("name", location),
            "text": text,
            "code": norm_code,
            "code_num": str(code),
            "temp": temp,
            "humidity": humidity,
            "aqi": aqi_n,
            "aqi_label": aqi_l,
            "uv": uv_n,
            "uv_label": uv_l,
            "ts": time.time() * 1000,
            "last_update": last_update,
            "source": "seniverse",
        }
    except Exception as e:
        print("[WEATHER] Seniverse error:", e)
        return None


# ===== 主入口 =====
_CACHE = None
_CACHE_TS = 0
_CACHE_TTL = 600  # 10分钟缓存


def get_weather(force_refresh=False):
    """
    获取天气数据 (含缓存)
    返回值参见文件头注释
    """
    global _CACHE, _CACHE_TS

    # 检查缓存
    now = time.time()
    if not force_refresh and _CACHE and (now - _CACHE_TS) < _CACHE_TTL:
        return _CACHE

    # 读取配置
    try:
        from framework import settings as _settings
        cfg = _settings.get("weather", {})
    except Exception:
        cfg = {}

    if not isinstance(cfg, dict):
        cfg = {}

    # 直接调用心知天气
    data = fetch_seniverse(cfg)

    # 失败时使用兜底数据
    if data is None:
        data = _fallback_data(cfg)

    _CACHE = data
    _CACHE_TS = now
    return data


def invalidate_cache():
    """清除缓存 (强制下次重新获取)"""
    global _CACHE, _CACHE_TS
    _CACHE = None
    _CACHE_TS = 0


# ===== 内置城市列表 (供前端下拉选择) =====
# 字段: id (中文), name (拼音/英文), lat, lon, province
CITIES = (
    # 直辖市
    {"id": "北京",   "name": "Beijing",  "lat": 39.9042, "lon": 116.4074, "province": "直辖市"},
    {"id": "上海",   "name": "Shanghai", "lat": 31.2304, "lon": 121.4737, "province": "直辖市"},
    {"id": "天津",   "name": "Tianjin",  "lat": 39.3434, "lon": 117.3616, "province": "直辖市"},
    {"id": "重庆",   "name": "Chongqing","lat": 29.4316, "lon": 106.9123, "province": "直辖市"},
    # 省会与主要城市
    {"id": "广州",   "name": "Guangzhou","lat": 23.1291, "lon": 113.2644, "province": "广东"},
    {"id": "深圳",   "name": "Shenzhen", "lat": 22.5431, "lon": 114.0579, "province": "广东"},
    {"id": "杭州",   "name": "Hangzhou", "lat": 30.2741, "lon": 120.1551, "province": "浙江"},
    {"id": "南京",   "name": "Nanjing",  "lat": 32.0603, "lon": 118.7969, "province": "江苏"},
    {"id": "苏州",   "name": "Suzhou",   "lat": 31.2989, "lon": 120.5853, "province": "江苏"},
    {"id": "成都",   "name": "Chengdu",  "lat": 30.5728, "lon": 104.0668, "province": "四川"},
    {"id": "武汉",   "name": "Wuhan",    "lat": 30.5928, "lon": 114.3055, "province": "湖北"},
    {"id": "西安",   "name": "Xi'an",    "lat": 34.3416, "lon": 108.9398, "province": "陕西"},
    {"id": "长沙",   "name": "Changsha", "lat": 28.2282, "lon": 112.9388, "province": "湖南"},
    {"id": "郑州",   "name": "Zhengzhou","lat": 34.7466, "lon": 113.6253, "province": "河南"},
    {"id": "济南",   "name": "Jinan",    "lat": 36.6512, "lon": 117.1201, "province": "山东"},
    {"id": "青岛",   "name": "Qingdao",  "lat": 36.0671, "lon": 120.3826, "province": "山东"},
    {"id": "合肥",   "name": "Hefei",    "lat": 31.8206, "lon": 117.2272, "province": "安徽"},
    {"id": "福州",   "name": "Fuzhou",   "lat": 26.0745, "lon": 119.2965, "province": "福建"},
    {"id": "厦门",   "name": "Xiamen",   "lat": 24.4798, "lon": 118.0894, "province": "福建"},
    {"id": "南昌",   "name": "Nanchang", "lat": 28.6820, "lon": 115.8579, "province": "江西"},
    {"id": "昆明",   "name": "Kunming",  "lat": 25.0389, "lon": 102.7183, "province": "云南"},
    {"id": "贵阳",   "name": "Guiyang",  "lat": 26.6470, "lon": 106.6302, "province": "贵州"},
    {"id": "南宁",   "name": "Nanning",  "lat": 22.8170, "lon": 108.3665, "province": "广西"},
    {"id": "海口",   "name": "Haikou",   "lat": 20.0444, "lon": 110.1989, "province": "海南"},
    {"id": "太原",   "name": "Taiyuan",  "lat": 37.8706, "lon": 112.5489, "province": "山西"},
    {"id": "兰州",   "name": "Lanzhou",  "lat": 36.0611, "lon": 103.8343, "province": "甘肃"},
    {"id": "西宁",   "name": "Xining",   "lat": 36.6232, "lon": 101.7804, "province": "青海"},
    {"id": "银川",   "name": "Yinchuan", "lat": 38.4872, "lon": 106.2309, "province": "宁夏"},
    {"id": "乌鲁木齐","name": "Urumqi",  "lat": 43.8256, "lon": 87.6168,  "province": "新疆"},
    {"id": "拉萨",   "name": "Lhasa",    "lat": 29.6500, "lon": 91.1700,  "province": "西藏"},
    {"id": "哈尔滨", "name": "Harbin",   "lat": 45.8038, "lon": 126.5340, "province": "黑龙江"},
    {"id": "长春",   "name": "Changchun","lat": 43.8868, "lon": 125.3245, "province": "吉林"},
    {"id": "沈阳",   "name": "Shenyang", "lat": 41.8057, "lon": 123.4315, "province": "辽宁"},
    {"id": "大连",   "name": "Dalian",   "lat": 38.9140, "lon": 121.6147, "province": "辽宁"},
    {"id": "呼和浩特","name": "Hohhot",  "lat": 40.8425, "lon": 111.7491, "province": "内蒙古"},
    # 国际 (英文名+经纬度)
    {"id": "香港",   "name": "Hong Kong","lat": 22.3193, "lon": 114.1694, "province": "特别行政区"},
    {"id": "澳门",   "name": "Macau",    "lat": 22.1987, "lon": 113.5439, "province": "特别行政区"},
    {"id": "台北",   "name": "Taipei",   "lat": 25.0330, "lon": 121.5654, "province": "台湾"},
    {"id": "东京",   "name": "Tokyo",    "lat": 35.6762, "lon": 139.6503, "province": "海外"},
    {"id": "首尔",   "name": "Seoul",    "lat": 37.5665, "lon": 126.9780, "province": "海外"},
    {"id": "新加坡", "name": "Singapore","lat": 1.3521,  "lon": 103.8198, "province": "海外"},
    {"id": "纽约",   "name": "New York", "lat": 40.7128, "lon": -74.0060, "province": "海外"},
    {"id": "伦敦",   "name": "London",   "lat": 51.5074, "lon": -0.1278,  "province": "海外"},
)


def find_city(city_id):
    """
    通过中文id查找城市

    参数: city_id 中文名, 例如 "武汉"
    返回: 字典 {"name": "Wuhan", "lat": ..., "lon": ...} 或 None
    """
    for c in CITIES:
        if c["id"] == city_id:
            return {
                "name": c["name"],
                "lat": c["lat"],
                "lon": c["lon"],
            }
    return None


def get_current_city():
    """获取当前配置的城市 (从 settings)"""
    try:
        from framework import settings as _settings
        cfg = _settings.get("weather", {})
        if isinstance(cfg, dict):
            return cfg.get("city", "深圳")
    except Exception:
        pass
    return "深圳"
"""
场地坐标映射（M3，2026-11-08）
=====================================================================
把车端影子上报的「逻辑坐标」(x, y) 映射成有物理含义的场地坐标，并且在需要时
输出合规地理坐标。设计上有三条硬约束：

1. **不碰在线地图数据**：默认底图是本地自绘矢量场地（field.json 里的 zones /
   geofence / waypoints），不加载任何在线瓦片、不画国界与行政区划。
2. **经纬度默认不输出**：geo.calibrated=false 时 geo 一律为 None，避免把示例
   原点 (0,0) 当成真实位置。只有现场用合规服务商采集基准点并显式校准后才输出，
   且坐标系固定 GCJ-02（火星坐标），与腾讯位置服务一致。
3. **配置热加载**：改 field.json 后下一请求立即生效，管理台或课堂上不用重启后端。

坐标约定（与车端 / canvas 完全一致）：
  逻辑坐标   x 向右、y 向下，左上角 (0,0)，单位 px
  场地米制   east / north：以场地原点为 (0,0)，向东/向北为正，单位 m
  地理坐标   由 east/north 经局部平面近似 + 方位旋转换算成 GCJ-02 经纬度

纯函数部分（meters / geo / inside / zone_at）不依赖 Flask，方便单独跑单测。
"""

import math
import os
import threading

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "field.json")

# 局部平面近似：低纬中纬地区 1° 纬度 ≈ 110540 m，1° 经度 ≈ 111320·cos(lat) m
_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LNG = 111320.0

_lock = threading.Lock()
_cached = {"path": None, "mtime": None, "map": None}


def _clean(obj):
    """递归丢掉以下划线开头的注释字段，避免把内部说明发给前端。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def point_in_polygon(x, y, polygon):
    """射线法判断点是否在多边形内（多边形首尾不要求闭合）。"""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _dist_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class FieldMap:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        lg = self.cfg.get("logical") or {}
        self.width = float(lg.get("width", 800))
        self.height = float(lg.get("height", 600))
        self.north_deg = float(lg.get("north_deg", 0))
        self.meters_per_unit = float((self.cfg.get("scale") or {}).get("meters_per_unit", 1.0))
        gf = self.cfg.get("geofence") or {}
        self.geofence_enabled = bool(gf.get("enabled", True))
        self.geofence_margin = float(gf.get("margin", 0))
        self.polygon = gf.get("polygon") or [[0, 0], [self.width, 0],
                                             [self.width, self.height], [0, self.height]]
        self.zones = self.cfg.get("zones") or []
        self.waypoints = self.cfg.get("waypoints") or []
        geo = self.cfg.get("geo") or {}
        self.geo_enabled = bool(geo.get("enabled")) and bool(geo.get("calibrated"))
        self.geo_datum = geo.get("datum") or "GCJ-02"
        self.origin_lng = float(geo.get("origin_lng") or 0.0)
        self.origin_lat = float(geo.get("origin_lat") or 0.0)
        self.rotation_deg = float(geo.get("rotation_deg") or 0.0)

    # ------------------------------------------------------------ 坐标换算
    def meters(self, x, y):
        """逻辑坐标 → 场地米制坐标 (east, north)，北为正、东为正。"""
        if x is None or y is None:
            return None, None
        east = float(x) * self.meters_per_unit
        north = -float(y) * self.meters_per_unit
        return east, north

    def geo(self, x, y):
        """场地米制坐标 → GCJ-02 经纬度；未校准时一律返回 None。"""
        east, north = self.meters(x, y)
        if east is None or not self.geo_enabled:
            return None
        r = math.radians(self.rotation_deg)
        e = east * math.cos(r) - north * math.sin(r)
        n = east * math.sin(r) + north * math.cos(r)
        lat = self.origin_lat
        lng = self.origin_lng + e / (_M_PER_DEG_LNG * max(math.cos(math.radians(lat)), 1e-6))
        lat = lat + n / _M_PER_DEG_LAT
        return {"lng": round(lng, 7), "lat": round(lat, 7), "datum": self.geo_datum}

    def inside(self, x, y):
        """是否在安全边界内（含内缩 margin）。geofence.enabled=false 时恒为 True。"""
        if x is None or y is None:
            return True                       # 影子没坐标时不因为映射而误拦命令
        if not self.geofence_enabled:
            return True
        x, y = float(x), float(y)
        if not point_in_polygon(x, y, self.polygon):
            return False
        if self.geofence_margin <= 0:
            return True
        poly = self.polygon
        for i in range(len(poly)):
            a = poly[i]
            b = poly[(i + 1) % len(poly)]
            if _dist_to_segment(x, y, a[0], a[1], b[0], b[1]) < self.geofence_margin:
                return False
        return True

    def zone_at(self, x, y):
        """返回点所在的区域（禁行区优先），没有则返回 None。"""
        if x is None or y is None:
            return None
        hit = None
        for z in self.zones:
            pts = z.get("points") or []
            if len(pts) >= 3 and point_in_polygon(float(x), float(y), pts):
                if z.get("type") == "restricted":
                    return z
                hit = hit or z
        return hit

    def position(self, x, y):
        """给 /status、/fleet_status 用的统一位置摘要。"""
        if x is None or y is None:
            return {"x": x, "y": y, "mx": None, "my": None,
                    "inside": True, "zone": None, "restricted": False, "geo": None}
        east, north = self.meters(x, y)
        z = self.zone_at(x, y)
        return {
            "x": float(x), "y": float(y),
            "mx": round(east, 3), "my": round(north, 3),
            "inside": self.inside(x, y),
            "zone": (z.get("name") or z.get("id")) if z else None,
            "restricted": bool(z and z.get("type") == "restricted"),
            "geo": self.geo(x, y),
            "unit": "m",
        }

    def distance(self, a, b):
        """两点间场地距离（米），a/b 为 (x, y) 逻辑坐标。"""
        if not a or not b or None in a or None in b:
            return None
        return round(math.hypot(a[0] - b[0], a[1] - b[1]) * self.meters_per_unit, 3)

    def public(self):
        """给前端 /field 的配置：去掉注释字段，未校准时抹掉示例原点。"""
        out = _clean(self.cfg)
        geo = out.get("geo") or {}
        if not self.geo_enabled:
            geo["origin_lng"] = None
            geo["origin_lat"] = None
        out["geo"] = geo
        out["computed"] = {
            "width_m": round(self.width * self.meters_per_unit, 3),
            "height_m": round(self.height * self.meters_per_unit, 3),
            "geo_ready": self.geo_enabled,
        }
        return out


def get(path=None):
    """按 mtime 热加载单例。文件缺失/损坏时退化为「800×600 无边界」的中性场地。"""
    p = path or DEFAULT_PATH
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = None
    with _lock:
        if _cached["map"] is None or _cached["path"] != p or _cached["mtime"] != mt:
            cfg = {}
            if mt is not None:
                try:
                    import json
                    with open(p, encoding="utf-8") as f:
                        cfg = json.load(f)
                except (OSError, ValueError):
                    cfg = {}
            neutral = {"logical": {"width": 800, "height": 600},
                       "scale": {"meters_per_unit": 1.0},
                       "geofence": {"enabled": False}, "geo": {"enabled": False},
                       "compliance": {"statement": "未读取到 field.json，退回中性局部坐标系"}}
            neutral.update(cfg)
            _cached.update(path=p, mtime=mt, map=FieldMap(neutral))
        return _cached["map"]


if __name__ == "__main__":
    import json
    m = get()
    print(json.dumps(m.public(), ensure_ascii=False, indent=2))
    for pt in [(0, 0), (100, 100), (400, 300), (400, 450), (799, 599)]:
        print(pt, json.dumps(m.position(*pt), ensure_ascii=False))

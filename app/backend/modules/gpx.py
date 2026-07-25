"""GPX 导入：解析轨迹 → 均匀采样 + 高低点识别 → Route。
GPX 是标准 XML，只依赖标准库。"""
import math
import uuid
import xml.etree.ElementTree as ET
from typing import List, Tuple

from models import Route, Waypoint

_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _parse_points(xml_text: str) -> List[Tuple[float, float, float]]:
    """返回 [(lat, lon, ele), ...]，兼容有/无命名空间的 GPX。"""
    root = ET.fromstring(xml_text)
    pts = []
    for tag in ("trkpt", "rtept"):
        nodes = root.findall(".//gpx:{}".format(tag), _NS) or root.findall(".//{}".format(tag))
        for n in nodes:
            lat, lon = float(n.get("lat")), float(n.get("lon"))
            ele_node = n.find("gpx:ele", _NS)
            if ele_node is None:
                ele_node = n.find("ele")
            ele = float(ele_node.text) if ele_node is not None and ele_node.text else 0.0
            pts.append((lat, lon, ele))
        if pts:
            break
    return pts


def gpx_to_route(xml_text: str, name: str, activity: str, days: int) -> Route:
    pts = _parse_points(xml_text)
    if len(pts) < 2:
        raise ValueError("GPX 中未找到轨迹点")

    total_km, ascent = 0.0, 0.0
    for i in range(1, len(pts)):
        total_km += _haversine_km(pts[i - 1][:2], pts[i][:2])
        gain = pts[i][2] - pts[i - 1][2]
        if gain > 0:
            ascent += gain

    # 采样策略：起终点 + 全程最高点 + 每日一个均匀分段点（上限 10 点，控制 API 用量）
    n_samples = min(10, max(4, days * 2))
    idxs = {0, len(pts) - 1, max(range(len(pts)), key=lambda i: pts[i][2])}
    for k in range(1, n_samples - 1):
        idxs.add(int(len(pts) * k / (n_samples - 1)))
    ordered = sorted(idxs)

    waypoints = []
    hi_idx = max(range(len(pts)), key=lambda i: pts[i][2])
    for rank, i in enumerate(ordered):
        lat, lon, ele = pts[i]
        if i == 0:
            kind, wname = "start", "起点"
        elif i == len(pts) - 1:
            kind, wname = "finish", "终点"
        elif i == hi_idx:
            kind, wname = "pass", "全程最高点（{:.0f}m）".format(ele)
        else:
            kind, wname = ("camp" if activity == "hiking" else "aid"), "途经点 {}".format(rank)
        day = min(days, 1 + int(rank * days / len(ordered)))
        waypoints.append(Waypoint(
            id="g{}".format(rank), name=wname, lat=round(lat, 5), lon=round(lon, 5),
            elevation=int(ele), kind=kind, day=day,
            risk="海拔最高，风与低温风险集中" if i == hi_idx else "",
        ))

    return Route(
        id="gpx_" + uuid.uuid4().hex[:8], name=name, activity=activity,
        region="GPX 导入", days=days, distance_km=round(total_km, 1),
        ascent_m=int(ascent), difficulty="以实际轨迹为准",
        summary="从 GPX 轨迹导入，自动采样 {} 个关键点".format(len(waypoints)),
        waypoints=waypoints, source="gpx",
    )

"""轨迹导入：解析 GPX / KML / KMZ → 均匀采样 + 高低点识别 → Route。
三种格式都是 XML（KMZ 是 zip 包的 KML），只依赖标准库。"""
import io
import math
import uuid
import xml.etree.ElementTree as ET
import zipfile
from typing import List, Tuple

from models import Route, Waypoint

_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _parse_points(xml_data) -> List[Tuple[float, float, float]]:
    """返回 [(lat, lon, ele), ...]，兼容有/无命名空间的 GPX。"""
    root = ET.fromstring(xml_data)
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


def _parse_kml_points(xml_data) -> List[Tuple[float, float, float]]:
    """解析 KML 轨迹点，返回 [(lat, lon, ele), ...]。
    兼容两种存法（都是 lon,lat[,ele] 顺序，注意与 GPX 相反）：
    - LineString/<coordinates>：空白分隔的 "lon,lat,ele" 串；
    - gx:Track/<gx:coord>：每点一个节点，空格分隔 "lon lat ele"。
    忽略命名空间前缀，两步路/六只脚/Google Earth 导出均可解。"""
    root = ET.fromstring(xml_data)
    segments: List[List[Tuple[float, float, float]]] = []
    gx_track: List[Tuple[float, float, float]] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "coordinates" and el.text:
            seg = []
            for tok in el.text.split():
                parts = tok.split(",")
                if len(parts) < 2:
                    continue
                ele = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
                seg.append((float(parts[1]), float(parts[0]), ele))
            if seg:
                segments.append(seg)
        elif tag == "coord" and el.text:
            parts = el.text.split()
            if len(parts) >= 2:
                ele = float(parts[2]) if len(parts) > 2 else 0.0
                gx_track.append((float(parts[1]), float(parts[0]), ele))
    if gx_track:
        return gx_track
    # 优先拼接多点线段（轨迹）；全是单点 Placemark 时按文档顺序串成路线
    lines = [s for s in segments if len(s) >= 2]
    if lines:
        return [p for s in lines for p in s]
    return [p for s in segments for p in s]


def track_to_route(data: bytes, filename: str, name: str,
                   activity: str, days: int) -> Route:
    """统一入口：按文件内容/后缀分流 GPX / KML / KMZ，解出轨迹点后建 Route。"""
    fname = (filename or "").lower()
    if data[:2] == b"PK" or fname.endswith(".kmz"):
        # KMZ = zip 包，取包内第一个 .kml（规范名 doc.kml）
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            kmls = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ValueError("KMZ 包内未找到 KML 文件")
            data = zf.read(kmls[0])
        pts = _parse_kml_points(data)
    elif fname.endswith(".kml") or b"<kml" in data[:2048].lower():
        pts = _parse_kml_points(data)
    else:
        pts = _parse_points(data)
    return _points_to_route(pts, name, activity, days)


def gpx_to_route(xml_text: str, name: str, activity: str, days: int) -> Route:
    """旧入口（仅 GPX），保留兼容。"""
    return _points_to_route(_parse_points(xml_text), name, activity, days)


def _points_to_route(pts: List[Tuple[float, float, float]], name: str,
                     activity: str, days: int) -> Route:
    if len(pts) < 2:
        raise ValueError("文件中未找到轨迹点")

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
        region="轨迹导入", days=days, distance_km=round(total_km, 1),
        ascent_m=int(ascent), difficulty="以实际轨迹为准",
        summary="从轨迹文件导入，自动采样 {} 个关键点".format(len(waypoints)),
        waypoints=waypoints, source="gpx",
    )

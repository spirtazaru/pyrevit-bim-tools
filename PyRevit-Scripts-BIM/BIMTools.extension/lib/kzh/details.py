# -*- coding: utf-8 -*-
"""Детали КЖ: библиотека гнутых стержней (хомуты, шпильки, П, Г, лягушки, корыта) с размерами,
которую пользователь собирает в «Конфигураторе деталей» и ставит щелчками по стержням в окнах
«Колонны» и «Балки».

Библиотека хранится в настройках плагина (не в проекте) — доступна в любом проекте.
Деталь — dict: name (имя — единственная подпись детали), kind, d, steel, dims {ключ: мм или None — «по месту»},
close ("hooks" | "overlap") для хомутов, hook (угол крюка 0/90/135/180), hook_len (мм, None — по Revit).
Размеры в ведомости деталей — наружные габариты участков (как пишут на чертежах КЖ).
"""
import math

from kzh import norms, settings

# Типы деталей: ключ -> (подпись, [(ключ размера, подпись, по умолчанию мм; None — «по месту»)], ставится в сечении)
KINDS = (
    ("straight", u"прямой", [("L", u"длина", 1000.0)], False),
    ("L", u"Г", [("A", u"полка", 1000.0), ("B", u"нога", 300.0)], False),
    ("U", u"П", [("A", u"спинка", 400.0), ("B", u"нога 1", 800.0), ("C", u"нога 2", 800.0)], False),
    ("stirrup", u"хомут", [("W", u"ширина", None), ("H", u"высота", None), ("lap", u"нахлёст", 200.0)], True),
    ("rhomb", u"ромб", [("W", u"ширина", None), ("H", u"высота", None)], True),
    ("pin", u"шпилька", [("L", u"длина", None)], True),
    ("frog", u"лягушка", [("A", u"полка", 150.0), ("H", u"высота", 200.0), ("B", u"верх", 300.0)], False),
    ("trough", u"корыто", [("A", u"полка", 800.0), ("H", u"глубина", 275.0), ("B", u"дно", 365.0)], False),
)
KIND_TITLES = dict((k, t) for k, t, _, _ in KINDS)
KIND_DIMS = dict((k, dims) for k, _, dims, _ in KINDS)
PLACEABLE = tuple(k for k, _, _, p in KINDS if p)

HOOK_ANGLES = (0, 90, 135, 180)

# Примеры из ведомости деталей учебного проекта (обезличенного)
DEFAULT_LIBRARY = [
    {"name": u"Хомут балки", "kind": "stirrup", "d": 12, "steel": u"А500",
     "dims": {"W": 640.0, "H": 1105.0, "lap": 200.0}, "close": "overlap", "hook": 0, "hook_len": None},
    {"name": u"Шпилька", "kind": "pin", "d": 8, "steel": u"А240",
     "dims": {"L": None}, "close": "hooks", "hook": 135, "hook_len": 75.0},
    {"name": u"Ромб колонны", "kind": "rhomb", "d": 8, "steel": u"А240",
     "dims": {"W": None, "H": None}, "close": "hooks", "hook": 135, "hook_len": None},
    {"name": u"Хомут по контуру", "kind": "stirrup", "d": 8, "steel": u"А240",
     "dims": {"W": None, "H": None, "lap": 200.0}, "close": "hooks", "hook": 135, "hook_len": None},
    {"name": u"П над балкой", "kind": "U", "d": 20, "steel": u"А500",
     "dims": {"A": 905.0, "B": 1250.0, "C": 1250.0}, "close": "hooks", "hook": 0, "hook_len": None},
    {"name": u"П-скоба края плиты", "kind": "U", "d": 16, "steel": u"А500",
     "dims": {"A": 280.0, "B": 800.0, "C": 800.0}, "close": "hooks", "hook": 0, "hook_len": None},
    {"name": u"Корыто", "kind": "trough", "d": 16, "steel": u"А500",
     "dims": {"A": 800.0, "H": 275.0, "B": 365.0}, "close": "hooks", "hook": 0, "hook_len": None},
    {"name": u"Г-стержень", "kind": "L", "d": 28, "steel": u"А500",
     "dims": {"A": 2195.0, "B": 450.0}, "close": "hooks", "hook": 0, "hook_len": None},
]


# ----------------------------------------------------------
# БИБЛИОТЕКА
# ----------------------------------------------------------
def new_detail(kind):
    return {"name": u"Новая деталь — {}".format(KIND_TITLES[kind]), "kind": kind, "d": 10,
            "steel": u"А500", "dims": dict((k, v) for k, _, v in KIND_DIMS[kind]),
            "close": "hooks", "hook": 135 if kind in ("stirrup", "rhomb", "pin") else 0, "hook_len": None}


def normalize(det):
    """Деталь с полным набором размеров своего типа (старые записи и чужие файлы)."""
    kind = det.get("kind") if det.get("kind") in KIND_TITLES else "straight"
    base = new_detail(kind)
    base.update(dict((k, v) for k, v in det.items() if k not in ("dims", "pos")))
    dims = dict(base["dims"])
    for k, v in (det.get("dims") or {}).items():
        if k in dims:
            dims[k] = v
    base["dims"] = dims
    base["kind"] = kind
    # Класс — из списка norms.json («А500С» на чертеже — класс А500)
    classes = norms.steel_classes()
    if base["steel"] not in classes:
        base["steel"] = next((c for c in classes if base["steel"].upper().startswith(c.upper())), classes[-1])
    return base


def load_library():
    items = settings.load("details").get("items")
    if not isinstance(items, list) or not items:
        return [normalize(d) for d in DEFAULT_LIBRARY]
    return [normalize(d) for d in items if isinstance(d, dict)]


def save_library(items):
    settings.save("details", {"items": items})


def find(items, name):
    return next((d for d in items if d.get("name") == name), None)


def title(det):
    """Подпись детали в списках и на рисунках — только её имя."""
    return det["name"]


# ----------------------------------------------------------
# ЭСКИЗ ДЛЯ ВЕДОМОСТИ ДЕТАЛЕЙ
# ----------------------------------------------------------
def _dim(det, key, fallback):
    v = det["dims"].get(key)
    return float(v) if v else float(fallback)


def sketch(det):
    """Эскиз детали в мм (y вверх): (полилиния, [(x, y, ключ размера, вертикально)] — места надписей размеров).
    Размеры «по месту» рисуются условной величиной."""
    k = det["kind"]
    if k == "straight":
        L = _dim(det, "L", 1000)
        return [(0, 0), (L, 0)], [(L / 2.0, 0, "L", False)]
    if k == "L":
        A, B = _dim(det, "A", 1000), _dim(det, "B", 300)
        return [(0, 0), (A, 0), (A, B)], [(A / 2.0, 0, "A", False), (A, B / 2.0, "B", True)]
    if k == "U":
        A, B, C = _dim(det, "A", 400), _dim(det, "B", 800), _dim(det, "C", 800)
        return [(B, A), (0, A), (0, 0), (C, 0)], [(B / 2.0, A, "B", False), (0, A / 2.0, "A", True), (C / 2.0, 0, "C", False)]
    if k == "stirrup":
        W, H = _dim(det, "W", 400), _dim(det, "H", 600)
        if det.get("close") == "overlap":
            # Замыкание нахлёстом: после обхода контура конец идёт вдоль первой ветви на длину нахлёста
            lap = min(_dim(det, "lap", 200), H * 0.8)
            off = W * 0.06
            return [(0, 0), (0, H), (W, H), (W, 0), (off, 0), (off, lap)],                 [(W / 2.0, H, "W", False), (0, H / 2.0, "H", True), (off, lap / 2.0, "lap", True)]
        return [(0, 0), (0, H), (W, H), (W, 0), (0, 0)], [(W / 2.0, H, "W", False), (0, H / 2.0, "H", True)]
    if k == "rhomb":
        W, H = _dim(det, "W", 400), _dim(det, "H", 400)
        return [(0, H / 2.0), (W / 2.0, H), (W, H / 2.0), (W / 2.0, 0), (0, H / 2.0)], \
            [(W / 2.0, H / 2.0, "W", False), (W / 2.0, H / 2.0 - H * 0.12, "H", False)]
    if k == "pin":
        L = _dim(det, "L", 400)
        h = 0.15 * L
        return [(-h * 0.6, h), (0, 0), (L, 0), (L + h * 0.6, h)], [(L / 2.0, 0, "L", False)]
    if k == "frog":
        A, H, B = _dim(det, "A", 150), _dim(det, "H", 200), _dim(det, "B", 300)
        return [(0, 0), (A, 0), (A, H), (A + B, H), (A + B, 0), (2 * A + B, 0)], \
            [(A / 2.0, 0, "A", False), (A, H / 2.0, "H", True), (A + B / 2.0, H, "B", False)]
    A, H, B = _dim(det, "A", 800), _dim(det, "H", 275), _dim(det, "B", 365)
    s = H * 0.35
    return [(0, H), (A, H), (A + s, 0), (A + s + B, 0), (A + 2 * s + B, H), (2 * A + 2 * s + B, H)], \
        [(A / 2.0, H, "A", False), (A + s / 2.0, H / 2.0, "H", True), (A + s + B / 2.0, 0, "B", False)]


def length(det):
    """Длина развёртки по ведомости (сумма размеров участков), мм; None — есть размеры «по месту»."""
    k, dims = det["kind"], det["dims"]
    vals = [dims.get(key) for key, _, _ in KIND_DIMS[k] if key != "lap"]
    if any(v is None for v in vals):
        return None
    v = [float(x) for x in vals]
    hook = float(det.get("hook_len") or 0.0) if det.get("hook") else 0.0
    if k == "straight":
        return v[0] + 2 * hook
    if k == "L":
        return v[0] + v[1]
    if k == "U":
        return v[0] + v[1] + v[2]
    if k == "stirrup":
        W, H = v
        if det.get("close") == "overlap":
            return 2 * W + 2 * H + float(dims.get("lap") or 0.0)
        return 2 * W + 2 * H + 2 * hook
    if k == "rhomb":
        W, H = v
        return 4 * math.hypot(W / 2.0, H / 2.0) + 2 * hook
    if k == "pin":
        return v[0] + 2 * hook
    if k == "frog":
        A, H, B = v
        return 2 * A + 2 * H + B
    A, H, B = v
    return 2 * A + 2 * H + B


def mass(det):
    L = length(det)
    m = norms.mass_per_m(det["d"]) if det.get("d") else None
    return L / 1000.0 * m if (L is not None and m) else None


# ----------------------------------------------------------
# ДЕТАЛЬ ПО ВЫБРАННЫМ СТЕРЖНЯМ СЕЧЕНИЯ
# ----------------------------------------------------------
def _hull(pts):
    """Выпуклая оболочка (против часовой стрелки)."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 1e-9:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 1e-9:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _offset(poly, dist):
    """Выпуклый многоугольник (против часовой) с отступом наружу на dist."""
    n = len(poly)
    lines = []
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1.0
        nx, ny = dy / L, -dx / L            # наружная нормаль для обхода против часовой
        lines.append(((a[0] + nx * dist, a[1] + ny * dist), (dx, dy)))
    out = []
    for i in range(n):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        den = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(den) < 1e-9:
            out.append(p2)
            continue
        t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
        q = (p1[0] + d1[0] * t, p1[1] + d1[1] * t)
        v = poly[i]
        if math.hypot(q[0] - v[0], q[1] - v[1]) > 2.0 * dist:
            # Острый угол: вершина ушла бы далеко за стержень — срез двумя точками у стержня
            L1 = math.hypot(d1[0], d1[1]) or 1.0
            out.append((v[0] + d1[1] / L1 * dist, v[1] - d1[0] / L1 * dist))
            out.append(p2)
            continue
        out.append(q)
    return out


def place_in_section(det, bars):
    """Ось детали в плоскости сечения по выбранным стержням bars [(u, v, Ø)], мм.
    Хомут и ромб охватывают выбранные стержни (выпуклая оболочка с отступом на радиусы стержней);
    шпилька — между двумя стержнями, от наружной стороны одного до наружной стороны другого.
    Возвращает (точки полилинии, замкнута, сообщение об ошибке или None)."""
    k = det["kind"]
    ds = float(det["d"])
    if k == "pin":
        if len(bars) != 2:
            return None, False, u"шпилька ставится между двумя стержнями"
        (u0, v0, d0), (u1, v1, d1) = bars
        L = math.hypot(u1 - u0, v1 - v0) or 1.0
        ex, ey = (u1 - u0) / L, (v1 - v0) / L
        a = (u0 - ex * (d0 + ds) / 2.0, v0 - ey * (d0 + ds) / 2.0)
        b = (u1 + ex * (d1 + ds) / 2.0, v1 + ey * (d1 + ds) / 2.0)
        return [a, b], False, None
    if k not in ("stirrup", "rhomb"):
        return None, False, u"в сечении ставятся хомуты, ромбы и шпильки"
    if len(bars) < 3:
        return None, False, u"хомут охватывает не меньше трёх стержней"
    hull = _hull([(u, v) for u, v, _ in bars])
    if len(hull) < 3:
        return None, False, u"выбранные стержни лежат на одной линии"
    r = max(d for _, _, d in bars) / 2.0 + ds / 2.0
    loop = _offset(hull, r)
    if det.get("close") == "overlap":
        # Нахлёст: контур не замыкается, конец заходит вдоль первой стороны на длину нахлёста со сдвигом на Ø
        lap = float(det["dims"].get("lap") or 0.0)
        a, b = loop[0], loop[1]
        L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        nx, ny = -uy, ux                    # внутрь контура
        lap = min(lap, L * 0.9)
        start = (a[0] + nx * ds, a[1] + ny * ds)
        end = (start[0] + ux * lap, start[1] + uy * lap)
        return loop + [start, end], False, None
    return loop + [loop[0]], True, None


def role(det):
    """Роль стержня для таблицы форм и стиля Revit: хомут и ромб — «stirrup», шпилька — «pin»."""
    return "pin" if det["kind"] == "pin" else "stirrup"


def apply_to(spec, det):
    """Класс, крюки и длина крюка детали -> BarSpec."""
    spec.steel = det.get("steel")
    if det["kind"] == "stirrup" and det.get("close") == "overlap":
        spec.hook_angle = 0
    else:
        spec.hook_angle = int(det.get("hook") or 0)
    spec.hook_len = det.get("hook_len")
    return spec


def placed(items, bars, lib):
    """Поставленные в сечении детали: items — [{"name", "bars": [номера]}], bars — стержни сечения [(u, v, Ø)].
    Возвращает ([(деталь, точки оси, замкнута, номер в items)], [замечания])."""
    out, notes = [], []
    for k, item in enumerate(items or []):
        det = find(lib, item.get("name"))
        if det is None:
            notes.append(u"деталь «{}» не найдена в библиотеке".format(item.get("name")))
            continue
        idx = item.get("bars") or []
        if any(i >= len(bars) for i in idx):
            notes.append(u"деталь «{}»: изменилось число стержней — поставьте её заново".format(det["name"]))
            continue
        pts, closed, err = place_in_section(det, [bars[i] for i in idx])
        if err:
            notes.append(u"деталь «{}»: {}".format(det["name"], err))
            continue
        out.append((det, pts, closed, k))
    return out, notes


def offsets(dets, d_main):
    """Сдвиги плоскостей деталей от плоскости основного хомута Ø d_main (мм): попеременно в обе стороны,
    каждая следующая на своей стороне — вплотную к предыдущей. Так детали не пересекаются друг с другом
    и не уходят к торцу элемента дальше, чем на половину стопки."""
    side = {1: d_main / 2.0, -1: d_main / 2.0}
    out = []
    for k, det in enumerate(dets):
        sgn = 1 if k % 2 == 0 else -1
        off = side[sgn] + det["d"] / 2.0
        side[sgn] = off + det["d"] / 2.0
        out.append(sgn * off)
    return out

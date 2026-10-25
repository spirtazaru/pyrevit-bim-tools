# -*- coding: utf-8 -*-
"""Геометрия для армирования: сечения колонн, плиты над и под элементом, единицы.

Внутренние единицы Revit — футы; в интерфейсе и в norms.json — миллиметры.
"""
from pyrevit import DB

MM_IN_FT = 304.8
TOL = 1.0 / MM_IN_FT    # 1 мм


def ft(mm):
    return mm / MM_IN_FT


def mm(feet):
    return feet * MM_IN_FT


def element_solids(element):
    """Тела элемента (с учётом вырезов и соединений), без пустых."""
    opts = DB.Options()
    opts.DetailLevel = DB.ViewDetailLevel.Fine
    out = []

    def walk(geom):
        if geom is None:
            return
        for obj in geom:
            if isinstance(obj, DB.Solid):
                if obj.Volume > 1e-9:
                    out.append(obj)
            elif isinstance(obj, DB.GeometryInstance):
                walk(obj.GetInstanceGeometry())

    walk(element.get_Geometry(opts))
    return out


class ColumnSection(object):
    """Вертикальная колонна в локальных осях: ось ax — сторона b, ay = Z × ax — сторона h.
    center — центр сечения на отметке низа; z_bot, z_top — низ и верх тела колонны."""

    def __init__(self, center, ax, ay, b, h, z_bot, z_top, is_round):
        self.center = center
        self.ax = ax
        self.ay = ay
        self.b = b
        self.h = h
        self.z_bot = z_bot
        self.z_top = z_top
        self.is_round = is_round

    def point(self, u, v, z):
        """Точка по локальным координатам сечения (футы) и абсолютной отметке z."""
        p = self.center + self.ax * u + self.ay * v
        return DB.XYZ(p.X, p.Y, z)

    def size_text(self):
        if self.is_round:
            return u"⌀{:.0f}".format(mm(self.b))
        return u"{:.0f}×{:.0f}".format(mm(self.b), mm(self.h))


def column_section(column):
    """ColumnSection или (None, причина) для неподходящих колонн."""
    try:
        t = column.GetTransform()
    except Exception:
        return None, u"не семейство колонны"
    if abs(t.BasisZ.Z) < 0.999:
        return None, u"наклонная колонна — не поддерживается"
    ax = DB.XYZ(t.BasisX.X, t.BasisX.Y, 0.0)
    if ax.GetLength() < 1e-6:
        return None, u"не удалось определить оси колонны"
    ax = ax.Normalize()
    ay = DB.XYZ.BasisZ.CrossProduct(ax)

    solids = element_solids(column)
    if not solids:
        return None, u"нет геометрии"
    pts, arcs = [], []
    for solid in solids:
        for edge in solid.Edges:
            curve = edge.AsCurve()
            if isinstance(curve, DB.Arc):
                arcs.append(curve)
            pts.extend(curve.Tessellate())
    o = t.Origin
    us = [(p - o).DotProduct(ax) for p in pts]
    vs = [(p - o).DotProduct(ay) for p in pts]
    zs = [p.Z for p in pts]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    b, h = u1 - u0, v1 - v0
    c = o + ax * ((u0 + u1) / 2.0) + ay * ((v0 + v1) / 2.0)
    # Круглое: все дуги — горизонтальные окружности одного радиуса с центром на оси колонны.
    # Размер берём по радиусу дуги: точки Tessellate() не попадают на крайние положения окружности.
    is_round = False
    if arcs:
        r = arcs[0].Radius
        c0 = arcs[0].Center
        is_round = all(abs(a.Radius - r) < TOL and abs(abs(a.Normal.Z) - 1.0) < 1e-6
                       and DB.XYZ(a.Center.X - c0.X, a.Center.Y - c0.Y, 0).GetLength() < TOL for a in arcs) \
            and abs(2 * r - max(b, h)) < ft(2)
        if is_round:
            b = h = 2 * r
            c = DB.XYZ(c0.X, c0.Y, 0.0)
    center = DB.XYZ(c.X, c.Y, min(zs))
    sec = ColumnSection(center, ax, ay, b, h, min(zs), max(zs), is_round)

    # Тип сечения: на середине высоты горизонтальные хорды проходят тело целиком.
    # Г-, Т-образные, крестовые и прочие сечения не пройдут проверку.
    z = (sec.z_bot + sec.z_top) / 2.0
    if is_round:
        r = b / 2.0 + TOL
        chords = [((-r, 0), (r, 0), b), ((0, -r), (0, r), b), ((-r * 0.7071, -r * 0.7071), (r * 0.7071, r * 0.7071), b)]
    else:
        hb, hh = b / 2.0 + TOL, h / 2.0 + TOL
        chords = []
        for k in (-0.4, 0.0, 0.4):
            chords.append(((-hb, k * h), (hb, k * h), b))
            chords.append(((k * b, -hh), (k * b, hh), h))
    for (u0, v0), (u1, v1), length in chords:
        if not _full_chord(solids, sec.point(u0, v0, z), sec.point(u1, v1, z), length):
            return None, u"сечение не прямоугольное и не круглое — не поддерживается"
    return sec, None


# ----------------------------------------------------------
# ПЛИТЫ НАД И ПОД ЭЛЕМЕНТОМ
# ----------------------------------------------------------
def _floors_near(doc, pts, z_lo, z_hi):
    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    outline = DB.Outline(DB.XYZ(min(xs) - TOL, min(ys) - TOL, z_lo), DB.XYZ(max(xs) + TOL, max(ys) + TOL, z_hi))
    return (DB.FilteredElementCollector(doc)
            .OfCategory(DB.BuiltInCategory.OST_Floors)
            .WhereElementIsNotElementType()
            .WherePasses(DB.BoundingBoxIntersectsFilter(outline))
            .ToElements())


def slab_at(doc, sample_points, z_ref, search=ft(1500)):
    """Плита, касающаяся отметки z_ref: (низ, верх) или None.

    Плиту ищем по вертикальным линиям через точки sample_points (центр колонны и точки чуть
    снаружи её граней: если колонна вырезает плиту, в центре плиты нет). Берём участки
    пересечения, которые содержат z_ref или примыкают к нему.
    """
    lines = []
    for p in sample_points:
        lines.append(DB.Line.CreateBound(DB.XYZ(p.X, p.Y, z_ref - search), DB.XYZ(p.X, p.Y, z_ref + search)))
    opts = DB.SolidCurveIntersectionOptions()
    best = None
    for floor in _floors_near(doc, sample_points, z_ref - search, z_ref + search):
        for solid in element_solids(floor):
            for line in lines:
                try:
                    res = solid.IntersectWithCurve(line, opts)
                except Exception:
                    continue
                for i in range(res.SegmentCount):
                    seg = res.GetCurveSegment(i)
                    z0 = min(seg.GetEndPoint(0).Z, seg.GetEndPoint(1).Z)
                    z1 = max(seg.GetEndPoint(0).Z, seg.GetEndPoint(1).Z)
                    if z0 - TOL <= z_ref <= z1 + TOL:
                        best = (z0, z1) if best is None else (min(best[0], z0), max(best[1], z1))
    return best


def column_samples(sec, z, gap=ft(50)):
    """Центр сечения и точки в gap снаружи каждой грани — для поиска плит."""
    hb, hh = sec.b / 2.0 + gap, sec.h / 2.0 + gap
    return [sec.point(0, 0, z), sec.point(hb, 0, z), sec.point(-hb, 0, z),
            sec.point(0, hh, z), sec.point(0, -hh, z)]


# ----------------------------------------------------------
# БАЛКИ
# ----------------------------------------------------------
class BeamSection(object):
    """Прямая балка в локальных осях (форма сечения — в shape, см. BeamShape).
    a — вдоль оси балки (от начала к концу), y = Z × a — поперёк по горизонтали, w = a × y — вверх по сечению.
    origin — на торце в начале тела балки, по центру ширины, на уровне низа. Длина тела — length."""

    def __init__(self, origin, a, y, w, b, h, length):
        self.origin = origin
        self.a = a
        self.y = y
        self.w = w
        self.b = b
        self.h = h
        self.length = length
        self.shape = None

    def point(self, x, v, t):
        """x — вдоль балки от начального торца, v — поперёк от оси, t — вверх от низа (футы)."""
        return self.origin + self.a * x + self.y * v + self.w * t

    def size_text(self):
        return u"{:.0f}×{:.0f}".format(mm(self.b), mm(self.h))


def _full_chord(solids, p0, p1, expected):
    """Отрезок p0-p1 пересекает тело на всю ожидаемую длину (сплошное сечение без уступов)."""
    line = DB.Line.CreateBound(p0, p1)
    opts = DB.SolidCurveIntersectionOptions()
    total = 0.0
    for solid in solids:
        try:
            res = solid.IntersectWithCurve(line, opts)
        except Exception:
            continue
        for i in range(res.SegmentCount):
            total += res.GetCurveSegment(i).Length
    return abs(total - expected) < ft(3)


class BeamShape(object):
    """Форма сечения балки (мм; v — поперёк от оси габарита, t — вверх от низа).
    kind — "rect" | "T" (полка сверху) | "BT" (полки внизу: ригель-обратный тавр или Г-образный) | "I" (двутавр);
    web — (v0, v1) ребра (стенки); при стенке переменной ширины — наименьшая ширина, taper — её разброс;
    top / bottom — полки (v0, v1, толщина) или None. Вуты между полкой и стенкой относятся к стенке."""

    TITLES = {"rect": u"прямоугольное", "T": u"тавр (полка сверху)", "BT": u"ригель с полками внизу",
              "I": u"двутавр"}

    def __init__(self, kind, web, taper, top, bottom):
        self.kind = kind
        self.web = web
        self.taper = taper
        self.top = top
        self.bottom = bottom
        self.rows = []

    def title(self):
        if self.kind == "BT" and self.bottom and (abs(self.bottom[0] - self.web[0]) < 3 or abs(self.bottom[1] - self.web[1]) < 3):
            return u"Г-образное (полка внизу с одной стороны)"
        return self.TITLES[self.kind]

    def key(self):
        r = lambda x: int(round(x))
        flange = lambda f: None if f is None else (r(f[0]), r(f[1]), r(f[2]))
        return (self.kind, r(self.web[0]), r(self.web[1]), flange(self.top), flange(self.bottom))


def _beam_rows(solids, sec, x, n=60):
    """Ширина сечения на n уровнях по высоте в точке x: [(t, (v0, v1) или None)], мм."""
    reach = sec.b + ft(200)
    rows = []
    for k in range(n):
        t = sec.h * (k + 0.5) / n
        ext = _extent(solids, sec.point(x, -reach, t), sec.point(x, reach, t))
        rows.append((mm(t), None if ext is None else (mm(ext[0] - reach), mm(ext[1] - reach))))
    return rows


def beam_shape(solids, sec):
    """BeamShape или (None, причина) по ширинам сечения в середине пролёта."""
    rows = _beam_rows(solids, sec, sec.length / 2.0)
    if any(e is None for _, e in rows):
        return None, u"сечение с пустотами — не поддерживается"
    h = mm(sec.h)
    same = lambda e1, e2: abs(e1[0] - e2[0]) < 3 and abs(e1[1] - e2[1]) < 3
    width = lambda e: e[1] - e[0]
    step = h / len(rows)

    def run(rows_seq):
        """Сколько строк подряд от начала совпадает с первой."""
        k = 0
        while k < len(rows_seq) and same(rows_seq[k][1], rows_seq[0][1]):
            k += 1
        return k

    w_min = min(width(e) for _, e in rows)
    k_bot, k_top = run(rows), run(list(reversed(rows)))
    bottom = top = None
    if k_bot * step >= 40 and width(rows[0][1]) >= w_min + 50:
        bottom = (rows[0][1][0], rows[0][1][1], k_bot * step)
    if k_top * step >= 40 and width(rows[-1][1]) >= w_min + 50:
        top = (rows[-1][1][0], rows[-1][1][1], k_top * step)
    # Стенка: строки не шире наименьшей + 60 мм; между полкой и стенкой допускается вут не выше max(80 мм, толщины полки)
    web_rows = [(t, e) for t, e in rows if width(e) <= w_min + 60]
    if not web_rows:
        return None, u"не удалось выделить стенку сечения"
    t_web0, t_web1 = web_rows[0][0], web_rows[-1][0]
    if any(width(e) > w_min + 60 for t, e in rows if t_web0 <= t <= t_web1):
        return None, u"сечение сложной формы (уступы по высоте стенки) — не поддерживается"
    haunch_bot = t_web0 - (bottom[2] if bottom else 0.0)
    haunch_top = (h - (top[2] if top else 0.0)) - t_web1
    for haunch, flange in ((haunch_bot, bottom), (haunch_top, top)):
        if haunch > max(80.0, flange[2] if flange else 0.0) + step:
            return None, u"сечение переменной ширины (трапеция или скос) — не поддерживается"
    narrow = min(web_rows, key=lambda r: width(r[1]))[1]
    taper = max(width(e) for _, e in web_rows) - w_min
    kind = {(False, False): "rect", (True, False): "T", (False, True): "BT", (True, True): "I"}[(top is not None, bottom is not None)]
    if kind == "rect":
        narrow = (rows[0][1][0], rows[0][1][1]) if taper < 3 else narrow
    shape = BeamShape(kind, narrow, taper, top, bottom)
    shape.rows = rows       # профиль сечения (для рисунка)
    return shape, None


def beam_section(beam):
    """BeamSection или (None, причина). Прямая балка без поворота сечения с постоянным по длине сечением:
    прямоугольное, тавр (полка сверху), ригель с полками внизу (обратный тавр, Г-образное), двутавр.
    Форма — в sec.shape (BeamShape)."""
    loc = beam.Location
    curve = getattr(loc, 'Curve', None)
    if not isinstance(curve, DB.Line):
        return None, u"криволинейная балка — не поддерживается"
    angle = beam.get_Parameter(DB.BuiltInParameter.STRUCTURAL_BEND_DIR_ANGLE)
    if angle is not None and abs(angle.AsDouble()) > 1e-4:
        return None, u"сечение повёрнуто — не поддерживается"
    a = (curve.GetEndPoint(1) - curve.GetEndPoint(0)).Normalize()
    if abs(a.Z) > 0.99:
        return None, u"вертикальный элемент"
    y = DB.XYZ.BasisZ.CrossProduct(a).Normalize()
    w = a.CrossProduct(y)
    solids = element_solids(beam)
    if not solids:
        return None, u"нет геометрии"
    pts = []
    for solid in solids:
        for edge in solid.Edges:
            pts.extend(edge.AsCurve().Tessellate())
    o = curve.GetEndPoint(0)
    xs = [(p - o).DotProduct(a) for p in pts]
    vs = [(p - o).DotProduct(y) for p in pts]
    ts = [(p - o).DotProduct(w) for p in pts]
    x0, x1, v0, v1, t0, t1 = min(xs), max(xs), min(vs), max(vs), min(ts), max(ts)
    sec = BeamSection(o + a * x0 + y * ((v0 + v1) / 2.0) + w * t0, a, y, w, v1 - v0, t1 - t0, x1 - x0)

    shape, err = beam_shape(solids, sec)
    if shape is None:
        return None, err
    # Сечение постоянно по длине: на четвертях пролёта ширины те же (двускатные, с вутами по длине — нет)
    mid = _beam_rows(solids, sec, sec.length / 2.0, 12)
    for xq in (0.25, 0.75):
        for (t, e1), (_, e2) in zip(mid, _beam_rows(solids, sec, sec.length * xq, 12)):
            if e2 is None or abs(e1[0] - e2[0]) > 3 or abs(e1[1] - e2[1]) > 3:
                return None, u"сечение меняется по длине (двускатная, вуты) — не поддерживается"
    sec.shape = shape
    return sec, None


SUPPORT_CATEGORIES = (DB.BuiltInCategory.OST_StructuralColumns, DB.BuiltInCategory.OST_Walls,
                      DB.BuiltInCategory.OST_StructuralFraming)


def beam_support(doc, beam, sec, at_start, search=ft(3000)):
    """Опора за торцом балки: (толщина опоры вдоль оси балки в футах, имя категории) или None.
    Ищем колонну, стену или другую балку, которая начинается сразу за торцом (до 5 мм)."""
    x_end = 0.0 if at_start else sec.length
    direction = sec.a.Negate() if at_start else sec.a
    start = sec.point(x_end, 0.0, sec.h / 2.0)
    end = start + direction * search
    outline = DB.Outline(DB.XYZ(min(start.X, end.X) - TOL, min(start.Y, end.Y) - TOL, min(start.Z, end.Z) - TOL),
                         DB.XYZ(max(start.X, end.X) + TOL, max(start.Y, end.Y) + TOL, max(start.Z, end.Z) + TOL))
    line = DB.Line.CreateBound(start, end)
    opts = DB.SolidCurveIntersectionOptions()
    best = None
    for bic in SUPPORT_CATEGORIES:
        elems = (DB.FilteredElementCollector(doc).OfCategory(bic).WhereElementIsNotElementType()
                 .WherePasses(DB.BoundingBoxIntersectsFilter(outline)).ToElements())
        for el in elems:
            if el.Id == beam.Id:
                continue
            for solid in element_solids(el):
                try:
                    res = solid.IntersectWithCurve(line, opts)
                except Exception:
                    continue
                for i in range(res.SegmentCount):
                    seg = res.GetCurveSegment(i)
                    d0 = min(seg.GetEndPoint(0).DistanceTo(start), seg.GetEndPoint(1).DistanceTo(start))
                    d1 = max(seg.GetEndPoint(0).DistanceTo(start), seg.GetEndPoint(1).DistanceTo(start))
                    if d0 < ft(5) and (best is None or d1 > best[0]):
                        best = (d1, el.Category.Name)
    return best


# ----------------------------------------------------------
# КОЛОННЫ С КОНСОЛЯМИ И СТУПЕНЧАТЫЕ: УЧАСТКИ ПО ВЫСОТЕ
# ----------------------------------------------------------
class Corbel(object):
    """Консоль с одной стороны участка. side — (su, sv): (-1, 0) — консоль в сторону −ax и т. д.
    face — координата грани нижнего участка, от которой выступает консоль; tip — координата конца консоли
    (u для su ≠ 0, v для sv ≠ 0, футы от оси колонны); z_bottom — низ консоли у грани, z_top — верх консоли,
    z_tip — низ вертикальной торцевой грани. samples — [(z, вылет от грани)] для хомутов по наклонному низу."""

    def __init__(self, side, face, tip, z_bottom, z_top, z_tip, samples):
        self.side = side
        self.face = face
        self.tip = tip
        self.z_bottom = z_bottom
        self.z_top = z_top
        self.z_tip = z_tip
        self.samples = samples

    @property
    def reach(self):
        return abs(self.tip - self.face)

    def reach_at(self, z):
        """Вылет консоли от грани на отметке z (линейно между отсчётами)."""
        pts = self.samples
        if not pts:
            return 0.0
        if z <= pts[0][0]:
            return pts[0][1]
        for (z0, r0), (z1, r1) in zip(pts[:-1], pts[1:]):
            if z0 <= z <= z1:
                return r0 + (r1 - r0) * (z - z0) / (z1 - z0) if z1 > z0 else r1
        return pts[-1][1]

    def side_text(self):
        return {(-1, 0): u"−X", (1, 0): u"+X", (0, -1): u"−Y", (0, 1): u"+Y"}[self.side]


class ColumnPart(object):
    """Участок колонны по высоте: kind — "shaft" (постоянное сечение, sec — ColumnSection участка)
    или "corbel" (зона консолей между участками: corbels — список Corbel, below/above — соседние участки)."""

    def __init__(self, kind, z0, z1, sec=None, corbels=None):
        self.kind = kind
        self.z0 = z0
        self.z1 = z1
        self.sec = sec
        self.corbels = corbels or []
        self.below = None
        self.above = None


class ColumnProfile(object):
    """Колонна по высоте: локальные оси, участки снизу вверх. Простая колонна — один участок."""

    def __init__(self, origin, ax, ay, z_bot, z_top, parts):
        self.origin = origin
        self.ax = ax
        self.ay = ay
        self.z_bot = z_bot
        self.z_top = z_top
        self.parts = parts

    @property
    def shafts(self):
        return [p for p in self.parts if p.kind == "shaft"]

    @property
    def corbel_zones(self):
        return [p for p in self.parts if p.kind == "corbel"]

    @property
    def is_simple(self):
        return len(self.parts) == 1


def _extent(solids, p0, p1):
    """(min, max) расстояний от p0 до точек пересечения отрезка p0-p1 с телами, или None."""
    line = DB.Line.CreateBound(p0, p1)
    opts = DB.SolidCurveIntersectionOptions()
    lo = hi = None
    for solid in solids:
        try:
            res = solid.IntersectWithCurve(line, opts)
        except Exception:
            continue
        for i in range(res.SegmentCount):
            seg = res.GetCurveSegment(i)
            for k in (0, 1):
                d = seg.GetEndPoint(k).DistanceTo(p0)
                lo = d if lo is None else min(lo, d)
                hi = d if hi is None else max(hi, d)
    return None if lo is None else (lo, hi)


def column_profile(column, step=ft(25)):
    """ColumnProfile или (None, причина). Участки и консоли определяются по сечениям на отметках с шагом step:
    участок — где сечение постоянно; консоль — где сечение шире участков ниже и выше. Сечение каждого участка
    должно быть прямоугольным; круглая колонна поддерживается только без консолей."""
    sec, err = column_section(column)
    if sec is None and err in (u"не семейство колонны", u"наклонная колонна — не поддерживается",
                               u"не удалось определить оси колонны", u"нет геометрии"):
        return None, err
    t = column.GetTransform()
    ax = DB.XYZ(t.BasisX.X, t.BasisX.Y, 0.0).Normalize()
    ay = DB.XYZ.BasisZ.CrossProduct(ax)
    solids = element_solids(column)
    pts = [p for s in solids for e in s.Edges for p in e.AsCurve().Tessellate()]
    o = DB.XYZ(t.Origin.X, t.Origin.Y, 0.0)
    us = [(p - o).DotProduct(ax) for p in pts]
    vs = [(p - o).DotProduct(ay) for p in pts]
    z_bot, z_top = min(p.Z for p in pts), max(p.Z for p in pts)
    if sec is not None:
        # Габарит тела совпадает с сечением на середине высоты — простая колонна без консолей
        cu = (sec.center - o).DotProduct(ax)
        cv = (sec.center - o).DotProduct(ay)
        if (abs(min(us) - (cu - sec.b / 2.0)) < ft(2) and abs(max(us) - (cu + sec.b / 2.0)) < ft(2)
                and abs(min(vs) - (cv - sec.h / 2.0)) < ft(2) and abs(max(vs) - (cv + sec.h / 2.0)) < ft(2)):
            return ColumnProfile(o, ax, ay, z_bot, z_top, [ColumnPart("shaft", z_bot, z_top, sec)]), None
    if sec is not None and sec.is_round:
        return None, u"круглая колонна с консолью — не поддерживается"

    reach = max(max(us) - min(us), max(vs) - min(vs)) + ft(100)
    vc_ref = (min(vs) + max(vs)) / 2.0

    def extents(z):
        """(u0, u1, v0, v1) сечения на отметке z в футах от оси колонны."""
        a = o + ay * vc_ref
        eu = _extent(solids, DB.XYZ(a.X, a.Y, z) - ax * reach, DB.XYZ(a.X, a.Y, z) + ax * reach)
        if eu is None:
            return None
        u0, u1 = eu[0] - reach, eu[1] - reach
        b = o + ax * ((u0 + u1) / 2.0)
        ev = _extent(solids, DB.XYZ(b.X, b.Y, z) - ay * reach, DB.XYZ(b.X, b.Y, z) + ay * reach)
        if ev is None:
            return None
        return (u0, u1, ev[0] - reach, ev[1] - reach)

    same = lambda e1, e2: e1 is not None and e2 is not None and all(abs(a - b) < ft(2) for a, b in zip(e1, e2))
    inside = lambda e, s: (e[0] >= s[0] - ft(2) and e[1] <= s[1] + ft(2) and e[2] >= s[2] - ft(2) and e[3] <= s[3] + ft(2))

    n = max(int((z_top - z_bot) / step), 8)
    zs = [z_bot + (z_top - z_bot) * (k + 0.5) / n for k in range(n)]
    samples = [(z, extents(z)) for z in zs]
    if any(e is None for _, e in samples):
        return None, u"не удалось определить сечение по высоте (пустоты в теле?)"

    # Серии подряд идущих одинаковых сечений
    runs = []
    for z, e in samples:
        if runs and same(runs[-1][2], e):
            runs[-1][1] = z
        else:
            runs.append([z, z, e])

    def boundary(z_in, z_out, ext):
        """Уточнение границы участка с сечением ext между отметками z_in (внутри) и z_out (снаружи), до 1 мм."""
        for _ in range(20):
            if abs(z_out - z_in) < ft(1):
                break
            zm = (z_in + z_out) / 2.0
            if same(extents(zm), ext):
                z_in = zm
            else:
                z_out = zm
        return (z_in + z_out) / 2.0

    # Проход снизу вверх: новый участок — длинная серия, которая не шире текущего участка;
    # всё, что шире текущего участка, — зона консолей
    shafts, zones = [], []
    cur, cur_z0, cur_last, zone_first, zone_last = runs[0][2], z_bot, runs[0][1], None, None
    for z_first, z_last, e in runs[1:]:
        if inside(e, cur) and z_last - z_first >= ft(200):
            if zone_first is None:
                z_b = boundary(cur_last, z_first, cur)       # ступенька без консоли
                shafts.append((cur_z0, z_b, cur))
            else:
                z_a = boundary(cur_last, zone_first, cur)
                z_b = boundary(z_first, zone_last, e)
                shafts.append((cur_z0, z_a, cur))
                zones.append((z_a, z_b))
            cur_z0 = z_b
            cur, cur_last, zone_first = e, z_last, None
        elif inside(e, cur) and zone_first is None:
            cur_last = z_last
        else:
            if zone_first is None:
                zone_first = z_first
            zone_last = z_last
    top_zone = None
    if zone_first is not None:
        # Консоли у самого верха колонны (например, колонны под лотки): участка над ними нет
        z_a = boundary(cur_last, zone_first, cur)
        shafts.append((cur_z0, z_a, cur))
        top_zone = (z_a, z_top)
    else:
        shafts.append((cur_z0, z_top, cur))

    parts = []
    for i, (z0, z1, (u0, u1, v0, v1)) in enumerate(shafts):
        c = o + ax * ((u0 + u1) / 2.0) + ay * ((v0 + v1) / 2.0)
        part_sec = ColumnSection(DB.XYZ(c.X, c.Y, z0), ax, ay, u1 - u0, v1 - v0, z0, z1, False)
        zm = (z0 + z1) / 2.0
        hb, hh = part_sec.b / 2.0 + TOL, part_sec.h / 2.0 + TOL
        for k in (-0.4, 0.0, 0.4):
            ok_u = _full_chord(solids, part_sec.point(-hb, k * part_sec.h, zm), part_sec.point(hb, k * part_sec.h, zm), part_sec.b)
            ok_v = _full_chord(solids, part_sec.point(k * part_sec.b, -hh, zm), part_sec.point(k * part_sec.b, hh, zm), part_sec.h)
            if not (ok_u and ok_v):
                return None, u"участок {} не прямоугольного сечения — не поддерживается".format(i + 1)
        parts.append(ColumnPart("shaft", z0, z1, part_sec))
        if i < len(zones) and zones[i][1] > zones[i][0]:
            parts.append(ColumnPart("corbel", zones[i][0], zones[i][1]))
    if top_zone is not None:
        parts.append(ColumnPart("corbel", top_zone[0], top_zone[1]))

    shaft_ext = dict((id(p), s[2]) for p, s in zip([p for p in parts if p.kind == "shaft"], shafts))
    for i, part in enumerate(parts):
        if part.kind != "corbel":
            continue
        part.below = parts[i - 1]
        part.above = parts[i + 1] if i + 1 < len(parts) else None
        lo = shaft_ext[id(part.below)]
        hi = shaft_ext[id(part.above)] if part.above is not None else lo
        zone = [(z, e) for z, e in samples if part.z0 <= z <= part.z1]
        if not zone:
            continue
        for axis, side in ((0, (-1, 0)), (1, (1, 0)), (2, (0, -1)), (3, (0, 1))):
            sign = -1 if axis in (0, 2) else 1
            ref = min(lo[axis], hi[axis]) if sign < 0 else max(lo[axis], hi[axis])
            vals = [e[axis] for _, e in zone]
            tip = min(vals) if sign < 0 else max(vals)
            if (tip - ref) * sign < ft(5):
                continue
            face = lo[axis]
            prof = [(z, max(0.0, (e[axis] - face) * sign)) for z, e in zone]
            z_tip = min(z for z, e in zone if abs(e[axis] - tip) < ft(3))
            part.corbels.append(Corbel(side, face, tip, part.z0, part.z1, z_tip, prof))
    return ColumnProfile(o, ax, ay, z_bot, z_top, parts), None

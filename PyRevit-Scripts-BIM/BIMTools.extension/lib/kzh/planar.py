# -*- coding: utf-8 -*-
"""Плоские элементы (стены и плиты): локальные оси, сечения прямыми, проёмы, раскладка стержней сеток.

Координаты — мм в локальных осях элемента: x, y — в плоскости, n — по толщине.
Стена: x — вдоль оси стены от её начала, y — абсолютная отметка, n — от оси стены к наружной грани.
Плита: x — вдоль основного направления армирования, y — поперёк, n — абсолютная отметка.

Стержни описываются ломаными в этих координатах. Соседние стержни одинаковой формы, сдвинутые друг
относительно друга на один и тот же вектор, собираются в наборы Revit (group_runs). Проёмы находятся
по самой геометрии (сечения тела прямыми с малым шагом), поэтому учитываются окна, двери, отверстия
семейств-пустот, шахты и вырезы — всё, что вырезает тело основы.
"""
import math

from pyrevit import DB

from kzh import rebar as kr
from kzh.geometry import element_solids, ft, mm

_OPTS = DB.SolidCurveIntersectionOptions()
EPS = 0.6       # мм — допуск совпадения точек


# ==========================================================
# ЭЛЕМЕНТ В ЛОКАЛЬНЫХ ОСЯХ
# ==========================================================
class Plate(object):
    """Плоский элемент: origin и единичные оси ex, ey, en (XYZ). Границы x0..x1, y0..y1, n0..n1 — мм.
    Грани по толщине — линейные функции от y (у стен переменной толщины грань наклонная)."""

    def __init__(self, element, origin, ex, ey, en, solids):
        self.element = element
        self.o, self.ex, self.ey, self.en = origin, ex, ey, en
        self.solids = solids
        self._cache = {}
        xs, ys, ns = [], [], []
        for solid in solids:
            for edge in solid.Edges:
                for p in edge.AsCurve().Tessellate():
                    x, y, n = self.local(p)
                    xs.append(x)
                    ys.append(y)
                    ns.append(n)
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.n0, self.n1 = min(ns), max(ns)
        # Грани: n = a + k·y (заполняет fit_faces)
        self.lo_a, self.lo_k, self.hi_a, self.hi_k = self.n0, 0.0, self.n1, 0.0

    def point(self, x, y, n):
        return self.o + self.ex * ft(x) + self.ey * ft(y) + self.en * ft(n)

    def vec(self, dx, dy, dn):
        return self.ex * ft(dx) + self.ey * ft(dy) + self.en * ft(dn)

    def local(self, p):
        d = p - self.o
        return mm(d.DotProduct(self.ex)), mm(d.DotProduct(self.ey)), mm(d.DotProduct(self.en))

    # ----------------------------------------------------------
    def segments(self, a, b, retry=True):
        """Части отрезка a–b (локальные точки, мм) внутри тела: [(s0, s1)] — доли длины по возрастанию."""
        key = tuple(int(round(v * 2)) for v in a + b)
        if key in self._cache:
            return self._cache[key]
        pa, pb = self.point(*a), self.point(*b)
        line = DB.Line.CreateBound(pa, pb)
        d = pb - pa
        dd = d.DotProduct(d)
        raw = []
        for solid in list(self.solids):
            try:
                res = solid.IntersectWithCurve(line, _OPTS)
            except Exception:
                if not retry:
                    continue
                # После изменения модели прежние тела недействительны — берём геометрию заново
                self.solids = element_solids(self.element)
                self._cache = {}
                return self.segments(a, b, False)
            for i in range(res.SegmentCount):
                seg = res.GetCurveSegment(i)
                t = sorted((seg.GetEndPoint(k) - pa).DotProduct(d) / dd for k in (0, 1))
                raw.append((t[0], t[1]))
        raw.sort()
        out = []
        tol = 0.5 / max(mm(math.sqrt(dd)), 1.0)
        for s0, s1 in raw:
            if out and s0 <= out[-1][1] + tol:
                out[-1] = (out[-1][0], max(out[-1][1], s1))
            else:
                out.append((s0, s1))
        self._cache[key] = out
        return out

    def chord(self, a, b):
        """Части отрезка a–b внутри тела: [(точка, точка)] в локальных мм."""
        lerp = lambda s: tuple(a[k] + (b[k] - a[k]) * s for k in range(3))
        return [(lerp(s0), lerp(s1)) for s0, s1 in self.segments(a, b)]

    def depth_at(self, x, y):
        """(n низа, n верха) тела по толщине в точке (x, y) или None."""
        segs = self.chord((x, y, self.n0 - 50), (x, y, self.n1 + 50))
        if not segs:
            return None
        return segs[0][0][2], segs[-1][1][2]

    # ----------------------------------------------------------
    def face_lo(self, y):
        return self.lo_a + self.lo_k * y

    def face_hi(self, y):
        return self.hi_a + self.hi_k * y

    def mid(self, y):
        return (self.face_lo(y) + self.face_hi(y)) / 2.0

    def thickness(self, y):
        return self.face_hi(y) - self.face_lo(y)

    def fit_faces(self, variable, robust=False):
        """Положение граней по толщине по замерам в нескольких точках. variable — грани могут быть наклонными
        (стены переменной толщины); иначе толщина должна быть постоянной. robust — грани по преобладающим
        замерам (площадки лестниц: в местах примыкания маршей тело толще). Возвращает текст ошибки или None."""
        samples = []
        for fx in (0.13, 0.31, 0.5, 0.69, 0.87):
            x = self.x0 + (self.x1 - self.x0) * fx
            for fy in (0.07, 0.3, 0.5, 0.7, 0.93):
                y = self.y0 + (self.y1 - self.y0) * fy
                d = self.depth_at(x, y)
                if d is not None:
                    samples.append((x, y, d[0], d[1]))
        if len(samples) < 3:
            return u"не удалось определить толщину"
        if variable:
            # Линейная зависимость от y по крайним по высоте замерам
            ya = min(s[1] for s in samples)
            yb = max(s[1] for s in samples)
            lo_a = [s for s in samples if abs(s[1] - ya) < 1]
            hi_b = [s for s in samples if abs(s[1] - yb) < 1]
            sa, sb = lo_a[0], hi_b[0]
            if yb - ya > 1:
                self.lo_k = (sb[2] - sa[2]) / (yb - ya)
                self.hi_k = (sb[3] - sa[3]) / (yb - ya)
            self.lo_a = sa[2] - self.lo_k * sa[1]
            self.hi_a = sa[3] - self.hi_k * sa[1]
        elif robust:
            counts = {}
            for s in samples:
                key = (int(round(s[2] / 5.0)), int(round(s[3] / 5.0)))
                counts[key] = counts.get(key, 0) + 1
            lo5, hi5 = max(counts, key=lambda k: counts[k])
            self.lo_a, self.hi_a = lo5 * 5.0, hi5 * 5.0
            return None
        else:
            self.lo_a = min(s[2] for s in samples)
            self.hi_a = max(s[3] for s in samples)
        for x, y, lo, hi in samples:
            if abs(lo - self.face_lo(y)) > 3 or abs(hi - self.face_hi(y)) > 3:
                return u"толщина меняется не по линейному закону (уступы, вуты, изменённая форма) — не поддерживается"
        return None

    @property
    def tapered(self):
        return abs(self.lo_k) > 1e-4 or abs(self.hi_k) > 1e-4


def wall_plate(wall):
    """Plate для прямой стены или (None, причина)."""
    curve = getattr(wall.Location, 'Curve', None)
    if not isinstance(curve, DB.Line):
        return None, u"криволинейная стена — не поддерживается"
    p0 = curve.GetEndPoint(0)
    ex = DB.XYZ(curve.Direction.X, curve.Direction.Y, 0.0).Normalize()
    en = wall.Orientation
    en = (en - ex * en.DotProduct(ex)).Normalize()
    solids = element_solids(wall)
    if not solids:
        return None, u"нет геометрии"
    plate = Plate(wall, DB.XYZ(p0.X, p0.Y, 0.0), ex, DB.XYZ.BasisZ, en, solids)
    err = plate.fit_faces(True)
    if err:
        return None, err
    return plate, None


def slab_plate(floor, angle=None, robust=False):
    """Plate для плиты. angle — направление x в плане (рад от оси X проекта); None — вдоль самой длинной
    прямой кромки верхней грани. robust — см. Plate.fit_faces."""
    solids = element_solids(floor)
    if not solids:
        return None, u"нет геометрии"
    if angle is None:
        angle = longest_edge_angle(floor, solids)
    ex = DB.XYZ(math.cos(angle), math.sin(angle), 0.0)
    ey = DB.XYZ.BasisZ.CrossProduct(ex)
    plate = Plate(floor, DB.XYZ.Zero, ex, ey, DB.XYZ.BasisZ, solids)
    err = plate.fit_faces(False, robust)
    if err:
        return None, err
    return plate, None


def longest_edge_angle(element, solids=None):
    """Угол самой длинной горизонтальной прямой кромки элемента (рад, от 0 до π)."""
    best, angle = 0.0, 0.0
    for solid in solids or element_solids(element):
        for edge in solid.Edges:
            c = edge.AsCurve()
            if isinstance(c, DB.Line) and abs(c.Direction.Z) < 1e-6 and c.Length > best:
                best = c.Length
                angle = math.atan2(c.Direction.Y, c.Direction.X)
    angle = angle % math.pi
    if abs(angle - math.pi) < 1e-6:
        angle = 0.0
    return angle


# ==========================================================
# ПРОЁМЫ
# ==========================================================
class Opening(object):
    """Проём в плоскости x–y (мм): прямоугольник-габарит и стороны габарита элемента, которых он касается."""

    def __init__(self, x0, x1, y0, y1, touches):
        self.x0, self.x1, self.y0, self.y1 = x0, x1, y0, y1
        self.touches = touches
        self.small = False

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0

    def across(self, along):
        """Интервал поперёк стержней направления along ('x' — стержни вдоль x, поперёк — y)."""
        return (self.y0, self.y1) if along == "x" else (self.x0, self.x1)

    def along(self, along):
        return (self.x0, self.x1) if along == "x" else (self.y0, self.y1)

    def text(self):
        return u"{:.0f}×{:.0f}".format(float(self.w), float(self.h))


def _gaps(segs, a, b):
    out, cur = [], a
    for s0, s1 in segs:
        if s0 > cur + 1.0:
            out.append((cur, min(s0, b)))
        cur = max(cur, s1)
    if cur < b - 1.0:
        out.append((cur, b))
    return [g for g in out if g[1] - g[0] > 1.0]


def find_openings(plate, res=20.0):
    """Проёмы — области внутри габарита, не занятые телом, на средней по толщине плоскости.
    Возвращает [Opening]; вырезы у края (касаются габарита) — тоже Opening, их отбирает вызывающий код."""
    x0, x1 = plate.x0, plate.x1
    rows = []
    y = plate.y0 + res / 2.0
    while y < plate.y1:
        n = plate.mid(y)
        segs = [(a[0], b[0]) for a, b in plate.chord((x0 - 50, y, n), (x1 + 50, y, n))]
        rows.append((y, _gaps(segs, x0, x1)))
        y += res
    parent = {}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    cells = []
    prev = []
    for i, (y, gaps) in enumerate(rows):
        cur = []
        for g in gaps:
            cid = len(cells)
            cells.append((y, g))
            parent[cid] = cid
            for pid, pg in prev:
                if pg[0] < g[1] and g[0] < pg[1]:
                    parent[find(cid)] = find(pid)
            cur.append((cid, g))
        prev = cur
    comps = {}
    for cid, (y, g) in enumerate(cells):
        comps.setdefault(find(cid), []).append((y, g))
    out = []
    for items in comps.values():
        ox0 = min(g[0] for _, g in items)
        ox1 = max(g[1] for _, g in items)
        oy0 = min(y for y, _ in items) - res / 2.0
        oy1 = max(y for y, _ in items) + res / 2.0
        # Уточнение низа и верха — сечением по вертикали через середину проёма
        xc = (max(g[0] for _, g in items) + min(g[1] for _, g in items)) / 2.0
        if not (ox0 < xc < ox1):
            xc = (ox0 + ox1) / 2.0
        ya, yb = plate.y0 - 50, plate.y1 + 50
        segs = [(a[1], b[1]) for a, b in plate.chord((xc, ya, plate.mid(ya)), (xc, yb, plate.mid(yb)))]
        ym = (oy0 + oy1) / 2.0
        for g in _gaps(segs, plate.y0, plate.y1):
            if g[0] - res <= ym <= g[1] + res and abs(g[0] - oy0) <= res and abs(g[1] - oy1) <= res:
                oy0, oy1 = g
                break
        touches = set()
        if ox0 <= plate.x0 + 1.0:
            touches.add("x0")
        if ox1 >= plate.x1 - 1.0:
            touches.add("x1")
        if oy0 <= plate.y0 + 1.0:
            touches.add("y0")
        if oy1 >= plate.y1 - 1.0:
            touches.add("y1")
        out.append(Opening(ox0, ox1, oy0, oy1, touches))
    out.sort(key=lambda o: (o.x0, o.y0))
    return out


def outline_rows(plate, res=None):
    """Контур тела для рисунка: [(y, [(x0, x1)])] на средней плоскости."""
    res = res or max((plate.y1 - plate.y0) / 120.0, 10.0)
    out = []
    y = plate.y0 + res / 2.0
    while y < plate.y1:
        n = plate.mid(y)
        out.append((y, [(a[0], b[0]) for a, b in plate.chord((plate.x0 - 50, y, n), (plate.x1 + 50, y, n))]))
        y += res
    return out


# ==========================================================
# СЛОЙ СЕТКИ
# ==========================================================
def positions(a, b, first, step):
    """Равномерная раскладка на [a, b] с отступом first от краёв и шагом не более step."""
    span = b - a - 2 * first
    if span <= 1.0:
        return [(a + b) / 2.0]
    n = int(math.ceil(span / step - 1e-6)) + 1
    pitch = span / (n - 1)
    return [a + first + i * pitch for i in range(n)]


class Layer(object):
    """Слой стержней: along — 'x' | 'y' (направление стержней); face — 'hi' | 'lo' | 'mid';
    off — от грани до оси стержня внутрь (для 'mid' — со знаком, к грани hi); d — Ø, step — шаг, мм."""

    def __init__(self, key, along, face, off, d, step):
        self.key, self.along, self.face, self.off, self.d, self.step = key, along, face, off, d, step

    def n(self, plate, y):
        if self.face == "hi":
            return plate.face_hi(y) - self.off
        if self.face == "lo":
            return plate.face_lo(y) + self.off
        return plate.mid(y) + self.off


class Bar(object):
    """Стержень: pts — ломаная в локальных мм; idx — номер позиции в раскладке (для наборов);
    hint — направление набора для прямого стержня (локальный вектор), если стержень один."""

    def __init__(self, pts, idx, hint=None):
        self.pts = pts
        self.idx = idx
        self.hint = hint

    @property
    def length(self):
        return sum(_dist(a, b) for a, b in zip(self.pts[:-1], self.pts[1:]))


def _dist(a, b):
    return math.sqrt(sum((a[k] - b[k]) ** 2 for k in range(3)))


def _sub(a, b):
    return tuple(a[k] - b[k] for k in range(3))


def _add(a, b):
    return tuple(a[k] + b[k] for k in range(3))


def _mul(a, s):
    return tuple(v * s for v in a)


def _dot(a, b):
    return sum(a[k] * b[k] for k in range(3))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    l = math.sqrt(_dot(a, a))
    return _mul(a, 1.0 / l) if l > 1e-9 else a


def shifted_positions(ps, holes, along, clear):
    """Позиции стержней, раздвинутые от малых отверстий: стержень, попадающий в отверстие (с зазором clear),
    переносится к ближайшему краю. Совпавшие после переноса стержни объединяются."""
    out = []
    for p in ps:
        for o in holes:
            a0, a1 = o.across(along)
            if a0 - clear < p < a1 + clear:
                p = a0 - clear if p - a0 < a1 - p else a1 + clear
        out.append(p)
    res = []
    for p in sorted(out):
        if res and p - res[-1] < clear:
            continue
        res.append(p)
    return res


def layer_bars(plate, layer, ps, openings, c_end, end_rule, clear, limit=None):
    """Стержни слоя по позициям ps. openings — крупные проёмы: у стержней, проходящих через проём или рядом
    с ним (ближе clear к стороне), участок у проёма вырезается, концы — с защитным слоем c_end от граней проёма.
    end_rule(сторона, позиция, координата конца) -> (вылет за край, [(dx, dy, dn) отгибы]) для концов
    у наружных краёв: сторона — 'start' | 'end' (начало/конец по направлению стержня).
    limit — (a, b): стержни только в этих пределах по длине (дополнительные стержни у проёмов).
    Возвращает ([Bar], [(позиция, a, b)] — участки для рисунка)."""
    bars, drawn = [], []
    for idx, p in enumerate(ps):
        if layer.along == "x":
            a = (plate.x0 - 100, p, layer.n(plate, p))
            b = (plate.x1 + 100, p, layer.n(plate, p))
            k = 0
        else:
            ya, yb = plate.y0 - 100, plate.y1 + 100
            a = (p, ya, layer.n(plate, ya))
            b = (p, yb, layer.n(plate, yb))
            k = 1
        segs = [(sa[k], sb[k], "outer", "outer") for sa, sb in plate.chord(a, b)]
        # Концы у проёмов, вырез у проёмов рядом со стержнем
        out = []
        for s0, s1, t0, t1 in segs:
            for o in openings:
                o0, o1 = o.along(layer.along)
                if o0 - 1.0 <= s0 <= o1 + 1.0 and _inside_across(o, layer.along, p, 1.0):
                    t0 = "hole"
                if o0 - 1.0 <= s1 <= o1 + 1.0 and _inside_across(o, layer.along, p, 1.0):
                    t1 = "hole"
            pieces = [(s0, s1, t0, t1)]
            for o in openings:
                if not _inside_across(o, layer.along, p, clear):
                    continue
                o0, o1 = o.along(layer.along)
                nxt = []
                for q0, q1, u0, u1 in pieces:
                    if o1 <= q0 or o0 >= q1:
                        nxt.append((q0, q1, u0, u1))
                        continue
                    if o0 > q0:
                        nxt.append((q0, o0, u0, "hole"))
                    if o1 < q1:
                        nxt.append((o1, q1, "hole", u1))
                pieces = nxt
            out.extend(pieces)
        if limit is not None:
            clipped = []
            for s0, s1, t0, t1 in out:
                if s1 <= limit[0] or s0 >= limit[1]:
                    continue
                if s0 < limit[0]:
                    s0, t0 = limit[0], "limit"
                if s1 > limit[1]:
                    s1, t1 = limit[1], "limit"
                clipped.append((s0, s1, t0, t1))
            out = clipped
        for s0, s1, t0, t1 in out:
            if t0 == "hole":
                s0 += c_end
            if t1 == "hole":
                s1 -= c_end
            ext0, legs0 = (0.0, []) if t0 != "outer" else end_rule("start", p, s0)
            ext1, legs1 = (0.0, []) if t1 != "outer" else end_rule("end", p, s1)
            s0 -= ext0
            s1 += ext1
            if s1 - s0 < max(100.0, 4 * layer.d):
                continue
            drawn.append((p, s0, s1))
            pt = lambda s: (s, p, layer.n(plate, p)) if layer.along == "x" else (p, s, layer.n(plate, s))
            pts = [pt(s0), pt(s1)]
            for leg in legs0:
                pts.insert(0, _add(pts[0], leg))
            for leg in legs1:
                pts.append(_add(pts[-1], leg))
            hint = (0.0, 1.0, (layer.n(plate, p + 1.0) - layer.n(plate, p))) if layer.along == "x" else (1.0, 0.0, 0.0)
            bars.append(Bar(pts, idx, hint))
    return bars, drawn


def _inside_across(o, along, p, clear):
    a0, a1 = o.across(along)
    return a0 - clear < p < a1 + clear


def split_long(bars, max_len, lap, along_index, shift):
    """Стержни длиннее max_len режутся на части с нахлёстом lap; у нечётных позиций стыки смещены
    на половину части (разбежка). Части с нечётным номером сдвинуты поперёк на shift (локальный вектор),
    чтобы стержни в нахлёсте не совпадали. along_index — 0 для стержней вдоль x, 1 — вдоль y."""
    out = []
    for bar in bars:
        total = bar.length
        if total <= max_len or len(bar.pts) < 2:
            out.append(bar)
            continue
        # Режется только прямой участок между отгибами: самый длинный отрезок ломаной
        segs = list(zip(bar.pts[:-1], bar.pts[1:]))
        main = max(range(len(segs)), key=lambda i: _dist(*segs[i]))
        a, b = segs[main]
        L = _dist(a, b)
        k = int(math.ceil((total - lap) / float(max_len - lap) - 1e-9))
        k = max(k, 2)
        u = _unit(_sub(b, a))
        if bar.idx % 2 == 0:
            joints = [L * i / k for i in range(1, k)]
        else:
            joints = [L * (i - 0.5) / k for i in range(1, k + 1)]
        cuts = [0.0]
        for j in joints:
            cuts += [j - lap / 2.0, j + lap / 2.0]
        cuts.append(L)
        starts = [cuts[0]] + [cuts[2 * i + 1] for i in range(len(joints))]
        ends = [cuts[2 * i + 2] for i in range(len(joints))] + [cuts[-1]]
        for i, (s0, s1) in enumerate(zip(starts, ends)):
            pts = [_add(a, _mul(u, s0)), _add(a, _mul(u, s1))]
            if i == 0:
                pts = list(bar.pts[:main]) + pts          # отгибы в начале остаются у первой части
            if i == len(starts) - 1:
                pts = pts + list(bar.pts[main + 2:])      # и в конце — у последней
            if i % 2 == 1:
                pts = [_add(q, shift) for q in pts]
            out.append(Bar(pts, bar.idx, bar.hint))
    return out


# ==========================================================
# НАБОРЫ
# ==========================================================
def _plane_normal(pts):
    """Нормаль плоскости ломаной или None для прямого стержня."""
    for i in range(1, len(pts) - 1):
        n = _cross(_sub(pts[i], pts[0]), _sub(pts[i + 1], pts[0]))
        if math.sqrt(_dot(n, n)) > 1e-3:
            return _unit(n)
    return None


def _translation_ok(pts, delta):
    """Сдвиг набора должен быть перпендикулярен плоскости стержня (для прямого — самому стержню)."""
    dl = math.sqrt(_dot(delta, delta))
    if dl < 1.0:
        return False
    nrm = _plane_normal(pts)
    if nrm is None:
        u = _unit(_sub(pts[-1], pts[0]))
        return abs(_dot(u, delta)) / dl < 1e-3
    return math.sqrt(_dot(_cross(nrm, delta), _cross(nrm, delta))) / dl < 1e-3


def _same(pts_a, pts_b, delta):
    if len(pts_a) != len(pts_b):
        return False
    for p, q in zip(pts_a, pts_b):
        if _dist(_add(p, delta), q) > EPS:
            return False
    return True


class Run(object):
    """Набор: первый стержень, вектор шага delta (мм, локальный), число стержней."""

    def __init__(self, bar):
        self.first = bar
        self.last = bar
        self.n = 1
        self.delta = None
        self.gap = None

    @property
    def normal(self):
        """Нормаль плоскости первого стержня (локальная, ед.) — по направлению набора."""
        if self.delta is not None:
            return _unit(self.delta)
        nrm = _plane_normal(self.first.pts)
        if nrm is not None:
            return nrm
        u = _unit(_sub(self.first.pts[-1], self.first.pts[0]))
        hint = self.first.hint or (0.0, 0.0, 1.0)
        h = _sub(hint, _mul(u, _dot(hint, u)))
        if math.sqrt(_dot(h, h)) < 1e-6:
            h = _cross(u, (0.0, 0.0, 1.0)) if abs(u[2]) < 0.9 else _cross(u, (1.0, 0.0, 0.0))
        return _unit(h)

    @property
    def span(self):
        return 0.0 if self.delta is None else math.sqrt(_dot(self.delta, self.delta)) * (self.n - 1)


def group_runs(bars):
    """Соседние по позиции стержни одинаковой формы с постоянным шагом -> наборы. Учитывает разбежку
    стыков (одинаковые стержни через одну позицию)."""
    by_idx = {}
    for bar in bars:
        by_idx.setdefault(bar.idx, []).append(bar)
    runs, active = [], []
    for idx in sorted(by_idx):
        for bar in by_idx[idx]:
            best = None
            for r in active:
                gap = idx - r.last.idx
                if gap < 1 or gap > 2 or (r.gap is not None and gap != r.gap):
                    continue
                delta = _sub(bar.pts[0], r.last.pts[0])
                if r.delta is not None and _dist(delta, r.delta) > EPS:
                    continue
                if not _same(r.last.pts, bar.pts, delta) or not _translation_ok(bar.pts, delta):
                    continue
                if best is None or gap < best[1]:
                    best = (r, gap, delta)
            if best is not None:
                r, gap, delta = best
                r.delta, r.gap = delta, gap
                r.last = bar
                r.n += 1
            else:
                r = Run(bar)
                runs.append(r)
                active.append(r)
        active = [r for r in active if idx - r.last.idx < (r.gap or 2)]
    return runs


def run_specs(plate, role, d, runs):
    """BarSpec для наборов."""
    specs = []
    for r in runs:
        curves = kr.polyline([plate.point(*p) for p in r.first.pts])
        nrm = r.normal
        norm = plate.vec(*nrm).Normalize()
        layout = ("fixed", r.n, ft(r.span)) if r.n > 1 else None
        specs.append(kr.BarSpec(role, d, curves, norm, layout, r.n))
    return specs


def bars_to_specs(plate, role, d, bars):
    return run_specs(plate, role, d, group_runs(bars))


def total_length(bars):
    return sum(b.length for b in bars)


def side_positions(start, direction, k, step, avoid, gap, lo, hi):
    """До k позиций от start с шагом step в сторону direction (±1) в пределах [lo, hi], не ближе gap
    к позициям avoid (стержни сетки) — для дополнительных стержней у проёмов."""
    out, p, guard = [], start, 0
    while len(out) < k and lo <= p <= hi and guard < 400:
        if all(abs(p - q) >= gap for q in avoid):
            out.append(p)
        p += direction * step
        guard += 1
    return out


def spread(a, b, n):
    """n точек от a до b включительно (n = 1 — только a)."""
    if n <= 1:
        return [a]
    return [a + k * (b - a) / (n - 1) for k in range(n)]


# ==========================================================
# СОСЕДНИЕ ЭЛЕМЕНТЫ
# ==========================================================
def probe(doc, plate, box, categories, exclude_id=None):
    """Элементы категорий categories (BuiltInCategory), пересекающие параллелепипед box = (x0, x1, y0, y1, n0, n1)
    в локальных мм элемента."""
    from System.Collections.Generic import List
    x0, x1, y0, y1, n0, n1 = box
    pts = [plate.point(x0, y0, n0), plate.point(x1, y0, n0), plate.point(x1, y1, n0), plate.point(x0, y1, n0)]
    solid = None
    for seq in (pts, list(reversed(pts))):
        loop = DB.CurveLoop()
        for a, b in zip(seq, seq[1:] + seq[:1]):
            loop.Append(DB.Line.CreateBound(a, b))
        try:
            solid = DB.GeometryCreationUtilities.CreateExtrusionGeometry(List[DB.CurveLoop]([loop]), plate.en, ft(n1 - n0))
            break
        except Exception:
            solid = None
    if solid is None:
        return []
    cats = List[DB.BuiltInCategory](categories)
    found = DB.FilteredElementCollector(doc).WherePasses(DB.ElementMulticategoryFilter(cats)) \
        .WhereElementIsNotElementType().WherePasses(DB.ElementIntersectsSolidFilter(solid)).ToElements()
    return [e for e in found if exclude_id is None or e.Id != exclude_id]

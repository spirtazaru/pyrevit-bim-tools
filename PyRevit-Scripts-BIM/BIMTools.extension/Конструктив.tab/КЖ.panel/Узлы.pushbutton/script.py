# -*- coding: utf-8 -*-
"""Армирование узлов (pyRevit / IronPython 2.7). Сейчас — узлы стена–стена.

Выбранные стены разбираются на узлы по геометрии осей в плане:
- L — угол: обе стены заканчиваются в точке пересечения осей;
- T — примыкание: одна стена заканчивается у другой, проходящей насквозь (X — это два T по обе стороны);
- стены, пересекающие друг друга без разрыва, пропускаются: их стержни идут насквозь.

Варианты армирования (переключатель в окне):
- L «Г-стержни» — угловые Г-стержни у наружной и у внутренней граней, внахлёст с горизонтальными
  стержнями обеих стен; L «П-стержни + Г снаружи» — П-стержни каждой стены, охватывающие угол до наружной
  сетки другой стены, и Г-стержни у наружной грани;
- T «П-стержни» — П-стержни примыкающей стены, охватывающие дальнюю сетку сквозной стены;
  T «Г-стержни» — стержни у граней примыкающей стены с загибом вдоль дальней грани сквозной стены.
Плюс вертикальные стержни в ядре узла — в точках пересечения сеток.

Ряды стержней узла — с тем же первым рядом и шагом, что у горизонтальных стержней кнопки «Стены»: стержень
узла ложится рядом со стержнем стены. Ножки — на длину нахлёста по СП 63, обрезаются по бетону (проёмы, торцы).
Арматура узла создаётся в одной из стен (у T — в примыкающей) с пометкой узла в «Комментариях»: повторный
запуск заменяет только стержни этого узла. Значения практики помечены в norms.json «сверить».
"""
__title__ = u"Узлы"
__author__ = u"Daniil Antonenko"
__doc__ = u"Армирование узлов: стена–стена (угол L, примыкание T, X): Г- и П-стержни, вертикальные стержни в узле"

import math
import traceback

from pyrevit import DB
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException
from System.Windows import Visibility

import sys
# pyRevit держит модули lib в памяти движка между запусками кнопок: без сброса после обновления
# кнопка может взять старую версию kzh (ImportError: cannot import name ...)
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from kzh import element_name, ui
from kzh import runner
from kzh import drawing as kd
from kzh import geometry as kg
from kzh import planar as kp
from kzh import norms, settings
from kzh import rebar as kr
from kzh.geometry import ft, mm
from kzh.section_editor import SectionEditor
from kzh.ui import show_message, Option

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

SETTINGS_SECTION = "nodes"
PART_NAME = u"Узел"
WHAT = {"many": u"узлов", "title": u"Армирование узлов", "done": u"Армировано узлов",
        "failed": u"Не армирован", "armed": u"уже армирован"}

VARIANTS = {"L": (("l_bars", u"Г-стержни"), ("l_u", u"П-стержни + Г снаружи")),
            "T": (("t_u", u"П-стержни"), ("t_bars", u"Г-стержни")),
            "SC": (("sc_full", u"Продавливание + надколонная"), ("sc_top", u"Только надколонная")),
            "SWE": (("swe_g", u"Г-стержни по краю"),),
            "SWI": (("swi_top", u"Надопорные верхние"),),
            "CF": (("cf_foot", u"Выпуски с лапками"), ("cf_straight", u"Прямые выпуски")),
            "BC": (("bc_full", u"Хомуты колонны + надопорные"), ("bc_hoops", u"Только хомуты колонны"))}
VARIANT_TITLES = dict(v for k in VARIANTS for v in VARIANTS[k])
TOP_COVER, TOP_LAP = "cover", "lap"

GENERAL_DEFAULTS = {"steel": u"А500", "concrete": u"В25"}
# Параметры узлов. Сетки стен (dh, dv, c, cond, hor_out, sh, first) — как у кнопки «Стены»; dg — Ø стержней узла
# (None — как горизонтальные), leg — длина ножек (None — нахлёст по СП 63), vert/dvn/top — вертикальные в ядре
OPTS_DEFAULTS = {"dh": 10, "dv": 12, "sh": 200.0, "first": None, "c": None, "cond": "indoor_normal", "hor_out": True,
                 "dg": None, "leg": None, "vert": True, "dvn": None, "top": TOP_COVER,
                 # плита–колонна: шпильки продавливания Ø, шаг рядов, рядов, шаг вдоль контура; надколонная Ø и вылет
                 "sc_dp": None, "sc_s": None, "sc_rows": None, "sc_st": None, "sc_dsup": None, "sc_lz": None,
                 # плита–стена: Ø и шаг стержней узла, вылет в плиту, ножка в стену
                 "sw_d": None, "sw_s": None, "sw_ls": None, "sw_lw": None,
                 # колонна–фундамент: Ø выпусков, нахлёст, лапка, фиксирующие хомуты Ø и число
                 "cf_d": None, "cf_lap": None, "cf_foot": None, "cf_dh": None, "cf_nh": None,
                 # балка–колонна: хомуты колонны Ø и шаг; надопорные: число, Ø, вылет
                 "bc_dh": None, "bc_s": None, "bc_n": None, "bc_d": None, "bc_ls": None}
# Защитные слои, диаметры и шаги элементов — из настроек их кнопок (то же, что поставили «Плиты», «Колонны», «Балки»)
SLAB_DEFAULTS = {"dbx": 12, "sbx": 200.0, "dby": 12, "sby": 200.0, "dtx": 12, "stx": 200.0, "dty": 12, "sty": 200.0,
                 "c_b": None, "c_t": None, "cond_b": "indoor_normal", "cond_t": "indoor_normal", "x_out": True}
COLUMN_DEFAULTS = {"d_ct": 16, "d_cb": 16, "n_t": 1, "d_t": 16, "n_b": 1, "d_b": 16, "n_s": 1, "d_s": 16,
                   "c_s": None, "c_t": None, "c_b": None, "n": 8, "d": 16, "c": None, "ds": 8, "cover_cond": "indoor_normal"}
BEAM_DEFAULTS = {"n_top": 2, "d_top": 12, "c_s": None, "c_t": None, "cover_cond": "indoor_normal", "dt": 8}


# ==========================================================
# 2D-ВЕКТОРЫ (мм, план)
# ==========================================================
def add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def mul(a, s):
    return (a[0] * s, a[1] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def intersect(p1, d1, p2, d2):
    """Точка пересечения прямых p1 + d1·s и p2 + d2·u (или None для параллельных)."""
    c = cross(d1, d2)
    if abs(c) < 1e-9:
        return None
    s = cross(sub(p2, p1), d2) / c
    return add(p1, mul(d1, s))


def sign(v):
    return 1.0 if v >= 0 else -1.0


# ==========================================================
# СТЕНЫ И УЗЛЫ
# ==========================================================
def wall_title(wall):
    return u"{} (id {})".format(element_name(wall.WallType), wall.Id.IntegerValue)


class WallInfo(object):
    """Прямая стена постоянной толщины: ось (локатор) в плане, грани по нормали, отметки низа и верха (мм)."""

    def __init__(self, wall, plate):
        self.wall = wall
        self.plate = plate
        self.title = wall_title(wall)
        self.o = (mm(plate.o.X), mm(plate.o.Y))
        self.ex = (plate.ex.X, plate.ex.Y)
        self.en = (plate.en.X, plate.en.Y)
        self.length = mm(wall.Location.Curve.Length)
        self.n_lo, self.n_hi = plate.face_lo(0.0), plate.face_hi(0.0)
        self.t = self.n_hi - self.n_lo
        self.z0, self.z1 = plate.y0, plate.y1

    def pt(self, x, n):
        return add(self.o, add(mul(self.ex, x), mul(self.en, n)))

    def local_x(self, p):
        return dot(sub(p, self.o), self.ex)

    def line(self, n):
        return self.pt(0.0, n), self.ex

    def face(self, side):
        """Смещение грани: side = +1 — грань n_hi (наружная по Orientation), −1 — n_lo."""
        return self.n_hi if side > 0 else self.n_lo

    def layer(self, side, calc, vertical):
        """Смещение оси слоя горизонтальных (vertical=False) или вертикальных стержней у грани side."""
        c, dh, dv = calc.c, calc.dh, calc.dv
        if calc.hor_out:
            off = c + dh + dv / 2.0 if vertical else c + dh / 2.0
        else:
            off = c + dv / 2.0 if vertical else c + dv + dh / 2.0
        return self.face(side) - off if side > 0 else self.face(side) + off

    def available(self, start, direction, n, z):
        """Сколько бетона (мм) от точки start вдоль direction (±ex) на смещении n и отметке z."""
        x = self.local_x(start)
        s = sign(dot(direction, self.ex))
        segs = self.plate.chord((x, z, n), (x + s * 20000.0, z, n))
        if not segs:
            return None
        return abs(segs[0][1][0] - x)


class Node(object):
    """Узел: kind — "L" | "T". L: a, b — стены, ua/ub — направления от узла в тело стен.
    T: a — примыкающая стена (ua — в её тело), b — сквозная. P — точка пересечения осей."""

    def __init__(self, kind, a, b, P, ua, ub):
        self.kind = kind
        self.a, self.b = a, b
        self.P, self.ua, self.ub = P, ua, ub
        self.variant = VARIANTS[kind][0][0]
        self.Name = u"{} — {} + {}".format(u"Угол L" if kind == "L" else u"Примыкание T", a.title, b.title)
        self.comment = u"КЖ: узел {} {}-{}".format(kind, a.wall.Id.IntegerValue, b.wall.Id.IntegerValue)
        self.host = a.wall

    cards = ("bars", "legs", "mesh", "vert")

    def build(self, calc):
        return build_walls(calc, self)

    def draw(self, canvas, calc, built, pad):
        return draw_node(canvas, calc, self, built.raw, pad)


def find_nodes(infos):
    """Узлы между всеми парами стен: ([Node], [пропуски])."""
    nodes, skipped = [], []
    for i in range(len(infos)):
        for j in range(i + 1, len(infos)):
            A, B = infos[i], infos[j]
            if abs(cross(A.ex, B.ex)) < math.sin(math.radians(10)):
                continue        # параллельные стены — не узел
            if min(A.z1, B.z1) - max(A.z0, B.z0) < 300:
                continue        # не пересекаются по высоте
            P = intersect(A.pt(0, (A.n_lo + A.n_hi) / 2.0), A.ex, B.pt(0, (B.n_lo + B.n_hi) / 2.0), B.ex)
            if P is None:
                continue
            tol = max(A.t, B.t) + 100.0
            ends, passes = [], []
            for W in (A, B):
                x = W.local_x(P)
                if abs(x) < tol:
                    ends.append(W.ex)
                elif abs(x - W.length) < tol:
                    ends.append(mul(W.ex, -1.0))
                else:
                    ends.append(None)
                passes.append(tol <= x <= W.length - tol)
            if ends[0] is not None and ends[1] is not None:
                nodes.append(Node("L", A, B, P, ends[0], ends[1]))
            elif ends[0] is not None and passes[1]:
                nodes.append(Node("T", A, B, P, ends[0], None))
            elif ends[1] is not None and passes[0]:
                nodes.append(Node("T", B, A, P, ends[1], None))
            elif passes[0] and passes[1]:
                skipped.append(u"{} и {} — пересекаются без разрыва: стержни проходят насквозь, узел не нужен".format(
                    A.title, B.title))
    return nodes, skipped


def collect(walls):
    infos, skipped = [], []
    for wall in walls:
        if not isinstance(wall, DB.Wall) or wall.WallType.Kind != DB.WallKind.Basic:
            continue
        if not kr.is_valid_host(wall):
            skipped.append(u"{} — Revit не допускает арматуру (стена не несущая или не бетон)".format(wall_title(wall)))
            continue
        try:
            plate, err = kp.wall_plate(wall)
        except Exception as ex:
            plate, err = None, u"ошибка геометрии: {}".format(ex)
        if plate is None:
            skipped.append(u"{} — {}".format(wall_title(wall), err))
            continue
        if plate.tapered:
            skipped.append(u"{} — стена переменной толщины: узлы не поддерживаются".format(wall_title(wall)))
            continue
        infos.append(WallInfo(wall, plate))
    nodes, skipped2 = find_nodes(infos)
    # X — два примыкания T к одной сквозной стене в одной точке: общее ядро, вертикальные стержни — один раз
    seen = []
    for node in nodes:
        if node.kind != "T":
            continue
        key = node.b.wall.Id.IntegerValue
        if any(k == key and math.hypot(node.P[0] - p[0], node.P[1] - p[1]) < max(node.a.t, node.b.t) for k, p in seen):
            node.no_vert = True
            node.Name = node.Name.replace(u"Примыкание T", u"Примыкание X (вторая сторона)")
        else:
            seen.append((key, node.P))
    return nodes, skipped + skipped2


# ==========================================================
# РАСЧЁТ
# ==========================================================
class Calc(object):
    def __init__(self, ho, so):
        self.ho, self.so = ho, so
        self.dh, self.dv = so["dh"], so["dv"]
        self.c_auto = norms.ceil_to(norms.cover_main(so["cond"], max(self.dh, self.dv)), 5.0)
        self.c = so["c"] or self.c_auto
        self.hor_out = so["hor_out"]
        self.sh = so["sh"]
        self.first = so["first"] if so["first"] is not None else norms.wall_rule("first_bar")["mm"]
        self.dg = so["dg"] or self.dh
        self.dvn = so["dvn"] or self.dv
        self.leg_auto = norms.lap_length(self.dh, ho["steel"], ho["concrete"], False)
        self.leg = so["leg"] or self.leg_auto
        self.warnings = []
        if self.leg < self.leg_auto:
            self.warnings.append(u"ножки {:.0f} мм короче нахлёста по СП 63 ({:.0f} мм)".format(
                float(self.leg), float(self.leg_auto)))
        pick = lambda defaults, src: dict((k, v) for k, v in (src or {}).items() if k in defaults and v is not None)
        self.slab = dict(SLAB_DEFAULTS)
        self.slab.update(pick(SLAB_DEFAULTS, settings.load("slabs").get("mesh_solid")))
        cs = settings.load("columns")
        self.col = dict(COLUMN_DEFAULTS)
        for part in (cs.get("rect"), cs.get("round"), cs):
            self.col.update(pick(COLUMN_DEFAULTS, part))
        walls = settings.load("walls").get("mesh_double") or {}
        self.wall_sv = walls.get("sv_e") or 200.0
        bs = settings.load("beams")
        self.beam = dict(BEAM_DEFAULTS)
        self.beam.update(pick(BEAM_DEFAULTS, bs.get("section")))
        self.beam.update(pick(BEAM_DEFAULTS, bs.get("length")))

    def lap(self, d):
        return norms.lap_length(d, self.ho["steel"], self.ho["concrete"], False)

    def anchorage(self, d):
        return norms.anchorage_length(d, self.ho["steel"], self.ho["concrete"], False)

    def slab_layers(self, plate):
        """Слои сеток плиты как у кнопки «Плиты» (сплошная): {"bx", "by", "tx", "ty"}, c_b, c_t."""
        o = self.slab
        auto = lambda cond, d: norms.ceil_to(norms.cover_main(cond, d), 5.0)
        c_b = o["c_b"] or auto(o["cond_b"], max(o["dbx"], o["dby"]))
        c_t = o["c_t"] or auto(o["cond_t"], max(o["dtx"], o["dty"]))
        layers = {}
        for key, face, c, dx, sx, dy, sy in (("b", "lo", c_b, o["dbx"], o["sbx"], o["dby"], o["sby"]),
                                             ("t", "hi", c_t, o["dtx"], o["stx"], o["dty"], o["sty"])):
            if o["x_out"]:
                off_x, off_y = c + dx / 2.0, c + dx + dy / 2.0
            else:
                off_y, off_x = c + dy / 2.0, c + dy + dx / 2.0
            layers[key + "x"] = kp.Layer(key + "x", "x", face, off_x, dx, sx)
            layers[key + "y"] = kp.Layer(key + "y", "y", face, off_y, dy, sy)
        return layers, c_b, c_t

    def col_cover(self):
        """Защитный слой продольных стержней колонны (как у кнопки «Колонны»)."""
        o = self.col
        d, ds = max(o["d_ct"], o["d"]), o["ds"]
        auto = norms.ceil_to(max(norms.cover_main(o["cover_cond"], d), norms.cover_constructive(o["cover_cond"], ds) + ds), 5.0)
        return o["c_t"] or o["c"] or auto


class NodePlan(object):
    def __init__(self):
        self.bars = {}          # категория -> [[Bar] по группам]
        self.notes = []
        self.rows = []

    def add(self, key, group):
        if group:
            self.bars.setdefault(key, []).append(group)

    def all(self, key):
        return [b for g in self.bars.get(key, []) for b in g]

    def count(self, key):
        return len(self.all(key))


CATEGORIES = (("g", "L", u"Г-стержни"), ("u", "U", u"П-стержни"), ("v", "straight", u"вертикальные в узле"))


def _leg(calc, wall, start, direction, n, z, want):
    """Длина ножки вдоль стены wall: не больше want и не дальше бетона (минус защитный слой)."""
    room = wall.available(start, direction, n, z)
    if room is None:
        return want
    return max(0.0, min(want, room - calc.c))


def plan_node(calc, node):
    A, B = node.a, node.b
    plan = NodePlan()
    z0, z1 = max(A.z0, B.z0), min(A.z1, B.z1)
    rows = kp.positions(z0, z1, calc.first, calc.sh)
    rows = [z for z in rows if z + calc.dh + 2 * calc.dg < z1 - calc.c]
    plan.rows = rows
    dz = (calc.dh + calc.dg) / 2.0
    hline = lambda W, side: W.line(W.layer(side, calc, False))
    vline = lambda W, side: W.line(W.layer(side, calc, True))
    X = lambda l1, l2: intersect(l1[0], l1[1], l2[0], l2[1])
    to3 = lambda p, z: (p[0], p[1], z)

    def bent(points, z, idx):
        return kp.Bar([to3(p, z) for p in points], idx)

    if node.kind == "L":
        ua, ub = node.ua, node.ub
        in_a = sign(dot(A.en, ub))      # грань стены A, обращённая к стене B (внутренняя)
        in_b = sign(dot(B.en, ua))
        q_out = X(hline(A, -in_a), hline(B, -in_b))
        q_in = X(hline(A, in_a), hline(B, in_b))
        g_out, g_in, u_a, u_b = [], [], [], []
        for i, z in enumerate(rows):
            if node.variant == "l_bars":
                zg = z + dz
                for q, side_a, side_b, grp in ((q_out, -in_a, -in_b, g_out), (q_in, in_a, in_b, g_in)):
                    la = _leg(calc, A, q, ua, A.layer(side_a, calc, False), zg, calc.leg)
                    lb = _leg(calc, B, q, ub, B.layer(side_b, calc, False), zg, calc.leg)
                    grp.append(bent([add(q, mul(ua, la)), q, add(q, mul(ub, lb))], zg, i))
            else:
                # П-стержни каждой стены: перемычка — по наружной сетке другой стены; Г — у наружной грани
                for W, Wo, u, in_w, in_o, grp, k in ((A, B, ua, in_a, in_b, u_a, 0), (B, A, ub, in_b, in_a, u_b, 1)):
                    zu = z + dz + k * calc.dg
                    w1 = X(hline(W, -in_w), hline(Wo, -in_o))
                    w2 = X(hline(W, in_w), hline(Wo, -in_o))
                    l1 = _leg(calc, W, w1, u, W.layer(-in_w, calc, False), zu, calc.leg)
                    l2 = _leg(calc, W, w2, u, W.layer(in_w, calc, False), zu, calc.leg)
                    leg = min(l1, l2)
                    grp.append(bent([add(w1, mul(u, leg)), w1, w2, add(w2, mul(u, leg))], zu, i))
                zg = z + dz + 2 * calc.dg
                la = _leg(calc, A, q_out, ua, A.layer(-in_a, calc, False), zg, calc.leg)
                lb = _leg(calc, B, q_out, ub, B.layer(-in_b, calc, False), zg, calc.leg)
                g_out.append(bent([add(q_out, mul(ua, la)), q_out, add(q_out, mul(ub, lb))], zg, i))
        for g in (g_out, g_in):
            plan.add("g", g)
        for g in (u_a, u_b):
            plan.add("u", g)
        verts = [X(vline(A, sa), vline(B, sb)) for sa in (in_a, -in_a) for sb in (in_b, -in_b)]
    else:
        S, W, us = A, B, node.ua
        near = sign(dot(W.en, us))          # грань сквозной стены со стороны примыкающей
        extra = W.t - calc.c - calc.dh / 2.0     # от дальней сетки до ближней грани сквозной стены
        u_grp, g_grp = [], {1.0: [], -1.0: []}
        for i, z in enumerate(rows):
            zz = z + dz
            if node.variant == "t_u":
                w1 = X(hline(S, 1.0), hline(W, -near))
                w2 = X(hline(S, -1.0), hline(W, -near))
                l1 = _leg(calc, S, w1, us, S.layer(1.0, calc, False), zz, calc.leg + extra)
                l2 = _leg(calc, S, w2, us, S.layer(-1.0, calc, False), zz, calc.leg + extra)
                leg = min(l1, l2)
                u_grp.append(bent([add(w1, mul(us, leg)), w1, w2, add(w2, mul(us, leg))], zz, i))
            else:
                for side in (1.0, -1.0):
                    p = X(hline(S, side), hline(W, -near))
                    away = mul(W.ex, sign(dot(W.ex, mul(S.en, side))))    # вдоль сквозной стены — от оси примыкающей
                    l_in = _leg(calc, S, p, us, S.layer(side, calc, False), zz, calc.leg + extra)
                    l_along = _leg(calc, W, p, away, W.layer(-near, calc, False), zz, calc.leg)
                    g_grp[side].append(bent([add(p, mul(us, l_in)), p, add(p, mul(away, l_along))], zz, i))
        plan.add("u", u_grp)
        plan.add("g", g_grp[1.0])
        plan.add("g", g_grp[-1.0])
        verts = [X(vline(S, ss), vline(W, sw)) for ss in (1.0, -1.0) for sw in (near, -near)]
    if getattr(node, "no_vert", False):
        plan.notes.append(u"вертикальные стержни ядра — в узле с первой стороны")
    elif calc.so["vert"]:
        top = z1 + calc.lap(calc.dvn) if calc.so["top"] == TOP_LAP else z1 - calc.c
        for k, p in enumerate(verts):
            if p is not None:
                plan.add("v", [kp.Bar([to3(p, z0), to3(p, top)], 0, (1.0, 0.0, 0.0))])
    if not rows:
        plan.notes.append(u"узел слишком низкий для рядов стержней")
    return plan


class WorldFrame(object):
    """Координаты стержней узла — мировые мм (X, Y, Z): для kp.run_specs."""

    def point(self, x, y, z):
        return DB.XYZ(ft(x), ft(y), ft(z))

    def vec(self, x, y, z):
        return DB.XYZ(ft(x), ft(y), ft(z))


def plan_specs(calc, plan):
    frame = WorldFrame()
    diam = {"g": calc.dg, "u": calc.dg, "v": calc.dvn}
    specs = []
    for key, role, _ in CATEGORIES:
        for group in plan.bars.get(key, []):
            specs.extend(kp.run_specs(frame, role, diam[key], kp.group_runs(group)))
    return specs


def plan_summary(node, plan):
    parts = [u"{} {}".format(title, plan.count(key)) for key, _, title in CATEGORIES if plan.count(key)]
    return u"{}: {}, рядов {}; стержней: {}".format(node.Name, VARIANT_TITLES[node.variant], len(plan.rows), u", ".join(parts))


def reinforce(nodes, ho, so, skipped):
    calc = Calc(ho, so)
    plans, skipped = [], list(skipped)
    for node in nodes:
        try:
            built = node.build(calc)
            parts = [(part, part_specs(part)) for part in built.parts]
        except Exception as ex:
            skipped.append(u"{} — ошибка расчёта: {}".format(node.Name, ex))
            continue
        parts = [(part, specs) for part, specs in parts if specs]
        if not parts:
            skipped.append(u"{} — нет стержней".format(node.Name))
            continue
        for part, specs in parts:
            counts = u", ".join(u"{} {}".format(t, len(b)) for _, _, _, t, b in part.groups)
            plans.append({"host": part.host, "title": node.Name + part.suffix, "specs": specs,
                          "summary": u"{}{}: {}; стержней: {}".format(node.Name, part.suffix, VARIANT_TITLES[node.variant], counts),
                          "notes": built.notes, "comment": node.comment})

    def existing(plan):
        ids = []
        for rid in kr.host_rebar_ids(plan["host"]):
            p = doc.GetElement(rid).get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p is not None and p.AsString() == plan["comment"]:
                ids.append(rid)
        return ids

    hook = kr.default_stirrup_hook(doc)
    runner.run(doc, plans, skipped, calc.warnings, ho["steel"], ho["steel"], {"stirrup": hook, "pin": hook},
               PART_NAME, WHAT, existing)


# ==========================================================
# ОБЩЕЕ ДЛЯ ВСЕХ УЗЛОВ: стержни узла по основам
# ==========================================================
class Part(object):
    """Стержни узла в одной основе: host, frame (перевод координат стержней в XYZ), группы
    [(ключ, роль, Ø, подпись, [Bar])]; стержни группы собираются в наборы."""

    def __init__(self, host, frame, suffix=u""):
        self.host, self.frame, self.suffix = host, frame, suffix
        self.groups = []

    def add(self, key, role, d, title, bars):
        if bars:
            self.groups.append((key, role, d, title, bars))


class Built(object):
    """Результат расчёта узла: части по основам, замечания, данные для рисунка."""

    def __init__(self):
        self.parts = []
        self.notes = []
        self.raw = None

    def bars(self, key):
        return [b for p in self.parts for k, _, _, _, bars in p.groups if k == key for b in bars]

    def counts(self):
        out = []
        for p in self.parts:
            for key, role, d, title, bars in p.groups:
                for i, (t, n) in enumerate(out):
                    if t == title:
                        out[i] = (t, n + len(bars))
                        break
                else:
                    out.append((title, len(bars)))
        return out


def part_specs(part):
    specs = []
    for key, role, d, title, bars in part.groups:
        specs.extend(kp.run_specs(part.frame, role, d, kp.group_runs(bars)))
    return specs


def counts_text(built):
    return u", ".join(u"{} {}".format(t, n) for t, n in built.counts())


def build_walls(calc, node):
    plan = plan_node(calc, node)
    built = Built()
    built.raw = plan
    built.notes = list(plan.notes)
    part = Part(node.a.wall, WorldFrame())
    # У примыкания T ядро узла — в теле сквозной стены: вертикальные стержни ядра принадлежат ей,
    # иначе Revit предупреждает «арматура полностью вне основы»
    core = Part(node.b.wall, WorldFrame(), u" — сквозная стена") if node.kind == "T" else part
    diam = {"g": calc.dg, "u": calc.dg, "v": calc.dvn}
    for key, role, title in CATEGORIES:
        for group in plan.bars.get(key, []):
            (core if key == "v" else part).add(key, role, diam[key], title, group)
    built.parts.append(part)
    if core is not part:
        built.parts.append(core)
    return built


def element_title(el):
    t = doc.GetElement(el.GetTypeId())
    return u"{} (id {})".format(element_name(t) if t is not None else u"?", el.Id.IntegerValue)


def world(p2, z):
    return (p2[0], p2[1], z)


def solid_chord(solids, a, b):
    """Участки отрезка a–b (XYZ) внутри тел: [(s0, s1)] в долях длины."""
    line = DB.Line.CreateBound(a, b)
    d = b - a
    dd = d.DotProduct(d)
    out = []
    for solid in solids:
        try:
            res = solid.IntersectWithCurve(line, DB.SolidCurveIntersectionOptions())
        except Exception:
            continue
        for i in range(res.SegmentCount):
            seg = res.GetCurveSegment(i)
            t = sorted((seg.GetEndPoint(k) - a).DotProduct(d) / dd for k in (0, 1))
            out.append((t[0], t[1]))
    return sorted(out)


class ColumnFrame(object):
    """Колонна в мм: центр сечения в плане, оси ax/ay (2D), размеры b (вдоль ax), h, отметки низа и верха."""

    def __init__(self, column, sec):
        self.column = column
        self.sec = sec
        self.c = (mm(sec.center.X), mm(sec.center.Y))
        self.ax = (sec.ax.X, sec.ax.Y)
        self.ay = (sec.ay.X, sec.ay.Y)
        self.b, self.h = mm(sec.b), mm(sec.h)
        self.z0, self.z1 = mm(sec.z_bot), mm(sec.z_top)
        self.round = sec.is_round
        self.title = element_title(column)

    def pt(self, u, v):
        return add(self.c, add(mul(self.ax, u), mul(self.ay, v)))

    def half_along(self, direction):
        """Половина размера колонны вдоль направления direction (2D, единичный)."""
        if self.round:
            return self.b / 2.0
        return abs(dot(self.ax, direction)) * self.b / 2.0 + abs(dot(self.ay, direction)) * self.h / 2.0

    def bar_positions(self, calc, d):
        """Продольные стержни колонны как у кнопки «Колонны»: [(u, v, наружу (du, dv), грань)]."""
        o, c = calc.col, calc.col_cover()
        out = []
        if self.round:
            r = self.b / 2.0 - c - d / 2.0
            n = int(o["n"])
            for k in range(n):
                a = 2 * math.pi * k / n
                out.append((r * math.cos(a), r * math.sin(a), (math.cos(a), math.sin(a)), k))
            return out
        ub, vb = self.b / 2.0 - c - d / 2.0, self.h / 2.0 - c - d / 2.0
        for su in (-1.0, 1.0):
            for sv in (-1.0, 1.0):
                out.append((su * ub, sv * vb, (su, 0.0), u"corner"))
        for sv, n in ((1.0, int(o["n_t"])), (-1.0, int(o["n_b"]))):
            for u in kp.spread(-ub, ub, n + 2)[1:-1] if n else []:
                out.append((u, sv * vb, (0.0, sv), u"face_v{}".format(sv)))
        for su in (-1.0, 1.0):
            n = int(o["n_s"])
            for v in kp.spread(-vb, vb, n + 2)[1:-1] if n else []:
                out.append((su * ub, v, (su, 0.0), u"face_u{}".format(su)))
        return out

    def hoop(self, calc, ds, z):
        """Замкнутый хомут колонны на отметке z (мировые мм) — по продольным стержням, как у кнопки «Колонны»."""
        c = calc.col_cover()
        hu, hv = self.b / 2.0 - c + ds / 2.0, self.h / 2.0 - c + ds / 2.0
        loop = [(-hu, -hv), (hu, -hv), (hu, hv), (-hu, hv), (-hu, -hv)]
        return [world(self.pt(u, v), z) for u, v in loop]


# ----------------------------------------------------------
# ПЛИТА – КОЛОННА
# ----------------------------------------------------------
class SlabColumnNode(object):
    """Колонна под плитой: продавливание (шпильки рядами вокруг колонны) и надколонная верхняя арматура.
    Координаты стержней — локальные мм плиты (x, y — в плане по осям армирования плиты, n — отметка)."""
    cards = ("pun", "sup")

    def __init__(self, floor, plate, col):
        self.kind = "SC"
        self.floor, self.plate, self.col = floor, plate, col
        self.host = floor
        loc = plate.local(col.sec.center)
        self.xc, self.yc = loc[0], loc[1]
        ex = (plate.ex.X, plate.ex.Y)
        cosa = abs(dot(col.ax, ex))
        sina = math.sqrt(max(0.0, 1.0 - cosa * cosa))
        self.rotated = not col.round and 0.02 < cosa < 0.9998
        if col.round:
            self.bx = self.by = col.b
        else:
            self.bx = col.b * cosa + col.h * sina
            self.by = col.b * sina + col.h * cosa
        self.spans = [None, None]
        self.variant = VARIANTS["SC"][0][0]
        self.Name = u"Плита–колонна — {} / {}".format(element_title(floor), col.title)
        self.comment = u"КЖ: узел SC {}-{}".format(floor.Id.IntegerValue, col.column.Id.IntegerValue)

    def zone(self, calc):
        """Вылеты надколонной арматуры от граней колонны (по x, по y)."""
        if calc.so["sc_lz"]:
            return calc.so["sc_lz"], calc.so["sc_lz"]
        rule = norms.node_rule("support_top")
        return tuple(norms.ceil_to(rule["k_span"] * s, 50.0) if s else rule["mm"] for s in self.spans)

    def punch(self, calc):
        """(h0, шаг рядов, рядов, шаг вдоль контура, Ø)."""
        layers, c_b, c_t = calc.slab_layers(self.plate)
        t = self.plate.thickness(0.0)
        h0 = t - c_b - calc.slab["dbx"]
        so = calc.so
        s = so["sc_s"] or max(50.0, math.floor(h0 * norms.node_rule("punch_step")["k_h0"] / 10.0) * 10.0)
        rows = int(so["sc_rows"]) if so["sc_rows"] else max(1, int(norms.node_rule("punch_zone")["k_h0"] * h0 / s + 1e-6))
        st = so["sc_st"] or s
        dp = so["sc_dp"] or norms.node_rule("punch_d")["mm"]
        return h0, s, rows, st, dp

    def _span(self, along, p, n, c_along, half, cover):
        """Отрезок стержня надколонной арматуры по бетону: через колонну (если она вырезает плиту) не прерывается."""
        P = self.plate
        if along == "x":
            segs = [(a[0], b[0]) for a, b in P.chord((P.x0 - 50, p, n), (P.x1 + 50, p, n))]
        else:
            segs = [(a[1], b[1]) for a, b in P.chord((p, P.y0 - 50, n), (p, P.y1 + 50, n))]
        gap = max(self.bx, self.by) + 40.0
        merged = []
        for a, b in segs:
            if merged and a - merged[-1][1] < gap and merged[-1][1] <= c_along + gap and a >= c_along - gap:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        lo, hi = c_along - half, c_along + half
        for a, b in merged:
            if a <= c_along <= b or (a < hi and b > lo):
                s0 = max(a + cover, lo) if a > lo else lo
                s1 = min(b - cover, hi) if b < hi else hi
                return (s0, s1) if s1 - s0 > 200 else None
        return None

    def build(self, calc):
        P, so = self.plate, calc.so
        layers, c_b, c_t = calc.slab_layers(P)
        built = Built()
        part = Part(self.floor, P)
        built.parts.append(part)
        if self.rotated:
            built.notes.append(u"колонна повёрнута относительно осей плиты — контур по габариту")
        if self.variant == "sc_full":
            h0, s, rows, st, dp = self.punch(calc)
            lb = layers["bx"] if calc.slab["x_out"] else layers["by"]
            lt = layers["tx"] if calc.slab["x_out"] else layers["ty"]
            z_lo = lb.n(P, 0.0) - lb.d / 2.0 - dp / 2.0
            z_hi = lt.n(P, 0.0) + lt.d / 2.0 + dp / 2.0
            inside = lambda x, y: all(P.depth_at(x + dx, y + dy) is not None
                                      for dx, dy in ((0, 0), (60, 0), (-60, 0), (0, 60), (0, -60)))
            pin = lambda x, y, i, hint: kp.Bar([(x, y, z_lo), (x, y, z_hi)], i, hint)
            for k in range(1, rows + 1):
                a = k * s
                if self.col.round:
                    r = self.bx / 2.0 + a
                    n = max(8, int(math.ceil(2 * math.pi * r / st - 1e-6)))
                    ring = [pin(self.xc + r * math.cos(2 * math.pi * j / n), self.yc + r * math.sin(2 * math.pi * j / n), j,
                                (1.0, 0.0, 0.0)) for j in range(n)]
                    part.add("pun", "pin", dp, u"шпильки продавливания", [b for b in ring if inside(b.pts[0][0], b.pts[0][1])])
                    continue
                hx, hy = self.bx / 2.0 + a, self.by / 2.0 + a
                nx = int(math.ceil(2 * hx / st - 1e-6)) + 1
                ny = int(math.ceil(2 * hy / st - 1e-6)) + 1
                xs = kp.spread(self.xc - hx, self.xc + hx, nx)
                ys = kp.spread(self.yc - hy, self.yc + hy, ny)[1:-1]
                for side in ((xs, self.yc - hy, "x"), (xs, self.yc + hy, "x"), (ys, self.xc - hx, "y"), (ys, self.xc + hx, "y")):
                    vals, fixed, along = side
                    grp = []
                    for v in vals:
                        x, y = (v, fixed) if along == "x" else (fixed, v)
                        if inside(x, y):
                            grp.append(pin(x, y, len(grp), (1.0, 0.0, 0.0) if along == "x" else (0.0, 1.0, 0.0)))
                    part.add("pun", "pin", dp, u"шпильки продавливания", grp)
        lx, ly = self.zone(calc)
        for key, along in (("tx", "x"), ("ty", "y")):
            lay = layers[key]
            d = so["sc_dsup"] or lay.d
            if along == "x":
                lo, hi, c_across, half_across, c_along, half_along = P.y0, P.y1, self.yc, self.by / 2.0 + ly, self.xc, self.bx / 2.0 + lx
            else:
                lo, hi, c_across, half_across, c_along, half_along = P.x0, P.x1, self.xc, self.bx / 2.0 + lx, self.yc, self.by / 2.0 + ly
            ps = [q + lay.step / 2.0 for q in kp.positions(lo, hi, norms.slab_rule("first_bar")["mm"], lay.step)]
            ps = [q for q in ps if abs(q - c_across) <= half_across]
            bars = []
            n = lay.n(P, 0.0)
            for q in ps:
                seg = self._span(along, q, n, c_along, half_along, c_t)
                if seg is None:
                    continue
                pts = [(seg[0], q, n), (seg[1], q, n)] if along == "x" else [(q, seg[0], n), (q, seg[1], n)]
                bars.append(kp.Bar(pts, len(bars), (0.0, 1.0, 0.0) if along == "x" else (1.0, 0.0, 0.0)))
            part.add("sup", "straight", d, u"надколонные верхние", bars)
        return built

    def draw(self, canvas, calc, built, pad):
        P = self.plate
        lx, ly = self.zone(calc)
        rx, ry = self.bx / 2.0 + lx + 300.0, self.by / 2.0 + ly + 300.0
        sk = kd.Sketch(canvas, (self.xc - rx, self.yc - ry, self.xc + rx, self.yc + ry), pad=pad)
        T = lambda x, y: (sk.X(x), sk.Y(y))
        for lp in slab_loops(self.floor, P):
            sk.poly(lp, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
        x0, x1 = self.xc - self.bx / 2.0, self.xc + self.bx / 2.0
        y0, y1 = self.yc - self.by / 2.0, self.yc + self.by / 2.0
        if self.col.round:
            r = self.bx / 2.0
            sk.poly([(self.xc + r * math.cos(a / 24.0 * math.pi), self.yc + r * math.sin(a / 24.0 * math.pi)) for a in range(48)],
                    kd.CONCRETE_EDGE, 1.5, "#FFE6E2DE", closed=True)
        else:
            sk.rect(x0, y0, x1, y1, kd.CONCRETE_EDGE, 1.5, "#FFE6E2DE")
        sk.rect(x0 - lx, y0 - ly, x1 + lx, y1 + ly, kd.CORNER, 0.8, dash=True)
        targets = {}
        sup = built.bars("sup")
        for bar in sup:
            sk.line(bar.pts[0][0], bar.pts[0][1], bar.pts[1][0], bar.pts[1][1], kd.CORNER, 1.6)
        left = [b for b in sup if abs(b.pts[0][1] - b.pts[1][1]) < 1]
        targets["sup"] = T(left[len(left) // 2].pts[0][0], left[len(left) // 2].pts[0][1]) if left else None
        pins = built.bars("pun")
        for bar in pins:
            sk.circle(bar.pts[0][0], bar.pts[0][1], 1.0, kd.STIRRUP, min_px=2.6)
        targets["pun"] = T(*max(pins, key=lambda b: (b.pts[0][0], -abs(b.pts[0][1] - self.yc))).pts[0][:2]) if pins else None
        sk.dim(x1, y1 + ly, x1 + lx, y1 + ly, u"{:.0f}".format(float(lx)), offset_px=10)
        sk.dim(x0, y0, x1, y0, u"{:.0f}".format(float(self.bx)), offset_px=12, outside="below")
        if pins:
            h0, s, rows, st, dp = self.punch(calc)
            sk.dim(x1, self.yc, x1 + s, self.yc, u"{:.0f}".format(float(s)), offset_px=8, outside="below")
        return targets


def slab_loops(floor, plate):
    """Контуры верхней грани плиты в локальных координатах плиты (для рисунка)."""
    loops = []
    try:
        for ref in DB.HostObjectUtils.GetTopFaces(floor):
            face = floor.GetGeometryObjectFromReference(ref)
            for lp in face.GetEdgesAsCurveLoops():
                pts = []
                for c in lp:
                    pts.extend(list(c.Tessellate())[:-1])
                loops.append([plate.local(q)[:2] for q in pts])
    except Exception:
        pass
    return loops


# ----------------------------------------------------------
# ПЛИТА – СТЕНА
# ----------------------------------------------------------
class SlabWallNode(object):
    """Стена под плитой. На краю плиты (SWE) — Г-стержни: от верхней сетки плиты через угол вниз в стену по её
    наружному вертикальному слою; над внутренней стеной (SWI) — надопорные верхние стержни поперёк стены.
    Координаты — мировые мм; m — от стены в сторону плиты (у SWI — нормаль стены)."""
    cards = ("gbar", "mesh")

    def __init__(self, floor, plate, wall, edge, m):
        self.kind = "SWE" if edge else "SWI"
        self.floor, self.plate, self.w, self.m = floor, plate, wall, m
        self.host = floor
        self.variant = VARIANTS[self.kind][0][0]
        self.Name = u"Плита–стена ({}) — {} / {}".format(u"край плиты" if edge else u"внутренняя стена",
                                                          element_title(floor), wall.title)
        self.comment = u"КЖ: узел SW {}-{}".format(floor.Id.IntegerValue, wall.wall.Id.IntegerValue)

    def top_layer(self, calc):
        """Слой верхней сетки плиты со стержнями поперёк стены."""
        layers, c_b, c_t = calc.slab_layers(self.plate)
        ex = (self.plate.ex.X, self.plate.ex.Y)
        lay = layers["ty"] if abs(dot(self.w.ex, ex)) > 0.7071 else layers["tx"]
        return lay, c_t

    def slab_room(self, start2, direction, z, cover):
        """Бетон плиты от точки start2 (мир, 2D) вдоль direction на отметке z, мм (минус защитный слой)."""
        P = self.plate
        a = P.local(DB.XYZ(ft(start2[0]), ft(start2[1]), ft(z)))
        e = add(start2, mul(direction, 20000.0))
        b = P.local(DB.XYZ(ft(e[0]), ft(e[1]), ft(z)))
        segs = P.chord(a, b)
        if not segs:
            return 0.0
        far = segs[0][1]
        return math.hypot(far[0] - a[0], far[1] - a[1]) - cover

    def positions(self, calc):
        W = self.w
        s = calc.so["sw_s"]
        if s:
            return kp.positions(W.plate.x0, W.plate.x1, 50.0, s)
        ps = kp.positions(W.plate.x0, W.plate.x1, norms.wall_rule("first_bar")["mm"], calc.wall_sv)
        return [(a + b) / 2.0 for a, b in zip(ps[:-1], ps[1:])]     # между вертикальными стержнями стены

    def legs(self, calc, d):
        if self.kind == "SWE":
            return calc.so["sw_ls"] or (self.w.t + calc.lap(d)), calc.so["sw_lw"] or calc.lap(d)
        return calc.so["sw_ls"] or norms.node_rule("support_top")["mm"], 0.0

    def build(self, calc):
        W, P, m = self.w, self.plate, self.m
        lay, c_t = self.top_layer(calc)
        d = calc.so["sw_d"] or lay.d
        z = lay.n(P, 0.0)
        z_slab_bot = P.face_lo(0.0)
        ls, lw = self.legs(calc, d)
        built = Built()
        part = Part(self.floor, WorldFrame())
        built.parts.append(part)
        bars = []
        n_mid = (W.n_lo + W.n_hi) / 2.0
        for x in self.positions(calc):
            probe = add(W.pt(x, n_mid), mul(m, W.t / 2.0 + 100.0))
            if P.depth_at(*P.local(DB.XYZ(ft(probe[0]), ft(probe[1]), 0.0))[:2]) is None:
                continue
            if self.kind == "SWE":
                side_out = -sign(dot(W.en, m))
                pc = W.pt(x, W.layer(side_out, calc, True))
                leg = min(ls, self.slab_room(pc, m, z, c_t))
                if leg < 200:
                    continue
                bars.append(kp.Bar([world(add(pc, mul(m, leg)), z), world(pc, z), world(pc, z_slab_bot - lw)], len(bars)))
            else:
                pc = W.pt(x, n_mid)
                la = min(W.t / 2.0 + ls, self.slab_room(pc, mul(m, -1.0), z, c_t))
                lb = min(W.t / 2.0 + ls, self.slab_room(pc, m, z, c_t))
                bars.append(kp.Bar([world(add(pc, mul(m, -la)), z), world(add(pc, mul(m, lb)), z)], len(bars),
                                   (W.ex[0], W.ex[1], 0.0)))
        if self.kind == "SWE":
            part.add("gw", "L", d, u"Г-стержни плита–стена", bars)
        else:
            part.add("gw", "straight", d, u"надопорные верхние", bars)
        return built

    def draw(self, canvas, calc, built, pad):
        """Разрез поперёк стены: s — от оси стены в сторону m, z — отметка."""
        W, P, m = self.w, self.plate, self.m
        lay, c_t = self.top_layer(calc)
        d = calc.so["sw_d"] or lay.d
        ls, lw = self.legs(calc, d)
        zb, zt = P.face_lo(0.0), P.face_hi(0.0)
        t = W.t
        edge = self.kind == "SWE"
        s_lo = -t / 2.0 if edge else -(t / 2.0 + ls + 300.0)
        s_hi = t / 2.0 + ls + 300.0
        z_lo = zb - max(lw, 600.0) - 300.0
        sk = kd.Sketch(canvas, (s_lo - 150, z_lo, s_hi, zt + 150), pad=pad, min_height_px=260)
        sk.rect(-t / 2.0, z_lo, t / 2.0, zb, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
        sk.rect(s_lo, zb, s_hi, zt, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
        sk.line(-t / 2.0, zb, t / 2.0, zb, kd.CONCRETE, 2.0)
        sk.line(s_hi, zb - 20, s_hi, zt + 20, kd.CONCRETE_EDGE, 0.8, dash=True)
        sk.line(-t / 2.0 - 20, z_lo, t / 2.0 + 20, z_lo, kd.CONCRETE_EDGE, 0.8, dash=True)
        n_mid = (W.n_lo + W.n_hi) / 2.0
        sgn = sign(dot(W.en, m))
        for side in (1.0, -1.0):
            s = (W.layer(side, calc, True) - n_mid) * sgn
            sk.line(s, z_lo + 20, s, zb - 30, kd.MAIN, 1.0, dash=True)
        layers, c_b, c_t2 = calc.slab_layers(P)
        for key in ("tx", "bx"):
            n = layers[key].n(P, 0.0)
            sk.line(s_lo + 30, n, s_hi - 30, n, kd.SECOND, 1.0, dash=True)
        targets = {}
        bars = built.bars("gw")
        if bars:
            bar = bars[len(bars) // 2]
            o = W.pt(W.local_x(bar.pts[1][:2]), n_mid)
            pts = [(dot(sub(q[:2], o), m), q[2]) for q in bar.pts]
            sk.poly(pts, kd.CORNER, 2.6)
            targets["gbar"] = (sk.X(pts[0][0]), sk.Y(pts[0][1])) if not edge else (sk.X(pts[-1][0]), sk.Y(pts[-1][1]))
            if edge:
                sk.dim(pts[1][0], pts[1][1], pts[0][0], pts[0][1], u"{:.0f}".format(float(abs(pts[0][0] - pts[1][0]))), offset_px=12)
                sk.dim(pts[1][0], zb, pts[2][0], pts[2][1], u"{:.0f}".format(float(zb - pts[2][1])), offset_px=12)
            else:
                sk.dim(t / 2.0, pts[1][1], pts[1][0], pts[1][1], u"{:.0f}".format(float(pts[1][0] - t / 2.0)), offset_px=12)
        else:
            targets["gbar"] = None
        targets["mesh"] = (sk.X(t / 2.0), sk.Y((zb + z_lo) / 2.0))
        sk.text(0.0, z_lo + 80, u"стена", size=11, bold=True)
        sk.text(s_hi - 250.0, zb - 90, u"плита", size=11, bold=True)
        return targets


# ----------------------------------------------------------
# КОЛОННА – ФУНДАМЕНТ
# ----------------------------------------------------------
class ColumnFoundationNode(object):
    """Выпуски из фундамента: по одному на каждый продольный стержень колонны, с лапками на нижнюю сетку или
    прямые на длину анкеровки; фиксирующие хомуты в теле фундамента. Координаты — мировые мм."""
    cards = ("dowel", "fix")

    def __init__(self, col, found, zf0, zf1):
        self.kind = "CF"
        self.col, self.found, self.zf0, self.zf1 = col, found, zf0, zf1
        self.host = found
        self.variant = VARIANTS["CF"][0][0]
        self.Name = u"Колонна–фундамент — {} / {}".format(col.title, element_title(found))
        self.comment = u"КЖ: узел CF {}-{}".format(found.Id.IntegerValue, col.column.Id.IntegerValue)

    def params(self, calc):
        o, so = calc.col, calc.so
        d = so["cf_d"] or (o["d"] if self.col.round else o["d_ct"])
        lap = so["cf_lap"] or calc.lap(d)
        foot = so["cf_foot"] or norms.ceil_to(max(norms.node_rule("dowel_foot")["k_d"] * d, norms.node_rule("dowel_foot")["mm"]))
        c_f = norms.cover_main("ground", d)
        z_foot = self.zf0 + c_f + 2 * 16.0 + d / 2.0          # над нижней сеткой фундамента (2 слоя Ø16 — практика)
        return d, lap, foot, z_foot

    def build(self, calc):
        col, so = self.col, calc.so
        d, lap, foot, z_foot = self.params(calc)
        z_top = col.z0 + lap
        built = Built()
        part = Part(self.found, WorldFrame())
        built.parts.append(part)
        lan = calc.anchorage(d)
        straight = self.variant == "cf_straight"
        if straight and col.z0 - lan < z_foot:
            built.notes.append(u"высоты фундамента не хватает на анкеровку {:.0f} мм — выпуски с лапками".format(float(lan)))
            straight = False
        groups = {}
        for u, v, out, face in col.bar_positions(calc, d):
            p = col.pt(u, v)
            if straight:
                pts = [world(p, col.z0 - lan), world(p, z_top)]
            else:
                o2 = add(mul(col.ax, out[0]), mul(col.ay, out[1]))
                pts = [world(add(p, mul(o2, foot)), z_foot), world(p, z_foot), world(p, z_top)]
            key = face if not col.round else 0
            grp = groups.setdefault(key, [])
            grp.append(kp.Bar(pts, len(grp), (col.ay[0], col.ay[1], 0.0) if out[0] else (col.ax[0], col.ax[1], 0.0)))
        for key in sorted(groups, key=lambda k: u"{}".format(k)):
            part.add("dw", "L" if not straight else "straight", d, u"выпуски", groups[key])
        if col.round:
            built.notes.append(u"круглая колонна — фиксирующие хомуты не ставятся")
        else:
            ds = so["cf_dh"] or calc.col["ds"]
            nh = int(so["cf_nh"] or norms.node_rule("fix_hoops")["value"])
            z_hi, z_lo = self.zf1 - 60.0, z_foot + 80.0
            zs = kp.spread(z_hi, max(z_lo, z_hi - 300.0 * (nh - 1)), nh) if nh > 1 else [z_hi]
            hoops = [kp.Bar(col.hoop(calc, ds, z), i) for i, z in enumerate(sorted(zs)) if z > z_lo - 1]
            part.add("fx", "stirrup", ds, u"фиксирующие хомуты", hoops)
        return built

    def draw(self, canvas, calc, built, pad):
        """Разрез по оси ax колонны: u — вдоль ax от центра, z — отметка."""
        col = self.col
        d, lap, foot, z_foot = self.params(calc)
        solids = kg.element_solids(self.found)
        zm = ft((self.zf0 + self.zf1) / 2.0)
        cpt = DB.XYZ(ft(col.c[0]), ft(col.c[1]), zm)
        axv = DB.XYZ(col.ax[0], col.ax[1], 0.0)
        segs = solid_chord(solids, cpt - axv * ft(20000), cpt + axv * ft(20000))
        u0, u1 = -col.b / 2.0 - 600.0, col.b / 2.0 + 600.0
        for s0, s1 in segs:
            a, b = (s0 - 0.5) * 40000.0, (s1 - 0.5) * 40000.0
            if a <= 0 <= b:
                u0, u1 = max(a, -2500.0), min(b, 2500.0)
        z_hi = col.z0 + lap + 500.0
        sk = kd.Sketch(canvas, (u0 - 150, self.zf0 - 150, u1 + 150, z_hi), pad=pad, min_height_px=300)
        sk.rect(u0, self.zf0, u1, self.zf1, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
        half = col.b / 2.0
        sk.rect(-half, col.z0, half, z_hi - 100, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
        sk.line(-half, self.zf1, half, self.zf1, kd.CONCRETE, 2.0)
        sk.line(-half - 20, z_hi - 100, half + 20, z_hi - 100, kd.CONCRETE_EDGE, 0.8, dash=True)
        sk.line(u0 + 40, z_foot - d / 2.0 - 16, u1 - 40, z_foot - d / 2.0 - 16, kd.SECOND, 1.0, dash=True)
        targets = {}
        to_uz = lambda q: (dot(sub(q[:2], col.c), col.ax), q[2])
        dw = built.bars("dw")
        for bar in dw:
            sk.poly([to_uz(q) for q in bar.pts], kd.CORNER, 2.0)
        if dw:
            right = max(dw, key=lambda b: to_uz(b.pts[-1])[0])
            targets["dowel"] = (sk.X(to_uz(right.pts[-1])[0]), sk.Y(to_uz(right.pts[-1])[1] - lap / 2.0))
            sk.dim(half, col.z0, half, col.z0 + lap, u"{:.0f}".format(float(lap)), offset_px=14, outside="right")
        else:
            targets["dowel"] = None
        fx = built.bars("fx")
        for bar in fx:
            us = [to_uz(q)[0] for q in bar.pts]
            sk.line(min(us), bar.pts[0][2], max(us), bar.pts[0][2], kd.STIRRUP, 2.0)
        targets["fix"] = (sk.X(max(to_uz(q)[0] for q in fx[0].pts)), sk.Y(fx[0].pts[0][2])) if fx else None
        sk.text(0.0, z_hi - 40, u"колонна", size=11, bold=True)
        sk.text((u0 + u1) / 2.0, self.zf0 + 60, u"фундамент", size=11, bold=True)
        return targets


# ----------------------------------------------------------
# БАЛКА – КОЛОННА
# ----------------------------------------------------------
class BeamColumnNode(object):
    """Балки, опирающиеся на колонну: хомуты колонны в зоне примыкания (по высоте балок) и, если балка неразрезная
    (балки с двух сторон на одной оси), — надопорные верхние стержни через колонну. Координаты — мировые мм."""
    cards = ("jhoop", "bsup")

    def __init__(self, col, beams):
        self.kind = "BC"
        self.col, self.beams = col, beams          # [(балка, BeamSection, направление от колонны в балку 2D)]
        self.host = col.column
        self.continuous = len(beams) == 2
        self.variant = VARIANTS["BC"][0][0] if self.continuous else VARIANTS["BC"][1][0]
        names = u" + ".join(element_title(b) for b, _, _ in beams)
        self.Name = u"Балка–колонна{} — {} / {}".format(u" (неразрезная)" if self.continuous else u"", col.title, names)
        self.comment = u"КЖ: узел BC {}-{}".format(col.column.Id.IntegerValue,
                                                     u"-".join(u"{}".format(b.Id.IntegerValue) for b, _, _ in beams))

    def z_range(self):
        zb = min(mm(s.origin.Z) for _, s, _ in self.beams)
        zt = max(mm(s.origin.Z) + mm(s.h) for _, s, _ in self.beams)
        return zb, zt

    def support(self, calc):
        """Надопорные: (Ø, число, вылет от грани, отметка оси, поперечные смещения от оси балки)."""
        o, so = calc.beam, calc.so
        beam, sec, u = self.beams[0]
        d_top, n_top = o["d_top"], int(o["n_top"])
        ds = o["dt"]
        auto = norms.ceil_to(max(norms.cover_main(o["cover_cond"], d_top), norms.cover_constructive(o["cover_cond"], ds) + ds), 5.0)
        c_t, c_s = o["c_t"] or auto, o["c_s"] or auto
        d = so["bc_d"] or d_top
        n = int(so["bc_n"] or 2)
        ls = so["bc_ls"] or norms.ceil_to(norms.node_rule("support_top")["k_span"] * min(mm(s.length) for _, s, _ in self.beams), 50.0)
        z = mm(sec.origin.Z) + mm(sec.h) - c_t - d_top / 2.0
        yt = mm(sec.b) / 2.0 - c_s - d_top / 2.0
        existing = kp.spread(-yt, yt, n_top) if n_top > 1 else [0.0]
        mids = [(a + b) / 2.0 for a, b in zip(existing[:-1], existing[1:])]
        vs = mids if len(mids) >= n else kp.spread(-yt * 0.5, yt * 0.5, n) if n > 1 else [0.0]
        return d, n, ls, z, vs[:n] if len(vs) > n else vs

    def build(self, calc):
        col, so = self.col, calc.so
        built = Built()
        zb, zt = self.z_range()
        hoop_part = Part(col.column, WorldFrame(), u" — хомуты колонны")
        built.parts.append(hoop_part)
        if col.round:
            built.notes.append(u"круглая колонна — хомуты в узле не ставятся (замкнутые круглые — кнопкой «Колонны»)")
        else:
            ds = so["bc_dh"] or calc.col["ds"]
            s = so["bc_s"] or norms.node_rule("joint_hoop_step")["mm"]
            hoops = [kp.Bar(col.hoop(calc, ds, z), i) for i, z in enumerate(kp.positions(zb, zt, 50.0, s))]
            hoop_part.add("jh", "stirrup", ds, u"хомуты колонны в узле", hoops)
        if self.continuous and self.variant == "bc_full":
            beam, sec, u = self.beams[0]
            d, n, ls, z, vs = self.support(calc)
            half = col.half_along(u)
            ny = (-u[1], u[0])
            part = Part(beam, WorldFrame(), u" — надопорные")
            bars = []
            for v in vs:
                base = add(col.c, mul(ny, v))
                bars.append(kp.Bar([world(add(base, mul(u, -(half + ls))), z), world(add(base, mul(u, half + ls)), z)],
                                   len(bars), (ny[0], ny[1], 0.0)))
            part.add("bs", "straight", d, u"надопорные верхние", bars)
            built.parts.append(part)
        elif not self.continuous:
            built.notes.append(u"балка с одной стороны — надопорные не нужны, анкеровка — кнопкой «Балки»")
        return built

    def draw(self, canvas, calc, built, pad):
        """Вид вдоль балки через центр колонны: s — вдоль балки от центра колонны, z — отметка."""
        col = self.col
        u = self.beams[0][2]
        half = col.half_along(u)
        zb, zt = self.z_range()
        d, n, ls, z_sup, vs = self.support(calc)
        R = half + ls + 500.0
        z_lo, z_hi = zb - 800.0, zt + 500.0
        sk = kd.Sketch(canvas, (-R, z_lo, R, z_hi), pad=pad, min_height_px=260)
        sk.rect(-half, z_lo, half, z_hi, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
        for beam, sec, ub in self.beams:
            sgn = sign(dot(ub, u))
            b0, b1 = (half, R - 60) if sgn > 0 else (-R + 60, -half)
            sk.rect(b0, mm(sec.origin.Z), b1, mm(sec.origin.Z) + mm(sec.h), kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
        c = calc.col_cover()
        dl = max(calc.col["d_ct"], calc.col["d"])
        for sgn in (-1.0, 1.0):
            x = sgn * (half - c - dl / 2.0)
            sk.line(x, z_lo + 20, x, z_hi - 20, kd.MAIN, 1.0, dash=True)
        targets = {}
        jh = built.bars("jh")
        for bar in jh:
            sk.line(-half + c, bar.pts[0][2], half - c, bar.pts[0][2], kd.STIRRUP, 1.6)
        targets["jhoop"] = (sk.X(half - c), sk.Y(jh[len(jh) // 2].pts[0][2])) if jh else None
        bs = built.bars("bs")
        if bs:
            sk.line(-half - ls, z_sup, half + ls, z_sup, kd.CORNER, 2.6)
            targets["bsup"] = (sk.X(-half - ls), sk.Y(z_sup))
            sk.dim(half, z_sup, half + ls, z_sup, u"{:.0f}".format(float(ls)), offset_px=14)
        else:
            targets["bsup"] = None
        sk.dim(-half, z_lo, half, z_lo, u"{:.0f}".format(float(2 * half)), offset_px=12, outside="below")
        sk.text(0.0, z_hi - 60, u"колонна", size=11, bold=True)
        return targets


# ----------------------------------------------------------
# ПОИСК УЗЛОВ СРЕДИ ВЫБРАННЫХ ЭЛЕМЕНТОВ
# ----------------------------------------------------------
def _cat(el):
    return el.Category.Id.IntegerValue if el.Category is not None else 0


def collect_all(elements):
    """Все узлы среди выбранных элементов: ([узлы], [пропуски])."""
    walls = [e for e in elements if isinstance(e, DB.Wall)]
    floors = [e for e in elements if isinstance(e, DB.Floor) and _cat(e) == int(DB.BuiltInCategory.OST_Floors)]
    founds = [e for e in elements if _cat(e) == int(DB.BuiltInCategory.OST_StructuralFoundation)]
    columns = [e for e in elements if _cat(e) == int(DB.BuiltInCategory.OST_StructuralColumns)]
    beams = [e for e in elements if _cat(e) == int(DB.BuiltInCategory.OST_StructuralFraming) and isinstance(e, DB.FamilyInstance)
             and u"плита" not in e.Symbol.Family.Name.lower()]
    nodes, skipped = collect(walls) if len(walls) > 1 else ([], [])
    infos = []
    for w in walls:
        try:
            plate, err = kp.wall_plate(w)
            if plate is not None and not plate.tapered and kr.is_valid_host(w):
                infos.append(WallInfo(w, plate))
        except Exception:
            pass
    cols = []
    for c in columns:
        try:
            sec, err = kg.column_section(c)
        except Exception as ex:
            sec, err = None, u"{}".format(ex)
        if sec is None:
            skipped.append(u"{} — {}".format(element_title(c), err))
        else:
            cols.append(ColumnFrame(c, sec))
    plates = []
    for f in floors:
        if not kr.is_valid_host(f):
            skipped.append(u"{} — Revit не допускает арматуру".format(element_title(f)))
            continue
        try:
            plate, err = kp.slab_plate(f)
        except Exception as ex:
            plate, err = None, u"{}".format(ex)
        if plate is None:
            skipped.append(u"{} — {}".format(element_title(f), err))
        else:
            plates.append((f, plate))
    # Плита – колонна, плита – стена
    for f, P in plates:
        lo, hi = P.face_lo(0.0), P.face_hi(0.0)
        sc = []
        for col in cols:
            if not (lo - 60 <= col.z1 <= hi + 60 and col.z0 < lo - 100):
                continue
            loc = P.local(col.sec.center)
            r = max(col.b, col.h) / 2.0 + 80.0
            if any(P.depth_at(loc[0] + dx, loc[1] + dy) is not None for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r), (0, 0))):
                sc.append(SlabColumnNode(f, P, col))
        for node in sc:          # пролёты — до соседних колонн под этой же плитой
            for axis in (0, 1):
                dists = [abs((o.xc, o.yc)[axis] - (node.xc, node.yc)[axis]) for o in sc if o is not node
                         and abs((o.xc, o.yc)[1 - axis] - (node.xc, node.yc)[1 - axis]) < abs((o.xc, o.yc)[axis] - (node.xc, node.yc)[axis])]
                node.spans[axis] = min(dists) if dists else None
        nodes.extend(sc)
        for W in infos:
            if not (lo - 60 <= W.z1 <= lo + 60):
                continue
            n_mid = (W.n_lo + W.n_hi) / 2.0
            inside = lambda p2: P.depth_at(*P.local(DB.XYZ(ft(p2[0]), ft(p2[1]), 0.0))[:2]) is not None
            if sum(1 for k in (0.25, 0.5, 0.75) if inside(W.pt(W.length * k, n_mid))) < 2:
                continue
            xm = W.length / 2.0
            plus = inside(W.pt(xm, n_mid + W.t / 2.0 + 250.0))
            minus = inside(W.pt(xm, n_mid - W.t / 2.0 - 250.0))
            if plus and minus:
                nodes.append(SlabWallNode(f, P, W, False, W.en))
            elif plus or minus:
                nodes.append(SlabWallNode(f, P, W, True, W.en if plus else mul(W.en, -1.0)))
    # Колонна – фундамент
    for fd in founds:
        if not kr.is_valid_host(fd):
            skipped.append(u"{} — Revit не допускает арматуру".format(element_title(fd)))
            continue
        solids = kg.element_solids(fd)
        for col in cols:
            p = DB.XYZ(ft(col.c[0]), ft(col.c[1]), ft(col.z0))
            segs = solid_chord(solids, p - DB.XYZ(0, 0, ft(8000)), p + DB.XYZ(0, 0, ft(60)))
            for s0, s1 in segs:
                z0, z1 = col.z0 - 8000 + s0 * 8060, col.z0 - 8000 + s1 * 8060
                if abs(z1 - col.z0) < 61:
                    nodes.append(ColumnFoundationNode(col, fd, z0, z1))
                    break
    # Балка – колонна
    for col in cols:
        ends = []
        for bm in beams:
            try:
                sec, err = kg.beam_section(bm)
            except Exception:
                sec = None
            if sec is None:
                continue
            for x_end, inward in ((0.0, 1.0), (sec.length, -1.0)):
                e = sec.point(x_end, 0.0, sec.h / 2.0)
                du = mm((e.X - ft(col.c[0])) * col.ax[0] + (e.Y - ft(col.c[1])) * col.ax[1])
                dv = mm((e.X - ft(col.c[0])) * col.ay[0] + (e.Y - ft(col.c[1])) * col.ay[1])
                if abs(du) <= col.b / 2.0 + 60 and abs(dv) <= col.h / 2.0 + 60 and col.z0 - 60 <= mm(e.Z) <= col.z1 + 60:
                    a2 = (sec.a.X * inward, sec.a.Y * inward)
                    ln = math.hypot(*a2)
                    ends.append((bm, sec, (a2[0] / ln, a2[1] / ln)))
        used = set()
        for i, (b1, s1, u1) in enumerate(ends):
            if i in used:
                continue
            pair = None
            for j in range(i + 1, len(ends)):
                if j not in used and dot(u1, ends[j][2]) < -0.999:
                    pair = j
                    break
            if pair is not None:
                used.update((i, pair))
                nodes.append(BeamColumnNode(col, [ends[i], ends[pair]]))
            else:
                used.add(i)
                nodes.append(BeamColumnNode(col, [ends[i]]))
    return nodes, skipped


# ==========================================================
# ПРЕДПРОСМОТР: ПЛАН УЗЛА
# ==========================================================
def node_outline(node, R):
    """Контур стен узла в плане (мм) в пределах R от точки узла."""
    A, B = node.a, node.b
    X = lambda W1, n1, W2, n2: intersect(W1.line(n1)[0], W1.ex, W2.line(n2)[0], W2.ex)
    if node.kind == "L":
        ua, ub = node.ua, node.ub
        in_a, in_b = sign(dot(A.en, ub)), sign(dot(B.en, ua))
        c_out = X(A, A.face(-in_a), B, B.face(-in_b))
        c_in = X(A, A.face(in_a), B, B.face(in_b))
        xa = A.local_x(node.P) + dot(ua, A.ex) * R
        xb = B.local_x(node.P) + dot(ub, B.ex) * R
        return [A.pt(xa, A.face(-in_a)), c_out, B.pt(xb, B.face(-in_b)), B.pt(xb, B.face(in_b)), c_in, A.pt(xa, A.face(in_a))], \
            [(A.pt(xa, A.face(-in_a)), A.pt(xa, A.face(in_a))), (B.pt(xb, B.face(-in_b)), B.pt(xb, B.face(in_b)))]
    S, W, us = A, B, node.ua
    near = sign(dot(W.en, us))
    xp = W.local_x(node.P)
    xs = S.local_x(node.P) + dot(us, S.ex) * R
    n_plus = X(S, S.face(1.0), W, W.face(near))
    n_minus = X(S, S.face(-1.0), W, W.face(near))
    if dot(sub(n_plus, node.P), W.ex) < 0:
        na, sa, nb, sb = n_plus, 1.0, n_minus, -1.0
    else:
        na, sa, nb, sb = n_minus, -1.0, n_plus, 1.0
    pts = [W.pt(xp - R, W.face(near)), na, S.pt(xs, S.face(sa)), S.pt(xs, S.face(sb)), nb, W.pt(xp + R, W.face(near)),
           W.pt(xp + R, W.face(-near)), W.pt(xp - R, W.face(-near))]
    breaks = [(W.pt(xp - R, W.face(near)), W.pt(xp - R, W.face(-near))), (W.pt(xp + R, W.face(near)), W.pt(xp + R, W.face(-near))),
              (S.pt(xs, S.face(sa)), S.pt(xs, S.face(sb)))]
    return pts, breaks


def draw_node(canvas, calc, node, plan, pad):
    """План узла на отметке первого ряда: контур, сетки стен (пунктир), стержни узла, вертикальные. Цели выносок."""
    leg = max(calc.leg, 300.0)
    R = leg + max(node.a.t, node.b.t) + 250.0
    P = node.P
    pts, breaks = node_outline(node, R)
    # Рамка рисунка — по контуру стен узла (угол L занимает одну четверть квадрата вокруг узла)
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    m = 120.0
    sk = kd.Sketch(canvas, (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m), pad=pad)
    T = lambda p: (sk.X(p[0]), sk.Y(p[1]))
    sk.poly(pts, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
    for a, b in breaks:
        sk.line(a[0], a[1], b[0], b[1], kd.CONCRETE, 2.5)
        sk.line(a[0], a[1], b[0], b[1], kd.CONCRETE_EDGE, 0.8, dash=True)
    # Горизонтальные сетки стен — пунктиром (их ставит кнопка «Стены»)
    for W, u in ((node.a, node.ua), (node.b, node.ub)):
        if u is None:
            u = W.ex
            ends = (-R, R)
        else:
            ends = (0.0, R)
        xp = W.local_x(P)
        for side in (1.0, -1.0):
            n = W.layer(side, calc, False)
            a = W.pt(xp + dot(u, W.ex) * ends[0], n)
            b = W.pt(xp + dot(u, W.ex) * ends[1], n)
            sk.line(a[0], a[1], b[0], b[1], kd.SECOND, 1.0, dash=True)
    targets = {}
    g = plan.all("g")
    u = plan.all("u")
    z_row = plan.rows[0] if plan.rows else None
    for bar in u:
        if bar.idx == 0:
            sk.poly([(q[0], q[1]) for q in bar.pts], kd.STIRRUP, 2.6)
            targets.setdefault("bars", T(bar.pts[1]))
    for bar in g:
        if bar.idx == 0:
            sk.poly([(q[0], q[1]) for q in bar.pts], kd.CORNER, 2.6)
            targets.setdefault("bars", T(bar.pts[1]))
    if not u and not g:
        targets["bars"] = None
    # Выноски — к точкам со стороны карточки: «Длина ножек» (слева) — к левому концу ножки,
    # «Вертикальные» (справа) — к правому вертикальному стержню
    ends = [q for b in g + u if b.idx == 0 for q in (b.pts[0], b.pts[-1])]
    if ends:
        targets["legs"] = T(min(ends, key=lambda q: q[0]))
    verts = plan.all("v")
    for bar in verts:
        sk.circle(bar.pts[0][0], bar.pts[0][1], calc.dvn / 2.0, kd.MAIN, min_px=4.0)
    targets["vert"] = T(max(verts, key=lambda b: b.pts[0][0]).pts[0]) if verts else None
    targets.setdefault("legs", targets.get("bars"))
    # Длина ножки — размер вдоль первой ножки первого Г-стержня (или П-стержня)
    first = next((b for b in g if b.idx == 0), None) or next((b for b in u if b.idx == 0), None)
    if first is not None:
        a, b = first.pts[0], first.pts[1]
        sk.dim(a[0], a[1], b[0], b[1], u"L = {:.0f}".format(float(math.hypot(a[0] - b[0], a[1] - b[1]))))
    for W, label in ((node.a, u"стена 1" if node.kind == "L" else u"примыкающая"),
                     (node.b, u"стена 2" if node.kind == "L" else u"сквозная")):
        u_dir = node.ua if W is node.a else (node.ub or W.ex)
        c = add(P, mul(u_dir, R * 0.8))
        off = mul(W.en, (W.t / 2.0 + 60.0))
        sk.text(c[0] + off[0], c[1] + off[1], label, size=11, bold=True)
    # «Сетки стен» (справа) — к правому концу пунктира сетки стены 1 / сквозной стены
    wa = node.b if node.kind == "T" else node.a
    xa = wa.local_x(P)
    cand = [wa.pt(xa + s_ * R * 0.7, wa.layer(1.0, calc, False)) for s_ in (1.0, -1.0)]
    targets["mesh"] = T(max(cand, key=lambda q: q[0]))
    return targets


# ==========================================================
# ОКНО
# ==========================================================
XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Армирование узлов" Width="1480" Height="900" MinWidth="1100" MinHeight="700"
        WindowStartupLocation="CenterScreen" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="CanResizeWithGrip"
        FontFamily="Segoe UI" FontSize="14" Foreground="#FF2B2118">
    <Window.Resources>
__RESOURCES__
    </Window.Resources>

    <Grid Margin="14">
        <Border CornerRadius="26" BorderThickness="1" BorderBrush="#CCFFFFFF">
            <Border.Background>
                <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
                    <GradientStop Color="#F5FFF9F2" Offset="0"/>
                    <GradientStop Color="#F5FFF2E5" Offset="1"/>
                </LinearGradientBrush>
            </Border.Background>

            <Grid ClipToBounds="True">
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                </Grid.RowDefinitions>
__TITLEBAR__
                <Grid Grid.Row="1" Margin="18,0,18,18">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="235"/>
                        <ColumnDefinition Width="16"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>

                    <!-- ЛЕВОЕ МЕНЮ НАВИГАЦИИ -->
                    <Border Grid.Column="0" Style="{StaticResource GlassCard}" Padding="14">
                        <Grid>
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <TextBlock Grid.Row="0" Text="КЖ • УЗЛЫ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navSection" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="План узла"/>
                                <RadioButton Name="navParams" GroupName="nav" Style="{StaticResource NavItem}" Content="Параметры"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="СТЕНЫ / УЗЛЫ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                    <TextBlock Name="tbCount" Text="—" FontSize="24" FontWeight="Bold" Foreground="#FFFF6A00" Margin="0,2,0,0"/>
                                    <TextBlock Name="tbDoc" Text="Модель" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis"/>
                                </StackPanel>
                            </Border>
                        </Grid>
                    </Border>

                    <!-- ПРАВАЯ РАБОЧАЯ ОБЛАСТЬ -->
                    <Grid Grid.Column="2">
                        <Grid.RowDefinitions>
                            <RowDefinition Height="Auto"/>
                            <RowDefinition Height="*"/>
                            <RowDefinition Height="Auto"/>
                        </Grid.RowDefinitions>
                        <Border Grid.Row="0" Style="{StaticResource GlassCard}" Padding="16,10" Margin="0,0,0,12">
                            <StackPanel>
                                <Grid>
                                    <Grid.ColumnDefinitions>
                                        <ColumnDefinition Width="*"/>
                                        <ColumnDefinition Width="Auto"/>
                                    </Grid.ColumnDefinitions>
                                    <ComboBox Name="cbGroup" Style="{StaticResource PillComboBox}" Tag="Узел:"/>
                                    <Button Name="btnSkipped" Grid.Column="1" Style="{StaticResource GlassButton}" Margin="10,0,0,0"/>
                                </Grid>
                                <WrapPanel Margin="0,10,0,0">
                                    <TextBlock Text="Армирование узла:" Style="{StaticResource Inline}" Margin="0,0,10,0"/>
                                    <RadioButton Name="rbV1" GroupName="variant" Style="{StaticResource ModePill}"/>
                                    <RadioButton Name="rbV2" GroupName="variant" Style="{StaticResource ModePill}"/>
                                    <TextBlock Name="tbVariant" Style="{StaticResource Hint}" VerticalAlignment="Center" Margin="12,0,0,0" MaxWidth="560"/>
                                </WrapPanel>
                            </StackPanel>
                        </Border>

                        <!-- РАЗДЕЛ 1: ПЛАН УЗЛА -->
                        <Border Name="pageSection" Grid.Row="1" Style="{StaticResource GlassCard}" Padding="10" Visibility="Visible">
                            <Grid>
                                <Grid.RowDefinitions>
                                    <RowDefinition Height="*"/>
                                    <RowDefinition Height="Auto"/>
                                </Grid.RowDefinitions>
                                <Border Background="#FFFFFFFF" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="16" MinHeight="360">
                                    <Grid>
                                        <Canvas Name="cnvSection" ClipToBounds="True"/>
                                        <Canvas Name="cnvFields" ClipToBounds="True"/>
                                    </Grid>
                                </Border>
                                <TextBlock Name="tbSectionInfo" Grid.Row="1" Style="{StaticResource Hint}" Margin="4,8,0,0"/>
                            </Grid>
                        </Border>

                        <!-- РАЗДЕЛ 2: ПАРАМЕТРЫ -->
                        <ScrollViewer Name="pageParams" Grid.Row="1" Visibility="Collapsed" VerticalScrollBarVisibility="Auto">
                            <WrapPanel Margin="0,0,6,0" ItemWidth="430">
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="Материалы" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <ComboBox Name="cbSteel" Style="{StaticResource PillComboBox}" Tag="Арматура:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbConcrete" Style="{StaticResource PillComboBox}" Tag="Бетон:"/>
                                        <TextBlock Style="{StaticResource Hint}" Text="Класс арматуры и бетон — для длины нахлёста ножек по СП 63."/>
                                    </StackPanel>
                                </Border>
                            </WrapPanel>
                        </ScrollViewer>

                        <!-- НИЖНЯЯ ПАНЕЛЬ -->
                        <Grid Grid.Row="2" Margin="4,12,4,0">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="Auto"/>
                            </Grid.ColumnDefinitions>
                            <TextBlock Name="tbInfo" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap" Margin="0,0,12,0"/>
                            <Button Name="btnRun" Grid.Column="1" Content="Армировать" Style="{StaticResource PrimaryButton}"/>
                        </Grid>
                    </Grid>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>"""

CONTROL_NAMES = ("tbCount", "tbDoc", "cbGroup", "btnSkipped", "rbV1", "rbV2", "tbVariant", "cnvSection", "cnvFields",
                 "tbSectionInfo", "cbSteel", "cbConcrete", "tbInfo", "btnRun")
NAV_PAGES = (("navSection", "pageSection"), ("navParams", "pageParams"))

VARIANT_NOTES = {
    "l_bars": u"Угловые Г-стержни у наружной и у внутренней граней в каждом ряду, ножки — внахлёст с горизонтальными стержнями обеих стен.",
    "l_u": u"П-стержни каждой стены заходят за угол до наружной сетки другой стены; у наружной грани — Г-стержни.",
    "t_u": u"П-стержни примыкающей стены охватывают дальнюю сетку сквозной стены; ножки — внахлёст с горизонтальными стержнями примыкающей стены.",
    "t_bars": u"Стержни у граней примыкающей стены доходят до дальней сетки сквозной стены и загибаются вдоль неё.",
    "sc_full": u"Шпильки продавливания рядами вокруг колонны (цепляют нижнюю и верхнюю сетки) и дополнительная верхняя арматура над колонной.",
    "sc_top": u"Только дополнительная верхняя арматура над колонной — если продавливание воспринимает бетон (по расчёту).",
    "swe_g": u"Г-стержни: от верхней сетки плиты через угол вниз в стену по её наружному вертикальному слою, между вертикальными стержнями стены.",
    "swi_top": u"Надопорные верхние стержни поперёк стены — над внутренней стеной под неразрезной плитой.",
    "cf_foot": u"Выпуски на каждый продольный стержень колонны: лапками на нижнюю сетку фундамента, вверх — на длину нахлёста.",
    "cf_straight": u"Прямые выпуски: заделка в фундамент на длину анкеровки (если высоты фундамента не хватает — с лапками).",
    "bc_full": u"Хомуты колонны с частым шагом по высоте балок и надопорные верхние стержни через колонну (балки с двух сторон).",
    "bc_hoops": u"Только хомуты колонны в зоне примыкания балок; анкеровку стержней балки ставит кнопка «Балки».",
}
VIEW_NOTES = {"L": u"План — на отметке первого ряда; сетки стен — пунктиром.", "T": u"План — на отметке первого ряда; сетки стен — пунктиром.",
              "SC": u"План у колонны: шпильки — точки, надколонные — зелёные, зона — пунктиром.",
              "SWE": u"Разрез поперёк стены: сетки — пунктиром, стержень узла — зелёный.",
              "SWI": u"Разрез поперёк стены: сетки — пунктиром, стержень узла — зелёный.",
              "CF": u"Разрез по оси колонны: выпуски — зелёные, фиксирующие хомуты — синие.",
              "BC": u"Вид вдоль балки: хомуты колонны — синие, надопорные — зелёные."}
GBAR_NOTES = {"SWE": u"Край плиты на стене: Г-стержни — вылет в плиту от наружной грани стены и ножка вниз в стену (внахлёст с её вертикальными стержнями).",
              "SWI": u"Внутренняя стена под плитой: надопорные стержни поперёк стены у верхней грани плиты — вылет от каждой грани стены."}


def _cond_options():
    return [Option(u"{} мм — {}".format(v, t), k) for k, t, v in norms.cover_conditions()]


FIELDS = [
    ("bars", "L", {"title": u"Стержни узла",
                   "note": u"Г- и П-стержни ставятся в каждом ряду горизонтальной арматуры стен, рядом с её стержнями. "
                           u"Ряды — как у кнопки «Стены»: первый от низа и шаг.",
                   "rows": [[u"Ø", ("diam", "dg")], [u"Шаг рядов", ("num", "sh", 50), u"мм"],
                            [u"Первый ряд от низа", ("num", "first", 46), u"мм"], [("hint", "first")]]}),
    ("legs", "L", {"title": u"Длина ножек",
                   "note": u"Ножки перекрываются с горизонтальными стержнями стен на длину нахлёста. Пусто — по СП 63; "
                           u"у проёмов и торцов ножки укорачиваются по бетону.",
                   "rows": [[u"L", ("num", "leg", 52), u"мм"], [("hint", "leg")]]}),
    ("mesh", "R", {"title": u"Сетки стен",
                   "note": u"Как в кнопке «Стены»: по ним находятся оси стержней узла (рядом со стержнями стен, не поперёк).",
                   "rows": [[u"Горизонтальные Ø", ("diam", "dh")], [u"Вертикальные Ø", ("diam", "dv")],
                            [u"Защитный слой", ("num", "c", 46), u"мм", ("combo", "cond", _cond_options(), 170)], [("hint", "c")],
                            [("combo", "hor_out", [Option(u"горизонтальные снаружи", True), Option(u"вертикальные снаружи", False)], 240)]]}),
    ("vert", "R", {"title": u"Вертикальные стержни в узле",
                   "note": u"Четыре стержня в ядре узла — там, где пересекаются вертикальные слои сеток стен.",
                   "rows": [[("combo", "vert", [Option(u"ставить", True), Option(u"не ставить", False)], 140)],
                            [u"Ø", ("diam", "dvn")],
                            [u"Верх", ("combo", "top", [Option(u"защитный слой", TOP_COVER), Option(u"выпуск на нахлёст", TOP_LAP)], 200)]]}),
    # Плита – колонна
    ("pun", "R", {"title": u"Продавливание",
                  "note": u"Шпильки рядами вокруг колонны: шаг рядов и шаг вдоль контура не более h0/3, зона — не менее 1,5·h0 "
                          u"от грани (СП 63, проверить расчётом). У края плиты ряды обрезаются.",
                  "rows": [[u"Ø", ("diam", "sc_dp")], [u"Шаг рядов", ("num", "sc_s", 50), u"мм"],
                           [u"Рядов", ("num", "sc_rows", 40)], [u"Шаг вдоль контура", ("num", "sc_st", 50), u"мм"],
                           [("hint", "sc_s")]]}),
    ("sup", "L", {"title": u"Надколонная верхняя арматура",
                  "note": u"Дополнительные стержни верхней сетки над колонной — между стержнями сетки, в обоих направлениях, "
                          u"на вылет от граней колонны.",
                  "rows": [[u"Ø", ("diam", "sc_dsup")], [u"Вылет от грани", ("num", "sc_lz", 52), u"мм"], [("hint", "sc_lz")]]}),
    # Плита – стена
    ("gbar", "L", {"title": u"Стержни узла плита–стена", "note": u" ",
                   "rows": [[u"Ø", ("diam", "sw_d")], [u"Шаг вдоль стены", ("num", "sw_s", 50), u"мм"],
                            [u"Вылет в плиту", ("num", "sw_ls", 52), u"мм"], [u"Ножка в стену", ("num", "sw_lw", 52), u"мм"],
                            [("hint", "sw_ls")]]}),
    # Колонна – фундамент
    ("dowel", "L", {"title": u"Выпуски из фундамента",
                    "note": u"По одному на каждый продольный стержень колонны (раскладка — как в кнопке «Колонны»). "
                            u"Вверх — на длину нахлёста со стержнями колонны.",
                    "rows": [[u"Ø", ("diam", "cf_d")], [u"Нахлёст", ("num", "cf_lap", 52), u"мм"],
                             [u"Лапка", ("num", "cf_foot", 52), u"мм"], [("hint", "cf_lap")]]}),
    ("fix", "R", {"title": u"Фиксирующие хомуты",
                  "note": u"В теле фундамента, держат выпуски в проектном положении при бетонировании.",
                  "rows": [[u"Ø", ("diam", "cf_dh")], [u"Количество", ("num", "cf_nh", 40), u"шт."]]}),
    # Балка – колонна
    ("jhoop", "R", {"title": u"Хомуты колонны в узле",
                    "note": u"По высоте примыкающих балок — с частым шагом (практика: не более 100 мм).",
                    "rows": [[u"Ø", ("diam", "bc_dh"), u"шаг", ("num", "bc_s", 50), u"мм"], [("hint", "bc_s")]]}),
    ("bsup", "L", {"title": u"Надопорные стержни балок",
                   "note": u"Над промежуточной колонной неразрезной балки: у верхней грани между стержнями балки, "
                           u"на вылет от граней колонны.",
                   "rows": [[u"Количество", ("num", "bc_n", 40), u"шт. Ø", ("diam", "bc_d")],
                            [u"Вылет от грани", ("num", "bc_ls", 52), u"мм"], [("hint", "bc_ls")]]}),
]


class NodesWindow(ui.KzhWindow):
    def __init__(self, nodes, skipped, total):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES, NAV_PAGES)
        self.result = None
        self.nodes = nodes
        self.skipped = skipped
        self.current = None
        self._loading = False
        self.tbCount.Text = u"{} / {}".format(total, len(nodes))
        self.tbDoc.Text = doc.Title
        self.btnSkipped.Content = u"Пропущено: {}".format(len(skipped))
        self.btnSkipped.Visibility = Visibility.Visible if skipped else Visibility.Collapsed
        self.btnSkipped.Click += lambda s, e: show_message(
            u"Эти стены и пересечения армироваться не будут.", details=self.skipped,
            details_header=u"Пропущено: {}".format(len(self.skipped)))
        self.editor = SectionEditor(self.window, self.cnvSection, self.cnvFields, self.refresh)
        self.editor.build(FIELDS)
        for key in ("dg", "dvn", "sc_dp", "sc_dsup", "sw_d", "cf_d", "cf_dh", "bc_dh", "bc_d"):
            cb = self.editor.inputs[key][1]
            cb.ItemsSource = [Option(u"авто", None)] + list(cb.ItemsSource)
            cb.Width = 104.0
        self.cbSteel.ItemsSource = [Option(s, s) for s in norms.steel_classes()]
        self.cbConcrete.ItemsSource = [Option(c, c) for c in norms.concrete_classes()]

        saved = settings.load(SETTINGS_SECTION)
        self.saved = saved
        self.opts = dict(OPTS_DEFAULTS)
        # Сетки стен — из настроек кнопки «Стены» (две сетки), затем сохранённые настройки узлов
        walls = settings.load("walls").get("mesh_double") or {}
        for src, dst in (("dh_e", "dh"), ("dv_e", "dv"), ("sh_e", "sh"), ("c_e", "c"), ("cond_e", "cond"), ("hor_out", "hor_out")):
            if walls.get(src) is not None:
                self.opts[dst] = walls[src]
        self.opts.update(dict((k, v) for k, v in (saved.get("opts") or {}).items() if k in self.opts))
        for node in nodes:
            v = (saved.get("variants") or {}).get(node.kind)
            if v in dict(VARIANTS[node.kind]):
                node.variant = v
        g = dict(GENERAL_DEFAULTS)
        g.update(dict((k, v) for k, v in (saved.get("general") or {}).items() if k in g))
        ui.select_option(self.cbSteel, g["steel"])
        ui.select_option(self.cbConcrete, g["concrete"])
        self.editor.load(self.opts)

        self.rbV1.Checked += lambda s, e: self.on_variant(0)
        self.rbV2.Checked += lambda s, e: self.on_variant(1)
        self.cbGroup.ItemsSource = nodes
        self.cbGroup.SelectionChanged += self.on_node_changed
        for cb in (self.cbSteel, self.cbConcrete):
            cb.SelectionChanged += lambda s, e: self.refresh()
        self.cnvSection.SizeChanged += lambda s, e: self.refresh()
        self.btnRun.Click += self.on_run
        self.cbGroup.SelectedIndex = 0

    def read_general(self):
        return {"steel": ui.selected_value(self.cbSteel), "concrete": ui.selected_value(self.cbConcrete)}

    def read_opts(self):
        o = self.opts
        self.editor.read(o, [k for k in self.editor.inputs])
        for key in ("dh", "dv"):
            if o[key] is None:
                o[key] = OPTS_DEFAULTS[key]
        if not o["sh"] or o["sh"] < 50:
            return u"Шаг рядов — не меньше 50 мм."
        for key in ("c", "first", "leg"):
            if o[key] is not None and o[key] < 0:
                return u"Значения не могут быть отрицательными."
        return None

    def on_node_changed(self, sender, args):
        node = self.cbGroup.SelectedItem
        if node is None:
            return
        self.current = node
        self._loading = True
        try:
            variants = VARIANTS[node.kind]
            self.rbV1.Content = variants[0][1]
            self.rbV1.IsChecked = node.variant == variants[0][0]
            single = len(variants) == 1 or (node.kind == "BC" and not node.continuous)
            self.rbV2.Visibility = Visibility.Collapsed if single else Visibility.Visible
            if not single:
                self.rbV2.Content = variants[1][1]
                self.rbV2.IsChecked = node.variant == variants[1][0]
        finally:
            self._loading = False
        self.refresh()

    def on_variant(self, index):
        variants = VARIANTS[self.current.kind] if self.current is not None else ()
        if self._loading or self.current is None or index >= len(variants):
            return
        self.current.variant = variants[index][0]
        self.refresh()

    def refresh(self):
        node = self.current
        if self._loading or node is None:
            return
        err = self.read_opts()
        if err:
            self.tbInfo.Text = err
            return
        try:
            calc = Calc(self.read_general(), self.opts)
            built = node.build(calc)
            self.editor.show_only(node.cards)
            self.set_hints(calc, node)
            self.tbVariant.Text = VARIANT_NOTES[node.variant]
            targets = node.draw(self.cnvSection, calc, built, self.editor.pad(top=30, bottom=30))
            self.editor.place(targets)
        except Exception as ex:
            self.tbInfo.Text = u"Проверьте значения в полях ({})".format(ex)
            return
        info = u"{} • стержней: {}".format(node.Name, counts_text(built) or u"нет")
        notes = built.notes + calc.warnings
        if notes:
            info += u" • Внимание: " + u"; ".join(notes)
        self.tbSectionInfo.Text = info + u" • " + VIEW_NOTES[node.kind]
        self.tbInfo.Text = self.status(calc, node)

    def status(self, calc, node):
        """Строка состояния: главное по типу узла."""
        if node.kind == "SC":
            h0, s, rows, st, dp = node.punch(calc)
            lx, ly = node.zone(calc)
            return u"h0 = {:.0f} мм • шпильки ⌀{}: шаг {:.0f} мм, рядов {} • надколонная: вылет {:.0f} × {:.0f} мм".format(
                float(h0), dp, float(s), rows, float(lx), float(ly))
        if node.kind in ("SWE", "SWI"):
            lay, c_t = node.top_layer(calc)
            d = calc.so["sw_d"] or lay.d
            ls, lw = node.legs(calc, d)
            return u"⌀{} • вылет в плиту {:.0f} мм{} • нахлёст ⌀{} — {:.0f} мм".format(
                d, float(ls), u", ножка в стену {:.0f} мм".format(float(lw)) if lw else u"", d, float(calc.lap(d)))
        if node.kind == "CF":
            d, lap, foot, z_foot = node.params(calc)
            return u"Выпуски ⌀{} • нахлёст {:.0f} мм • анкеровка {:.0f} мм • фундамент {:.0f} мм".format(
                d, float(lap), float(calc.anchorage(d)), float(node.zf1 - node.zf0))
        if node.kind == "BC":
            d, n, ls, z, vs = node.support(calc)
            return u"Надопорные {} × ⌀{}, вылет {:.0f} мм • хомуты колонны — шаг {:.0f} мм".format(
                n, d, float(ls), float(calc.so["bc_s"] or norms.node_rule("joint_hoop_step")["mm"]))
        return u"Нахлёст ⌀{} — {:.0f} мм • анкеровка — {:.0f} мм".format(
            calc.dh, float(calc.leg_auto), float(calc.anchorage(calc.dh)))

    def set_hints(self, calc, node):
        o, ed = self.opts, self.editor
        auto = lambda key, text: ed.set_hint(key, u"" if o.get(key) else u"пусто — " + text)
        if node.kind in ("L", "T"):
            ed.set_hint("first", u"" if o["first"] is not None else u"пусто — {:.0f}".format(float(calc.first)))
            ed.set_hint("leg", u"" if o["leg"] else u"пусто — {:.0f} (нахлёст ⌀{}, СП 63)".format(float(calc.leg_auto), calc.dh))
        ed.set_hint("c", u"" if o["c"] else u"пусто — {:.0f} по норме".format(float(calc.c_auto)))
        if node.kind == "SC":
            h0, s, rows, st, dp = node.punch(calc)
            lx, ly = node.zone(calc)
            auto("sc_s", u"h0/3 = {:.0f} мм; h0 = {:.0f}; рядов {} (до 1,5·h0)".format(float(s), float(h0), rows))
            auto("sc_lz", u"{:.0f} × {:.0f} мм (1/4 пролёта до соседних колонн или 1200)".format(float(lx), float(ly)))
        if node.kind in ("SWE", "SWI"):
            lay, c_t = node.top_layer(calc)
            d = o["sw_d"] or lay.d
            ls, lw = node.legs(calc, d)
            auto("sw_ls", u"вылет {:.0f}{}".format(float(ls), u", ножка {:.0f} (нахлёст)".format(float(lw)) if lw else u""))
            ed.set_note("gbar", GBAR_NOTES[node.kind])
        if node.kind == "CF":
            d, lap, foot, z_foot = node.params(calc)
            auto("cf_lap", u"нахлёст {:.0f}, лапка {:.0f} (⌀{}, СП 63)".format(float(lap), float(foot), d))
        if node.kind == "BC":
            d, n, ls, z, vs = node.support(calc)
            auto("bc_s", u"{:.0f} мм (практика)".format(float(norms.node_rule("joint_hoop_step")["mm"])))
            auto("bc_ls", u"{:.0f} (1/4 пролёта балки)".format(float(ls)))

    def on_run(self, sender, args):
        err = self.read_opts()
        if err:
            show_message(err)
            return
        ho = self.read_general()
        data = dict(self.saved)
        data["general"] = ho
        data["opts"] = dict(self.opts)
        variants = dict(data.get("variants") or {})
        if self.current is not None:
            variants[self.current.kind] = self.current.variant
        data["variants"] = variants
        settings.save(SETTINGS_SECTION, data)
        self.result = (ho, dict(self.opts))
        self.window.Close()


# ==========================================================
# ЗАПУСК
# ==========================================================
NODE_CATEGORIES = (DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_StructuralColumns,
                   DB.BuiltInCategory.OST_StructuralFraming, DB.BuiltInCategory.OST_StructuralFoundation)


class WallFilter(ISelectionFilter):
    def AllowElement(self, element):
        return element.Category is not None and element.Category.Id.IntegerValue in [int(c) for c in NODE_CATEGORIES]

    def AllowReference(self, reference, point):
        return False


def get_walls():
    flt = WallFilter()
    walls = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    walls = [w for w in walls if w is not None and flt.AllowElement(w)]
    if walls:
        return walls
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, u"Выберите элементы узлов (стены, плиты, колонны, балки, фундаменты) и нажмите «Готово»")
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: в семействе армирование узлов не выполняется.")
        return
    walls = get_walls()
    if not walls:
        return
    nodes, skipped = collect_all(walls)
    if not nodes:
        show_message(u"Среди выбранных элементов нет узлов. Узел — это стены, сходящиеся углом или примыканием; колонна "
                     u"или стена под плитой; колонна на фундаменте; балка, опирающаяся на колонну.",
                     details=skipped or None, details_header=u"Пропущено: {}".format(len(skipped)))
        return
    win = NodesWindow(nodes, skipped, len(walls))
    win.show_modal()
    if win.result is not None:
        ho, so = win.result
        reinforce(nodes, ho, so, skipped)


try:
    main()
except Exception:
    show_message(u"Ошибка армирования узлов:\n{}".format(traceback.format_exc()), title=u"Ошибка")

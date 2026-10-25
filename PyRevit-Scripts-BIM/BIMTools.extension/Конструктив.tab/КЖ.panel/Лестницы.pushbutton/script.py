# -*- coding: utf-8 -*-
"""Армирование монолитных лестниц (pyRevit / IronPython 2.7).

Поддерживаются лестницы Revit (Stairs), выполненные монолитно (CastInPlace), с прямыми маршами. Основа
арматуры — сама лестница (марши и площадки основой быть не могут), геометрия берётся по маршам и площадкам:
проступь, подступёнок, уклон и толщина марша — по сечениям тел. Сборные лестницы пропускаются.

Марш: нижняя рабочая сетка (продольные стержни вдоль уклона и поперечные), верхняя сетка (по выбору),
П-стержни по боковым граням, ступени — Г-стержень по контуру каждой ступени с шагом поперёк марша и продольный
стержень в углу ступени (первую и последнюю ступень можно не армировать). Концы рабочих стержней: низ —
выпуск в перекрытие на длину анкеровки или обрыв с защитным слоем, верх — заведение в площадку по бетону.
Площадки: нижняя и верхняя сетки, как у плиты. Несколько лестниц и маршей — за один запуск.
Значения практики помечены в norms.json «сверить».
"""
__title__ = u"Лестницы"
__author__ = u"Daniil Antonenko"
__doc__ = u"Армирование монолитных лестниц: сетки маршей, ступени, П-стержни по краям, площадки"

import math
import traceback

from pyrevit import DB
from Autodesk.Revit.DB.Architecture import Stairs
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

SETTINGS_SECTION = "stairs"
PART_NAME = u"Лестница"
WHAT = {"many": u"лестниц", "title": u"Армирование лестниц", "done": u"Армировано лестниц",
        "failed": u"Не армирована", "armed": u"уже армирована"}

LOW_LAP, LOW_COVER = "lap", "cover"
EDGE_BOTH, EDGE_LEFT, EDGE_RIGHT, EDGE_NONE = "both", "left", "right", "none"

GENERAL_DEFAULTS = {"steel": u"А500", "st_steel": u"А240", "concrete": u"В25"}
# Параметры. Марш: dm/sm — продольные (рабочие) Ø и шаг, dd/sd — поперечные; top — верхняя сетка, dt/st — её
# продольные; c — защитный слой (None — по норме), cond — условия; low — низ рабочих стержней.
# Ступени: dg/sg — Г-стержни Ø и шаг поперёк марша, dn — стержень в углу; first/last — армировать первую/последнюю.
# Края: edge — где П-стержни, du — их Ø. Площадки: dl1/sl1 — нижняя сетка, dl2/sl2 — верхняя.
OPTS_DEFAULTS = {"dm": None, "sm": None, "dd": None, "sd": None, "top": False, "dt": None, "st": None,
                 "c": None, "cond": "indoor_normal", "low": LOW_LAP,
                 "dg": None, "sg": None, "dn": None, "first": True, "last": True,
                 "edge": EDGE_BOTH, "du": None,
                 "dl1": None, "sl1": None, "dl2": None, "sl2": None}


# ==========================================================
# ГЕОМЕТРИЯ
# ==========================================================
def solid_chord(solids, a, b):
    """Участки отрезка a–b (XYZ) внутри тел: [(s0, s1)] в долях длины, слитые."""
    line = DB.Line.CreateBound(a, b)
    d = b - a
    dd = d.DotProduct(d)
    raw = []
    for solid in solids:
        try:
            res = solid.IntersectWithCurve(line, DB.SolidCurveIntersectionOptions())
        except Exception:
            continue
        for i in range(res.SegmentCount):
            seg = res.GetCurveSegment(i)
            t = sorted((seg.GetEndPoint(k) - a).DotProduct(d) / dd for k in (0, 1))
            raw.append((t[0], t[1]))
    raw.sort()
    out = []
    for s0, s1 in raw:
        if out and s0 <= out[-1][1] + 1e-4:
            out[-1] = (out[-1][0], max(out[-1][1], s1))
        else:
            out.append((s0, s1))
    return out


class WorldFrame(object):
    """Координаты стержней — мировые мм (X, Y, Z): для kp.run_specs."""

    def point(self, x, y, z):
        return DB.XYZ(ft(x), ft(y), ft(z))

    def vec(self, x, y, z):
        return DB.XYZ(ft(x), ft(y), ft(z))


class RunGeo(object):
    """Прямой монолитный марш в координатах (s, l, z), мм: s — по горизонтали вдоль пути подъёма от начала пути,
    l — поперёк (влево), z — отметка. Профиль (ступени, низ марша) — по вертикальным сечениям тела по оси марша."""

    def __init__(self, run, stair_solids):
        self.run = run
        path = list(run.GetStairsPath())
        if not path or any(not isinstance(c, DB.Line) for c in path):
            raise ValueError(u"марш не прямой — не поддерживается")
        a, b = path[0].GetEndPoint(0), path[-1].GetEndPoint(1)
        self.p0 = (mm(a.X), mm(a.Y))
        dx, dy = mm(b.X) - self.p0[0], mm(b.Y) - self.p0[1]
        self.L = math.hypot(dx, dy)
        self.d = (dx / self.L, dy / self.L)
        self.l = (-self.d[1], self.d[0])
        self.W = mm(run.ActualRunWidth)
        self.solids = kg.element_solids(run)
        self.stair_solids = stair_solids
        bb = run.get_BoundingBox(None)
        self.zmin, self.zmax = mm(bb.Min.Z) - 100.0, mm(bb.Max.Z) + 100.0
        self._profile()

    def P(self, s, l, z):
        return (self.p0[0] + self.d[0] * s + self.l[0] * l, self.p0[1] + self.d[1] * s + self.l[1] * l, z)

    def zrange(self, s, l, solids=None):
        x, y, _ = self.P(s, l, 0.0)
        segs = solid_chord(solids or self.solids, DB.XYZ(ft(x), ft(y), ft(self.zmin)), DB.XYZ(ft(x), ft(y), ft(self.zmax)))
        if not segs:
            return None
        span = self.zmax - self.zmin
        return self.zmin + segs[0][0] * span, self.zmin + segs[-1][1] * span

    def _profile(self):
        samples = []
        s = -300.0
        while s <= self.L + 300.0:
            samples.append((s, self.zrange(s, 0.0)))
            s += 10.0
        self.samples = samples
        inside = [(s, z) for s, z in samples if z is not None]
        if len(inside) < 10:
            raise ValueError(u"не удалось построить профиль марша")
        # Проступи — участки постоянной отметки верха; подступёнок — скачок между ними
        treads = []
        start, z0 = inside[0][0], inside[0][1][1]
        prev = inside[0][0]
        for s, (zb, zt) in inside[1:]:
            if abs(zt - z0) > 15.0 or s - prev > 15.0:
                treads.append((start, prev, z0))
                start, z0 = s, zt
            prev = s
        treads.append((start, prev, z0))
        treads = [t for t in treads if t[1] - t[0] >= 60.0]
        if len(treads) < 2:
            raise ValueError(u"в марше меньше двух ступеней")
        self.treads = treads
        rises = [b[2] - a[2] for a, b in zip(treads[:-1], treads[1:])]
        self.h = sorted(rises)[len(rises) // 2]
        self.g = sorted(t[1] - t[0] for t in treads)[len(treads) // 2]
        # Низ марша (наклонная плита): прямая по середине марша
        lo, hi = treads[0][1], treads[-1][0]
        pts = [(s, z[0]) for s, z in inside if lo <= s <= hi]
        if len(pts) < 5:
            pts = [(s, z[0]) for s, z in inside]
        n = float(len(pts))
        ms, mz = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
        sxx = sum((p[0] - ms) ** 2 for p in pts) or 1.0
        self.k = sum((p[0] - ms) * (p[1] - mz) for p in pts) / sxx
        self.a = mz - self.k * ms
        self.cos = 1.0 / math.sqrt(1.0 + self.k * self.k)
        self.sin = self.k * self.cos
        # Верх наклонной плиты — по внутренним углам ступеней (низ подступёнков)
        corners = [(t[0], t[2] - self.h) for t in treads[1:]]
        self.a_top = sum(z - self.k * s for s, z in corners) / len(corners)
        self.t = (self.a_top - self.a) * self.cos
        if self.t < 60:
            raise ValueError(u"не удалось определить толщину марша")

    def soffit(self, s):
        return self.a + self.k * s

    def at_normal(self, s, nn):
        """Точка (s, z) на нормали к низу марша в точке s на расстоянии nn от низа."""
        return s - nn * self.sin, self.soffit(s) + nn * self.cos

    def chord_along(self, l, nn, solids):
        """Участок прямой, параллельной низу марша (на нормали nn, сдвиг l), внутри тел: (s0, s1) у середины марша."""
        s_a, s_b = -4000.0, self.L + 4000.0
        pa = self.at_normal(s_a, nn)
        pb = self.at_normal(s_b, nn)
        A, B = self.P(pa[0], l, pa[1]), self.P(pb[0], l, pb[1])
        segs = solid_chord(solids, DB.XYZ(ft(A[0]), ft(A[1]), ft(A[2])), DB.XYZ(ft(B[0]), ft(B[1]), ft(B[2])))
        mid = (self.L / 2.0 - s_a) / (s_b - s_a)
        for t0, t1 in segs:
            if t0 <= mid <= t1:
                return s_a + t0 * (s_b - s_a), s_a + t1 * (s_b - s_a)
        return None


def stair_title(st):
    t = doc.GetElement(st.GetTypeId())
    return u"{} (id {})".format(element_name(t) if t is not None else u"?", st.Id.IntegerValue)


class StairItem(object):
    def __init__(self, stairs):
        self.stairs = stairs
        self.title = stair_title(stairs)
        self.Name = self.title
        self.runs, self.landings, self.notes = [], [], []
        comps = [doc.GetElement(i) for i in list(stairs.GetStairsRuns()) + list(stairs.GetStairsLandings())]
        solids = []
        for c in comps:
            solids.extend(kg.element_solids(c))
        self.solids = solids
        for rid in stairs.GetStairsRuns():
            run = doc.GetElement(rid)
            try:
                self.runs.append(RunGeo(run, solids))
            except Exception as ex:
                self.notes.append(u"марш id {} — {}".format(rid.IntegerValue, ex))
        for lid in stairs.GetStairsLandings():
            land = doc.GetElement(lid)
            try:
                plate, err = kp.slab_plate(land, None, True)
            except Exception as ex:
                plate, err = None, u"{}".format(ex)
            if plate is None:
                self.notes.append(u"площадка id {} — {}".format(lid.IntegerValue, err))
            else:
                self.landings.append((land, plate))
        self.run_items = [RunChoice(u"Марш {} — {} ступ., {:.0f}×{:.0f}".format(i + 1, len(r.treads), float(r.h), float(r.g)), i)
                          for i, r in enumerate(self.runs)]


class RunChoice(object):
    def __init__(self, name, index):
        self.Name = name
        self.index = index


def collect(elements):
    items, skipped = [], []
    for el in elements:
        if not isinstance(el, Stairs):
            continue
        st_type = doc.GetElement(el.GetTypeId())
        method = u"{}".format(getattr(st_type, "ConstructionMethod", u""))
        if method != u"CastInPlace":
            skipped.append(u"{} — сборная лестница (заводские изделия), не армируется".format(stair_title(el)))
            continue
        if not kr.is_valid_host(el):
            skipped.append(u"{} — Revit не допускает арматуру (материал не бетон?)".format(stair_title(el)))
            continue
        try:
            item = StairItem(el)
        except Exception as ex:
            skipped.append(u"{} — ошибка геометрии: {}".format(stair_title(el), ex))
            continue
        if not item.runs:
            skipped.append(u"{} — нет прямых монолитных маршей ({})".format(item.title, u"; ".join(item.notes)))
            continue
        items.append(item)
    return items, skipped


# ==========================================================
# РАСЧЁТ
# ==========================================================
class Calc(object):
    def __init__(self, ho, so):
        self.ho, self.so = ho, so
        r = norms.stair_rule
        self.dm = so["dm"] or r("main")["d"]
        self.sm = so["sm"] or r("main")["step"]
        self.dd = so["dd"] or r("dist")["d"]
        self.sd = so["sd"] or r("dist")["step"]
        self.dt = so["dt"] or self.dm
        self.st = so["st"] or self.sm
        self.c_auto = norms.ceil_to(norms.cover_main(so["cond"], max(self.dm, self.dd)), 5.0)
        self.c = so["c"] or self.c_auto
        self.dg = so["dg"] or r("steps")["d"]
        self.sg = so["sg"] or r("steps")["step"]
        self.dn = so["dn"] or r("steps")["nose_d"]
        self.du = so["du"] or r("edge_u")["d"]
        self.u_leg = r("edge_u")["leg"]
        self.dl1 = so["dl1"] or r("landing")["d"]
        self.sl1 = so["sl1"] or r("landing")["step"]
        self.dl2 = so["dl2"] or r("landing")["d"]
        self.sl2 = so["sl2"] or r("landing")["step"]
        self.warnings = []

    def anchorage(self, d):
        return norms.anchorage_length(d, self.ho["steel"], self.ho["concrete"], False)


class Plan(object):
    def __init__(self):
        self.groups = []        # (ключ, роль, Ø, подпись, frame, [Bar])
        self.notes = []
        self.ends = {}          # марш -> как заделаны концы рабочих стержней

    def add(self, key, role, d, title, frame, bars):
        if bars:
            self.groups.append((key, role, d, title, frame, bars))

    def bars(self, key, run=None):
        return [b for k, _, _, _, _, bars in self.groups if k == key for b in bars
                if run is None or getattr(b, "run", None) == run]

    def counts(self):
        out = []
        for key, role, d, title, frame, bars in self.groups:
            for i, (t, n) in enumerate(out):
                if t == title:
                    out[i] = (t, n + len(bars))
                    break
            else:
                out.append((title, len(bars)))
        return out


def tag(bars, run):
    for b in bars:
        b.run = run
    return bars


def plan_run(calc, item, ri, plan):
    R = item.runs[ri]
    so, c = calc.so, calc.c
    world = WorldFrame()
    lan = calc.anchorage(calc.dm)
    W = R.W
    # Нижняя рабочая сетка: продольные — вдоль уклона (на нормали c + dm/2 от низа), поперечные — над ними
    nn_m = c + calc.dm / 2.0
    ls = kp.positions(-W / 2.0, W / 2.0, c + calc.dm / 2.0, calc.sm)
    main = []
    ends = set()
    s_first, s_last = R.treads[0][0], R.treads[-1][1]
    for j, l in enumerate(ls):
        seg = R.chord_along(l, nn_m, item.solids)
        if seg is None:
            continue
        s0, s1 = seg
        # Конец у площадки — заводится в неё по бетону; у перекрытия (бетона лестницы на анкеровку не хватает) —
        # выпуск на длину анкеровки или обрыв с защитным слоем
        if (s1 - s_last) / R.cos >= 0.9 * lan:
            s1 -= c * R.cos
            ends.add(u"верх — в площадку")
        elif so["low"] == LOW_LAP:
            s1 = s_last + lan * R.cos
            ends.add(u"верх — выпуск в перекрытие")
        else:
            s1 -= c * R.cos
            ends.add(u"верх — обрыв")
        if (s_first - s0) / R.cos >= 0.9 * lan:
            s0 += c * R.cos
            ends.add(u"низ — в площадку")
        elif so["low"] == LOW_LAP:
            s0 = s_first - lan * R.cos
            ends.add(u"низ — выпуск в перекрытие")
        else:
            s0 += c * R.cos
            ends.add(u"низ — обрыв")
        a, b = R.at_normal(s0, nn_m), R.at_normal(s1, nn_m)
        main.append(kp.Bar([R.P(a[0], l, a[1]), R.P(b[0], l, b[1])], j, (R.l[0], R.l[1], 0.0)))
    plan.add("main", "straight", calc.dm, u"рабочие марша", world, tag(main, ri))
    plan.ends[ri] = sorted(ends, key=lambda t: t[:2] == u"ни")
    # Поперечные — по длине марша между первой и последней ступенью
    nn_d = c + calc.dm + calc.dd / 2.0
    s_lo, s_hi = R.treads[0][0], R.treads[-1][1]
    dist = []
    for i, s in enumerate(kp.positions(s_lo, s_hi, 50.0 * R.cos, calc.sd * R.cos)):
        p = R.at_normal(s, nn_d)
        dist.append(kp.Bar([R.P(p[0], -(W / 2.0 - c), p[1]), R.P(p[0], W / 2.0 - c, p[1])], i))
    plan.add("dist", "straight", calc.dd, u"поперечные марша", world, tag(dist, ri))
    # Верхняя сетка (по выбору): продольные под внутренними углами ступеней
    if so["top"]:
        nn_t = R.t - c - calc.dt / 2.0
        top = []
        for j, l in enumerate(kp.positions(-W / 2.0, W / 2.0, c + calc.dt / 2.0, calc.st)):
            a, b = R.at_normal(s_lo, nn_t), R.at_normal(s_hi, nn_t)
            top.append(kp.Bar([R.P(a[0], l, a[1]), R.P(b[0], l, b[1])], j, (R.l[0], R.l[1], 0.0)))
        plan.add("top", "straight", calc.dt, u"верхние марша", world, tag(top, ri))
    # П-стержни по боковым граням: в плоскости, перпендикулярной уклону (наборы — вдоль уклона)
    sides = {EDGE_BOTH: (1.0, -1.0), EDGE_LEFT: (1.0,), EDGE_RIGHT: (-1.0,), EDGE_NONE: ()}[so["edge"]]
    for side in sides:
        grp = []
        lw = side * (W / 2.0 - c - calc.du / 2.0)
        lf = lw - side * calc.u_leg
        nb, nt = c + calc.du / 2.0, R.t - c - calc.du / 2.0
        for i, s in enumerate(kp.positions(s_lo, s_hi, (50.0 + calc.sd / 2.0) * R.cos, calc.sd * R.cos)):
            pb, pt = R.at_normal(s, nb), R.at_normal(s, nt)
            grp.append(kp.Bar([R.P(pb[0], lf, pb[1]), R.P(pb[0], lw, pb[1]), R.P(pt[0], lw, pt[1]), R.P(pt[0], lf, pt[1])], i))
        plan.add("edge", "U", calc.du, u"П-стержни по краям", world, tag(grp, ri))
    # Ступени: Г-стержень по контуру (подступёнок + проступь) с шагом поперёк марша и стержень в углу
    ks = list(range(len(R.treads)))
    if not so["first"]:
        ks = ks[1:]
    if not so["last"]:
        ks = ks[:-1]
    lg = kp.positions(-W / 2.0, W / 2.0, c + calc.dg / 2.0, calc.sg)
    for k in ks:
        s0, s1, z = R.treads[k]
        if k == len(R.treads) - 1 and s1 - s0 > 1.5 * R.g:
            continue                       # последняя «проступь» — это уже площадка
        sx = s0 + c + calc.dg / 2.0
        zc = z - c - calc.dg / 2.0
        if zc - (z - R.h + c) < 40 or (s1 - c) - sx < 60:
            continue
        g_bars = [kp.Bar([R.P(sx, l, z - R.h + c), R.P(sx, l, zc), R.P(s1 - c, l, zc)], j) for j, l in enumerate(lg)]
        plan.add("step", "L", calc.dg, u"Г-стержни ступеней", world, tag(g_bars, ri))
        sn = sx + (calc.dg + calc.dn) / 2.0
        zn = zc - (calc.dg + calc.dn) / 2.0
        plan.add("nose", "straight", calc.dn, u"стержни в углах ступеней", world,
                 tag([kp.Bar([R.P(sn, -(W / 2.0 - c), zn), R.P(sn, W / 2.0 - c, zn)], 0, (R.d[0], R.d[1], 0.0))], ri))


def plan_landings(calc, item, plan):
    c = calc.c
    for land, P in item.landings:
        for key, face, d, step, title in (("lb", "lo", calc.dl1, calc.sl1, u"нижняя сетка площадок"),
                                          ("lt", "hi", calc.dl2, calc.sl2, u"верхняя сетка площадок")):
            for along, off in (("x", c + d / 2.0), ("y", c + 1.5 * d)):
                lay = kp.Layer(key + along, along, face, off, d, step)
                lo, hi = (P.y0, P.y1) if along == "x" else (P.x0, P.x1)
                ps = kp.positions(lo, hi, norms.slab_rule("first_bar")["mm"], step)
                bars, _ = kp.layer_bars(P, lay, ps, [], c, lambda side, pos, s: (-c, []), 1.0)
                plan.add(key, "straight", d, title, P, bars)


def plan_stairs(calc, item):
    plan = Plan()
    plan.notes.extend(item.notes)
    for ri in range(len(item.runs)):
        plan_run(calc, item, ri, plan)
    plan_landings(calc, item, plan)
    return plan


def plan_specs(plan):
    """Наборы — внутри каждой группы (ступень, сторона марша, слой сетки)."""
    specs = []
    for key, role, d, title, frame, bars in plan.groups:
        specs.extend(kp.run_specs(frame, role, d, kp.group_runs(bars)))
    return specs


def plan_summary(item, plan):
    parts = u", ".join(u"{} {}".format(t, n) for t, n in plan.counts())
    return u"{}: маршей {}, площадок {}; стержней: {}".format(item.title, len(item.runs), len(item.landings), parts)


def reinforce(items, ho, so, skipped):
    calc = Calc(ho, so)
    plans, skipped = [], list(skipped)
    for item in items:
        try:
            plan = plan_stairs(calc, item)
            specs = plan_specs(plan)
        except Exception as ex:
            skipped.append(u"{} — ошибка расчёта: {}".format(item.title, ex))
            continue
        plans.append({"host": item.stairs, "title": item.title, "specs": specs, "summary": plan_summary(item, plan),
                      "notes": plan.notes})
    runner.run(doc, plans, skipped, calc.warnings, ho["steel"], ho["st_steel"], {}, PART_NAME, WHAT)


# ==========================================================
# ПРЕДПРОСМОТР
# ==========================================================
def draw_run(canvas, calc, item, ri, plan, pad):
    """Разрез по оси марша: профиль (ступени, низ марша, начало площадки), стержни марша. Цели выносок."""
    R = item.runs[ri]
    prof = []
    s = -300.0
    while s <= R.L + 900.0:
        z = R.zrange(s, 0.0, item.solids)
        prof.append((s, z))
        s += 15.0
    pts = [(s, z[1]) for s, z in prof if z is not None] + [(s, z[0]) for s, z in reversed(prof) if z is not None]
    xs = [p[0] for p in pts] or [0.0, R.L]
    zs = [p[1] for p in pts] or [0.0, 1000.0]
    lan = calc.anchorage(calc.dm)
    x0 = min(xs) - (lan * R.cos if calc.so["low"] == LOW_LAP else 0.0) - 150
    sk = kd.Sketch(canvas, (x0, min(zs) - 250, max(xs) + 150, max(zs) + 200), pad=pad)
    T = lambda s, z: (sk.X(s), sk.Y(z))
    if pts:
        sk.poly(pts, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
    to_sz = lambda q: (R.d[0] * (q[0] - R.p0[0]) + R.d[1] * (q[1] - R.p0[1]), q[2])
    targets = {}
    main = plan.bars("main", ri)
    if main:
        b = main[len(main) // 2]
        a, e = to_sz(b.pts[0]), to_sz(b.pts[-1])
        sk.line(a[0], a[1], e[0], e[1], kd.MAIN, 2.4)
        targets["main"] = T(a[0] + (e[0] - a[0]) * 0.3, a[1] + (e[1] - a[1]) * 0.3)
    for b in plan.bars("dist", ri):
        p = to_sz(b.pts[0])
        sk.circle(p[0], p[1], calc.dd / 2.0, kd.SECOND, min_px=2.2)
    top = plan.bars("top", ri)
    if top:
        b = top[len(top) // 2]
        a, e = to_sz(b.pts[0]), to_sz(b.pts[-1])
        sk.line(a[0], a[1], e[0], e[1], kd.MAIN, 1.8)
        targets["top"] = T(a[0] + (e[0] - a[0]) * 0.6, a[1] + (e[1] - a[1]) * 0.6)
    steps = plan.bars("step", ri)
    for b in steps:
        if b.idx == 0:
            sk.poly([to_sz(q) for q in b.pts], kd.CORNER, 1.8)
    if steps:
        mid = [b for b in steps if b.idx == 0]
        m = mid[len(mid) // 2] if mid else steps[0]
        targets["steps"] = T(*to_sz(m.pts[1]))
    for b in plan.bars("nose", ri):
        p = to_sz(b.pts[0])
        sk.circle(p[0], p[1], calc.dn / 2.0, kd.CORNER, min_px=2.6)
    edge = [b for b in plan.bars("edge", ri) if b.idx == 0]
    if edge:
        targets["edge"] = T(*to_sz(edge[0].pts[1]))
    # Размеры: проступь и подступёнок средней ступени, толщина марша
    k = len(R.treads) // 2
    s0, s1, z = R.treads[k]
    sk.dim(s0, z, s1, z, u"{:.0f}".format(float(R.g)), offset_px=10)
    sk.dim(s1, z, s1, z + R.h, u"{:.0f}".format(float(R.h)), offset_px=10, outside="right")
    p = R.at_normal(R.L * 0.35, 0.0)
    q = R.at_normal(R.L * 0.35, R.t)
    sk.dim_line(p[0], p[1], q[0], q[1])
    sk.text(p[0], p[1], u"t = {:.0f}".format(float(R.t)), size=10.5, align="left", dx=8, dy=10)
    targets.setdefault("main", None)
    targets.setdefault("top", None)
    targets.setdefault("steps", None)
    targets.setdefault("edge", None)
    targets["anchor"] = T(x0 + 100, min(zs)) if calc.so["low"] == LOW_LAP else None
    return targets


def draw_landing(canvas, calc, item, plan, pad):
    """План площадок лестницы: контуры и стержни нижней сетки. Цели выносок."""
    if not item.landings:
        kd.Sketch(canvas, (0, 0, 1, 1))
        return {"land": None}
    land, P = item.landings[0]
    sk = kd.Sketch(canvas, (P.x0 - 200, P.y0 - 200, P.x1 + 200, P.y1 + 200), pad=pad)
    sk.rect(P.x0, P.y0, P.x1, P.y1, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
    bars = [b for b in plan.bars("lb") if b.pts[0][2] < P.mid(0.0)]
    target = None
    for b in bars:
        if abs(b.pts[0][2] - b.pts[-1][2]) < 1 and P.x0 - 1 <= b.pts[0][0] <= P.x1 + 1:
            sk.line(b.pts[0][0], b.pts[0][1], b.pts[-1][0], b.pts[-1][1], kd.MAIN, 1.0)
            target = target or (sk.X(b.pts[0][0]), sk.Y(b.pts[0][1]))
    sk.dim(P.x0, P.y1, P.x1, P.y1, u"{:.0f}".format(float(P.x1 - P.x0)))
    sk.dim(P.x0, P.y0, P.x0, P.y1, u"{:.0f}".format(float(P.y1 - P.y0)))
    return {"land": target}


# ==========================================================
# ОКНО
# ==========================================================
XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Армирование лестниц" Width="1480" Height="900" MinWidth="1100" MinHeight="700"
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
                            <TextBlock Grid.Row="0" Text="КЖ • ЛЕСТНИЦЫ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navSection" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Марш"/>
                                <RadioButton Name="navLength" GroupName="nav" Style="{StaticResource NavItem}" Content="Площадки"/>
                                <RadioButton Name="navParams" GroupName="nav" Style="{StaticResource NavItem}" Content="Параметры"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="ЛЕСТНИЦЫ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
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
                            <Grid>
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="10"/>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <ComboBox Name="cbGroup" Style="{StaticResource PillComboBox}" Tag="Лестница:"/>
                                <ComboBox Name="cbItem" Grid.Column="2" Style="{StaticResource PillComboBox}" Tag="Марш:"/>
                                <Button Name="btnSkipped" Grid.Column="3" Style="{StaticResource GlassButton}" Margin="10,0,0,0"/>
                            </Grid>
                        </Border>

                        <!-- РАЗДЕЛ 1: МАРШ -->
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

                        <!-- РАЗДЕЛ 2: ПЛОЩАДКИ -->
                        <Border Name="pageLength" Grid.Row="1" Style="{StaticResource GlassCard}" Padding="10" Visibility="Collapsed">
                            <Grid>
                                <Grid.RowDefinitions>
                                    <RowDefinition Height="*"/>
                                    <RowDefinition Height="Auto"/>
                                </Grid.RowDefinitions>
                                <Border Background="#FFFFFFFF" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="16">
                                    <Grid>
                                        <Canvas Name="cnvElev" ClipToBounds="True"/>
                                        <Canvas Name="cnvElevFields" ClipToBounds="True"/>
                                    </Grid>
                                </Border>
                                <TextBlock Name="tbElevInfo" Grid.Row="1" Style="{StaticResource Hint}" Margin="4,8,0,0"/>
                            </Grid>
                        </Border>

                        <!-- РАЗДЕЛ 3: ПАРАМЕТРЫ -->
                        <ScrollViewer Name="pageParams" Grid.Row="1" Visibility="Collapsed" VerticalScrollBarVisibility="Auto">
                            <WrapPanel Margin="0,0,6,0" ItemWidth="430">
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="Материалы" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <ComboBox Name="cbSteel" Style="{StaticResource PillComboBox}" Tag="Рабочая:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbStSteel" Style="{StaticResource PillComboBox}" Tag="Ступени, П:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbConcrete" Style="{StaticResource PillComboBox}" Tag="Бетон:"/>
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

CONTROL_NAMES = ("tbCount", "tbDoc", "cbGroup", "cbItem", "btnSkipped", "cnvSection", "cnvFields", "tbSectionInfo",
                 "cnvElev", "cnvElevFields", "tbElevInfo", "cbSteel", "cbStSteel", "cbConcrete", "tbInfo", "btnRun")
NAV_PAGES = (("navSection", "pageSection"), ("navLength", "pageLength"), ("navParams", "pageParams"))


def _cond_options():
    return [Option(u"{} мм — {}".format(v, t), k) for k, t, v in norms.cover_conditions()]


YES_NO = lambda yes, no: [Option(yes, True), Option(no, False)]

RUN_FIELDS = [
    ("main", "L", {"title": u"Нижняя (рабочая) сетка марша",
                   "note": u"Продольные стержни — вдоль уклона по низу марша, поперечные — над ними поперёк марша. "
                           u"Защитный слой — от низа марша до продольного стержня.",
                   "rows": [[u"Продольные Ø", ("diam", "dm"), u"шаг", ("num", "sm", 50), u"мм"],
                            [u"Поперечные Ø", ("diam", "dd"), u"шаг", ("num", "sd", 50), u"мм"],
                            [u"Защитный слой", ("num", "c", 46), u"мм", ("combo", "cond", _cond_options(), 170)],
                            [("hint", "c")], [("hint", "sm")]]}),
    ("top", "L", {"title": u"Верхняя сетка марша",
                  "note": u"По желанию: продольные стержни под ступенями (у верхней грани наклонной плиты).",
                  "rows": [[("combo", "top", YES_NO(u"ставить", u"не ставить"), 150)],
                           [u"Продольные Ø", ("diam", "dt"), u"шаг", ("num", "st", 50), u"мм"]]}),
    ("anchor", "L", {"title": u"Концы рабочих стержней",
                     "note": u"У площадки — заводятся в неё по бетону. У перекрытия (бетона лестницы на анкеровку не "
                             u"хватает) — выпуск в перекрытие на длину анкеровки или обрыв с защитным слоем.",
                     "rows": [[u"У перекрытий", ("combo", "low", [Option(u"выпуск на анкеровку", LOW_LAP), Option(u"обрыв с защитным слоем", LOW_COVER)], 220)],
                              [("hint", "low")]]}),
    ("steps", "R", {"title": u"Ступени",
                    "note": u"Г-стержень по контуру каждой ступени (подступёнок + проступь) с шагом поперёк марша "
                            u"и продольный стержень в углу ступени.",
                    "rows": [[u"Г-стержни Ø", ("diam", "dg"), u"шаг", ("num", "sg", 50), u"мм"],
                             [u"В углу ступени Ø", ("diam", "dn")],
                             [u"Первая ступень", ("combo", "first", YES_NO(u"армировать", u"не армировать"), 150)],
                             [u"Последняя ступень", ("combo", "last", YES_NO(u"армировать", u"не армировать"), 150)]]}),
    ("edge", "R", {"title": u"П-стержни по краям марша",
                   "note": u"Охватывают нижнюю и верхнюю грани марша у боковых торцов; шаг — как у поперечных стержней.",
                   "rows": [[("combo", "edge", [Option(u"с обеих сторон", EDGE_BOTH), Option(u"слева", EDGE_LEFT),
                                                Option(u"справа", EDGE_RIGHT), Option(u"не ставить", EDGE_NONE)], 170)],
                            [u"Ø", ("diam", "du")]]}),
]
LAND_FIELDS = [
    ("land", "L", {"title": u"Сетки площадок",
                   "note": u"Нижняя и верхняя сетки в обоих направлениях, как у плиты; защитный слой — как у маршей.",
                   "rows": [[u"Нижняя Ø", ("diam", "dl1"), u"шаг", ("num", "sl1", 50), u"мм"],
                            [u"Верхняя Ø", ("diam", "dl2"), u"шаг", ("num", "sl2", 50), u"мм"], [("hint", "sl1")]]}),
]
AUTO_DIAMS = ("dm", "dd", "dt", "dg", "dn", "du", "dl1", "dl2")


class StairsWindow(ui.KzhWindow):
    def __init__(self, items, skipped, total):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES, NAV_PAGES)
        self.result = None
        self.items = items
        self.skipped = skipped
        self.current = None
        self._loading = False
        self.tbCount.Text = u"{}".format(len(items))
        self.tbDoc.Text = doc.Title
        self.btnSkipped.Content = u"Пропущено: {}".format(len(skipped))
        self.btnSkipped.Visibility = Visibility.Visible if skipped else Visibility.Collapsed
        self.btnSkipped.Click += lambda s, e: show_message(
            u"Эти лестницы армироваться не будут.", details=self.skipped,
            details_header=u"Пропущено: {}".format(len(self.skipped)))
        self.editor = SectionEditor(self.window, self.cnvSection, self.cnvFields, self.refresh)
        self.editor.build(RUN_FIELDS)
        self.leditor = SectionEditor(self.window, self.cnvElev, self.cnvElevFields, self.refresh)
        self.leditor.build(LAND_FIELDS)
        for ed in (self.editor, self.leditor):
            for key in AUTO_DIAMS:
                if key in ed.inputs:
                    cb = ed.inputs[key][1]
                    cb.ItemsSource = [Option(u"авто", None)] + list(cb.ItemsSource)
                    cb.Width = 104.0
        steel = lambda: [Option(s, s) for s in norms.steel_classes()]
        self.cbSteel.ItemsSource = steel()
        self.cbStSteel.ItemsSource = steel()
        self.cbConcrete.ItemsSource = [Option(c, c) for c in norms.concrete_classes()]
        saved = settings.load(SETTINGS_SECTION)
        self.saved = saved
        self.opts = dict(OPTS_DEFAULTS)
        self.opts.update(dict((k, v) for k, v in (saved.get("opts") or {}).items() if k in self.opts))
        g = dict(GENERAL_DEFAULTS)
        g.update(dict((k, v) for k, v in (saved.get("general") or {}).items() if k in g))
        ui.select_option(self.cbSteel, g["steel"])
        ui.select_option(self.cbStSteel, g["st_steel"])
        ui.select_option(self.cbConcrete, g["concrete"])
        self.editor.load(self.opts)
        self.leditor.load(self.opts)
        self.cbGroup.ItemsSource = items
        self.cbGroup.SelectionChanged += self.on_group_changed
        self.cbItem.SelectionChanged += lambda s, e: self.refresh()
        for cb in (self.cbSteel, self.cbStSteel, self.cbConcrete):
            cb.SelectionChanged += lambda s, e: self.refresh()
        for cnv in (self.cnvSection, self.cnvElev):
            cnv.SizeChanged += lambda s, e: self.refresh()
        self.btnRun.Click += self.on_run
        self.cbGroup.SelectedIndex = 0

    def read_general(self):
        return {"steel": ui.selected_value(self.cbSteel), "st_steel": ui.selected_value(self.cbStSteel),
                "concrete": ui.selected_value(self.cbConcrete)}

    def read_opts(self):
        o = self.opts
        self.editor.read(o, [k for k in self.editor.inputs])
        self.leditor.read(o, [k for k in self.leditor.inputs])
        for key in ("sm", "sd", "st", "sg", "sl1", "sl2"):
            if o[key] is not None and o[key] < 50:
                return u"Шаг стержней — не меньше 50 мм."
        if o["c"] is not None and o["c"] < 0:
            return u"Защитный слой не может быть отрицательным."
        return None

    def on_group_changed(self, sender, args):
        item = self.cbGroup.SelectedItem
        if item is None:
            return
        self.current = item
        self._loading = True
        try:
            self.cbItem.ItemsSource = item.run_items
            self.cbItem.SelectedIndex = 0
        finally:
            self._loading = False
        self.refresh()

    def refresh(self):
        item = self.current
        if self._loading or item is None:
            return
        err = self.read_opts()
        if err:
            self.tbInfo.Text = err
            return
        ri = self.cbItem.SelectedItem.index if self.cbItem.SelectedItem is not None else 0
        try:
            calc = Calc(self.read_general(), self.opts)
            plan = plan_stairs(calc, item)
            o = self.opts
            self.editor.set_hint("c", u"" if o["c"] else u"пусто — {:.0f} по норме".format(float(calc.c_auto)))
            self.editor.set_hint("sm", u"по умолчанию: продольные ⌀{} шаг {:.0f}, поперечные ⌀{} шаг {:.0f} (практика)".format(
                calc.dm, float(calc.sm), calc.dd, float(calc.sd)))
            self.editor.set_hint("low", u"анкеровка ⌀{} — {:.0f} мм; марш {}: {}".format(
                calc.dm, float(calc.anchorage(calc.dm)), ri + 1, u", ".join(plan.ends.get(ri, []))))
            self.leditor.set_hint("sl1", u"по умолчанию ⌀{} шаг {:.0f} (практика)".format(calc.dl1, float(calc.sl1)))
            targets = draw_run(self.cnvSection, calc, item, ri, plan, self.editor.pad(top=30, bottom=30))
            self.editor.place(targets)
            ltargets = draw_landing(self.cnvElev, calc, item, plan, self.leditor.pad(top=30, bottom=30))
            self.leditor.place(ltargets)
        except Exception as ex:
            self.tbInfo.Text = u"Проверьте значения в полях ({})".format(ex)
            return
        R = item.runs[ri]
        info = u"Марш {}: ступеней {}, подступёнок {:.0f}, проступь {:.0f}, уклон 1:{:.2f}, толщина {:.0f} мм • стержней: {}".format(
            ri + 1, len(R.treads), float(R.h), float(R.g), float(1.0 / R.k if R.k else 0.0), float(R.t),
            u", ".join(u"{} {}".format(t, n) for t, n in plan.counts()))
        if plan.notes:
            info += u" • Внимание: " + u"; ".join(plan.notes)
        self.tbSectionInfo.Text = info
        self.tbElevInfo.Text = u"Площадок: {}. Нижняя сетка — линии; верхняя — такая же у верхней грани.".format(len(item.landings))
        self.tbInfo.Text = u"Анкеровка рабочих ⌀{} — {:.0f} мм • защитный слой {:.0f} мм".format(
            calc.dm, float(calc.anchorage(calc.dm)), float(calc.c))

    def on_run(self, sender, args):
        err = self.read_opts()
        if err:
            show_message(err)
            return
        ho = self.read_general()
        data = dict(self.saved)
        data["general"] = ho
        data["opts"] = dict(self.opts)
        settings.save(SETTINGS_SECTION, data)
        self.result = (ho, dict(self.opts))
        self.window.Close()


# ==========================================================
# ЗАПУСК
# ==========================================================
class StairsFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, Stairs)

    def AllowReference(self, reference, point):
        return False


def get_stairs():
    """Лестницы из выделения (выбранные марши и площадки — их лестницы), иначе — выбор в модели."""
    out, seen = [], set()
    for i in uidoc.Selection.GetElementIds():
        el = doc.GetElement(i)
        st = el if isinstance(el, Stairs) else None
        if st is None and hasattr(el, "GetStairs"):
            try:
                st = el.GetStairs()
            except Exception:
                st = None
        if st is not None and st.Id.IntegerValue not in seen:
            seen.add(st.Id.IntegerValue)
            out.append(st)
    if out:
        return out
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, StairsFilter(), u"Выберите лестницы и нажмите «Готово»")
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: в семействе армирование лестниц не выполняется.")
        return
    stairs = get_stairs()
    if not stairs:
        return
    items, skipped = collect(stairs)
    if not items:
        show_message(u"Среди выбранных нет монолитных лестниц, которые можно армировать.", details=skipped or None,
                     details_header=u"Пропущено: {}".format(len(skipped)))
        return
    win = StairsWindow(items, skipped, len(stairs))
    win.show_modal()
    if win.result is not None:
        ho, so = win.result
        reinforce(items, ho, so, skipped)


try:
    main()
except Exception:
    show_message(u"Ошибка армирования лестниц:\n{}".format(traceback.format_exc()), title=u"Ошибка")

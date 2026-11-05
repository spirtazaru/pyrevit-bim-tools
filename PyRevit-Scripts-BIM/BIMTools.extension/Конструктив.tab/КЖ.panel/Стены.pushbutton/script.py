# -*- coding: utf-8 -*-
"""Армирование монолитных стен (pyRevit / IronPython 2.7).

Поддерживаются прямые стены постоянной или линейно меняющейся по высоте толщины (подпорные).
Проёмы находятся по геометрии стены: окна, двери, отверстия семейств-пустот, вырезы. Выбранные стены
группируются по типу и толщине: у группы свой тип армирования и свои параметры.

Типы армирования (переключатель в окне):
- «Две сетки» — вертикальные и горизонтальные стержни у каждой грани, П-стержни у свободных торцов
  и у проёмов, шпильки между сетками в шахматном порядке;
- «Одна сетка» — сетка по оси стены (тонкие стены, до 160 мм — практика);
- «Диафрагма с краевыми зонами» — две сетки и краевые зоны у торцов и проёмов: стержни большего
  диаметра в замкнутых хомутах вместо вертикальных стержней сетки;
- «Подпорная / подвальная» — две сетки с разными параметрами граней: наружная (к грунту) с защитным
  слоем «в грунте», П-стержни по верху; толщина может меняться по высоте.

Проёмы: стержни, проходящие через проём, обрезаются с защитным слоем; с каждой стороны проёма —
дополнительные стержни (не менее половины прерванных, заведение на длину анкеровки); у отверстий
до 300 мм стержни не обрезаются, а раздвигаются. Верх вертикальных стержней: выпуск на нахлёст
(стена выше), загиб в плиту (плита над стеной) или обрыв с защитным слоем — определяется по модели.
Значения практики проектирования помечены в norms.json «сверить».
"""
__title__ = u"Стены"
__author__ = u"Daniil Antonenko"
__doc__ = u"Армирование стен: две сетки, одна сетка, диафрагма с краевыми зонами, подпорная; проёмы, П-стержни, шпильки"

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
from kzh import planar as kp
from kzh import norms, settings
from kzh import rebar as kr
from kzh.geometry import mm
from kzh.section_editor import SectionEditor
from kzh.ui import show_message, Option

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

SETTINGS_SECTION = "walls"
PART_NAME = u"Стена"
WHAT = {"many": u"стен", "title": u"Армирование стен", "done": u"Армировано стен",
        "failed": u"Не армирована", "armed": u"уже армирована"}

KINDS = (("double", u"Две сетки"), ("single", u"Одна сетка"), ("pier", u"Диафрагма с краевыми зонами"),
         ("retaining", u"Подпорная / подвальная"))
KIND_TITLES = dict(KINDS)
TOP_AUTO, TOP_LAP, TOP_BEND, TOP_COVER = "auto", "lap", "bend", "cover"
TOP_TITLES = {TOP_LAP: u"выпуск на нахлёст", TOP_BEND: u"загиб в плиту", TOP_COVER: u"обрыв с защитным слоем"}
BOT_JOINT, BOT_COVER = "joint", "cover"
PINS_CHESS, PINS_NONE = "chess", "none"
SLAB_TOP_GAP = 50.0     # от верха плиты до загиба вертикальных стержней: защитный слой и сетка плиты (практика)

GENERAL_DEFAULTS = {"steel": u"А500", "st_steel": u"А240", "concrete": u"В25", "max_len": 11700.0, "stagger": True,
                    "hook": None, "tie_hook": None, "u_ends": True, "u_open": True}
# Параметры группы. Грани: _e — наружная (Exterior стены), _i — внутренняя. dv/sv — вертикальные Ø и шаг,
# dh/sh — горизонтальные; c — защитный слой до ближайшего к грани стержня (None — по норме), cond — условия.
# du — П-стержни (None — как горизонтальные), d_add/n_add — доп. стержни у проёмов (None — как сетка / по норме),
# dp/sp — шпильки, le/ne/de/dse/se — краевая зона: длина, стержней у грани, их Ø, хомут Ø и шаг.
MESH_DEFAULTS = {"kind": None, "dv_e": 12, "sv_e": 200.0, "dh_e": 10, "sh_e": 200.0,
                 "dv_i": 12, "sv_i": 200.0, "dh_i": 10, "sh_i": 200.0,
                 "c_e": None, "c_i": None, "cond_e": "indoor_normal", "cond_i": "indoor_normal", "hor_out": True,
                 "du": None, "d_add": None, "n_add": None, "pins": PINS_CHESS, "dp": 6, "sp": None,
                 "le": None, "ne": 3, "de": 16, "dse": 8, "se": None,
                 "top": TOP_AUTO, "bottom": BOT_JOINT, "top_u": False, "small": None}
# Значения, зависящие от типа армирования (настройки сохраняются отдельно для каждого типа)
KIND_DEFAULTS = {"double": {}, "single": {}, "pier": {}, "retaining": {"cond_e": "ground", "top_u": True}}


# ==========================================================
# СТЕНЫ И ГРУППЫ
# ==========================================================
def wall_title(wall):
    return u"{} (id {})".format(element_name(wall.WallType), wall.Id.IntegerValue)


class WallItem(object):
    """Стена с геометрией: Plate, проёмы, свободные торцы, что над стеной."""

    def __init__(self, wall, plate):
        self.wall = wall
        self.plate = plate
        self.title = wall_title(wall)
        self.Name = self.title
        all_openings = kp.find_openings(plate)
        # Вырезы, касающиеся торцов (подрезка на углах, уступы) — это контур, а не проёмы
        self.openings = [o for o in all_openings if not ({"x0", "x1"} & o.touches)]
        loc = wall.Location
        self.free = (len(list(loc.get_ElementsAtJoin(0))) == 0, len(list(loc.get_ElementsAtJoin(1))) == 0)
        self.lap_ranges, self.slab_top = self._above()
        self.outline = self._outline()

    def _above(self):
        """([(x0, x1)] — участки по длине, над которыми стоит другая стена; отметка верха плиты над стеной или None)."""
        p = self.plate
        margin = 300.0 if p.x1 - p.x0 > 800 else 50.0
        box = (p.x0 + margin, p.x1 - margin, p.y1 + 5, p.y1 + 600, p.n0 + 10, p.n1 - 10)
        cats = [DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_StructuralFoundation]
        found = kp.probe(doc, p, box, cats, self.wall.Id)
        slab_top = None
        floors = [e for e in found if isinstance(e, DB.Floor)]
        if floors:
            slab_top = min(mm(f.get_BoundingBox(None).Max.Z) for f in floors)
        ranges = []
        for w in found:
            curve = getattr(w.Location, 'Curve', None) if isinstance(w, DB.Wall) else None
            if curve is None:
                continue
            half = mm(w.Width) / 2.0
            xs = [p.local(curve.GetEndPoint(k))[0] for k in (0, 1)]
            ranges.append((max(min(xs) - half, p.x0), min(max(xs) + half, p.x1)))
        return ranges, slab_top

    def top_at(self, choice, x):
        """Верх вертикального стержня в точке x по длине стены."""
        if choice != TOP_AUTO:
            return choice
        if any(a <= x <= b for a, b in self.lap_ranges):
            return TOP_LAP
        return TOP_BEND if self.slab_top is not None else TOP_COVER

    def top_text(self, choice, xs):
        modes = []
        for x in xs:
            m = self.top_at(choice, x)
            if m not in modes:
                modes.append(m)
        return u" / ".join(TOP_TITLES[m] for m in modes) or TOP_TITLES[TOP_COVER]

    def _outline(self):
        """Контуры наружной грани в координатах (x, y) — для рисунка фасада."""
        loops = []
        try:
            for ref in DB.HostObjectUtils.GetSideFaces(self.wall, DB.ShellLayerType.Exterior):
                face = self.wall.GetGeometryObjectFromReference(ref)
                for lp in face.GetEdgesAsCurveLoops():
                    pts = []
                    for c in lp:
                        pts.extend(list(c.Tessellate())[:-1])
                    loops.append([self.plate.local(q)[:2] for q in pts])
        except Exception:
            pass
        if not loops:
            p = self.plate
            loops = [[(p.x0, p.y0), (p.x1, p.y0), (p.x1, p.y1), (p.x0, p.y1)]]
        area = lambda pts: abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2.0
        loops.sort(key=lambda pts: -area(pts))
        return loops


class WallGroup(object):
    def __init__(self, type_name, t, tapered):
        self.type_name = type_name
        self.t = t
        self.tapered = tapered
        self.items = []
        self.opts = None
        self.Name = u""

    def default_kind(self):
        name = self.type_name.lower()
        if self.tapered or u"подпорн" in name or u"подвал" in name or u"приямк" in name:
            return "retaining"
        if self.t <= norms.wall_rule("single_mesh_t")["mm"]:
            return "single"
        return "double"


def group_walls(walls):
    groups, skipped = {}, []
    for wall in walls:
        if not isinstance(wall, DB.Wall) or wall.WallType.Kind != DB.WallKind.Basic:
            skipped.append(u"{} — не базовая стена (витраж, составная)".format(
                wall_title(wall) if isinstance(wall, DB.Wall) else wall.Id.IntegerValue))
            continue
        if not kr.is_valid_host(wall):
            skipped.append(u"{} — Revit не допускает арматуру: стена не несущая (включите «Несущая конструкция») "
                           u"или материал не бетон".format(wall_title(wall)))
            continue
        try:
            plate, err = kp.wall_plate(wall)
            item = WallItem(wall, plate) if plate is not None else None
        except Exception as ex:
            item, err = None, u"ошибка геометрии: {}".format(ex)
        if item is None:
            skipped.append(u"{} — {}".format(wall_title(wall), err))
            continue
        t = int(round(plate.thickness(plate.y0)))
        t_top = int(round(plate.thickness(plate.y1)))
        key = (element_name(wall.WallType), t, t_top)
        if key not in groups:
            groups[key] = WallGroup(key[0], max(t, t_top), plate.tapered)
        groups[key].items.append(item)
    ordered = sorted(groups.values(), key=lambda g: (-len(g.items), g.type_name))
    for g in ordered:
        size = u"{} мм".format(g.t) if not g.tapered else u"переменная толщина"
        g.Name = u"{} ({}) — {} шт.".format(g.type_name, size, len(g.items))
    return ordered, skipped


# ==========================================================
# РАСЧЁТ (без изменения модели). Размеры — мм
# ==========================================================
class Calc(object):
    """Величины для группы: ho — общие параметры, so — параметры группы, t — толщина (наибольшая)."""

    def __init__(self, ho, so, t):
        self.ho, self.so, self.t = ho, so, float(t)
        self.kind = so["kind"]
        self.two = self.kind != "single"
        faces = ("e", "i") if self.two else ("e",)
        self.c = {}
        for f in ("e", "i"):
            src = f if f in faces else "e"
            d_out = max(so["dh_" + src], so["dv_" + src])
            auto = norms.ceil_to(norms.cover_main(so["cond_" + src], d_out), 5.0)
            self.c[f] = so["c_" + src] or auto
            setattr(self, "c_auto_" + f, auto)
        self.du = so["du"] or so["dh_e"]
        self.dp = so["dp"]
        self.sp = so["sp"] or norms.wall_rule("pin_spacing")["mm"]
        self.small = so["small"] if so["small"] is not None else norms.wall_rule("small_opening")["mm"]
        self.first = norms.wall_rule("first_bar")["mm"]
        self.le = so["le"] or max(norms.wall_rule("edge_zone")["k_t"] * self.t, norms.wall_rule("edge_zone")["mm"])
        self.se = so["se"] or norms.wall_rule("edge_stirrup")["mm"]
        self.layers = {}
        if self.two:
            for f, face in (("e", "hi"), ("i", "lo")):
                dh, dv, c = so["dh_" + f], so["dv_" + f], self.c[f]
                if so["hor_out"]:
                    off_h, off_v = c + dh / 2.0, c + dh + dv / 2.0
                else:
                    off_v, off_h = c + dv / 2.0, c + dv + dh / 2.0
                self.layers["h_" + f] = kp.Layer("h_" + f, "x", face, off_h, dh, so["sh_" + f])
                self.layers["v_" + f] = kp.Layer("v_" + f, "y", face, off_v, dv, so["sv_" + f])
        else:
            dh, dv = so["dh_e"], so["dv_e"]
            self.layers["h_e"] = kp.Layer("h_e", "x", "mid", -dv / 2.0, dh, so["sh_e"])
            self.layers["v_e"] = kp.Layer("v_e", "y", "mid", dh / 2.0, dv, so["sv_e"])

        w = self.warnings = []
        s_v, s_h = norms.wall_spacing_max(self.t, True), norms.wall_spacing_max(self.t, False)
        for f in faces:
            if so["sv_" + f] > s_v:
                w.append(u"шаг вертикальных {:.0f} мм больше {:.0f} мм (2t и 400 мм)".format(float(so["sv_" + f]), float(s_v)))
            if so["sh_" + f] > s_h:
                w.append(u"шаг горизонтальных {:.0f} мм больше {:.0f} мм".format(float(so["sh_" + f]), float(s_h)))
            if self.c[f] < norms.cover_main(so["cond_" + f], max(so["dh_" + f], so["dv_" + f])):
                w.append(u"защитный слой {:.0f} мм меньше нормы".format(float(self.c[f])))
        mu_min = norms.wall_rule("mu_min")["value"]
        mu_v, mu_h = self.mu()
        if mu_v < mu_min:
            w.append(u"вертикальное армирование μ = {:.2f} % меньше {} %".format(float(mu_v), mu_min))
        if mu_h < mu_min:
            w.append(u"горизонтальное армирование μ = {:.2f} % меньше {} %".format(float(mu_h), mu_min))
        if self.kind == "single" and self.t > norms.wall_rule("single_mesh_t")["mm"]:
            w.append(u"одна сетка в стене толщиной {:.0f} мм (практика — до {} мм)".format(
                float(self.t), norms.wall_rule("single_mesh_t")["mm"]))
        if self.two and self.kind != "pier" and self.dp < norms.wall_rule("pin_d")["mm"] and so["pins"] != PINS_NONE:
            w.append(u"шпильки тоньше {} мм".format(norms.wall_rule("pin_d")["mm"]))

    def mu(self):
        """(μ вертикальной, μ горизонтальной) в % от сечения стены."""
        so = self.so
        q = lambda d, s: math.pi * d * d / 4.0 * 1000.0 / s
        faces = ("e", "i") if self.two else ("e",)
        av = sum(q(so["dv_" + f], so["sv_" + f]) for f in faces)
        ah = sum(q(so["dh_" + f], so["sh_" + f]) for f in faces)
        return 100.0 * av / (self.t * 1000.0), 100.0 * ah / (self.t * 1000.0)

    def anchorage(self, d):
        return norms.anchorage_length(d, self.ho["steel"], self.ho["concrete"], False)

    def lap(self, d):
        return norms.lap_length(d, self.ho["steel"], self.ho["concrete"], False)

    def cover_of(self, layer):
        return self.c["i"] if layer.face == "lo" else self.c["e"]

    def info_text(self):
        so = self.so
        return u"Анкеровка: верт. ⌀{} — {:.0f} мм, гор. ⌀{} — {:.0f} мм • нахлёст верт. — {:.0f} мм • защитный слой по норме {:.0f} / {:.0f} мм".format(
            so["dv_e"], float(self.anchorage(so["dv_e"])), so["dh_e"], float(self.anchorage(so["dh_e"])),
            float(self.lap(so["dv_e"])), float(self.c_auto_e), float(self.c_auto_i))


class WallPlan(object):
    """Стержни стены по категориям в локальных мм (для рисунка) и сводка для подтверждения."""

    def __init__(self):
        self.bars = {}          # категория -> [Bar]
        self.drawn = {}         # ключ слоя -> [(позиция, a, b)]
        self.notes = []
        self.ps = {}            # ключ слоя -> позиции
        self.zones = []         # краевые зоны (x0, x1)
        self.top = TOP_COVER

    def add(self, key, bars):
        self.bars.setdefault(key, []).extend(bars)

    def count(self, key):
        return len(self.bars.get(key, []))


# Категории стержней: (ключ, роль, подпись в сводке)
CATEGORIES = (
    ("v_e", "straight", u"верт. наружн."), ("v_i", "straight", u"верт. внутр."),
    ("h_e", "straight", u"гор. наружн."), ("h_i", "straight", u"гор. внутр."),
    ("zone", "straight", u"краевые зоны"), ("hoop", "stirrup", u"хомуты зон"),
    ("u", "U", u"П-стержни"), ("add", "straight", u"доп. у проёмов"), ("pin", "pin", u"шпильки"),
)


def vertical_rule(calc, item, layer):
    """Концы вертикальных стержней у низа и верха стены (верх — по месту стержня: выпуск, загиб, обрыв)."""
    so = calc.so
    c = calc.cover_of(layer)
    p = item.plate

    def rule(side, pos, s):
        if side == "start":
            return (0.0, []) if so["bottom"] == BOT_JOINT else (-c, [])
        top = item.top_at(so["top"], pos)
        if top == TOP_LAP:
            return calc.lap(layer.d), []
        if top == TOP_BEND and item.slab_top is not None:
            rise = item.slab_top - p.y1 - SLAB_TOP_GAP - layer.d / 2.0
            rule_b = norms.wall_rule("top_bend")
            leg = norms.ceil_to(max(rule_b["k_d"] * layer.d, rule_b["mm"]))
            sign = 1.0 if layer.face == "lo" else -1.0       # загиб внутрь стены (под плитой с обеих сторон)
            return max(rise, 0.0), [(0.0, 0.0, sign * leg)]
        return -c, []
    return rule


def plan_wall(calc, item):
    """WallPlan или (None, причина)."""
    so, ho, p = calc.so, calc.ho, item.plate
    plan = WallPlan()
    if p.x1 - p.x0 < 300 or p.y1 - p.y0 < 300:
        return None, u"стена слишком мала"
    big = [o for o in item.openings if max(o.w, o.h) > calc.small]
    small = [o for o in item.openings if max(o.w, o.h) <= calc.small]
    if small:
        plan.notes.append(u"отверстий до {:.0f} мм: {} — стержни раздвинуты".format(float(calc.small), len(small)))
    u_ends = ho["u_ends"] and calc.two and calc.kind != "pier"
    u_open = ho["u_open"] and calc.two and calc.kind != "pier"
    first = calc.first
    rule_h = lambda side, pos, s: (-calc.c["e"], [])

    # Краевые зоны диафрагмы: у свободных торцов и у сторон проёмов
    if calc.kind == "pier":
        le = calc.le
        if item.free[0]:
            plan.zones.append((p.x0, min(p.x0 + le, p.x1), "x0"))
        if item.free[1]:
            plan.zones.append((max(p.x1 - le, p.x0), p.x1, "x1"))
        for o in big:
            plan.zones.append((max(o.x0 - le, p.x0), o.x0, "x1"))
            plan.zones.append((o.x1, min(o.x1 + le, p.x1), "x0"))
    in_zone = lambda x: any(a - 1.0 <= x <= b + 1.0 for a, b, _ in plan.zones)

    # Сетки
    for f in (("e", "i") if calc.two else ("e",)):
        lv, lh = calc.layers["v_" + f], calc.layers["h_" + f]
        c = calc.cover_of(lv)
        clear_v = c + (calc.du if u_open else 0.0) + lv.d / 2.0
        clear_h = c + (calc.du if u_open else 0.0) + lh.d / 2.0
        ps_v = kp.positions(p.x0, p.x1, first, lv.step)
        ps_v = kp.shifted_positions(ps_v, small, "y", c + lv.d / 2.0)
        ps_v = [x for x in ps_v if not in_zone(x)]
        ps_h = kp.positions(p.y0, p.y1, first, lh.step)
        ps_h = kp.shifted_positions(ps_h, small, "x", c + lh.d / 2.0)
        bars_v, drawn_v = kp.layer_bars(p, lv, ps_v, big, c, vertical_rule(calc, item, lv), clear_v)
        bars_h, drawn_h = kp.layer_bars(p, lh, ps_h, big, c, rule_h, clear_h)
        bars_v = kp.split_long(bars_v, ho["max_len"], calc.lap(lv.d), 1, (lv.d, 0.0, 0.0))
        bars_h = kp.split_long(bars_h, ho["max_len"], calc.lap(lh.d), 0, (0.0, lh.d, 0.0))
        plan.add("v_" + f, bars_v)
        plan.add("h_" + f, bars_h)
        plan.drawn["v_" + f], plan.drawn["h_" + f] = drawn_v, drawn_h
        plan.ps["v_" + f], plan.ps["h_" + f] = ps_v, ps_h
        _opening_bars(calc, item, plan, big, lv, lh, ps_v, ps_h, clear_v, clear_h)
    if calc.two:
        _ubars(calc, item, plan, big, u_ends, u_open)
        if calc.kind != "pier" and so["pins"] != PINS_NONE:
            _pins(calc, item, plan)
    if calc.kind == "pier":
        _zones(calc, item, plan)
    plan.top = item.top_text(so["top"], plan.ps.get("v_e", []))
    if so["top"] == TOP_BEND and item.slab_top is None:
        plan.notes.append(u"плита над стеной не найдена — загиба нет, верх стержней у верха стены")
    return plan, None


def _opening_bars(calc, item, plan, big, lv, lh, ps_v, ps_h, clear_v, clear_h):
    """Дополнительные стержни у проёмов: горизонтальные над и под проёмом, вертикальные по сторонам."""
    so, p = calc.so, item.plate
    c = calc.cover_of(lv)
    d_add = so["d_add"]
    rule = lambda side, pos, s: (-c, [])
    for o in big:
        for lay, ps, clear, along in ((lh, ps_h, clear_h, "x"), (lv, ps_v, clear_v, "y")):
            if along == "y" and calc.kind == "pier":
                continue        # у сторон проёмов диафрагмы — краевые зоны
            d = d_add or lay.d
            a0, a1 = o.across(along)
            n_cut = len([q for q in ps if a0 <= q <= a1])
            if n_cut == 0:
                continue
            k = int(so["n_add"]) if so["n_add"] else norms.opening_bars(n_cut, 'wall')
            step = max(50.0, 2.0 * d + 25.0)
            gap = (d + lay.d) / 2.0 + 25.0
            lan = calc.anchorage(d)
            b0, b1 = o.along(along)
            add_layer = kp.Layer("add", along, lay.face, lay.off, d, lay.step)
            lo, hi = (p.y0, p.y1) if along == "x" else (p.x0, p.x1)
            sides = []
            if not ("y0" in o.touches and along == "x"):
                sides.append(kp.side_positions(a0 - clear, -1, k, step, ps, gap, lo + c, hi - c))
            if not ("y1" in o.touches and along == "x"):
                sides.append(kp.side_positions(a1 + clear, 1, k, step, ps, gap, lo + c, hi - c))
            for side_ps in sides:
                if not side_ps:
                    continue
                bars, _ = kp.layer_bars(p, add_layer, sorted(side_ps), big, c, rule, 1.0, (b0 - lan, b1 + lan))
                for bar in bars:
                    bar.d = d
                plan.add("add", bars)


def _ubars(calc, item, plan, big, u_ends, u_open):
    """П-стержни: у свободных торцов и сторон проёмов — горизонтальные (охватывают торец), над и под
    проёмом — вертикальные; по верху стены — для подпорных стен при обрыве вертикальных стержней."""
    so, p = calc.so, item.plate
    du = calc.du
    lh_e, lh_i = calc.layers["h_e"], calc.layers["h_i"]
    lv_e, lv_i = calc.layers["v_e"], calc.layers["v_i"]
    c = calc.c["e"]
    leg_h = norms.ceil_to(norms.wall_rule("ubar_leg")["k_lan"] * calc.anchorage(lh_e.d))
    leg_v = norms.ceil_to(norms.wall_rule("ubar_leg")["k_lan"] * calc.anchorage(lv_e.d))
    ps_h = plan.ps["h_e"]
    ps_v = plan.ps["v_e"]
    dy = (lh_e.d + du) / 2.0
    dx = (lv_e.d + du) / 2.0

    def jamb(x_face, sign, y_lo, y_hi):
        """П-стержни у вертикальной грани x_face, ножки в сторону sign (±1), ряды горизонтальных стержней."""
        out = []
        x_web = x_face + sign * (c + du / 2.0)
        room = (p.x1 - c - x_web) if sign > 0 else (x_web - p.x0 - c)
        leg = min(leg_h, room)
        if leg < 100:
            return out
        for i, y in enumerate(ps_h):
            yu = y + dy
            if not (y_lo + c < yu < y_hi - c):
                continue
            n_hi, n_lo = lh_e.n(p, yu), lh_i.n(p, yu)
            x_leg = x_web + sign * leg
            out.append(kp.Bar([(x_leg, yu, n_hi), (x_web, yu, n_hi), (x_web, yu, n_lo), (x_leg, yu, n_lo)], i))
        return out

    def cap(y_face, sign, x_lo, x_hi):
        """П-стержни у горизонтальной грани y_face (верх/низ проёма, верх стены), ножки в сторону sign."""
        out = []
        y_web = y_face + sign * (c + du / 2.0)
        room = (p.y1 - c - y_web) if sign > 0 else (y_web - p.y0 - c)
        leg = min(leg_v, room)
        if leg < 100:
            return out
        for i, x in enumerate(ps_v):
            xu = x + dx
            if not (x_lo + c < xu < x_hi - c):
                continue
            y_leg = y_web + sign * leg
            out.append(kp.Bar([(xu, y_leg, lv_e.n(p, y_leg)), (xu, y_web, lv_e.n(p, y_web)),
                               (xu, y_web, lv_i.n(p, y_web)), (xu, y_leg, lv_i.n(p, y_leg))], i))
        return out

    groups = []
    if u_ends:
        if item.free[0]:
            groups.append(jamb(p.x0, 1.0, p.y0, p.y1))
        if item.free[1]:
            groups.append(jamb(p.x1, -1.0, p.y0, p.y1))
    if u_open:
        for o in big:
            groups.append(jamb(o.x0, -1.0, o.y0, o.y1))
            groups.append(jamb(o.x1, 1.0, o.y0, o.y1))
            if "y0" not in o.touches:
                groups.append(cap(o.y0, -1.0, o.x0, o.x1))
            if "y1" not in o.touches:
                groups.append(cap(o.y1, 1.0, o.x0, o.x1))
    if so["top_u"]:
        # По верху — только там, где вертикальные стержни обрываются (нет стены и плиты выше)
        top_u = [b for b in cap(p.y1, -1.0, p.x0, p.x1) if item.top_at(so["top"], b.pts[0][0]) == TOP_COVER]
        groups.append(top_u)
        plan.top_u_bars = top_u
    for g in groups:
        plan.add("u", g)
    plan.u_groups = groups


def _pins(calc, item, plan):
    """Шпильки между сетками в шахматном порядке: у пересечений вертикальных и горизонтальных стержней."""
    so, p = calc.so, item.plate
    lv_e, lh_e = calc.layers["v_e"], calc.layers["h_e"]
    lv_i, lh_i = calc.layers["v_i"], calc.layers["h_i"]
    ps_v, ps_h = plan.ps["v_e"], plan.ps["h_e"]
    if len(ps_v) < 2 or len(ps_h) < 2:
        return
    pitch_v = (ps_v[-1] - ps_v[0]) / (len(ps_v) - 1)
    pitch_h = (ps_h[-1] - ps_h[0]) / (len(ps_h) - 1)
    k_row = max(1, int(round(calc.sp / pitch_h)))
    m_col = max(1, int(round(calc.sp / pitch_v)))
    rows_h = {}
    for pos, a, b in plan.drawn["h_e"]:
        rows_h.setdefault(round(pos, 1), []).append((a, b))
    cols_v = {}
    for pos, a, b in plan.drawn["v_e"]:
        cols_v.setdefault(round(pos, 1), []).append((a, b))
    inside = lambda segs, s: any(a + 20 <= s <= b - 20 for a, b in segs)
    dp = calc.dp
    outer_e = lh_e if so["hor_out"] else lv_e
    outer_i = lh_i if so["hor_out"] else lv_i
    groups = []
    for r, i in enumerate(range(0, len(ps_h), k_row)):
        y = ps_h[i]
        offset = (m_col // 2) if (r % 2 == 1 and m_col >= 2) else 0
        row = []
        for j in range(offset, len(ps_v), m_col):
            x = ps_v[j]
            if not inside(rows_h.get(round(y, 1), []), x) or not inside(cols_v.get(round(x, 1), []), y):
                continue
            xp, yp = x + (lv_e.d + dp) / 2.0, y + (lh_e.d + dp) / 2.0
            n_hi = outer_e.n(p, yp) + outer_e.d / 2.0 + dp / 2.0
            n_lo = outer_i.n(p, yp) - outer_i.d / 2.0 - dp / 2.0
            row.append(kp.Bar([(xp, yp, n_lo), (xp, yp, n_hi)], len(row), (1.0, 0.0, 0.0)))
        groups.append(row)
        plan.add("pin", row)
    plan.pin_groups = groups


def _zones(calc, item, plan):
    """Краевые зоны: вертикальные стержни у каждой грани и замкнутые хомуты по рядам горизонтальных стержней."""
    so, p = calc.so, item.plate
    lh_e = calc.layers["h_e"]
    de, dse, ne = so["de"], so["dse"], max(2, int(so["ne"]))
    groups = []
    for xa, xb, edge in plan.zones:
        for f, face in (("e", "hi"), ("i", "lo")):
            c = calc.c[f]
            off = c + max(dse, so["dh_" + f]) + de / 2.0
            lay = kp.Layer("zone", "y", face, off, de, 0)
            if edge == "x0":
                xs = kp.spread(xa + c + dse + de / 2.0, xb - de / 2.0, ne)
            else:
                xs = kp.spread(xb - c - dse - de / 2.0, xa + de / 2.0, ne)
            bars, _ = kp.layer_bars(p, lay, sorted(xs), [], c, vertical_rule(calc, item, lay), 1.0)
            plan.add("zone", bars)
        # Хомуты: по горизонтальным рядам с шагом не более se
        ps_h = plan.ps["h_e"]
        pitch = (ps_h[-1] - ps_h[0]) / (len(ps_h) - 1) if len(ps_h) > 1 else calc.se
        k = max(1, int(calc.se / pitch + 1e-6))
        c = calc.c["e"]
        if edge == "x0":
            x_e, x_f = xa + c + dse / 2.0, xb + dse / 2.0
        else:
            x_e, x_f = xb - c - dse / 2.0, xa - dse / 2.0
        hoops = []
        for i in range(0, len(ps_h), k):
            y = ps_h[i] + (lh_e.d + dse) / 2.0
            if not (p.y0 + c < y < p.y1 - c):
                continue
            n_hi = p.face_hi(y) - calc.c["e"] - dse / 2.0
            n_lo = p.face_lo(y) + calc.c["i"] + dse / 2.0
            hoops.append(kp.Bar([(x_e, y, n_lo), (x_f, y, n_lo), (x_f, y, n_hi), (x_e, y, n_hi), (x_e, y, n_lo)], len(hoops)))
        groups.append(hoops)
        plan.add("hoop", hoops)
    plan.hoop_groups = groups


def plan_specs(item, plan, calc):
    """BarSpec по категориям; П-стержни, шпильки и хомуты — наборами внутри своих групп."""
    specs = []
    p = item.plate
    so = calc.so
    diam = {"v_e": so["dv_e"], "v_i": so["dv_i"], "h_e": so["dh_e"], "h_i": so["dh_i"], "zone": so["de"],
            "hoop": so["dse"], "u": calc.du, "pin": calc.dp}
    grouped = {"u": getattr(plan, "u_groups", []), "pin": getattr(plan, "pin_groups", []),
               "hoop": getattr(plan, "hoop_groups", [])}
    for key, role, _ in CATEGORIES:
        bars = plan.bars.get(key, [])
        if not bars:
            continue
        if key in grouped:
            for g in grouped[key]:
                if g:
                    specs.extend(kp.bars_to_specs(p, role, diam[key], g))
            continue
        if key == "add":
            by_d = {}
            for b in bars:
                by_d.setdefault(round(_bar_d(calc, b), 1), []).append(b)
            for d, bs in by_d.items():
                specs.extend(kp.bars_to_specs(p, "straight", d, bs))
            continue
        straight = [b for b in bars if len(b.pts) == 2]
        bent = [b for b in bars if len(b.pts) > 2]
        if straight:
            specs.extend(kp.bars_to_specs(p, "straight", diam[key], straight))
        if bent:
            specs.extend(kp.bars_to_specs(p, "L", diam[key], bent))
    return specs


def _bar_d(calc, bar):
    return getattr(bar, "d", None) or calc.so["d_add"] or calc.so["dh_e"]


def plan_summary(item, plan, calc):
    parts = []
    for key, _, title in CATEGORIES:
        n = plan.count(key)
        if n:
            parts.append(u"{} {}".format(title, n))
    return u"{}: {}, {}, верх — {}; стержней: {}".format(
        item.title, KIND_TITLES[calc.kind], u"проёмов {}".format(len(item.openings)) if item.openings else u"без проёмов",
        plan.top, u", ".join(parts))


def reinforce(groups, ho, skipped):
    plans, warnings, skipped = [], [], list(skipped)
    for g in groups:
        calc = Calc(ho, g.opts, g.t)
        warnings.extend(u"{}: {}".format(g.Name, w) for w in calc.warnings)
        for item in g.items:
            try:
                plan, reason = plan_wall(calc, item)
                specs = plan_specs(item, plan, calc) if plan is not None else None
            except Exception as ex:
                plan, reason = None, u"ошибка расчёта: {}".format(ex)
            if plan is None:
                skipped.append(u"{} — {}".format(item.title, reason))
                continue
            plans.append({"host": item.wall, "title": item.title, "specs": specs,
                          "summary": plan_summary(item, plan, calc), "notes": plan.notes})
    hooks = {"stirrup": runner.find_hook(doc, ho["hook"]), "pin": runner.find_hook(doc, ho["tie_hook"])}
    runner.run(doc, plans, skipped, warnings, ho["steel"], ho["st_steel"], hooks, PART_NAME, WHAT)


# ==========================================================
# ПРЕДПРОСМОТР
# ==========================================================
def _bar_xy(sk, bar, color, thick, dash=False):
    sk.poly([(q[0], q[1]) for q in bar.pts], color, thick, dash=dash)


def draw_facade(canvas, calc, item, plan, pad):
    """Вид стены с наружной стороны: контур, проёмы, наружная сетка, П-стержни, доп. стержни, шпильки, зоны.
    Возвращает цели выносок."""
    p = item.plate
    top_ext = max([max(q[1] for q in b.pts) for b in plan.bars.get("v_e", [])] + [p.y1])
    sk = kd.Sketch(canvas, (p.x0 - 200, p.y0 - 350, p.x1 + 200, top_ext + 250), pad=pad)
    P = lambda x, y: (sk.X(x), sk.Y(y))
    loops = item.outline
    sk.poly(loops[0], kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
    for lp in loops[1:]:
        sk.poly(lp, kd.CONCRETE_EDGE, 1.2, "#FFF4F0EC", closed=True)
    for o in item.openings:
        sk.line(o.x0, o.y0, o.x1, o.y1, kd.CONCRETE_EDGE, 0.5)
        sk.line(o.x0, o.y1, o.x1, o.y0, kd.CONCRETE_EDGE, 0.5)
    for a, b, _ in plan.zones:
        sk.rect(a, p.y0, b, p.y1, kd.STIRRUP, 0.8, "#142A6FDB", dash=True)
    for bar in plan.bars.get("v_e", []):
        _bar_xy(sk, bar, kd.MAIN, 1.0)
    for bar in plan.bars.get("h_e", []):
        _bar_xy(sk, bar, kd.SECOND, 1.0)
    for bar in plan.bars.get("zone", []):
        _bar_xy(sk, bar, kd.MAIN, 1.8)
    for bar in plan.bars.get("hoop", []):
        _bar_xy(sk, bar, kd.STIRRUP, 1.2)
    for bar in plan.bars.get("add", []):
        _bar_xy(sk, bar, kd.CORNER, 2.0)
    for bar in plan.bars.get("u", []):
        _bar_xy(sk, bar, kd.STIRRUP, 1.6)
    for bar in plan.bars.get("pin", []):
        sk.circle(bar.pts[0][0], bar.pts[0][1], 1.0, kd.STIRRUP, min_px=2.2)
    sk.dim(p.x0, p.y1, p.x1, p.y1, u"L = {:.0f}".format(float(p.x1 - p.x0)), offset_px=12)
    sk.dim(p.x0, p.y0, p.x0, p.y1, u"H = {:.0f}".format(float(p.y1 - p.y0)))
    for o in item.openings:
        sk.text((o.x0 + o.x1) / 2.0, (o.y0 + o.y1) / 2.0, o.text(), size=10.5)
    # Привязки проёмов — цепочка под стеной
    xs = sorted(set([p.x0, p.x1] + [o.x0 for o in item.openings] + [o.x1 for o in item.openings]))
    yc = p.y0 - 120
    for a, b in zip(xs[:-1], xs[1:]):
        if b - a > 1:
            sk.dim_line(a, yc, b, yc)
            sk.text((a + b) / 2.0, yc, u"{:.0f}".format(float(b - a)), size=10, dy=9)

    def first(key):
        bars = plan.bars.get(key, [])
        return bars[len(bars) // 2] if bars else None

    targets = {}
    v = first("v_e")
    if v is not None:
        targets["top"] = P(v.pts[-1][0], max(q[1] for q in v.pts))
        targets["bottom"] = P(v.pts[0][0], v.pts[0][1])
    a = first("add")
    targets["add"] = P(a.pts[0][0] + (a.pts[-1][0] - a.pts[0][0]) * 0.2, a.pts[0][1]) if a is not None else (
        P(item.openings[0].x0, item.openings[0].y1) if item.openings else None)
    u = getattr(plan, "top_u_bars", [])
    targets["top_u"] = P(u[len(u) // 2].pts[1][0], u[len(u) // 2].pts[1][1]) if u else (sk.X(p.x1), sk.Y(p.y1), False)
    targets["small"] = (sk.X(p.x1), sk.Y((p.y0 + p.y1) / 2.0), False)
    return targets


def draw_plan(canvas, calc, item, plan, pad):
    """Горизонтальный разрез стены у её начала: сетки, П-стержни, шпильки, краевая зона. Возвращает цели выносок."""
    p = item.plate
    so = calc.so
    ps_h = plan.ps.get("h_e") or [(p.y0 + p.y1) / 2.0]
    y_c = ps_h[len(ps_h) // 2]
    win = min(p.x1 - p.x0, max(1500.0, 8 * calc.t))
    x0, x1 = p.x0, p.x0 + win
    n_lo, n_hi = p.face_lo(y_c), p.face_hi(y_c)
    sk = kd.Sketch(canvas, (x0 - 60, n_lo - 40, x1 + 60, n_hi + 40), pad=pad, min_height_px=280)
    P = lambda x, n: (sk.X(x), sk.Y(n))
    # Бетон по сечению на отметке y_c (с разрывом справа)
    segs = [(max(sa[0], x0), min(sb[0], x1)) for sa, sb in p.chord((x0 - 50, y_c, p.mid(y_c)), (x1, y_c, p.mid(y_c)))]
    segs = [(a, b) for a, b in segs if b > a]
    for a, b in segs:
        sk.rect(a, n_lo, b, n_hi, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
    # Проём в разрезе: грани стены и стержни сетки над (под) проёмом — пунктиром, а не обрывом
    for (a0, b0), (a1, b1) in zip(segs[:-1], segs[1:]):
        sk.line(b0, n_lo, a1, n_lo, kd.CONCRETE_EDGE, 1.0, dash=True)
        sk.line(b0, n_hi, a1, n_hi, kd.CONCRETE_EDGE, 1.0, dash=True)
        for key in ("h_e", "h_i"):
            lay = calc.layers.get(key)
            if lay is not None:
                sk.line(b0, lay.n(p, y_c), a1, lay.n(p, y_c), kd.SECOND, 1.0, dash=True)
        sk.text((b0 + a1) / 2.0, (n_lo + n_hi) / 2.0, u"проём", size=10.5)
    sk.break_line(x1, n_lo - 25, x1, n_hi + 25)
    targets = {}

    def near_rows(bars, y, tol):
        return [b for b in bars if abs(b.pts[0][1] - y) < tol]

    dy_u = (calc.layers["h_e"].d + calc.du) / 2.0
    u_x = None
    for bar in near_rows(plan.bars.get("u", []), y_c + dy_u, 1.0):
        if all(x0 - 1 <= q[0] <= x1 + 1 for q in bar.pts):
            sk.poly([(q[0], q[2]) for q in bar.pts], kd.STIRRUP, min(max(1.5, calc.du * sk.k), 3.0))
            # Выноска карточки справа — к самому правому П-стержню (короче и не через весь рисунок)
            if "ubar" not in targets or bar.pts[1][0] > u_x:
                u_x = bar.pts[1][0]
                targets["ubar"] = P(bar.pts[1][0], (bar.pts[1][2] + bar.pts[2][2]) / 2.0)
    for key, color in (("h_e", kd.SECOND), ("h_i", kd.SECOND)):
        for bar in near_rows(plan.bars.get(key, []), y_c, 1.0):
            pts = [(q[0], q[2]) for q in bar.pts if x0 - 1 <= q[0] <= x1 + 1]
            if bar.pts[0][0] < x1 and bar.pts[-1][0] > x0:
                sk.line(max(bar.pts[0][0], x0), bar.pts[0][2], min(bar.pts[-1][0], x1), bar.pts[0][2], color,
                        max(1.5, calc.layers[key].d * sk.k))
    for key in ("v_e", "v_i", "zone"):
        for bar in plan.bars.get(key, []):
            x = bar.pts[0][0]
            ys = [q[1] for q in bar.pts]
            if x0 <= x <= x1 and min(ys) <= y_c <= max(ys):
                d = so["de"] if key == "zone" else calc.layers[key].d
                n = bar.pts[0][2] if key == "zone" else calc.layers[key].n(p, y_c)
                sk.circle(x, n, d / 2.0, kd.CORNER if key == "zone" else kd.MAIN)
                targets.setdefault("mesh_" + key[-1] if key != "zone" else "edge", P(x, n))
    pins = [b for b in plan.bars.get("pin", []) if x0 <= b.pts[0][0] <= x1]
    if pins:
        y_pin = min(pins, key=lambda b: abs(b.pts[0][1] - y_c)).pts[0][1]
        for bar in pins:
            if abs(bar.pts[0][1] - y_pin) < 1:
                xq = bar.pts[0][0]
                sk.line(xq, bar.pts[0][2], xq, bar.pts[1][2], kd.STIRRUP, max(1.2, calc.dp * sk.k))
                targets["pins"] = P(xq, (bar.pts[0][2] + bar.pts[1][2]) / 2.0)      # последняя — самая правая
    hoops = [b for b in plan.bars.get("hoop", []) if x0 - 1 <= b.pts[0][0] <= x1 + 1]
    if hoops:
        hb = min(hoops, key=lambda b: abs(b.pts[0][1] - y_c))
        sk.poly([(q[0], q[2]) for q in hb.pts], kd.STIRRUP, max(1.5, so["dse"] * sk.k))
        targets["hoop"] = P(hb.pts[1][0], (hb.pts[1][2] + hb.pts[2][2]) / 2.0)
    t = n_hi - n_lo
    sk.dim(x0, n_lo, x0, n_hi, u"t = {:.0f}".format(float(t)))
    # Защитные слои — в бетоне, между первыми вертикальными стержнями (не в проёме)
    xd = segs[0][0] + (segs[0][1] - segs[0][0]) * 0.45 if segs else x0 + win * 0.3
    if calc.two:
        sk.cover_dim(xd, n_hi, xd, n_hi - calc.c["e"], u"c = {:.0f}".format(float(calc.c["e"])))
        sk.cover_dim(xd, n_lo, xd, n_lo + calc.c["i"], u"c = {:.0f}".format(float(calc.c["i"])))
    else:
        n_mesh = calc.layers["h_e"].n(p, y_c)
        sk.cover_dim(xd, n_hi, xd, n_mesh, u"{:.0f} до оси сетки".format(float(n_hi - n_mesh)))
    sk.text(x0 + win * 0.5, n_hi + 26, u"наружная грань", size=10.5)
    sk.text(x0 + win * 0.5, n_lo - 26, u"внутренняя грань", size=10.5)
    targets.setdefault("mesh_e", P(x0 + win / 2.0, n_hi))
    targets.setdefault("mesh_i", P(x0 + win / 2.0, n_lo))
    targets["order"] = None
    targets.setdefault("ubar", None)
    targets.setdefault("pins", None)
    targets.setdefault("edge", P(x0 + 100, (n_lo + n_hi) / 2.0))
    return targets


# ==========================================================
# ОКНО НАСТРОЕК
# ==========================================================
XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Армирование стен" Width="1480" Height="900" MinWidth="1100" MinHeight="700"
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
                            <TextBlock Grid.Row="0" Text="КЖ • СТЕНЫ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navSection" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Сечение"/>
                                <RadioButton Name="navLength" GroupName="nav" Style="{StaticResource NavItem}" Content="Фасад"/>
                                <RadioButton Name="navParams" GroupName="nav" Style="{StaticResource NavItem}" Content="Параметры"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="СТЕНЫ / ГРУППЫ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
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
                                        <ColumnDefinition Width="10"/>
                                        <ColumnDefinition Width="*"/>
                                        <ColumnDefinition Width="Auto"/>
                                    </Grid.ColumnDefinitions>
                                    <ComboBox Name="cbGroup" Style="{StaticResource PillComboBox}" Tag="Группа:"/>
                                    <ComboBox Name="cbItem" Grid.Column="2" Style="{StaticResource PillComboBox}" Tag="Стена:"/>
                                    <Button Name="btnSkipped" Grid.Column="3" Style="{StaticResource GlassButton}" Margin="10,0,0,0"/>
                                </Grid>
                                <WrapPanel Margin="0,10,0,0">
                                    <TextBlock Text="Тип армирования:" Style="{StaticResource Inline}" Margin="0,0,10,0"/>
                                    <RadioButton Name="rbDouble" GroupName="kind" Style="{StaticResource ModePill}" Content="Две сетки"/>
                                    <RadioButton Name="rbSingle" GroupName="kind" Style="{StaticResource ModePill}" Content="Одна сетка"/>
                                    <RadioButton Name="rbPier" GroupName="kind" Style="{StaticResource ModePill}" Content="Диафрагма с краевыми зонами"/>
                                    <RadioButton Name="rbRetaining" GroupName="kind" Style="{StaticResource ModePill}" Content="Подпорная / подвальная"/>
                                </WrapPanel>
                            </StackPanel>
                        </Border>

                        <!-- РАЗДЕЛ 1: СЕЧЕНИЕ (горизонтальный разрез) -->
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

                        <!-- РАЗДЕЛ 2: ФАСАД -->
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
                                        <ComboBox Name="cbSteel" Style="{StaticResource PillComboBox}" Tag="Сетки:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbStSteel" Style="{StaticResource PillComboBox}" Tag="Хомуты, шпильки:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbConcrete" Style="{StaticResource PillComboBox}" Tag="Бетон:"/>
                                    </StackPanel>
                                </Border>
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="П-стержни" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <CheckBox Name="chkUEnds" Style="{StaticResource PillCheck}" Content="У свободных торцов стены" Margin="0,0,0,8"/>
                                        <CheckBox Name="chkUOpen" Style="{StaticResource PillCheck}" Content="По контуру проёмов" Margin="0,0,0,4"/>
                                        <TextBlock Style="{StaticResource Hint}" Text="Охватывают обе сетки, шаг — как у стержней сетки, ножки — на длину анкеровки. У торцов, примыкающих к другим стенам, не ставятся (угловые узлы — отдельной кнопкой)."/>
                                    </StackPanel>
                                </Border>
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="Длинные стержни и отгибы" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <Grid Margin="0,0,0,6">
                                            <Grid.ColumnDefinitions>
                                                <ColumnDefinition Width="190"/>
                                                <ColumnDefinition Width="*"/>
                                            </Grid.ColumnDefinitions>
                                            <TextBlock Text="Наибольшая длина, мм" Style="{StaticResource FieldLabel}"/>
                                            <TextBox Name="tbMaxLen" Grid.Column="1" Style="{StaticResource PillTextBox}"/>
                                        </Grid>
                                        <CheckBox Name="chkStagger" Style="{StaticResource PillCheck}" Content="Стыки вразбежку (через стержень)" Margin="0,0,0,8"/>
                                        <ComboBox Name="cbHook" Style="{StaticResource PillComboBox}" Tag="Хомуты зон:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbTieHook" Style="{StaticResource PillComboBox}" Tag="Шпильки:"/>
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

CONTROL_NAMES = (
    "tbCount", "tbDoc", "cbGroup", "cbItem", "btnSkipped", "rbDouble", "rbSingle", "rbPier", "rbRetaining",
    "cnvSection", "cnvFields", "tbSectionInfo", "cnvElev", "cnvElevFields", "tbElevInfo",
    "cbSteel", "cbStSteel", "cbConcrete", "chkUEnds", "chkUOpen", "tbMaxLen", "chkStagger", "cbHook", "cbTieHook",
    "tbInfo", "btnRun",
)
NAV_PAGES = (("navSection", "pageSection"), ("navLength", "pageLength"), ("navParams", "pageParams"))
KIND_BUTTONS = (("rbDouble", "double"), ("rbSingle", "single"), ("rbPier", "pier"), ("rbRetaining", "retaining"))


def _cond_options():
    return [Option(u"{} мм — {}".format(v, t), k) for k, t, v in norms.cover_conditions()]


def _mesh_card(face, title, note):
    return {"title": title, "note": note, "rows": [
        [u"Вертикальные Ø", ("diam", "dv_" + face), u"шаг", ("num", "sv_" + face, 50), u"мм"],
        [u"Горизонтальные Ø", ("diam", "dh_" + face), u"шаг", ("num", "sh_" + face, 50), u"мм"],
        [u"Защитный слой", ("num", "c_" + face, 46), u"мм", ("combo", "cond_" + face, _cond_options(), 170)],
        [("hint", "c_" + face)], [("hint", "mesh_" + face)]]}


MESH_NOTE_E = (u"У наружной грани стены. Вертикальные стержни идут по высоте, горизонтальные — вдоль стены. "
               u"Шаг — расстояние между осями стержней. Защитный слой — от грани бетона до ближайшего к ней стержня.")
MESH_NOTE_SINGLE = (u"Одна сетка по середине толщины стены. Вертикальные стержни идут по высоте, горизонтальные — "
                    u"вдоль стены. Шаг — расстояние между осями стержней.")
# Раздел «Сечение» (горизонтальный разрез стены): слева — сетки, справа — П-стержни, шпильки, краевые зоны
PLAN_FIELDS = [
    ("mesh_e", "L", _mesh_card("e", u"Наружная сетка", MESH_NOTE_E)),
    ("mesh_i", "L", _mesh_card("i", u"Внутренняя сетка", u"У внутренней грани стены. Поля — как у наружной сетки.")),
    ("order", "L", {"title": u"Порядок слоёв", "note": u"Какие стержни сетки ближе к грани стены (снаружи).",
                    "rows": [[("combo", "hor_out", [Option(u"горизонтальные снаружи", True),
                                                    Option(u"вертикальные снаружи", False)], 240)]]}),
    ("ubar", "R", {"title": u"П-стержни у торцов и проёмов",
                   "note": u"Охватывают обе сетки у свободных торцов стены и по краям проёмов. Шаг — как у "
                           u"горизонтальных (вертикальных) стержней, ножки — на длину анкеровки.",
                   "rows": [[u"Ø", ("diam", "du")], [("hint", "du")]]}),
    ("pins", "R", {"title": u"Шпильки между сетками",
                   "note": u"Короткие стержни поперёк стены, связывают наружную и внутреннюю сетки.",
                   "rows": [[("combo", "pins", [Option(u"в шахматном порядке", PINS_CHESS), Option(u"без шпилек", PINS_NONE)], 230)],
                            [u"Ø", ("diam", "dp"), u"шаг", ("num", "sp", 50), u"мм"], [("hint", "sp")]]}),
    ("edge", "R", {"title": u"Краевые зоны",
                   "note": u"У свободных торцов и по сторонам проёмов: вертикальные стержни в замкнутых хомутах "
                           u"вместо стержней сетки (как в колонне).",
                   "rows": [[u"Длина зоны", ("num", "le", 50), u"мм"], [("hint", "le")],
                            [u"Стержней у каждой грани", ("num", "ne", 34), u"шт. Ø", ("diam", "de")],
                            [u"Хомуты Ø", ("diam", "dse"), u"шаг", ("num", "se", 50), u"мм"], [("hint", "se")]]}),
]
# Раздел «Фасад»: слева — низ и проёмы, справа — верх
FACADE_FIELDS = [
    ("add", "L", {"title": u"Усиление проёмов",
                  "note": u"С каждой стороны проёма — дополнительные стержни параллельно прерванным, с заведением за "
                          u"проём на длину анкеровки. У малых отверстий стержни не обрезаются, а раздвигаются.",
                  "rows": [[u"Ø", ("diam", "d_add"), u"по", ("num", "n_add", 34), u"шт. с каждой стороны"], [("hint", "n_add")],
                           [u"Малые отверстия — до", ("num", "small", 46), u"мм"], [("hint", "small")]]}),
    ("bottom", "L", {"title": u"Низ вертикальных стержней",
                     "note": u"«Стык с выпусками» — стержни начинаются от низа стены и стыкуются внахлёст с выпусками "
                             u"из фундамента или плиты; «Защитный слой» — стержни не доходят до низа на защитный слой.",
                     "rows": [[("combo", "bottom", [Option(u"Стык с выпусками", BOT_JOINT), Option(u"Защитный слой", BOT_COVER)], 200)]]}),
    ("top", "R", {"title": u"Верх вертикальных стержней",
                  "note": u"Авто — по модели для каждого стержня: стена выше — выпуск на длину нахлёста, плита над стеной — "
                          u"загиб в плиту, ничего выше — обрыв с защитным слоем.",
                  "rows": [[("combo", "top", [Option(u"Авто (по модели)", TOP_AUTO), Option(u"Выпуск на нахлёст", TOP_LAP),
                                              Option(u"Загиб в плиту", TOP_BEND), Option(u"Защитный слой", TOP_COVER)], 200)],
                           [("hint", "top")]]}),
    ("top_u", "R", {"title": u"П-стержни по верху стены",
                    "note": u"Там, где вертикальные стержни обрываются (подпорные стены): охватывают верх стены.",
                    "rows": [[("combo", "top_u", [Option(u"не ставить", False), Option(u"ставить", True)], 140)]]}),
]
DIAM_DEFAULT_KEYS = ("du", "d_add")       # выбор Ø «авто» — как у сетки


def visible_plan(kind):
    keys = ["mesh_e"]
    if kind != "single":
        keys += ["mesh_i", "order"]
    if kind in ("double", "retaining"):
        keys += ["ubar", "pins"]
    if kind == "pier":
        keys += ["edge"]
    return keys


def visible_facade(kind):
    keys = ["top", "bottom", "add"]
    if kind != "single":
        keys.append("top_u")
    return keys


class WallsWindow(ui.KzhWindow):
    def __init__(self, groups, skipped, total):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES, NAV_PAGES)
        self.result = None
        self.groups = groups
        self.skipped = skipped
        self.current = None
        self._loading = False
        self.tbCount.Text = u"{} / {}".format(total, len(groups))
        self.tbDoc.Text = doc.Title
        self.btnSkipped.Content = u"Не поддерживается: {}".format(len(skipped))
        self.btnSkipped.Visibility = Visibility.Visible if skipped else Visibility.Collapsed
        self.btnSkipped.Click += lambda s, e: show_message(
            u"Эти стены армироваться не будут.", details=self.skipped,
            details_header=u"Пропущено: {}".format(len(self.skipped)))

        self.editor = SectionEditor(self.window, self.cnvSection, self.cnvFields, self.refresh)
        self.editor.build(PLAN_FIELDS)
        self.feditor = SectionEditor(self.window, self.cnvElev, self.cnvElevFields, self.refresh)
        self.feditor.build(FACADE_FIELDS)
        for ed in (self.editor, self.feditor):
            for key in DIAM_DEFAULT_KEYS:
                if key in ed.inputs:
                    cb = ed.inputs[key][1]
                    cb.ItemsSource = [Option(u"авто", None)] + list(cb.ItemsSource)
                    cb.Width = 104.0

        steel = lambda: [Option(s, s) for s in norms.steel_classes()]
        self.cbSteel.ItemsSource = steel()
        self.cbStSteel.ItemsSource = steel()
        self.cbConcrete.ItemsSource = [Option(c, c) for c in norms.concrete_classes()]
        hook_names = [element_name(h) for h in kr.hook_types(doc)]
        self.cbHook.ItemsSource = [Option(u"Без отгибов", u"")] + [Option(n, n) for n in hook_names]
        self.cbTieHook.ItemsSource = [Option(u"Без отгибов", u"")] + [Option(n, n) for n in hook_names]

        saved = settings.load(SETTINGS_SECTION)
        self.saved = saved
        for g in groups:
            g.by_kind = {}
            g.opts = self.opts_for(g.default_kind())
        self.load_general(saved.get("general") or {})
        for name, kind in KIND_BUTTONS:
            getattr(self, name).Checked += lambda s, e, kind=kind: self.on_kind(kind)
        self.cbGroup.ItemsSource = groups
        self.cbGroup.SelectionChanged += self.on_group_changed
        self.cbItem.SelectionChanged += lambda s, e: self.refresh()
        self.cbGroup.SelectedIndex = 0

        for cb in (self.cbSteel, self.cbStSteel, self.cbConcrete):
            cb.SelectionChanged += lambda s, e: self.refresh()
        for chk in (self.chkUEnds, self.chkUOpen, self.chkStagger):
            chk.Checked += lambda s, e: self.refresh()
            chk.Unchecked += lambda s, e: self.refresh()
        self.tbMaxLen.TextChanged += lambda s, e: self.refresh()
        for cnv in (self.cnvSection, self.cnvElev):
            cnv.SizeChanged += lambda s, e: self.refresh()
        self.btnRun.Click += self.on_run

    def load_general(self, saved):
        o = dict(GENERAL_DEFAULTS)
        o.update(dict((k, v) for k, v in saved.items() if k in o))
        default_hook = kr.default_stirrup_hook(doc)
        default_hook = element_name(default_hook) if default_hook is not None else u""
        ui.select_option(self.cbSteel, o["steel"])
        ui.select_option(self.cbStSteel, o["st_steel"])
        ui.select_option(self.cbConcrete, o["concrete"])
        ui.select_option(self.cbHook, default_hook if o["hook"] is None else o["hook"])
        ui.select_option(self.cbTieHook, default_hook if o["tie_hook"] is None else o["tie_hook"])
        self.chkUEnds.IsChecked = bool(o["u_ends"])
        self.chkUOpen.IsChecked = bool(o["u_open"])
        self.chkStagger.IsChecked = bool(o["stagger"])
        self.tbMaxLen.Text = ui.number_text(o["max_len"])

    def read_general(self):
        return {
            "steel": ui.selected_value(self.cbSteel), "st_steel": ui.selected_value(self.cbStSteel),
            "concrete": ui.selected_value(self.cbConcrete),
            "max_len": ui.parse_number(self.tbMaxLen.Text, GENERAL_DEFAULTS["max_len"]),
            "stagger": bool(self.chkStagger.IsChecked), "hook": ui.selected_value(self.cbHook, u""),
            "tie_hook": ui.selected_value(self.cbTieHook, u""),
            "u_ends": bool(self.chkUEnds.IsChecked), "u_open": bool(self.chkUOpen.IsChecked),
        }

    # ----------------------------------------------------------
    def on_kind(self, kind):
        g = self.current
        if self._loading or g is None:
            return
        self.read_opts()
        g.by_kind[g.opts["kind"]] = g.opts
        g.opts = g.by_kind.get(kind) or self.opts_for(kind)
        self.load_opts(g)
        self.refresh()

    def opts_for(self, kind):
        """Параметры группы для типа армирования: по умолчанию, затем сохранённые для этого типа."""
        o = dict(MESH_DEFAULTS)
        o.update(KIND_DEFAULTS[kind])
        o.update(dict((k, v) for k, v in (self.saved.get("mesh_" + kind) or {}).items() if k in o))
        o["kind"] = kind
        return o

    def load_opts(self, g):
        self._loading = True
        try:
            kind = g.opts["kind"]
            for name, k in KIND_BUTTONS:
                getattr(self, name).IsChecked = (k == kind)
            self.editor.show_only(visible_plan(kind))
            self.feditor.show_only(visible_facade(kind))
            self.editor.load(g.opts)
            self.feditor.load(g.opts)
        finally:
            self._loading = False

    def read_opts(self):
        """Поля -> opts текущей группы. Возвращает текст ошибки или None."""
        g = self.current
        o = g.opts
        plan_keys = [k for k in self.editor.inputs]
        facade_keys = [k for k in self.feditor.inputs]
        self.editor.read(o, plan_keys)
        self.feditor.read(o, facade_keys)
        for key in ("dv_e", "dh_e", "dv_i", "dh_i", "dp", "de", "dse"):
            if o[key] is None:
                o[key] = MESH_DEFAULTS[key]
        for key in ("sv_e", "sh_e", "sv_i", "sh_i"):
            if not o[key] or o[key] < 50:
                return u"Шаг стержней сетки — не меньше 50 мм."
        for key in ("c_e", "c_i", "sp", "le", "se", "small"):
            if o[key] is not None and o[key] < 0:
                return u"Значения не могут быть отрицательными."
        for key in ("ne", "n_add"):
            if o[key] is not None and (o[key] < 1 or o[key] != int(o[key])):
                return u"Число стержней — целое число от 1."
        if o["ne"] is None:
            o["ne"] = MESH_DEFAULTS["ne"]
        return None

    def on_group_changed(self, sender, args):
        g = self.cbGroup.SelectedItem
        if g is None:
            return
        if self.current is not None:
            self.read_opts()
        self.current = g
        self._loading = True
        try:
            self.cbItem.ItemsSource = g.items
            self.cbItem.SelectedIndex = 0
        finally:
            self._loading = False
        self.load_opts(g)
        self.refresh()

    def refresh(self):
        g = self.current
        if self._loading or g is None:
            return
        err = self.read_opts()
        if err:
            self.tbInfo.Text = err
            return
        item = self.cbItem.SelectedItem or g.items[0]
        try:
            ho = self.read_general()
            calc = Calc(ho, g.opts, g.t)
            plan, reason = plan_wall(calc, item)
            if plan is None:
                self.tbInfo.Text = reason
                return
            so = g.opts
            for f in ("e", "i"):
                self.editor.set_hint("c_" + f, u"" if so["c_" + f] else u"пусто — {:.0f} по норме".format(
                    float(getattr(calc, "c_auto_" + f))))
            s_v = norms.wall_spacing_max(calc.t, True)
            self.editor.set_hint("mesh_e", u"шаг по СП 63: верт. ≤ {:.0f}, гор. ≤ {:.0f} мм".format(
                float(s_v), float(norms.wall_spacing_max(calc.t, False))))
            self.editor.set_hint("mesh_i", u"")
            single = calc.kind == "single"
            self.editor.set_title("mesh_e", u"Сетка по оси стены" if single else u"Наружная сетка")
            self.editor.set_note("mesh_e", MESH_NOTE_SINGLE if single else MESH_NOTE_E)
            self.editor.set_hint("du", u"шаг — как у сетки, ножки {:.0f} мм".format(
                float(norms.ceil_to(calc.anchorage(calc.layers["h_e"].d)))))
            self.editor.set_hint("sp", u"" if so["sp"] else u"пусто — {:.0f} (практика)".format(float(calc.sp)))
            self.editor.set_hint("le", u"" if so["le"] else u"пусто — {:.0f} (2t, ≥ 400)".format(float(calc.le)))
            self.editor.set_hint("se", u"" if so["se"] else u"пусто — {:.0f}".format(float(calc.se)))
            self.feditor.set_hint("top", u"по модели: {}".format(item.top_text(TOP_AUTO, plan.ps.get("v_e", []))) if so["top"] == TOP_AUTO else u"")
            self.feditor.set_hint("n_add", u"" if so["n_add"] else u"пусто — по норме (≥ половины прерванных, ≥ 2)")
            self.feditor.set_hint("small", u"" if so["small"] is not None else u"пусто — {:.0f} (практика)".format(float(calc.small)))
            targets = draw_plan(self.cnvSection, calc, item, plan, self.editor.pad(top=40, bottom=60))
            self.editor.place(targets)
            ftargets = draw_facade(self.cnvElev, calc, item, plan, self.feditor.pad(top=40, bottom=70))
            self.feditor.place(ftargets)
        except Exception as ex:
            self.tbInfo.Text = u"Проверьте значения в полях ({})".format(ex)
            return
        mu_v, mu_h = calc.mu()
        counts = u", ".join(u"{} {}".format(title, plan.count(key)) for key, _, title in CATEGORIES if plan.count(key))
        info = u"{} • t = {:.0f} мм • μ верт. = {:.2f} %, гор. = {:.2f} % • стержней: {}".format(
            KIND_TITLES[calc.kind], float(calc.t), float(mu_v), float(mu_h), counts)
        notes = plan.notes + calc.warnings
        if notes:
            info += u" • Внимание: " + u"; ".join(notes)
        self.tbSectionInfo.Text = info
        self.tbElevInfo.Text = (u"Вид с наружной стороны: наружная сетка, П-стержни, дополнительные стержни у проёмов (зелёные), "
                                u"шпильки (точки). Проёмов: {}. Свободные торцы: {}. Верх — {}.".format(
                                    len(item.openings), u", ".join(n for n, f in zip((u"начало", u"конец"), item.free) if f) or u"нет",
                                    plan.top))
        self.tbInfo.Text = calc.info_text()

    def on_run(self, sender, args):
        if self.current is not None:
            err = self.read_opts()
            if err:
                show_message(err)
                return
        ho = self.read_general()
        if not ho["max_len"] or ho["max_len"] < 3000:
            show_message(u"Наибольшая длина стержня — не меньше 3000 мм.")
            return
        data = dict(self.saved)
        data["general"] = ho
        if self.current is not None:
            data["mesh_" + self.current.opts["kind"]] = dict((k, v) for k, v in self.current.opts.items() if k != "kind")
        settings.save(SETTINGS_SECTION, data)
        self.result = ho
        self.window.Close()


# ==========================================================
# ЗАПУСК
# ==========================================================
class WallFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.Wall)

    def AllowReference(self, reference, point):
        return False


def get_walls():
    """Стены из текущего выделения, иначе — выбор в модели."""
    flt = WallFilter()
    walls = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    walls = [w for w in walls if w is not None and flt.AllowElement(w)]
    if walls:
        return walls
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, u"Выберите стены и нажмите «Готово»")
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: в семействе армирование стен не выполняется.")
        return
    walls = get_walls()
    if not walls:
        return
    groups, skipped = group_walls(walls)
    if not groups:
        show_message(u"Среди выбранных нет стен, которые можно армировать.", details=skipped,
                     details_header=u"Пропущено: {}".format(len(skipped)))
        return
    win = WallsWindow(groups, skipped, len(walls))
    win.show_modal()
    if win.result is not None:
        reinforce(groups, win.result, skipped)


try:
    main()
except Exception:
    show_message(u"Ошибка армирования стен:\n{}".format(traceback.format_exc()), title=u"Ошибка")

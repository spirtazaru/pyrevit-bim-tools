# -*- coding: utf-8 -*-
"""Армирование монолитных плит (pyRevit / IronPython 2.7).

Поддерживаются плиты постоянной толщины любого контура в плане. Отверстия (шахты, вырезы, семейства-
пустоты) находятся по геометрии плиты. Выбранные плиты группируются по типу и толщине.

Типы армирования (переключатель в окне):
- «Сплошная» — перекрытие: нижняя и верхняя сетки (по два слоя X/Y), П-стержни по контуру,
  поддерживающие каркасы (лягушки) под верхнюю сетку;
- «Фундаментная» — то же с защитным слоем «в грунте» и большими диаметрами по умолчанию;
- «По профлисту» — верхняя сетка над гофрами и по стержню в каждой гофре вдоль пролёта
  (шаг гофр — по профилю из типа плиты, его можно изменить);
- «Ребристая» — полка с одной сеткой по середине; рёбра — балки под плитой (армируются кнопкой «Балки»).
Сборные плиты (ПК, ПБ) — заводские изделия, не армируются.

Отверстия: стержни через отверстие обрезаются, с каждой стороны — дополнительные стержни (не менее
половины прерванных, заведение на длину анкеровки), по контуру — П-стержни; у отверстий до 300 мм
стержни не обрезаются, а раздвигаются. Лягушки: один элемент, их число — в параметре «АрмЧисло».
Значения практики проектирования помечены в norms.json «сверить».
"""
__title__ = u"Плиты"
__author__ = u"Daniil Antonenko"
__doc__ = u"Армирование плит: сплошная, фундаментная, по профлисту, ребристая; отверстия, П-стержни, лягушки"

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

SETTINGS_SECTION = "slabs"
PART_NAME = u"Плита"
WHAT = {"many": u"плит", "title": u"Армирование плит", "done": u"Армировано плит",
        "failed": u"Не армирована", "armed": u"уже армирована"}

KINDS = (("solid", u"Сплошная"), ("foundation", u"Фундаментная"), ("deck", u"По профлисту"), ("ribbed", u"Ребристая"))
KIND_TITLES = dict(KINDS)
FROG_W, FROG_FOOT = 200.0, 100.0        # лягушка: ширина верхней полки и длина опорных лапок, мм (практика)

GENERAL_DEFAULTS = {"steel": u"А500", "st_steel": u"А240", "concrete": u"В25", "max_len": 11700.0, "stagger": True,
                    "u_edges": True, "u_open": True}
# Параметры группы: d/s — Ø и шаг слоёв: b — нижняя сетка, t — верхняя (для ребристой — одна сетка: b);
# x/y — направление стержней. c_b/c_t — защитный слой до ближайшего к грани стержня (None — по норме).
# du — П-стержни (None — как сетка), d_add/n_add — доп. стержни у отверстий, df/sf — лягушки,
# dr/pr/fr — стержни в гофрах профлиста: Ø, шаг гофр, первая гофра от края (None — по профилю)
MESH_DEFAULTS = {"kind": None, "dbx": 12, "sbx": 200.0, "dby": 12, "sby": 200.0,
                 "dtx": 12, "stx": 200.0, "dty": 12, "sty": 200.0,
                 "c_b": None, "c_t": None, "cond_b": "indoor_normal", "cond_t": "indoor_normal", "x_out": True,
                 "du": None, "d_add": None, "n_add": None, "small": None, "frogs": True, "df": None, "sf": None,
                 "dr": 12, "pr": None, "fr": None}
# Значения по умолчанию при переключении типа
KIND_DEFAULTS = {
    "solid": {"dbx": 12, "dby": 12, "dtx": 12, "dty": 12, "sbx": 200.0, "sby": 200.0, "stx": 200.0, "sty": 200.0,
              "cond_b": "indoor_normal", "cond_t": "indoor_normal", "frogs": True},
    "foundation": {"dbx": 16, "dby": 16, "dtx": 16, "dty": 16, "sbx": 200.0, "sby": 200.0, "stx": 200.0, "sty": 200.0,
                   "cond_b": "ground", "cond_t": "ground", "frogs": True},
    "deck": {"dtx": 8, "dty": 8, "stx": 200.0, "sty": 200.0, "cond_b": "indoor_normal", "cond_t": "indoor_normal",
             "frogs": False},
    "ribbed": {"dbx": 8, "dby": 8, "sbx": 200.0, "sby": 200.0, "cond_b": "indoor_normal", "cond_t": "indoor_normal",
               "frogs": False},
}


# ==========================================================
# ПЛИТЫ И ГРУППЫ
# ==========================================================
def slab_title(el):
    t = doc.GetElement(el.GetTypeId())
    return u"{} (id {})".format(element_name(t) if t is not None else u"?", el.Id.IntegerValue)


def deck_info(floor):
    """Профлист типа плиты: {"name", "h", "pitch"} или None. Шаг гофр — сумма длин полок профиля."""
    try:
        cs = floor.FloorType.GetCompoundStructure()
    except Exception:
        return None
    if cs is None:
        return None
    for k in range(cs.LayerCount):
        if cs.GetLayerFunction(k) != DB.MaterialFunctionAssignment.StructuralDeck:
            continue
        prof = doc.GetElement(cs.GetDeckProfileId(k))
        info = {"name": element_name(prof) if prof is not None else u"профиль", "h": None, "pitch": None}
        if prof is not None:
            vals = {}
            for prm in prof.Parameters:
                try:
                    if prm.StorageType == DB.StorageType.Double:
                        vals[prm.Definition.Name] = mm(prm.AsDouble())
                except Exception:
                    pass
            info["h"] = vals.get(u"Высота")
            flanges = [v for k2, v in vals.items() if u"полки" in k2.lower()]
            if len(flanges) >= 2:
                info["pitch"] = sum(flanges[:2])
        return info
    return None


class SlabItem(object):
    """Плита с геометрией: Plate, отверстия, контур, профлист, рёбра (балки под плитой)."""

    def __init__(self, floor, plate, deck):
        self.floor = floor
        self.plate = plate
        self.deck = deck
        self.title = slab_title(floor)
        self.Name = self.title
        # Отверстия — области внутри контура; вырезы у края — это контур плиты
        self.openings = [o for o in kp.find_openings(plate, 25.0) if not o.touches]
        self.outline = self._outline()
        self.edges = self._edges()
        p = plate
        below = kp.probe(doc, p, (p.x0, p.x1, p.y0, p.y1, p.n0 - 60, p.n0 - 5),
                         [DB.BuiltInCategory.OST_StructuralFraming, DB.BuiltInCategory.OST_StructuralColumns], floor.Id)
        self.ribs = [e for e in below if e.Category.Id.IntegerValue == int(DB.BuiltInCategory.OST_StructuralFraming)]
        self.columns = [e for e in below if e.Category.Id.IntegerValue == int(DB.BuiltInCategory.OST_StructuralColumns)]

    def _outline(self):
        loops = []
        try:
            for ref in DB.HostObjectUtils.GetTopFaces(self.floor):
                face = self.floor.GetGeometryObjectFromReference(ref)
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

    def _edges(self):
        """Прямые кромки наружного контура: [((x0, y0), (x1, y1))] длиннее 300 мм."""
        pts = self.outline[0]
        out = []
        for a, b in zip(pts, pts[1:] + pts[:1]):
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 300:
                out.append((a, b))
        return out


class SlabGroup(object):
    def __init__(self, type_name, t, foundation, deck):
        self.type_name = type_name
        self.t = t
        self.foundation = foundation
        self.deck = deck
        self.items = []
        self.opts = None
        self.Name = u""

    def default_kind(self):
        if self.foundation:
            return "foundation"
        if self.deck:
            return "deck"
        if any(it.ribs for it in self.items):
            return "ribbed"
        return "solid"


def group_slabs(elements):
    groups, skipped = {}, []
    for el in elements:
        if not isinstance(el, DB.Floor):
            skipped.append(u"{} — сборная плита (заводское изделие): армирование по серии, не армируется".format(slab_title(el)))
            continue
        if not kr.is_valid_host(el):
            skipped.append(u"{} — Revit не допускает арматуру: плита не несущая (включите «Несущие конструкции») "
                           u"или материал не бетон".format(slab_title(el)))
            continue
        try:
            deck = deck_info(el)
            angle = None
            if deck is not None:
                try:
                    angle = el.SpanDirectionAngle      # гофры — вдоль направления пролёта
                except Exception:
                    angle = None
            plate, err = kp.slab_plate(el, angle)
            item = SlabItem(el, plate, deck) if plate is not None else None
        except Exception as ex:
            item, err = None, u"ошибка геометрии: {}".format(ex)
        if item is None:
            skipped.append(u"{} — {}".format(slab_title(el), err))
            continue
        t = int(round(plate.thickness(0.0)))
        ftype = doc.GetElement(el.GetTypeId())
        foundation = el.Category.Id.IntegerValue == int(DB.BuiltInCategory.OST_StructuralFoundation) or \
            getattr(ftype, 'IsFoundationSlab', False)
        key = (element_name(ftype), t, foundation)
        if key not in groups:
            groups[key] = SlabGroup(key[0], t, foundation, deck)
        groups[key].items.append(item)
    ordered = sorted(groups.values(), key=lambda g: (-len(g.items), g.type_name))
    for g in ordered:
        g.Name = u"{} ({} мм) — {} шт.".format(g.type_name, g.t, len(g.items))
    return ordered, skipped


# ==========================================================
# РАСЧЁТ (без изменения модели). Размеры — мм
# ==========================================================
class Calc(object):
    """Величины для группы: ho — общие параметры, so — параметры группы, t — толщина, deck — профлист."""

    def __init__(self, ho, so, t, deck):
        self.ho, self.so, self.t, self.deck = ho, so, float(t), deck
        self.kind = so["kind"]
        kind = self.kind
        auto = lambda cond, d: norms.ceil_to(norms.cover_main(cond, d), 5.0)
        self.c_auto_b = auto(so["cond_b"], max(so["dbx"], so["dby"]))
        self.c_auto_t = auto(so["cond_t"], max(so["dtx"], so["dty"]))
        self.c_b = so["c_b"] or self.c_auto_b
        self.c_t = so["c_t"] or self.c_auto_t
        self.du = so["du"] or max(so["dbx"], so["dby"])
        self.small = so["small"] if so["small"] is not None else norms.slab_rule("small_opening")["mm"]
        self.first = norms.slab_rule("first_bar")["mm"]
        fd = norms.slab_rule("frog_d")
        self.df = so["df"] or (fd["mm_thick"] if self.t > fd["h_mm"] else fd["mm"])
        self.sf = so["sf"] or norms.slab_rule("frog_step")["mm"]
        self.deck_h = (deck or {}).get("h") or 0.0
        self.pr = so["pr"] or (deck or {}).get("pitch") or 200.0
        self.fr = so["fr"] if so["fr"] is not None else self.pr / 2.0
        self.layers = {}
        L = kp.Layer
        if kind in ("solid", "foundation"):
            self._two_layers("b", "lo", self.c_b, so["dbx"], so["sbx"], so["dby"], so["sby"])
            self._two_layers("t", "hi", self.c_t, so["dtx"], so["stx"], so["dty"], so["sty"])
        elif kind == "deck":
            self._two_layers("t", "hi", self.c_t, so["dtx"], so["stx"], so["dty"], so["sty"])
            rib_c = norms.slab_rule("deck_rib_cover")["mm"]
            self.layers["rib"] = L("rib", "x", "lo", rib_c + so["dr"] / 2.0, so["dr"], self.pr)
        else:
            dx, dy = so["dbx"], so["dby"]
            self.layers["bx"] = L("bx", "x", "mid", -dy / 2.0, dx, so["sbx"])
            self.layers["by"] = L("by", "y", "mid", dx / 2.0, dy, so["sby"])

        w = self.warnings = []
        s_max = norms.slab_spacing_max(self.t if kind != "ribbed" else self.t)
        for key, lay in self.layers.items():
            if key != "rib" and lay.step > s_max:
                w.append(u"шаг {} {:.0f} мм больше {:.0f} мм".format(LAYER_TITLES[key], float(lay.step), float(s_max)))
        if self.c_b < norms.cover_main(so["cond_b"], max(so["dbx"], so["dby"])) and kind != "deck":
            w.append(u"нижний защитный слой меньше нормы")
        if self.c_t < norms.cover_main(so["cond_t"], max(so["dtx"], so["dty"])) and kind != "ribbed":
            w.append(u"верхний защитный слой меньше нормы")
        mu_min = norms.slab_rule("mu_min")["value"]
        for key in ("bx", "by"):
            if key in self.layers and self.mu(key) < mu_min:
                w.append(u"{}: μ = {:.2f} % меньше {} %".format(LAYER_TITLES[key], float(self.mu(key)), mu_min))
        if kind == "deck" and self.deck_h and self.t - self.deck_h < 2 * self.c_t + 30:
            w.append(u"над гофрами мало места для сетки")
        if kind == "ribbed" and self.t > 160:
            w.append(u"полка толщиной {:.0f} мм — для толстой плиты подойдёт тип «Сплошная»".format(float(self.t)))

    def _two_layers(self, face_key, face, c, dx, sx, dy, sy):
        """Два слоя сетки у грани: снаружи — X или Y (so["x_out"])."""
        L = kp.Layer
        if self.so["x_out"]:
            off_x, off_y = c + dx / 2.0, c + dx + dy / 2.0
        else:
            off_y, off_x = c + dy / 2.0, c + dy + dx / 2.0
        self.layers[face_key + "x"] = L(face_key + "x", "x", face, off_x, dx, sx)
        self.layers[face_key + "y"] = L(face_key + "y", "y", face, off_y, dy, sy)

    def mu(self, key):
        lay = self.layers[key]
        return 100.0 * math.pi * lay.d * lay.d / 4.0 * 1000.0 / lay.step / (self.t * 1000.0)

    def anchorage(self, d):
        return norms.anchorage_length(d, self.ho["steel"], self.ho["concrete"], False)

    def lap(self, d):
        return norms.lap_length(d, self.ho["steel"], self.ho["concrete"], False)

    def cover_of(self, layer):
        return self.c_t if layer.face == "hi" else self.c_b

    def info_text(self):
        lay = self.layers.get("bx") or self.layers.get("tx")
        return u"Анкеровка ⌀{} — {:.0f} мм, нахлёст — {:.0f} мм • защитный слой по норме: низ {:.0f}, верх {:.0f} мм".format(
            lay.d, float(self.anchorage(lay.d)), float(self.lap(lay.d)), float(self.c_auto_b), float(self.c_auto_t))


LAYER_TITLES = {"bx": u"низ X", "by": u"низ Y", "tx": u"верх X", "ty": u"верх Y", "rib": u"в гофрах"}

# Категории стержней: (ключ, роль, подпись в сводке)
CATEGORIES = (
    ("bx", "straight", u"низ X"), ("by", "straight", u"низ Y"), ("tx", "straight", u"верх X"), ("ty", "straight", u"верх Y"),
    ("rib", "straight", u"в гофрах"), ("add", "straight", u"доп. у отверстий"), ("u", "U", u"П-стержни"),
    ("frog", "frog", u"лягушки"),
)


class SlabPlan(object):
    def __init__(self):
        self.bars = {}
        self.drawn = {}
        self.ps = {}
        self.notes = []
        self.u_groups = []
        self.frogs = 0

    def add(self, key, bars):
        self.bars.setdefault(key, []).extend(bars)

    def count(self, key):
        if key == "frog":
            return self.frogs
        return len(self.bars.get(key, []))


def plan_slab(calc, item):
    """SlabPlan или (None, причина)."""
    so, ho, p = calc.so, calc.ho, item.plate
    plan = SlabPlan()
    if p.x1 - p.x0 < 300 or p.y1 - p.y0 < 300:
        return None, u"плита слишком мала"
    big = [o for o in item.openings if max(o.w, o.h) > calc.small]
    small = [o for o in item.openings if max(o.w, o.h) <= calc.small]
    if small:
        plan.notes.append(u"отверстий до {:.0f} мм: {} — стержни раздвинуты".format(float(calc.small), len(small)))
    two = calc.kind in ("solid", "foundation")
    u_open = ho["u_open"] and two
    for key, lay in sorted(calc.layers.items()):
        c = calc.cover_of(lay)
        clear = c + (calc.du if u_open else 0.0) + lay.d / 2.0
        lo, hi = (p.y0, p.y1) if lay.along == "x" else (p.x0, p.x1)
        if key == "rib":
            ps, k = [], 0
            while lo + calc.fr + k * calc.pr < hi - c:
                ps.append(lo + calc.fr + k * calc.pr)
                k += 1
        else:
            ps = kp.positions(lo, hi, calc.first, lay.step)
            ps = kp.shifted_positions(ps, small, lay.along, c + lay.d / 2.0)
        rule = lambda side, pos, s, c=c: (-c, [])
        bars, drawn = kp.layer_bars(p, lay, ps, big, c, rule, clear)
        shift = (0.0, lay.d, 0.0) if lay.along == "x" else (lay.d, 0.0, 0.0)
        bars = kp.split_long(bars, ho["max_len"], calc.lap(lay.d), 0 if lay.along == "x" else 1, shift)
        plan.add(key, bars)
        plan.drawn[key] = drawn
        plan.ps[key] = ps
        if key != "rib":
            _opening_bars(calc, item, plan, big, lay, ps, clear)
    if two:
        _ubars(calc, item, plan, big, ho["u_edges"], u_open)
        if so["frogs"]:
            _frogs(calc, item, plan, big)
    if calc.kind == "ribbed":
        plan.notes.append(u"рёбра под плитой: {} — армируйте кнопкой «Балки»".format(len(item.ribs)) if item.ribs
                          else u"балок-рёбер под плитой не найдено")
    if calc.kind == "deck" and not item.deck:
        plan.notes.append(u"в типе плиты нет профлиста — шаг гофр задайте вручную")
    return plan, None


def _opening_bars(calc, item, plan, big, lay, ps, clear):
    """Дополнительные стержни у отверстий: параллельно слою с обеих сторон отверстия, заведение на lan."""
    so, p = calc.so, item.plate
    c = calc.cover_of(lay)
    rule = lambda side, pos, s: (-c, [])
    for o in big:
        a0, a1 = o.across(lay.along)
        n_cut = len([q for q in ps if a0 <= q <= a1])
        if n_cut == 0:
            continue
        d = so["d_add"] or lay.d
        k = int(so["n_add"]) if so["n_add"] else norms.opening_bars(n_cut, 'slab')
        step = max(50.0, 2.0 * d + 25.0)
        gap = (d + lay.d) / 2.0 + 25.0
        lan = calc.anchorage(d)
        b0, b1 = o.along(lay.along)
        lo, hi = (p.y0, p.y1) if lay.along == "x" else (p.x0, p.x1)
        add_layer = kp.Layer("add", lay.along, lay.face, lay.off, d, lay.step)
        for side_ps in (kp.side_positions(a0 - clear, -1, k, step, ps, gap, lo + c, hi - c),
                        kp.side_positions(a1 + clear, 1, k, step, ps, gap, lo + c, hi - c)):
            if not side_ps:
                continue
            bars, _ = kp.layer_bars(p, add_layer, sorted(side_ps), big, c, rule, 1.0, (b0 - lan, b1 + lan))
            for bar in bars:
                bar.d = d
            plan.add("add", bars)


def _ubars(calc, item, plan, big, u_edges, u_open):
    """П-стержни по кромкам плиты и по контуру отверстий: в вертикальной плоскости поперёк кромки,
    ножки — в уровне слоёв, параллельных ножкам (между их стержнями)."""
    p = item.plate
    du = calc.du
    groups = []

    def edge(a, b, inward):
        """a, b — концы кромки (x, y); inward — единичный вектор внутрь плиты (x, y)."""
        ex, ey = b[0] - a[0], b[1] - a[1]
        length = math.hypot(ex, ey)
        ex, ey = ex / length, ey / length
        legs_x = abs(inward[0]) >= abs(inward[1])
        lb, lt = (calc.layers["bx"], calc.layers["tx"]) if legs_x else (calc.layers["by"], calc.layers["ty"])
        c = max(calc.c_b, calc.c_t)
        leg = norms.ceil_to(norms.slab_rule("ubar_leg")["k_lan"] * calc.anchorage(max(lb.d, lt.d)))
        # Позиции вдоль кромки: рядом со стержнями слоя, если кромка вдоль оси, иначе — равномерно
        out = []
        if abs(ex) < 1e-3 or abs(ey) < 1e-3:
            along_x = abs(ex) > abs(ey)
            ps = plan.ps.get(lb.key, [])
            s0, s1 = sorted((a[0], b[0]) if along_x else (a[1], b[1]))
            pts2 = []
            for q in ps:
                v = q + (lb.d + du) / 2.0
                if s0 + c < v < s1 - c:
                    pts2.append((v, a[1]) if along_x else (a[0], v))
        else:
            pts2 = [(a[0] + ex * s, a[1] + ey * s) for s in kp.positions(0.0, length, calc.first, lb.step)]
        for i, (x, y) in enumerate(pts2):
            wx, wy = x + inward[0] * (c + du / 2.0), y + inward[1] * (c + du / 2.0)
            # Длина ножки не больше, чем есть бетона до противоположной кромки или отверстия
            n_mid = p.mid(0.0)
            segs = p.chord((wx, wy, n_mid), (wx + inward[0] * (leg + 1000), wy + inward[1] * (leg + 1000), n_mid))
            room = segs[0][1] if segs else None
            avail = math.hypot(room[0] - wx, room[1] - wy) - c if room else 0.0
            L = min(leg, avail)
            if L < 100:
                continue
            fx, fy = wx + inward[0] * L, wy + inward[1] * L
            nb, nt = lb.n(p, 0.0), lt.n(p, 0.0)
            out.append(kp.Bar([(fx, fy, nb), (wx, wy, nb), (wx, wy, nt), (fx, fy, nt)], len(out)))
        return out

    if u_edges:
        pts = item.outline[0]
        for a, b in item.edges:
            # Нормаль внутрь — с той стороны кромки, где есть плита
            ex, ey = b[0] - a[0], b[1] - a[1]
            length = math.hypot(ex, ey)
            nx, ny = -ey / length, ex / length
            mx, my = (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
            if p.depth_at(mx + nx * 60, my + ny * 60) is None:
                nx, ny = -nx, -ny
            groups.append(edge(a, b, (nx, ny)))
    if u_open:
        for o in big:
            groups.append(edge((o.x0, o.y0), (o.x1, o.y0), (0.0, -1.0)))
            groups.append(edge((o.x0, o.y1), (o.x1, o.y1), (0.0, 1.0)))
            groups.append(edge((o.x0, o.y0), (o.x0, o.y1), (-1.0, 0.0)))
            groups.append(edge((o.x1, o.y0), (o.x1, o.y1), (1.0, 0.0)))
    for g in groups:
        plan.add("u", g)
    plan.u_groups = groups


def _frogs(calc, item, plan, big):
    """Лягушки под верхнюю сетку по сетке с шагом sf. Ставится одна, число — в АрмЧисло."""
    p = item.plate
    df = calc.df
    lb_in = calc.layers["by"] if calc.so["x_out"] else calc.layers["bx"]
    lt_in = calc.layers["ty"] if calc.so["x_out"] else calc.layers["tx"]
    nb = lb_in.n(p, 0.0) + lb_in.d / 2.0 + df / 2.0
    nt = lt_in.n(p, 0.0) - lt_in.d / 2.0 - df / 2.0
    if nt - nb < 40:
        plan.notes.append(u"между сетками меньше 40 мм — лягушки не ставятся (нужны фиксаторы)")
        return
    sf = calc.sf
    xs = kp.positions(p.x0, p.x1, sf / 2.0, sf)
    ys = kp.positions(p.y0, p.y1, sf / 2.0, sf)
    ps_bx = plan.ps.get("bx", [])
    valid = []
    for x in xs:
        for y in ys:
            if any(o.x0 - 150 < x < o.x1 + 150 and o.y0 - 150 < y < o.y1 + 150 for o in big):
                continue
            ok = all(p.depth_at(x + dx, y) is not None for dx in (-FROG_W / 2.0 - FROG_FOOT, FROG_W / 2.0 + FROG_FOOT))
            if ok:
                valid.append((x, y))
    if not valid:
        return
    x, y = valid[0]
    if ps_bx:
        # Рядом со стержнем нижнего слоя X, не в одной плоскости с ним
        y = min(ps_bx, key=lambda q: abs(q - y)) + (calc.layers["bx"].d + df) / 2.0
    w, a = FROG_W / 2.0, FROG_FOOT
    bar = kp.Bar([(x - w - a, y, nb), (x - w, y, nb), (x - w, y, nt), (x + w, y, nt), (x + w, y, nb), (x + w + a, y, nb)], 0)
    bar.arm_count = len(valid)
    plan.add("frog", [bar])
    plan.frogs = len(valid)
    plan.frog_grid = valid


def plan_specs(item, plan, calc):
    specs = []
    p = item.plate
    so = calc.so
    for key, role, _ in CATEGORIES:
        bars = plan.bars.get(key, [])
        if not bars:
            continue
        if key == "u":
            for g in plan.u_groups:
                if g:
                    specs.extend(kp.bars_to_specs(p, "U", calc.du, g))
        elif key == "frog":
            for bar in bars:
                sp = kp.bars_to_specs(p, "frog", calc.df, [bar])[0]
                sp.arm_count = bar.arm_count
                specs.append(sp)
        elif key == "add":
            by_d = {}
            for b in bars:
                by_d.setdefault(b.d, []).append(b)
            for d, bs in by_d.items():
                specs.extend(kp.bars_to_specs(p, "straight", d, bs))
        else:
            specs.extend(kp.bars_to_specs(p, role, calc.layers[key].d, bars))
    return specs


def plan_summary(item, plan, calc):
    parts = [u"{} {}".format(title, plan.count(key)) for key, _, title in CATEGORIES if plan.count(key)]
    return u"{}: {}, t = {:.0f}, {}; стержней: {}".format(
        item.title, KIND_TITLES[calc.kind], float(calc.t),
        u"отверстий {}".format(len(item.openings)) if item.openings else u"без отверстий", u", ".join(parts))


def reinforce(groups, ho, skipped):
    plans, warnings, skipped = [], [], list(skipped)
    for g in groups:
        calc = Calc(ho, g.opts, g.t, g.deck)
        warnings.extend(u"{}: {}".format(g.Name, w) for w in calc.warnings)
        for item in g.items:
            try:
                plan, reason = plan_slab(calc, item)
                specs = plan_specs(item, plan, calc) if plan is not None else None
            except Exception as ex:
                plan, reason = None, u"ошибка расчёта: {}".format(ex)
            if plan is None:
                skipped.append(u"{} — {}".format(item.title, reason))
                continue
            plans.append({"host": item.floor, "title": item.title, "specs": specs,
                          "summary": plan_summary(item, plan, calc), "notes": plan.notes})
    runner.run(doc, plans, skipped, warnings, ho["steel"], ho["st_steel"], {}, PART_NAME, WHAT)


# ==========================================================
# ПРЕДПРОСМОТР
# ==========================================================
def draw_plan(canvas, calc, item, plan, pad):
    """План плиты: контур, отверстия, нижняя сетка (или верхняя у профлиста), доп. стержни, П-стержни,
    лягушки, стержни в гофрах, рёбра. Возвращает цели выносок."""
    p = item.plate
    sk = kd.Sketch(canvas, (p.x0 - 300, p.y0 - 450, p.x1 + 300, p.y1 + 300), pad=pad)
    P = lambda x, y: (sk.X(x), sk.Y(y))
    loops = item.outline
    sk.poly(loops[0], kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
    for lp in loops[1:]:
        sk.poly(lp, kd.CONCRETE_EDGE, 1.2, "#FFF4F0EC", closed=True)
    for o in item.openings:
        sk.line(o.x0, o.y0, o.x1, o.y1, kd.CONCRETE_EDGE, 0.5)
        sk.line(o.x0, o.y1, o.x1, o.y0, kd.CONCRETE_EDGE, 0.5)
    for rib in item.ribs:
        bb = rib.get_BoundingBox(None)
        if bb is not None:
            corners = [p.local(DB.XYZ(x, y, bb.Min.Z))[:2] for x in (bb.Min.X, bb.Max.X) for y in (bb.Min.Y, bb.Max.Y)]
            xs, ys = [q[0] for q in corners], [q[1] for q in corners]
            sk.rect(min(xs), min(ys), max(xs), max(ys), kd.CONCRETE_EDGE, 0.8, dash=True)
    keys = ("bx", "by") if "bx" in calc.layers else ("tx", "ty")
    for key, color in zip(keys, (kd.MAIN, kd.SECOND)):
        for bar in plan.bars.get(key, []):
            sk.poly([(q[0], q[1]) for q in bar.pts], color, 0.8)
    for bar in plan.bars.get("rib", []):
        sk.poly([(q[0], q[1]) for q in bar.pts], kd.STIRRUP, 1.2, dash=True)
    for bar in plan.bars.get("add", []):
        sk.poly([(q[0], q[1]) for q in bar.pts], kd.CORNER, 2.0)
    for bar in plan.bars.get("u", []):
        sk.line(bar.pts[0][0], bar.pts[0][1], bar.pts[1][0], bar.pts[1][1], kd.STIRRUP, 1.6)
    for x, y in getattr(plan, "frog_grid", [])[:2000]:
        sk.rect(x - 60, y - 60, x + 60, y + 60, kd.WARN, 1.0)
    sk.dim(p.x0, p.y1, p.x1, p.y1, u"{:.0f}".format(float(p.x1 - p.x0)), offset_px=12)
    sk.dim(p.x0, p.y0, p.x0, p.y1, u"{:.0f}".format(float(p.y1 - p.y0)))
    for o in item.openings:
        sk.text((o.x0 + o.x1) / 2.0, (o.y0 + o.y1) / 2.0, o.text(), size=10.5)
    # Оси X/Y армирования
    ox, oy = p.x0 - 200, p.y0 - 300
    sk.line(ox, oy, ox + 700, oy, kd.DIM, 1.2)
    sk.line(ox, oy, ox, oy + 700, kd.DIM, 1.2)
    sk.text(ox + 760, oy, u"X", bold=True)
    sk.text(ox, oy + 760, u"Y", bold=True)

    def mid(key):
        bars = plan.bars.get(key, [])
        return bars[len(bars) // 2] if bars else None

    targets = {}
    a = mid("add")
    targets["add"] = P(a.pts[0][0] + (a.pts[-1][0] - a.pts[0][0]) * 0.15, a.pts[0][1]) if a is not None else (
        P(item.openings[0].x0, item.openings[0].y0) if item.openings else None)
    targets["small"] = (sk.X(p.x1), sk.Y(p.y0), False)
    grid = getattr(plan, "frog_grid", [])
    targets["frog"] = P(grid[0][0], grid[0][1]) if grid else None
    return targets


def draw_section(canvas, calc, item, plan, pad):
    """Разрез плиты поперёк X у края y0 (средняя линия по x): слои сеток, П-стержень, лягушка, стержни в гофрах.
    Возвращает цели выносок."""
    p = item.plate
    so = calc.so
    x_c = (p.x0 + p.x1) / 2.0
    xs_bars = [b.pts[0][0] for b in plan.bars.get("by", []) + plan.bars.get("ty", [])]
    if xs_bars:
        x_c = min(xs_bars, key=lambda x: abs(x - x_c)) + 1.0
    n_lo, n_hi = p.face_lo(0.0), p.face_hi(0.0)
    win = min(p.y1 - p.y0, max(1400.0, 6 * calc.t))
    y0, y1 = p.y0, p.y0 + win
    sk = kd.Sketch(canvas, (y0 - 80, n_lo - 60, y1 + 60, n_hi + 60), pad=pad, min_height_px=300)
    P = lambda y, n: (sk.X(y), sk.Y(n))
    for sa, sb in p.chord((x_c, y0 - 50, p.mid(0.0)), (x_c, y1, p.mid(0.0))):
        sk.rect(max(sa[1], y0), n_lo, min(sb[1], y1), n_hi, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
    sk.break_line(y1, n_lo - 30, y1, n_hi + 30)
    if calc.kind == "deck" and calc.deck_h:
        # Профлист схематично: между гофрами (бетон в гофрах) — гребни листа без бетона
        top = n_lo + calc.deck_h
        ribs = [y for y in plan.ps.get("rib", []) if y0 - calc.pr <= y <= y1 + calc.pr]
        bw, tw = calc.pr * 0.22, calc.pr * 0.32
        for a, b in zip(ribs[:-1], ribs[1:]):
            pts = [(a + bw, n_lo), (b - bw, n_lo), (b - tw, top), (a + tw, top)]
            pts = [(min(max(y, y0), y1), n) for y, n in pts]
            sk.poly(pts, kd.CONCRETE_EDGE, 0.8, "#FFF4F0EC", closed=True)
        if ribs and ribs[0] - tw > y0:
            sk.poly([(y0, n_lo), (ribs[0] - bw, n_lo), (ribs[0] - tw, top), (y0, top)], kd.CONCRETE_EDGE, 0.8,
                    "#FFF4F0EC", closed=True)
    targets = {}
    # Слои вдоль Y — линии, вдоль X — точки
    for key in ("by", "ty"):
        lay = calc.layers.get(key)
        if lay is None:
            continue
        for bar in plan.bars.get(key, []):
            if abs(bar.pts[0][0] - x_c) < lay.step:
                a, b = max(bar.pts[0][1], y0), min(bar.pts[-1][1], y1)
                if b > a:
                    sk.line(a, lay.n(p, 0.0), b, lay.n(p, 0.0), kd.SECOND, max(1.5, lay.d * sk.k))
                    break
    for key in ("bx", "tx", "rib"):
        lay = calc.layers.get(key)
        if lay is None:
            continue
        for bar in plan.bars.get(key, []):
            y = bar.pts[0][1]
            if y0 <= y <= y1 and bar.pts[0][0] <= x_c <= bar.pts[-1][0]:
                sk.circle(y, lay.n(p, 0.0), lay.d / 2.0, kd.STIRRUP if key == "rib" else kd.MAIN)
                targets.setdefault(key, P(y, lay.n(p, 0.0)))
    if "by" in calc.layers and calc.kind == "ribbed":
        lay = calc.layers["by"]
        sk.line(y0 + 40, lay.n(p, 0.0), y1, lay.n(p, 0.0), kd.SECOND, max(1.5, lay.d * sk.k))
    for bar in plan.bars.get("u", []):
        if abs(bar.pts[1][1] - p.y0) < calc.c_b + calc.du + 5 and abs(bar.pts[0][0] - x_c) < 400:
            sk.poly([(q[1], q[2]) for q in bar.pts], kd.STIRRUP, max(1.5, calc.du * sk.k))
            targets["ubar"] = P(bar.pts[1][1], (bar.pts[1][2] + bar.pts[2][2]) / 2.0)
            break
    frog = plan.bars.get("frog", [])
    if frog:
        f = frog[0]
        base = y0 + win * 0.55
        sk.poly([(base + (q[0] - f.pts[0][0]), q[2]) for q in f.pts], kd.WARN, 1.5, dash=True)
        targets["frog"] = P(base + FROG_FOOT + FROG_W / 2.0, f.pts[2][2])
    sk.dim(y0, n_lo, y0, n_hi, u"t = {:.0f}".format(float(n_hi - n_lo)))
    yc = y0 + win * 0.35
    if calc.kind in ("solid", "foundation"):
        sk.cover_dim(yc, n_lo, yc, n_lo + calc.c_b, u"c = {:.0f}".format(float(calc.c_b)))
    if calc.kind != "ribbed":
        sk.cover_dim(yc, n_hi, yc, n_hi - calc.c_t, u"c = {:.0f}".format(float(calc.c_t)))
    else:
        n_mesh = calc.layers["bx"].n(p, 0.0)
        sk.cover_dim(yc, n_hi, yc, n_mesh, u"{:.0f} до оси сетки".format(float(n_hi - n_mesh)))
    targets.setdefault("bot", targets.get("bx"))
    targets.setdefault("top", targets.get("tx"))
    targets["bot"] = targets.get("bx")
    targets["top"] = targets.get("tx")
    targets["mesh"] = targets.get("bx")
    targets["deck"] = targets.get("rib")
    targets["order"] = None
    targets.setdefault("ubar", None)
    targets.setdefault("frog", None)
    return targets


# ==========================================================
# ОКНО НАСТРОЕК
# ==========================================================
XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Армирование плит" Width="1480" Height="900" MinWidth="1100" MinHeight="700"
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
                            <TextBlock Grid.Row="0" Text="КЖ • ПЛИТЫ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navSection" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Сечение"/>
                                <RadioButton Name="navLength" GroupName="nav" Style="{StaticResource NavItem}" Content="План"/>
                                <RadioButton Name="navParams" GroupName="nav" Style="{StaticResource NavItem}" Content="Параметры"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="ПЛИТЫ / ГРУППЫ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
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
                                    <ComboBox Name="cbItem" Grid.Column="2" Style="{StaticResource PillComboBox}" Tag="Плита:"/>
                                    <Button Name="btnSkipped" Grid.Column="3" Style="{StaticResource GlassButton}" Margin="10,0,0,0"/>
                                </Grid>
                                <WrapPanel Margin="0,10,0,0">
                                    <TextBlock Text="Тип плиты:" Style="{StaticResource Inline}" Margin="0,0,10,0"/>
                                    <RadioButton Name="rbSolid" GroupName="kind" Style="{StaticResource ModePill}" Content="Сплошная"/>
                                    <RadioButton Name="rbFoundation" GroupName="kind" Style="{StaticResource ModePill}" Content="Фундаментная"/>
                                    <RadioButton Name="rbDeck" GroupName="kind" Style="{StaticResource ModePill}" Content="По профлисту"/>
                                    <RadioButton Name="rbRibbed" GroupName="kind" Style="{StaticResource ModePill}" Content="Ребристая"/>
                                </WrapPanel>
                            </StackPanel>
                        </Border>

                        <!-- РАЗДЕЛ 1: СЕЧЕНИЕ -->
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

                        <!-- РАЗДЕЛ 2: ПЛАН -->
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
                                        <ComboBox Name="cbStSteel" Style="{StaticResource PillComboBox}" Tag="Лягушки:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbConcrete" Style="{StaticResource PillComboBox}" Tag="Бетон:"/>
                                    </StackPanel>
                                </Border>
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="П-стержни" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <CheckBox Name="chkUEdges" Style="{StaticResource PillCheck}" Content="По контуру плиты" Margin="0,0,0,8"/>
                                        <CheckBox Name="chkUOpen" Style="{StaticResource PillCheck}" Content="По контуру отверстий" Margin="0,0,0,4"/>
                                        <TextBlock Style="{StaticResource Hint}" Text="Охватывают нижнюю и верхнюю сетки, ножки — в уровне слоя, параллельного ножкам, на длину анкеровки. Для плит с двумя сетками (сплошная, фундаментная)."/>
                                    </StackPanel>
                                </Border>
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="Длинные стержни" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <Grid Margin="0,0,0,6">
                                            <Grid.ColumnDefinitions>
                                                <ColumnDefinition Width="190"/>
                                                <ColumnDefinition Width="*"/>
                                            </Grid.ColumnDefinitions>
                                            <TextBlock Text="Наибольшая длина, мм" Style="{StaticResource FieldLabel}"/>
                                            <TextBox Name="tbMaxLen" Grid.Column="1" Style="{StaticResource PillTextBox}"/>
                                        </Grid>
                                        <CheckBox Name="chkStagger" Style="{StaticResource PillCheck}" Content="Стыки вразбежку (через стержень)"/>
                                        <TextBlock Style="{StaticResource Hint}" Text="Длинные стержни режутся на части с нахлёстом по СП 63; соседние части сдвинуты на диаметр, чтобы не совпадать."/>
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
    "tbCount", "tbDoc", "cbGroup", "cbItem", "btnSkipped", "rbSolid", "rbFoundation", "rbDeck", "rbRibbed",
    "cnvSection", "cnvFields", "tbSectionInfo", "cnvElev", "cnvElevFields", "tbElevInfo",
    "cbSteel", "cbStSteel", "cbConcrete", "chkUEdges", "chkUOpen", "tbMaxLen", "chkStagger", "tbInfo", "btnRun",
)
NAV_PAGES = (("navSection", "pageSection"), ("navLength", "pageLength"), ("navParams", "pageParams"))
KIND_BUTTONS = (("rbSolid", "solid"), ("rbFoundation", "foundation"), ("rbDeck", "deck"), ("rbRibbed", "ribbed"))


def _cond_options():
    return [Option(u"{} мм — {}".format(v, t), k) for k, t, v in norms.cover_conditions()]


def _mesh_card(title, note, kx, sx, ky, sy, cover=None, hint=None):
    rows = [[u"Вдоль X:  Ø", ("diam", kx), u"шаг", ("num", sx, 50), u"мм"],
            [u"Вдоль Y:  Ø", ("diam", ky), u"шаг", ("num", sy, 50), u"мм"]]
    if cover:
        rows.append([u"Защитный слой", ("num", "c_" + cover, 46), u"мм", ("combo", "cond_" + cover, _cond_options(), 170)])
        rows.append([("hint", "c_" + cover)])
    rows.append([("hint", hint)])
    return {"title": title, "note": note, "rows": rows}


# Раздел «Сечение»: слева — сетки (стержни вдоль X и Y, защитный слой), справа — остальное
SECTION_FIELDS = [
    ("top", "L", _mesh_card(u"Верхняя сетка",
                            u"У верхней грани плиты. «Вдоль X» — стержни по оси X плана (вдоль самой длинной кромки, "
                            u"стрелка X на «Плане»), «вдоль Y» — поперёк. Шаг — между осями стержней. Защитный слой — "
                            u"от верхней грани бетона до ближайшего стержня.", "dtx", "stx", "dty", "sty", "t", "top")),
    ("bot", "L", _mesh_card(u"Нижняя сетка",
                            u"У нижней грани плиты, поля — как у верхней. Защитный слой — от нижней грани.",
                            "dbx", "sbx", "dby", "sby", "b", "bot")),
    ("mesh", "L", _mesh_card(u"Сетка полки", u"Одна сетка по середине толщины полки ребристой плиты.",
                             "dmx", "smx", "dmy", "smy", None, "mesh")),
    ("order", "R", {"title": u"Порядок слоёв",
                    "note": u"Какие стержни ближе к граням плиты: у нижней сетки — самые нижние, у верхней — самые верхние.",
                    "rows": [[("combo", "x_out", [Option(u"снаружи стержни X", True), Option(u"снаружи стержни Y", False)], 210)]]}),
    ("ubar", "R", {"title": u"П-стержни по контуру",
                   "note": u"Охватывают нижнюю и верхнюю сетки по краю плиты и по краям отверстий "
                           u"(включаются в «Параметрах»). Ножки — на длину анкеровки.",
                   "rows": [[u"Ø", ("diam", "du")], [("hint", "du")]]}),
    ("frog", "R", {"title": u"Поддерживающие каркасы (лягушки)",
                   "note": u"Держат верхнюю сетку над нижней. Ставится один элемент, общее число — в параметре «АрмЧисло».",
                   "rows": [[("combo", "frogs", [Option(u"ставить", True), Option(u"не ставить", False)], 140)],
                            [u"Ø", ("diam", "df"), u"шаг", ("num", "sf", 50), u"мм"], [("hint", "sf")]]}),
    ("deck", "R", {"title": u"Стержни в гофрах профлиста",
                   "note": u"По одному стержню в каждой гофре вдоль пролёта. Шаг гофр берётся из профиля в типе плиты.",
                   "rows": [[u"Ø", ("diam", "dr")], [u"Шаг гофр", ("num", "pr", 50), u"мм"],
                            [u"Первая гофра от края", ("num", "fr", 50), u"мм"], [("hint", "pr")]]}),
]
# Строка «Сетка» ребристой плиты редактирует те же значения, что и «Низ»: свои ключи полей -> ключи параметров
MESH_ROW = (("dmx", "dbx"), ("smx", "sbx"), ("dmy", "dby"), ("smy", "sby"))
PLAN_FIELDS = [
    ("add", "L", {"title": u"Усиление отверстий",
                  "note": u"С каждой стороны отверстия — дополнительные стержни параллельно прерванным, с заведением "
                          u"за отверстие на длину анкеровки. У малых отверстий стержни не обрезаются, а раздвигаются.",
                  "rows": [[u"Ø", ("diam", "d_add"), u"по", ("num", "n_add", 34), u"шт. с каждой стороны"], [("hint", "n_add")],
                           [u"Малые отверстия — до", ("num", "small", 46), u"мм"], [("hint", "small")]]}),
]
DIAM_DEFAULT_KEYS = ("du", "d_add", "df")       # пустой выбор Ø — «как сетка» / по норме


def visible_section(kind):
    if kind in ("solid", "foundation"):
        return ["top", "bot", "ubar", "frog", "order"]
    if kind == "deck":
        return ["top", "deck", "order"]
    return ["mesh"]


class SlabsWindow(ui.KzhWindow):
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
            u"Эти плиты армироваться не будут.", details=self.skipped,
            details_header=u"Пропущено: {}".format(len(self.skipped)))

        self.editor = SectionEditor(self.window, self.cnvSection, self.cnvFields, self.refresh)
        self.editor.build(SECTION_FIELDS)
        self.peditor = SectionEditor(self.window, self.cnvElev, self.cnvElevFields, self.refresh)
        self.peditor.build(PLAN_FIELDS)
        for ed in (self.editor, self.peditor):
            for key in DIAM_DEFAULT_KEYS:
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
        for chk in (self.chkUEdges, self.chkUOpen, self.chkStagger):
            chk.Checked += lambda s, e: self.refresh()
            chk.Unchecked += lambda s, e: self.refresh()
        self.tbMaxLen.TextChanged += lambda s, e: self.refresh()
        for cnv in (self.cnvSection, self.cnvElev):
            cnv.SizeChanged += lambda s, e: self.refresh()
        self.btnRun.Click += self.on_run

    def load_general(self, saved):
        o = dict(GENERAL_DEFAULTS)
        o.update(dict((k, v) for k, v in saved.items() if k in o))
        ui.select_option(self.cbSteel, o["steel"])
        ui.select_option(self.cbStSteel, o["st_steel"])
        ui.select_option(self.cbConcrete, o["concrete"])
        self.chkUEdges.IsChecked = bool(o["u_edges"])
        self.chkUOpen.IsChecked = bool(o["u_open"])
        self.chkStagger.IsChecked = bool(o["stagger"])
        self.tbMaxLen.Text = ui.number_text(o["max_len"])

    def read_general(self):
        return {
            "steel": ui.selected_value(self.cbSteel), "st_steel": ui.selected_value(self.cbStSteel),
            "concrete": ui.selected_value(self.cbConcrete),
            "max_len": ui.parse_number(self.tbMaxLen.Text, GENERAL_DEFAULTS["max_len"]),
            "stagger": bool(self.chkStagger.IsChecked),
            "u_edges": bool(self.chkUEdges.IsChecked), "u_open": bool(self.chkUOpen.IsChecked),
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
        """Параметры группы для типа плиты: по умолчанию, затем сохранённые для этого типа."""
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
            self.editor.show_only(visible_section(kind))
            self.peditor.show_only(["add"])
            values = dict(g.opts)
            for field, key in MESH_ROW:
                values[field] = g.opts[key]
            self.editor.load(values)
            self.peditor.load(g.opts)
        finally:
            self._loading = False

    def read_opts(self):
        """Поля -> opts текущей группы. Возвращает текст ошибки или None."""
        g = self.current
        o = g.opts
        values = {}
        self.editor.read(values, [k for k in self.editor.inputs])
        self.peditor.read(values, [k for k in self.peditor.inputs])
        for field, key in MESH_ROW:
            if o["kind"] == "ribbed":
                values[key] = values[field]
            del values[field]
        o.update(values)
        for key in ("dbx", "dby", "dtx", "dty", "dr"):
            if o[key] is None:
                o[key] = MESH_DEFAULTS[key]
        for key in ("sbx", "sby", "stx", "sty"):
            if not o[key] or o[key] < 50:
                return u"Шаг стержней сетки — не меньше 50 мм."
        for key in ("c_b", "c_t", "sf", "small", "pr", "fr"):
            if o[key] is not None and o[key] < 0:
                return u"Значения не могут быть отрицательными."
        if o["pr"] is not None and o["pr"] < 50:
            return u"Шаг гофр — не меньше 50 мм."
        if o["n_add"] is not None and (o["n_add"] < 1 or o["n_add"] != int(o["n_add"])):
            return u"Число дополнительных стержней — целое число от 1."
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
            calc = Calc(ho, g.opts, g.t, g.deck)
            plan, reason = plan_slab(calc, item)
            if plan is None:
                self.tbInfo.Text = reason
                return
            so = g.opts
            self.editor.set_hint("c_b", u"" if so["c_b"] else u"пусто — {:.0f} по норме".format(float(calc.c_auto_b)))
            self.editor.set_hint("c_t", u"" if so["c_t"] else u"пусто — {:.0f} по норме".format(float(calc.c_auto_t)))
            s_max = norms.slab_spacing_max(calc.t)
            self.editor.set_hint("bot", u"шаг по СП 63 — не более {:.0f} мм".format(float(s_max)))
            self.editor.set_hint("mesh", u"одна сетка по середине полки; шаг ≤ {:.0f} мм".format(float(s_max)))
            self.editor.set_hint("top", u"шаг по СП 63 — не более {:.0f} мм".format(float(s_max)))
            self.editor.set_hint("du", u"шаг — как у сетки")
            self.editor.set_hint("sf", u"{} шт. → АрмЧисло; пусто — шаг {:.0f}, Ø {}".format(
                plan.frogs, float(calc.sf), calc.df) if so["frogs"] else u"")
            deck = item.deck or {}
            self.editor.set_hint("pr", u"профиль «{}»: высота {:.0f}, шаг {:.0f}".format(
                deck.get("name", u"—"), float(deck.get("h") or 0), float(deck.get("pitch") or 0)) if deck else
                u"в типе плиты нет профлиста — задайте шаг")
            self.peditor.set_hint("n_add", u"" if so["n_add"] else u"пусто — по норме (≥ половины прерванных, ≥ 2)")
            self.peditor.set_hint("small", u"" if so["small"] is not None else u"пусто — {:.0f} (практика)".format(float(calc.small)))
            targets = draw_section(self.cnvSection, calc, item, plan, self.editor.pad(top=40, bottom=60))
            self.editor.place(targets)
            ptargets = draw_plan(self.cnvElev, calc, item, plan, self.peditor.pad(top=30, bottom=30))
            self.peditor.place(ptargets)
        except Exception as ex:
            self.tbInfo.Text = u"Проверьте значения в полях ({})".format(ex)
            return
        counts = u", ".join(u"{} {}".format(title, plan.count(key)) for key, _, title in CATEGORIES if plan.count(key))
        mus = u", ".join(u"{} {:.2f} %".format(LAYER_TITLES[k], float(calc.mu(k))) for k in ("bx", "by", "tx", "ty") if k in calc.layers)
        info = u"{} • t = {:.0f} мм • μ: {} • стержней: {}".format(KIND_TITLES[calc.kind], float(calc.t), mus, counts)
        notes = plan.notes + calc.warnings
        if notes:
            info += u" • Внимание: " + u"; ".join(notes)
        self.tbSectionInfo.Text = info
        self.tbElevInfo.Text = (u"План: {} (линии), дополнительные стержни у отверстий (зелёные), П-стержни (синие), "
                                u"лягушки (квадраты), рёбра — пунктиром. Отверстий: {}. Ось X — вдоль самой длинной кромки{}.".format(
                                    u"нижняя сетка" if calc.kind != "deck" else u"верхняя сетка", len(item.openings),
                                    u" (у профлиста — вдоль пролёта)" if calc.kind == "deck" else u""))
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
def _is_slab(element):
    cat = element.Category
    if cat is None:
        return False
    cid = cat.Id.IntegerValue
    if isinstance(element, DB.Floor):
        return cid in (int(DB.BuiltInCategory.OST_Floors), int(DB.BuiltInCategory.OST_StructuralFoundation))
    # Сборные плиты (каркас несущий) — чтобы показать причину пропуска
    if cid == int(DB.BuiltInCategory.OST_StructuralFraming) and isinstance(element, DB.FamilyInstance):
        return u"плита" in element.Symbol.Family.Name.lower()
    return False


class SlabFilter(ISelectionFilter):
    def AllowElement(self, element):
        return _is_slab(element)

    def AllowReference(self, reference, point):
        return False


def get_slabs():
    """Плиты из текущего выделения, иначе — выбор в модели."""
    flt = SlabFilter()
    els = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    els = [e for e in els if e is not None and flt.AllowElement(e)]
    if els:
        return els
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, u"Выберите плиты и нажмите «Готово»")
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: в семействе армирование плит не выполняется.")
        return
    slabs = get_slabs()
    if not slabs:
        return
    groups, skipped = group_slabs(slabs)
    if not groups:
        show_message(u"Среди выбранных нет плит, которые можно армировать.", details=skipped,
                     details_header=u"Пропущено: {}".format(len(skipped)))
        return
    win = SlabsWindow(groups, skipped, len(slabs))
    win.show_modal()
    if win.result is not None:
        reinforce(groups, win.result, skipped)


try:
    main()
except Exception:
    show_message(u"Ошибка армирования плит:\n{}".format(traceback.format_exc()), title=u"Ошибка")

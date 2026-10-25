# -*- coding: utf-8 -*-
"""Армирование балок (pyRevit / IronPython 2.7).

Поддерживаются прямые балки без поворота сечения с постоянным по длине сечением. Форма сечения
определяется по геометрии: прямоугольное; тавр с полкой сверху; ригель с полками внизу (обратный тавр
и Г-образный — полка с одной стороны); двутавр. Двускатные, трапециевидные, криволинейные балки
пропускаются с указанием причины. Выбранные балки группируются по сечению: у каждой группы своя
раскладка и свои зоны хомутов.

Армирование составного сечения «ребро + полки» (конструктивная схема, параметры — в полях):
- ребро — как прямоугольная балка: верх, низ, боковые (при h > 700 мм по СП 63), замкнутый хомут
  на всю высоту, шпильки к боковым; при стенке уже 140 мм — плоский каркас по оси стенки
  (по одному стержню сверху и снизу, поперечные стержни с шагом зон);
- полка — продольные стержни у верхней и нижней граней каждого свеса (n шт. от кромки к ребру)
  и замкнутые хомуты по контуру полки с заданным шагом.

Раздел «Сечение» — как в ЛИРА: рисунок и поля у стержней. Раздел «По длине» — вид балки с полями
на чертеже: зоны хомутов от начала балки (длина, шаг, Ø, «+ зона»), отступ первого хомута.
Раздел «Параметры» — материалы, концы стержней, отгибы, шаг шпилек.

Концы верхних и нижних стержней ребра: опора (колонна, стена, балка) ищется по геометрии за торцом.
Если её толщины хватает на анкеровку — прямой заход на lan, иначе до дальней грани опоры
с загибом (нижние — вверх, верхние — вниз). Без опоры стержень обрывается у торца.
"""
__title__ = u"Балки"
__author__ = u"Daniil Antonenko"
__doc__ = u"Армирование балок: прямоугольные, тавр, ригель с полками внизу, двутавр; сечение как в ЛИРА, зоны хомутов по длине"

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
from kzh import norms, settings
from kzh import rebar as kr
from kzh.geometry import ft, mm
from kzh.section_editor import SectionEditor, ZoneEditor
from kzh.ui import show_message, Option

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

SETTINGS_SECTION = "beams"
PART_NAME = u"Балка"
WHAT = {"many": u"балок", "title": u"Армирование балок", "done": u"Армировано балок",
        "failed": u"Не армирована", "armed": u"уже армирована"}

ENDS_AUTO, ENDS_FLUSH = "auto", "flush"
TIES_AUTO, TIES_NONE = "auto", "none"

LENGTH_DEFAULTS = {
    "steel": u"А500", "st_steel": u"А240", "concrete": u"В25", "cover_cond": "indoor_normal",
    "ends": ENDS_AUTO, "first_offset": None, "hook": None, "tie_hook": None, "tie_every": 2,
}
# Раскладка в сечении (как в ЛИРА): ребро — верх, низ, боковые у каждой грани (n_side None — по СП 63);
# защитные слои по СП 63 до поверхности продольных стержней: c_s — у боковых граней и кромок полок,
# c_t — сверху, c_b — снизу. Полки: nf — стержней у каждой грани свеса, df — их Ø, dsf/sf — хомут полки Ø и шаг.
# Хомут ребра Ø и шаг S — основная зона хомутов (зона с пустой длиной на виде по длине)
SECTION_DEFAULTS = {"n_bot": 3, "d_bot": 16, "n_top": 2, "d_top": 12, "n_side": None, "d_side": 12,
                    "c_s": None, "c_t": None, "c_b": None, "ties": TIES_AUTO, "dt": 8,
                    "nf_t": 2, "df_t": 10, "dsf_t": 8, "sf_t": None,
                    "nf_b": 2, "df_b": 12, "dsf_b": 8, "sf_b": None}


# ==========================================================
# ГРУППЫ БАЛОК ПО СЕЧЕНИЮ
# ==========================================================
class BeamGroup(object):
    def __init__(self, sec):
        self.b = int(round(mm(sec.b)))      # мм — габарит
        self.h = int(round(mm(sec.h)))
        self.shape = sec.shape
        self.items = []     # [(балка, BeamSection)]
        self.opts = None
        self.zones = []
        self.Name = u""
        self._supports = {}

    def supports(self, index):
        """(опора у начала, опора у конца): (толщина в футах, категория) или None. С кэшем."""
        if index not in self._supports:
            beam, sec = self.items[index]
            self._supports[index] = (kg.beam_support(doc, beam, sec, True), kg.beam_support(doc, beam, sec, False))
        return self._supports[index]


def beam_title(beam):
    sym = beam.Symbol if hasattr(beam, 'Symbol') else None
    name = element_name(sym) if sym is not None else beam.Name
    return u"{} (id {})".format(name, beam.Id.IntegerValue)


def group_beams(beams):
    groups, skipped = {}, []
    for beam in beams:
        if not kr.is_valid_host(beam):
            skipped.append(u"{} — не может быть основой арматуры (материал не бетон?)".format(beam_title(beam)))
            continue
        try:
            sec, err = kg.beam_section(beam)
        except Exception as ex:
            sec, err = None, u"ошибка геометрии: {}".format(ex)
        if sec is None:
            skipped.append(u"{} — {}".format(beam_title(beam), err))
            continue
        key = (int(round(mm(sec.b))), int(round(mm(sec.h))), sec.shape.key())
        if key not in groups:
            groups[key] = BeamGroup(sec)
        groups[key].items.append((beam, sec))
    ordered = sorted(groups.values(), key=lambda g: (-len(g.items), g.b, g.h))
    for g in ordered:
        name = u"{}×{}".format(g.b, g.h)
        if g.shape.kind != "rect":
            name += u", " + g.shape.title()
        g.Name = u"{} — {} шт.".format(name, len(g.items))
    return ordered, skipped


# ==========================================================
# РАСЧЁТ (без изменения модели). Размеры — мм, кроме явно указанных футов
# ==========================================================
FLAT_WEB = norms.beam_rule("flat_web_mm")["value"]


class Calc(object):
    """Величины для группы балок: ho — общие настройки, so — раскладка в сечении, shape — форма сечения,
    h — высота, zones — зоны хомутов ребра."""

    def __init__(self, ho, so, shape, h, zones):
        self.ho, self.so, self.shape, self.zones = ho, so, shape, zones
        self.ds = max(z["d"] for z in zones)
        ds = self.ds
        cond = ho["cover_cond"]
        d_main = max(so["d_bot"], so["d_top"])
        self.cover_min = norms.cover_main(cond, d_main)
        self.cover_st_min = norms.cover_constructive(cond, ds)
        self.cover_auto = norms.ceil_to(max(self.cover_min, self.cover_st_min + ds), 5.0)
        self.covers = dict((k, so[k] or self.cover_auto) for k in ("c_s", "c_t", "c_b"))
        self.flat = (shape.web[1] - shape.web[0]) < FLAT_WEB
        self.n_side = so["n_side"]
        if self.flat:
            self.n_side = 0
        elif self.n_side is None:
            self.n_side = norms.beam_side_bars(h, self.covers["c_t"], self.covers["c_b"])
        self.diams = [so["d_bot"], so["d_top"]] + ([so["d_side"]] if self.n_side else [])
        self.cover_end = norms.cover_main(cond, max(self.diams))      # у торцов балки без опоры
        self.h0 = h - self.covers["c_b"] - so["d_bot"] / 2.0
        self.first_offset = (ho["first_offset"] if ho["first_offset"] is not None
                             else norms.beam_rule("first_stirrup_offset")["value"])
        self.flange_step = norms.beam_rule("flange_stirrup")["mm"]

        w = self.warnings = []
        if ds < norms.beam_stirrup_d_min(h):
            w.append(u"диаметр хомутов {} мм меньше {} мм".format(ds, norms.beam_stirrup_d_min(h)))
        s_mid = norms.beam_stirrup_spacing(self.h0, False)
        for i, z in enumerate(zones):
            if z["step"] > s_mid:
                w.append(u"зона {}: шаг {:.0f} мм больше {:.0f} мм (0,75h0, ≤ 500)".format(
                    i + 1, float(z["step"]), float(s_mid)))
        for key, c in sorted(self.covers.items()):
            if c < self.cover_min:
                w.append(u"защитный слой {} = {:.0f} мм меньше {:.0f} мм".format(key, float(c), float(self.cover_min)))
            elif c - ds < self.cover_st_min:
                w.append(u"защитный слой хомута у {} — {:.0f} мм, меньше {:.0f} мм".format(
                    key, float(c - ds), float(self.cover_st_min)))
        if not self.flat and self.n_side == 0 and norms.beam_side_bars(h, self.covers["c_t"], self.covers["c_b"]) > 0:
            w.append(u"при h > {} мм по СП 63 нужны боковые стержни".format(norms.beam_rule("side_bars")["h_mm"]))
        if shape.taper >= 3:
            w.append(u"стенка переменной ширины ({:.0f} мм) — раскладка по наименьшей".format(float(shape.taper)))

    def anchorage(self, d):
        ho = self.ho
        return norms.anchorage_length(d, ho["steel"], ho["concrete"], False)

    def info_text(self):
        return u"Анкеровка: низ ⌀{} — {:.0f} мм, верх ⌀{} — {:.0f} мм • защитный слой по норме {:.0f} мм".format(
            self.so["d_bot"], float(self.anchorage(self.so["d_bot"])), self.so["d_top"],
            float(self.anchorage(self.so["d_top"])), float(self.cover_auto))


def default_zones(length, h, d_bot):
    """Зоны хомутов по умолчанию: у опор — 1/4 пролёта с шагом 0,5h0, в середине — остаток с шагом 0,75h0."""
    h0 = h - 30 - d_bot / 2.0
    sup = norms.floor_to(norms.beam_stirrup_spacing(h0, True))
    mid = norms.floor_to(norms.beam_stirrup_spacing(h0, False))
    zone = norms.ceil_to(length * norms.beam_rule("support_zone")["k_span"], 50.0)
    d = max(8, norms.beam_stirrup_d_min(h))
    return [{"len": zone, "step": sup, "d": d}, {"len": None, "step": mid, "d": d}, {"len": zone, "step": sup, "d": d}]


def spread(a, b, n):
    """n точек от a до b включительно (n = 1 — только a)."""
    if n <= 1:
        return [a]
    return [a + k * (b - a) / (n - 1) for k in range(n)]


class Layout(object):
    """Стержни в сечении (мм): v — поперёк от оси габарита, t — вверх от низа.
    Ребро — по ширине стенки; ось хомута Ø ds — на (слой − ds/2) от граней. Полки — свои стержни и хомуты."""

    def __init__(self, calc, b, h, ds=None):
        so, cv, sh = calc.so, calc.covers, calc.shape
        ds = ds or calc.ds
        self.error = None
        self.notes = []
        cs, ct, cb = cv["c_s"], cv["c_t"], cv["c_b"]
        wv0, wv1 = sh.web
        self.vc = (wv0 + wv1) / 2.0                # ось ребра
        bw = wv1 - wv0
        self.flat = calc.flat
        nb, nt, ns = int(so["n_bot"]), int(so["n_top"]), int(calc.n_side)
        self.t_bot = cb + so["d_bot"] / 2.0
        self.t_top = h - ct - so["d_top"] / 2.0
        if self.flat:
            # Плоский каркас: по одному стержню сверху и снизу по оси стенки, поперечные стержни рядом с ними
            nb = nt = 1
            self.v_bot = [self.vc]
            self.v_top = [self.vc]
            self.link_v = self.vc + (max(so["d_bot"], so["d_top"]) + ds) / 2.0
            self.st_v = self.st_top = self.st_bot = None
            self.notes.append(u"стенка {:.0f} мм — плоский каркас по оси стенки".format(float(bw)))
        else:
            self.st_v = bw / 2.0 - cs + ds / 2.0          # от оси ребра
            self.st_top = h - ct + ds / 2.0
            self.st_bot = cb - ds / 2.0
            yb, yt = bw / 2.0 - cs - so["d_bot"] / 2.0, bw / 2.0 - cs - so["d_top"] / 2.0
            self.v_bot = [self.vc - yb + k * 2 * yb / (nb - 1) for k in range(nb)] if nb > 1 else []
            self.v_top = [self.vc - yt + k * 2 * yt / (nt - 1) for k in range(nt)] if nt > 1 else []
            if nb < 2 or nt < 2:
                self.error = u"нижних и верхних стержней ребра должно быть не меньше 2"
            elif yb <= 0 or yt <= 0:
                self.error = u"ребро слишком узкое для таких защитных слоёв"
            else:
                clear_min = norms.column_rule("clear_spacing_min")["value"]
                for pitch, d, name in ((2 * yb / (nb - 1), so["d_bot"], u"нижних"), (2 * yt / (nt - 1), so["d_top"], u"верхних")):
                    if pitch - d < max(d, clear_min):
                        self.error = u"{} стержней не помещается: в свету {:.0f} мм < {:.0f} мм".format(
                            name, float(pitch - d), float(max(d, clear_min)))
        self.v_side = bw / 2.0 - cs - so["d_side"] / 2.0          # от оси ребра
        self.t_side = [self.t_bot + (k + 1) * (self.t_top - self.t_bot) / (ns + 1) for k in range(ns)]
        self.ties = list(range(ns)) if (so["ties"] != TIES_NONE and not self.flat) else []
        if self.t_top - self.t_bot <= 0:
            self.error = u"сечение слишком низкое для таких защитных слоёв"
        if ns and (self.t_top - self.t_bot) / (ns + 1) > norms.beam_rule("side_bars")["spacing_mm"]:
            self.notes.append(u"шаг боковых стержней больше {} мм".format(norms.beam_rule("side_bars")["spacing_mm"]))

        # Полки: (имя, v0, v1, t0, t1, n, d, ds, верх ли)
        self.flanges = []
        for key, f, top in (("t", sh.top, True), ("b", sh.bottom, False)):
            if f is None:
                continue
            v0, v1, hf = f
            t0, t1 = (h - hf, h) if top else (0.0, hf)
            n, d, dsf = int(so["nf_" + key]), so["df_" + key], so["dsf_" + key]
            fl = {"key": key, "v0": v0, "v1": v1, "t0": t0, "t1": t1, "n": n, "d": d, "ds": dsf, "top": top}
            # Слои стержней полки: у наружной грани (верх полки сверху, низ полки снизу) и у внутренней
            outer = (t1 - ct - d / 2.0) if top else (t0 + cb + d / 2.0)
            inner = (t0 + cb + d / 2.0) if top else (t1 - ct - d / 2.0)
            fl["layers"] = [outer] + ([inner] if abs(inner - outer) >= 2 * d else [])
            if len(fl["layers"]) == 1:
                self.notes.append(u"полка {} тонкая — стержни только у наружной грани".format(u"сверху" if top else u"снизу"))
            # Свесы: слева и справа от ребра, стержни от кромки к ребру
            fl["overhangs"] = []
            for tip, face, sign in ((v0, wv0, 1.0), (v1, wv1, -1.0)):
                if abs(tip - face) < 30:
                    continue
                a = tip + sign * (cs + d / 2.0)
                bnd = face - sign * (d / 2.0 + 15.0)
                span = (bnd - a) * sign
                # Сколько стержней помещается в свес с расстоянием в свету не меньше d и 25 мм
                fit = int(span / (d + max(d, norms.column_rule("clear_spacing_min")["value"]))) + 1 if span > 0 else 1
                k = max(1, min(n, fit))
                note = u"в свес полки {} помещается {} стерж. ⌀{} из {}".format(u"сверху" if top else u"снизу", k, d, n)
                if k < n and note not in self.notes:
                    self.notes.append(note)
                fl["overhangs"].append(spread(a, bnd, k) if k > 1 else [a])
            fl["loop"] = (v0 + cs - dsf / 2.0, v1 - cs + dsf / 2.0,
                          t0 + cb - dsf / 2.0, t1 - ct + dsf / 2.0)
            self.flanges.append(fl)

        self.count = nb + nt + 2 * ns + sum(len(fl["layers"]) * sum(len(o) for o in fl["overhangs"]) for fl in self.flanges)
        q = lambda d: math.pi * d * d / 4.0
        self.as_bot = nb * q(so["d_bot"])
        self.as_top = nt * q(so["d_top"])
        self.mu = 100.0 * self.as_bot / (bw * calc.h0) if calc.h0 > 0 else 0.0


def bar_end(calc, d, support, mode):
    """(вылет за торец балки, длина загиба) в мм для конца верхнего или нижнего стержня."""
    c = calc.cover_end
    if mode == ENDS_FLUSH or support is None:
        return -c, 0.0
    available = mm(support[0]) - c
    lan = calc.anchorage(d)
    if available >= lan:
        return lan, 0.0
    return available, norms.ceil_to(max(lan - max(available, 0.0), norms.beam_rule("bend_leg_k_d")["value"] * d))


def row_profile(calc, length, t, d, supports, up, h):
    """Точки (x, t) в мм для стержня ряда: up — загибы вверх (нижний ряд) или вниз (верхний).
    Возвращает (точки, число загибов)."""
    mode = calc.ho["ends"]
    ext0, leg0 = bar_end(calc, d, supports[0], mode)
    ext1, leg1 = bar_end(calc, d, supports[1], mode)
    room = h - calc.covers["c_t"] - calc.covers["c_b"] - d       # загиб не длиннее, чем помещается по высоте
    sign = 1.0 if up else -1.0
    pts = []
    if leg0:
        pts.append((-ext0, t + sign * min(leg0, room)))
    pts += [(-ext0, t), (length + ext1, t)]
    if leg1:
        pts.append((length + ext1, t + sign * min(leg1, room)))
    return pts, (1 if leg0 else 0) + (1 if leg1 else 0)


def plan_long(calc, sec, length, b, h, supports, specs):
    """Продольные стержни: ребро (верх, низ, боковые) и полки. Возвращает Layout."""
    lay = Layout(calc, b, h)
    if lay.error:
        return lay
    so = calc.so
    to_xyz = lambda pts, v: [sec.point(ft(x), ft(v), ft(t)) for x, t in pts]
    role = {0: "straight", 1: "L", 2: "U"}
    # Нижний и верхний ряды ребра: набор вдоль y (поперёк балки)
    for vs, t, d, up in ((lay.v_bot, lay.t_bot, so["d_bot"], True), (lay.v_top, lay.t_top, so["d_top"], False)):
        pts, bends = row_profile(calc, length, t, d, supports, up, h)
        specs.append(kr.BarSpec(role[bends], d, kr.polyline(to_xyz(pts, vs[0])), sec.y,
                                ("fixed", len(vs), ft(vs[-1] - vs[0])) if len(vs) > 1 else None, len(vs)))
    c = calc.cover_end
    straight = lambda v, t: kr.polyline([sec.point(ft(c), ft(v), ft(t)), sec.point(ft(length - c), ft(v), ft(t))])
    # Боковые стержни ребра: прямые, от торца до торца с защитным слоем; набор вдоль w у каждой грани
    if lay.t_side:
        ns = len(lay.t_side)
        span = lay.t_side[-1] - lay.t_side[0]
        for v in (lay.vc - lay.v_side, lay.vc + lay.v_side):
            specs.append(kr.BarSpec("straight", so["d_side"], straight(v, lay.t_side[0]), sec.w,
                                    ("fixed", ns, ft(span)) if ns > 1 else None, ns))
    # Полки: стержни у граней каждого свеса — набор вдоль y
    for fl in lay.flanges:
        for vs in fl["overhangs"]:
            vs = sorted(vs)
            for t in fl["layers"]:
                specs.append(kr.BarSpec("straight", fl["d"], straight(vs[0], t), sec.y,
                                        ("fixed", len(vs), ft(vs[-1] - vs[0])) if len(vs) > 1 else None, len(vs)))
    return lay


def plan_stirrups(calc, sec, length, b, h, specs):
    """Поперечная арматура: хомуты ребра (или стержни плоского каркаса) и шпильки по зонам, хомуты полок.
    Возвращает (зоны, число хомутов ребра, число шпилек, число хомутов полок)."""
    so, ho = calc.so, calc.ho
    x0, x3 = calc.first_offset, length - calc.first_offset
    zones = kr.zone_layout(calc.zones, x0, x3)
    n_st = n_ties = n_fl = 0
    dt = so["dt"]
    for x, zlen, step, d, first, last in zones:
        lay = Layout(calc, b, h, d)
        count = kr.zone_count(zlen, step, first, last)
        if count < 1:
            continue
        layout = ("spacing", ft(step), ft(zlen), first, last)
        if lay.flat:
            # Поперечный стержень плоского каркаса: вертикальный, рядом с продольными, от низа до верха с защитным слоем
            line = kr.polyline([sec.point(ft(x), ft(lay.link_v), ft(calc.covers["c_b"])),
                                sec.point(ft(x), ft(lay.link_v), ft(h - calc.covers["c_t"]))])
            specs.append(kr.BarSpec("link", d, line, sec.a, layout, count))
            n_st += count
            continue
        loop = [(lay.vc - lay.st_v, lay.st_bot), (lay.vc + lay.st_v, lay.st_bot), (lay.vc + lay.st_v, lay.st_top),
                (lay.vc - lay.st_v, lay.st_top), (lay.vc - lay.st_v, lay.st_bot)]
        curves = kr.polyline([sec.point(ft(x), ft(v), ft(t)) for v, t in loop])
        specs.append(kr.BarSpec("stirrup", d, curves, sec.a, layout, count))
        n_st += count
        tie_step = step * int(ho["tie_every"] or 1)
        tie_count = kr.zone_count(zlen, tie_step, first, last)
        xt = x + (d + dt) / 2.0
        ext = (so["d_side"] + dt) / 2.0
        for i in lay.ties:
            if tie_count < 1:
                break
            t = lay.t_side[i]
            line = kr.polyline([sec.point(ft(xt), ft(lay.vc - lay.v_side - ext), ft(t)),
                                sec.point(ft(xt), ft(lay.vc + lay.v_side + ext), ft(t))])
            specs.append(kr.BarSpec("pin", dt, line, sec.a, ("spacing", ft(tie_step), ft(zlen), first, last), tie_count))
            n_ties += tie_count
    # Хомуты полок: замкнутые по контуру полки, один шаг по всей длине
    lay = Layout(calc, b, h)
    for fl in lay.flanges:
        step = so["sf_" + fl["key"]] or calc.flange_step
        xa, xb = x0, x3
        count = kr.zone_count(xb - xa, step, True, True)
        va, vb, ta, tb = fl["loop"]
        loop = [(va, ta), (vb, ta), (vb, tb), (va, tb), (va, ta)]
        xs = xa + (fl["ds"] + calc.ds)        # рядом с хомутом ребра, не в одной плоскости
        curves = kr.polyline([sec.point(ft(xs), ft(v), ft(t)) for v, t in loop])
        specs.append(kr.BarSpec("stirrup", fl["ds"], curves, sec.a,
                                ("spacing", ft(step), ft(xb - xs), True, True), count))
        n_fl += count
    return zones, n_st, n_ties, n_fl


def plan_beam(calc, group, index):
    beam, sec = group.items[index]
    b, h, length = float(group.b), float(group.h), mm(sec.length)
    supports = group.supports(index)
    specs = []
    lay = plan_long(calc, sec, length, b, h, supports, specs)
    if lay.error:
        return None, lay.error
    if length - 2 * calc.first_offset < 100:
        return None, u"балка слишком короткая для хомутов"
    zones, n_st, n_ties, n_fl = plan_stirrups(calc, sec, length, b, h, specs)
    so = calc.so
    sup_text = u", ".join(s[1] if s else u"нет" for s in supports)
    flanges = u"".join(u", полка {}: {} стерж. ⌀{}, хомуты ×{}".format(
        u"верх" if fl["top"] else u"низ", len(fl["layers"]) * sum(len(o) for o in fl["overhangs"]), fl["d"], n_fl)
        for fl in lay.flanges)
    summary = u"{}: {}{}, L = {:.0f}, низ {}⌀{}, верх {}⌀{}{}, {} ×{}{}{}, опоры: {}".format(
        beam_title(beam), sec.size_text(), u" ({})".format(group.shape.title()) if group.shape.kind != "rect" else u"",
        float(length), len(lay.v_bot), so["d_bot"], len(lay.v_top), so["d_top"],
        u", боковые 2×{}⌀{}".format(len(lay.t_side), so["d_side"]) if lay.t_side else u"",
        u"поперечные стержни каркаса" if lay.flat else u"хомуты", n_st,
        u", шпильки ×{}".format(n_ties) if n_ties else u"", flanges, sup_text)
    return {"host": beam, "title": beam_title(beam), "specs": specs, "summary": summary, "notes": lay.notes}, None


def reinforce(groups, ho, skipped):
    plans, warnings, skipped = [], [], list(skipped)
    for g in groups:
        calc = Calc(ho, g.opts, g.shape, float(g.h), g.zones)
        warnings.extend(u"{}: {}".format(g.Name, w) for w in calc.warnings)
        for i in range(len(g.items)):
            try:
                plan, reason = plan_beam(calc, g, i)
            except Exception as ex:
                plan, reason = None, u"ошибка расчёта: {}".format(ex)
            if plan is None:
                skipped.append(u"{} — {}".format(beam_title(g.items[i][0]), reason))
            else:
                plans.append(plan)
    hooks = {"stirrup": runner.find_hook(doc, ho["hook"]), "pin": runner.find_hook(doc, ho["tie_hook"])}
    runner.run(doc, plans, skipped, warnings, ho["steel"], ho["st_steel"], hooks, PART_NAME, WHAT)


# ==========================================================
# ПРЕДПРОСМОТР
# ==========================================================
def section_outline(shape, h):
    """Контур сечения по профилю (мм): слева снизу вверх и справа сверху вниз."""
    rows = shape.rows
    pts = []
    prev = None
    for t, e in rows:
        if prev is not None and abs(prev - e[0]) > 0.5:
            pts.append((prev, t))
        pts.append((e[0], t))
        prev = e[0]
    pts = [(rows[0][1][0], 0.0)] + pts + [(rows[-1][1][0], h)]
    right = []
    prev = None
    for t, e in reversed(rows):
        if prev is not None and abs(prev - e[1]) > 0.5:
            right.append((prev, t))
        right.append((e[1], t))
        prev = e[1]
    right = [(rows[-1][1][1], h)] + right + [(rows[0][1][1], 0.0)]
    return pts + right


def draw_section(canvas, calc, group, pad):
    """Сечение балки с полями в стиле ЛИРА. Возвращает (Layout, цели выносок)."""
    so, sh = calc.so, calc.shape
    b, h = float(group.b), float(group.h)
    sk = kd.Sketch(canvas, (-b / 2.0, 0.0, b / 2.0, h), pad=pad)
    lay = Layout(calc, b, h)
    cv = calc.covers
    P = lambda v, t: (sk.X(v), sk.Y(t))
    sk.poly(section_outline(sh, h), kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
    wv0, wv1 = sh.web
    for fl in lay.flanges:
        va, vb, ta, tb = fl["loop"]
        sk.rect(va, ta, vb, tb, kd.STIRRUP, max(1.5, fl["ds"] * sk.k))
    if lay.flat:
        sk.line(lay.link_v, cv["c_b"], lay.link_v, h - cv["c_t"], kd.STIRRUP, max(2.0, calc.ds * sk.k))
    else:
        sk.rect(lay.vc - lay.st_v, lay.st_bot, lay.vc + lay.st_v, lay.st_top, kd.STIRRUP, max(2.0, calc.ds * sk.k))
    ext = (so["d_side"] + so["dt"]) / 2.0
    for i in lay.ties:
        sk.line(lay.vc - lay.v_side - ext, lay.t_side[i], lay.vc + lay.v_side + ext, lay.t_side[i], kd.STIRRUP,
                max(1.5, so["dt"] * sk.k))
    for k, v in enumerate(lay.v_bot):
        sk.circle(v, lay.t_bot, so["d_bot"] / 2.0, kd.CORNER if k in (0, len(lay.v_bot) - 1) else kd.SECOND)
    for k, v in enumerate(lay.v_top):
        sk.circle(v, lay.t_top, so["d_top"] / 2.0, kd.CORNER if k in (0, len(lay.v_top) - 1) else kd.SECOND)
    for t in lay.t_side:
        for v in (lay.vc - lay.v_side, lay.vc + lay.v_side):
            sk.circle(v, t, so["d_side"] / 2.0, kd.SECOND)
    for fl in lay.flanges:
        for vs in fl["overhangs"]:
            for v in vs:
                for t in fl["layers"]:
                    sk.circle(v, t, fl["d"] / 2.0, kd.SECOND)
    sk.dim(-b / 2.0, h, b / 2.0, h, u"b = {:.0f}".format(float(b)))
    sk.dim(b / 2.0, 0, b / 2.0, h, u"h = {:.0f}".format(float(h)), outside="right")
    if sh.kind != "rect":
        sk.dim(wv0, 0, wv1, 0, u"{:.0f}".format(float(wv1 - wv0)), outside="below")
    # Защитные слои — у левого верхнего угла ребра и правого нижнего
    xs, yt, yb = wv0 + cv["c_s"], h - cv["c_t"], cv["c_b"]
    top_edge = sh.top[0] if sh.top else wv0
    sk.cover_dim(top_edge, yt, top_edge + cv["c_s"], yt, u"c = {:.0f}".format(float(cv["c_s"])), left=True)
    sk.cover_dim(top_edge + cv["c_s"], h, top_edge + cv["c_s"], yt, u"c = {:.0f}".format(float(cv["c_t"])))
    bot_edge = sh.bottom[1] if sh.bottom else wv1
    sk.cover_dim(bot_edge - cv["c_s"], 0, bot_edge - cv["c_s"], yb, u"c = {:.0f}".format(float(cv["c_b"])))
    targets = {
        "cover_s": P(top_edge + cv["c_s"] / 2.0, yt),
        "cover_t": P(top_edge + cv["c_s"], (h + yt) / 2.0),
        "top": P(lay.v_top[-1], lay.t_top),
        "side": P(lay.vc + lay.v_side, lay.t_side[len(lay.t_side) // 2]) if lay.t_side else P(wv1, h / 2.0),
        "bot": P(lay.v_bot[-1], lay.t_bot),
        "cover_b": P(bot_edge - cv["c_s"], yb / 2.0),
        "stirrup": P(lay.link_v, h / 2.0) if lay.flat else P(lay.vc - lay.st_v, h / 2.0),
        "ties": P(lay.vc + lay.v_side * 0.5, lay.t_side[lay.ties[0]]) if lay.ties else None,
    }
    for fl in lay.flanges:
        key = fl["key"]
        tip = fl["overhangs"][-1][0] if fl["overhangs"] else fl["v1"]       # стержень у кромки свеса
        targets["fbars_" + key] = P(tip, fl["layers"][0])
        va, vb, ta, tb = fl["loop"]
        targets["fstir_" + key] = P(va, (ta + tb) / 2.0)
    return lay, targets


def ties_text(so, lay):
    if lay.flat:
        return u"плоский каркас — шпильки не ставятся"
    if lay.ties:
        return u"к боковым стержням — {} шт. по высоте (поперёк b)".format(len(lay.ties))
    if not lay.t_side:
        return u"нет боковых стержней — шпильки не нужны"
    return u"шпильки не ставятся"


def draw_elevation(canvas, calc, group, pad):
    """Вид первой балки группы по длине: опоры, стержни, хомуты по зонам, цепочка размеров зон снизу.
    Возвращает (цели выносок, фактические зоны [(начало, длина, шаг, Ø, число)])."""
    beam, sec = group.items[0]
    so, sh = calc.so, calc.shape
    b, h, length = float(group.b), float(group.h), mm(sec.length)
    supports = group.supports(0)
    widths = [mm(s[0]) if s else 0.0 for s in supports]
    sk = kd.Sketch(canvas, (-max(widths[0], 200.0), -350.0, length + max(widths[1], 200.0), h + 250.0),
                   pad=pad, min_height_px=260)
    if supports[0]:
        sk.rect(-widths[0], -200, 0, h + 150, kd.CONCRETE_EDGE, 1.0, kd.SLAB, dash=True)
    if supports[1]:
        sk.rect(length, -200, length + widths[1], h + 150, kd.CONCRETE_EDGE, 1.0, kd.SLAB, dash=True)
    sk.rect(0, 0, length, h, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
    for f, top in ((sh.top, True), (sh.bottom, False)):
        if f is not None:
            t = h - f[2] if top else f[2]
            sk.line(0, t, length, t, kd.CONCRETE_EDGE, 0.8, dash=True)       # граница полки на виде
    specs = []
    lay = plan_long(calc, sec, length, b, h, supports, specs)
    if lay.error:
        sk.text(length / 2.0, h / 2.0, lay.error, kd.WARN)
        return {}, []
    for t, d, up in ((lay.t_bot, so["d_bot"], True), (lay.t_top, so["d_top"], False)):
        pts, _ = row_profile(calc, length, t, d, supports, up, h)
        sk.poly(pts, kd.MAIN, 1.8)
    for t in lay.t_side:
        sk.line(calc.cover_end, t, length - calc.cover_end, t, kd.SECOND, 1.2, dash=True)
    st = []
    zones, _, _, _ = plan_stirrups(calc, sec, length, b, h, st)
    drawn = 0
    for x, zlen, step, d, first, last in zones:
        lz = Layout(calc, b, h, d)
        n = kr.zone_count(zlen, step, first, last)
        pitch = zlen / max(math.ceil(zlen / step - 1e-6), 1)
        start = x if first else x + pitch
        ta, tb = (calc.covers["c_b"], h - calc.covers["c_t"]) if lz.flat else (lz.st_bot, lz.st_top)
        for i in range(max(n, 0)):
            if drawn > 300:
                break
            sk.line(start + i * pitch, ta, start + i * pitch, tb, kd.STIRRUP, 1.0)
            drawn += 1
    # Цепочка зон под балкой, общий размер над балкой
    y_chain = -200.0
    targets, facts = {}, []
    for x, zlen, step, d, first, last in zones:
        sk.dim_line(x, y_chain, x + zlen, y_chain)
        facts.append((x, zlen, step, d, kr.zone_count(zlen, step, first, last)))
    for i in range(len(group.zones)):
        if i < len(zones):
            x, zlen = zones[i][0], zones[i][1]
            targets[u"zone_{}".format(i)] = (sk.X(x + zlen / 2.0), sk.Y(y_chain))
        else:
            targets[u"zone_{}".format(i)] = (sk.X(length), sk.Y(h), False)
    targets["zone_add"] = (sk.X(length), sk.Y(h) - 40, False)
    sk.dim(0, h, length, h, u"L = {:.0f}".format(float(length)))
    targets["first"] = (sk.X(calc.first_offset), sk.Y(lay.st_bot if lay.st_bot is not None else calc.covers["c_b"]))
    return targets, facts


# ==========================================================
# ОКНО НАСТРОЕК
# ==========================================================
XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Армирование балок" Width="1480" Height="900" MinWidth="1100" MinHeight="700"
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
                            <TextBlock Grid.Row="0" Text="КЖ • БАЛКИ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navSection" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Сечение"/>
                                <RadioButton Name="navLength" GroupName="nav" Style="{StaticResource NavItem}" Content="По длине"/>
                                <RadioButton Name="navParams" GroupName="nav" Style="{StaticResource NavItem}" Content="Параметры"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="БАЛКИ / СЕЧЕНИЯ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
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
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <ComboBox Name="cbGroup" Style="{StaticResource PillComboBox}" Tag="Группа:"/>
                                <Button Name="btnSkipped" Grid.Column="1" Style="{StaticResource GlassButton}" Margin="10,0,0,0"/>
                            </Grid>
                        </Border>

                        <!-- РАЗДЕЛ 1: СЕЧЕНИЕ -->
                        <Border Name="pageSection" Grid.Row="1" Style="{StaticResource GlassCard}" Padding="10" Visibility="Visible">
                            <Grid>
                                <Grid.RowDefinitions>
                                    <RowDefinition Height="*"/>
                                    <RowDefinition Height="Auto"/>
                                </Grid.RowDefinitions>
                                <Border Background="#FFFFFFFF" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="16" MinHeight="380">
                                    <Grid>
                                        <Canvas Name="cnvSection" ClipToBounds="True"/>
                                        <Canvas Name="cnvFields" ClipToBounds="True"/>
                                    </Grid>
                                </Border>
                                <TextBlock Name="tbSectionInfo" Grid.Row="1" Style="{StaticResource Hint}" Margin="4,8,0,0"/>
                            </Grid>
                        </Border>

                        <!-- РАЗДЕЛ 2: ПО ДЛИНЕ -->
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
                                        <TextBlock Text="Концы верхних и нижних стержней" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <WrapPanel>
                                            <RadioButton Name="rbEndsAuto" GroupName="ends" Style="{StaticResource ModePill}" Content="Анкеровка в опору" Margin="0,0,8,6"/>
                                            <RadioButton Name="rbEndsFlush" GroupName="ends" Style="{StaticResource ModePill}" Content="Обрыв у торца" Margin="0,0,8,6"/>
                                        </WrapPanel>
                                        <TextBlock Style="{StaticResource Hint}" Text="Опора (колонна, стена, балка) ищется за торцом. Хватает толщины — прямой заход на длину анкеровки, иначе до дальней грани опоры с загибом (нижние — вверх, верхние — вниз, не менее 10d). Нет опоры — обрыв у торца с защитным слоем."/>
                                    </StackPanel>
                                </Border>
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="Материалы" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <ComboBox Name="cbSteel" Style="{StaticResource PillComboBox}" Tag="Продольная:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbStSteel" Style="{StaticResource PillComboBox}" Tag="Хомуты:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbConcrete" Style="{StaticResource PillComboBox}" Tag="Бетон:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbCover" Style="{StaticResource PillComboBox}" Tag="Условия:"/>
                                    </StackPanel>
                                </Border>
                                <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                    <StackPanel>
                                        <TextBlock Text="Отгибы и шпильки" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                        <ComboBox Name="cbHook" Style="{StaticResource PillComboBox}" Tag="Хомуты:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbTieHook" Style="{StaticResource PillComboBox}" Tag="Шпильки:" Margin="0,0,0,6"/>
                                        <ComboBox Name="cbTieEvery" Style="{StaticResource PillComboBox}" Tag="Шпильки по длине:"/>
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
    "tbCount", "tbDoc", "cbGroup", "btnSkipped", "cnvSection", "cnvFields", "tbSectionInfo",
    "cnvElev", "cnvElevFields", "tbElevInfo", "rbEndsAuto", "rbEndsFlush",
    "cbSteel", "cbStSteel", "cbConcrete", "cbCover", "cbHook", "cbTieHook", "cbTieEvery", "tbInfo", "btnRun",
)
NAV_PAGES = (("navSection", "pageSection"), ("navLength", "pageLength"), ("navParams", "pageParams"))

def _flange_card(key, title):
    return {"title": title,
            "note": u"Продольные стержни — у каждой грани свеса, считая от кромки к ребру; замкнутые хомуты — по контуру полки.",
            "rows": [[u"Стержни", ("num", "nf_" + key, 40), u"шт. Ø", ("diam", "df_" + key)], [("hint", "nf_" + key)],
                     [u"Хомуты Ø", ("diam", "dsf_" + key), u"шаг", ("num", "sf_" + key, 52), u"мм"], [("hint", "sf_" + key)]]}


# Раздел «Сечение»: слева — защитные слои, хомуты, шпильки; справа — продольные стержни и полки
SECTION_FIELDS = [
    ("cover_t", "L", {"title": u"Защитные слои",
                      "note": u"От грани бетона до поверхности продольного стержня. Пусто — по СП 63 для условий из «Параметров».",
                      "rows": [[u"Сверху", ("num", "c_t", 46), u"мм"], [("hint", "c_t")],
                               [u"Снизу", ("num", "c_b", 46), u"мм"], [("hint", "c_b")],
                               [u"По бокам", ("num", "c_s", 46), u"мм"], [("hint", "c_s")]]}),
    ("stirrup", "L", {"title": u"Хомуты ребра",
                      "note": u"Ø и шаг основной зоны. Зоны с другим шагом (учащение у опор) — в разделе «По длине».",
                      "rows": [[u"Ø", ("diam", "ds"), u"шаг", ("num", "step", 52), u"мм"], [("hint", "step")]]}),
    ("ties", "L", {"title": u"Шпильки", "note": u"Поперёк ребра к боковым стержням.",
                   "rows": [[("combo", "ties", [Option(u"к боковым стержням", TIES_AUTO), Option(u"без шпилек", TIES_NONE)], 200)],
                            [u"Ø", ("diam", "dt")], [("hint", "ties")]]}),
    ("top", "R", {"title": u"Верхние стержни ребра", "note": u"У верхней грани, включая угловые.",
                  "rows": [[u"Количество", ("num", "n_top", 40), u"шт. Ø", ("diam", "d_top")], [("hint", "n_top")]]}),
    ("side", "R", {"title": u"Боковые стержни", "note": u"У каждой боковой грани ребра (по СП 63 — при высоте более 700 мм).",
                   "rows": [[u"Количество", ("num", "n_side", 40), u"шт. Ø", ("diam", "d_side")], [("hint", "n_side")]]}),
    ("bot", "R", {"title": u"Нижние стержни ребра", "note": u"У нижней грани, включая угловые.",
                  "rows": [[u"Количество", ("num", "n_bot", 40), u"шт. Ø", ("diam", "d_bot")], [("hint", "n_bot")]]}),
    ("fbars_t", "R", _flange_card("t", u"Полка сверху")),
    ("fbars_b", "R", _flange_card("b", u"Полка снизу")),
]
BASE_GROUPS = ("cover_t", "stirrup", "top", "side", "bot", "ties")
ELEV_FIELDS = [
    ("first", "L", {"title": u"Первый хомут", "note": u"Расстояние от торца балки до первого хомута.",
                    "rows": [[u"Отступ", ("num", "first_offset", 46), u"мм"], [("hint", "first_offset")]]}),
]


def visible_groups(shape):
    keys = list(BASE_GROUPS)
    if shape.top is not None:
        keys.append("fbars_t")
    if shape.bottom is not None:
        keys.append("fbars_b")
    return keys


class BeamsWindow(ui.KzhWindow):
    def __init__(self, groups, skipped, total):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES, NAV_PAGES)
        self.result = None
        self.groups = groups
        self.skipped = skipped
        self.current = None
        self._loading = False
        self._src = "section"
        self.tbCount.Text = u"{} / {}".format(total, len(groups))
        self.tbDoc.Text = doc.Title
        self.btnSkipped.Content = u"Не поддерживается: {}".format(len(skipped))
        self.btnSkipped.Visibility = Visibility.Visible if skipped else Visibility.Collapsed
        self.btnSkipped.Click += lambda s, e: show_message(
            u"Эти балки армироваться не будут.", details=self.skipped,
            details_header=u"Пропущено: {}".format(len(self.skipped)))

        self.editor = SectionEditor(self.window, self.cnvSection, self.cnvFields, lambda: self.on_edit("section"))
        self.editor.build(SECTION_FIELDS)
        self.zeditor = ZoneEditor(self.window, self.cnvElev, self.cnvElevFields, lambda: self.on_edit("zones"))
        self.zeditor.build(ELEV_FIELDS)

        steel = lambda: [Option(s, s) for s in norms.steel_classes()]
        self.cbSteel.ItemsSource = steel()
        self.cbStSteel.ItemsSource = steel()
        self.cbConcrete.ItemsSource = [Option(c, c) for c in norms.concrete_classes()]
        self.cbCover.ItemsSource = [Option(u"{} — {} мм".format(t, v), k) for k, t, v in norms.cover_conditions()]
        hook_names = [element_name(h) for h in kr.hook_types(doc)]
        self.cbHook.ItemsSource = [Option(u"Без отгибов", u"")] + [Option(n, n) for n in hook_names]
        self.cbTieHook.ItemsSource = [Option(u"Без отгибов", u"")] + [Option(n, n) for n in hook_names]
        self.cbTieEvery.ItemsSource = [Option(u"на каждом хомуте", 1), Option(u"через один хомут", 2)]

        saved = settings.load(SETTINGS_SECTION)
        self.saved = saved
        for g in groups:
            g.opts = dict(SECTION_DEFAULTS)
            g.opts.update(dict((k, v) for k, v in (saved.get("section") or {}).items() if k in g.opts))
            g.zones = default_zones(mm(g.items[0][1].length), float(g.h), g.opts["d_bot"])
        self.load_length(saved.get("length") or {})
        self.cbGroup.ItemsSource = groups
        self.cbGroup.SelectionChanged += self.on_group_changed
        self.cbGroup.SelectedIndex = 0

        for cb in (self.cbSteel, self.cbStSteel, self.cbConcrete, self.cbCover, self.cbTieEvery, self.cbHook):
            cb.SelectionChanged += lambda s, e: self.refresh()
        for rb in (self.rbEndsAuto, self.rbEndsFlush):
            rb.Checked += lambda s, e: self.refresh()
        for cnv in (self.cnvSection, self.cnvElev):
            cnv.SizeChanged += lambda s, e: self.refresh()
        self.btnRun.Click += self.on_run

    def load_length(self, saved):
        o = dict(LENGTH_DEFAULTS)
        o.update(dict((k, v) for k, v in saved.items() if k in o))
        default_hook = kr.default_stirrup_hook(doc)
        default_hook = element_name(default_hook) if default_hook is not None else u""
        ui.select_option(self.cbSteel, o["steel"])
        ui.select_option(self.cbStSteel, o["st_steel"])
        ui.select_option(self.cbConcrete, o["concrete"])
        ui.select_option(self.cbCover, o["cover_cond"])
        ui.select_option(self.cbHook, default_hook if o["hook"] is None else o["hook"])
        ui.select_option(self.cbTieHook, default_hook if o["tie_hook"] is None else o["tie_hook"])
        ui.select_option(self.cbTieEvery, o["tie_every"])
        self.zeditor.load({"first_offset": o["first_offset"]})
        self.rbEndsAuto.IsChecked = o["ends"] != ENDS_FLUSH
        self.rbEndsFlush.IsChecked = o["ends"] == ENDS_FLUSH

    def read_length(self):
        v = {}
        self.zeditor.read(v, ("first_offset",))
        return {
            "steel": ui.selected_value(self.cbSteel), "st_steel": ui.selected_value(self.cbStSteel),
            "concrete": ui.selected_value(self.cbConcrete), "cover_cond": ui.selected_value(self.cbCover),
            "ends": ENDS_FLUSH if self.rbEndsFlush.IsChecked else ENDS_AUTO, "first_offset": v["first_offset"],
            "hook": ui.selected_value(self.cbHook, u""), "tie_hook": ui.selected_value(self.cbTieHook, u""),
            "tie_every": ui.selected_value(self.cbTieEvery, 2),
        }

    @staticmethod
    def fill_zone(g):
        for z in g.zones:
            if not z["len"]:
                return z
        return max(g.zones, key=lambda z: z["len"])

    def load_section(self, g):
        self._loading = True
        try:
            self.editor.show_only(visible_groups(g.shape))
            values = dict(g.opts)
            fz = self.fill_zone(g)
            values["ds"], values["step"] = fz["d"], fz["step"]
            self.editor.load(values)
        finally:
            self._loading = False

    def read_section(self):
        g = self.current
        o = g.opts
        v = {}
        self.editor.read(v, ("ds", "step"))
        self.editor.read(o, SECTION_DEFAULTS.keys())
        fz = self.fill_zone(g)
        if v["ds"]:
            fz["d"] = v["ds"]
        if v["step"] and v["step"] > 0:
            fz["step"] = v["step"]
        for key in ("n_top", "n_bot"):
            if o[key] is None or o[key] < 1 or o[key] != int(o[key]):
                return u"Верхних и нижних стержней — целое число (для замкнутого хомута не меньше 2)."
        for key in ("n_side", "nf_t", "nf_b"):
            if o[key] is not None and (o[key] < 0 or o[key] != int(o[key])):
                return u"Число стержней — целое число от 0."
        if any(o[k] is not None and o[k] <= 0 for k in ("c_s", "c_t", "c_b", "sf_t", "sf_b")):
            return u"Защитный слой и шаг хомутов полок должны быть больше нуля."
        for key in ("nf_t", "nf_b"):
            if o[key] is None:
                o[key] = SECTION_DEFAULTS[key]
        return None

    def on_edit(self, source):
        self._src = source
        self.refresh()

    def read_all(self):
        g = self.current
        g.zones = self.zeditor.zones
        if self._src == "section":
            err = self.zeditor.read_zones() or self.read_section()
        else:
            err = self.read_section() or self.zeditor.read_zones()
        if not err:
            fz = self.fill_zone(g)
            i = g.zones.index(fz)
            self.zeditor.load({u"z_step_{}".format(i): fz["step"], u"z_d_{}".format(i): fz["d"]})
            self.editor.load({"ds": fz["d"], "step": fz["step"]})
        return err

    def on_group_changed(self, sender, args):
        g = self.cbGroup.SelectedItem
        if g is None:
            return
        if self.current is not None:
            self.read_all()
        self.current = g
        self._loading = True
        try:
            self.zeditor.set_zones(g.zones)
            g.zones = self.zeditor.zones
        finally:
            self._loading = False
        self.load_section(g)
        self.refresh()

    def refresh(self):
        g = self.current
        if self._loading or g is None:
            return
        err = self.read_all()
        if err:
            self.tbInfo.Text = err
            return
        try:
            ho = self.read_length()
            calc = Calc(ho, g.opts, g.shape, float(g.h), g.zones)
            for key in ("c_s", "c_t", "c_b"):
                self.editor.set_hint(key, u"" if g.opts.get(key) else u"пусто — {:.0f} по норме".format(float(calc.cover_auto)))
            self.editor.set_hint("step", u"основная зона по длине")
            flat = u"плоский каркас: 1 стержень" if calc.flat else u""
            self.editor.set_hint("n_top", flat)
            self.editor.set_hint("n_bot", flat)
            self.editor.set_hint("n_side", u"плоский каркас — нет" if calc.flat else (
                u"" if g.opts["n_side"] is not None else
                u"пусто — {} по СП 63 (при h > {} мм)".format(calc.n_side, norms.beam_rule("side_bars")["h_mm"])))
            for key in ("t", "b"):
                self.editor.set_hint("nf_" + key, u"у каждой грани свеса, от кромки к ребру")
                self.editor.set_hint("sf_" + key, u"" if g.opts["sf_" + key] else
                                     u"пусто — {:.0f} (практика)".format(float(calc.flange_step)))
            lay, targets = draw_section(self.cnvSection, calc, g, self.editor.pad())
            self.editor.set_hint("ties", ties_text(g.opts, lay))
            self.editor.place(targets)
            self.zeditor.set_hint("first_offset", u"" if ho["first_offset"] is not None else
                                  u"пусто — {:.0f}".format(float(calc.first_offset)))
            etargets, facts = draw_elevation(self.cnvElev, calc, g, self.zeditor.pad(top=30, bottom=30))
            for i in range(len(g.zones)):
                if i < len(facts):
                    x, zlen, step, d, count = facts[i]
                    self.zeditor.set_hint(u"z_{}".format(i), u"{:.0f} мм, {} {} шт.".format(
                        float(zlen), u"поперечных стержней" if calc.flat else u"хомутов", count))
                else:
                    self.zeditor.set_hint(u"z_{}".format(i), u"не помещается по длине")
            self.zeditor.place(etargets)
        except Exception as ex:
            self.tbInfo.Text = u"Проверьте значения в полях ({})".format(ex)
            return
        info = u"{} • стержней: {} • As низ ребра = {:.1f} см², верх = {:.1f} см² • μ = {:.2f} % • h0 = {:.0f} мм".format(
            g.shape.title().capitalize(), lay.count, float(lay.as_bot / 100.0), float(lay.as_top / 100.0),
            float(lay.mu), float(calc.h0))
        if lay.error:
            info += u" • ОШИБКА: " + lay.error
        notes = lay.notes + calc.warnings
        if notes:
            info += u" • Внимание: " + u"; ".join(notes)
        self.tbSectionInfo.Text = info
        self.tbElevInfo.Text = (u"Зоны хомутов ребра — от начала балки; пустая длина — зона занимает остаток. "
                                u"Хомуты полок — с шагом из раздела «Сечение» по всей длине. "
                                u"Вид — по первой балке группы, опоры найдены по геометрии.")
        self.tbInfo.Text = calc.info_text()

    def on_run(self, sender, args):
        if self.current is not None:
            err = self.read_all()
            if err:
                show_message(err)
                return
        ho = self.read_length()
        if ho["first_offset"] is not None and ho["first_offset"] < 0:
            show_message(u"Отступ первого хомута не может быть отрицательным.")
            return
        data = dict(self.saved)
        data["length"] = ho
        if self.current is not None:
            data["section"] = dict(self.current.opts)
        settings.save(SETTINGS_SECTION, data)
        self.result = ho
        self.window.Close()


# ==========================================================
# ЗАПУСК
# ==========================================================
class BeamFilter(ISelectionFilter):
    def AllowElement(self, element):
        cat = element.Category
        return cat is not None and cat.Id.IntegerValue == int(DB.BuiltInCategory.OST_StructuralFraming)

    def AllowReference(self, reference, point):
        return False


def get_beams():
    """Балки из текущего выделения, иначе — выбор в модели."""
    flt = BeamFilter()
    beams = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    beams = [b for b in beams if b is not None and flt.AllowElement(b)]
    if beams:
        return beams
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, u"Выберите балки и нажмите «Готово»")
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: в семействе армирование балок не выполняется.")
        return
    beams = get_beams()
    if not beams:
        return
    groups, skipped = group_beams(beams)
    if not groups:
        show_message(u"Среди выбранных нет балок, которые можно армировать.", details=skipped,
                     details_header=u"Пропущено: {}".format(len(skipped)))
        return
    win = BeamsWindow(groups, skipped, len(beams))
    win.show_modal()
    if win.result is not None:
        reinforce(groups, win.result, skipped)


try:
    main()
except Exception:
    show_message(u"Ошибка армирования балок:\n{}".format(traceback.format_exc()), title=u"Ошибка")

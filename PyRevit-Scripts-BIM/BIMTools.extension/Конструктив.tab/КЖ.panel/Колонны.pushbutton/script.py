# -*- coding: utf-8 -*-
"""Армирование колонн (pyRevit / IronPython 2.7).

Колонна разбирается по геометрии на участки по высоте: участок постоянного сечения (прямоугольное
или круглое) и зона консолей между участками или у верха (колонны с консолями, ступенчатые колонны).
Выбранные колонны группируются по форме («400×400 — 5 шт.», «600×400 / консоли ±X / 600×400 — 2 шт.»).

Раздел «Сечение» — как в ЛИРА: рисунок сечения участка и поля у стержней (угловые и промежуточные
верх/низ, боковые, хомут Ø и S, защитные слои, шпильки); для зоны консолей — профиль консоли и поля
её армирования (рабочие стержни поверху, горизонтальные хомуты). Раздел «По высоте» — вид колонны
с полями прямо на чертеже: зоны хомутов снизу вверх (длина, шаг, Ø, «+ зона»), отступы хомутов,
нахлёст; монолитная или сборная колонна, материалы.

Монолитная: низ стержней — низ колонны (стык с выпусками снизу), верх — выпуск, отгиб, загиб в плиту
или обрыв; подрезка по плите над колонной. Сборная: стержни обрываются с защитным слоем у торцов.
Нормы — из lib/kzh/norms.json.
"""
__title__ = u"Колонны"
__author__ = u"Daniil Antonenko"
__doc__ = u"Армирование колонн, в том числе с консолями: сечение как в ЛИРА, зоны хомутов по высоте, выпуски, сборные и монолитные"

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

SETTINGS_SECTION = "columns"
PART_NAME = u"Колонна"
RECT, ROUND, PROFILE = "rect", "round", "profile"
MONO, PRECAST = "mono", "precast"

JOINT_LAP = "lap"        # прямой выпуск на длину нахлёста
JOINT_CRANK = "crank"    # выпуск с отгибом внутрь на один диаметр
JOINT_BEND = "bend"      # верхняя колонна: загиб в плиту
JOINT_FLUSH = "flush"    # без выпуска: обрыв под верхом с защитным слоем
JOINT_TEXT = {
    JOINT_LAP: u"прямой выпуск",
    JOINT_CRANK: u"выпуск с отгибом",
    JOINT_BEND: u"загиб в плиту",
    JOINT_FLUSH: u"без выпуска",
}
JOINT_ROLE = {JOINT_LAP: "straight", JOINT_CRANK: "crank", JOINT_BEND: "L", JOINT_FLUSH: "straight"}

TIES_AUTO, TIES_EACH, TIES_OTHER, TIES_NONE = "auto", "each", "other", "none"
TIES_OPTIONS = ((TIES_AUTO, u"Авто по СП 63"), (TIES_EACH, u"К каждому стержню"),
                (TIES_OTHER, u"Через один"), (TIES_NONE, u"Без шпилек"))

# Настройки по высоте — общие для всех выбранных колонн
HEIGHT_DEFAULTS = {
    "steel": u"А500", "st_steel": u"А240", "concrete": u"В25", "cover_cond": "indoor_normal",
    "build": MONO, "joint": JOINT_LAP, "compressed": False, "lap": None, "cut_by_slab": True,
    "first_offset": None, "top_offset": None, "hook": None, "tie_hook": None,
}
# Раскладка в сечении участка (как в ЛИРА): угловые верх/низ, промежуточные верх/низ (у граней b),
# боковые (у граней h, обе стороны), защитные слои по СП 63 до поверхности продольных стержней:
# c_s — у боковых граней, c_t — у верхней, c_b — у нижней (None — по норме). Хомут Ø и шаг S в сечении —
# это основная зона хомутов (зона с пустой длиной на виде по высоте).
SECTION_DEFAULTS = {
    RECT: {"d_ct": 16, "d_cb": 16, "n_t": 1, "d_t": 16, "n_b": 1, "d_b": 16, "n_s": 1, "d_s": 16,
           "c_s": None, "c_t": None, "c_b": None, "ties": TIES_AUTO, "dt": 8},
    ROUND: {"n": 8, "d": 16, "c": None},
}
# Зона консолей: рабочие стержни поверху (cn шт. Ø cd), горизонтальные хомуты Ø cds с шагом cstep, слой cc
CORBEL_DEFAULTS = {"cn": 3, "cd": 16, "cds": 8, "cstep": None, "cc": None}


def default_zones(dmax, ds):
    """Зоны хомутов по умолчанию: учащение внизу (зона нахлёста), основная (остаток), учащение вверху."""
    dense = norms.floor_to(norms.column_stirrup_spacing(dmax, dense=True))
    step = norms.floor_to(norms.column_stirrup_spacing(dmax))
    lap = norms.lap_length(dmax, HEIGHT_DEFAULTS["steel"], HEIGHT_DEFAULTS["concrete"], False)
    top = norms.column_rule("dense_top")["value"]
    return [{"len": lap, "step": dense, "d": ds}, {"len": None, "step": step, "d": ds},
            {"len": float(top), "step": dense, "d": ds}]


# ==========================================================
# ГРУППЫ КОЛОНН ПО ФОРМЕ
# ==========================================================
def column_title(col):
    sym = col.Symbol if hasattr(col, 'Symbol') else None
    name = element_name(sym) if sym is not None else col.Name
    return u"{} (id {})".format(name, col.Id.IntegerValue)


def part_kind(part):
    return ROUND if part.sec.is_round else RECT


def profile_key(prof):
    """Ключ группы: форма колонны без высот участков (их учитывают зоны хомутов)."""
    key = []
    for p in prof.parts:
        if p.kind == "shaft":
            cu = (p.sec.center - prof.origin).DotProduct(prof.ax)
            cv = (p.sec.center - prof.origin).DotProduct(prof.ay)
            key.append((part_kind(p), int(round(mm(p.sec.b))), int(round(mm(p.sec.h))),
                        int(round(mm(cu))), int(round(mm(cv)))))
        else:
            key.append(("corbel", tuple(sorted((k.side, int(round(mm(k.reach))), int(round(mm(k.z_top - k.z_bottom))))
                                              for k in p.corbels))))
    return tuple(key)


def part_title(prof, i):
    p = prof.parts[i]
    levels = u"{:+.3f}…{:+.3f}".format(float(p.z0 * 0.3048), float(p.z1 * 0.3048))
    if p.kind == "shaft":
        n = sum(1 for q in prof.parts[:i + 1] if q.kind == "shaft")
        return u"Участок {}: {} ({})".format(n, p.sec.size_text(), levels)
    sides = u", ".join(u"{} {:.0f}".format(k.side_text(), mm(k.reach)) for k in p.corbels) or u"переход"
    return u"Консоли {} ({})".format(sides, levels)


class ColumnGroup(object):
    def __init__(self, key, prof):
        self.key = key
        self.items = []         # [(колонна, ColumnProfile)]
        self.prof0 = prof       # профиль первой колонны — для рисунков
        self.kind = PROFILE if not prof.is_simple else part_kind(prof.parts[0])
        self.part_opts = []     # настройки по участкам (как prof.parts)
        self.zones = []
        self.Name = u""
        self._slab = {}

    def title(self):
        prof = self.prof0
        if prof.is_simple:
            sec = prof.parts[0].sec
            name = (u"Круглое ⌀{:.0f}".format(mm(sec.b)) if sec.is_round
                    else u"Прямоугольное {:.0f}×{:.0f}".format(mm(sec.b), mm(sec.h)))
        else:
            bits = []
            for p in prof.parts:
                if p.kind == "shaft":
                    bits.append(p.sec.size_text())
                else:
                    bits.append(u"консоли " + u", ".join(k.side_text() for k in p.corbels) if p.corbels else u"переход")
            name = u" / ".join(bits)
        return u"{} — {} шт.".format(name, len(self.items))

    def slab(self, index=0):
        """Плита над колонной (для подрезки), с кэшем."""
        if index not in self._slab:
            col, prof = self.items[index]
            top = prof.parts[-1]
            sec = top.sec if top.kind == "shaft" else top.below.sec
            self._slab[index] = kg.slab_at(doc, kg.column_samples(sec, prof.z_top), prof.z_top)
        return self._slab[index]


def group_columns(columns):
    """(группы по форме колонны, пропущенные с причиной)."""
    groups, skipped = {}, []
    for col in columns:
        if not kr.is_valid_host(col):
            skipped.append(u"{} — не может быть основой арматуры (материал не бетон?)".format(column_title(col)))
            continue
        try:
            prof, err = kg.column_profile(col)
        except Exception as ex:
            prof, err = None, u"ошибка геометрии: {}".format(ex)
        if prof is None:
            skipped.append(u"{} — {}".format(column_title(col), err))
            continue
        key = profile_key(prof)
        if key not in groups:
            groups[key] = ColumnGroup(key, prof)
        groups[key].items.append((col, prof))
    ordered = sorted(groups.values(), key=lambda g: (g.kind != RECT, -len(g.items), g.key))
    for g in ordered:
        g.Name = g.title()
    return ordered, skipped


# ==========================================================
# РАСЧЁТ (без изменения модели). Размеры — мм, кроме явно указанных футов
# ==========================================================
class Calc(object):
    """Величины для участка: ho — настройки по высоте, so — раскладка в сечении, zones — зоны хомутов."""

    def __init__(self, ho, so, kind, zones):
        self.ho, self.so, self.kind, self.zones = ho, so, kind, zones
        self.ds = max(z["d"] for z in zones)
        ds = self.ds
        if kind == ROUND:
            self.diams = [so["d"]]
        else:
            self.diams = ([so["d_ct"], so["d_cb"]] + ([so["d_t"]] if so["n_t"] else []) +
                          ([so["d_b"]] if so["n_b"] else []) + ([so["d_s"]] if so["n_s"] else []))
        dmax, dmin = max(self.diams), min(self.diams)
        self.first_offset = (ho["first_offset"] if ho["first_offset"] is not None
                             else norms.column_rule("first_stirrup_offset")["value"])
        self.top_offset = ho["top_offset"] if ho["top_offset"] is not None else self.first_offset

        cond = ho["cover_cond"]
        self.cover_min = norms.cover_main(cond, dmax)
        self.cover_st_min = norms.cover_constructive(cond, ds)
        # По норме: слой до продольных такой, чтобы и у хомута был свой минимум (округление до 5 мм)
        self.cover_auto = norms.ceil_to(max(self.cover_min, self.cover_st_min + ds), 5.0)
        if kind == ROUND:
            self.covers = {"c": so["c"] or self.cover_auto}
        else:
            self.covers = dict((k, so[k] or self.cover_auto) for k in ("c_s", "c_t", "c_b"))
        self.cover_long = min(self.covers.values())       # для вида по высоте
        self.cover_end = norms.cover_main(cond, dmax)     # у торцов: верх плиты, торцы сборной колонны

        w = self.warnings = []
        if dmin < norms.column_rule("long_d_min")["value"]:
            w.append(u"диаметр продольных {} мм меньше {} мм".format(dmin, norms.column_rule("long_d_min")["value"]))
        if ds < norms.column_stirrup_d_min(dmax):
            w.append(u"диаметр хомутов {} мм меньше {:.0f} мм".format(
                ds, float(math.ceil(norms.column_stirrup_d_min(dmax)))))
        step_max = norms.column_stirrup_spacing(dmin)
        for i, z in enumerate(zones):
            if z["step"] > step_max:
                w.append(u"зона {}: шаг {:.0f} мм больше {:.0f} мм".format(i + 1, float(z["step"]), float(step_max)))
        for key, c in sorted(self.covers.items()):
            if c < self.cover_min:
                w.append(u"защитный слой {} = {:.0f} мм меньше {:.0f} мм".format(key, float(c), float(self.cover_min)))
            elif c - ds < self.cover_st_min:
                w.append(u"защитный слой хомута у {} — {:.0f} мм, меньше {:.0f} мм".format(
                    key, float(c - ds), float(self.cover_st_min)))

    def lap(self, d):
        ho = self.ho
        return ho["lap"] or norms.lap_length(d, ho["steel"], ho["concrete"], ho["compressed"])

    def anchorage(self, d):
        ho = self.ho
        return norms.anchorage_length(d, ho["steel"], ho["concrete"], ho["compressed"])

    def info_text(self):
        if self.ho["build"] == PRECAST:
            return u"Сборная колонна: стержни обрываются у торцов с защитным слоем {:.0f} мм".format(float(self.cover_end))
        laps = u", ".join(u"⌀{} — {:.0f}".format(d, float(self.lap(d))) for d in sorted(set(self.diams)))
        return u"Нахлёст: {} мм • защитный слой по норме {:.0f} мм".format(laps, float(self.cover_auto))


def tied_indices(mode, count, face, bars_on_face):
    """Индексы промежуточных стержней грани, закрепляемых шпильками."""
    if count == 0 or mode == TIES_NONE:
        return []
    if mode == TIES_AUTO:
        if not norms.column_ties_needed(face, bars_on_face):
            return []
        mode = TIES_OTHER
    if mode == TIES_EACH:
        return list(range(count))
    # Через один: между закреплёнными (угловые закреплены хомутом) не больше одного свободного
    return [i for i in range(count) if i % 2 == 1]


def spread(a, b, n):
    """n точек, равномерно между a и b (без самих a и b)."""
    return [a + (k + 1) * (b - a) / (n + 1) for k in range(n)]


class Layout(object):
    """Положение стержней в сечении (мм, начало — центр; u — вдоль b, v — вдоль h, вверх на рисунке)
    и проверки раскладки. Ось хомута Ø ds отстоит от грани на (защитный слой − ds/2)."""

    def __init__(self, calc, b, h, ds=None):
        so, cv = calc.so, calc.covers
        ds = ds or calc.ds
        self.error = None
        self.notes = []
        clear_min = norms.column_rule("clear_spacing_min")["value"]
        pitch_max = norms.column_rule("long_spacing_max")["value"]
        checks = []       # (шаг между осями, наибольший диаметр соседних стержней)
        if calc.kind == ROUND:
            d, n = so["d"], int(so["n"])
            self.stirrup_r = b / 2.0 - cv["c"] + ds / 2.0
            self.r = b / 2.0 - cv["c"] - d / 2.0
            self.round_bars = [(self.r * math.cos(2 * math.pi * k / n), self.r * math.sin(2 * math.pi * k / n))
                               for k in range(n)]
            if n:
                checks.append((2 * math.pi * self.r / n, d))
            self.count = n
            self.area = n * math.pi * d * d / 4.0
            self.mu = 100.0 * self.area / (math.pi * b * b / 4.0)
            self.tie_side, self.tie_mid = [], []
            if self.r <= 0 or n < 4:
                self.error = u"сечение слишком мало или стержней меньше 4"
        else:
            cs, ct, cb = cv["c_s"], cv["c_t"], cv["c_b"]
            nt, nb, ns = int(so["n_t"]), int(so["n_b"]), int(so["n_s"])
            # Ось хомута: слева/справа, сверху, снизу
            self.st_u = b / 2.0 - cs + ds / 2.0
            self.st_top = h / 2.0 - ct + ds / 2.0
            self.st_bot = -h / 2.0 + cb - ds / 2.0
            # Угловые
            self.ut = b / 2.0 - cs - so["d_ct"] / 2.0
            self.ub = b / 2.0 - cs - so["d_cb"] / 2.0
            self.vt = h / 2.0 - ct - so["d_ct"] / 2.0
            self.vb = -h / 2.0 + cb + so["d_cb"] / 2.0
            # Промежуточные у верхней и нижней граней, боковые
            self.v_mid_t = h / 2.0 - ct - so["d_t"] / 2.0
            self.v_mid_b = -h / 2.0 + cb + so["d_b"] / 2.0
            self.u_top = spread(-self.ut, self.ut, nt)
            self.u_bot = spread(-self.ub, self.ub, nb)
            self.u_side = b / 2.0 - cs - so["d_s"] / 2.0
            self.v_side = spread(self.vb, self.vt, ns)
            checks += [(2 * self.ut / (nt + 1), max(so["d_ct"], so["d_t"] if nt else 0)),
                       (2 * self.ub / (nb + 1), max(so["d_cb"], so["d_b"] if nb else 0)),
                       ((self.vt - self.vb) / (ns + 1), max(so["d_ct"], so["d_cb"], so["d_s"] if ns else 0))]
            # Шпильки: к боковым — поперёк b; верх-низ — поперёк h, если промежуточные стоят друг над другом
            self.tie_side = tied_indices(so["ties"], ns, h, ns + 2)
            self.tie_mid = []
            if nt == nb and nt:
                self.tie_mid = tied_indices(so["ties"], nt, b, nt + 2)
            elif so["ties"] != TIES_NONE and (nt or nb) and norms.column_ties_needed(b, max(nt, nb) + 2):
                self.notes.append(u"шпильки верх-низ не поставлены: разное число промежуточных стержней")
            self.count = 4 + nt + nb + 2 * ns
            q = lambda d: math.pi * d * d / 4.0
            self.area = (2 * q(so["d_ct"]) + 2 * q(so["d_cb"]) + nt * q(so["d_t"]) + nb * q(so["d_b"])
                         + 2 * ns * q(so["d_s"]))
            self.mu = 100.0 * self.area / (b * h)
            if min(self.ut, self.ub, self.vt, -self.vb) <= 0:
                self.error = u"сечение слишком мало для таких защитных слоёв"
            if so["ties"] == TIES_NONE and (norms.column_ties_needed(b, max(nt, nb) + 2)
                                            or norms.column_ties_needed(h, ns + 2)):
                self.notes.append(u"по СП 63 нужны шпильки")
        for pitch, d in checks:
            if pitch - d < max(d, clear_min) and self.error is None:
                self.error = u"стержни не помещаются: расстояние в свету {:.0f} мм < {:.0f} мм".format(
                    float(pitch - d), float(max(d, clear_min)))
            if pitch > pitch_max:
                self.notes.append(u"шаг продольных {:.0f} мм > {} мм".format(float(pitch), pitch_max))


# ==========================================================
# ГЕОМЕТРИЯ СТЕРЖНЕЙ (футы)
# ==========================================================
def long_bar(calc, d, base, inward, z_start, end):
    """Осевые линии продольного стержня снизу вверх. base — точка в плане, inward — направление внутрь.
    end: ("stop", z) — обрыв на отметке z; ("joint", z_top, z_ref, slab) — верх колонны по типу стыка."""
    p = lambda shift, z: DB.XYZ(base.X + inward.X * shift, base.Y + inward.Y * shift, z)
    if end[0] == "stop":
        return kr.polyline([p(0, z_start), p(0, end[1])])
    _, z_top, z_ref, slab = end
    joint = calc.ho["joint"]
    if joint == JOINT_LAP:
        return kr.polyline([p(0, z_start), p(0, z_ref + ft(calc.lap(d)))])
    if joint == JOINT_CRANK:
        off, rise = ft(d), ft(norms.crank_rise(d))
        return kr.polyline([p(0, z_start), p(0, z_ref), p(off, z_ref + rise), p(off, z_ref + rise + ft(calc.lap(d)))])
    z_end = z_ref - ft(calc.cover_end)
    if joint == JOINT_BEND:
        return kr.polyline([p(0, z_start), p(0, z_end), p(ft(bend_leg(calc, d, z_end, z_top, slab)), z_end)])
    return kr.polyline([p(0, z_start), p(0, z_end)])


def bend_leg(calc, d, z_end, z_top, slab):
    """Длина Г-загиба в плиту, мм: добирает анкеровку сверх заделки в плиту, не менее 10d."""
    embed = max(0.0, mm(z_end - (slab[0] if slab is not None else z_top)))
    return norms.ceil_to(max(calc.anchorage(d) - embed, 10 * d))


def stirrup_loop(calc, sec, z, ds):
    """Замкнутый хомут Ø ds на отметке z (обход против часовой стрелки, вид сверху).
    Круглый: замкнутую окружность Revit не принимает (проверено в Revit 2022), поэтому с отгибами —
    дуга 355° (концы закрывают отгибы), без отгибов — кольцо с нахлёстом концов 30–90°."""
    lay = Layout(calc, mm(sec.b), mm(sec.h), ds)
    if sec.is_round:
        r = ft(lay.stirrup_r)
        pt = lambda a: sec.point(r * math.cos(a), r * math.sin(a), z)
        arc = lambda a0, a1: DB.Arc.Create(pt(a0), pt(a1), pt((a0 + a1) / 2.0))
        if calc.ho["hook"]:
            return [arc(0.0, math.radians(355))]
        ho = calc.ho
        lap = norms.lap_length(ds, ho["st_steel"], ho["concrete"], False)
        overlap = min(max(lap / lay.stirrup_r, math.radians(30)), math.radians(90))
        return [arc(0.0, math.pi), arc(math.pi, 2 * math.pi + overlap)]
    u, t, b = ft(lay.st_u), ft(lay.st_top), ft(lay.st_bot)
    corners = [(-u, b), (u, b), (u, t), (-u, t), (-u, b)]
    return kr.polyline([sec.point(x, y, z) for x, y in corners])


def column_levels(prof, slab, build):
    """(отметка для выпусков, конец зоны хомутов) в футах: подрезка по плите только у монолитной."""
    if build == PRECAST or slab is None:
        return prof.z_top, prof.z_top
    return max(prof.z_top, slab[1]), min(prof.z_top, slab[0])


class Run(object):
    """Подряд идущие участки с одинаковым сечением и раскладкой — продольные стержни сквозь них."""

    def __init__(self, first, last, index):
        self.first = first
        self.last = last
        self.index = index      # индекс первого участка в prof.parts (его настройки)


def runs_of(prof, part_opts):
    runs = []
    for i, p in enumerate(prof.parts):
        if p.kind != "shaft":
            continue
        if runs:
            prev = prof.parts[runs[-1].index]
            same = (abs(prev.sec.b - p.sec.b) < ft(2) and abs(prev.sec.h - p.sec.h) < ft(2)
                    and DB.XYZ(prev.sec.center.X - p.sec.center.X, prev.sec.center.Y - p.sec.center.Y, 0).GetLength() < ft(2)
                    and part_opts[runs[-1].index] == part_opts[i])
            if same:
                runs[-1].last = p
                continue
        runs.append(Run(p, p, i))
    return runs


def plan_long(calc, prof, run, is_bottom, is_top, slab, role, specs):
    """Продольные стержни одного прохода (run). Возвращает Layout сечения."""
    sec0 = run.first.sec
    sec = kg.ColumnSection(sec0.center, sec0.ax, sec0.ay, sec0.b, sec0.h, run.first.z0, run.last.z1, sec0.is_round)
    lay = Layout(calc, mm(sec.b), mm(sec.h))
    so, ho = calc.so, calc.ho
    c_end = ft(calc.cover_end)
    d_max = max(calc.diams)
    # Низ: у нижнего прохода — низ колонны (монолит) или защитный слой у торца (сборная);
    # у прохода над консолью/ступенью — заход вниз на длину анкеровки
    if is_bottom:
        z_start = prof.z_bot + (c_end if ho["build"] == PRECAST else 0.0)
    else:
        z_start = max(prof.z_bot + c_end, run.first.z0 - ft(calc.anchorage(d_max)))
    # Верх: у верхнего прохода — по типу стыка (монолит) или защитный слой у торца (сборная);
    # у нижнего прохода — верх зоны консолей над ним (или ступени) минус защитный слой
    if is_top:
        if ho["build"] == PRECAST:
            end = ("stop", prof.z_top - c_end)
        else:
            z_ref, _ = column_levels(prof, slab, ho["build"])
            end = ("joint", prof.z_top, z_ref, slab)
    else:
        idx = prof.parts.index(run.last)
        nxt = prof.parts[idx + 1] if idx + 1 < len(prof.parts) else None
        top = nxt.z1 if nxt is not None and nxt.kind == "corbel" else run.last.z1
        end = ("stop", top - c_end)
        role = "straight"
    if is_top and prof.parts[-1].kind == "corbel" and ho["build"] == MONO and ho["joint"] == JOINT_BEND:
        role = "L"
    prof_line = lambda d, u, v, inward: long_bar(calc, d, sec.point(ft(u), ft(v), z_start), inward, z_start, end)
    z_axis = DB.XYZ.BasisZ
    if calc.kind == ROUND:
        d = so["d"]
        for u, v in lay.round_bars:
            radial = (sec.ax * u + sec.ay * v).Normalize()
            specs.append(kr.BarSpec(role, d, prof_line(d, u, v, radial.Negate()), z_axis.CrossProduct(radial)))
        return lay
    ax, ay = sec.ax, sec.ay
    down, up = ay, ay.Negate()       # направление «внутрь» от нижней и от верхней грани
    specs.append(kr.BarSpec(role, so["d_cb"], prof_line(so["d_cb"], -lay.ub, lay.vb, down), ax,
                            ("fixed", 2, ft(2 * lay.ub)), 2))
    specs.append(kr.BarSpec(role, so["d_ct"], prof_line(so["d_ct"], -lay.ut, lay.vt, up), ax,
                            ("fixed", 2, ft(2 * lay.ut)), 2))
    for us, v, d, inward in ((lay.u_bot, lay.v_mid_b, so["d_b"], down), (lay.u_top, lay.v_mid_t, so["d_t"], up)):
        if us:
            specs.append(kr.BarSpec(role, d, prof_line(d, us[0], v, inward), ax,
                                    ("fixed", len(us), ft(us[-1] - us[0])) if len(us) > 1 else None, len(us)))
    if lay.v_side:
        n, d = len(lay.v_side), so["d_s"]
        for u, inward in ((-lay.u_side, ax), (lay.u_side, ax.Negate())):
            specs.append(kr.BarSpec(role, d, prof_line(d, u, lay.v_side[0], inward), ay,
                                    ("fixed", n, ft(lay.v_side[-1] - lay.v_side[0])) if n > 1 else None, n))
    return lay


def plan_stirrups(calc_of, prof, zones_mm, specs):
    """Хомуты и шпильки по зонам, с разбиением по участкам; в зонах консолей хомуты колонны не ставятся
    (их заменяют хомуты консоли). calc_of(индекс участка) -> Calc. Возвращает (число хомутов, число шпилек)."""
    n_st = n_ties = 0
    z_axis = DB.XYZ.BasisZ
    holes = [(mm(p.z0 - prof.z_bot), mm(p.z1 - prof.z_bot)) for p in prof.parts if p.kind == "corbel" and p.corbels]
    for z, length, step, d, first, last in zones_mm:
        for i, part in enumerate(prof.parts):
            if part.kind != "shaft":
                continue
            a0, b0 = mm(part.z0 - prof.z_bot), mm(part.z1 - prof.z_bot)
            for a, b in kr.cut_intervals([(max(z, a0), min(z + length, b0))], holes):
                # Сетка зоны сохраняется: первый хомут куска — на ближайшем узле сетки зоны
                k0 = int(math.ceil((a - z) / step - 1e-6))
                if not first and k0 == 0:
                    k0 = 1
                za = z + k0 * step
                if za > b + 1e-6:
                    continue
                count = int(math.floor((b - za) / step + 1e-6)) + 1
                calc = calc_of(i)
                zf = prof.z_bot + ft(za)
                layout = ("spacing", ft(step), ft((count - 1) * step), True, True) if count > 1 else None
                specs.append(kr.BarSpec("stirrup", d, stirrup_loop(calc, part.sec, zf, d), z_axis, layout, count))
                n_st += count
                if calc.kind == RECT:
                    n_ties += plan_ties(calc, part.sec, zf, d, layout, count, specs)
    return n_st, n_ties


def plan_ties(calc, sec, z, ds, layout, count, specs):
    """Шпильки над хомутом: от наружной стороны одного стержня до наружной стороны противоположного."""
    so = calc.so
    lay = Layout(calc, mm(sec.b), mm(sec.h), ds)
    dt = so["dt"]
    zt = z + ft((ds + dt) / 2.0)
    n = 0
    ext_s = (so["d_s"] + dt) / 2.0
    for i in lay.tie_side:
        a = sec.point(ft(-lay.u_side - ext_s), ft(lay.v_side[i]), zt)
        b = sec.point(ft(lay.u_side + ext_s), ft(lay.v_side[i]), zt)
        specs.append(kr.BarSpec("pin", dt, kr.polyline([a, b]), DB.XYZ.BasisZ, layout, count))
        n += count
    for i in lay.tie_mid:
        u = (lay.u_top[i] + lay.u_bot[i]) / 2.0
        a = sec.point(ft(u), ft(lay.v_mid_b - (so["d_b"] + dt) / 2.0), zt)
        b = sec.point(ft(u), ft(lay.v_mid_t + (so["d_t"] + dt) / 2.0), zt)
        specs.append(kr.BarSpec("pin", dt, kr.polyline([a, b]), DB.XYZ.BasisZ, layout, count))
        n += count
    return n


# ----------------------------------------------------------
# КОНСОЛИ
# ----------------------------------------------------------
class CorbelCalc(object):
    """Величины армирования зоны консолей: co — настройки консоли, ho — общие настройки."""

    def __init__(self, ho, co, part):
        self.ho, self.co, self.part = ho, co, part
        cond = ho["cover_cond"]
        self.cover_auto = norms.ceil_to(max(norms.cover_main(cond, co["cd"]),
                                            norms.cover_constructive(cond, co["cds"]) + co["cds"]), 5.0)
        self.c = co["cc"] or self.cover_auto
        self.h = mm(part.z1 - part.z0)
        self.step = co["cstep"] or norms.floor_to(norms.corbel_stirrup_spacing(self.h), 25.0)
        self.anchorage = norms.anchorage_length(co["cd"], ho["steel"], ho["concrete"], False)
        self.warnings = []
        if self.step > norms.corbel_stirrup_spacing(self.h):
            self.warnings.append(u"шаг хомутов консоли {:.0f} мм больше {:.0f} мм".format(
                float(self.step), float(norms.corbel_stirrup_spacing(self.h))))


def corbel_frame(prof, part):
    """Габарит участка под зоной консолей в плане: ((u0, u1), (v0, v1)), футы от оси колонны."""
    sec = part.below.sec
    cu = (sec.center - prof.origin).DotProduct(prof.ax)
    cv = (sec.center - prof.origin).DotProduct(prof.ay)
    return (cu - sec.b / 2.0, cu + sec.b / 2.0), (cv - sec.h / 2.0, cv + sec.h / 2.0)


def plan_corbels(prof, part, cc, specs):
    """Рабочие стержни поверху консолей и горизонтальные хомуты зоны консолей.
    Две консоли по одной оси — общие стержни от торца до торца (П); одна — от торца до дальней грани
    колонны (Г), если анкеровки не хватает — с загибом вниз у дальней грани. Возвращает (стержней, хомутов)."""
    co = cc.co
    (u0, u1), (v0, v1) = corbel_frame(prof, part)
    c, d, ds = ft(cc.c), co["cd"], co["cds"]
    o, z_axis = prof.origin, DB.XYZ.BasisZ
    n_top = n_st = 0
    for axis in (0, 1):
        ks = [k for k in part.corbels if (k.side[0] != 0) == (axis == 0)]
        if not ks:
            continue
        lo_k = next((k for k in ks if -1 in k.side), None)
        hi_k = next((k for k in ks if 1 in k.side), None)
        a0, a1 = (u0, u1) if axis == 0 else (v0, v1)       # габарит участка вдоль оси консоли
        w0, w1 = (v0, v1) if axis == 0 else (u0, u1)       # поперёк
        if axis == 0:
            to_xyz = lambda a, w, z: o + prof.ax * a + prof.ay * w + DB.XYZ(0, 0, z)
        else:
            to_xyz = lambda a, w, z: o + prof.ax * w + prof.ay * a + DB.XYZ(0, 0, z)
        zb = part.z1 - c - ft(d) / 2.0
        r = ft(d) / 2.0

        def tip_leg(k):
            room = (k.z_top - k.z_tip) - 2 * c
            return max(0.0, min(max(ft(10 * d), room), room))

        start = (lo_k.tip + c + r) if lo_k else (a0 + c + r)
        end = (hi_k.tip - c - r) if hi_k else (a1 - c - r)
        far_leg = 0.0
        if (lo_k is None) != (hi_k is None):
            k = lo_k or hi_k
            straight_in = abs((end if lo_k else start) - k.face)
            if straight_in < ft(cc.anchorage):
                far_leg = max(ft(cc.anchorage) - straight_in, ft(10 * d))
                far_leg = min(far_leg, part.z1 - part.below.z0 - 2 * c)
        pts = []
        if lo_k:
            pts.append((start, zb - tip_leg(lo_k)))
        elif far_leg:
            pts.append((start, zb - far_leg))
        pts += [(start, zb), (end, zb)]
        if hi_k:
            pts.append((end, zb - tip_leg(hi_k)))
        elif far_leg:
            pts.append((end, zb - far_leg))
        w_first = w0 + c + r
        n = int(co["cn"])
        span = (w1 - c - r) - w_first
        role = {0: "straight", 1: "L", 2: "U"}[min(len(pts) - 2, 2)]
        norm = prof.ay if axis == 0 else prof.ax
        specs.append(kr.BarSpec(role, d, kr.polyline([to_xyz(a, w_first, z) for a, z in pts]), norm,
                                ("fixed", n, span) if n > 1 else None, n))
        n_top += n
        # Горизонтальные хомуты от низа зоны до верха: обходят консоль(и) и сечение колонны
        z = part.z0 + c
        while z <= part.z1 - c + 1e-6:
            ea = (lo_k.face - lo_k.reach_at(z) if lo_k else a0) + c - ft(ds) / 2.0
            eb = (hi_k.face + hi_k.reach_at(z) if hi_k else a1) - c + ft(ds) / 2.0
            fa, fb = w0 + c - ft(ds) / 2.0, w1 - c + ft(ds) / 2.0
            corners = [(ea, fa), (eb, fa), (eb, fb), (ea, fb), (ea, fa)]
            if axis == 1:
                corners = [(w, a) for a, w in reversed(corners)]     # сохранить обход против часовой стрелки
            to_plan = (lambda p, z=z: o + prof.ax * p[0] + prof.ay * p[1] + DB.XYZ(0, 0, z)) if axis == 0 else \
                (lambda p, z=z: o + prof.ax * p[0] + prof.ay * p[1] + DB.XYZ(0, 0, z))
            specs.append(kr.BarSpec("stirrup", ds, kr.polyline([to_plan(p) for p in corners]), z_axis))
            n_st += 1
            z += ft(cc.step)
    return n_top, n_st


# ----------------------------------------------------------
# ПЛАН КОЛОННЫ
# ----------------------------------------------------------
def plan_column(ho, group, index):
    """(план, None) или (None, причина пропуска). План: колонна, список BarSpec, подпись."""
    col, prof = group.items[index]
    slab = group.slab(index) if (ho["cut_by_slab"] and ho["build"] == MONO) else None
    zones = group.zones
    calcs = {}

    def calc_of(i):
        if i not in calcs:
            calcs[i] = Calc(ho, group.part_opts[i], part_kind(prof.parts[i]), zones)
        return calcs[i]

    specs, notes = [], []
    runs = runs_of(prof, group.part_opts)
    role = JOINT_ROLE[ho["joint"]] if ho["build"] == MONO else "straight"
    n_long = 0
    for k, run in enumerate(runs):
        lay = plan_long(calc_of(run.index), prof, run, k == 0, k == len(runs) - 1, slab, role, specs)
        if lay.error:
            return None, u"{}: {}".format(part_title(prof, run.index), lay.error)
        notes += lay.notes
        n_long += lay.count

    calc0 = calc_of(runs[0].index)
    _, z_st_end = column_levels(prof, slab, ho["build"])
    z0 = calc0.first_offset
    z3 = mm(z_st_end - prof.z_bot) - calc0.top_offset
    if z3 - z0 < 100:
        return None, u"колонна слишком короткая для хомутов"
    n_st, n_ties = plan_stirrups(calc_of, prof, kr.zone_layout(zones, z0, z3), specs)

    n_cb = n_cbs = 0
    for i, part in enumerate(prof.parts):
        if part.kind == "corbel" and part.corbels:
            a, b = plan_corbels(prof, part, CorbelCalc(ho, group.part_opts[i], part), specs)
            n_cb += a
            n_cbs += b

    summary = u"{}: {} стерж., хомуты ×{}{}{}, {}".format(
        column_title(col), n_long, n_st, u", шпильки ×{}".format(n_ties) if n_ties else u"",
        u", консоли: стержни ×{}, хомуты ×{}".format(n_cb, n_cbs) if n_cb else u"",
        u"сборная" if ho["build"] == PRECAST else JOINT_TEXT[ho["joint"]] +
        (u", подрезка по плите" if slab is not None else u""))
    return {"host": col, "title": column_title(col), "specs": specs, "summary": summary, "notes": notes}, None


# ==========================================================
# СОЗДАНИЕ АРМАТУРЫ
# ==========================================================
WHAT = {"many": u"колонн", "title": u"Армирование колонн", "done": u"Армировано колонн",
        "failed": u"Не армирована", "armed": u"уже армирована"}


def reinforce(groups, ho, skipped):
    plans, warnings, skipped = [], [], list(skipped)
    for g in groups:
        for i, part in enumerate(g.prof0.parts):
            if part.kind == "shaft":
                c = Calc(ho, g.part_opts[i], part_kind(part), g.zones)
            elif part.corbels:
                c = CorbelCalc(ho, g.part_opts[i], part)
            else:
                continue
            warnings.extend(u"{}, {}: {}".format(g.Name, part_title(g.prof0, i), w) for w in c.warnings)
        for i in range(len(g.items)):
            try:
                plan, reason = plan_column(ho, g, i)
            except Exception as ex:
                plan, reason = None, u"ошибка расчёта: {}".format(ex)
            if plan is None:
                skipped.append(u"{} — {}".format(column_title(g.items[i][0]), reason))
            else:
                plans.append(plan)
    hooks = {"stirrup": runner.find_hook(doc, ho["hook"]), "pin": runner.find_hook(doc, ho["tie_hook"])}
    runner.run(doc, plans, skipped, warnings, ho["steel"], ho["st_steel"], hooks, PART_NAME, WHAT)


# ==========================================================
# ПРЕДПРОСМОТР: СЕЧЕНИЕ УЧАСТКА, ПРОФИЛЬ КОНСОЛИ, ВИД ПО ВЫСОТЕ
# ==========================================================
def draw_section(canvas, calc, sec, pad):
    """Рисунок сечения участка в области холста за вычетом pad (место под поля редактора).
    Возвращает (Layout, {ключ группы полей: точка выноски в px})."""
    so = calc.so
    b, h = mm(sec.b), mm(sec.h)
    sk = kd.Sketch(canvas, (-b / 2.0, -h / 2.0, b / 2.0, h / 2.0), pad=pad)
    lay = Layout(calc, b, h)
    P = lambda u, v: (sk.X(u), sk.Y(v))
    if sec.is_round:
        circle = lambda r: [(r * math.cos(a * math.pi / 36), r * math.sin(a * math.pi / 36)) for a in range(72)]
        sk.poly(circle(b / 2.0), kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
        sk.poly(circle(lay.stirrup_r), kd.STIRRUP, max(2.0, calc.ds * sk.k), closed=True)
        for u, v in lay.round_bars:
            sk.circle(u, v, so["d"] / 2.0, kd.CORNER)
        sk.dim(-b / 2.0, b / 2.0, b / 2.0, b / 2.0, u"⌀{:.0f}".format(float(b)))
        a = math.pi / 4
        c = calc.covers["c"]
        sk.dim_line((b / 2.0 - c) * math.cos(-a), (b / 2.0 - c) * math.sin(-a),
                    b / 2.0 * math.cos(-a), b / 2.0 * math.sin(-a))
        sk.text(b / 2.0 * math.cos(-a), b / 2.0 * math.sin(-a), u"c = {:.0f}".format(float(c)), size=10.5, align="left", dx=6, dy=6)
        targets = {
            "bars": P(-lay.r * math.cos(a), lay.r * math.sin(a)),
            "stirrup": P(lay.stirrup_r, 0.0),
            "cover": P(-(b / 2.0 - c / 2.0) * math.cos(-a), (b / 2.0 - c / 2.0) * math.sin(-a)),
        }
        return lay, targets

    cv = calc.covers
    sk.rect(-b / 2.0, -h / 2.0, b / 2.0, h / 2.0, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE)
    sk.rect(-lay.st_u, lay.st_bot, lay.st_u, lay.st_top, kd.STIRRUP, max(2.0, calc.ds * sk.k))
    # Шпильки — как в модели: от наружной стороны одного стержня до наружной стороны противоположного
    tie_w = max(1.5, so["dt"] * sk.k)
    ext_s = (so["d_s"] + so["dt"]) / 2.0
    for i in lay.tie_side:
        sk.line(-lay.u_side - ext_s, lay.v_side[i], lay.u_side + ext_s, lay.v_side[i], kd.STIRRUP, tie_w)
    for i in lay.tie_mid:
        u = (lay.u_top[i] + lay.u_bot[i]) / 2.0
        sk.line(u, lay.v_mid_b - (so["d_b"] + so["dt"]) / 2.0, u, lay.v_mid_t + (so["d_t"] + so["dt"]) / 2.0,
                kd.STIRRUP, tie_w)
    for su in (-1, 1):
        sk.circle(su * lay.ut, lay.vt, so["d_ct"] / 2.0, kd.CORNER)
        sk.circle(su * lay.ub, lay.vb, so["d_cb"] / 2.0, kd.CORNER)
        for v in lay.v_side:
            sk.circle(su * lay.u_side, v, so["d_s"] / 2.0, kd.SECOND)
    for u in lay.u_top:
        sk.circle(u, lay.v_mid_t, so["d_t"] / 2.0, kd.SECOND)
    for u in lay.u_bot:
        sk.circle(u, lay.v_mid_b, so["d_b"] / 2.0, kd.SECOND)
    sk.dim(-b / 2.0, h / 2.0, b / 2.0, h / 2.0, u"b = {:.0f}".format(float(b)))
    sk.dim(b / 2.0, -h / 2.0, b / 2.0, h / 2.0, u"h = {:.0f}".format(float(h)), outside="right")
    # Защитные слои — короткие размеры с засечками от грани до поверхности продольного стержня
    xs, yt, yb = -b / 2.0 + cv["c_s"], h / 2.0 - cv["c_t"], -h / 2.0 + cv["c_b"]
    sk.cover_dim(-b / 2.0, yt, xs, yt, u"c = {:.0f}".format(float(cv["c_s"])), left=True)
    sk.cover_dim(xs, h / 2.0, xs, yt, u"c = {:.0f}".format(float(cv["c_t"])))
    sk.cover_dim(b / 2.0 - cv["c_s"], -h / 2.0, b / 2.0 - cv["c_s"], yb, u"c = {:.0f}".format(float(cv["c_b"])))
    targets = {
        "cover_s": P((-b / 2.0 + xs) / 2.0, yt),
        "cover_t": P(xs, (h / 2.0 + yt) / 2.0),
        # Выноски карточек — к ближней к ним стороне сечения: карточки стержней слева, хомутов и шпилек справа
        "corner_top": P(-lay.ub, lay.vb),
        "mid_top": P(-lay.u_side, lay.v_side[len(lay.v_side) // 2]) if lay.v_side else (
            P(lay.u_bot[0], lay.v_mid_b) if lay.u_bot else P(-lay.ub / 2.0, lay.v_mid_b)),
        "side": P(lay.u_side, lay.v_side[len(lay.v_side) // 2]) if lay.v_side else P(lay.u_side, 0.0),
        "cover_b": P(b / 2.0 - cv["c_s"], (-h / 2.0 + yb) / 2.0),
        "stirrup": P(lay.st_u, (lay.st_top + lay.st_bot) / 2.0),
        "mid_bot": P(lay.u_bot[0], lay.v_mid_b) if lay.u_bot else P(-lay.ub / 2.0, lay.v_mid_b),
        "corner_bot": P(-lay.ub, lay.vb),
        "ties": tie_target(lay, P),
    }
    return lay, targets


def tie_target(lay, P):
    """Точка выноски поля шпилек: на нижнюю шпильку верх-низ или на нижнюю шпильку к боковым; нет шпилек — None."""
    if lay.tie_mid:
        i = lay.tie_mid[0]
        u = (lay.u_top[i] + lay.u_bot[i]) / 2.0
        return P(u, lay.v_mid_b + (lay.v_mid_t - lay.v_mid_b) * 0.2)
    if lay.tie_side:
        return P(lay.u_side * 0.5, lay.v_side[lay.tie_side[0]])
    return None


def ties_text(so, lay):
    """Пояснение под полем шпилек: сколько и какие стержни связывают."""
    parts = []
    if lay.tie_side:
        parts.append(u"к боковым — {} шт. (поперёк b)".format(len(lay.tie_side)))
    if lay.tie_mid:
        parts.append(u"верх–низ — {} шт. (поперёк h)".format(len(lay.tie_mid)))
    if parts:
        return u"; ".join(parts) + u"; на каждом хомуте"
    if so["ties"] == TIES_NONE:
        return u"шпильки не ставятся"
    if so["ties"] == TIES_AUTO:
        return u"по СП 63 не нужны: грани ≤ 400 мм и у грани ≤ 4 стержней"
    return u"нет промежуточных стержней — шпильки не к чему ставить"


def outline(prof):
    """Контур колонны на виде вдоль оси Y: [(u, z)] мм, z — от низа колонны (слева вверх, справа вниз)."""
    left, right = [], []
    for p in prof.parts:
        if p.kind == "shaft":
            cu = (p.sec.center - prof.origin).DotProduct(prof.ax)
            for z in (p.z0, p.z1):
                left.append((mm(cu - p.sec.b / 2.0), mm(z - prof.z_bot)))
                right.append((mm(cu + p.sec.b / 2.0), mm(z - prof.z_bot)))
            continue
        (u0, u1), _ = corbel_frame(prof, p)
        lo_k = next((k for k in p.corbels if k.side == (-1, 0)), None)
        hi_k = next((k for k in p.corbels if k.side == (1, 0)), None)
        n = 16
        for j in range(n + 1):
            z = p.z0 + (p.z1 - p.z0) * j / float(n)
            left.append((mm(lo_k.face - lo_k.reach_at(z)) if lo_k else mm(u0), mm(z - prof.z_bot)))
            right.append((mm(hi_k.face + hi_k.reach_at(z)) if hi_k else mm(u1), mm(z - prof.z_bot)))
    return left + list(reversed(right))


def polyline_mm(prof, curves):
    """Осевые линии стержня -> [(u, z)] мм на виде вдоль оси Y."""
    rel = lambda p: (mm((p - prof.origin).DotProduct(prof.ax)), mm(p.Z - prof.z_bot))
    return [rel(c.GetEndPoint(0)) for c in curves] + [rel(curves[-1].GetEndPoint(1))]


def draw_corbel(canvas, cc, prof, part, pad):
    """Профиль зоны консолей (вид вдоль оси Y): бетон, рабочие стержни поверху, горизонтальные хомуты.
    Возвращает цели выносок полей консоли."""
    co = cc.co
    pts = outline(prof)
    z_lo, z_hi = mm(part.z0 - prof.z_bot) - 400, mm(part.z1 - prof.z_bot) + 300
    us = [u for u, z in pts]
    sk = kd.Sketch(canvas, (min(us) - 50, z_lo, max(us) + 50, z_hi), pad=pad)
    clip = [(u, max(z_lo, min(z, z_hi))) for u, z in pts]
    sk.poly(clip, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
    specs = []
    plan_corbels(prof, part, cc, specs)
    top_pt = loop_pt = None
    for sp in specs:
        pl = polyline_mm(prof, sp.curves)
        if sp.role == "stirrup":
            ua, ub = min(u for u, z in pl), max(u for u, z in pl)
            if ub - ua < 1:      # консоль поперёк вида: хомут виден торцом
                continue
            sk.line(ua, pl[0][1], ub, pl[0][1], kd.STIRRUP, max(1.5, co["cds"] * sk.k))
            if loop_pt is None:
                loop_pt = (sk.X(ua + 0.1 * (ub - ua)), sk.Y(pl[0][1]))
        else:
            sk.poly(pl, kd.MAIN, max(1.8, co["cd"] * sk.k))
            top_pt = (sk.X((pl[1][0] + pl[2][0]) / 2.0 if len(pl) > 2 else pl[0][0]), sk.Y(pl[1][1]))
    zt = mm(part.z1 - prof.z_bot)
    zb = mm(part.z0 - prof.z_bot)
    sk.dim(max(us), zb, max(us), zt, u"h = {:.0f}".format(float(zt - zb)), outside="right")
    sk.cover_dim(max(us) - cc.c, zt, max(us) - cc.c, zt - cc.c, u"c = {:.0f}".format(float(cc.c)))
    return {"c_top": top_pt, "c_stir": loop_pt, "c_cover": (sk.X(max(us) - cc.c), sk.Y(zt - cc.c / 2.0))}


def draw_elevation(canvas, ho, group, calc_of, pad):
    """Вид колонны по высоте (вдоль оси Y): контур, плита, крайние стержни, хомуты по зонам, консоли.
    Возвращает (цели выносок полей, фактические зоны [(начало, длина, шаг, Ø, число)])."""
    col, prof = group.items[0]
    slab = group.slab(0) if (ho["cut_by_slab"] and ho["build"] == MONO) else None
    z_ref, z_st_end = column_levels(prof, slab, ho["build"])
    H = mm(prof.z_top - prof.z_bot)
    pts = outline(prof)
    us = [u for u, z in pts]
    runs = runs_of(prof, group.part_opts)
    calc0 = calc_of(group, runs[0].index)
    d_max = max(calc0.diams)
    top = mm(z_ref - prof.z_bot)
    if ho["build"] == MONO and ho["joint"] in (JOINT_LAP, JOINT_CRANK):
        top += calc0.lap(d_max) + norms.crank_rise(d_max)
    sk = kd.Sketch(canvas, (min(us) - 150, -100.0, max(us) + 150, top + 100.0), pad=pad, min_width_px=170)
    if slab is not None:
        sk.rect(min(us) - 300, mm(slab[0] - prof.z_bot), max(us) + 300, mm(slab[1] - prof.z_bot),
                kd.CONCRETE_EDGE, 1.0, kd.SLAB, dash=True)
    sk.poly(pts, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)

    # Продольные стержни: по проходам; на виде — крайние слева и справа
    role = JOINT_ROLE[ho["joint"]] if ho["build"] == MONO else "straight"
    long_lines = []
    for k, run in enumerate(runs):
        specs = []
        plan_long(calc_of(group, run.index), prof, run, k == 0, k == len(runs) - 1, slab, role, specs)
        lines = [polyline_mm(prof, sp.curves) for sp in specs]
        if lines:
            u_lo = min(pl[0][0] for pl in lines)
            u_hi = max(pl[0][0] for pl in lines)
            for pl in lines:
                if abs(pl[0][0] - u_lo) < 1 or abs(pl[0][0] - u_hi) < 1:
                    sk.poly(pl, kd.MAIN, 1.8)
                    long_lines.append(pl)

    z0 = calc0.first_offset
    z3 = mm(z_st_end - prof.z_bot) - calc0.top_offset
    zones = kr.zone_layout(group.zones, z0, z3)
    st_specs = []
    plan_stirrups(lambda i: calc_of(group, i), prof, zones, st_specs)
    for sp in st_specs:
        if sp.role != "stirrup":
            continue
        pl = polyline_mm(prof, sp.curves)
        ua, ub = min(u for u, z in pl), max(u for u, z in pl)
        step = mm(sp.layout[1]) if sp.layout else 0.0
        for j in range(sp.count):
            sk.line(ua, pl[0][1] + j * step, ub, pl[0][1] + j * step, kd.STIRRUP, 1.0)
    for i, part in enumerate(prof.parts):
        if part.kind == "corbel" and part.corbels:
            cs = []
            plan_corbels(prof, part, CorbelCalc(ho, group.part_opts[i], part), cs)
            for sp in cs:
                pl = polyline_mm(prof, sp.curves)
                if sp.role == "stirrup":
                    sk.line(min(u for u, z in pl), pl[0][1], max(u for u, z in pl), pl[0][1], kd.STIRRUP, 1.0)
                else:
                    sk.poly(pl, kd.MAIN, 1.8)

    # Цепочка размеров зон справа, общий размер слева
    x_chain = max(us) + 70
    targets, facts = {}, []
    for z, length, step, d, first, last in zones:
        sk.dim_line(x_chain, z, x_chain, z + length)
        facts.append((z, length, step, d, kr.zone_count(length, step, first, last)))
    for i in range(len(group.zones)):
        if i < len(zones):
            z, length = zones[i][0], zones[i][1]
            targets[u"zone_{}".format(i)] = (sk.X(x_chain), sk.Y(z + length / 2.0))
        else:
            targets[u"zone_{}".format(i)] = (sk.X(x_chain), sk.Y(z3), False)
    targets["zone_add"] = (sk.X(x_chain), sk.Y(z3) - 40, False)
    sk.dim(min(us), 0, min(us), H, u"{:.0f}".format(float(H)))
    # Отступы хомутов — выноски к первому и последнему хомуту у левой грани участка
    face = lambda part: mm((part.sec.center - prof.origin).DotProduct(prof.ax) - part.sec.b / 2.0)
    shafts = prof.shafts
    targets["first"] = (sk.X(face(shafts[0])), sk.Y(z0))
    targets["topoff"] = (sk.X(face(shafts[-1])), sk.Y(z3))
    targets["lap"] = None
    if ho["build"] == MONO and ho["joint"] in (JOINT_LAP, JOINT_CRANK) and long_lines:
        top_pl = max(long_lines, key=lambda pl: pl[-1][1])
        targets["lap"] = (sk.X(top_pl[-1][0]), sk.Y(top_pl[-1][1] - calc0.lap(d_max) / 2.0))
    return targets, facts


# ==========================================================
# ОКНО НАСТРОЕК
# ==========================================================
XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Армирование колонн" Width="1480" Height="900" MinWidth="1100" MinHeight="700"
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
                            <TextBlock Grid.Row="0" Text="КЖ • КОЛОННЫ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navSection" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Сечение"/>
                                <RadioButton Name="navHeight" GroupName="nav" Style="{StaticResource NavItem}" Content="По высоте"/>
                                <RadioButton Name="navParams" GroupName="nav" Style="{StaticResource NavItem}" Content="Параметры"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="КОЛОННЫ / ГРУППЫ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
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

                        <!-- Группа колонн — общая для обоих разделов -->
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
                        <Grid Name="pageSection" Grid.Row="1" Visibility="Visible">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                            </Grid.RowDefinitions>
                            <ComboBox Name="cbPart" Style="{StaticResource PillComboBox}" Tag="Участок:" Margin="0,0,0,10"/>
                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="10">
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
                        </Grid>

                        <!-- РАЗДЕЛ 2: ПО ВЫСОТЕ -->
                        <Grid Name="pageHeight" Grid.Row="1" Visibility="Collapsed">
                            <Border Style="{StaticResource GlassCard}" Padding="10">
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
                        </Grid>


                        <!-- РАЗДЕЛ 3: ПАРАМЕТРЫ -->
                        <ScrollViewer Name="pageParams" Grid.Row="1" Visibility="Collapsed" VerticalScrollBarVisibility="Auto">
                                <WrapPanel Margin="0,0,6,0" ItemWidth="430">
                                    <Border Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                        <StackPanel>
                                            <TextBlock Text="Колонна" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                            <WrapPanel>
                                                <RadioButton Name="rbMono" GroupName="build" Style="{StaticResource ModePill}" Content="Монолитная" Margin="0,0,8,6"/>
                                                <RadioButton Name="rbPrecast" GroupName="build" Style="{StaticResource ModePill}" Content="Сборная" Margin="0,0,8,6"/>
                                            </WrapPanel>
                                            <TextBlock Style="{StaticResource Hint}" Text="Сборная: без выпусков и подрезки по плите, стержни обрываются у обоих торцов с защитным слоем."/>
                                        </StackPanel>
                                    </Border>
                                    <Border Name="bdJoint" Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,10,10">
                                        <StackPanel>
                                            <TextBlock Text="Верх стержней" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                            <WrapPanel>
                                                <RadioButton Name="rbLap" GroupName="joint" Style="{StaticResource ModePill}" Content="Прямой выпуск" Margin="0,0,8,6"/>
                                                <RadioButton Name="rbCrank" GroupName="joint" Style="{StaticResource ModePill}" Content="С отгибом" Margin="0,0,8,6"/>
                                                <RadioButton Name="rbBend" GroupName="joint" Style="{StaticResource ModePill}" Content="Загиб в плиту" Margin="0,0,8,6"/>
                                                <RadioButton Name="rbFlush" GroupName="joint" Style="{StaticResource ModePill}" Content="Без выпуска" Margin="0,0,8,6"/>
                                            </WrapPanel>
                                            <ComboBox Name="cbStress" Style="{StaticResource PillComboBox}" Tag="Стык:" Margin="0,2,0,8"/>
                                            <CheckBox Name="chkSlab" Style="{StaticResource PillCheck}" Content="Подрезка по плите над колонной"/>
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
                                            <TextBlock Text="Отгибы" FontSize="16" FontWeight="Bold" Margin="0,0,0,8"/>
                                            <ComboBox Name="cbHook" Style="{StaticResource PillComboBox}" Tag="Хомуты:" Margin="0,0,0,6"/>
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
    "tbCount", "tbDoc", "cbGroup", "btnSkipped", "cbPart", "cnvSection", "cnvFields", "tbSectionInfo",
    "cnvElev", "cnvElevFields", "tbElevInfo", "rbMono", "rbPrecast", "bdJoint", "rbLap", "rbCrank", "rbBend",
    "rbFlush", "cbStress", "chkSlab", "cbSteel", "cbStSteel", "cbConcrete", "cbCover", "cbHook", "cbTieHook",
    "tbInfo", "btnRun",
)
NAV_PAGES = (("navSection", "pageSection"), ("navHeight", "pageHeight"), ("navParams", "pageParams"))

# Поля редактора сечения: (группа, сторона, части) — см. kzh.section_editor.
# Хомут Ø и S — основная зона хомутов (с пустой длиной на виде по высоте)
COVER_NOTE = u"От грани бетона до поверхности продольного стержня. Пусто — по СП 63 для условий из «Параметров»."
# Раздел «Сечение»: слева — продольные стержни и защитные слои, справа — хомуты и шпильки.
# «Верх», «низ», «боковые» — стороны по рисунку сечения: верх/низ — грани b (по горизонтали), боковые — грани h.
SECTION_FIELDS = [
    ("corner_top", "L", {"title": u"Угловые стержни",
                         "note": u"Стержни в углах хомута. Верхние и нижние — по рисунку сечения.",
                         "rows": [[u"Верхние Ø", ("diam", "d_ct")], [u"Нижние Ø", ("diam", "d_cb")]]}),
    ("mid_top", "L", {"title": u"Промежуточные стержни",
                      "note": u"Между угловыми — сколько стержней у каждой грани, не считая угловых. Верхняя и нижняя "
                              u"грани — стороны b, боковые — стороны h.",
                      "rows": [[u"Верхняя грань", ("num", "n_t", 40), u"шт. Ø", ("diam", "d_t")],
                               [u"Нижняя грань", ("num", "n_b", 40), u"шт. Ø", ("diam", "d_b")],
                               [u"Боковые грани", ("num", "n_s", 40), u"шт. Ø", ("diam", "d_s")]]}),
    ("cover_t", "L", {"title": u"Защитные слои", "note": COVER_NOTE,
                      "rows": [[u"Сверху", ("num", "c_t", 46), u"мм"], [("hint", "c_t")],
                               [u"Снизу", ("num", "c_b", 46), u"мм"], [("hint", "c_b")],
                               [u"По бокам", ("num", "c_s", 46), u"мм"], [("hint", "c_s")]]}),
    ("stirrup", "R", {"title": u"Хомуты",
                      "note": u"Ø и шаг основной зоны. Зоны с другим шагом (учащение у узлов) — в разделе «По высоте».",
                      "rows": [[u"Ø", ("diam", "ds"), u"шаг", ("num", "step", 52), u"мм"], [("hint", "step")]]}),
    ("ties", "R", {"title": u"Шпильки",
                   "note": u"Закрепляют промежуточные стержни: по СП 63 — при грани шире 400 мм или более 4 стержней у грани.",
                   "rows": [[("combo", "ties", [Option(t, k) for k, t in TIES_OPTIONS], 220)], [u"Ø", ("diam", "dt")],
                            [("hint", "ties")]]}),
    # Круглая колонна
    ("bars", "L", {"title": u"Продольные стержни", "note": u"Равномерно по окружности.",
                   "rows": [[u"Количество", ("num", "n", 40), u"шт. Ø", ("diam", "d")]]}),
    ("cover", "L", {"title": u"Защитный слой", "note": COVER_NOTE, "rows": [[u"c", ("num", "c", 46), u"мм"], [("hint", "c")]]}),
    # Консоль
    ("c_top", "R", {"title": u"Рабочие стержни консоли", "note": u"Поверху консоли, с загибом у торца.",
                    "rows": [[u"Количество", ("num", "cn", 40), u"шт. Ø", ("diam", "cd")]]}),
    ("c_stir", "L", {"title": u"Хомуты консоли", "note": u"Горизонтальные хомуты по высоте консоли.",
                     "rows": [[u"Ø", ("diam", "cds"), u"шаг", ("num", "cstep", 52), u"мм"], [("hint", "cstep")]]}),
    ("c_cover", "R", {"title": u"Защитный слой консоли", "note": COVER_NOTE,
                      "rows": [[u"c", ("num", "cc", 46), u"мм"], [("hint", "cc")]]}),
]
VISIBLE_GROUPS = {
    RECT: ("corner_top", "mid_top", "cover_t", "stirrup", "ties"),
    ROUND: ("bars", "stirrup", "cover"),
    "corbel": ("c_top", "c_stir", "c_cover"),
}
# Поля на виде по высоте (кроме строк зон)
ELEV_FIELDS = [
    ("first", "L", {"title": u"Первый хомут снизу", "note": u"Расстояние от низа колонны до первого хомута.",
                    "rows": [[u"Отступ", ("num", "first_offset", 46), u"мм"], [("hint", "first_offset")]]}),
    ("topoff", "L", {"title": u"Последний хомут сверху", "note": u"Расстояние от верхнего хомута до верха колонны.",
                     "rows": [[u"Отступ", ("num", "top_offset", 46), u"мм"], [("hint", "top_offset")]]}),
    ("lap", "L", {"title": u"Выпуски вверх", "note": u"Длина нахлёста выпусков со стержнями колонны выше.",
                  "rows": [[u"Нахлёст", ("num", "lap", 52), u"мм"], [("hint", "lap")]]}),
]


class PartItem(object):
    """Пункт списка участков (PillComboBox показывает Name)."""

    def __init__(self, name, index):
        self.Name = name
        self.index = index


class ColumnsWindow(ui.KzhWindow):
    def __init__(self, groups, skipped, total):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES, NAV_PAGES)
        self.result = None
        self.groups = groups
        self.skipped = skipped
        self.current = None
        self.part = 0
        self._loading = False
        self.tbCount.Text = u"{} / {}".format(total, len(groups))
        self.tbDoc.Text = doc.Title
        self.joint_buttons = ((self.rbLap, JOINT_LAP), (self.rbCrank, JOINT_CRANK),
                              (self.rbBend, JOINT_BEND), (self.rbFlush, JOINT_FLUSH))
        self.btnSkipped.Content = u"Не поддерживается: {}".format(len(skipped))
        self.btnSkipped.Visibility = Visibility.Visible if skipped else Visibility.Collapsed
        self.btnSkipped.Click += lambda s, e: show_message(
            u"Эти колонны армироваться не будут.", details=self.skipped,
            details_header=u"Пропущено: {}".format(len(self.skipped)))

        self._src = "section"     # где был последний ввод: хомут Ø/S есть и в сечении, и в зонах по высоте
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
        self.cbStress.ItemsSource = [Option(u"растянутая (α = 1,2)", False), Option(u"сжатая (α = 0,9)", True)]

        saved = settings.load(SETTINGS_SECTION)
        self.saved = saved
        for g in groups:
            g.part_opts = []
            for p in g.prof0.parts:
                if p.kind == "shaft":
                    o = dict(SECTION_DEFAULTS[part_kind(p)])
                    o.update(dict((k, v) for k, v in (saved.get(part_kind(p)) or {}).items() if k in o))
                else:
                    o = dict(CORBEL_DEFAULTS)
                    o.update(dict((k, v) for k, v in (saved.get("corbel") or {}).items() if k in o))
                g.part_opts.append(o)
            s0 = g.prof0.shafts[0]
            o0 = g.part_opts[g.prof0.parts.index(s0)]
            dmax = o0["d"] if part_kind(s0) == ROUND else max(o0["d_ct"], o0["d_cb"])
            g.zones = [dict(z) for z in (saved.get("zones") or default_zones(dmax, 8))]
        self.load_height(saved.get("height") or {})
        self.cbGroup.ItemsSource = groups
        self.cbGroup.SelectionChanged += self.on_group_changed
        self.cbPart.SelectionChanged += self.on_part_changed
        self.cbGroup.SelectedIndex = 0

        for cb in (self.cbSteel, self.cbStSteel, self.cbConcrete, self.cbCover, self.cbStress, self.cbHook):
            cb.SelectionChanged += lambda s, e: self.refresh()
        for rb, _ in self.joint_buttons:
            rb.Checked += lambda s, e: self.refresh()
        for rb in (self.rbMono, self.rbPrecast):
            rb.Checked += lambda s, e: self.refresh()
        self.chkSlab.Click += lambda s, e: self.refresh()
        for cnv in (self.cnvSection, self.cnvElev):
            cnv.SizeChanged += lambda s, e: self.refresh()
        self.btnRun.Click += self.on_run

    # ----------------------------------------------------------
    # ЧТЕНИЕ И ЗАПИСЬ ПОЛЕЙ
    # ----------------------------------------------------------
    def load_height(self, saved):
        o = dict(HEIGHT_DEFAULTS)
        o.update(dict((k, v) for k, v in saved.items() if k in o))
        default_hook = kr.default_stirrup_hook(doc)
        default_hook = element_name(default_hook) if default_hook is not None else u""
        ui.select_option(self.cbSteel, o["steel"])
        ui.select_option(self.cbStSteel, o["st_steel"])
        ui.select_option(self.cbConcrete, o["concrete"])
        ui.select_option(self.cbCover, o["cover_cond"])
        ui.select_option(self.cbStress, o["compressed"])
        ui.select_option(self.cbHook, default_hook if o["hook"] is None else o["hook"])
        ui.select_option(self.cbTieHook, default_hook if o["tie_hook"] is None else o["tie_hook"])
        self.zeditor.load({"first_offset": o["first_offset"], "top_offset": o["top_offset"], "lap": o["lap"]})
        self.chkSlab.IsChecked = bool(o["cut_by_slab"])
        self.rbMono.IsChecked = o["build"] != PRECAST
        self.rbPrecast.IsChecked = o["build"] == PRECAST
        for rb, key in self.joint_buttons:
            rb.IsChecked = key == o["joint"]

    def read_height(self):
        v = {}
        self.zeditor.read(v, ("first_offset", "top_offset", "lap"))
        return {
            "steel": ui.selected_value(self.cbSteel), "st_steel": ui.selected_value(self.cbStSteel),
            "concrete": ui.selected_value(self.cbConcrete), "cover_cond": ui.selected_value(self.cbCover),
            "build": PRECAST if self.rbPrecast.IsChecked else MONO,
            "joint": next((key for rb, key in self.joint_buttons if rb.IsChecked), JOINT_LAP),
            "compressed": ui.selected_value(self.cbStress, False), "lap": v["lap"],
            "cut_by_slab": bool(self.chkSlab.IsChecked),
            "first_offset": v["first_offset"], "top_offset": v["top_offset"],
            "hook": ui.selected_value(self.cbHook, u""), "tie_hook": ui.selected_value(self.cbTieHook, u""),
        }

    @staticmethod
    def fill_zone(g):
        """Основная зона хомутов (пустая длина), иначе самая длинная."""
        for z in g.zones:
            if not z["len"]:
                return z
        return max(g.zones, key=lambda z: z["len"])

    @staticmethod
    def part_kind_of(g, i):
        p = g.prof0.parts[i]
        return part_kind(p) if p.kind == "shaft" else "corbel"

    def load_part(self):
        g, i = self.current, self.part
        self._loading = True
        try:
            self.editor.show_only(VISIBLE_GROUPS[self.part_kind_of(g, i)])
            values = dict(g.part_opts[i])
            fz = self.fill_zone(g)
            values["ds"], values["step"] = fz["d"], fz["step"]
            self.editor.load(values)
        finally:
            self._loading = False

    def read_part(self):
        """Поля участка -> настройки. Возвращает текст ошибки или None."""
        g, i = self.current, self.part
        kind = self.part_kind_of(g, i)
        o = g.part_opts[i]
        if kind == "corbel":
            self.editor.read(o, CORBEL_DEFAULTS.keys())
            if o["cn"] is None or o["cn"] < 1 or o["cn"] != int(o["cn"]):
                return u"Число рабочих стержней консоли — целое, не меньше 1."
            if any(o[k] is not None and o[k] <= 0 for k in ("cstep", "cc")):
                return u"Шаг хомутов и защитный слой консоли должны быть больше нуля."
            return None
        v = {}
        self.editor.read(v, ("ds", "step"))
        self.editor.read(o, SECTION_DEFAULTS[kind].keys())
        fz = self.fill_zone(g)
        if v["ds"]:
            fz["d"] = v["ds"]
        if v["step"] and v["step"] > 0:
            fz["step"] = v["step"]
        if kind == ROUND:
            if o["n"] is None or o["n"] < 4 or o["n"] != int(o["n"]):
                return u"Укажите целое число стержней круглой колонны (не меньше 4)."
            covers = ("c",)
        else:
            for key in ("n_t", "n_b", "n_s"):
                if o[key] is None or o[key] < 0 or o[key] != int(o[key]):
                    return u"Число промежуточных и боковых стержней — целое, от 0."
            covers = ("c_s", "c_t", "c_b")
        if any(o[k] is not None and o[k] <= 0 for k in covers):
            return u"Защитный слой должен быть больше нуля."
        return None

    def calc_of(self, g, i):
        """Calc участка i группы g (для зоны консолей — участка под ней)."""
        p = g.prof0.parts[i]
        if p.kind != "shaft":
            i = g.prof0.parts.index(p.below)
        return Calc(self.read_height(), g.part_opts[i], part_kind(g.prof0.parts[i]), g.zones)

    # ----------------------------------------------------------
    # ОБНОВЛЕНИЕ ПРЕДПРОСМОТРА
    # ----------------------------------------------------------
    def on_group_changed(self, sender, args):
        g = self.cbGroup.SelectedItem
        if g is None:
            return
        if self.current is not None:
            self.read_all()
        self.current = g
        self._loading = True
        try:
            self.cbPart.ItemsSource = [PartItem(part_title(g.prof0, i), i) for i in range(len(g.prof0.parts))]
            self.cbPart.SelectedIndex = 0
            self.cbPart.Visibility = Visibility.Collapsed if g.prof0.is_simple else Visibility.Visible
            self.zeditor.set_zones(g.zones)
            g.zones = self.zeditor.zones
        finally:
            self._loading = False
        self.part = 0
        self.load_part()
        self.refresh()

    def on_part_changed(self, sender, args):
        item = self.cbPart.SelectedItem
        if self._loading or item is None or self.current is None:
            return
        self.read_all()
        self.part = item.index
        self.load_part()
        self.refresh()

    def on_edit(self, source):
        self._src = source
        self.refresh()

    def sync_fill_fields(self):
        """Хомут основной зоны — одинаково в полях сечения и в строке зоны на виде по высоте."""
        g = self.current
        fz = self.fill_zone(g)
        i = g.zones.index(fz)
        self.zeditor.load({u"z_step_{}".format(i): fz["step"], u"z_d_{}".format(i): fz["d"]})
        self.editor.load({"ds": fz["d"], "step": fz["step"]})

    def read_all(self):
        """Поля -> настройки группы; последнее изменённое место (сечение или зоны) главнее."""
        g = self.current
        g.zones = self.zeditor.zones
        if self._src == "section":
            err = self.zeditor.read_zones() or self.read_part()
        else:
            err = self.read_part() or self.zeditor.read_zones()
        if not err:
            self.sync_fill_fields()
        return err

    def refresh(self):
        g = self.current
        if self._loading or g is None:
            return
        err = self.read_all()
        if err:
            self.tbInfo.Text = err
            return
        ho = self.read_height()
        self.bdJoint.Visibility = Visibility.Collapsed if ho["build"] == PRECAST else Visibility.Visible
        try:
            self.refresh_section(g, ho)
            self.refresh_elevation(g, ho)
        except Exception as ex:
            self.tbInfo.Text = u"Проверьте значения в полях ({})".format(ex)
            return
        self.tbInfo.Text = self.calc_of(g, 0).info_text()

    def refresh_section(self, g, ho):
        i = self.part
        part = g.prof0.parts[i]
        if part.kind != "shaft":
            co = g.part_opts[i]
            cc = CorbelCalc(ho, co, part)
            self.editor.set_hint("cstep", u"" if co["cstep"] else u"пусто — {:.0f} (h/4, ≤ 150)".format(float(cc.step)))
            self.editor.set_hint("cc", u"" if co["cc"] else u"пусто — {:.0f} по норме".format(float(cc.cover_auto)))
            if not part.corbels:
                self.editor.show_only(())
                kd.Sketch(self.cnvSection, (0, 0, 1, 1))
                self.editor.place({})
                self.tbSectionInfo.Text = u"Переход сечения без консоли — отдельного армирования не требуется."
                return
            self.editor.place(draw_corbel(self.cnvSection, cc, g.prof0, part, self.editor.pad()))
            sides = u", ".join(u"{} — вылет {:.0f} мм".format(k.side_text(), mm(k.reach)) for k in part.corbels)
            info = u"Консоли: {} • высота у грани {:.0f} мм • анкеровка рабочих стержней {:.0f} мм".format(
                sides, float(cc.h), float(cc.anchorage))
            if cc.warnings:
                info += u" • Внимание: " + u"; ".join(cc.warnings)
            self.tbSectionInfo.Text = info
            return
        so = g.part_opts[i]
        calc = Calc(ho, so, part_kind(part), g.zones)
        for key in ("c_s", "c_t", "c_b", "c"):
            self.editor.set_hint(key, u"" if so.get(key) else u"пусто — {:.0f} по норме".format(float(calc.cover_auto)))
        self.editor.set_hint("step", u"основная зона по высоте")
        lay, targets = draw_section(self.cnvSection, calc, part.sec, self.editor.pad())
        if part_kind(part) == RECT:
            self.editor.set_hint("ties", ties_text(so, lay))
        self.editor.place(targets)
        info = u"Стержней: {} • As = {:.1f} см² • μ = {:.2f} %".format(lay.count, float(lay.area / 100.0), float(lay.mu))
        if lay.error:
            info += u" • ОШИБКА: " + lay.error
        notes = lay.notes + calc.warnings
        if notes:
            info += u" • Внимание: " + u"; ".join(notes)
        self.tbSectionInfo.Text = info

    def refresh_elevation(self, g, ho):
        calc0 = self.calc_of(g, 0)
        self.zeditor.set_hint("first_offset", u"" if ho["first_offset"] is not None else
                              u"пусто — {:.0f}".format(float(calc0.first_offset)))
        self.zeditor.set_hint("top_offset", u"" if ho["top_offset"] is not None else
                              u"пусто — {:.0f}".format(float(calc0.top_offset)))
        dmax = max(calc0.diams)
        self.zeditor.set_hint("lap", u"" if ho["lap"] else u"пусто — {:.0f} (⌀{}, СП 63)".format(float(calc0.lap(dmax)), dmax))
        show_lap = ho["build"] == MONO and ho["joint"] in (JOINT_LAP, JOINT_CRANK)
        self.zeditor.show_only([k for k in self.zeditor.groups if k != "lap" or show_lap])
        targets, facts = draw_elevation(self.cnvElev, ho, g, self.calc_of, self.zeditor.pad(top=30, bottom=30))
        for i in range(len(g.zones)):
            if i < len(facts):
                z, length, step, d, count = facts[i]
                self.zeditor.set_hint(u"z_{}".format(i), u"{:.0f} мм, хомутов {} шт.".format(float(length), count))
            else:
                self.zeditor.set_hint(u"z_{}".format(i), u"не помещается по высоте")
        self.zeditor.place(targets)
        self.tbElevInfo.Text = (u"Зоны хомутов — снизу вверх; пустая длина — зона занимает остаток высоты. "
                                u"В зонах консолей вместо хомутов колонны — хомуты консоли (раздел «Сечение»). "
                                u"Вид — по первой колонне группы.")

    def on_run(self, sender, args):
        if self.current is not None:
            err = self.read_all()
            if err:
                show_message(err)
                return
        ho = self.read_height()
        if ho["lap"] is not None and ho["lap"] <= 0:
            show_message(u"Длина нахлёста должна быть больше нуля.")
            return
        if any(ho[k] is not None and ho[k] < 0 for k in ("first_offset", "top_offset")):
            show_message(u"Отступы хомутов не могут быть отрицательными.")
            return
        data = dict(self.saved)
        data["height"] = ho
        for g in self.groups:
            for p, o in zip(g.prof0.parts, g.part_opts):
                data[part_kind(p) if p.kind == "shaft" else "corbel"] = dict(o)
        data["zones"] = [dict(z) for z in self.current.zones]
        settings.save(SETTINGS_SECTION, data)
        self.result = ho
        self.window.Close()


# ==========================================================
# ЗАПУСК
# ==========================================================
class ColumnFilter(ISelectionFilter):
    def AllowElement(self, element):
        cat = element.Category
        return cat is not None and cat.Id.IntegerValue == int(DB.BuiltInCategory.OST_StructuralColumns)

    def AllowReference(self, reference, point):
        return False


def get_columns():
    """Колонны из текущего выделения, иначе — выбор в модели."""
    flt = ColumnFilter()
    cols = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
    cols = [c for c in cols if c is not None and flt.AllowElement(c)]
    if cols:
        return cols
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, flt, u"Выберите колонны и нажмите «Готово»")
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: в семействе армирование колонн не выполняется.")
        return
    columns = get_columns()
    if not columns:
        return
    groups, skipped = group_columns(columns)
    if not groups:
        show_message(u"Среди выбранных нет колонн, которые можно армировать.", details=skipped,
                     details_header=u"Пропущено: {}".format(len(skipped)))
        return
    win = ColumnsWindow(groups, skipped, len(columns))
    win.show_modal()
    if win.result is not None:
        reinforce(groups, win.result, skipped)


try:
    main()
except Exception:
    show_message(u"Ошибка армирования колонн:\n{}".format(traceback.format_exc()), title=u"Ошибка")

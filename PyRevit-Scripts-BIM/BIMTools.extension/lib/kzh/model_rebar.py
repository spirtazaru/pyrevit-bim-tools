# -*- coding: utf-8 -*-
"""Арматура, которая уже стоит в модели, в сечении элемента — для окон «Колонны» и «Балки».

Сечение задаётся функцией точки элемента point(a, b, c) -> XYZ (футы, как у ColumnSection / BeamSection)
и номером оси вдоль элемента along (0, 1 или 2) с отметкой level (футы) на этой оси. Продольные стержни дают
точки пересечения с плоскостью сечения; поперечные (хомуты, шпильки, ромбы) — ближайший к сечению стержень
каждого набора, спроецированный на плоскость.
"""
from collections import Counter

from pyrevit import DB

from kzh import element_name
from kzh import drawing as kd
from kzh import rebar as kr
from kzh.geometry import mm

ST = DB.Structure


class Frame(object):
    """Локальные оси элемента по функции точки: local(p) -> (a, b, c) в футах."""

    def __init__(self, point):
        self.o = point(0.0, 0.0, 0.0)
        self.e = [point(1.0, 0.0, 0.0) - self.o, point(0.0, 1.0, 0.0) - self.o, point(0.0, 0.0, 1.0) - self.o]

    def local(self, p):
        d = p - self.o
        return [d.DotProduct(e) / e.DotProduct(e) for e in self.e]


def _bar_curves(rb, i, acc):
    curves = list(rb.GetCenterlineCurves(False, False, True, ST.MultiplanarOption.IncludeOnlyPlanarCurves, 0))
    if i and acc is not None:
        tr = acc.GetBarPositionTransform(i)
        curves = [c.CreateTransformed(tr) for c in curves]
    return curves


def section(doc, host, point, along, level, near=1.0):
    """Арматура основы в сечении: (стержни [(x, y, Ø)], контуры [[(x, y)]], сводка [строки]).
    Координаты — мм в двух осях, кроме along. near — насколько далеко (футы) от сечения искать поперечные."""
    fr = Frame(point)
    other = [k for k in range(3) if k != along]
    bars, loops = [], []
    long_c, trans_c = Counter(), Counter()
    for rid in kr.host_rebar_ids(host):
        rb = doc.GetElement(rid)
        if not isinstance(rb, ST.Rebar):
            continue
        d = kr.bar_diameter_mm(doc.GetElement(rb.GetTypeId())) or 10.0
        try:
            n = rb.NumberOfBarPositions
            acc = rb.GetShapeDrivenAccessor()
        except Exception:
            n, acc = 1, None
        best = None           # (расстояние до сечения, кривые) — ближайший поперечный стержень набора
        crossed = 0
        for i in range(n):
            try:
                if rb.DoesBarExistAtPosition(i) is False:
                    continue
            except Exception:
                pass
            curves = _bar_curves(rb, i, acc)
            pts = []
            for c in curves:
                a, b = fr.local(c.GetEndPoint(0)), fr.local(c.GetEndPoint(1))
                pts.extend(fr.local(q) for q in list(c.Tessellate())[:-1])     # дуги — по точкам, не по концам
                lo, hi = sorted((a[along], b[along]))
                if hi - lo > 1e-6 and lo <= level <= hi:
                    t = (level - a[along]) / (b[along] - a[along])
                    # продольный участок: вдоль оси элемента заметно больше, чем поперёк
                    span_along = abs(b[along] - a[along])
                    span_cross = max(abs(b[k] - a[k]) for k in other)
                    if span_along > 3 * span_cross:
                        bars.append((mm(a[other[0]] + t * (b[other[0]] - a[other[0]])),
                                     mm(a[other[1]] + t * (b[other[1]] - a[other[1]])), d))
                        crossed += 1
            pts.append(fr.local(curves[-1].GetEndPoint(1)))
            flat = max(p[along] for p in pts) - min(p[along] for p in pts) < 0.2
            if flat:
                dist = abs(sum(p[along] for p in pts) / len(pts) - level)
                if dist < near and (best is None or dist < best[0]):
                    best = (dist, [(mm(p[other[0]]), mm(p[other[1]])) for p in pts])
        if crossed:
            long_c[d] += crossed
        if best is not None:
            loops.append((best[1], d, element_name(doc.GetElement(rb.GetShapeId()))))
            trans_c[d] += 1
    summary = []
    if long_c:
        summary.append(u"продольные: " + u", ".join(u"{} ⌀{:.0f}".format(v, float(k)) for k, v in sorted(long_c.items())))
    if trans_c:
        summary.append(u"поперечных наборов у сечения: " + u", ".join(u"{} ⌀{:.0f}".format(v, float(k))
                                                                   for k, v in sorted(trans_c.items())))
    return bars, loops, summary


def draw(canvas, outline, bars, loops, pad, size_text):
    """Сечение по модели: контур бетона outline [(x, y)], стержни, контуры поперечных. Возвращает Sketch."""
    xs = [p[0] for p in outline] + [b[0] for b in bars]
    ys = [p[1] for p in outline] + [b[1] for b in bars]
    sk = kd.Sketch(canvas, (min(xs), min(ys), max(xs), max(ys)), pad=pad)
    sk.poly(outline, kd.CONCRETE_EDGE, 1.5, kd.CONCRETE, closed=True)
    for pts, d, _ in loops:
        sk.poly(pts, kd.STIRRUP, 2.0)
    for x, y, d in bars:
        sk.circle(x, y, d / 2.0, kd.CORNER)
    x0, x1, y0, y1 = min(p[0] for p in outline), max(p[0] for p in outline), min(p[1] for p in outline), max(p[1] for p in outline)
    sk.dim(x0, y1, x1, y1, size_text[0])
    sk.dim(x1, y0, x1, y1, size_text[1], outside="right")
    return sk


# Карточка «Арматура в модели» — вверху своей колонки, без выноски
TARGET = (0.0, 0.0, False)


def stacked(editor):
    """Места карточек, пока рисунок взят из модели: без выносок (рисунок другой), «Арматура в модели» — первой."""
    out = dict((k, (0.0, float(i + 1), False)) for i, k in enumerate(sorted(editor.groups)))
    out["model"] = TARGET
    return out

CARD = ("model", "R", {
    "title": u"Арматура в модели",
    "note": u"Показать сечение по арматуре, которая уже стоит в модели у выбранного элемента (а не по расчёту окна).",
    "rows": [[("button", "mdl_show", u"Показать из модели")], [("hint", "mdl")]]})

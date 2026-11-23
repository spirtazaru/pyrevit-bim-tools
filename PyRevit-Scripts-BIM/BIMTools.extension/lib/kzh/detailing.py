# -*- coding: utf-8 -*-
"""Оформление армирования на чертежах и видах:

- сечение колонны на чертёжном виде по арматуре из модели (ГОСТ 21.501): контур бетона тонкой линией,
  стержни — залитыми кружками, хомуты и шпильки — основной линией, цепочки размеров слева и снизу
  (до осей стержней) и габариты, позиции АрмМарка на полках выносок справа и сверху без пересечений
  (kzh.labels), заголовок с масштабом;
- 3D-вид армирования выбранных элементов: подрезка по выбору, арматура сплошная и поверх бетона,
  остальное скрыто; бетон основ — скрыть или показать (переключатель).

Чертёжный вид рисуется в натуральную величину (мм модели), масштаб вида задаёт размер текста и размеров.
Все функции — только внутри транзакции.
"""
import math

from System.Collections.Generic import List
from pyrevit import DB

from kzh import element_name, labels, lines, model_rebar
from kzh import geometry as kg
from kzh import rebar as kr
from kzh.geometry import ft

TEXT_MM = 2.5           # высота текста на бумаге


def _unique_view_name(doc, base):
    names = set(v.Name for v in DB.FilteredElementCollector(doc).OfClass(DB.View))
    if base not in names:
        return base
    k = 2
    while u"{} ({})".format(base, k) in names:
        k += 1
    return u"{} ({})".format(base, k)


def _view_type(doc, family):
    return next(t.Id for t in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType) if t.ViewFamily == family)


def _solid_fill_type(doc):
    """Тип заливки со сплошным образцом (для стержней в сечении) или None."""
    for t in DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType):
        pat = doc.GetElement(t.ForegroundPatternId)
        try:
            if pat is not None and pat.GetFillPattern().IsSolidFill:
                return t.Id
        except Exception:
            pass
    return None


def _text_type(doc):
    best, err = None, None
    for t in DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType):
        p = t.get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
        if p is not None:
            e = abs(p.AsDouble() * 304.8 - TEXT_MM)
            if err is None or e < err:
                best, err = t.Id, e
    return best or doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)


class _Pen(object):
    """Рисование на виде в мм модели (начало — центр сечения)."""

    def __init__(self, doc, view, scale):
        self.doc, self.view, self.scale = doc, view, scale
        self.styles = lines.ensure_styles(doc)
        self.fill = _solid_fill_type(doc)
        tt = _text_type(doc)
        self.opts = DB.TextNoteOptions(tt)
        self.opts.HorizontalAlignment = DB.HorizontalTextAlignment.Center
        self.opts.VerticalAlignment = DB.VerticalTextAlignment.Bottom

    @staticmethod
    def P(x, y):
        return DB.XYZ(ft(x), ft(y), 0.0)

    def curve(self, curve, style):
        dc = self.doc.Create.NewDetailCurve(self.view, curve)
        gs = self.styles.get(style)
        if gs is not None:
            dc.LineStyle = gs
        return dc

    def line(self, x0, y0, x1, y1, style):
        if math.hypot(x1 - x0, y1 - y0) < 0.5:
            return None
        return self.curve(DB.Line.CreateBound(self.P(x0, y0), self.P(x1, y1)), style)

    def poly(self, pts, style, closed=False):
        seq = list(pts) + ([pts[0]] if closed else [])
        for a, b in zip(seq[:-1], seq[1:]):
            self.line(a[0], a[1], b[0], b[1], style)

    def circle(self, x, y, r, style):
        c = self.P(x, y)
        for a0, a1 in ((0.0, math.pi), (math.pi, 2 * math.pi)):
            self.curve(DB.Arc.Create(c, ft(r), a0, a1, DB.XYZ.BasisX, DB.XYZ.BasisY), style)

    def dot(self, x, y, r):
        """Стержень в сечении — залитый кружок (или окружность, если в проекте нет сплошной заливки)."""
        if self.fill is None:
            self.circle(x, y, r, u"КЖ_Стержень")
            return
        c = self.P(x, y)
        loop = DB.CurveLoop()
        loop.Append(DB.Arc.Create(c, ft(r), 0.0, math.pi, DB.XYZ.BasisX, DB.XYZ.BasisY))
        loop.Append(DB.Arc.Create(c, ft(r), math.pi, 2 * math.pi, DB.XYZ.BasisX, DB.XYZ.BasisY))
        DB.FilledRegion.Create(self.doc, self.fill, self.view.Id, List[DB.CurveLoop]([loop]))

    def text(self, x, y, value):
        DB.TextNote.Create(self.doc, self.view.Id, self.P(x, y), value, self.opts)

    def dims(self, refs, x0, y0, x1, y1):
        """Цепочка размеров по ссылкам на линии refs вдоль линии (x0, y0)–(x1, y1)."""
        arr = DB.ReferenceArray()
        for dc in refs:
            if dc is not None:
                arr.Append(dc.GeometryCurve.Reference)
        if arr.Size >= 2:
            self.doc.Create.NewDimension(self.view, DB.Line.CreateBound(self.P(x0, y0), self.P(x1, y1)), arr)


def _uniq(values, tol=1.0):
    out = []
    for v in sorted(values):
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def column_section_view(doc, column, scale=20):
    """Чертёжный вид «Сечение колонны» по арматуре колонны в модели (на середине высоты).
    Возвращает (вид или None, [замечания])."""
    res = kg.column_section(column)
    if isinstance(res, tuple):
        return None, [res[1]]
    sec = res
    level = (sec.z_bot + sec.z_top) / 2.0
    bars, loops, _ = model_rebar.section(doc, column, sec.point, 2, level, marks=True)
    notes = []
    if not bars:
        return None, [u"в сечении нет продольных стержней (арматура не создана или не в этой колонне)"]
    b, h = sec.b * 304.8, sec.h * 304.8
    view = DB.ViewDrafting.Create(doc, _view_type(doc, DB.ViewFamily.Drafting))
    view.Name = _unique_view_name(doc, u"КЖ — Сечение колонны {}".format(element_name(column)))
    view.Scale = scale
    pen = _Pen(doc, view, scale)
    k = float(scale)                     # мм бумаги -> мм модели

    # Контур бетона и стержни
    edges = {}
    if sec.is_round:
        pen.circle(0.0, 0.0, b / 2.0, u"КЖ_Контур бетона")
    else:
        edges["L"] = pen.line(-b / 2, -h / 2, -b / 2, h / 2, u"КЖ_Контур бетона")
        edges["R"] = pen.line(b / 2, -h / 2, b / 2, h / 2, u"КЖ_Контур бетона")
        edges["B"] = pen.line(-b / 2, -h / 2, b / 2, -h / 2, u"КЖ_Контур бетона")
        edges["T"] = pen.line(-b / 2, h / 2, b / 2, h / 2, u"КЖ_Контур бетона")
    for pts, d, shape, mark in loops:
        pen.poly(pts, u"КЖ_Хомут")

    # Размеры: цепочки до осей стержней нижнего ряда и левого столбца, затем габариты
    if not sec.is_round:
        y_bot = min(y for _, y, _, _ in bars)
        x_left = min(x for x, _, _, _ in bars)
        xs = _uniq([x for x, y, _, _ in bars if abs(y - y_bot) < 1.0])
        ys = _uniq([y for x, y, _, _ in bars if abs(x - x_left) < 1.0])
        d0 = max(d for _, _, d, _ in bars)
        # Опорные засечки по осям стержней — короче радиуса, скрыты под заливкой кружка
        t = max(d0 / 4.0, 1.0)
        ticks_x = [pen.line(x, y_bot - t, x, y_bot + t, u"КЖ_Размер") for x in xs]
        ticks_y = [pen.line(x_left - t, y, x_left + t, y, u"КЖ_Размер") for y in ys]
        off1, off2 = 8.0 * k, 16.0 * k
        pen.dims([edges["L"]] + ticks_x + [edges["R"]], -b / 2, -h / 2 - off1, b / 2, -h / 2 - off1)
        pen.dims([edges["L"], edges["R"]], -b / 2, -h / 2 - off2, b / 2, -h / 2 - off2)
        pen.dims([edges["B"]] + ticks_y + [edges["T"]], -b / 2 - off1, -h / 2, -b / 2 - off1, h / 2)
        pen.dims([edges["B"], edges["T"]], -b / 2 - off2, -h / 2, -b / 2 - off2, h / 2)
    else:
        notes.append(u"круглое сечение: размеры не ставятся — диаметр в заголовке")
    # Стержни — после засечек размеров: заливка кружка (созданная позже) перекрывает засечку
    for x, y, d, mark in bars:
        pen.dot(x, y, d / 2.0)

    # Позиции: продольные — колонкой справа (выноска к крайнему правому стержню марки),
    # поперечные — рядом сверху (выноска к левой точке контура)
    th = TEXT_MM * k
    labs, unmarked = [], False
    by_mark = {}
    for x, y, d, mark in bars:
        key = mark or u"⌀{:.0f}".format(d)
        unmarked = unmarked or not mark
        by_mark.setdefault(key, []).append((x, y))
    for key, pts in by_mark.items():
        lab = labels.Label(max(pts, key=lambda p: (p[0], p[1])), key, (len(key) * 0.75 * TEXT_MM + 3.0) * k, th * 1.6)
        lab.side = "R"
        labs.append(lab)
    seen = set()
    for pts, d, shape, mark in loops:
        key = mark or u"⌀{:.0f}".format(d)
        unmarked = unmarked or not mark
        if key in seen or key in by_mark:
            continue
        seen.add(key)
        lab = labels.Label(min(pts, key=lambda p: (p[0], -p[1])), key, (len(key) * 0.75 * TEXT_MM + 3.0) * k, th * 1.6)
        lab.side = "T"
        labs.append(lab)
    labels.layout(labs, (-b / 2, -h / 2, b / 2, h / 2), margin=10.0 * k, gap=2.0 * k)
    for lab in labs:
        (tx, ty), (lx, ly) = labels.leader(lab)
        sx, sy = lab.shelf
        pen.line(tx, ty, lx, ly, u"КЖ_Выноска")
        pen.line(sx, sy, sx + lab.w, sy, u"КЖ_Выноска")
        pen.text(sx + lab.w / 2.0, sy + 0.5 * k, lab.text)
    if unmarked:
        notes.append(u"у части стержней нет АрмМарка — подписан диаметр (присвойте марки в «Сервисе»)")
    bad = labels.conflicts(labs)
    if bad:
        notes.append(u"подписи пересекаются: {} — поправьте вручную".format(len(bad)))

    # Заголовок под размерами
    size = u"⌀{:.0f}".format(b) if sec.is_round else u"{:.0f}×{:.0f}".format(b, h)
    pen.text(0.0, -h / 2 - 30.0 * k, u"Сечение колонны {} ({}), М 1:{}".format(element_name(column), size, scale))
    return view, notes


# ----------------------------------------------------------
# 3D-ВИД АРМИРОВАНИЯ
# ----------------------------------------------------------
def rebar_3d_view(doc, hosts, hide_concrete=False, margin_mm=300.0):
    """3D-вид: подрезка по габариту выбранных основ, их арматура — сплошная и поверх бетона,
    остальные категории и элементы скрыты. Возвращает (вид, число наборов арматуры)."""
    view = DB.View3D.CreateIsometric(doc, _view_type(doc, DB.ViewFamily.ThreeDimensional))
    view.Name = _unique_view_name(doc, u"КЖ — Армирование 3D")
    try:
        view.DetailLevel = DB.ViewDetailLevel.Fine
    except Exception:
        pass
    lo, hi = None, None
    for el in hosts:
        bb = el.get_BoundingBox(None)
        if bb is None:
            continue
        lo = bb.Min if lo is None else DB.XYZ(min(lo.X, bb.Min.X), min(lo.Y, bb.Min.Y), min(lo.Z, bb.Min.Z))
        hi = bb.Max if hi is None else DB.XYZ(max(hi.X, bb.Max.X), max(hi.Y, bb.Max.Y), max(hi.Z, bb.Max.Z))
    if lo is not None:
        m = ft(margin_mm)
        box = DB.BoundingBoxXYZ()
        box.Min = DB.XYZ(lo.X - m, lo.Y - m, lo.Z - m)
        box.Max = DB.XYZ(hi.X + m, hi.Y + m, hi.Z + m)
        view.SetSectionBox(box)

    host_ids = set(el.Id.IntegerValue for el in hosts)
    rebar_ids = set()
    for el in hosts:
        for rid in kr.host_rebar_ids(el):
            rebar_ids.add(rid.IntegerValue)
    keep_cats = set([int(DB.BuiltInCategory.OST_Rebar)] +
                    [el.Category.Id.IntegerValue for el in hosts if el.Category is not None])
    for cat in doc.Settings.Categories:
        if cat.CategoryType == DB.CategoryType.Model and cat.Id.IntegerValue not in keep_cats:
            try:
                if view.CanCategoryBeHidden(cat.Id):
                    view.SetCategoryHidden(cat.Id, True)
            except Exception:
                pass
    # В оставленных категориях — только выбранные основы и их арматура
    hide = List[DB.ElementId]()
    for cid in keep_cats:
        for el in DB.FilteredElementCollector(doc).OfCategoryId(DB.ElementId(cid)).WhereElementIsNotElementType():
            i = el.Id.IntegerValue
            if i in host_ids or i in rebar_ids:
                continue
            if el.CanBeHidden(view):
                hide.Add(el.Id)
    if hide.Count:
        view.HideElements(hide)
    for i in rebar_ids:
        rb = doc.GetElement(DB.ElementId(i))
        if isinstance(rb, DB.Structure.Rebar):
            try:
                rb.SetSolidInView(view, True)
                rb.SetUnobscuredInView(view, True)
            except Exception:
                pass
    if hide_concrete:
        set_concrete_hidden(doc, view, True)
    return view, len(rebar_ids)


def view_hosts(doc, view):
    """Основы арматуры, видимой на виде: [Element]."""
    ids = set()
    for rb in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Structure.Rebar):
        hid = rb.GetHostId()
        if hid is not None and hid != DB.ElementId.InvalidElementId:
            ids.add(hid.IntegerValue)
    return [doc.GetElement(DB.ElementId(i)) for i in ids if doc.GetElement(DB.ElementId(i)) is not None]


def concrete_hidden(doc, view):
    """Скрыт ли бетон основ на виде (хотя бы одна основа видима — нет)."""
    hosts = view_hosts(doc, view)
    return bool(hosts) and all(h.IsHidden(view) for h in hosts)


def set_concrete_hidden(doc, view, hidden):
    """Скрывает или показывает основы арматуры на виде. Возвращает число основ."""
    hosts = [h for h in view_hosts(doc, view) if h.CanBeHidden(view)]
    ids = List[DB.ElementId]([h.Id for h in hosts])
    if ids.Count:
        if hidden:
            view.HideElements(ids)
        else:
            view.UnhideElements(ids)
    return len(hosts)

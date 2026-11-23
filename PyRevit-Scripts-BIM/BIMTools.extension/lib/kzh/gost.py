# -*- coding: utf-8 -*-
"""Спецификация и ведомость расхода стали по ГОСТ 21.501 — три выдачи одних и тех же данных:

1. Спецификация Revit (ViewSchedule по категории «Несущая арматура»): живая, обновляется вместе с моделью.
   Масса в Revit 2022 у арматуры не считается встроенным параметром — она есть в таблице и в Excel.
2. Таблица на чертёжном виде (линии и текст): спецификация по форме 7 ГОСТ 21.101 и ведомость расхода
   стали по ГОСТ 21.501 — в мм бумаги, масштаб вида 1:1. Статичная: пересоздаётся кнопкой.
3. Excel (.xlsx) — два листа, без внешних библиотек (zip-архив из XML средствами .NET).

Данные: марки service.plan_marks (одинаковые стержни — одна позиция) и количество с учётом АрмЧисло.
"""
import clr
clr.AddReference('System.IO.Compression')

from System.IO import FileMode, FileStream, StreamWriter
from System.IO.Compression import ZipArchive, ZipArchiveMode
from System.Text import UTF8Encoding

from pyrevit import DB

from kzh import lines, norms, params
from kzh import service
from kzh.geometry import ft

STEEL_STANDARD = u"ГОСТ 34028-2016"         # прокат арматурный для ЖБ конструкций


# ----------------------------------------------------------
# ДАННЫЕ
# ----------------------------------------------------------
class SpecRow(object):
    """Позиция спецификации: одинаковые стержни одной конструкции."""

    def __init__(self, part, mark, d, cls, shape, length, count):
        self.part, self.mark, self.d, self.cls, self.shape, self.length, self.count = (
            part, mark, d, cls, shape, length, count)
        self.unit_mass = length / 1000.0 * (norms.mass_per_m(d) or 0.0)
        self.mass = self.unit_mass * count

    def name(self):
        """Наименование: ⌀12 А500, L = 2450 (форма — если стержень не прямой)."""
        text = u"⌀{:.0f} {}, L = {:.0f}".format(float(self.d), self.cls, float(self.length))
        if self.shape and not self.shape.lower().startswith(u"прям") and self.shape not in (u"00", u"M_00"):
            text += u" (форма {})".format(self.shape)
        return text


def spec_rows(doc, marks):
    """[SpecRow] по конструкциям, в порядке марок. marks — service.plan_marks."""
    rows = []
    for key, (mark, rbs) in marks.items():
        count = sum(service.bar_count(rb) * service.arm_count(rb) for rb in rbs)
        rows.append(SpecRow(key[0], mark, key[1], key[2], key[3], key[4], count))
    return sorted(rows, key=lambda r: (r.part, int(r.mark) if r.mark.isdigit() else 0))


def steel_matrix(rows):
    """Ведомость расхода стали: ({конструкция: {(класс, Ø): кг}}, [(класс, [Ø])]) — колонки по классам."""
    data, cols = {}, {}
    for r in rows:
        data.setdefault(r.part, {})
        k = (r.cls, r.d)
        data[r.part][k] = data[r.part].get(k, 0.0) + r.mass
        cols.setdefault(r.cls, set()).add(r.d)
    return data, [(cls, sorted(ds)) for cls, ds in sorted(cols.items())]


def matrix_table(rows):
    """Ведомость расхода стали как таблица: (заголовок 1, заголовок 2, строки) — текст ячеек."""
    data, cols = steel_matrix(rows)
    head1 = [(u"Марка элемента", 1)]
    head2 = [u""]
    for cls, ds in cols:
        head1.append((u"Арматура класса {} {}".format(cls, STEEL_STANDARD), len(ds) + 1))
        head2 += [u"⌀{:.0f}".format(float(d)) for d in ds] + [u"Итого"]
    head1.append((u"Всего", 1))
    head2.append(u"")
    body, grand = [], {}
    for part in sorted(data):
        line, total = [part], 0.0
        for cls, ds in cols:
            sub = 0.0
            for d in ds:
                m = data[part].get((cls, d), 0.0)
                sub += m
                grand[(cls, d)] = grand.get((cls, d), 0.0) + m
                line.append(u"{:.1f}".format(m) if m else u"—")
            line.append(u"{:.1f}".format(sub))
            total += sub
        line.append(u"{:.1f}".format(total))
        body.append(line)
    line, total = [u"Всего"], 0.0
    for cls, ds in cols:
        sub = sum(grand.get((cls, d), 0.0) for d in ds)
        line += [u"{:.1f}".format(grand.get((cls, d), 0.0)) for d in ds] + [u"{:.1f}".format(sub)]
        total += sub
    line.append(u"{:.1f}".format(total))
    body.append(line)
    return head1, head2, body


SPEC_COLUMNS = ((15.0, u"Поз."), (60.0, u"Обозначение"), (65.0, u"Наименование"), (10.0, u"Кол."),
                (15.0, u"Масса ед., кг"), (20.0, u"Примечание"))      # форма 7 ГОСТ 21.101, ширина 185 мм


def spec_table(rows):
    """Спецификация: строки ячеек; конструкция — строка-заголовок раздела (по центру «Наименования»)."""
    body, part = [], None
    for r in rows:
        if r.part != part:
            part = r.part
            body.append([u"", u"", part, u"", u"", u""])
        body.append([r.mark, STEEL_STANDARD, r.name(), u"{}".format(r.count),
                     u"{:.2f}".format(r.unit_mass), u"{:.1f} кг".format(r.mass)])
    return body


# ----------------------------------------------------------
# 1. СПЕЦИФИКАЦИЯ REVIT
# ----------------------------------------------------------
def _unique_view_name(doc, base):
    names = set(v.Name for v in DB.FilteredElementCollector(doc).OfClass(DB.View))
    if base not in names:
        return base
    k = 2
    while u"{} ({})".format(base, k) in names:
        k += 1
    return u"{} ({})".format(base, k)


def create_revit_schedule(doc, name=u"КЖ — Спецификация арматуры"):
    """Спецификация несущей арматуры (только внутри транзакции): конструкция → марка, Ø, длина, количество,
    общая длина; группировка по АрмКонструкция с заголовком, одна строка на марку. Возвращает ViewSchedule."""
    vs = DB.ViewSchedule.CreateSchedule(doc, DB.ElementId(DB.BuiltInCategory.OST_Rebar))
    vs.Name = _unique_view_name(doc, name)
    defn = vs.Definition
    fields = list(defn.GetSchedulableFields())

    def by_builtin(bip):
        pid = DB.ElementId(bip)
        return next((f for f in fields if f.ParameterId == pid), None)

    def by_name(text):
        return next((f for f in fields if f.GetName(doc) == text), None)

    wanted = (
        (by_name(params.PRM_PART), u"Конструкция", False),
        (by_name(params.PRM_MARK), u"Поз.", False),
        (by_builtin(DB.BuiltInParameter.REBAR_BAR_DIAMETER), u"Ø, мм", False),
        (by_builtin(DB.BuiltInParameter.REBAR_ELEM_LENGTH), u"Длина стержня", False),
        (by_builtin(DB.BuiltInParameter.REBAR_ELEM_QUANTITY_OF_BARS), u"Кол.", True),
        (by_name(params.PRM_COUNT), u"АрмЧисло", False),
        (by_builtin(DB.BuiltInParameter.REBAR_ELEM_TOTAL_LENGTH), u"Общая длина", True),
    )
    added = {}
    for sf, heading, totals in wanted:
        if sf is None:
            continue
        field = defn.AddField(sf)
        field.ColumnHeading = heading
        if totals:
            try:
                field.DisplayType = DB.ScheduleFieldDisplayType.Totals
            except Exception:
                pass
        added[heading] = field
    for heading, header in ((u"Конструкция", True), (u"Поз.", False)):
        if heading in added:
            sg = DB.ScheduleSortGroupField(added[heading].FieldId)
            sg.ShowHeader = header
            defn.AddSortGroupField(sg)
    if u"Конструкция" in added:
        added[u"Конструкция"].IsHidden = True       # конструкция — в заголовке группы
    defn.IsItemized = False
    defn.ShowGrandTotal = True
    return vs


# ----------------------------------------------------------
# 2. ТАБЛИЦА НА ЧЕРТЁЖНОМ ВИДЕ
# ----------------------------------------------------------
ROW_H = 8.0          # высота строки, мм (ГОСТ 21.101)
HEAD_H = 15.0        # высота заголовка
TEXT_MM = 2.5


def _text_type(doc):
    """Тип текста с высотой, ближайшей к 2,5 мм; иначе тип по умолчанию."""
    best, err = None, None
    for t in DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType):
        p = t.get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
        if p is None:
            continue
        e = abs(p.AsDouble() * 304.8 - TEXT_MM)
        if err is None or e < err:
            best, err = t.Id, e
    return best or doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)


def _drafting_type(doc):
    return next(t.Id for t in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType)
                if t.ViewFamily == DB.ViewFamily.Drafting)


class _Table(object):
    """Рисование таблицы линиями и текстом на виде; координаты — мм бумаги от левого верхнего угла."""

    def __init__(self, doc, view, x0, y0):
        self.doc, self.view, self.x0, self.y0 = doc, view, x0, y0
        st = lines.ensure_styles(doc)
        self.thick = st.get(u"КЖ_Таблица рамка")
        self.thin = st.get(u"КЖ_Таблица")
        self.opts = DB.TextNoteOptions(_text_type(doc))
        self.opts.HorizontalAlignment = DB.HorizontalTextAlignment.Center
        self.opts.VerticalAlignment = DB.VerticalTextAlignment.Middle
        self.opts_left = DB.TextNoteOptions(_text_type(doc))
        self.opts_left.HorizontalAlignment = DB.HorizontalTextAlignment.Left
        self.opts_left.VerticalAlignment = DB.VerticalTextAlignment.Middle
        self._seen = set()

    def P(self, x, y):
        return DB.XYZ(ft(self.x0 + x), ft(self.y0 - y), 0.0)

    def line(self, x0, y0, x1, y1, thick=False):
        key = (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))
        if key in self._seen or (abs(x1 - x0) < 0.01 and abs(y1 - y0) < 0.01):
            return
        self._seen.add(key)
        dc = self.doc.Create.NewDetailCurve(self.view, DB.Line.CreateBound(self.P(x0, y0), self.P(x1, y1)))
        style = self.thick if thick else self.thin
        if style is not None:
            dc.LineStyle = style

    def text(self, x, y, w, h, value, left=False):
        if not value:
            return
        if left:
            DB.TextNote.Create(self.doc, self.view.Id, self.P(x + 1.5, y + h / 2.0), value, self.opts_left)
        else:
            DB.TextNote.Create(self.doc, self.view.Id, self.P(x + w / 2.0, y + h / 2.0), value, self.opts)

    def grid(self, widths, head_rows, body, title=None, left_cols=()):
        """head_rows — [[(текст, число колонок)]] (строки заголовка делят HEAD_H поровну), body — [[текст]].
        Рамка и заголовок — толстой линией, строки — тонкой. Возвращает высоту таблицы, мм."""
        y = 0.0
        if title:
            DB.TextNote.Create(self.doc, self.view.Id, self.P(sum(widths) / 2.0, -5.0), title, self.opts)
        xs = [0.0]
        for w in widths:
            xs.append(xs[-1] + w)
        hh = HEAD_H / len(head_rows)
        for k, row in enumerate(head_rows):
            col = 0
            for text, span in row:
                xa, xb = xs[col], xs[col + span]
                last = k == len(head_rows) - 1
                self.text(xa, y, xb - xa, hh, text)
                self.line(xa, y, xa, y + hh, True)
                self.line(xa, y, xb, y, True)
                if last:
                    self.line(xa, y + hh, xb, y + hh, True)
                col += span
            y += hh
        for row in body:
            for c, text in enumerate(row):
                self.text(xs[c], y, widths[c], ROW_H, text, left=c in left_cols)
                self.line(xs[c], y, xs[c], y + ROW_H, True)       # вертикали граф — основной линией (ГОСТ 21.101)
            self.line(0.0, y + ROW_H, xs[-1], y + ROW_H, False)
            y += ROW_H
        self.line(xs[-1], 0.0, xs[-1], y, True)
        self.line(0.0, y, xs[-1], y, True)
        return y


def draw_tables(doc, rows, name=u"КЖ — Спецификация и ведомость расхода стали"):
    """Чертёжный вид со спецификацией и ведомостью расхода стали (только внутри транзакции). Возвращает вид."""
    view = DB.ViewDrafting.Create(doc, _drafting_type(doc))
    view.Name = _unique_view_name(doc, name)
    view.Scale = 1
    t = _Table(doc, view, 0.0, 0.0)
    widths = [w for w, _ in SPEC_COLUMNS]
    h = t.grid(widths, [[(title, 1) for _, title in SPEC_COLUMNS]], spec_table(rows),
               title=u"Спецификация арматуры", left_cols=(2,))
    head1, head2, body = matrix_table(rows)
    n_cols = len(head2)
    widths2 = [30.0] + [15.0] * (n_cols - 2) + [20.0]
    t2 = _Table(doc, view, 0.0, -(h + 25.0))
    t2.grid(widths2, [head1, [(text, 1) for text in head2]], body,
            title=u"Ведомость расхода стали, кг", left_cols=(0,))
    return view


# ----------------------------------------------------------
# 3. EXCEL (.xlsx)
# ----------------------------------------------------------
def _esc(text):
    return (u"{}".format(text).replace(u"&", u"&amp;").replace(u"<", u"&lt;").replace(u">", u"&gt;")
            .replace(u'"', u"&quot;"))


def _col(i):
    s = u""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = u"ABCDEFGHIJKLMNOPQRSTUVWXYZ"[r] + s
    return s


def _sheet_xml(rows):
    """Лист: числа — числами (для формул Excel), остальное — встроенными строками."""
    out = [u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           u'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for r, row in enumerate(rows):
        cells = []
        for c, value in enumerate(row):
            ref = u"{}{}".format(_col(c), r + 1)
            num = None
            if isinstance(value, (int, float)):
                num = float(value)
            elif value:
                try:
                    num = float(u"{}".format(value))
                except ValueError:
                    num = None
            if num is not None:
                cells.append(u'<c r="{}"><v>{}</v></c>'.format(ref, repr(num)))
            elif value not in (u"", None):
                cells.append(u'<c r="{}" t="inlineStr"><is><t>{}</t></is></c>'.format(ref, _esc(value)))
        out.append(u'<row r="{}">{}</row>'.format(r + 1, u"".join(cells)))
    out.append(u"</sheetData></worksheet>")
    return u"".join(out)


def write_xlsx(path, sheets):
    """sheets — [(имя листа, [[ячейки]])]. Минимальная книга Office Open XML."""
    files = {
        u"[Content_Types].xml":
            u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            u'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            u'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            u'<Default Extension="xml" ContentType="application/xml"/>'
            u'<Override PartName="/xl/workbook.xml" '
            u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
            u"".join(u'<Override PartName="/xl/worksheets/sheet{}.xml" '
                     u'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                     .format(i + 1) for i in range(len(sheets))) + u"</Types>",
        u"_rels/.rels":
            u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            u'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            u'officeDocument" Target="xl/workbook.xml"/></Relationships>',
        u"xl/workbook.xml":
            u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            u'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            u'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' +
            u"".join(u'<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(_esc(n[:31]), i + 1, i + 1)
                     for i, (n, _) in enumerate(sheets)) + u"</sheets></workbook>",
        u"xl/_rels/workbook.xml.rels":
            u'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            u'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            u"".join(u'<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                     u'relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(i + 1)
                     for i in range(len(sheets))) + u"</Relationships>",
    }
    for i, (_, rows) in enumerate(sheets):
        files[u"xl/worksheets/sheet{}.xml".format(i + 1)] = _sheet_xml(rows)
    stream = FileStream(path, FileMode.Create)
    try:
        archive = ZipArchive(stream, ZipArchiveMode.Create)
        try:
            for name, text in files.items():
                writer = StreamWriter(archive.CreateEntry(name).Open(), UTF8Encoding(False))
                try:
                    writer.Write(text)
                finally:
                    writer.Dispose()
        finally:
            archive.Dispose()
    finally:
        stream.Dispose()


def excel_sheets(rows):
    """Листы книги: спецификация и ведомость расхода стали (те же данные, что в таблице на чертеже)."""
    spec = [[u"Конструкция"] + [t for _, t in SPEC_COLUMNS[:-1]] + [u"Масса, кг"]]
    for r in rows:
        spec.append([r.part, r.mark, STEEL_STANDARD, r.name(), r.count, round(r.unit_mass, 2), round(r.mass, 1)])
    head1, head2, body = matrix_table(rows)
    top = []
    for text, span in head1:
        top += [text] + [u""] * (span - 1)
    return [(u"Спецификация", spec), (u"Ведомость расхода стали", [top, head2] + body)]

# -*- coding: utf-8 -*-
"""Сервис КЖ: проверка созданной арматуры, марки и ведомость расхода стали, отпечаток геометрии основы
для поиска элементов, изменённых после армирования.

Отпечаток (объём, габариты, тип, число проёмов) хранится в Extensible Storage основы и записывается
при армировании (runner). Изменилась основа — отпечаток не совпадает, арматуру нужно обновить.
"""
import System
from pyrevit import DB
from Autodesk.Revit.DB.ExtensibleStorage import Schema, SchemaBuilder, Entity, AccessLevel

from kzh import element_name, norms, params
from kzh import rebar as kr
from kzh.geometry import element_solids, ft, mm

ST = DB.Structure

# Кнопки армирования по категории основы: (команда pyRevit, подпись)
BUTTONS = (
    (DB.BuiltInCategory.OST_Walls, u"bimtools_конструктив_кж_стены", u"Стены"),
    (DB.BuiltInCategory.OST_Floors, u"bimtools_конструктив_кж_плиты", u"Плиты"),
    (DB.BuiltInCategory.OST_StructuralFoundation, u"bimtools_конструктив_кж_плиты", u"Плиты"),
    (DB.BuiltInCategory.OST_StructuralColumns, u"bimtools_конструктив_кж_колонны", u"Колонны"),
    (DB.BuiltInCategory.OST_StructuralFraming, u"bimtools_конструктив_кж_балки", u"Балки"),
    (DB.BuiltInCategory.OST_Stairs, u"bimtools_конструктив_кж_лестницы", u"Лестницы"),
)
# Бетонные элементы, в которые арматура может выходить из основы (выпуски, анкеровка в соседний элемент)
CONCRETE_CATEGORIES = tuple(b[0] for b in BUTTONS)


def button_for(element):
    cid = element.Category.Id.IntegerValue if element.Category is not None else None
    for bic, cmd, title in BUTTONS:
        if cid == int(bic):
            return cmd, title
    return None, None


# ----------------------------------------------------------
# АРМАТУРА В ОБЛАСТИ: ВЫБРАННЫЕ ОСНОВЫ ИЛИ СТЕРЖНИ, ИНАЧЕ ВСЯ МОДЕЛЬ
# ----------------------------------------------------------
def collect_rebar(doc, selected_ids):
    """[Rebar]: выбранные стержни и арматура выбранных основ; ничего не выбрано — вся арматура модели."""
    if not selected_ids:
        return list(DB.FilteredElementCollector(doc).OfClass(ST.Rebar))
    out, seen = [], set()
    for eid in selected_ids:
        el = doc.GetElement(eid)
        if isinstance(el, ST.Rebar):
            found = [el]
        else:
            found = [doc.GetElement(i) for i in kr.host_rebar_ids(el)] if kr.is_valid_host(el) else []
        for rb in found:
            if isinstance(rb, ST.Rebar) and rb.Id.IntegerValue not in seen:
                seen.add(rb.Id.IntegerValue)
                out.append(rb)
    return out


def bar_type(doc, rb):
    return doc.GetElement(rb.GetTypeId())


def steel_class(bt):
    """Класс стали из имени типа стержня (по списку классов norms.json), иначе «—»."""
    name = kr._norm_class(element_name(bt))
    for cls in sorted(norms.steel_classes(), key=len, reverse=True):
        if kr._norm_class(cls) in name:
            return cls
    return u"—"


def construction(doc, rb):
    """АрмКонструкция стержня; если пусто — категория основы."""
    p = rb.LookupParameter(params.PRM_PART)
    if p is not None and p.AsString():
        return p.AsString()
    host = doc.GetElement(rb.GetHostId())
    return host.Category.Name if host is not None and host.Category is not None else u"—"


def arm_count(rb):
    p = rb.LookupParameter(params.PRM_COUNT)
    try:
        v = p.AsInteger() if p is not None else 0
    except Exception:
        v = 0
    return v if v > 0 else 1


def is_transverse(doc, rb):
    shape = doc.GetElement(rb.GetShapeId())
    try:
        return shape is not None and shape.RebarStyle == ST.RebarStyle.StirrupTie
    except Exception:
        return False


def bar_count(rb):
    try:
        return rb.Quantity
    except Exception:
        return 1


def bar_positions(rb):
    """Осевые линии стержней набора (без отгибов-скруглений): первый и последний стержень."""
    try:
        n = rb.NumberOfBarPositions
        acc = rb.GetShapeDrivenAccessor()
    except Exception:
        n, acc = 1, None
    out = []
    for i in sorted(set([0, max(0, n - 1)])):
        try:
            if rb.DoesBarExistAtPosition(i) is False:
                continue
        except Exception:
            pass
        curves = list(rb.GetCenterlineCurves(False, False, True, ST.MultiplanarOption.IncludeOnlyPlanarCurves, 0))
        if i and acc is not None:
            tr = acc.GetBarPositionTransform(i)
            curves = [c.CreateTransformed(tr) for c in curves]
        out.append(curves)
    return out


# ----------------------------------------------------------
# ПРОВЕРКА АРМИРОВАНИЯ
# ----------------------------------------------------------
class Issue(object):
    def __init__(self, rb, host_title, kind, text):
        self.rebar_id = rb.Id
        self.host_title = host_title
        self.kind = kind            # out | cover | spacing
        self.text = text


def _inside(solids, p, u, opts):
    """Лежит ли точка p внутри тел: пробная линия вдоль u через p, точка — на отрезке пересечения."""
    probe = DB.Line.CreateBound(p - u * ft(300), p + u * ft(300))
    for s in solids:
        res = s.IntersectWithCurve(probe, opts)
        for k in range(res.SegmentCount):
            seg = res.GetCurveSegment(k)
            t0 = (seg.GetEndPoint(0) - probe.GetEndPoint(0)).DotProduct(u)
            t1 = (seg.GetEndPoint(1) - probe.GetEndPoint(0)).DotProduct(u)
            if min(t0, t1) - ft(0.5) <= ft(300) <= max(t0, t1) + ft(0.5):
                return True
    return False


class _Concrete(object):
    """Тела бетонных элементов модели с кэшем: выход стержня из основы в соседний элемент — не ошибка."""

    def __init__(self, doc):
        self.doc = doc
        self.cache = {}

    def solids(self, el):
        key = el.Id.IntegerValue
        if key not in self.cache:
            self.cache[key] = element_solids(el)
        return self.cache[key]

    def boxes(self):
        """[(элемент, min, max)] бетонных элементов модели — собираются один раз."""
        if not hasattr(self, '_boxes'):
            self._boxes = []
            for bic in CONCRETE_CATEGORIES:
                for el in DB.FilteredElementCollector(self.doc).OfCategory(bic).WhereElementIsNotElementType():
                    bb = el.get_BoundingBox(None)
                    if bb is not None:
                        self._boxes.append((el, bb.Min, bb.Max))
        return self._boxes

    def contains(self, p, u, opts, skip_id):
        e = ft(1)
        for el, lo, hi in self.boxes():
            if el.Id.IntegerValue == skip_id:
                continue
            if lo.X - e <= p.X <= hi.X + e and lo.Y - e <= p.Y <= hi.Y + e and lo.Z - e <= p.Z <= hi.Z + e:
                if _inside(self.solids(el), p, u, opts):
                    return True
        return False


def _planar_faces(solids):
    return [f for s in solids for f in s.Faces if isinstance(f, DB.PlanarFace)]


def _spacing_limit(doc, host, rb, transverse):
    """Наибольший допустимый шаг для набора (мм) или None — не проверяется."""
    if transverse:
        return None
    cid = host.Category.Id.IntegerValue if host.Category is not None else None
    if cid == int(DB.BuiltInCategory.OST_Walls) and isinstance(host, DB.Wall):
        try:
            d = rb.GetShapeDrivenAccessor().Normal
        except Exception:
            return None
        vertical = abs(d.Z) < 0.5       # вертикальные стержни раскладываются по горизонтали
        return norms.wall_spacing_max(mm(host.Width), vertical)
    if cid in (int(DB.BuiltInCategory.OST_Floors), int(DB.BuiltInCategory.OST_StructuralFoundation)):
        p = host.get_Parameter(DB.BuiltInParameter.FLOOR_ATTR_THICKNESS_PARAM)
        if p is None or not p.HasValue:
            return None
        return norms.slab_spacing_max(mm(p.AsDouble()))
    return None


def _max_gap(rb):
    """Наибольшее расстояние между соседними стержнями набора, мм (None — одиночный стержень)."""
    try:
        n = rb.NumberOfBarPositions
        acc = rb.GetShapeDrivenAccessor()
    except Exception:
        return None
    if n < 2:
        return None
    pts = []
    for i in range(n):
        try:
            if rb.DoesBarExistAtPosition(i) is False:
                continue
        except Exception:
            pass
        pts.append(acc.GetBarPositionTransform(i).Origin)
    gaps = [a.DistanceTo(b) for a, b in zip(pts[:-1], pts[1:])]
    return mm(max(gaps)) if gaps else None


def check_rebar(doc, rebars, condition, progress=None):
    """Проверка стержней: выход из бетона, защитный слой меньше нормы (сбоку стержня), шаг набора больше нормы.
    condition — ключ условий эксплуатации из norms.json. Возвращает [Issue]."""
    opts = DB.SolidCurveIntersectionOptions()
    concrete = _Concrete(doc)
    hosts = {}
    issues = []
    for k, rb in enumerate(rebars):
        if progress is not None:
            progress(k, len(rebars))
        host = doc.GetElement(rb.GetHostId())
        if host is None:
            continue
        hk = host.Id.IntegerValue
        if hk not in hosts:
            solids = concrete.solids(host)
            hosts[hk] = (solids, _planar_faces(solids), u"{} (id {})".format(element_name(host), hk))
        solids, faces, title = hosts[hk]
        bt = bar_type(doc, rb)
        d = kr.bar_diameter_mm(bt) or 0.0
        transverse = is_transverse(doc, rb)
        need = norms.cover_constructive(condition, d) if transverse else norms.cover_main(condition, d)
        worst_cover, outside = None, 0
        for curves in bar_positions(rb):
            for c in curves:
                if not isinstance(c, DB.Line) or c.Length < ft(20):
                    continue
                u = c.Direction
                for t in (0.1, 0.5, 0.9):
                    p = c.Evaluate(t, True)
                    if not _inside(solids, p, u, opts):
                        if not concrete.contains(p, u, opts, hk):
                            outside += 1
                        continue
                    for f in faces:
                        if abs(f.FaceNormal.DotProduct(u)) > 0.05:
                            continue
                        res = f.Project(p)
                        if res is None:
                            continue
                        cover = mm(res.Distance) - d / 2.0
                        if worst_cover is None or cover < worst_cover:
                            worst_cover = cover
        if outside:
            issues.append(Issue(rb, title, 'out', u"⌀{:.0f}: часть стержня вне бетона".format(float(d))))
        if worst_cover is not None and worst_cover < need - 0.5:
            issues.append(Issue(rb, title, 'cover', u"⌀{:.0f}: защитный слой {:.0f} мм < {:.0f} мм".format(
                float(d), worst_cover, float(need))))
        limit = _spacing_limit(doc, host, rb, transverse)
        gap = _max_gap(rb) if limit else None
        if gap is not None and gap > limit + 1:
            issues.append(Issue(rb, title, 'spacing', u"⌀{:.0f}: шаг {:.0f} мм > {:.0f} мм".format(float(d), gap, float(limit))))
    return issues


# ----------------------------------------------------------
# МАРКИ И ВЕДОМОСТЬ РАСХОДА СТАЛИ
# ----------------------------------------------------------
def bar_length_mm(rb):
    """Длина одного стержня (мм): у набора — средняя."""
    n = bar_count(rb)
    return mm(rb.TotalLength) / n if n else 0.0


def mark_key(doc, rb):
    """Одинаковые стержни — одна марка: конструкция, тип стержня, форма, длина (с точностью 5 мм)."""
    bt = bar_type(doc, rb)
    shape = doc.GetElement(rb.GetShapeId())
    return (construction(doc, rb), kr.bar_diameter_mm(bt) or 0.0, steel_class(bt),
            element_name(shape) if shape is not None else u"", int(round(bar_length_mm(rb) / 5.0)) * 5)


def plan_marks(doc, rebars):
    """{ключ марки: (марка, [Rebar])}. Нумерация в пределах конструкции: по убыванию диаметра и длины."""
    groups = {}
    for rb in rebars:
        groups.setdefault(mark_key(doc, rb), []).append(rb)
    out, counters = {}, {}
    for key in sorted(groups, key=lambda k: (k[0], -k[1], k[2], -k[4], k[3])):
        counters[key[0]] = counters.get(key[0], 0) + 1
        out[key] = (u"{}".format(counters[key[0]]), groups[key])
    return out


def mark_param_ready(rebars):
    """Привязан ли АрмМарка к арматуре (проверка по первому стержню)."""
    return bool(rebars) and rebars[0].LookupParameter(params.PRM_MARK) is not None


def apply_marks(doc, marks):
    """Записывает АрмМарка. Только внутри транзакции. Возвращает число изменённых стержней."""
    changed = 0
    for mark, rbs in marks.values():
        for rb in rbs:
            p = rb.LookupParameter(params.PRM_MARK)
            if p is None or p.IsReadOnly or (p.AsString() or u"") == mark:
                continue
            p.Set(mark)
            changed += 1
    return changed


class ScheduleRow(object):
    def __init__(self, part, cls, d, count, length_m, mass_kg, total=False):
        self.Part = part
        self.Class = cls
        self.D = u"⌀{:.0f}".format(float(d)) if d else u""
        self.d = d
        self.Count = u"{}".format(count)
        self.count = count
        self.Length = u"{:.1f}".format(length_m)
        self.length = length_m
        self.Mass = u"{:.1f}".format(mass_kg)
        self.mass = mass_kg
        self.FontWeight = u"Bold" if total else u"Normal"


def schedule(doc, rebars):
    """Ведомость: строки «конструкция × класс × диаметр» (шт с учётом АрмЧисло, м, кг) и итоги по конструкциям."""
    acc = {}
    for rb in rebars:
        bt = bar_type(doc, rb)
        d = kr.bar_diameter_mm(bt) or 0.0
        key = (construction(doc, rb), steel_class(bt), d)
        k = arm_count(rb)
        row = acc.setdefault(key, [0, 0.0])
        row[0] += bar_count(rb) * k
        row[1] += mm(rb.TotalLength) / 1000.0 * k
    rows, total = [], 0.0
    for part in sorted(set(k[0] for k in acc)):
        part_mass = 0.0
        for key in sorted((k for k in acc if k[0] == part), key=lambda k: (k[1], k[2])):
            count, length = acc[key]
            mass = length * (norms.mass_per_m(key[2]) or 0.0)
            part_mass += mass
            rows.append(ScheduleRow(part, key[1], key[2], count, length, mass))
        rows.append(ScheduleRow(u"{} — итого".format(part), u"", 0, sum(acc[k][0] for k in acc if k[0] == part),
                                sum(acc[k][1] for k in acc if k[0] == part), part_mass, True))
        total += part_mass
    return rows, total


def write_csv(path, rows, total):
    """CSV для Excel: разделитель «;», UTF-8 с BOM, десятичная запятая."""
    import io

    def num(text):
        return text.replace(u".", u",")
    lines = [u"Конструкция;Класс;Диаметр, мм;Количество, шт;Длина, м;Масса, кг"]
    for r in rows:
        lines.append(u";".join([r.Part, r.Class, u"{:.0f}".format(float(r.d)) if r.d else u"", r.Count,
                               num(r.Length), num(r.Mass)]))
    lines.append(u"Всего;;;;;{}".format(num(u"{:.1f}".format(total))))
    with io.open(path, "w", encoding="utf-8-sig") as f:
        f.write(u"\r\n".join(lines) + u"\r\n")


# ----------------------------------------------------------
# ОТПЕЧАТОК ГЕОМЕТРИИ ОСНОВЫ (EXTENSIBLE STORAGE)
# ----------------------------------------------------------
SCHEMA_GUID = System.Guid("6f3c2a1e-8b4d-4e7a-9c21-5d0b7e4a3f18")
FIELD = "Signature"


def _schema():
    schema = Schema.Lookup(SCHEMA_GUID)
    if schema is not None:
        return schema
    sb = SchemaBuilder(SCHEMA_GUID)
    sb.SetSchemaName("KzhHostSignature")
    sb.SetReadAccessLevel(AccessLevel.Public)
    sb.SetWriteAccessLevel(AccessLevel.Public)
    sb.AddSimpleField(FIELD, System.String)
    return sb.Finish()


def signature(host):
    """Отпечаток основы: тип, объём (см³), габариты (мм), число вставок (проёмов, окон, дверей)."""
    solids = element_solids(host)
    vol = int(round(sum(s.Volume for s in solids) * (304.8 ** 3) / 1000.0))
    bb = host.get_BoundingBox(None)
    box = u",".join(u"{:.0f}".format(mm(v)) for v in (bb.Min.X, bb.Min.Y, bb.Min.Z, bb.Max.X, bb.Max.Y, bb.Max.Z)) if bb else u""
    inserts = 0
    if isinstance(host, (DB.Wall, DB.Floor)):
        try:
            inserts = len(list(host.FindInserts(True, True, True, True)))
        except Exception:
            inserts = 0
    return u"{}|{}|{}|{}".format(host.GetTypeId().IntegerValue, vol, box, inserts)


def store_signature(host):
    """Записывает отпечаток в основу. Только внутри транзакции."""
    schema = _schema()
    ent = Entity(schema)
    ent.Set[System.String](FIELD, signature(host))
    host.SetEntity(ent)


def stored_signature(host):
    schema = Schema.Lookup(SCHEMA_GUID)
    if schema is None:
        return None
    ent = host.GetEntity(schema)
    if ent is None or not ent.IsValid():
        return None
    return ent.Get[System.String](FIELD)


class HostRow(object):
    """Основа с арматурой: состояние отпечатка и кнопка, которой её армировать заново."""

    def __init__(self, host, rebar_count):
        self.host = host
        self.Title = u"{} (id {})".format(element_name(host), host.Id.IntegerValue)
        self.cmd, self.Button = button_for(host)
        self.Category = host.Category.Name if host.Category is not None else u"—"
        self.Rebar = u"{}".format(rebar_count)
        old = stored_signature(host)
        if old is None:
            self.state = 'none'
            text, kind = u"Нет отпечатка (армировано раньше)", 'warn'
        elif old != signature(host):
            self.state = 'changed'
            text, kind = u"Изменена после армирования", 'err'
        else:
            self.state = 'ok'
            text, kind = u"Без изменений", 'ok'
        self.StatusText = text
        self.StatusBg, self.StatusFg = params.STATUS_COLORS[kind]
        self.IsChecked = self.state == 'changed' and self.cmd is not None


def host_rows(doc, rebars):
    counts = {}
    for rb in rebars:
        hid = rb.GetHostId()
        counts[hid.IntegerValue] = counts.get(hid.IntegerValue, 0) + 1
    rows = []
    for hid, n in counts.items():
        host = doc.GetElement(DB.ElementId(hid))
        if host is not None:
            rows.append(HostRow(host, n))
    order = {'changed': 0, 'none': 1, 'ok': 2}
    return sorted(rows, key=lambda r: (order[r.state], r.Category, r.Title))

# -*- coding: utf-8 -*-
"""Создание системной арматуры Revit: типы стержней, формы по ролям, наборы, транзакции.

Формы — гибрид: если в таблице ролей задано имя формы и она есть в проекте, стержень создаётся
по ней (Rebar.CreateFromCurvesAndShape); иначе Rebar.CreateFromCurves подбирает подходящую
форму проекта или один раз создаёт новую (useExistingShapeIfPossible=True, createNewShape=True).
"""
import math

from System.Collections.Generic import List
from pyrevit import DB

from kzh import element_name, norms, settings
from kzh.geometry import ft, mm

ST = DB.Structure

STYLE_MAIN = ST.RebarStyle.Standard
STYLE_STIRRUP = ST.RebarStyle.StirrupTie


# ----------------------------------------------------------
# ТИПЫ СТЕРЖНЕЙ
# ----------------------------------------------------------
def bar_diameter_mm(bar_type):
    # Revit 2022: BarDiameter; в 2023+ свойство заменено на BarNominalDiameter
    for attr in ('BarNominalDiameter', 'BarDiameter'):
        try:
            return mm(getattr(bar_type, attr))
        except Exception:
            pass
    p = bar_type.get_Parameter(DB.BuiltInParameter.REBAR_BAR_DIAMETER)
    return mm(p.AsDouble()) if p is not None else None


def _norm_class(text):
    """«A500C» латиницей и «А500С» кириллицей считаем одинаковыми."""
    return (text or u"").upper().replace(u"A", u"А").replace(u"C", u"С").replace(u"B", u"В").replace(u" ", u"")


def bar_type_name(d, steel):
    return u"{} {}".format(int(d), steel)


def find_bar_type(doc, d, steel):
    """Тип стержня нужного диаметра; при нескольких — тот, в имени которого есть класс."""
    found = []
    for bt in DB.FilteredElementCollector(doc).OfClass(ST.RebarBarType):
        dm = bar_diameter_mm(bt)
        if dm is not None and abs(dm - d) < 0.05:
            found.append(bt)
    if not found:
        return None
    cls = _norm_class(steel)
    with_class = [bt for bt in found if cls in _norm_class(element_name(bt))]
    pool = with_class or found
    return sorted(pool, key=lambda bt: element_name(bt))[0]


def create_bar_type(doc, d, steel):
    """Новый тип стержня с диаметром оправки по norms.json. Только внутри транзакции."""
    bt = ST.RebarBarType.Create(doc)
    name = bar_type_name(d, steel)
    existing = set(element_name(t) for t in DB.FilteredElementCollector(doc).OfClass(ST.RebarBarType))
    suffix = 1
    while name in existing:
        suffix += 1
        name = u"{} ({})".format(bar_type_name(d, steel), suffix)
    bt.Name = name
    try:
        # Revit 2023+
        bt.BarNominalDiameter = ft(d)
        bt.BarModelDiameter = ft(d)
    except Exception:
        bt.BarDiameter = ft(d)
    bend = ft(norms.bend_diameter(d))
    for attr in ('StandardBendDiameter', 'StandardHookBendDiameter', 'StirrupTieBendDiameter'):
        try:
            setattr(bt, attr, bend)
        except Exception:
            pass
    return bt


# ----------------------------------------------------------
# ФОРМЫ И ОТГИБЫ
# ----------------------------------------------------------
def shapes_by_name(doc):
    return dict((element_name(s), s) for s in DB.FilteredElementCollector(doc).OfClass(ST.RebarShape))


def role_shapes(doc):
    """{роль: RebarShape или None}: None — автоподбор (имя не задано или формы нет в проекте)."""
    shapes = shapes_by_name(doc)
    return dict((role, shapes.get(name) if name else None) for role, name in settings.shape_roles().items())


def hook_types(doc):
    return sorted(DB.FilteredElementCollector(doc).OfClass(ST.RebarHookType),
                  key=lambda h: element_name(h))


def hook_by_angle(doc, angle, style):
    """Тип отгиба проекта с углом angle (градусы) и стилем style; None — такого нет или angle = 0."""
    if not angle:
        return None
    for h in hook_types(doc):
        try:
            if abs(math.degrees(h.HookAngle) - angle) < 1.0 and h.Style == style:
                return h
        except Exception:
            pass
    return None


def default_stirrup_hook(doc):
    """Отгиб 135° для хомутов, если такой тип есть в проекте."""
    for h in hook_types(doc):
        try:
            if abs(h.HookAngle - 2.356194) < 0.02 and h.Style == STYLE_STIRRUP:
                return h
        except Exception:
            pass
    return None


def allow_hook(bar_type, hook):
    """Разрешает отгиб для типа стержня (без этого Revit не создаст стержень с отгибом)."""
    if hook is None:
        return
    try:
        if not bar_type.GetHookPermission(hook.Id):
            bar_type.SetHookPermission(hook.Id, True)
    except Exception:
        pass


# ----------------------------------------------------------
# СТЕРЖНИ И НАБОРЫ
# ----------------------------------------------------------
def polyline(points):
    """Отрезки между соседними точками (нулевые пропускаются)."""
    out = []
    for a, b in zip(points[:-1], points[1:]):
        if a.DistanceTo(b) > 1e-6:
            out.append(DB.Line.CreateBound(a, b))
    return out


def create_bar(doc, host, style, bar_type, curves, norm, shape=None, hook=None):
    """Стержень по осевым линиям curves в плоскости с нормалью norm. Возвращает Rebar.
    Отгибы (если заданы) — с обеих сторон, внутрь контура при обходе против часовой стрелки."""
    curve_list = List[DB.Curve](curves)
    orient = ST.RebarHookOrientation.Left
    if shape is not None:
        try:
            rb = ST.Rebar.CreateFromCurvesAndShape(doc, shape, bar_type, hook, hook, host, norm, curve_list,
                                                   orient, orient)
            if rb is not None:
                return rb
        except Exception:
            pass    # форма из таблицы ролей не подошла к геометрии — подбираем автоматически
    rb = ST.Rebar.CreateFromCurves(doc, style, bar_type, hook, hook, host, norm, curve_list,
                                   orient, orient, True, True)
    if rb is None:
        raise Exception(u"Revit не создал стержень (геометрия не подошла ни к одной форме)")
    return rb


def layout_fixed(rb, count, length):
    """Набор из count стержней на длине length вдоль нормали."""
    if count > 1:
        rb.GetShapeDrivenAccessor().SetLayoutAsFixedNumber(count, length, True, True, True)


def layout_spacing(rb, spacing, length, first=True, last=True):
    """Набор с шагом не более spacing на длине length вдоль нормали."""
    rb.GetShapeDrivenAccessor().SetLayoutAsMaximumSpacing(spacing, length, True, first, last)


def spacing_zones(start, end, dense_start, dense_end, dense_step, normal_step):
    """Зоны раскладки поперечных стержней на отрезке [start, end] (футы): учащение длиной dense_start
    у начала и dense_end у конца, между ними обычный шаг. Если места мало — весь отрезок с учащением.
    Возвращает [(начало, длина, шаг, первый, последний)]: средняя зона без крайних стержней,
    чтобы они не совпали со стержнями соседних зон."""
    total = end - start
    if dense_start + dense_end + normal_step >= total:
        return [(start, total, dense_step, True, True)]
    return [(start, dense_start, dense_step, True, True),
            (start + dense_start, total - dense_start - dense_end, normal_step, False, False),
            (end - dense_end, dense_end, dense_step, True, True)]


def zone_count(length, step, first, last):
    """Сколько стержней даст SetLayoutAsMaximumSpacing на длине length."""
    return int(math.ceil(length / step - 1e-6)) + 1 - (0 if first else 1) - (0 if last else 1)


# ----------------------------------------------------------
# ЗОНЫ ХОМУТОВ, ЗАДАННЫЕ ПОЛЬЗОВАТЕЛЕМ (колонны — по высоте, балки — по длине)
# ----------------------------------------------------------
def zone_layout(zones, z0, z3):
    """Фактические зоны на отрезке [z0, z3] (мм от начала элемента): [(начало, длина, шаг, Ø, первый, последний)].
    Зона с пустой длиной получает остаток высоты; зоны выше конца отрезка отбрасываются."""
    total = z3 - z0
    fixed = sum(z["len"] for z in zones if z["len"])
    rest = max(0.0, total - fixed)
    out, z = [], z0
    for zone in zones:
        length = zone["len"] if zone["len"] else rest
        length = min(length, z3 - z)
        if length <= 1e-6:
            continue
        out.append((z, length, zone["step"], zone["d"], not out, True))
        z += length
    if z < z3 - 1e-6 and out:
        # Зоны короче высоты и нет зоны «остаток» — последняя продлевается до верха
        s, length, step, d, first, last = out[-1]
        out[-1] = (s, z3 - s, step, d, first, last)
    return out


def cut_intervals(intervals, holes):
    """Отрезки intervals [(a, b)] за вычетом holes [(a, b)]."""
    out = list(intervals)
    for ha, hb in holes:
        nxt = []
        for a, b in out:
            if hb <= a or ha >= b:
                nxt.append((a, b))
                continue
            if ha > a:
                nxt.append((a, ha))
            if hb < b:
                nxt.append((hb, b))
        out = nxt
    return [(a, b) for a, b in out if b - a > 1e-6]


def bars_in_set(rb):
    try:
        return rb.Quantity
    except Exception:
        return 1


# ----------------------------------------------------------
# ПЛАН АРМИРОВАНИЯ ЭЛЕМЕНТА: СПИСОК СТЕРЖНЕЙ И НАБОРОВ
# ----------------------------------------------------------
TRANSVERSE_ROLES = ("stirrup", "pin", "link")    # link — поперечный стержень плоского каркаса


class BarSpec(object):
    """Стержень или набор: роль (ключ из settings.SHAPE_ROLES), диаметр, осевые линии первого стержня,
    нормаль плоскости и раскладка вдоль нормали.
    layout: None — одиночный; ("fixed", n, длина); ("spacing", шаг, длина, первый, последний)."""

    def __init__(self, role, d, curves, norm, layout=None, count=1):
        self.role = role
        self.d = d
        self.curves = curves
        self.norm = norm
        self.layout = layout
        self.count = count
        self.arm_count = 1      # значение АрмЧисло: один элемент вместо N одинаковых (лягушки плит)
        self.steel = None       # класс детали из конфигуратора (None — класс кнопки по роли)
        self.hook_angle = None  # крюк детали: угол в градусах (0 — без крюков; None — отгиб кнопки по роли)
        self.hook_len = None    # длина крюка детали, мм (None — по типу стержня Revit)

    @property
    def transverse(self):
        return self.role in TRANSVERSE_ROLES


def type_key(spec, steel_long, steel_trans):
    if spec.steel:
        return (spec.d, spec.steel)
    return (spec.d, steel_trans if spec.transverse else steel_long)


def needed_bar_types(doc, specs, steel_long, steel_trans):
    """{(диаметр, класс): тип стержня или None, если его нужно создать}."""
    keys = set(type_key(s, steel_long, steel_trans) for s in specs)
    return dict((k, find_bar_type(doc, k[0], k[1])) for k in keys)


def create_missing_bar_types(doc, types):
    for key in sorted(types):
        if types[key] is None:
            types[key] = create_bar_type(doc, key[0], key[1])


def build_specs(doc, host, specs, types, steel_long, steel_trans, shapes, hooks):
    """Создаёт стержни по плану. hooks: {роль: тип отгиба или None}. Возвращает (элементов, стержней)."""
    created, quantity = [], 0
    for spec in specs:
        bt = types[type_key(spec, steel_long, steel_trans)]
        style = STYLE_STIRRUP if spec.role in ("stirrup", "pin") else STYLE_MAIN
        hook = hooks.get(spec.role) if spec.hook_angle is None else hook_by_angle(doc, spec.hook_angle, style)
        allow_hook(bt, hook)
        if hook is not None and spec.hook_len:
            # Длина крюка детали: в Revit задаётся у типа стержня для этого типа крюка
            bt.SetAutoCalcHookLengths(hook.Id, False)
            bt.SetHookLength(hook.Id, ft(spec.hook_len))
        rb = create_bar(doc, host, style, bt, spec.curves, spec.norm, shapes.get(spec.role), hook)
        if spec.layout is not None:
            if spec.layout[0] == "fixed":
                layout_fixed(rb, spec.layout[1], spec.layout[2])
            else:
                layout_spacing(rb, *spec.layout[1:])
        created.append(rb)
        quantity += bars_in_set(rb)
    # Revit при создании притягивает стержни к своему защитному слою и к соседним стержням — возвращаем
    # на задуманное место; регенерация — одна на весь план, а не на каждый стержень
    if created:
        doc.Regenerate()
        if sum(1 for rb in created if detach_from_rebar(rb)):
            doc.Regenerate()
        for rb, spec in zip(created, specs):
            restore_planned(rb, spec)
    return created, quantity


def _bar_points(rb):
    curves = rb.GetCenterlineCurves(False, True, True, ST.MultiplanarOption.IncludeOnlyPlanarCurves, 0)
    return [curves[0].GetEndPoint(0)] + [c.GetEndPoint(1) for c in curves]


def detach_from_rebar(rb):
    """Ручки, которые Revit привязал к соседним стержням (шпилькам, хомутам), переводятся на грань основы:
    иначе стержень прижимается к соседнему, а не стоит, где задуман. True — что-то изменено (нужна регенерация)."""
    m = rb.GetRebarConstraintsManager()
    switched = False
    for h in m.GetAllHandles():
        c = m.GetCurrentConstraintOnHandle(h)
        if c is None or not c.IsToOtherRebar():
            continue
        for cand in m.GetConstraintCandidatesForHandle(h):
            if cand.IsToHostFaceOrCover() and cand.NumberOfTargets == 1:
                try:
                    m.SetPreferredConstraintForHandle(h, cand)
                    switched = True
                    break
                except Exception:
                    pass
    return switched


def restore_planned(rb, spec):
    """Возвращает стержень на задуманное место, если Revit притянул его ручки к своему защитному слою
    (концы у грани, крайние участки): у ручек с привязкой к грани или защитному слою меняется отступ
    на разницу «план − факт» по нормали этой грани. Нужна регенерация до вызова. Возвращает число исправлений."""
    want = [spec.curves[0].GetEndPoint(0)] + [c.GetEndPoint(1) for c in spec.curves]
    m = rb.GetRebarConstraintsManager()
    got = _bar_points(rb)
    if len(want) != len(got):
        return 0
    fixed = 0
    for h in m.GetAllHandles():
        c = m.GetCurrentConstraintOnHandle(h)
        if c is None or not c.IsToHostFaceOrCover() or c.NumberOfTargets != 1:
            continue
        kind = h.GetHandleType()
        if kind == ST.RebarHandleType.StartOfBar:
            idx = [0]
        elif kind == ST.RebarHandleType.EndOfBar:
            idx = [len(want) - 1]
        elif kind == ST.RebarHandleType.Edge:
            k = h.GetEdgeNumber()
            if k + 1 >= len(want):
                continue
            idx = [k, k + 1]
        elif kind == ST.RebarHandleType.RebarPlane:
            idx = [0]
        else:
            continue
        tr = DB.Transform.Identity
        face = c.GetTargetHostFaceAndTransform(0, tr)
        if not isinstance(face, DB.PlanarFace):
            continue
        n = tr.OfVector(face.FaceNormal).Normalize()
        o = tr.OfPoint(face.Origin)
        # Глубина от грани внутрь бетона: у плана и у факта
        s_want = min((o - want[i]).DotProduct(n) for i in idx)
        s_got = min((o - got[i]).DotProduct(n) for i in idx)
        delta = s_want - s_got
        if abs(delta) < ft(0.5):
            continue
        try:
            if c.IsToCover():
                c.SetDistanceToTargetCover(c.GetDistanceToTargetCover() - delta)
            else:
                c.SetDistanceToTargetHostFace(c.GetDistanceToTargetHostFace() - delta)
            m.SetPreferredConstraintForHandle(h, c)
            fixed += 1
        except Exception:
            pass        # отступ меньше допустимого Revit — стержень остаётся, как поставил Revit
    return fixed


# ----------------------------------------------------------
# ОСНОВА (ХОСТ)
# ----------------------------------------------------------
def is_valid_host(element):
    try:
        return ST.RebarHostData.IsValidHost(element)
    except Exception:
        return ST.RebarHostData.GetRebarHostData(element) is not None


def host_rebar_ids(host):
    data = ST.RebarHostData.GetRebarHostData(host)
    if data is None:
        return []
    return [r.Id for r in data.GetRebarsInHost()]


# ----------------------------------------------------------
# ЗАЩИТНЫЙ СЛОЙ ОСНОВЫ (НАСТРОЙКА REVIT)
# ----------------------------------------------------------
# Revit привязывает стержень к своему защитному слою основы: стержень ближе к грани, чем этот слой, при создании
# сдвигается внутрь (наружный слой сетки наезжает на внутренний, хомут — на продольные стержни). Поэтому перед
# созданием защитный слой основы уменьшается до наименьшего, который реально есть в армировании элемента.
COVER_PARAMS = (DB.BuiltInParameter.CLEAR_COVER_EXTERIOR, DB.BuiltInParameter.CLEAR_COVER_INTERIOR,
                DB.BuiltInParameter.CLEAR_COVER_TOP, DB.BuiltInParameter.CLEAR_COVER_BOTTOM,
                DB.BuiltInParameter.CLEAR_COVER_OTHER, DB.BuiltInParameter.CLEAR_COVER)


def host_cover_changes(doc, host, min_cover):
    """[(параметр, было мм)] — защитные слои основы больше min_cover (их нужно уменьшить)."""
    out = []
    for bip in COVER_PARAMS:
        p = host.get_Parameter(bip)
        if p is None or p.IsReadOnly:
            continue
        ct = doc.GetElement(p.AsElementId())
        if ct is not None and mm(ct.CoverDistance) > min_cover + 0.5:
            out.append((bip, mm(ct.CoverDistance)))
    return out


def cover_type(doc, distance):
    """Тип защитного слоя Revit на distance мм: существующий или новый «КЖ N мм». Только внутри транзакции."""
    for ct in DB.FilteredElementCollector(doc).OfClass(ST.RebarCoverType):
        if abs(mm(ct.CoverDistance) - distance) < 0.5:
            return ct
    names = set(element_name(ct) for ct in DB.FilteredElementCollector(doc).OfClass(ST.RebarCoverType))
    name = u"КЖ {:.0f} мм".format(float(distance))
    k = 1
    while name in names:
        k += 1
        name = u"КЖ {:.0f} мм ({})".format(float(distance), k)
    return ST.RebarCoverType.Create(doc, name, ft(distance))


def min_lateral_cover(host, specs):
    """Наименьший защитный слой стержней плана сбоку (мм): от поверхности стержня до грани основы, параллельной
    участку стержня. Замер — в середине каждого участка первого и последнего стержня набора, только внутри бетона
    основы. Концы стержней Revit не двигает, поэтому они не учитываются. None — замерить нечего."""
    from kzh.geometry import element_solids
    faces = []
    for solid in element_solids(host):
        for f in solid.Faces:
            if isinstance(f, DB.PlanarFace):
                faces.append(f)
    opts = DB.SolidCurveIntersectionOptions()
    solids = element_solids(host)
    best = None
    for spec in specs:
        shifts = [DB.XYZ.Zero]
        if spec.layout is not None and spec.layout[0] == "fixed":
            shifts.append(spec.norm.Normalize() * spec.layout[2])
        for shift in shifts:
            for c in spec.curves:
                a, b = c.GetEndPoint(0) + shift, c.GetEndPoint(1) + shift
                d = b - a
                if d.GetLength() < ft(20):
                    continue
                u = d.Normalize()
                m = (a + b) * 0.5
                probe = DB.Line.CreateBound(m - u * ft(1), m + u * ft(1))
                if not any(s2.IntersectWithCurve(probe, opts).SegmentCount for s2 in solids):
                    continue          # середина участка вне бетона основы (выпуск, заведение в соседний элемент)
                for f in faces:
                    if abs(f.FaceNormal.DotProduct(u)) > 0.05:
                        continue
                    res = f.Project(m)
                    if res is None:
                        continue
                    cover = mm(res.Distance) - spec.d / 2.0
                    if cover >= -0.5 and (best is None or cover < best):
                        best = cover
    return best


def lower_host_cover(doc, host, min_cover):
    """Уменьшает защитные слои основы до min_cover (мм, вниз до целого). Только внутри транзакции."""
    changes = host_cover_changes(doc, host, min_cover)
    if not changes:
        return
    ct = cover_type(doc, math.floor(min_cover + 1e-6))
    for bip, _ in changes:
        host.get_Parameter(bip).Set(ct.Id)


# ----------------------------------------------------------
# ТРАНЗАКЦИЯ БЕЗ ВСПЛЫВАЮЩИХ ПРЕДУПРЕЖДЕНИЙ
# ----------------------------------------------------------
class WarningCollector(DB.IFailuresPreprocessor):
    """Собирает предупреждения Revit в список (для отчёта) и убирает их окна. Ошибки не трогает."""

    def __init__(self):
        self.messages = []

    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            if failure.GetSeverity() == DB.FailureSeverity.Warning:
                text = failure.GetDescriptionText()
                if text not in self.messages:
                    self.messages.append(text)
                accessor.DeleteWarning(failure)
        return DB.FailureProcessingResult.Continue


def start_transaction(doc, name):
    """Запущенная транзакция и сборщик предупреждений."""
    t = DB.Transaction(doc, name)
    collector = WarningCollector()
    opts = t.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(collector)
    t.SetFailureHandlingOptions(opts)
    t.Start()
    return t, collector

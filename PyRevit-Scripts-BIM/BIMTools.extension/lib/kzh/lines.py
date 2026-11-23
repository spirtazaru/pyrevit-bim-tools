# -*- coding: utf-8 -*-
"""Стили линий КЖ и толщины линий на виде.

Стили КЖ — подкатегории категории «Линии» (Revit: стили линий) с весами по ГОСТ 2.303 и ГОСТ 21.501:
арматура — сплошная толстая основная, контур бетона — сплошная тонкая, выноски и размеры — тонкие,
оси — штрихпунктирная тонкая. Вес — номер пера Revit (1…16): фактическая толщина зависит от масштаба
вида и таблицы «Толщины линий» проекта (Управление → Дополнительные параметры).
"""
from pyrevit import DB

# (имя стиля, вес пера, имя образца линии или None — сплошная, назначение)
KZH_STYLES = (
    (u"КЖ_Контур бетона", 2, None, u"контур железобетонного элемента — сплошная тонкая"),
    (u"КЖ_Стержень", 6, None, u"рабочая арматура — сплошная толстая основная"),
    (u"КЖ_Хомут", 4, None, u"хомуты, шпильки, детали — сплошная основная"),
    (u"КЖ_Выноска", 1, None, u"линии-выноски позиций — сплошная тонкая"),
    (u"КЖ_Размер", 1, None, u"выносные и размерные линии, нарисованные линиями"),
    (u"КЖ_Ось", 1, u"Осевая", u"оси — штрихпунктирная тонкая"),
    (u"КЖ_Таблица рамка", 5, None, u"рамка и линии заголовка таблиц — основная"),
    (u"КЖ_Таблица", 1, None, u"строки таблиц — тонкая"),
)
PROJECTION = DB.GraphicsStyleType.Projection


def lines_category(doc):
    return doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)


def _pattern_id(doc, name):
    """Образец линии по имени (без учёта регистра, по началу имени); сплошная — если нет."""
    if name:
        for el in DB.FilteredElementCollector(doc).OfClass(DB.LinePatternElement):
            if el.Name.lower().startswith(name.lower()):
                return el.Id
    return DB.LinePatternElement.GetSolidPatternId()


def missing_styles(doc):
    """Имена стилей КЖ, которых ещё нет в проекте."""
    have = set(c.Name for c in lines_category(doc).SubCategories)
    return [s[0] for s in KZH_STYLES if s[0] not in have]


def ensure_styles(doc):
    """Создаёт недостающие стили КЖ (только внутри транзакции). Существующие не меняет — их вес
    пользователь мог настроить сам. Возвращает {имя: GraphicsStyle}."""
    cat = lines_category(doc)
    subs = dict((c.Name, c) for c in cat.SubCategories)
    for name, weight, pattern, _ in KZH_STYLES:
        if name in subs:
            continue
        sub = doc.Settings.Categories.NewSubcategory(cat, name)
        sub.SetLineWeight(weight, PROJECTION)
        sub.LineColor = DB.Color(0, 0, 0)
        sub.SetLinePatternId(_pattern_id(doc, pattern), PROJECTION)
        subs[name] = sub
    return dict((name, subs[name].GetGraphicsStyle(PROJECTION)) for name, _, _, _ in KZH_STYLES if name in subs)


def styles(doc):
    """{имя: GraphicsStyle} существующих стилей КЖ (без создания)."""
    out = {}
    for c in lines_category(doc).SubCategories:
        if c.Name in [s[0] for s in KZH_STYLES]:
            out[c.Name] = c.GetGraphicsStyle(PROJECTION)
    return out


class LineRow(object):
    """Строка окна «Линии на виде»: стиль линий, сколько линий этого стиля на виде, вес пера."""

    def __init__(self, doc, category, count):
        self.category = category
        self.Name = category.Name
        self.Count = u"{}".format(count)
        self.weight = category.GetLineWeight(PROJECTION) or 1
        self.new_weight = self.weight
        color = category.LineColor
        self.Color = u"#FF{:02X}{:02X}{:02X}".format(color.Red, color.Green, color.Blue) if color.IsValid else u"#FF000000"
        pid = category.GetLinePatternId(PROJECTION)
        pat = doc.GetElement(pid) if pid is not None and pid.IntegerValue > 0 else None
        self.Pattern = pat.Name if pat is not None else u"Сплошная"
        self.IsKzh = category.Name.startswith(u"КЖ_")


def view_line_rows(doc, view):
    """Стили линий, которыми нарисованы линии детализации и модели на виде view, — по числу линий."""
    counts = {}
    for ce in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.CurveElement):
        try:
            gs = ce.LineStyle
        except Exception:
            continue
        if gs is None:
            continue
        cat = gs.GraphicsStyleCategory
        if cat is None:
            continue
        key = cat.Id.IntegerValue
        if key not in counts:
            counts[key] = [cat, 0]
        counts[key][1] += 1
    rows = [LineRow(doc, cat, n) for cat, n in counts.values()]
    return sorted(rows, key=lambda r: (not r.IsKzh, r.Name))


def all_style_rows(doc):
    """Все стили линий проекта (подкатегории «Линии»): для общей настройки, без подсчёта на виде."""
    rows = [LineRow(doc, c, 0) for c in lines_category(doc).SubCategories]
    for r in rows:
        r.Count = u"—"
    return sorted(rows, key=lambda r: (not r.IsKzh, r.Name))


def apply_weights(rows):
    """Записывает новые веса (только внутри транзакции). Возвращает число изменённых стилей."""
    changed = 0
    for r in rows:
        if r.new_weight != r.weight:
            r.category.SetLineWeight(int(r.new_weight), PROJECTION)
            r.weight = r.new_weight
            changed += 1
    return changed

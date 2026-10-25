# -*- coding: utf-8 -*-
"""Параметры КЖ: привязка из ФОП пользователя к арматуре и группам модели.

Подход как в «Схемах СС»: параметры берутся из файла общих параметров пользователя;
недостающие определения можно один раз дописать в этот ФОП (после подтверждения).

АрмЧисло — число одинаковых каркасов/стержней, представленных одним элементом
(один каркас или лягушка + число). В спецификации: «Количество × АрмЧисло».
"""
from pyrevit import DB

from kzh.ui import show_message

PRM_COUNT = u"АрмЧисло"
PRM_MARK = u"АрмМарка"
PRM_PART = u"АрмКонструкция"

# (имя, данные "int"/"text", назначение). Все — параметры экземпляра
PARAM_SPECS = (
    (PRM_COUNT, "int", u"Число одинаковых каркасов/стержней: в спецификации Количество × АрмЧисло"),
    (PRM_MARK, "text", u"Марка стержня или каркаса"),
    (PRM_PART, "text", u"Конструкция (Колонна, Плита, Стена…) — для фильтров спецификаций"),
)
FOP_GROUP_NAME = u"КЖ"

# Арматура и группы модели (сварные каркасы собираются в группы)
TARGET_CATEGORY_NAMES = ("OST_Rebar", "OST_IOSModelGroups")

STATUS_COLORS = {
    'ok': ("#1A1E8E3E", "#FF1E7A34"),
    'warn': ("#1AFF7A1A", "#FFC24E00"),
    'err': ("#1AE8452C", "#FFA8241A"),
}


class ParamRow(object):
    def __init__(self, spec):
        self.Name, self.data_kind, self.Description = spec
        self.DataText = u"Целое" if self.data_kind == "int" else u"Текст"
        self.BindText = u"Экземпляр"
        self.in_fop = None          # None — ФОП не указан или не прочитан
        self.state = 'none'         # none | ok | partial | type
        self.missing_cats = []
        self.FopText = u"—"
        self.set_status(u"Не привязан", 'err')

    def set_status(self, text, kind):
        self.ProjectText = text
        self.StatusBg, self.StatusFg = STATUS_COLORS[kind]


def target_categories(doc):
    """(категории, которые можно привязать, имена категорий, которые Revit не разрешает)."""
    cats, refused = [], []
    for name in TARGET_CATEGORY_NAMES:
        bic = getattr(DB.BuiltInCategory, name, None)
        cat = None
        if bic is not None:
            try:
                cat = doc.Settings.Categories.get_Item(bic)
            except Exception:
                cat = None
        if cat is not None and cat.AllowsBoundParameters:
            cats.append(cat)
        else:
            refused.append(cat.Name if cat is not None else name)
    return cats, refused


def project_bindings(doc):
    """{имя параметра: (определение, привязка)} для параметров проекта."""
    out = {}
    it = doc.ParameterBindings.ForwardIterator()
    it.Reset()
    while it.MoveNext():
        out[it.Key.Name] = (it.Key, it.Current)
    return out


def read_fop_definitions(app, fop_path):
    """Открывает ФОП и возвращает (файл, {имя: определение}) для параметров КЖ.
    Меняет app.SharedParametersFilename — вызывающий обязан вернуть прежнее значение."""
    app.SharedParametersFilename = fop_path
    fop = app.OpenSharedParameterFile()
    if fop is None:
        raise Exception(u"Не удалось открыть ФОП: {}".format(fop_path))
    names = set(spec[0] for spec in PARAM_SPECS)
    defs = {}
    for group in fop.Groups:
        for d in group.Definitions:
            if d.Name in names and d.Name not in defs:
                defs[d.Name] = d
    return fop, defs


def restore_fop(app, old_path):
    try:
        app.SharedParametersFilename = old_path or u""
    except Exception:
        pass


def analyze_params(doc, app, fop_path):
    """Состояние каждого параметра: есть ли в ФОП, привязан ли к проекту и ко всем ли категориям."""
    cats, refused = target_categories(doc)
    bindings = project_bindings(doc)
    fop_defs, fop_error = None, None
    if fop_path:
        old = app.SharedParametersFilename
        try:
            _, fop_defs = read_fop_definitions(app, fop_path)
        except Exception as ex:
            fop_error = u"{}".format(ex)
        finally:
            restore_fop(app, old)

    rows = []
    for spec in PARAM_SPECS:
        row = ParamRow(spec)
        if fop_defs is not None:
            row.in_fop = row.Name in fop_defs
            row.FopText = u"Есть" if row.in_fop else u"Нет"
        if row.Name in bindings:
            _, binding = bindings[row.Name]
            bound = set(c.Id.IntegerValue for c in binding.Categories)
            row.missing_cats = [c for c in cats if c.Id.IntegerValue not in bound]
            if isinstance(binding, DB.TypeBinding):
                # АрмЧисло задаётся у каждого экземпляра — привязка к типу не подходит
                row.state = 'type'
                row.set_status(u"Привязан к типу — нужен экземпляр", 'err')
            elif row.missing_cats:
                row.state = 'partial'
                row.set_status(u"Нет категорий: {}".format(len(row.missing_cats)), 'warn')
            else:
                row.state = 'ok'
                row.set_status(u"Привязан", 'ok')
        rows.append(row)
    return rows, cats, refused, fop_error


def create_fop_definition(fop, row):
    group = None
    for g in fop.Groups:
        if g.Name == FOP_GROUP_NAME:
            group = g
    if group is None:
        group = fop.Groups.Create(FOP_GROUP_NAME)
    is_int = row.data_kind == "int"
    try:
        spec = DB.SpecTypeId.Int.Integer if is_int else DB.SpecTypeId.String.Text
        opts = DB.ExternalDefinitionCreationOptions(row.Name, spec)
    except Exception:
        # Запасной вариант на случай, если в этой сборке API нет конструктора с ForgeTypeId
        ptype = DB.ParameterType.Integer if is_int else DB.ParameterType.Text
        opts = DB.ExternalDefinitionCreationOptions(row.Name, ptype)
    opts.Description = row.Description
    return group.Definitions.Create(opts)


def prepare_params(doc, app, fop_path):
    """Привязывает параметры КЖ к арматуре и группам. Недостающие в ФОП определения дописывает
    (после подтверждения). Возвращает True, если модель изменена."""
    rows, cats, refused, fop_error = analyze_params(doc, app, fop_path)
    if fop_error:
        show_message(u"ФОП не прочитан: {}".format(fop_error))
        return False
    if not cats:
        show_message(u"Revit не разрешает привязать параметры ни к одной из категорий: {}".format(
            u", ".join(refused)))
        return False

    wrong_type = [r for r in rows if r.state == 'type']
    to_bind = [r for r in rows if r.state == 'none']
    to_extend = [r for r in rows if r.state == 'partial']
    to_fop = [r for r in to_bind if not r.in_fop]
    if wrong_type:
        show_message(u"Параметры привязаны к типу, а нужны у экземпляра: {}. Удалите их привязку в "
                     u"«Управление → Параметры проекта» и запустите подготовку снова.".format(
                         u", ".join(r.Name for r in wrong_type)))
    if not to_bind and not to_extend:
        if not wrong_type:
            show_message(u"Все параметры КЖ уже привязаны ко всем нужным категориям.", kind='info')
        return False
    if to_fop and not fop_path:
        show_message(u"Укажите ФОП: параметры {} нужно взять из файла общих параметров.".format(
            u", ".join(r.Name for r in to_fop)))
        return False

    cat_names = u", ".join(sorted(c.Name for c in cats))
    lines = []
    for r in to_fop:
        lines.append(u"Дописать в ФОП (группа «{}»): {} — {}".format(FOP_GROUP_NAME, r.Name, r.DataText))
    for r in to_bind:
        lines.append(u"Привязать {} как экземпляр к категориям: {}".format(r.Name, cat_names))
    for r in to_extend:
        lines.append(u"Добавить к {} категории: {}".format(r.Name, u", ".join(c.Name for c in r.missing_cats)))
    question = u"Подготовить параметры проекта?"
    if to_fop:
        question += u" Файл ФОП будет изменён: в него добавятся {} определений.".format(len(to_fop))
    if refused:
        question += u"\n\nRevit не разрешает привязку к категориям: {} — они пропущены.".format(u", ".join(refused))
    if not show_message(question, ask=True, details=lines, details_header=u"Действий: {}".format(len(lines))):
        return False

    old = app.SharedParametersFilename
    try:
        defs = {}
        if fop_path:
            fop, defs = read_fop_definitions(app, fop_path)
            for r in to_fop:
                defs[r.Name] = create_fop_definition(fop, r)
        bindings = project_bindings(doc)
        t = DB.Transaction(doc, u"КЖ: привязка параметров")
        t.Start()
        try:
            for r in to_bind:
                cat_set = app.Create.NewCategorySet()
                for c in cats:
                    cat_set.Insert(c)
                binding = app.Create.NewInstanceBinding(cat_set)
                if not doc.ParameterBindings.Insert(defs[r.Name], binding, DB.BuiltInParameterGroup.PG_DATA):
                    raise Exception(u"Revit не принял привязку параметра {}".format(r.Name))
            for r in to_extend:
                definition, binding = bindings[r.Name]
                for c in r.missing_cats:
                    binding.Categories.Insert(c)
                if not doc.ParameterBindings.ReInsert(definition, binding):
                    raise Exception(u"Revit не принял новые категории параметра {}".format(r.Name))
            t.Commit()
        except Exception:
            if t.GetStatus() == DB.TransactionStatus.Started:
                t.RollBack()
            raise
    finally:
        restore_fop(app, old)
    show_message(u"Готово: привязано параметров — {}, дополнено категориями — {}{}.".format(
        len(to_bind), len(to_extend), u", дописано в ФОП — {}".format(len(to_fop)) if to_fop else u""), kind='info')
    return True


def set_values(element, values):
    """Записывает параметры КЖ, если они привязаны. values: {имя: значение}."""
    for name, value in values.items():
        p = element.LookupParameter(name)
        if p is None or p.IsReadOnly:
            continue
        try:
            p.Set(value)
        except Exception:
            pass

# -*- coding: utf-8 -*-
"""Сечение колонны (pyRevit / IronPython 2.7).

Для каждой выбранной колонны с арматурой — чертёжный вид с сечением на середине высоты по арматуре из
модели: контур, стержни, хомуты и шпильки, размеры до осей стержней и габариты, позиции АрмМарка на
полках выносок без пересечений, заголовок с масштабом. Стили линий — КЖ_* (создаются, если их нет).
"""
__title__ = u"Сечение\nколонны"
__doc__ = u"Чертёжный вид с сечением колонны по арматуре модели: размеры и позиции по ГОСТ 21.501"

import traceback

import sys
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from pyrevit import DB
from kzh import detailing, element_name
from kzh import rebar as kr
from kzh.ui import pick_elements, show_message

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

SCALE = 20


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: кнопка работает в проекте.")
        return
    cols = pick_elements(uidoc, [int(DB.BuiltInCategory.OST_StructuralColumns)],
                         u"Выберите армированные колонны и нажмите «Готово»")
    if not cols:
        return
    armed = [c for c in cols if kr.host_rebar_ids(c)]
    empty = [c for c in cols if c not in armed]
    if not armed:
        show_message(u"У выбранных колонн нет арматуры: сначала армируйте их кнопкой «Колонны».", kind='info')
        return
    lines = [u"{} (id {})".format(element_name(c), c.Id.IntegerValue) for c in armed]
    if not show_message(u"Создать чертёжные виды сечений, М 1:{} ({} шт.)? Модель не меняется: добавляются виды, "
                        u"линии, размеры и текст на них{}.".format(
                            SCALE, len(armed), u"; без арматуры пропущено: {}".format(len(empty)) if empty else u""),
                        ask=True, kind='info', details=lines, details_header=u"Колонн: {}".format(len(armed))):
        return
    made, notes = [], []
    t = DB.Transaction(doc, u"КЖ: сечения колонн")
    t.Start()
    try:
        for c in armed:
            view, msgs = detailing.column_section_view(doc, c, SCALE)
            title = u"{} (id {})".format(element_name(c), c.Id.IntegerValue)
            if view is not None:
                made.append(view)
            notes += [u"{}: {}".format(title, m) for m in msgs]
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    if made:
        uidoc.ActiveView = made[-1]
    show_message(u"Создано видов сечений: {}. Открыт последний.".format(len(made)), kind='info' if not notes else 'warn',
                 details=notes or None, details_header=u"Замечания" if notes else None)


try:
    main()
except Exception:
    show_message(u"Ошибка кнопки «Сечение колонны»:\n{}".format(traceback.format_exc()), title=u"Ошибка")

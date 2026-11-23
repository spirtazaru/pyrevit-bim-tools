# -*- coding: utf-8 -*-
"""Скрыть бетон (pyRevit / IronPython 2.7).

Переключатель на активном виде: скрывает бетонные основы арматуры, видимой на виде (остаётся арматура),
повторное нажатие — показывает их снова. Скрытие — обычное «Скрыть на виде», модель не меняется.
"""
__title__ = u"Скрыть\nбетон"
__doc__ = u"Скрыть или показать бетон основ арматуры на активном виде"

import traceback

import sys
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from pyrevit import DB
from kzh import detailing
from kzh.ui import show_message

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document


def main():
    view = doc.ActiveView
    if view is None or view.IsTemplate or isinstance(view, (DB.ViewSchedule, DB.ViewSheet)):
        show_message(u"Откройте 3D-вид, план или разрез с арматурой.", kind='info')
        return
    if not detailing.view_hosts(doc, view):
        show_message(u"На активном виде нет арматуры.", kind='info')
        return
    hide = not detailing.concrete_hidden(doc, view)
    t = DB.Transaction(doc, u"КЖ: скрыть бетон" if hide else u"КЖ: показать бетон")
    t.Start()
    try:
        detailing.set_concrete_hidden(doc, view, hide)
        t.Commit()
    except Exception:
        t.RollBack()
        raise


try:
    main()
except Exception:
    show_message(u"Ошибка кнопки «Скрыть бетон»:\n{}".format(traceback.format_exc()), title=u"Ошибка")

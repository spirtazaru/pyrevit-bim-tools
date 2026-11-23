# -*- coding: utf-8 -*-
"""3D армирование (pyRevit / IronPython 2.7).

Выделите колонны, балки, плиты, стены, фундаменты — создаётся 3D-вид с подрезкой по ним: арматура
сплошная и поверх бетона, остальные элементы и категории скрыты. Бетон выбранных элементов можно скрыть
сразу или потом кнопкой «Скрыть бетон».
"""
__title__ = u"3D\nармирование"
__doc__ = u"3D-вид армирования выбранных элементов: только их арматура, бетон можно скрыть"

import traceback

import sys
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from pyrevit import DB
from kzh import detailing, element_name, service
from kzh import rebar as kr
from kzh.ui import pick_elements, show_message

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: кнопка работает в проекте.")
        return
    picked = pick_elements(uidoc, [int(c) for c in service.CONCRETE_CATEGORIES],
                           u"Выберите армированные колонны, балки, плиты, стены и нажмите «Готово»")
    if not picked:
        return
    hosts = [e for e in picked if kr.host_rebar_ids(e)]
    if not hosts:
        show_message(u"У выбранных элементов нет арматуры: сначала армируйте их кнопками КЖ.", kind='info')
        return
    lines = [u"{} (id {})".format(element_name(e), e.Id.IntegerValue) for e in hosts]
    if not show_message(u"Создать 3D-вид армирования {} элементов? Бетон этих элементов будет скрыт — показать "
                        u"его можно кнопкой «Скрыть бетон». Модель не меняется: добавляется вид.".format(len(hosts)),
                        ask=True, kind='info', details=lines, details_header=u"Элементов: {}".format(len(hosts))):
        return
    t = DB.Transaction(doc, u"КЖ: 3D армирование")
    t.Start()
    try:
        view, n = detailing.rebar_3d_view(doc, hosts, hide_concrete=True)
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    uidoc.ActiveView = view


try:
    main()
except Exception:
    show_message(u"Ошибка кнопки «3D армирование»:\n{}".format(traceback.format_exc()), title=u"Ошибка")

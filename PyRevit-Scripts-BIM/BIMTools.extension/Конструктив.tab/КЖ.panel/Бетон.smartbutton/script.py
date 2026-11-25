# -*- coding: utf-8 -*-
"""Бетон — переключатель на ленте (pyRevit smartbutton / IronPython 2.7).

Нажатие скрывает бетонные основы арматуры, видимой на активном виде (остаётся арматура), повторное —
показывает их. Иконка на ленте показывает состояние активного вида: бетон показан (on.png) или скрыт
(off.png); при переходе на другой вид иконка обновляется. Скрытие — обычное «Скрыть на виде»,
модель не меняется. Состояние запоминается по видам на время сеанса Revit.
"""
__title__ = u"Бетон"
__doc__ = u"Переключатель: скрыть или показать бетон основ арматуры на активном виде. Иконка показывает состояние"

import os

import System

# Id видов, на которых бетон скрыт кнопкой (общий для запусков кнопки и обработчика смены вида)
STATE_KEY = "bimtools_kzh_concrete_hidden"
HANDLER_KEY = "bimtools_kzh_concrete_handler"
HERE = os.path.dirname(__file__)


def _hidden_views():
    from System.Collections.Generic import HashSet
    data = System.AppDomain.CurrentDomain.GetData(STATE_KEY)
    if data is None:
        data = HashSet[int]()
        System.AppDomain.CurrentDomain.SetData(STATE_KEY, data)
    return data


def _set_icon(ui_button, hidden):
    from pyrevit.coreutils import ribbon
    icon = os.path.join(HERE, "off.png" if hidden else "on.png")
    try:
        ui_button.set_icon(icon, icon_size=ribbon.ICON_LARGE)
    except Exception:
        pass


def __selfinit__(script_cmp, ui_button_cmp, __rvt__):
    """При загрузке ленты: иконка по активному виду и подписка на смену вида (старая подписка — снимается)."""
    hidden = _hidden_views()

    def on_view_activated(sender, args):
        try:
            view = args.CurrentActiveView
            _set_icon(ui_button_cmp, view is not None and view.Id.IntegerValue in hidden)
        except Exception:
            pass

    old = System.AppDomain.CurrentDomain.GetData(HANDLER_KEY)
    if old is not None:
        try:
            __rvt__.ViewActivated -= old
        except Exception:
            pass
    __rvt__.ViewActivated += on_view_activated
    System.AppDomain.CurrentDomain.SetData(HANDLER_KEY, on_view_activated)
    _set_icon(ui_button_cmp, False)
    return True


def main():
    import sys
    import traceback
    for name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
        del sys.modules[name]
    from pyrevit import DB, script
    from kzh import detailing
    from kzh.ui import show_message

    doc = __revit__.ActiveUIDocument.Document     # noqa: F821 — задаётся pyRevit при запуске кнопки
    try:
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
        hidden = _hidden_views()
        if hide:
            hidden.Add(view.Id.IntegerValue)
        else:
            hidden.Remove(view.Id.IntegerValue)
        # on.png — бетон показан, off.png — скрыт
        script.toggle_icon(not hide, icon_size=script.ICON_LARGE)
    except Exception:
        show_message(u"Ошибка кнопки «Бетон»:\n{}".format(traceback.format_exc()), title=u"Ошибка")


# При загрузке ленты pyRevit импортирует скрипт ради __selfinit__ — тогда main() не выполняется
if __name__ == "__main__":
    main()

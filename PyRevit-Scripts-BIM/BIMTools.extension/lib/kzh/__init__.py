# -*- coding: utf-8 -*-
"""Общий код кнопок раздела «Конструктив → КЖ» (армирование и инструменты КЖ)."""


def element_name(element):
    """Имя элемента или типа. В pyRevit 6 (IronPython 2.7.12) Element.Name — дескриптор класса:
    DB.Element.Name.GetValue там не работает, а element.Name у типов недоступен."""
    from pyrevit import DB
    try:
        return DB.Element.Name.__get__(element)
    except Exception:
        return DB.Element.Name.GetValue(element)

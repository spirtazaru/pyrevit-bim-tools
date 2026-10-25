# -*- coding: utf-8 -*-
"""Настройки кнопок КЖ: один JSON в %APPDATA%, по разделу на кнопку.

Таблица ролей форм стержней: роль -> имя формы (RebarShape) из шаблона проекта.
Пустое имя — автоподбор: Rebar.CreateFromCurves находит подходящую форму или создаёт новую.
"""
import json
import os

SETTINGS_FILE = os.path.join(os.getenv('APPDATA') or u"", 'BimTools_KZh.json')

# Роли форм: (ключ, подпись). Порядок — как в окне «Подготовка проекта»
SHAPE_ROLES = (
    ("straight", u"Прямой стержень"),
    ("crank", u"Стержень с отгибом (выпуск)"),
    ("L", u"Г-образный стержень"),
    ("U", u"П-образный стержень"),
    ("stirrup", u"Хомут"),
    ("pin", u"Шпилька"),
    ("frog", u"Лягушка (поддерживающий каркас)"),
)


def _load_all():
    try:
        with open(SETTINGS_FILE, 'rb') as f:
            return json.loads(f.read().decode('utf-8'))
    except Exception:
        return {}


def load(section):
    data = _load_all().get(section)
    return data if isinstance(data, dict) else {}


def save(section, values):
    data = _load_all()
    data[section] = values
    try:
        with open(SETTINGS_FILE, 'wb') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'))
    except Exception:
        pass


def shape_roles():
    """{роль: имя формы или ""} для всех ролей."""
    saved = load('shapes')
    return dict((key, saved.get(key) or u"") for key, _ in SHAPE_ROLES)


def save_shape_roles(roles):
    save('shapes', dict((key, roles.get(key) or u"") for key, _ in SHAPE_ROLES))

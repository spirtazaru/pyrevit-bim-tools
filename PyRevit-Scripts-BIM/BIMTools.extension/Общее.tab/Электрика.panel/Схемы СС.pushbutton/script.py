# -*- coding: utf-8 -*-
"""Генератор схемы слаботочных систем (pyRevit / IronPython 2.7).

Схема: этаж (по отметке) -> щиты слева -> шлейфы -> устройства по порядку подключения.
Порядок устройств в шлейфе определяется удалённостью от щита (как режим траектории
"Все устройства": от щита к ближайшему, затем к ближайшему из оставшихся).

Параметры элементов (все текстовые, кроме CEC_Это_Щит - Да/Нет):
  CEC_Имя_Системы   - система (у всех элементов одинаково), по ней выбирается схема
  CEC_Это_Щит       - Да у семейства щита/панели
  CEC_Панель        - имя щита. У щита: его имя ("ШПС 9"), у устройств: имя щита, к которому
                      они подключены. Если у щита пусто, берётся Марка или имя типоразмера.
  CEC_Номер_Шлейфа  - у устройства: шлейф ("L1"); у щита: перечень шлейфов через запятую
                      ("L1, L2, S1, S2"). Устройство без CEC_Панель попадает в щит,
                      в перечне которого есть его шлейф (или в единственный щит проекта).
  CEC_Имя_УГО       - имя типоразмера (или семейства) типовой аннотации
  CEC_Роль          - необязательно: "Блок" - устройство внутри щита (контрольный блок,
                      источник питания), рисуется внутри рамки щита
  CEC_Марка         - необязательно: подпись устройства; если пусто, берётся стандартный
                      параметр "Марка", а если и он пуст - "<шлейф>.<номер по порядку>"
  CEC_Корпус        - необязательно: разделяет схему на корпуса

Раздел «Параметры проекта» привязывает CEC_ к нужным категориям из ФОП пользователя
(недостающие определения можно один раз дописать в этот ФОП). Кнопка «Демо-схема» строит схему
на синтетических данных — для проверки раскладки без элементов в модели.
Оформление — в стиле окна «Аудит модели» (оранжевая палитра, левое меню по вкладкам).
"""
__title__ = u"Схемы\nСС"
__author__ = u"Daniil Antonenko"
__persistentengine__ = True

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import json
import os
import traceback
import System
from System.Collections.Generic import List
from System.Diagnostics import Process
from System.Windows import WindowState, Visibility
from System.Windows.Controls import Button
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Markup import XamlReader
from System.Windows.Media import VisualTreeHelper
from Microsoft.Win32 import OpenFileDialog
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent, ExternalEventRequest
from pyrevit import DB

PRM_SYSTEM = u"CEC_Имя_Системы"
PRM_PANEL = u"CEC_Панель"
PRM_LOOP = u"CEC_Номер_Шлейфа"
PRM_UGO = u"CEC_Имя_УГО"
PRM_IS_PANEL = u"CEC_Это_Щит"
PRM_BUILDING = u"CEC_Корпус"
PRM_MARK = u"CEC_Марка"
PRM_ROLE = u"CEC_Роль"

# Описание параметров для привязки к проекту: (имя, данные "text"/"yesno", "instance"/"type", назначение)
PARAM_SPECS = (
    (PRM_SYSTEM, "text", "instance", u"Система — по ней выбирается схема"),
    (PRM_IS_PANEL, "yesno", "type", u"Да — семейство щита/прибора"),
    (PRM_PANEL, "text", "instance", u"Имя щита (у устройств — щит подключения)"),
    (PRM_LOOP, "text", "instance", u"Шлейф (у щита — перечень через запятую)"),
    (PRM_UGO, "text", "type", u"Имя типовой аннотации УГО"),
    (PRM_ROLE, "text", "type", u"«Блок» — устройство внутри щита"),
    (PRM_MARK, "text", "instance", u"Подпись устройства на схеме"),
    (PRM_BUILDING, "text", "instance", u"Корпус"),
)
FOP_GROUP_NAME = u"CEC"
SETTINGS_FILE = os.path.join(os.getenv('APPDATA'), 'BimTools_SchemaSS.json')

TXT_NO_PARAMS = u"Параметры CEC_ не заполнены!"
APP_KEY = "CEC_SchemaSS_Window"
# Подпись этажа: "elev" -> "Отм. +6.150", "name" -> имя уровня, "both" -> "Уровень 2 (Отм. +6.150)"
LEVEL_LABEL_MODE = "elev"

TARGET_CATEGORY_NAMES = (
    "OST_ElectricalEquipment", "OST_ElectricalFixtures",
    "OST_LightingDevices", "OST_LightingFixtures",
    "OST_FireAlarmDevices", "OST_SecurityDevices",
    "OST_DataDevices", "OST_CommunicationDevices",
    "OST_NurseCallDevices", "OST_TelephoneDevices",
)

# Стили линий: (имя подкатегории, RGB, толщина, ключ штриха).
# t0..t7 - цвета типов шлейфов (тип = буквенная часть имени: L1, L2 -> "L").
LOOP_COLORS = ((0, 176, 80), (0, 0, 255), (255, 0, 0), (176, 0, 176),
               (255, 128, 0), (0, 150, 170), (140, 90, 0), (90, 90, 90))
STYLE_DEFS = {
    "cab": (u"CEC_Щит", (0, 0, 0), 2, None),
    "floor": (u"CEC_Разделитель_Этажа", (128, 128, 128), 1, "small"),
}
for _i, _rgb in enumerate(LOOP_COLORS):
    STYLE_DEFS["t%d" % _i] = (u"CEC_Шлейф_Тип{}".format(_i + 1), _rgb, 2, None)
PATTERN_DEFS = {
    "small": (u"CEC_Штрих_Мелкий", 0.0066, 0.005),
}


# >>> PURE-BEGIN  (чистый Python: раскладка схемы, не зависит от Revit API)
import math
import re

TXT_NO_LEVEL = u"Без уровня"
TXT_NO_LOOP = u"Без шлейфа"
TXT_NO_PANEL = u"Без щита"

# --- Размеры в "условных единицах": 1 ед. = UNIT_MM мм на бумаге (не зависит от масштаба вида)
UNIT_MM = 20.0
MM_IN_FT = 304.8
TEXT_W_K = 0.75        # ширина символа текста относительно его высоты

AUTO_MARK = True       # если марка пустая, подписать "<шлейф>.<номер по порядку>"
DEV_R = 0.25           # полуразмер УГО устройства (УГО ~ 10x10 мм, точка вставки в центре)
DEV_LABEL_GAP = 0.05
DEV_PER_ROW = 15       # максимум устройств в одной строке шлейфа
ROW_GAP = 0.45
BLK_H = 0.7            # высота УГО блока внутри щита
BLK_W = 1.0
OUT_ROW = 0.7          # шаг выходов шлейфов из щита
PANEL_GAP = 0.6
LEVEL_PAD = 0.5
RISER_DX = 0.6         # расстояние между вертикальными стояками шлейфов
FRAME_GAP = 2.0
LOOP_STYLE_COUNT = 8   # число цветов линий по типам шлейфов
LOOP_STYLE = {}        # имя шлейфа -> ключ стиля линии (заполняется в draw_hierarchy)
ORDER_MODE = "path"    # порядок устройств в шлейфе: "path" - по удалённости от щита, "mark" - по маркам
WARNINGS = []          # замечания к данным, найденные при построении (выводятся в отчёте)


def natural_key(value):
    return [(1, int(p)) if p.isdigit() else (0, p.lower())
            for p in re.split(r'(\d+)', value or u"")]


def level_key_desc(key):
    """Верхние этажи выше: сортировка по убыванию отметки, без отметки - в конец."""
    elev, name = key
    return (elev is None, -(elev if elev is not None else 0.0), natural_key(name))


def mark_key(rec):
    return natural_key(rec["mark"] or u"")


def loop_key_sort(k):
    """k = (щит, шлейф): сначала по имени шлейфа (L1, L2, S1...), затем по щиту."""
    return (natural_key(k[1]), natural_key(k[0]))


def prefixed(value, prefix, skip):
    if value in skip or value.lower().startswith(prefix.strip().lower()):
        return value
    return prefix + value


def split_loops(text):
    out = []
    for part in re.split(r'[,;]+', text or u""):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def loop_type(name):
    """Тип шлейфа: 'L1' -> 'L', 'S12' -> 'S', 'Витая пара' -> 'Витая пара'."""
    m = re.match(r'^(.*?)[\s\-_]*\d+$', name)
    return m.group(1) if m and m.group(1) else name


def loop_style(name):
    return LOOP_STYLE.get(name, "t0")


def dist3(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def order_by_path(recs, start, start_elev=None):
    """Порядок подключения как в режиме траектории 'Все устройства':
    этажи по удалённости от этажа щита; на каждом этаже от щита (или последнего устройства)
    к ближайшему из оставшихся устройств."""
    pool = sorted(recs, key=mark_key)
    if not pool or any(r["pt"] is None for r in pool):
        return pool
    floors = {}
    for r in pool:
        floors.setdefault(r["elev"], []).append(r)
    ref = start_elev
    if ref is None:
        ref = next((e for e in floors if e is not None), None)

    def floor_order(e):
        return (e is None, abs(e - ref) if (e is not None and ref is not None) else 0.0, -(e or 0.0))

    cur = start if start is not None else pool[0]["pt"]
    out = []
    for e in sorted(floors.keys(), key=floor_order):
        left = floors[e]
        while left:
            best = min(left, key=lambda r: dist3(cur, r["pt"]))
            left.remove(best)
            out.append(best)
            cur = best["pt"]
    return out


def build_cabinets(records):
    cabs, owners = {}, {}

    def get_cab(name):
        if name not in cabs:
            cabs[name] = {"name": name, "panel": None, "blocks": [], "loops": {}}
        return cabs[name]

    # 1) щиты и перечень шлейфов, объявленных в щите ("L1, L2, S1, S2").
    #    У разных приборов шлейфы часто называются одинаково (у каждого свой L1) - запоминаем всех владельцев.
    for r in records:
        if r["role"] != "panel":
            continue
        name = r["panel"] or r["mark"] or r["model"] or TXT_NO_PANEL
        cab = get_cab(name)
        if cab["panel"] is None:
            cab["panel"] = r
        for ln in split_loops(r["loop_raw"]):
            cab["loops"].setdefault(ln, [])
            if name not in owners.setdefault(ln, []):
                owners[ln].append(name)
    real = list(cabs.keys())

    # 2) устройства и блоки: щит по CEC_Панель, иначе по перечню шлейфов, иначе единственный щит
    ambiguous, no_loop = {}, 0
    for r in records:
        if r["role"] == "panel":
            continue
        loop_owners = owners.get(r["loop"], [])
        if r["panel"]:
            name = r["panel"]
        elif r["role"] == "device" and len(loop_owners) == 1:
            name = loop_owners[0]
        elif r["role"] == "device" and len(loop_owners) > 1:
            ambiguous[r["loop"]] = ambiguous.get(r["loop"], 0) + 1
            name = TXT_NO_PANEL
        elif len(real) == 1:
            name = real[0]
        else:
            name = TXT_NO_PANEL
        if r["role"] == "device" and not r["loop_raw"]:
            no_loop += 1
        cab = get_cab(name)
        if r["role"] == "block":
            cab["blocks"].append(r)
        else:
            cab["loops"].setdefault(r["loop"], []).append(r)
    for ln in sorted(ambiguous.keys(), key=natural_key):
        WARNINGS.append(u"Шлейф {} объявлен в нескольких щитах ({}): {} устр. без CEC_Панель отнесены к «{}». "
                        u"Заполните у них CEC_Панель.".format(ln, u", ".join(owners[ln]), ambiguous[ln], TXT_NO_PANEL))
    if no_loop:
        WARNINGS.append(u"Устройств без CEC_Номер_Шлейфа: {} (на схеме - шлейф «{}»).".format(no_loop, TXT_NO_LOOP))

    # 3) место щита, порядок устройств по удалённости, подписи
    for cab in cabs.values():
        members = list(cab["blocks"])
        for devs in cab["loops"].values():
            members.extend(devs)
        src = cab["panel"]
        if src is None and members:
            src = sorted(members, key=mark_key)[0]
        src = src or {"building": None, "level": TXT_NO_LEVEL, "elev": None, "pt": None}
        cab["building"] = src["building"]
        cab["level"], cab["elev"] = src["level"], src["elev"]
        start = cab["panel"]["pt"] if cab["panel"] is not None else None
        start_elev = cab["panel"]["elev"] if cab["panel"] is not None else None
        for lname in list(cab["loops"].keys()):
            devs = cab["loops"][lname]
            if ORDER_MODE == "mark":
                ordered = sorted(devs, key=mark_key)
            else:
                if any(r["pt"] is None for r in devs):
                    WARNINGS.append(u"Щит {}, шлейф {}: у части устройств нет координат - порядок взят по маркам.".format(
                        cab["name"], lname))
                ordered = order_by_path(devs, start, start_elev)
            for i, r in enumerate(ordered):
                r["label"] = r["mark"] or (u"{}.{}".format(lname, i + 1) if AUTO_MARK else u"")
            cab["loops"][lname] = ordered
        for r in cab["blocks"]:
            r["label"] = r["mark"] or u""
        if cab["panel"] is not None:
            m = cab["panel"]["mark"] or u""
            cab["panel"]["label"] = u"" if m == cab["name"] else m   # имя уже в заголовке рамки
    return cabs


def build_tree(cabs):
    """корпус -> (отметка, подпись этажа) -> {devs: {(щит, шлейф): [устройства по порядку]}, cabs: [...]}"""
    tree = {}

    def band(building, lkey):
        return tree.setdefault(building, {}).setdefault(lkey, {"devs": {}, "cabs": []})

    for cab in cabs.values():
        band(cab["building"], (cab["elev"], cab["level"]))["cabs"].append(cab)
    for cab in cabs.values():
        for lname, devs in cab["loops"].items():
            for r in devs:
                b = band(r["building"] or cab["building"], (r["elev"], r["level"]))
                b["devs"].setdefault((cab["name"], lname), []).append(r)
    return tree


def layout_panel(cv, cab):
    cw, lh = cv.cw, cv.lh
    blocks = sorted(cab["blocks"], key=mark_key)
    if cab["panel"] is not None and cab["panel"]["ugo"]:
        blocks.insert(0, cab["panel"])      # собственное УГО щита (CEC_Имя_УГО) - первым внутри рамки
    loops = sorted(cab["loops"].keys(), key=natural_key)
    hdr = lh + 0.4
    item_h = lh + DEV_LABEL_GAP + BLK_H + 0.35
    body = len(blocks) * item_h + 0.15
    rows = 0.3 + len(loops) * OUT_ROW
    height = hdr + max(body, rows, 0.8) + 0.2
    mark_w = max([len(b["label"]) for b in blocks] or [0]) * cw
    width = max(len(cab["name"]) * cw + 0.8, mark_w + 0.8, BLK_W + 0.8, 3.0)
    cab["L"] = {"w": width, "h": height, "hdr": hdr, "item_h": item_h,
                "blocks": blocks, "loops": loops}


def draw_panel(cv, cab, x0, y0, risers, touch):
    L, lh = cab["L"], cv.lh
    w = L["w"]
    cv.rect(x0, y0, w, L["h"], "cab")
    cv.line(x0, y0 - L["hdr"], x0 + w, y0 - L["hdr"], "cab")
    cv.text_c(x0 + w / 2.0, y0 - 0.2, cab["name"])
    xc, yb = x0 + w / 2.0, y0 - L["hdr"] - 0.15
    for b in L["blocks"]:
        cv.text_c(xc, yb, b["label"])
        cv.device(b, xc, yb - lh - DEV_LABEL_GAP - BLK_H / 2.0)
        yb -= L["item_h"]
    for i, name in enumerate(L["loops"]):
        ry = y0 - L["hdr"] - 0.5 - i * OUT_ROW
        key = (cab["name"], name)
        cv.line(x0 + w, ry, risers[key], ry, loop_style(name))
        cv.text(x0 + w + 0.15, ry + lh + 0.05, name)
        touch(key, ry)


def draw_chain(cv, key, devs, risers, chain_x0, cell_w, box_w, ytop, touch):
    """Цепочка устройств шлейфа на одном этаже: слева направо в порядке подключения.
    Длинная цепочка переносится на следующую строку «змейкой» от последнего устройства,
    чтобы на схеме было видно последовательное подключение, а не отдельную ветвь от стояка."""
    lh = cv.lh
    style, rx = loop_style(key[1]), risers[key]
    y_t = ytop
    prev_end = None
    for start in range(0, len(devs), DEV_PER_ROW):
        chunk = devs[start:start + DEV_PER_ROW]
        yc = y_t - (lh + DEV_LABEL_GAP + DEV_R)
        xs = [chain_x0 + cell_w * (i + 0.5) for i in range(len(chunk))]
        if start == 0:
            bx, by = chain_x0 - box_w - 0.2, yc + 0.1 + lh + 0.2
            cv.rect(bx, by, box_w, lh + 0.2, "cab")
            cv.text_c(bx + box_w / 2.0, by - 0.1, key[1])
            cv.line(rx, yc, xs[0] - DEV_R, yc, style)
            touch(key, yc)
        else:
            px, py = prev_end
            y_mid = py - DEV_R - ROW_GAP / 2.0
            x_back = xs[0] - DEV_R - 0.15
            cv.line(px, py, px + 0.15, py, style)
            cv.line(px + 0.15, py, px + 0.15, y_mid, style)
            cv.line(px + 0.15, y_mid, x_back, y_mid, style)
            cv.line(x_back, y_mid, x_back, yc, style)
            cv.line(x_back, yc, xs[0] - DEV_R, yc, style)
        for i, rec in enumerate(chunk):
            if i > 0:
                cv.line(xs[i - 1] + DEV_R, yc, xs[i] - DEV_R, yc, style)
            cv.device(rec, xs[i], yc)
            cv.text_c(xs[i], yc + DEV_R + DEV_LABEL_GAP + lh, rec["label"])
        prev_end = (xs[-1] + DEV_R, yc)
        y_t = yc - DEV_R - ROW_GAP
    return y_t - 0.1


def draw_building(cv, bname, levels, ox, oy):
    cw, lh = cv.cw, cv.lh
    bands = list(levels.values())
    all_cabs = [c for b in bands for c in b["cabs"]]
    for c in all_cabs:
        layout_panel(cv, c)
    panel_w = max([c["L"]["w"] for c in all_cabs] or [3.0])

    keys = set()
    for b in bands:
        keys.update(b["devs"].keys())
        for c in b["cabs"]:
            for ln in c["loops"]:
                keys.add((c["name"], ln))
    keys = sorted(keys, key=loop_key_sort)
    lab_max = max([len(k[1]) for k in keys] or [0]) * cw
    maxlab, maxrow = 0, 1
    for b in bands:
        for devs in b["devs"].values():
            maxrow = max(maxrow, min(len(devs), DEV_PER_ROW))
            for r in devs:
                maxlab = max(maxlab, len(r["label"] or u""))
    cell_w = max(1.3, maxlab * cw + 0.35)
    box_w = lab_max + 0.4

    lab_w = max(max([len(k[1]) for k in levels.keys()] or [0]) * cw + 0.6, 3.0)
    panel_x = ox + lab_w + 0.5
    riser_x0 = panel_x + panel_w + lab_max + 0.6
    risers = dict((k, riser_x0 + i * RISER_DX) for i, k in enumerate(keys))
    chain_x0 = riser_x0 + max(len(keys), 1) * RISER_DX + box_w + 0.6
    frame_w = chain_x0 - ox + maxrow * cell_w + 0.5

    ext = {}

    def touch(key, y):
        span = ext.get(key)
        if span is None:
            ext[key] = [y, y]
        else:
            span[0], span[1] = min(span[0], y), max(span[1], y)

    cur = oy - ((lh + 0.5) if bname else 0.0)
    top = cur
    level_keys = sorted(levels.keys(), key=level_key_desc)
    for li, lkey in enumerate(level_keys):
        band = levels[lkey]
        band_top = cur
        cv.text(ox + 0.25, band_top - 0.15, lkey[1])
        yp = band_top - LEVEL_PAD
        for c in sorted(band["cabs"], key=lambda c: natural_key(c["name"])):
            draw_panel(cv, c, panel_x, yp, risers, touch)
            yp -= c["L"]["h"] + PANEL_GAP
        if band["cabs"]:
            yp += PANEL_GAP
        yc = band_top - LEVEL_PAD
        for key in sorted(band["devs"].keys(), key=loop_key_sort):
            yc = draw_chain(cv, key, band["devs"][key], risers, chain_x0, cell_w, box_w, yc, touch)
        band_bot = min(yp, yc) - LEVEL_PAD
        if li < len(level_keys) - 1:
            cv.line(ox, band_bot, ox + frame_w, band_bot, "floor")
        cur = band_bot

    fbot = cur
    cv.rect(ox, top, frame_w, top - fbot, "floor")
    cv.line(ox + lab_w, top, ox + lab_w, fbot, "floor")
    if bname:
        cv.text_c(ox + frame_w / 2.0, oy - 0.15, prefixed(bname, u"Корпус ", ()))
    for key, span in ext.items():
        cv.line(risers[key], span[1], risers[key], span[0], loop_style(key[1]))
    return oy - fbot


def draw_hierarchy(cv, records):
    del WARNINGS[:]
    cabs = build_cabinets(records)
    names = set()
    for c in cabs.values():
        names.update(c["loops"].keys())
    types = sorted(set(loop_type(n) for n in names), key=natural_key)
    style_of = dict((t, "t%d" % (i % LOOP_STYLE_COUNT)) for i, t in enumerate(types))
    LOOP_STYLE.clear()
    for n in names:
        LOOP_STYLE[n] = style_of[loop_type(n)]
    tree = build_tree(cabs)
    y = 0.0
    for bname in sorted(tree.keys(), key=lambda b: (b is not None, natural_key(b or u""))):
        y -= draw_building(cv, bname, tree[bname], 0.0, y) + FRAME_GAP
    return cabs


def demo_records():
    """Синтетические данные для проверки раскладки без элементов в модели (кнопка «Демо-схема»):
    два прибора с одинаковым шлейфом L1, три этажа, длинный шлейф с переносом строки, устройство без шлейфа."""
    floors = [(0.0, u"Отм. +0.000"), (13.12, u"Отм. +4.000"), (26.25, u"Отм. +8.000")]
    recs = []

    def add(role, floor, x, y, panel=None, loop=None, mark=None, model=None):
        elev, level = floors[floor]
        recs.append({"level": level, "elev": elev, "pt": (x, y, elev),
                     "loop": loop or TXT_NO_LOOP, "loop_raw": loop, "ugo": None, "panel": panel,
                     "role": role, "building": None, "mark": mark, "model": model})

    add("panel", 0, 0.0, 0.0, panel=u"ПКП-1", loop=u"L1, L2, S1", model=u"Прибор")
    add("panel", 1, 60.0, 0.0, panel=u"ПКП-2", loop=u"L1", model=u"Прибор")
    add("block", 0, 0.0, 1.0, panel=u"ПКП-1", mark=u"БП-1")
    for i in range(18):                       # длинный шлейф: перенос на вторую строку
        add("device", 0, 3.0 + i * 4.0, 2.0, panel=u"ПКП-1", loop=u"L1")
    for i in range(4):
        add("device", 1, 5.0 + i * 6.0, -3.0, panel=u"ПКП-1", loop=u"L1")
    for i in range(5):
        add("device", 2, 2.0 + i * 5.0, 4.0, panel=u"ПКП-1", loop=u"L2")
    for i in range(4):
        add("device", 0, 4.0, -2.0 - i * 3.0, panel=u"ПКП-1", loop=u"S1")
    for i in range(6):
        add("device", 1, 62.0 + i * 4.0, 1.0, panel=u"ПКП-2", loop=u"L1")
    add("device", 1, 70.0, 6.0, loop=u"L1")    # L1 есть у двух приборов - щит не определить
    add("device", 2, 10.0, 10.0, panel=u"ПКП-1")  # устройство без шлейфа
    return recs
# <<< PURE-END


def valid_id(eid):
    return eid is not None and eid != DB.ElementId.InvalidElementId


def get_name(el):
    if el is None or not el.IsValidObject:
        return None
    try:
        return DB.Element.Name.GetValue(el)
    except Exception:
        try:
            return el.Name
        except Exception:
            return None


def param_text(param):
    if param is None or not param.HasValue:
        return None
    try:
        if param.StorageType == DB.StorageType.String:
            value = param.AsString()
        elif param.StorageType == DB.StorageType.Integer:
            value = param.AsValueString()
            if not value:
                value = unicode(param.AsInteger())
        else:
            value = param.AsValueString()
        value = value.strip() if value else None
        return value or None
    except Exception:
        return None


def get_string_param(element, name):
    try:
        value = param_text(element.LookupParameter(name))
        if value:
            return value
        type_id = element.GetTypeId()
        if valid_id(type_id):
            typ = element.Document.GetElement(type_id)
            if typ is not None and typ.IsValidObject:
                return param_text(typ.LookupParameter(name))
    except Exception:
        pass
    return None


def is_panel_element(element):
    # Yes/No is stored as Integer. AsValueString() can be localized or empty.
    try:
        type_id = element.GetTypeId()
        typ = element.Document.GetElement(type_id) if valid_id(type_id) else None
    except Exception:
        typ = None
    for obj in (element, typ):
        if obj is None or not obj.IsValidObject:
            continue
        p = obj.LookupParameter(PRM_IS_PANEL)
        if p is not None and p.HasValue:
            if p.StorageType == DB.StorageType.Integer:
                return p.AsInteger() != 0
            return (param_text(p) or u"").lower() in (u"да", u"yes", u"true", u"1")
    return False


def detect_role(is_panel, role_text):
    if is_panel:
        return "panel"
    t = (role_text or u"").strip().lower()
    if t.startswith((u"щит", u"шкаф", u"панел")):
        return "panel"
    if t.startswith((u"блок", u"внутр")):
        return "block"
    return "device"


def target_elements(doc):
    categories = List[DB.BuiltInCategory]()
    for name in TARGET_CATEGORY_NAMES:
        bic = getattr(DB.BuiltInCategory, name, None)
        if bic is not None:
            categories.Add(bic)
    if categories.Count == 0:
        return ()
    return (DB.FilteredElementCollector(doc)
            .WherePasses(DB.ElementMulticategoryFilter(categories))
            .WhereElementIsNotElementType())


def available_systems(doc):
    values = set()
    for element in target_elements(doc):
        value = get_string_param(element, PRM_SYSTEM)
        if value:
            values.add(value)
    return sorted(values, key=natural_key)


def level_label(level):
    """Подпись этажа по отметке уровня (в метрах, относительно базовой точки проекта)."""
    name = get_name(level) or TXT_NO_LEVEL
    if LEVEL_LABEL_MODE == "name":
        return name
    meters = None
    try:
        p = level.get_Parameter(DB.BuiltInParameter.LEVEL_ELEV)
        if p is not None and p.HasValue:
            meters = p.AsDouble() * 0.3048
    except Exception:
        pass
    if meters is None:
        try:
            meters = level.ProjectElevation * 0.3048
        except Exception:
            try:
                meters = level.Elevation * 0.3048
            except Exception:
                return name
    if abs(meters) < 0.0005:
        meters = 0.0
    text = u"Отм. {:+.3f}".format(meters)
    return u"{} ({})".format(name, text) if LEVEL_LABEL_MODE == "both" else text


def level_table(doc):
    by_id, ordered = {}, []
    for lv in DB.FilteredElementCollector(doc).OfClass(DB.Level):
        entry = (lv.Elevation, level_label(lv))
        by_id[lv.Id.ToString()] = entry
        ordered.append(entry)
    ordered.sort(key=lambda t: t[0])
    return by_id, ordered


def get_level_entry(element, by_id, ordered, pt):
    """(отметка в футах для сортировки, подпись этажа)."""
    level_id = DB.ElementId.InvalidElementId
    try:
        level_id = element.LevelId
    except Exception:
        pass
    if not valid_id(level_id):
        for bip in (DB.BuiltInParameter.FAMILY_LEVEL_PARAM,
                    DB.BuiltInParameter.INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM):
            try:
                p = element.get_Parameter(bip)
                if p is not None and p.HasValue:
                    level_id = p.AsElementId()
                    if valid_id(level_id):
                        break
            except Exception:
                pass
    if valid_id(level_id):
        entry = by_id.get(level_id.ToString())
        if entry is not None:
            return entry
    if pt is not None and ordered:
        best = ordered[0]
        for item in ordered:
            if item[0] <= pt[2] + 0.01:
                best = item
        return best
    return None, TXT_NO_LEVEL


def element_point(element):
    try:
        loc = element.Location
        if isinstance(loc, DB.LocationPoint):
            p = loc.Point
            return (p.X, p.Y, p.Z)
    except Exception:
        pass
    try:
        bb = element.get_BoundingBox(None)
        if bb is not None:
            return ((bb.Min.X + bb.Max.X) / 2.0, (bb.Min.Y + bb.Max.Y) / 2.0,
                    (bb.Min.Z + bb.Max.Z) / 2.0)
    except Exception:
        pass
    return None


def get_mark(element):
    value = get_string_param(element, PRM_MARK)
    if value:
        return value
    try:
        return param_text(element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK))
    except Exception:
        return None


def collect_records(doc, system_name):
    by_id, ordered = level_table(doc)
    records = []
    for element in target_elements(doc):
        if get_string_param(element, PRM_SYSTEM) != system_name:
            continue
        pt = element_point(element)
        elev, level = get_level_entry(element, by_id, ordered, pt)
        try:
            model = get_name(doc.GetElement(element.GetTypeId()))
        except Exception:
            model = None
        loop_raw = get_string_param(element, PRM_LOOP)
        records.append({
            "level": level, "elev": elev, "pt": pt,
            "loop": loop_raw or TXT_NO_LOOP, "loop_raw": loop_raw,
            "ugo": get_string_param(element, PRM_UGO),
            "panel": get_string_param(element, PRM_PANEL),
            "role": detect_role(is_panel_element(element), get_string_param(element, PRM_ROLE)),
            "building": get_string_param(element, PRM_BUILDING),
            "mark": get_mark(element), "model": model,
        })
    return records


def symbol_cache(doc):
    # Keep IDs, not API wrappers that can become invalid during regeneration.
    by_family, by_type = {}, {}
    symbols = (DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)
               .OfCategory(DB.BuiltInCategory.OST_GenericAnnotation))
    for sym in symbols:
        type_name = get_name(sym)
        try:
            family_name = sym.FamilyName
        except Exception:
            family_name = get_name(sym.Family)
        if family_name and family_name.strip():
            by_family.setdefault(family_name.strip().lower(), sym.Id)
        if type_name and type_name.strip():
            by_type.setdefault(type_name.strip().lower(), sym.Id)
    by_family.update(by_type)
    return by_family


def drafting_type_id(doc):
    for typ in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
        if typ.ViewFamily == DB.ViewFamily.Drafting:
            return typ.Id
    return DB.ElementId.InvalidElementId


def find_view_id(doc, name):
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        if get_name(view) == name:
            return view.Id
    return None


def view_alive(doc, view_id):
    if not valid_id(view_id):
        return False
    v = doc.GetElement(view_id)
    return v is not None and v.IsValidObject


def view_content_ids(doc, view):
    # Коллектор с view.Id может вернуть не только содержимое вида, но и сам вид
    # (и другие виды). Явно исключаем сам вид и любые View, берём только элементы, принадлежащие виду.
    view_id = view.Id
    ids = List[DB.ElementId]()
    for el in DB.FilteredElementCollector(doc, view_id).WhereElementIsNotElementType():
        if not el.IsValidObject or el.Id == view_id or isinstance(el, DB.View):
            continue
        if el.OwnerViewId == view_id:
            ids.Add(el.Id)
    return ids


def clear_view(doc, view):
    # Единственное место со строкой Delete в скрипте; вызывается после подтверждения пользователя
    ids = view_content_ids(doc, view)
    if ids.Count:
        doc.Delete(ids)


def ensure_patterns(doc):
    result, existing = {}, {}
    for lp in DB.FilteredElementCollector(doc).OfClass(DB.LinePatternElement):
        existing[get_name(lp)] = lp.Id
    for key, (name, dash, space) in PATTERN_DEFS.items():
        if name in existing:
            result[key] = existing[name]
            continue
        segments = List[DB.LinePatternSegment]()
        segments.Add(DB.LinePatternSegment(DB.LinePatternSegmentType.Dash, dash))
        segments.Add(DB.LinePatternSegment(DB.LinePatternSegmentType.Space, space))
        pattern = DB.LinePattern(name)
        pattern.SetSegments(segments)
        result[key] = DB.LinePatternElement.Create(doc, pattern).Id
    return result


def ensure_line_styles(doc):
    patterns, styles = ensure_patterns(doc), {}
    lines = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    for key, (name, rgb, weight, pat_key) in STYLE_DEFS.items():
        if lines.SubCategories.Contains(name):
            # Стиль уже есть — не трогаем: пользователь мог настроить цвет и толщину под себя
            sub = lines.SubCategories.get_Item(name)
        else:
            sub = doc.Settings.Categories.NewSubcategory(lines, name)
            sub.LineColor = DB.Color(*rgb)
            sub.SetLineWeight(weight, DB.GraphicsStyleType.Projection)
            if pat_key:
                sub.SetLinePatternId(patterns[pat_key], DB.GraphicsStyleType.Projection)
        styles[key] = sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection).Id
    return styles


# ==========================================================
# ДИАЛОГИ В СТИЛЕ ОКНА АУДИТА (вместо стандартных TaskDialog)
# ==========================================================
# Палитры: 'info' — оранжевая (результаты), 'warn' — красная (предупреждения и подтверждения)
DIALOG_PALETTES = {
    'info': {'__TOP__': '#FFFF9440', '__BOTTOM__': '#FFFF6A00', '__ACCENT__': '#FFFF7A1A',
             '__SOFT__': '#1AFF7A1A', '__BORDER__': '#66FF7A1A', '__TITLE__': '#FF2B2118'},
    'warn': {'__TOP__': '#FFFF6B4A', '__BOTTOM__': '#FFDC2F1C', '__ACCENT__': '#FFE8452C',
             '__SOFT__': '#1AE8452C', '__BORDER__': '#80E8452C', '__TITLE__': '#FFA8241A'},
}

XAML_DIALOG = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="580" SizeToContent="Height" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="NoResize" ShowInTaskbar="False"
        FontFamily="Segoe UI" FontSize="14" Foreground="#FF2B2118">
    <Window.Resources>
        <LinearGradientBrush x:Key="AccentGrad" StartPoint="0,0" EndPoint="0,1">
            <GradientStop Color="__TOP__" Offset="0"/>
            <GradientStop Color="__BOTTOM__" Offset="1"/>
        </LinearGradientBrush>
        <Style x:Key="PrimaryButton" TargetType="Button">
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="MinWidth" Value="110"/>
            <Setter Property="Padding" Value="20,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="{StaticResource AccentGrad}" CornerRadius="19" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="bd" Property="Opacity" Value="0.9"/></Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
        <Style x:Key="GlassButton" TargetType="Button">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="MinWidth" Value="110"/>
            <Setter Property="Padding" Value="18,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="__BORDER__" BorderThickness="1" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="__ACCENT__"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
        <Style TargetType="ScrollBar">
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Setter Property="Width" Value="10"/>
            <Setter Property="Margin" Value="4,0,0,0"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ScrollBar">
                        <Grid>
                            <Border Background="__SOFT__" CornerRadius="5"/>
                            <Track Name="PART_Track" IsDirectionReversed="True">
                                <Track.Thumb>
                                    <Thumb>
                                        <Thumb.Template>
                                            <ControlTemplate TargetType="Thumb">
                                                <Border Background="{StaticResource AccentGrad}" CornerRadius="5" Margin="1" Opacity="0.8"/>
                                            </ControlTemplate>
                                        </Thumb.Template>
                                    </Thumb>
                                </Track.Thumb>
                            </Track>
                        </Grid>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
    </Window.Resources>

    <Border Margin="16" CornerRadius="20" Background="#FAFFFFFF" BorderBrush="__BORDER__" BorderThickness="1.5">
        <Border.Effect>
            <DropShadowEffect BlurRadius="18" ShadowDepth="0" Opacity="0.3" Color="#FF000000"/>
        </Border.Effect>
        <StackPanel>
            <Border Name="HeaderBar" CornerRadius="19,19,0,0" Background="__SOFT__" Padding="20,14" Cursor="SizeAll">
                <StackPanel Orientation="Horizontal">
                    <Grid Width="30" Height="30" Margin="0,0,12,0">
                        <Ellipse Fill="{StaticResource AccentGrad}"/>
                        <TextBlock Name="tbIcon" Text="!" Foreground="White" FontWeight="Bold" FontSize="17"
                                   HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </Grid>
                    <TextBlock Name="tbTitle" Foreground="__TITLE__" FontWeight="Bold" FontSize="16" VerticalAlignment="Center"/>
                </StackPanel>
            </Border>
            <TextBlock Name="tbMessage" TextWrapping="Wrap" FontSize="15" FontWeight="SemiBold" Margin="22,16,22,0"/>
            <Border Name="bdInput" Margin="22,12,22,0" CornerRadius="19" Background="#FFFFFFFF"
                    BorderBrush="__BORDER__" BorderThickness="1" Padding="14,0" Height="38">
                <TextBox Name="tbInput" BorderThickness="0" Background="Transparent" VerticalAlignment="Center" FontSize="14"/>
            </Border>
            <Border Name="bdDetails" Margin="22,12,22,0" CornerRadius="12" Background="__SOFT__"
                    BorderBrush="__BORDER__" BorderThickness="1" Padding="14,10,8,10">
                <StackPanel>
                    <TextBlock Name="tbDetailsHeader" FontWeight="Bold" Foreground="__TITLE__" Margin="0,0,0,6"/>
                    <ScrollViewer MaxHeight="320" VerticalScrollBarVisibility="Auto">
                        <TextBlock Name="tbDetails" TextWrapping="Wrap" FontSize="13" LineHeight="20"/>
                    </ScrollViewer>
                </StackPanel>
            </Border>
            <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="22,18,22,20">
                <Button Name="btnNo" Style="{StaticResource GlassButton}" Content="Нет" Margin="0,0,10,0" IsCancel="True"/>
                <Button Name="btnYes" Style="{StaticResource PrimaryButton}" Content="Да"/>
            </StackPanel>
        </StackPanel>
    </Border>
</Window>"""

# Окно плагина — владелец диалогов (задаётся при его создании), чтобы диалог был поверх и по центру
dialog_owner = None


def _run_dialog(text, kind, title, details, ask, limit, input_default, details_header):
    xaml = XAML_DIALOG
    for token, color in DIALOG_PALETTES.get(kind, DIALOG_PALETTES['warn']).items():
        xaml = xaml.replace(token, color)
    dlg = XamlReader.Parse(xaml)
    result = [False]

    tb_title = dlg.FindName('tbTitle')
    tb_title.Text = title or (u"Подтверждение" if ask else (u"Внимание" if kind == 'warn' else u"Схемы СС"))
    dlg.Title = tb_title.Text
    dlg.FindName('tbIcon').Text = u"!" if kind == 'warn' else u"i"
    dlg.FindName('tbMessage').Text = text

    tb_input = dlg.FindName('tbInput')
    if input_default is None:
        dlg.FindName('bdInput').Visibility = Visibility.Collapsed
    else:
        tb_input.Text = input_default

    if details:
        lines = list(details)
        shown = u"\n".join(lines[:limit])
        if len(lines) > limit:
            shown += u"\n… и ещё {} шт.".format(len(lines) - limit)
        dlg.FindName('tbDetailsHeader').Text = details_header or u"Элементов: {}".format(len(lines))
        dlg.FindName('tbDetails').Text = shown
    else:
        dlg.FindName('bdDetails').Visibility = Visibility.Collapsed

    btn_yes = dlg.FindName('btnYes')
    btn_no = dlg.FindName('btnNo')
    if not ask:
        btn_no.Visibility = Visibility.Collapsed
        btn_yes.Content = u"OK"
        btn_yes.IsDefault = True
        btn_yes.IsCancel = True
    elif input_default is not None:
        btn_yes.Content = u"Сохранить"
        btn_no.Content = u"Отмена"
        btn_yes.IsDefault = True

    def on_yes(s, e):
        result[0] = True
        dlg.Close()

    def on_drag(s, e):
        try:
            dlg.DragMove()
        except Exception:
            pass

    btn_yes.Click += on_yes
    btn_no.Click += lambda s, e: dlg.Close()
    dlg.FindName('HeaderBar').MouseLeftButtonDown += on_drag

    owner = dialog_owner
    if owner is not None and owner.IsVisible:
        dlg.Owner = owner
        dlg.WindowStartupLocation = System.Windows.WindowStartupLocation.CenterOwner
    else:
        dlg.WindowStartupLocation = System.Windows.WindowStartupLocation.CenterScreen
        try:
            WindowInteropHelper(dlg).Owner = Process.GetCurrentProcess().MainWindowHandle
        except Exception:
            pass
    if input_default is not None:
        dlg.Loaded += lambda s, e: (tb_input.Focus(), tb_input.SelectAll())
    dlg.ShowDialog()
    return result[0], tb_input.Text


def show_message(text, kind='warn', title=None, details=None, ask=False, limit=500, details_header=None):
    """Модальный диалог в стиле аудита. ask=True — кнопки Да/Нет. Возвращает True при «Да»/«OK»."""
    ok, _ = _run_dialog(text, kind, title, details, ask, limit, None, details_header)
    return ok


# ==========================================================
# XAML — ОБЩИЕ РЕСУРСЫ (СТИЛИ ОКНА АУДИТА) И ОКНО ПЛАГИНА
# ==========================================================
XAML_RESOURCES = u"""
        <SolidColorBrush x:Key="Accent" Color="#FFFF7A1A"/>
        <LinearGradientBrush x:Key="AccentGrad" StartPoint="0,0" EndPoint="0,1">
            <GradientStop Color="#FFFF9440" Offset="0"/>
            <GradientStop Color="#FFFF6A00" Offset="1"/>
        </LinearGradientBrush>
        <LinearGradientBrush x:Key="GlassFill" StartPoint="0,0" EndPoint="1,1">
            <GradientStop Color="#E6FFFFFF" Offset="0"/>
            <GradientStop Color="#B8FFFFFF" Offset="1"/>
        </LinearGradientBrush>

        <Style x:Key="ScrollThumbStyle" TargetType="Thumb">
            <Setter Property="SnapsToDevicePixels" Value="True"/>
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Setter Property="IsTabStop" Value="False"/>
            <Setter Property="Focusable" Value="False"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Thumb">
                        <Border Name="thumbBd" Background="{StaticResource AccentGrad}" BorderBrush="#80FFFFFF"
                                BorderThickness="1" CornerRadius="5" Opacity="0.72"/>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="thumbBd" Property="Opacity" Value="0.95"/>
                            </Trigger>
                            <Trigger Property="IsDragging" Value="True">
                                <Setter TargetName="thumbBd" Property="Opacity" Value="1.0"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="ScrollPageButtonStyle" TargetType="RepeatButton">
            <Setter Property="SnapsToDevicePixels" Value="True"/>
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Setter Property="IsTabStop" Value="False"/>
            <Setter Property="Focusable" Value="False"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="RepeatButton">
                        <Border Background="Transparent"/>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style TargetType="ScrollBar">
            <Setter Property="SnapsToDevicePixels" Value="True"/>
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Style.Triggers>
                <Trigger Property="Orientation" Value="Vertical">
                    <Setter Property="Width" Value="10"/>
                    <Setter Property="Height" Value="Auto"/>
                    <Setter Property="Margin" Value="4,2,2,2"/>
                    <Setter Property="Template">
                        <Setter.Value>
                            <ControlTemplate TargetType="ScrollBar">
                                <Grid>
                                    <Border Background="#1FFF7A1A" BorderBrush="#33FF7A1A" BorderThickness="1" CornerRadius="5"/>
                                    <Track Name="PART_Track" IsDirectionReversed="True">
                                        <Track.DecreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageUpCommand"/>
                                        </Track.DecreaseRepeatButton>
                                        <Track.Thumb>
                                            <Thumb Style="{StaticResource ScrollThumbStyle}" Margin="1"/>
                                        </Track.Thumb>
                                        <Track.IncreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageDownCommand"/>
                                        </Track.IncreaseRepeatButton>
                                    </Track>
                                </Grid>
                            </ControlTemplate>
                        </Setter.Value>
                    </Setter>
                </Trigger>
                <Trigger Property="Orientation" Value="Horizontal">
                    <Setter Property="Width" Value="Auto"/>
                    <Setter Property="Height" Value="10"/>
                    <Setter Property="Margin" Value="2,4,2,2"/>
                    <Setter Property="Template">
                        <Setter.Value>
                            <ControlTemplate TargetType="ScrollBar">
                                <Grid>
                                    <Border Background="#1FFF7A1A" BorderBrush="#33FF7A1A" BorderThickness="1" CornerRadius="5"/>
                                    <Track Name="PART_Track" IsDirectionReversed="False">
                                        <Track.DecreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageLeftCommand"/>
                                        </Track.DecreaseRepeatButton>
                                        <Track.Thumb>
                                            <Thumb Style="{StaticResource ScrollThumbStyle}" Margin="1"/>
                                        </Track.Thumb>
                                        <Track.IncreaseRepeatButton>
                                            <RepeatButton Style="{StaticResource ScrollPageButtonStyle}" Command="ScrollBar.PageRightCommand"/>
                                        </Track.IncreaseRepeatButton>
                                    </Track>
                                </Grid>
                            </ControlTemplate>
                        </Setter.Value>
                    </Setter>
                </Trigger>
            </Style.Triggers>
        </Style>

        <Style x:Key="GlassCard" TargetType="Border">
            <Setter Property="Background" Value="{StaticResource GlassFill}"/>
            <Setter Property="BorderBrush" Value="#80FFFFFF"/>
            <Setter Property="BorderThickness" Value="1"/>
            <Setter Property="CornerRadius" Value="20"/>
        </Style>

        <Style x:Key="PrimaryButton" TargetType="Button">
            <Setter Property="Foreground" Value="White"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Padding" Value="20,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="{StaticResource AccentGrad}" CornerRadius="19" BorderThickness="0" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="bd" Property="Opacity" Value="0.9"/></Trigger>
                            <Trigger Property="IsEnabled" Value="False"><Setter TargetName="bd" Property="Opacity" Value="0.45"/></Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="GlassButton" TargetType="Button">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Padding" Value="18,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1" Padding="{TemplateBinding Padding}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/></Trigger>
                            <Trigger Property="IsEnabled" Value="False"><Setter TargetName="bd" Property="Opacity" Value="0.45"/></Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Круглая кнопка «...» для выбора файла или папки -->
        <Style x:Key="IconButton" TargetType="Button">
            <Setter Property="Foreground" Value="#FFC24E00"/>
            <Setter Property="FontWeight" Value="Bold"/>
            <Setter Property="Width" Value="38"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Красная круглая кнопка удаления строки -->
        <Style x:Key="RemoveButton" TargetType="Button">
            <Setter Property="Foreground" Value="#FFD93025"/>
            <Setter Property="FontWeight" Value="Bold"/>
            <Setter Property="Width" Value="38"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="ToolTip" Value="Убрать модель из списка"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Button">
                        <Border Name="bd" Background="#14E8452C" CornerRadius="19" BorderBrush="#66E8452C" BorderThickness="1">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#26E8452C"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFE8452C"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <!-- Переключатель-«таблетка» (режим экспорта, время запуска) -->
        <Style x:Key="ModePill" TargetType="RadioButton">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="MinWidth" Value="110"/>
            <Setter Property="Margin" Value="0,0,8,0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="RadioButton">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1" Padding="18,0">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                            </Trigger>
                            <Trigger Property="IsChecked" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="{StaticResource AccentGrad}"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="Transparent"/>
                                <Setter Property="Foreground" Value="White"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PillTextBox" TargetType="TextBox">
            <Setter Property="Height" Value="38"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Padding" Value="12,0"/>
            <Setter Property="VerticalContentAlignment" Value="Center"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="TextBox">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1">
                            <ScrollViewer Name="PART_ContentHost" Margin="{TemplateBinding Padding}" VerticalAlignment="Center"/>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsKeyboardFocused" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                            </Trigger>
                            <Trigger Property="IsEnabled" Value="False">
                                <Setter TargetName="bd" Property="Opacity" Value="0.5"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="PillComboBox" TargetType="ComboBox">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ComboBox">
                        <Grid>
                            <ToggleButton Name="ToggleButton" Focusable="False" ClickMode="Press"
                                          IsChecked="{Binding Path=IsDropDownOpen, Mode=TwoWay, RelativeSource={RelativeSource TemplatedParent}}">
                                <ToggleButton.Template>
                                    <ControlTemplate TargetType="ToggleButton">
                                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1" Padding="16,0,14,0">
                                            <Grid>
                                                <Grid.ColumnDefinitions>
                                                    <ColumnDefinition Width="Auto"/>
                                                    <ColumnDefinition Width="*"/>
                                                    <ColumnDefinition Width="Auto"/>
                                                </Grid.ColumnDefinitions>
                                                <TextBlock Grid.Column="0" Text="{Binding Path=Tag, RelativeSource={RelativeSource AncestorType=ComboBox}}" Foreground="#FF8C7A6B" FontSize="12" FontWeight="SemiBold" VerticalAlignment="Center" Margin="0,0,8,0"/>
                                                <TextBlock Grid.Column="1" Text="{Binding Path=SelectedItem.Name, RelativeSource={RelativeSource AncestorType=ComboBox}, FallbackValue='Выберите...'}"
                                                           Foreground="#FF2B2118" FontSize="13" FontWeight="Bold" VerticalAlignment="Center" TextTrimming="CharacterEllipsis"/>
                                                <Path Grid.Column="2" Data="M0,0 L5,5 L10,0" Stroke="#FFFF7A1A" StrokeThickness="2" VerticalAlignment="Center" Margin="10,2,0,0"/>
                                            </Grid>
                                        </Border>
                                        <ControlTemplate.Triggers>
                                            <Trigger Property="IsMouseOver" Value="True">
                                                <Setter TargetName="bd" Property="Background" Value="#FFFFFFFF"/>
                                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                                            </Trigger>
                                            <Trigger Property="IsChecked" Value="True">
                                                <Setter TargetName="bd" Property="Background" Value="#FFFFF4EB"/>
                                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFF7A1A"/>
                                            </Trigger>
                                        </ControlTemplate.Triggers>
                                    </ControlTemplate>
                                </ToggleButton.Template>
                            </ToggleButton>
                            <Popup Name="Popup" Placement="Bottom" VerticalOffset="6" IsOpen="{TemplateBinding IsDropDownOpen}"
                                   AllowsTransparency="True" Focusable="False" PopupAnimation="Slide">
                                <Border Background="#FFFFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Padding="6"
                                        MinWidth="{TemplateBinding ActualWidth}" MaxHeight="320">
                                    <ScrollViewer SnapsToDevicePixels="True" VerticalScrollBarVisibility="Auto">
                                        <StackPanel IsItemsHost="True" KeyboardNavigation.DirectionalNavigation="Contained"/>
                                    </ScrollViewer>
                                </Border>
                            </Popup>
                        </Grid>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
            <Setter Property="ItemContainerStyle">
                <Setter.Value>
                    <Style TargetType="ComboBoxItem">
                        <Setter Property="Padding" Value="12,8"/>
                        <Setter Property="Cursor" Value="Hand"/>
                        <Setter Property="Template">
                            <Setter.Value>
                                <ControlTemplate TargetType="ComboBoxItem">
                                    <Border Name="ibd" Background="Transparent" CornerRadius="10" Padding="{TemplateBinding Padding}">
                                        <TextBlock Text="{Binding Name}" FontWeight="SemiBold" FontSize="13" Foreground="#FF2B2118"/>
                                    </Border>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsHighlighted" Value="True">
                                            <Setter TargetName="ibd" Property="Background" Value="#1FFF7A1A"/>
                                        </Trigger>
                                        <Trigger Property="IsSelected" Value="True">
                                            <Setter TargetName="ibd" Property="Background" Value="#33FF7A1A"/>
                                        </Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Setter.Value>
                        </Setter>
                    </Style>
                </Setter.Value>
            </Setter>
        </Style>

        <Style x:Key="NavItem" TargetType="RadioButton">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontSize" Value="14"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="Height" Value="44"/>
            <Setter Property="Margin" Value="0,0,0,6"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="RadioButton">
                        <Border Name="bd" CornerRadius="14" Background="Transparent" Padding="14,0">
                            <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                                <Ellipse Name="dot" Width="8" Height="8" Fill="{StaticResource Accent}" Margin="0,0,10,0"/>
                                <ContentPresenter VerticalAlignment="Center"/>
                            </StackPanel>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="#14FF7A1A"/>
                            </Trigger>
                            <Trigger Property="IsChecked" Value="True">
                                <Setter TargetName="bd" Property="Background" Value="{StaticResource AccentGrad}"/>
                                <Setter TargetName="dot" Property="Fill" Value="White"/>
                                <Setter Property="Foreground" Value="White"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>
"""

XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Схемы СС" Width="1060" Height="720" MinWidth="900" MinHeight="600"
        WindowStartupLocation="CenterScreen" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="CanResizeWithGrip"
        FontFamily="Segoe UI" FontSize="14" Foreground="#FF2B2118">
    <Window.Resources>
__RESOURCES__
    </Window.Resources>

    <Grid Margin="14">
        <Border CornerRadius="26" BorderThickness="1" BorderBrush="#CCFFFFFF">
            <Border.Background>
                <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
                    <GradientStop Color="#F5FFF9F2" Offset="0"/>
                    <GradientStop Color="#F5FFF2E5" Offset="1"/>
                </LinearGradientBrush>
            </Border.Background>

            <Grid ClipToBounds="True">
                <Grid.RowDefinitions>
                    <RowDefinition Height="Auto"/>
                    <RowDefinition Height="*"/>
                </Grid.RowDefinitions>

                <!-- ВЕРХНЯЯ ПАНЕЛЬ -->
                <Grid Name="TitleBar" Grid.Row="0" Height="46" Background="Transparent">
                    <StackPanel Orientation="Horizontal" VerticalAlignment="Center" Margin="24,0,0,0">
                        <Button Name="btnCloseDot" Width="14" Height="14" Background="#FFFF5F57" Margin="0,0,8,0" Cursor="Hand" ToolTip="Закрыть">
                            <Button.Template>
                                <ControlTemplate TargetType="Button">
                                    <Grid Background="Transparent">
                                        <Ellipse Fill="{TemplateBinding Background}"/>
                                        <Path Name="icon" Data="M4,4 L10,10 M10,4 L4,10" Stroke="#66000000" StrokeThickness="1.5" Visibility="Hidden"/>
                                    </Grid>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="icon" Property="Visibility" Value="Visible"/></Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Button.Template>
                        </Button>
                        <Button Name="btnMinDot" Width="14" Height="14" Background="#FFFEBC2E" Margin="0,0,8,0" Cursor="Hand" ToolTip="Свернуть">
                            <Button.Template>
                                <ControlTemplate TargetType="Button">
                                    <Grid Background="Transparent">
                                        <Ellipse Fill="{TemplateBinding Background}"/>
                                        <Path Name="icon" Data="M3.5,7 L10.5,7" Stroke="#66000000" StrokeThickness="1.5" Visibility="Hidden"/>
                                    </Grid>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="icon" Property="Visibility" Value="Visible"/></Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Button.Template>
                        </Button>
                        <Button Name="btnMaxDot" Width="14" Height="14" Background="#FF28C840" Margin="0,0,8,0" Cursor="Hand" ToolTip="Развернуть">
                            <Button.Template>
                                <ControlTemplate TargetType="Button">
                                    <Grid Background="Transparent">
                                        <Ellipse Fill="{TemplateBinding Background}"/>
                                        <Path Name="icon" Data="M3.5,3.5 L7.5,3.5 L3.5,7.5 Z M10.5,10.5 L6.5,10.5 L10.5,6.5 Z" Fill="#66000000" Visibility="Hidden"/>
                                    </Grid>
                                    <ControlTemplate.Triggers>
                                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="icon" Property="Visibility" Value="Visible"/></Trigger>
                                    </ControlTemplate.Triggers>
                                </ControlTemplate>
                            </Button.Template>
                        </Button>
                    </StackPanel>
                </Grid>

                <Grid Grid.Row="1" Margin="18,0,18,18">
                    <Grid.ColumnDefinitions>
                        <ColumnDefinition Width="235"/>
                        <ColumnDefinition Width="16"/>
                        <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>

                    <!-- ЛЕВОЕ МЕНЮ НАВИГАЦИИ -->
                    <Border Grid.Column="0" Style="{StaticResource GlassCard}" Padding="14">
                        <Grid>
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <TextBlock Grid.Row="0" Text="СХЕМЫ СС" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navGenerate" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Генерация схемы"/>
                                <RadioButton Name="navParams" GroupName="nav" Style="{StaticResource NavItem}" Content="Параметры проекта"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="ПАРАМЕТРЫ CEC_" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                    <TextBlock Name="tbSidebarParams" Text="—" FontSize="24" FontWeight="Bold" Foreground="#FFFF6A00" Margin="0,2,0,0"/>
                                    <TextBlock Name="tbSidebarDoc" Text="Модель" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis"/>
                                </StackPanel>
                            </Border>
                        </Grid>
                    </Border>

                    <!-- ПРАВАЯ РАБОЧАЯ ОБЛАСТЬ -->
                    <Grid Grid.Column="2">

                        <!-- ========================================== -->
                        <!-- РАЗДЕЛ 1: ГЕНЕРАЦИЯ СХЕМЫ                 -->
                        <!-- ========================================== -->
                        <Grid Name="pageGenerate" Visibility="Visible">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <StackPanel Grid.Row="0" Margin="4,0,4,12">
                                <TextBlock Text="Генерация схемы СС" FontSize="26" FontWeight="Bold"/>
                                <TextBlock Text="Этажи по отметкам → щиты → шлейфы → устройства в порядке подключения. Данные — из параметров CEC_" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
                            </StackPanel>
                            <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                                <StackPanel Margin="0,0,6,0">
                                    <Border Style="{StaticResource GlassCard}" Padding="18,14" Margin="0,0,0,12">
                                        <StackPanel>
                                            <TextBlock Text="Что строим" FontSize="16" FontWeight="Bold" Margin="0,0,0,10"/>
                                            <Grid Margin="0,0,0,10">
                                                <Grid.ColumnDefinitions>
                                                    <ColumnDefinition Width="*"/>
                                                    <ColumnDefinition Width="Auto"/>
                                                </Grid.ColumnDefinitions>
                                                <ComboBox Name="cbSystems" Style="{StaticResource PillComboBox}" Tag="Система:"/>
                                                <Button Name="btnRefreshSystems" Grid.Column="1" Content="Обновить список" Style="{StaticResource GlassButton}" Margin="10,0,0,0"/>
                                            </Grid>
                                            <Grid>
                                                <Grid.ColumnDefinitions>
                                                    <ColumnDefinition Width="Auto"/>
                                                    <ColumnDefinition Width="*"/>
                                                </Grid.ColumnDefinitions>
                                                <TextBlock Text="Имя чертёжного вида:" FontWeight="SemiBold" VerticalAlignment="Center" Margin="4,0,12,0"/>
                                                <TextBox Name="txtViewName" Grid.Column="1" Style="{StaticResource PillTextBox}" Text="Схема СС - Авто"/>
                                            </Grid>
                                        </StackPanel>
                                    </Border>
                                    <Border Style="{StaticResource GlassCard}" Padding="18,14" Margin="0,0,0,12">
                                        <StackPanel>
                                            <TextBlock Text="Правила построения" FontSize="16" FontWeight="Bold" Margin="0,0,0,10"/>
                                            <ComboBox Name="cbOrder" Style="{StaticResource PillComboBox}" Tag="Порядок устройств в шлейфе:" Margin="0,0,0,8"/>
                                            <ComboBox Name="cbLevelLabel" Style="{StaticResource PillComboBox}" Tag="Подпись этажа:"/>
                                            <TextBlock FontSize="12" Foreground="#FF8C7A6B" TextWrapping="Wrap" Margin="4,10,0,0"
                                                       Text="«По удалённости от щита» — от щита к ближайшему устройству, затем к ближайшему из оставшихся (этажи по удалённости от этажа щита). «По маркам» — если марки уже пронумерованы в порядке подключения (L1.1, L1.2…)."/>
                                        </StackPanel>
                                    </Border>
                                    <Border Style="{StaticResource GlassCard}" Padding="18,14">
                                        <StackPanel>
                                            <TextBlock Text="Демо-схема" FontSize="16" FontWeight="Bold" Margin="0,0,0,6"/>
                                            <TextBlock FontSize="13" TextWrapping="Wrap" Foreground="#FF8C7A6B"
                                                       Text="Строит схему на синтетических данных (2 прибора, 3 этажа, длинный шлейф, повтор L1 у двух приборов) на отдельном виде с пометкой «демо». Элементы в модели не нужны — удобно проверять логику раскладки."/>
                                        </StackPanel>
                                    </Border>
                                </StackPanel>
                            </ScrollViewer>
                            <Grid Grid.Row="2" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbGenerateStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
                                <Button Name="btnDemo" Grid.Column="1" Content="Демо-схема" Style="{StaticResource GlassButton}" Margin="0,0,10,0"/>
                                <Button Name="btnGenerate" Grid.Column="2" Content="Сгенерировать схему" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                        <!-- ========================================== -->
                        <!-- РАЗДЕЛ 2: ПАРАМЕТРЫ ПРОЕКТА               -->
                        <!-- ========================================== -->
                        <Grid Name="pageParams" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <StackPanel Grid.Row="0" Margin="4,0,4,12">
                                <TextBlock Text="Параметры проекта" FontSize="26" FontWeight="Bold"/>
                                <TextBlock Text="Привязка параметров CEC_ к категориям оборудования из вашего ФОП — одной кнопкой" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
                            </StackPanel>
                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,0,12">
                                <Grid>
                                    <Grid.ColumnDefinitions>
                                        <ColumnDefinition Width="Auto"/>
                                        <ColumnDefinition Width="*"/>
                                        <ColumnDefinition Width="Auto"/>
                                    </Grid.ColumnDefinitions>
                                    <TextBlock Text="Файл общих параметров (ФОП):" FontWeight="SemiBold" VerticalAlignment="Center" Margin="0,0,12,0"/>
                                    <TextBox Name="txtFop" Grid.Column="1" Style="{StaticResource PillTextBox}"/>
                                    <Button Name="btnBrowseFop" Grid.Column="2" Style="{StaticResource IconButton}" Content="..." Margin="8,0,0,0" ToolTip="Выбрать ФОП"/>
                                </Grid>
                            </Border>
                            <Border Grid.Row="2" Style="{StaticResource GlassCard}" Padding="14,10,6,10">
                                <Grid>
                                    <Grid.RowDefinitions>
                                        <RowDefinition Height="Auto"/>
                                        <RowDefinition Height="*"/>
                                    </Grid.RowDefinitions>
                                    <Grid Margin="12,4,18,8">
                                        <Grid.ColumnDefinitions>
                                            <ColumnDefinition Width="*"/>
                                            <ColumnDefinition Width="90"/>
                                            <ColumnDefinition Width="100"/>
                                            <ColumnDefinition Width="80"/>
                                            <ColumnDefinition Width="200"/>
                                        </Grid.ColumnDefinitions>
                                        <TextBlock Text="ПАРАМЕТР" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                        <TextBlock Grid.Column="1" Text="ДАННЫЕ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                        <TextBlock Grid.Column="2" Text="ПРИВЯЗКА" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                        <TextBlock Grid.Column="3" Text="В ФОП" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                        <TextBlock Grid.Column="4" Text="В ПРОЕКТЕ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                    </Grid>
                                    <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                                        <ItemsControl Name="icParams" Margin="0,0,6,0">
                                            <ItemsControl.ItemTemplate>
                                                <DataTemplate>
                                                    <Border Background="#B3FFFFFF" BorderBrush="#66FFFFFF" BorderThickness="1" CornerRadius="12" Padding="12,8" Margin="0,0,0,6">
                                                        <Grid>
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="*"/>
                                                                <ColumnDefinition Width="90"/>
                                                                <ColumnDefinition Width="100"/>
                                                                <ColumnDefinition Width="80"/>
                                                                <ColumnDefinition Width="200"/>
                                                            </Grid.ColumnDefinitions>
                                                            <StackPanel VerticalAlignment="Center">
                                                                <TextBlock Text="{Binding Name}" FontWeight="SemiBold"/>
                                                                <TextBlock Text="{Binding Description}" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis"/>
                                                            </StackPanel>
                                                            <TextBlock Grid.Column="1" Text="{Binding DataText}" FontSize="13" VerticalAlignment="Center"/>
                                                            <TextBlock Grid.Column="2" Text="{Binding BindText}" FontSize="13" VerticalAlignment="Center"/>
                                                            <TextBlock Grid.Column="3" Text="{Binding FopText}" FontSize="13" FontWeight="SemiBold" VerticalAlignment="Center"/>
                                                            <Border Grid.Column="4" Background="{Binding StatusBg}" CornerRadius="9" Padding="10,4" HorizontalAlignment="Left" VerticalAlignment="Center">
                                                                <TextBlock Text="{Binding ProjectText}" Foreground="{Binding StatusFg}" FontSize="12" FontWeight="SemiBold" TextWrapping="Wrap"/>
                                                            </Border>
                                                        </Grid>
                                                    </Border>
                                                </DataTemplate>
                                            </ItemsControl.ItemTemplate>
                                        </ItemsControl>
                                    </ScrollViewer>
                                </Grid>
                            </Border>
                            <Grid Grid.Row="3" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbParamsStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
                                <Button Name="btnCheckParams" Grid.Column="1" Content="Проверить" Style="{StaticResource GlassButton}" Margin="0,0,10,0"/>
                                <Button Name="btnPrepare" Grid.Column="2" Content="Подготовить проект" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>
                    </Grid>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>"""

CONTROL_NAMES = (
    "TitleBar", "btnCloseDot", "btnMinDot", "btnMaxDot",
    "navGenerate", "navParams", "pageGenerate", "pageParams", "tbSidebarParams", "tbSidebarDoc",
    "cbSystems", "btnRefreshSystems", "txtViewName", "cbOrder", "cbLevelLabel",
    "tbGenerateStatus", "btnDemo", "btnGenerate",
    "txtFop", "btnBrowseFop", "icParams", "tbParamsStatus", "btnCheckParams", "btnPrepare",
)


class Option(object):
    """Пункт выпадающего списка (PillComboBox показывает свойство Name)."""
    def __init__(self, name, value):
        self.Name = name
        self.Value = value

    def ToString(self):
        return self.Name

    def __str__(self):
        return self.Name


ORDER_OPTIONS = [Option(u"По удалённости от щита", "path"), Option(u"По маркам", "mark")]
LEVEL_LABEL_OPTIONS = [Option(u"Отметка (Отм. +6.150)", "elev"), Option(u"Имя уровня", "name"),
                       Option(u"Имя уровня и отметка", "both")]


def load_settings():
    try:
        with open(SETTINGS_FILE, 'rb') as f:
            return json.loads(f.read().decode('utf-8'))
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, 'wb') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
    except Exception:
        pass



class RevitActions(IExternalEventHandler):
    def __init__(self):
        self._queue = []
        self._event = ExternalEvent.Create(self)

    def request(self, fn):
        self._queue.append(fn)
        try:
            result = self._event.Raise()
            if result not in (ExternalEventRequest.Accepted, ExternalEventRequest.Pending):
                raise Exception(u"ExternalEvent не принят: {}".format(result))
        except Exception:
            self._queue.remove(fn)
            raise

    def Execute(self, uiapp):
        queue, self._queue = self._queue, []
        for fn in queue:
            try:
                fn(uiapp)
            except Exception:
                show_message(u"Ошибка выполнения операции:\n{}".format(traceback.format_exc()), title=u"Ошибка")

    def GetName(self):
        return u"Генератор схем СС"


class Canvas(object):
    def __init__(self, doc, view_id, symbols, styles, text_id, size_mm):
        self.doc, self.view_id = doc, view_id
        self.symbols, self.styles = symbols, styles
        self.text_id = text_id
        view = self.get_view()
        self.k = UNIT_MM * float(view.Scale or 100) / MM_IN_FT
        self.opts = DB.TextNoteOptions(text_id) if valid_id(text_id) else None
        self.txt_h = size_mm / UNIT_MM
        self.lh, self.cw = self.txt_h * 1.35, self.txt_h * TEXT_W_K
        self.placed_names, self.missing_families = set(), set()
        self.offsets = {}
        self.placed_total = self.missing_param = self.device_total = 0

    def get_view(self):
        view = self.doc.GetElement(self.view_id)
        if view is None or not view.IsValidObject:
            raise Exception(u"Чертежный вид больше не существует.")
        return view

    def style(self, key):
        sid = self.styles.get(key)
        if not valid_id(sid):
            return None
        gs = self.doc.GetElement(sid)
        if gs is None or not gs.IsValidObject:
            raise Exception(u"Стиль линии '{}' недействителен.".format(key))
        return gs

    def text_w(self, txt):
        return len(txt or u"") * self.cw

    def text(self, x, y, txt, width=None):
        if self.opts is None or not txt:
            return
        pos = DB.XYZ(x * self.k, y * self.k, 0.0)
        if width is not None:
            try:
                w = width * UNIT_MM / MM_IN_FT
                lo = DB.TextNote.GetMinimumAllowedWidth(self.doc, self.text_id)
                hi = DB.TextNote.GetMaximumAllowedWidth(self.doc, self.text_id)
                DB.TextNote.Create(self.doc, self.view_id, pos, min(max(w, lo), hi), txt, self.opts)
                return
            except Exception:
                pass
        DB.TextNote.Create(self.doc, self.view_id, pos, txt, self.opts)

    def text_c(self, cx, y, txt):
        self.text(cx - self.text_w(txt) / 2.0, y, txt)

    def line(self, x1, y1, x2, y2, style=None):
        dx, dy = (x2 - x1) * self.k, (y2 - y1) * self.k
        if math.sqrt(dx * dx + dy * dy) < 0.005:
            return
        curve = DB.Line.CreateBound(DB.XYZ(x1 * self.k, y1 * self.k, 0.0),
                                    DB.XYZ(x2 * self.k, y2 * self.k, 0.0))
        dc = self.doc.Create.NewDetailCurve(self.get_view(), curve)
        if style:
            gs = self.style(style)
            if gs is not None:
                dc.LineStyle = gs

    def rect(self, x, ytop, w, h, style=None):
        self.line(x, ytop, x + w, ytop, style)
        self.line(x + w, ytop, x + w, ytop - h, style)
        self.line(x + w, ytop - h, x, ytop - h, style)
        self.line(x, ytop - h, x, ytop, style)

    def circle(self, cx, cy, r, style=None):
        c, rr = DB.XYZ(cx * self.k, cy * self.k, 0.0), r * self.k
        left = DB.XYZ(c.X - rr, c.Y, 0.0)
        right = DB.XYZ(c.X + rr, c.Y, 0.0)
        for top in (DB.XYZ(c.X, c.Y + rr, 0.0), DB.XYZ(c.X, c.Y - rr, 0.0)):
            dc = self.doc.Create.NewDetailCurve(self.get_view(), DB.Arc.Create(left, right, top))
            if style:
                gs = self.style(style)
                if gs is not None:
                    dc.LineStyle = gs

    def symbol(self, name, x, y):
        sid = self.symbols.get(name.strip().lower())
        sym = self.doc.GetElement(sid) if valid_id(sid) else None
        if sym is None or not sym.IsValidObject:
            self.missing_families.add(name)
            return False
        if not sym.IsActive:
            sym.Activate()
            self.doc.Regenerate()
            sym = self.doc.GetElement(sid)
            if sym is None or not sym.IsValidObject:
                raise Exception(u"Тип УГО '{}' утрачен после Regenerate().".format(name))
        self.place(sid, sym, x * self.k, y * self.k)
        self.placed_names.add(name.strip().lower())
        self.placed_total += 1
        return True

    def place(self, sid, sym, px, py):
        """Ставит УГО так, чтобы центр его габаритов совпал с точкой (px, py),
        где бы в семействе ни находилась точка вставки. Смещение измеряется один раз на тип."""
        view = self.get_view()
        key = sid.ToString()
        off = self.offsets.get(key)
        if off is not None:
            self.doc.Create.NewFamilyInstance(DB.XYZ(px - off[0], py - off[1], 0.0), sym, view)
            return
        inst = self.doc.Create.NewFamilyInstance(DB.XYZ(px, py, 0.0), sym, view)
        off = (0.0, 0.0)
        try:
            inst_id = inst.Id
            self.doc.Regenerate()
            inst = self.doc.GetElement(inst_id)
            bb = inst.get_BoundingBox(self.get_view())
            if bb is not None:
                off = ((bb.Min.X + bb.Max.X) / 2.0 - px, (bb.Min.Y + bb.Max.Y) / 2.0 - py)
                if abs(off[0]) > 1e-6 or abs(off[1]) > 1e-6:
                    DB.ElementTransformUtils.MoveElement(
                        self.doc, inst_id, DB.XYZ(-off[0], -off[1], 0.0))
        except Exception:
            off = (0.0, 0.0)
        self.offsets[key] = off

    def cross_mark(self, cx, cy, s, style, ugo=None):
        if ugo and self.symbol(ugo, cx, cy):
            return
        h = s / 2.0
        self.line(cx - h, cy + h, cx - h, cy - h, style)
        self.line(cx + h, cy + h, cx + h, cy - h, style)
        self.line(cx - h, cy + h, cx + h, cy - h, style)
        self.line(cx - h, cy - h, cx + h, cy + h, style)

    def device(self, rec, x, y):
        self.device_total += 1
        name = rec["ugo"]
        if not name:
            self.missing_param += 1
        elif self.symbol(name, x, y):
            return
        h = 0.25
        self.rect(x - h, y + h, 2 * h, 2 * h, "cab")


def generate_scheme(uiapp, sys_name, view_name, demo=False):
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None or uidoc.Document is None:
        show_message(u"Нет открытого документа.")
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект, а не документ семейства.")
        return

    stage = u"Сбор данных"
    transaction = None
    try:
        records = demo_records() if demo else collect_records(doc, sys_name)
        if not records:
            show_message(u"Элементы системы «{}» не найдены.".format(sys_name))
            return
        stage = u"Проверка вида и типовых аннотаций"
        existing_id = find_view_id(doc, view_name)
        existing = doc.GetElement(existing_id) if valid_id(existing_id) else None
        if existing is not None and (not isinstance(existing, DB.ViewDrafting) or existing.IsTemplate):
            show_message(u"Имя занято другим видом: «{}».".format(view_name))
            return
        view_type_id = drafting_type_id(doc)
        if existing is None and not valid_id(view_type_id):
            show_message(u"В проекте нет типа чертёжного вида.")
            return
        if existing is not None:
            count = view_content_ids(doc, existing).Count
            if count and not show_message(
                    u"Вид «{}» уже существует. Всё его содержимое ({} элем.), включая ручные правки, "
                    u"будет удалено и схема будет нарисована заново. Продолжить?".format(view_name, count), ask=True):
                return
        symbols = symbol_cache(doc)

        transaction = DB.Transaction(doc, u"Генерация схемы СС")
        stage = u"Начало транзакции"
        transaction.Start()
        stage = u"Создание или очистка вида"
        if existing is None:
            view = DB.ViewDrafting.Create(doc, view_type_id)
            view.Name = view_name
            view_id = view.Id
        else:
            view_id = existing_id
            clear_view(doc, existing)
            if not view_alive(doc, view_id):
                view = DB.ViewDrafting.Create(doc, view_type_id)
                view.Name = view_name
                view_id = view.Id

        stage = u"Подготовка типа текста"
        text_id = doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)
        if not valid_id(text_id):
            first_text = DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType).FirstElement()
            text_id = first_text.Id if first_text is not None else None
        size_mm = 2.5
        if valid_id(text_id):
            tp = doc.GetElement(text_id).get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
            if tp is not None and tp.HasValue:
                size_mm = max(tp.AsDouble() * MM_IN_FT, 1.0)

        stage = u"Создание стилей линий"
        styles = ensure_line_styles(doc)
        if not view_alive(doc, view_id):
            raise Exception(u"Вид пропал после создания стилей линий.")
        stage = u"Отрисовка схемы"
        cv = Canvas(doc, view_id, symbols, styles, text_id, size_mm)
        cabs = draw_hierarchy(cv, records)
        stage = u"Фиксация транзакции"
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise Exception(u"Транзакция не была зафиксирована.")
    except Exception:
        details = traceback.format_exc()
        if transaction is not None:
            try:
                if transaction.GetStatus() == DB.TransactionStatus.Started:
                    transaction.RollBack()
            except Exception:
                details += u"\nОшибка при откате:\n" + traceback.format_exc()
        show_message(u"Ошибка генерации. Этап: {}".format(stage), title=u"Ошибка генерации",
                     details=details.splitlines(), details_header=u"Подробности")
        return

    try:
        view = doc.GetElement(view_id)
        if view is not None and view.IsValidObject:
            uidoc.ActiveView = view
    except Exception:
        pass

    real = [c for c in cabs.values() if c["panel"] is not None]
    virtual = sorted([c["name"] for c in cabs.values() if c["panel"] is None], key=natural_key)
    no_level = len([r for r in records if r["level"] == TXT_NO_LEVEL])
    loops = sum(len(c["loops"]) for c in cabs.values())
    summary = u"Схема{} построена на виде «{}».\nЩитов: {} • Шлейфов: {} • УГО на схеме: {} • Уникальных УГО: {}".format(
        u" (демо)" if demo else u"", view_name, len(real), loops, cv.device_total, len(cv.placed_names))

    notes = list(WARNINGS)
    if virtual:
        notes.append(u"Нет элемента-щита (CEC_Это_Щит = Да) для: {}. Щит нарисован условно.".format(u", ".join(virtual)))
    if no_level:
        notes.append(u"Элементов без уровня: {}".format(no_level))
    if cv.missing_param and not demo:
        notes.append(u"Элементов без CEC_Имя_УГО (нарисованы квадратом): {}".format(cv.missing_param))
    for name in sorted(cv.missing_families, key=natural_key):
        notes.append(u"Не найдена типовая аннотация: {}".format(name))
    if notes:
        show_message(summary, kind='warn', title=u"Отчёт о генерации", details=notes,
                     details_header=u"Замечания к данным: {}".format(len(notes)))
    else:
        show_message(summary, kind='info', title=u"Отчёт о генерации")


# ==========================================================
# ПАРАМЕТРЫ ПРОЕКТА: ПРИВЯЗКА CEC_ ИЗ ФОП ПОЛЬЗОВАТЕЛЯ
# ==========================================================
STATUS_COLORS = {
    'ok': ("#1A1E8E3E", "#FF1E7A34"),
    'warn': ("#1AFF7A1A", "#FFC24E00"),
    'err': ("#1AE8452C", "#FFA8241A"),
}


class ParamRow(object):
    def __init__(self, spec):
        self.Name, data_kind, bind_kind, self.Description = spec
        self.is_type = bind_kind == "type"
        self.is_yesno = data_kind == "yesno"
        self.DataText = u"Да/Нет" if self.is_yesno else u"Текст"
        self.BindText = u"Тип" if self.is_type else u"Экземпляр"
        self.in_fop = None          # None — ФОП не указан или не прочитан
        self.state = 'none'         # none | ok | partial | other
        self.missing_cats = []
        self.FopText = u"—"
        self.set_status(u"Не привязан", 'err')

    def set_status(self, text, kind):
        self.ProjectText = text
        self.StatusBg, self.StatusFg = STATUS_COLORS[kind]


def target_categories(doc):
    cats = []
    for name in TARGET_CATEGORY_NAMES:
        bic = getattr(DB.BuiltInCategory, name, None)
        if bic is None:
            continue
        try:
            cat = doc.Settings.Categories.get_Item(bic)
        except Exception:
            cat = None
        if cat is not None and cat.AllowsBoundParameters:
            cats.append(cat)
    return cats


def project_bindings(doc):
    """{имя параметра: (определение, привязка)} для параметров проекта."""
    out = {}
    it = doc.ParameterBindings.ForwardIterator()
    it.Reset()
    while it.MoveNext():
        out[it.Key.Name] = (it.Key, it.Current)
    return out


def read_fop_definitions(app, fop_path):
    """Открывает ФОП и возвращает (файл, {имя: определение}) для CEC_.
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
    """Состояние каждого CEC_: есть ли в ФОП, привязан ли к проекту и ко всем ли категориям."""
    cats = target_categories(doc)
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
            is_type = isinstance(binding, DB.TypeBinding)
            if row.missing_cats:
                row.state = 'partial'
                row.set_status(u"Нет категорий: {}".format(len(row.missing_cats)), 'warn')
            elif is_type != row.is_type:
                # Скрипт читает и тип, и экземпляр — работать будет, просто привязка иная
                row.state = 'other'
                row.set_status(u"Привязан как {}".format(u"тип" if is_type else u"экземпляр"), 'ok')
            else:
                row.state = 'ok'
                row.set_status(u"Привязан", 'ok')
        rows.append(row)
    return rows, cats, fop_error


def create_fop_definition(fop, row):
    group = None
    for g in fop.Groups:
        if g.Name == FOP_GROUP_NAME:
            group = g
    if group is None:
        group = fop.Groups.Create(FOP_GROUP_NAME)
    try:
        spec = DB.SpecTypeId.Boolean.YesNo if row.is_yesno else DB.SpecTypeId.String.Text
        opts = DB.ExternalDefinitionCreationOptions(row.Name, spec)
    except Exception:
        # Запасной вариант на случай, если в этой сборке API нет конструктора с ForgeTypeId
        ptype = DB.ParameterType.YesNo if row.is_yesno else DB.ParameterType.Text
        opts = DB.ExternalDefinitionCreationOptions(row.Name, ptype)
    opts.Description = row.Description
    return group.Definitions.Create(opts)


def prepare_params(uiapp, fop_path):
    """Привязывает CEC_ к категориям оборудования. Недостающие в ФОП определения дописывает (после подтверждения)."""
    doc = uiapp.ActiveUIDocument.Document
    app = uiapp.Application
    rows, cats, fop_error = analyze_params(doc, app, fop_path)
    if fop_error:
        show_message(u"ФОП не прочитан: {}".format(fop_error))
        return False

    to_bind = [r for r in rows if r.state == 'none']
    to_extend = [r for r in rows if r.state == 'partial']
    to_fop = [r for r in to_bind if not r.in_fop]
    if not to_bind and not to_extend:
        show_message(u"Все параметры CEC_ уже привязаны ко всем нужным категориям.", kind='info')
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
        lines.append(u"Привязать {} как {} к {} категориям".format(r.Name, r.BindText.lower(), len(cats)))
    for r in to_extend:
        lines.append(u"Добавить к {} категории: {}".format(r.Name, u", ".join(c.Name for c in r.missing_cats)))
    question = u"Подготовить параметры проекта?"
    if to_fop:
        question += u" Файл ФОП будет изменён: в него добавятся {} определений.".format(len(to_fop))
    if not show_message(question + u"\n\nКатегории: " + cat_names, ask=True, details=lines,
                        details_header=u"Действий: {}".format(len(lines))):
        return False

    old = app.SharedParametersFilename
    try:
        defs = {}
        if fop_path:
            fop, defs = read_fop_definitions(app, fop_path)
            for r in to_fop:
                defs[r.Name] = create_fop_definition(fop, r)
        bindings = project_bindings(doc)
        t = DB.Transaction(doc, u"Привязка параметров схем СС")
        t.Start()
        try:
            for r in to_bind:
                cat_set = app.Create.NewCategorySet()
                for c in cats:
                    cat_set.Insert(c)
                binding = app.Create.NewTypeBinding(cat_set) if r.is_type else app.Create.NewInstanceBinding(cat_set)
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


# ==========================================================
# ОКНО
# ==========================================================
class SchemaApp(object):
    def __init__(self):
        global dialog_owner
        self.window = XamlReader.Parse(XAML_UI.replace('__RESOURCES__', XAML_RESOURCES))
        dialog_owner = self.window
        for name in CONTROL_NAMES:
            setattr(self, name, self.window.FindName(name))
        self.actions = RevitActions()
        self.settings = load_settings()
        self._params_loaded = False

        self.TitleBar.MouseLeftButtonDown += self.on_drag
        self.btnCloseDot.Click += self.on_close
        self.btnMinDot.Click += self.on_minimize
        self.btnMaxDot.Click += self.on_maximize
        self.window.Closed += self.on_closed
        for nav in (self.navGenerate, self.navParams):
            nav.Checked += self.on_nav_changed

        self.btnGenerate.Click += self.on_generate
        self.btnDemo.Click += self.on_demo
        self.btnRefreshSystems.Click += lambda s, e: self.actions.request(self.load_systems)
        self.btnBrowseFop.Click += self.on_browse_fop
        self.txtFop.LostFocus += lambda s, e: self.remember_fop()
        self.btnCheckParams.Click += lambda s, e: self.actions.request(self.refresh_params)
        self.btnPrepare.Click += self.on_prepare

        self.cbSystems.ItemsSource = [Option(TXT_NO_PARAMS, None)]
        self.cbSystems.SelectedIndex = 0
        self.cbOrder.ItemsSource = ORDER_OPTIONS
        self.cbLevelLabel.ItemsSource = LEVEL_LABEL_OPTIONS
        self.select_option(self.cbOrder, self.settings.get("Order", "path"))
        self.select_option(self.cbLevelLabel, self.settings.get("LevelLabel", "elev"))
        self.txtViewName.Text = self.settings.get("ViewName", u"Схема СС - Авто")
        self.txtFop.Text = self.settings.get("FopPath", u"")

    @staticmethod
    def select_option(combo, value):
        for opt in combo.ItemsSource:
            if opt.Value == value:
                combo.SelectedItem = opt
                return
        combo.SelectedIndex = 0

    # ----------------------------------------------------------
    # ОКНО И НАВИГАЦИЯ
    # ----------------------------------------------------------
    def on_drag(self, sender, args):
        try:
            node = args.OriginalSource
            while node is not None and node != self.TitleBar:
                if isinstance(node, Button):
                    return
                node = VisualTreeHelper.GetParent(node)
            self.window.DragMove()
        except Exception:
            pass

    def on_close(self, sender, args):
        self.window.Close()

    def on_minimize(self, sender, args):
        self.window.WindowState = WindowState.Minimized

    def on_maximize(self, sender, args):
        self.window.WindowState = (WindowState.Normal if self.window.WindowState == WindowState.Maximized
                                   else WindowState.Maximized)

    def on_closed(self, sender, args):
        self.save_ui_settings()
        try:
            if System.AppDomain.CurrentDomain.GetData(APP_KEY) is self:
                System.AppDomain.CurrentDomain.SetData(APP_KEY, None)
        except Exception:
            pass

    def on_nav_changed(self, sender, args):
        if self.pageGenerate is None:
            return
        self.pageGenerate.Visibility = Visibility.Visible if self.navGenerate.IsChecked else Visibility.Collapsed
        self.pageParams.Visibility = Visibility.Visible if self.navParams.IsChecked else Visibility.Collapsed
        if self.navParams.IsChecked and not self._params_loaded:
            self.actions.request(self.refresh_params)

    def save_ui_settings(self):
        self.settings["ViewName"] = (self.txtViewName.Text or u"").strip()
        self.settings["FopPath"] = (self.txtFop.Text or u"").strip()
        if self.cbOrder.SelectedItem is not None:
            self.settings["Order"] = self.cbOrder.SelectedItem.Value
        if self.cbLevelLabel.SelectedItem is not None:
            self.settings["LevelLabel"] = self.cbLevelLabel.SelectedItem.Value
        save_settings(self.settings)

    # ----------------------------------------------------------
    # ГЕНЕРАЦИЯ
    # ----------------------------------------------------------
    def load_systems(self, uiapp):
        try:
            uidoc = uiapp.ActiveUIDocument
            doc = uidoc.Document if uidoc is not None else None
            if doc is not None:
                self.tbSidebarDoc.Text = doc.Title
                if not self.txtFop.Text:
                    self.txtFop.Text = uiapp.Application.SharedParametersFilename or u""
            systems = [] if (doc is None or doc.IsFamilyDocument) else available_systems(doc)
            items = [Option(s, s) for s in systems] or [Option(TXT_NO_PARAMS, None)]
            self.cbSystems.ItemsSource = items
            self.cbSystems.SelectedIndex = 0
            self.tbGenerateStatus.Text = (u"Найдено систем: {}".format(len(systems)) if systems
                                          else u"В модели нет элементов с заполненным CEC_Имя_Системы")
        except Exception:
            show_message(u"Не удалось прочитать системы:\n{}".format(traceback.format_exc()), title=u"Ошибка")

    def set_busy(self, busy):
        self.btnGenerate.IsEnabled = not busy
        self.btnDemo.IsEnabled = not busy
        self.btnGenerate.Content = u"Генерация..." if busy else u"Сгенерировать схему"

    def apply_rule_options(self):
        global ORDER_MODE, LEVEL_LABEL_MODE
        if self.cbOrder.SelectedItem is not None:
            ORDER_MODE = self.cbOrder.SelectedItem.Value
        if self.cbLevelLabel.SelectedItem is not None:
            LEVEL_LABEL_MODE = self.cbLevelLabel.SelectedItem.Value

    def start_generation(self, system, view_name, demo):
        self.apply_rule_options()
        self.save_ui_settings()
        self.set_busy(True)

        def job(uiapp):
            try:
                generate_scheme(uiapp, system, view_name, demo)
            finally:
                self.set_busy(False)

        try:
            self.actions.request(job)
        except Exception:
            self.set_busy(False)
            show_message(u"Ошибка интерфейса:\n{}".format(traceback.format_exc()), title=u"Ошибка")

    def on_generate(self, sender, args):
        opt = self.cbSystems.SelectedItem
        name = (self.txtViewName.Text or u"").strip()
        if opt is None or opt.Value is None or not name:
            show_message(u"Выберите систему и укажите имя вида. Если систем нет — заполните CEC_Имя_Системы "
                         u"у элементов (параметры привязываются в разделе «Параметры проекта»).")
            return
        self.start_generation(opt.Value, name, False)

    def on_demo(self, sender, args):
        name = (self.txtViewName.Text or u"").strip() or u"Схема СС"
        self.start_generation(None, name + u" (демо)", True)

    # ----------------------------------------------------------
    # ПАРАМЕТРЫ ПРОЕКТА
    # ----------------------------------------------------------
    def fop_path(self):
        return (self.txtFop.Text or u"").strip()

    def remember_fop(self):
        self.settings["FopPath"] = self.fop_path()
        save_settings(self.settings)

    def on_browse_fop(self, sender, args):
        dialog = OpenFileDialog()
        dialog.Filter = u"Файл общих параметров (*.txt)|*.txt|Все файлы (*.*)|*.*"
        if dialog.ShowDialog():
            self.txtFop.Text = dialog.FileName
            self.remember_fop()
            self.actions.request(self.refresh_params)

    def refresh_params(self, uiapp):
        uidoc = uiapp.ActiveUIDocument
        if uidoc is None or uidoc.Document is None or uidoc.Document.IsFamilyDocument:
            self.tbParamsStatus.Text = u"Откройте проект"
            return
        doc = uidoc.Document
        self.tbSidebarDoc.Text = doc.Title
        if not self.txtFop.Text:
            self.txtFop.Text = uiapp.Application.SharedParametersFilename or u""
        rows, cats, fop_error = analyze_params(doc, uiapp.Application, self.fop_path())
        self._params_loaded = True
        self.icParams.ItemsSource = rows
        bound = sum(1 for r in rows if r.state in ('ok', 'other'))
        self.tbSidebarParams.Text = u"{} из {}".format(bound, len(rows))
        status = u"Привязано полностью: {} из {} • Категорий оборудования: {}".format(bound, len(rows), len(cats))
        if fop_error:
            status += u" • ФОП не прочитан: {}".format(fop_error)
        elif not self.fop_path():
            status += u" • ФОП не указан"
        self.tbParamsStatus.Text = status

    def on_prepare(self, sender, args):
        self.remember_fop()
        fop = self.fop_path()
        if fop and not os.path.isfile(fop):
            show_message(u"Файл ФОП не найден:\n{}".format(fop))
            return

        def job(uiapp):
            try:
                prepare_params(uiapp, fop)
            finally:
                self.refresh_params(uiapp)

        self.actions.request(job)

    def show(self):
        WindowInteropHelper(self.window).Owner = Process.GetCurrentProcess().MainWindowHandle
        self.window.Show()
        self.actions.request(self.load_systems)


def activate_existing():
    try:
        app = System.AppDomain.CurrentDomain.GetData(APP_KEY)
        if app is not None and app.window.IsLoaded:
            if app.window.WindowState == WindowState.Minimized:
                app.window.WindowState = WindowState.Normal
            app.window.Activate()
            return True
    except Exception:
        pass
    return False


if __name__ == '__main__' and not activate_existing():
    app = SchemaApp()
    System.AppDomain.CurrentDomain.SetData(APP_KEY, app)
    try:
        app.show()
    except Exception:
        System.AppDomain.CurrentDomain.SetData(APP_KEY, None)
        raise

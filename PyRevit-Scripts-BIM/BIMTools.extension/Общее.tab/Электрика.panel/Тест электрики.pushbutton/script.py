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
"""
__title__ = u"Схемы\nСС"
__author__ = u"BIM"
__persistentengine__ = True

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import traceback
import System
from System.Collections.Generic import List
from System.Diagnostics import Process
from System.Windows import WindowState
from System.Windows.Controls import Button
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Markup import XamlReader
from System.Windows.Media import VisualTreeHelper
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent, ExternalEventRequest
from pyrevit import DB, forms

PRM_SYSTEM = u"CEC_Имя_Системы"
PRM_PANEL = u"CEC_Панель"
PRM_LOOP = u"CEC_Номер_Шлейфа"
PRM_UGO = u"CEC_Имя_УГО"
PRM_IS_PANEL = u"CEC_Это_Щит"
PRM_BUILDING = u"CEC_Корпус"
PRM_MARK = u"CEC_Марка"
PRM_ROLE = u"CEC_Роль"

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
    cabs, owner = {}, {}

    def get_cab(name):
        if name not in cabs:
            cabs[name] = {"name": name, "panel": None, "blocks": [], "loops": {}}
        return cabs[name]

    # 1) щиты и перечень шлейфов, объявленных в щите ("L1, L2, S1, S2")
    for r in records:
        if r["role"] != "panel":
            continue
        name = r["panel"] or r["mark"] or r["model"] or TXT_NO_PANEL
        cab = get_cab(name)
        if cab["panel"] is None:
            cab["panel"] = r
        for ln in split_loops(r["loop_raw"]):
            if owner.setdefault(ln, name) == name:
                cab["loops"].setdefault(ln, [])
    real = list(cabs.keys())

    # 2) устройства и блоки: щит по CEC_Панель, иначе по перечню шлейфов, иначе единственный щит
    for r in records:
        if r["role"] == "panel":
            continue
        if r["panel"]:
            name = r["panel"]
        elif r["role"] == "device" and r["loop"] in owner:
            name = owner[r["loop"]]
        elif len(real) == 1:
            name = real[0]
        else:
            name = TXT_NO_PANEL
        cab = get_cab(name)
        if r["role"] == "block":
            cab["blocks"].append(r)
        else:
            cab["loops"].setdefault(r["loop"], []).append(r)

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
            ordered = order_by_path(cab["loops"][lname], start, start_elev)
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
    """Цепочка устройств шлейфа на одном этаже: слева направо в порядке подключения."""
    lh = cv.lh
    style, rx = loop_style(key[1]), risers[key]
    y_t = ytop
    for start in range(0, len(devs), DEV_PER_ROW):
        chunk = devs[start:start + DEV_PER_ROW]
        yc = y_t - (lh + DEV_LABEL_GAP + DEV_R)
        if start == 0:
            bx, by = chain_x0 - box_w - 0.2, yc + 0.1 + lh + 0.2
            cv.rect(bx, by, box_w, lh + 0.2, "cab")
            cv.text_c(bx + box_w / 2.0, by - 0.1, key[1])
        xs = [chain_x0 + cell_w * (i + 0.5) for i in range(len(chunk))]
        cv.line(rx, yc, xs[0] - DEV_R, yc, style)
        touch(key, yc)
        for i, rec in enumerate(chunk):
            if i > 0:
                cv.line(xs[i - 1] + DEV_R, yc, xs[i] - DEV_R, yc, style)
            cv.device(rec, xs[i], yc)
            cv.text_c(xs[i], yc + DEV_R + DEV_LABEL_GAP + lh, rec["label"])
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


def clear_view(doc, view):
    # Коллектор с view.Id может вернуть не только содержимое вида, но и сам вид
    # (и другие виды). Это единственное место со строкой Delete в скрипте, поэтому
    # явно исключаем сам вид и любые View, удаляем только элементы, принадлежащие виду.
    view_id = view.Id
    ids = List[DB.ElementId]()
    for el in DB.FilteredElementCollector(doc, view_id).WhereElementIsNotElementType():
        if not el.IsValidObject or el.Id == view_id or isinstance(el, DB.View):
            continue
        if el.OwnerViewId == view_id:
            ids.Add(el.Id)
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
            sub = lines.SubCategories.get_Item(name)
        else:
            sub = doc.Settings.Categories.NewSubcategory(lines, name)
        sub.LineColor = DB.Color(*rgb)
        sub.SetLineWeight(weight, DB.GraphicsStyleType.Projection)
        if pat_key:
            sub.SetLinePatternId(patterns[pat_key], DB.GraphicsStyleType.Projection)
        styles[key] = sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection).Id
    return styles


XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
 xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" Title="Генератор Схем СС"
 Width="600" Height="400" MinWidth="600" MinHeight="400"
 WindowStartupLocation="CenterScreen" WindowStyle="None" AllowsTransparency="True"
 Background="Transparent" ResizeMode="CanResize" FontFamily="Times New Roman"
 FontSize="15" Foreground="#FF30251D">
 <Window.Resources>
  <LinearGradientBrush x:Key="Orange" StartPoint="0,0" EndPoint="1,1">
   <GradientStop Color="#FFFFA55A" Offset="0"/>
   <GradientStop Color="#FFE76D2E" Offset="1"/>
  </LinearGradientBrush>
  <Style x:Key="Dot" TargetType="Button">
   <Setter Property="Width" Value="14"/><Setter Property="Height" Value="14"/>
   <Setter Property="Margin" Value="0,0,9,0"/><Setter Property="Cursor" Value="Hand"/>
   <Setter Property="Template"><Setter.Value><ControlTemplate TargetType="Button">
    <Ellipse x:Name="circle" Fill="{TemplateBinding Background}" Stroke="#33000000" StrokeThickness="1"/>
    <ControlTemplate.Triggers><Trigger Property="IsMouseOver" Value="True">
     <Setter TargetName="circle" Property="Opacity" Value="0.65"/>
    </Trigger></ControlTemplate.Triggers>
   </ControlTemplate></Setter.Value></Setter>
  </Style>
  <Style x:Key="Action" TargetType="Button">
   <Setter Property="Foreground" Value="White"/><Setter Property="FontWeight" Value="Bold"/>
   <Setter Property="Cursor" Value="Hand"/>
   <Setter Property="Template"><Setter.Value><ControlTemplate TargetType="Button">
    <Border x:Name="surface" Background="{StaticResource Orange}" CornerRadius="15">
     <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
    </Border>
    <ControlTemplate.Triggers>
     <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="surface" Property="Opacity" Value="0.86"/></Trigger>
     <Trigger Property="IsEnabled" Value="False"><Setter TargetName="surface" Property="Opacity" Value="0.42"/></Trigger>
    </ControlTemplate.Triggers>
   </ControlTemplate></Setter.Value></Setter>
  </Style>
 </Window.Resources>
 <Grid Margin="16">
  <Border CornerRadius="26" BorderThickness="1" BorderBrush="#B3FFFFFF">
   <Border.Effect><DropShadowEffect BlurRadius="22" ShadowDepth="3" Opacity="0.24"/></Border.Effect>
   <Border.Background><LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
    <GradientStop Color="#F5FFF9F1" Offset="0"/><GradientStop Color="#F2F3E3D0" Offset="1"/>
   </LinearGradientBrush></Border.Background>
   <Grid>
    <Grid.RowDefinitions><RowDefinition Height="52"/><RowDefinition Height="*"/></Grid.RowDefinitions>
    <Grid x:Name="TitleBar" Grid.Row="0" Background="Transparent">
     <StackPanel Orientation="Horizontal" VerticalAlignment="Center" Margin="24,0,0,0">
      <Button x:Name="btnCloseDot" Style="{StaticResource Dot}" Background="#FFFF6058" ToolTip="Закрыть"/>
      <Button x:Name="btnMinDot" Style="{StaticResource Dot}" Background="#FFFFBD2F" ToolTip="Свернуть"/>
      <Button x:Name="btnMaxDot" Style="{StaticResource Dot}" Background="#FF29C940" ToolTip="Развернуть"/>
     </StackPanel>
     <TextBlock Text="Генерация схемы СС" HorizontalAlignment="Center" VerticalAlignment="Center"
      FontSize="17" FontWeight="Bold" IsHitTestVisible="False"/>
    </Grid>
    <Grid Grid.Row="1" Margin="30,10,30,24">
     <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
      <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/><RowDefinition Height="Auto"/>
     </Grid.RowDefinitions>
     <TextBlock Grid.Row="0" Text="1. Выберите систему" FontWeight="Bold" Margin="0,0,0,6"/>
     <ComboBox x:Name="cbSystems" Grid.Row="1" Height="32" Margin="0,0,0,18" VerticalContentAlignment="Center"/>
     <TextBlock Grid.Row="2" Text="2. Имя чертежного вида" FontWeight="Bold" Margin="0,0,0,6"/>
     <TextBox x:Name="txtViewName" Grid.Row="3" Text="Схема СС - Авто" Height="32"
      Padding="8,0" VerticalContentAlignment="Center"/>
     <TextBlock Grid.Row="4" Margin="0,14,0,0" TextWrapping="Wrap" Opacity="0.65" FontSize="13"
      Text="Схема строится по модели: этажи по отметкам, щиты, шлейфы и устройства в порядке удалённости от щита."/>
     <Button x:Name="btnGenerate" Grid.Row="5" Height="46" Content="Сгенерировать схему"
      Style="{StaticResource Action}" FontSize="16"/>
    </Grid>
   </Grid>
  </Border>
 </Grid>
</Window>"""

CONTROL_NAMES = ("TitleBar", "btnCloseDot", "btnMinDot", "btnMaxDot", "cbSystems",
                 "txtViewName", "btnGenerate")



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
                forms.alert(u"Ошибка ExternalEvent:\n{}".format(traceback.format_exc()), title=u"Ошибка")

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


def generate_scheme(uiapp, sys_name, view_name):
    uidoc = uiapp.ActiveUIDocument
    if uidoc is None or uidoc.Document is None:
        forms.alert(u"Нет открытого документа.", title=u"Внимание")
        return
    doc = uidoc.Document
    if doc.IsFamilyDocument:
        forms.alert(u"Откройте проект, а не документ семейства.", title=u"Внимание")
        return

    stage = u"Сбор данных"
    transaction = None
    try:
        records = collect_records(doc, sys_name)
        if not records:
            forms.alert(u"Элементы системы '{}' не найдены.".format(sys_name), title=u"Внимание")
            return
        stage = u"Проверка вида и типовых аннотаций"
        existing_id = find_view_id(doc, view_name)
        existing = doc.GetElement(existing_id) if valid_id(existing_id) else None
        if existing is not None and (not isinstance(existing, DB.ViewDrafting) or existing.IsTemplate):
            forms.alert(u"Имя занято другим видом: '{}'.".format(view_name), title=u"Ошибка")
            return
        view_type_id = drafting_type_id(doc)
        if existing is None and not valid_id(view_type_id):
            forms.alert(u"В проекте нет типа чертежного вида.", title=u"Ошибка")
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
        forms.alert(u"Этап: {}\n\n{}".format(stage, details), title=u"Ошибка генерации")
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
    report = u"Схема сгенерирована.\n\nЩитов: {}\nУГО на схеме (устройства и блоки): {}\nУникальных УГО: {}".format(
        len(real), cv.device_total, len(cv.placed_names))
    if virtual:
        report += (u"\n\nНет элемента-щита (CEC_Это_Щит = Да) для: " + u", ".join(virtual) +
                   u". Щит нарисован условно, порядок устройств взят по маркам.")
    if no_level:
        report += u"\n\nЭлементов без уровня: {}".format(no_level)
    if cv.missing_param:
        report += u"\n\nЭлементов без CEC_Имя_УГО (нарисованы квадратом): {}".format(cv.missing_param)
    if cv.missing_families:
        report += u"\n\nНе найдены типовые аннотации:\n- " + u"\n- ".join(
            sorted(cv.missing_families, key=natural_key))
    forms.alert(report, title=u"Отчет о генерации")


class SchemaApp(object):
    def __init__(self):
        self.window = XamlReader.Parse(XAML_UI)
        for name in CONTROL_NAMES:
            setattr(self, name, self.window.FindName(name))
        self.actions = RevitActions()
        self.TitleBar.MouseLeftButtonDown += self.on_drag
        self.btnCloseDot.Click += self.on_close
        self.btnMinDot.Click += self.on_minimize
        self.btnMaxDot.Click += self.on_maximize
        self.btnGenerate.Click += self.on_generate
        self.window.Closed += self.on_closed
        self.cbSystems.ItemsSource = [TXT_NO_PARAMS]
        self.cbSystems.SelectedIndex = 0

    def load_systems(self, uiapp):
        try:
            uidoc = uiapp.ActiveUIDocument
            systems = [] if (uidoc is None or uidoc.Document is None or
                             uidoc.Document.IsFamilyDocument) else available_systems(uidoc.Document)
            self.cbSystems.ItemsSource = systems if systems else [TXT_NO_PARAMS]
            self.cbSystems.SelectedIndex = 0
        except Exception:
            forms.alert(u"Не удалось прочитать системы:\n{}".format(traceback.format_exc()),
                        title=u"Ошибка")

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
        try:
            if System.AppDomain.CurrentDomain.GetData(APP_KEY) is self:
                System.AppDomain.CurrentDomain.SetData(APP_KEY, None)
        except Exception:
            pass

    def set_busy(self, busy):
        self.btnGenerate.IsEnabled = not busy
        self.btnGenerate.Content = u"Генерация..." if busy else u"Сгенерировать схему"

    def on_generate(self, sender, args):
        try:
            system = self.cbSystems.SelectedItem
            name = (self.txtViewName.Text or u"").strip()
            if not system or system == TXT_NO_PARAMS or not name:
                forms.alert(u"Выберите систему и укажите имя вида. Проверьте параметры CEC_.",
                            title=u"Внимание")
                return
            self.set_busy(True)

            def job(uiapp):
                try:
                    generate_scheme(uiapp, system, name)
                finally:
                    self.set_busy(False)

            self.actions.request(job)
        except Exception:
            self.set_busy(False)
            forms.alert(u"Ошибка интерфейса:\n{}".format(traceback.format_exc()), title=u"Ошибка")

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

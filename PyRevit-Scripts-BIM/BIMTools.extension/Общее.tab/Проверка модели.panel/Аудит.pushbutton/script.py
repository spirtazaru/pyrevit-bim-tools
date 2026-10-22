# -*- coding: utf-8 -*-
"""Комплексный аудит и очистка модели Revit.
Раздел 1 «Связи, оси и уровни» — проверка рабочих наборов связей, осей и уровней + автоисправление.
Раздел 2 «Аудит элементов» — 4 отдельные колонки с сортировкой, выпадающие мультифильтры с чекбоксами
         «Выбрать все», оранжевая кнопка «Обновить элементы» слева от галочки «Сканировать всю модель».
Раздел 3 «Создание РН» — создание рабочих наборов по CSV + кнопки «Скачать шаблон РН» и «Скачать список РН из проекта».
Раздел 4 «Закрепление» — контроль закрепления осей, уровней, связей и точек координат.
Раздел 5 «Очистка модели» — ленивая проверка, выпадающий мультифильтр с чекбоксом «Выбрать все» по ВСЕМ категориям
         (DWG, PDF/картинки, разрезы, группы/фильтры и каждая категория семейств), раскрытие по клику на шапку.
Раздел 6 «Здоровье модели» — раскрывающиеся карточки проблем (если штрафа нет — кнопки раскрытия нет, если есть —
         раскрывается список каждого предупреждения/элемента с чекбоксами для выбора и выделения в модели).
Оформление: кастомные скругленные оранжевые ползунки прокрутки (ScrollBar) вверх/вниз и вправо/влево в стиле кнопок.
"""
__title__ = u"Аудит\nмодели"
__author__ = u"Daniil Antonenko"
__persistentengine__ = True

import re
import clr
import os
import io
import sys

clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import System
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Windows import Visibility, WindowState, RoutedEventHandler
from System.Windows.Controls.Primitives import ButtonBase
from System.Windows.Markup import XamlReader
from System.Windows.Interop import WindowInteropHelper
from System.Diagnostics import Process
from System.Collections.Generic import List
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
from pyrevit import revit, DB, forms

doc = revit.doc
uidoc = revit.uidoc

if not doc.IsWorkshared:
    forms.alert(u"Модель не является совместной (рабочие наборы не включены).", exitscript=True)

# ==========================================================
# 1. КОНСТАНТЫ И БАЗОВЫЕ УТИЛИТЫ
# ==========================================================
GRIDS_LEVELS_WS_NAME = u"Общие уровни и сетки"
LINK_WS_MARKER = u"связь"
CUSTOM_RULES = {}
BAD_WS_CHARS = u'\\:{}[]|;<>?`~'

DEFAULT_CSV_TEMPLATE = u"""Рабочий набор;Ключевое слово связи
Общие уровни и сетки;
00_Связь_АР;АР
00_Связь_КЖ;КЖ
00_Связь_КМ;КМ
00_Связь_ОВ;ОВ
00_Связь_ВК;ВК
00_Связь_ЭОМ;ЭОМ
00_Связь_СС;СС
01_Несущие конструкции;
02_Архитектура;
03_Инженерные сети;
"""

BLACKLIST_BIC = set()
for _bic_name in (
    'OST_Materials', 'OST_ProjectInformation', 'OST_Cameras', 'OST_Viewers',
    'OST_SketchLines', 'OST_Lines', 'OST_WeakDims', 'OST_Constraints',
    'OST_Grids', 'OST_Levels', 'OST_RvtLinks', 'OST_ImportObjectStyles',
    'OST_ProjectBasePoint', 'OST_SharedBasePoint', 'OST_IOSModelGroups',
    'OST_Mass', 'OST_MassFloor', 'OST_MassForm', 'OST_Rooms', 'OST_Spaces',
    'OST_Areas', 'OST_ZoneEquipment', 'OST_PipingSystem', 'OST_DuctSystem',
    'OST_ElectricalCircuit', 'OST_SwitchSystem', 'OST_ShaftOpening',
    'OST_StructuralInternalLoads', 'OST_AnalyticalNodes', 'OST_AnalyticalMember',
    'OST_AnalyticalPanel'
):
    if hasattr(DB.BuiltInCategory, _bic_name):
        BLACKLIST_BIC.add(int(getattr(DB.BuiltInCategory, _bic_name)))


def id_int(eid):
    if eid is None:
        return -1
    try:
        return eid.IntegerValue
    except Exception:
        return eid.Value


def norm_name(text):
    text = (text or u"").replace(u"ё", u"е").replace(u"Ё", u"Е")
    return re.sub(r"\s+", u" ", text, flags=re.UNICODE).strip().lower()


def safe_name(element):
    if element is None:
        return u"<нет элемента>"
    try:
        val = DB.Element.Name.GetValue(element)
        if val:
            return val
    except Exception:
        pass
    try:
        if element.Name:
            return element.Name
    except Exception:
        pass
    return u"<без имени>"


def get_user_worksets(d):
    return list(DB.FilteredWorksetCollector(d).OfKind(DB.WorksetKind.UserWorkset))


def ws_info_maps(worksets):
    names_by_id = {}
    open_by_id = {}
    for w in worksets:
        wid = id_int(w.Id)
        names_by_id[wid] = w.Name
        open_by_id[wid] = w.IsOpen
    return names_by_id, open_by_id


def ws_name_of(d, ws_id, names_by_id):
    wid = id_int(ws_id)
    if wid in names_by_id:
        return names_by_id[wid]
    try:
        ws = d.GetWorksetTable().GetWorkset(ws_id)
        return ws.Name if ws else u"—"
    except Exception:
        return u"—"


def find_gl_workset(worksets):
    for ws in worksets:
        if ws.Name == GRIDS_LEVELS_WS_NAME:
            return ws
    target = norm_name(GRIDS_LEVELS_WS_NAME)
    for ws in worksets:
        if norm_name(ws.Name) == target:
            return ws
    for ws in worksets:
        n = norm_name(ws.Name)
        if u"уров" in n and (u"сетк" in n or re.search(u"(^|\\W)оси(\\W|$)", n, flags=re.UNICODE)):
            return ws
        if u"levels" in n and u"grids" in n:
            return ws
    return None


def read_text_file(path):
    with io.open(path, 'rb') as f:
        raw = f.read()
    for enc in ('utf-8-sig', 'cp1251'):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode('utf-8', 'ignore')


def write_utf8_csv(path, text):
    with io.open(path, 'w', encoding='utf-8-sig') as f:
        f.write(text)


def parse_rules_csv(text):
    delim = u';' if u';' in text else (u'\t' if u'\t' in text else u',')
    rows = []
    for line in text.splitlines():
        parts = [p.strip().strip(u'"').strip() for p in line.split(delim)]
        if not parts or not parts[0]:
            continue
        if norm_name(parts[0]) in (u"рабочий набор", u"рн", u"workset", u"имя рн", u"имя рабочего набора"):
            continue
        rows.append((parts[0], parts[1] if len(parts) > 1 else u""))
    return rows


def extract_keyword(ws_name, fallback=False):
    if ws_name in CUSTOM_RULES and CUSTOM_RULES[ws_name]:
        return CUSTOM_RULES[ws_name].upper()
    cleaned = re.sub(u"связь", u"", ws_name, flags=re.IGNORECASE | re.UNICODE)
    cleaned = cleaned.replace(u"!", u"").strip()
    cleaned = re.sub(r'^[^a-zA-Zа-яА-ЯёЁ0-9]+', u'', cleaned, flags=re.UNICODE)
    cleaned = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9]+$', u'', cleaned, flags=re.UNICODE)
    if cleaned:
        return cleaned.upper()
    return ws_name.upper() if fallback else None


def keyword_found_in_name(keyword, name_upper):
    start = 0
    while True:
        idx = name_upper.find(keyword, start)
        if idx == -1:
            return False
        before_ok = (idx == 0) or (not name_upper[idx - 1].isalnum())
        after_pos = idx + len(keyword)
        after_ok = (after_pos == len(name_upper)) or (not name_upper[after_pos].isalnum())
        if before_ok and after_ok:
            return True
        start = idx + 1


def build_link_rules(worksets):
    rules = {}
    for ws in worksets:
        if LINK_WS_MARKER in ws.Name.lower() or ws.Name in CUSTOM_RULES:
            kw = extract_keyword(ws.Name)
            if kw:
                rules[kw] = ws
    return [(kw, rules[kw]) for kw in sorted(rules.keys(), key=len, reverse=True)]


def get_link_name(d, link_instance):
    type_id = link_instance.GetTypeId()
    link_type = d.GetElement(type_id) if type_id and type_id != DB.ElementId.InvalidElementId else None
    if link_type is not None:
        n = safe_name(link_type)
        if n and n != u"<без имени>":
            return n
    return safe_name(link_instance)


def ensure_active_3d_view(uidoc_instance, doc_instance):
    if isinstance(uidoc_instance.ActiveView, DB.View3D) and not uidoc_instance.ActiveView.IsTemplate:
        return uidoc_instance.ActiveView
    views_3d = [
        v for v in DB.FilteredElementCollector(doc_instance).OfClass(DB.View3D).ToElements()
        if not v.IsTemplate
    ]
    if views_3d:
        target_view = views_3d[0]
        for v in views_3d:
            if u"3D" in v.Name or u"3d" in v.Name:
                target_view = v
                break
        try:
            uidoc_instance.ActiveView = target_view
            return target_view
        except Exception:
            return target_view
    return None


def set_element_workset(element, ws_int):
    try:
        param = element.get_Parameter(DB.BuiltInParameter.ELEM_PARTITION_PARAM)
        if param is None or param.IsReadOnly:
            return False
        return bool(param.Set(ws_int))
    except Exception:
        return False


# ==========================================================
# 2. МОДЕЛИ ДАННЫХ ДЛЯ ТАБЛИЦ, ФИЛЬТРОВ, ОЧИСТКИ И ЗДОРОВЬЯ
# ==========================================================
class NotifyBase(INotifyPropertyChanged):
    def __init__(self):
        self._handlers = []

    def add_PropertyChanged(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)

    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)

    def raise_changed(self, prop_name):
        args = PropertyChangedEventArgs(prop_name)
        for h in list(self._handlers):
            try:
                h(self, args)
            except Exception:
                pass


class LinkRow(object):
    def __init__(self, el_id, name, subtext, current_ws, target_ws, target_ws_id, kind, status):
        self.RevitId = el_id
        self.LinkName = name
        self.SubText = subtext
        self.CurrentWS = current_ws
        self.TargetWS = target_ws
        self.TargetWsId = target_ws_id
        self.Kind = kind
        self.Status = status


class ElementAuditRow(NotifyBase):
    def __init__(self, el_id, category, fam_type, workset, is_ws_open):
        NotifyBase.__init__(self)
        self.RevitId = el_id
        self.Category = category
        self.FamilyType = fam_type
        self.Workset = workset
        self.IsWsOpen = is_ws_open
        self.WorksetBadge = workset if is_ws_open else u"🔒 " + workset
        self.WsStateKind = 'open' if is_ws_open else 'closed'
        self._is_checked = False

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        if self._is_checked != bool(value):
            self._is_checked = bool(value)
            self.raise_changed('IsChecked')


class FilterItem(NotifyBase):
    def __init__(self, name, is_checked=True, badge=u"", key=None):
        NotifyBase.__init__(self)
        self._name = name
        self._key = key if key is not None else name
        self._badge = badge
        self._is_checked = bool(is_checked)

    @property
    def Name(self):
        return self._name

    @property
    def Key(self):
        return self._key

    @property
    def DisplayText(self):
        return u"{}  {}".format(self._name, self._badge).strip() if self._badge else self._name

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        val = bool(value)
        if self._is_checked != val:
            self._is_checked = val
            self.raise_changed('IsChecked')

    def set_silent(self, value):
        val = bool(value)
        if self._is_checked != val:
            self._is_checked = val
            self.raise_changed('IsChecked')


class PinRow(NotifyBase):
    def __init__(self, category, name, element, workset_name):
        NotifyBase.__init__(self)
        self.Category = category
        self.Name = name
        self.Workset = workset_name
        self.Element = element
        self.RevitId = element.Id
        self.IsPinned = bool(element.Pinned)
        self.Status = u"📌 Закреплено" if self.IsPinned else u"⚠ Не закреплено"
        self.Kind = 'ok' if self.IsPinned else 'warn'
        self._is_checked = False

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        if self._is_checked != bool(value):
            self._is_checked = bool(value)
            self.raise_changed('IsChecked')


class PurgeItemRow(NotifyBase):
    """Отдельный элемент внутри раздела «Очистка модели»."""
    def __init__(self, el_id, group_key, category_label, name, info_text, status_text, kind, default_checked=False, can_show=True):
        NotifyBase.__init__(self)
        self.RevitId = el_id
        self.IdText = u"ID: {}".format(id_int(el_id))
        self.GroupKey = group_key
        self.CategoryLabel = category_label
        self.Name = name
        self.InfoText = info_text
        self.Status = status_text
        self.Kind = kind  # 'err', 'warn', 'ok'
        self.CanShow = can_show
        self._is_checked = bool(default_checked)
        self.ParentSubGroup = None
        self.ParentGroup = None

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        val = bool(value)
        if self._is_checked != val:
            self._is_checked = val
            self.raise_changed('IsChecked')
            if self.ParentSubGroup is not None:
                self.ParentSubGroup.on_child_checked_changed()
            elif self.ParentGroup is not None:
                self.ParentGroup.on_child_checked_changed()

    def set_checked_silent(self, value):
        val = bool(value)
        if self._is_checked != val:
            self._is_checked = val
            self.raise_changed('IsChecked')


class PurgeSubCategoryGroup(NotifyBase):
    """Раскрывающаяся подгруппа категории внутри раздела «4. Неиспользуемые загружаемые семейства»."""
    def __init__(self, category_name, items_list, parent_group, is_expanded=False):
        NotifyBase.__init__(self)
        self.CategoryName = category_name
        self.ParentGroup = parent_group
        self.Items = ObservableCollection[object]()
        self._all_items = list(items_list)
        for it in self._all_items:
            it.ParentSubGroup = self
            it.ParentGroup = parent_group
        self._is_expanded = bool(is_expanded)
        self._is_checked = all(it.IsChecked for it in self._all_items) if self._all_items else False
        self._updating = False

    def apply_filter(self, q, only_unused):
        self.Items.Clear()
        for it in self._all_items:
            if only_unused and it.Kind == 'ok':
                continue
            if q:
                haystack = u"{} {} {} {} {}".format(self.CategoryName, it.Name, it.InfoText, it.Status, it.IdText).lower()
                if q not in haystack:
                    continue
            self.Items.Add(it)
        self.sync_check_state()

    @property
    def VisibleCount(self):
        return self.Items.Count

    @property
    def CountBadge(self):
        checked = sum(1 for it in self._all_items if it.IsChecked)
        total = len(self._all_items)
        if checked > 0:
            return u"Выбрано: {} из {}".format(checked, total)
        return u"{} шт.".format(total)

    @property
    def IsExpanded(self):
        return self._is_expanded

    @IsExpanded.setter
    def IsExpanded(self, value):
        val = bool(value)
        if self._is_expanded != val:
            self._is_expanded = val
            self.raise_changed('IsExpanded')

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        if self._updating:
            return
        val = bool(value)
        self._is_checked = val
        self._updating = True
        try:
            for it in self.Items:
                it.set_checked_silent(val)
        finally:
            self._updating = False
        self.raise_changed('IsChecked')
        self.raise_changed('CountBadge')
        if self.ParentGroup is not None:
            self.ParentGroup.on_child_checked_changed()

    def sync_check_state(self):
        vis = list(self.Items)
        self._is_checked = all(it.IsChecked for it in vis) if vis else False
        self.raise_changed('IsChecked')
        self.raise_changed('CountBadge')

    def on_child_checked_changed(self):
        if self._updating:
            return
        self._updating = True
        try:
            self.sync_check_state()
        finally:
            self._updating = False
        if self.ParentGroup is not None:
            self.ParentGroup.on_child_checked_changed()


class PurgeCategoryGroup(NotifyBase):
    """Основная раскрывающаяся карточка в разделе «Очистка модели»."""
    def __init__(self, key, title, subtitle, is_expanded=False, use_subgroups=False):
        NotifyBase.__init__(self)
        self.Key = key
        self.Title = title
        self.Subtitle = subtitle
        self.UseSubGroups = bool(use_subgroups)
        self.FlatVisibility = Visibility.Collapsed if self.UseSubGroups else Visibility.Visible
        self.SubGroupsVisibility = Visibility.Visible if self.UseSubGroups else Visibility.Collapsed

        self.Items = ObservableCollection[object]()
        self.SubGroups = ObservableCollection[object]()
        self._all_items = []
        self._all_subgroups = []
        self._is_expanded = bool(is_expanded)
        self._is_checked = False
        self._updating = False
        self.OnGroupSelectionChanged = None

    def set_items(self, items_list):
        self._all_items = list(items_list)
        for it in self._all_items:
            it.ParentGroup = self

        if self.UseSubGroups:
            by_cat = {}
            for it in self._all_items:
                by_cat.setdefault(it.CategoryLabel, []).append(it)
            self._all_subgroups = []
            for cname in sorted(by_cat.keys(), key=lambda x: x.lower()):
                sg = PurgeSubCategoryGroup(cname, by_cat[cname], self, is_expanded=False)
                self._all_subgroups.append(sg)
        self.apply_filter(u"", False, None)

    def apply_filter(self, search_query, only_unused, allowed_categories=None):
        q = (search_query or u"").strip().lower()
        if self.UseSubGroups:
            self.SubGroups.Clear()
            for sg in self._all_subgroups:
                if allowed_categories is not None and sg.CategoryName not in allowed_categories:
                    continue
                sg.apply_filter(q, only_unused)
                if sg.VisibleCount > 0:
                    self.SubGroups.Add(sg)
        else:
            self.Items.Clear()
            for it in self._all_items:
                if allowed_categories is not None and it.CategoryLabel not in allowed_categories:
                    continue
                if only_unused and it.Kind == 'ok':
                    continue
                if q:
                    haystack = u"{} {} {} {} {}".format(it.CategoryLabel, it.Name, it.InfoText, it.Status, it.IdText).lower()
                    if q not in haystack:
                        continue
                self.Items.Add(it)

        self.on_child_checked_changed()
        self.raise_changed('CountBadge')
        self.raise_changed('UnusedBadge')

    def get_visible_leaf_items(self):
        if self.UseSubGroups:
            res = []
            for sg in self.SubGroups:
                for it in sg.Items:
                    res.append(it)
            return res
        return list(self.Items)

    @property
    def TotalCount(self):
        return len(self._all_items)

    @property
    def VisibleCount(self):
        return len(self.get_visible_leaf_items())

    @property
    def CheckedCount(self):
        return sum(1 for it in self._all_items if it.IsChecked)

    @property
    def CountBadge(self):
        checked = self.CheckedCount
        total = len(self._all_items)
        if checked > 0:
            return u"Выбрано: {} из {}".format(checked, total)
        return u"Всего: {} шт.".format(total)

    @property
    def UnusedBadge(self):
        if self.Key == 'pdf_img':
            unplaced = sum(1 for it in self._all_items if it.Kind == 'err')
            return u"Не размещено: {} шт.".format(unplaced)
        elif self.Key == 'dwg':
            return u"Проверьте нужные перед удалением"
        elif self.Key == 'sections':
            return u"Не на листах: {} шт.".format(len(self._all_items))
        elif self.Key == 'families':
            return u"Категорий: {} • Семейств: {}".format(len(self.SubGroups), self.VisibleCount)
        else:
            return u"Не используется: {} шт.".format(len(self._all_items))

    @property
    def IsExpanded(self):
        return self._is_expanded

    @IsExpanded.setter
    def IsExpanded(self, value):
        val = bool(value)
        if self._is_expanded != val:
            self._is_expanded = val
            self.raise_changed('IsExpanded')

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        if self._updating:
            return
        val = bool(value)
        self._is_checked = val
        self._updating = True
        try:
            if self.UseSubGroups:
                for sg in self.SubGroups:
                    for it in sg.Items:
                        it.set_checked_silent(val)
                    sg.sync_check_state()
            else:
                for it in self.Items:
                    it.set_checked_silent(val)
        finally:
            self._updating = False
        self.raise_changed('IsChecked')
        self.raise_changed('CountBadge')
        if self.OnGroupSelectionChanged:
            self.OnGroupSelectionChanged()

    def on_child_checked_changed(self):
        if self._updating:
            return
        self._updating = True
        try:
            vis = self.get_visible_leaf_items()
            self._is_checked = all(it.IsChecked for it in vis) if vis else False
            if self.UseSubGroups:
                for sg in self.SubGroups:
                    sg.sync_check_state()
        finally:
            self._updating = False
        self.raise_changed('IsChecked')
        self.raise_changed('CountBadge')
        if self.OnGroupSelectionChanged:
            self.OnGroupSelectionChanged()


class HealthIssueLeafRow(NotifyBase):
    """Отдельная проблема/предупреждение внутри раскрывающейся карточки раздела «Здоровье модели»."""
    def __init__(self, name, info_text, status_text, kind, element_ids):
        NotifyBase.__init__(self)
        self.Name = name
        self.InfoText = info_text
        self.Status = status_text
        self.Kind = kind
        self.ElementIds = list(element_ids or [])
        if len(self.ElementIds) == 1:
            self.IdText = u"ID: {}".format(id_int(self.ElementIds[0]))
        elif len(self.ElementIds) > 1:
            self.IdText = u"Элем.: {} шт.".format(len(self.ElementIds))
        else:
            self.IdText = u"—"
        self.CanShow = len(self.ElementIds) > 0
        self._is_checked = False
        self.ParentGroup = None

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        val = bool(value)
        if self._is_checked != val:
            self._is_checked = val
            self.raise_changed('IsChecked')
            if self.ParentGroup is not None:
                self.ParentGroup.on_child_checked_changed()

    def set_checked_silent(self, value):
        val = bool(value)
        if self._is_checked != val:
            self._is_checked = val
            self.raise_changed('IsChecked')


class HealthGroupCard(NotifyBase):
    """Раскрывающаяся карточка показателя здоровья модели.
    Если Count == 0 (все отлично), то чекбокс и кнопка раскрытия скрыты (HasIssuesVisibility = Collapsed).
    Если Count > 0 (есть штрафы), то карточку можно раскрыть и выбрать конкретные предупреждения/элементы.
    """
    def __init__(self, code, title, desc, count, penalty_text, kind, leaf_items):
        NotifyBase.__init__(self)
        self.Code = code
        self.Title = title
        self.Description = desc
        self.Count = count
        self.PenaltyText = penalty_text
        self.Kind = kind
        if kind == 'ok':
            self.Status = u"Отлично (0 штрафа)"
        elif kind == 'warn':
            self.Status = u"Внимание ({})".format(penalty_text)
        else:
            self.Status = u"Критично ({})".format(penalty_text)

        self.Items = ObservableCollection[object]()
        self._all_items = list(leaf_items or [])
        for it in self._all_items:
            it.ParentGroup = self
            self.Items.Add(it)

        self.HasIssues = len(self._all_items) > 0
        self.HasIssuesVisibility = Visibility.Visible if self.HasIssues else Visibility.Collapsed
        self._is_expanded = False
        self._is_checked = False
        self._updating = False
        self.OnSelectionChanged = None

    @property
    def CheckedCount(self):
        return sum(1 for it in self._all_items if it.IsChecked)

    @property
    def CountBadge(self):
        if not self.HasIssues:
            return u"Проблем: 0"
        chk = self.CheckedCount
        if chk > 0:
            return u"Выбрано: {} из {}".format(chk, self.Count)
        return u"Найдено: {} шт.".format(self.Count)

    @property
    def BodyVisibility(self):
        return Visibility.Visible if (self.HasIssues and self._is_expanded) else Visibility.Collapsed

    @property
    def IsExpanded(self):
        return self._is_expanded

    @IsExpanded.setter
    def IsExpanded(self, value):
        if not self.HasIssues:
            return
        val = bool(value)
        if self._is_expanded != val:
            self._is_expanded = val
            self.raise_changed('IsExpanded')
            self.raise_changed('BodyVisibility')

    @property
    def IsChecked(self):
        return self._is_checked

    @IsChecked.setter
    def IsChecked(self, value):
        if self._updating or not self.HasIssues:
            return
        val = bool(value)
        self._is_checked = val
        self._updating = True
        try:
            for it in self._all_items:
                it.set_checked_silent(val)
        finally:
            self._updating = False
        self.raise_changed('IsChecked')
        self.raise_changed('CountBadge')
        if self.OnSelectionChanged:
            self.OnSelectionChanged()

    def on_child_checked_changed(self):
        if self._updating:
            return
        self._updating = True
        try:
            self._is_checked = all(it.IsChecked for it in self._all_items) if self._all_items else False
        finally:
            self._updating = False
        self.raise_changed('IsChecked')
        self.raise_changed('CountBadge')
        if self.OnSelectionChanged:
            self.OnSelectionChanged()


class WorksetOption(object):
    def __init__(self, ws):
        self.Name = ws.Name
        self.IdInt = id_int(ws.Id)

    def ToString(self):
        return self.Name

    def __str__(self):
        return self.Name


# ==========================================================
# 3. ЛОГИКА СКАНИРОВАНИЯ, ОЧИСТКИ И ЗДОРОВЬЯ МОДЕЛИ
# ==========================================================
class LinksState(object):
    def __init__(self):
        self.rows = []
        self.gl_ws = None
        self.err_count = 0
        self.total_checked = 0


def scan_links(d, worksets):
    st = LinksState()
    names_by_id, _ = ws_info_maps(worksets)
    rules = build_link_rules(worksets)

    links = DB.FilteredElementCollector(d).OfClass(DB.RevitLinkInstance).ToElements()
    st.total_checked += len(links)
    for link in links:
        name = get_link_name(d, link)
        cur_name = ws_name_of(d, link.WorksetId, names_by_id)
        target = None
        upper = name.upper()
        for kw, ws in rules:
            if keyword_found_in_name(kw, upper):
                target = ws
                break
        if target is None:
            st.rows.append(LinkRow(link.Id, name, u"Связь RVT", cur_name, u"Правило не найдено", None, 'warn', u"Нет правила"))
        elif id_int(link.WorksetId) == id_int(target.Id):
            st.rows.append(LinkRow(link.Id, name, u"Связь RVT", cur_name, target.Name, target.Id, 'ok', u"В своём РН"))
        else:
            st.rows.append(LinkRow(link.Id, name, u"Связь RVT", cur_name, target.Name, target.Id, 'err', u"Ошибка РН"))
            st.err_count += 1

    st.gl_ws = find_gl_workset(worksets)
    if st.gl_ws is None:
        st.rows.append(LinkRow(None, u"Оси и уровни", u"Системная проверка", u"—",
                               u"Создайте РН «{}»".format(GRIDS_LEVELS_WS_NAME), None, 'warn', u"РН не найден"))
    else:
        gl_id = id_int(st.gl_ws.Id)
        for single, plural, cat in (
            (u"Ось", u"Координационные оси", DB.BuiltInCategory.OST_Grids),
            (u"Уровень", u"Уровни модели", DB.BuiltInCategory.OST_Levels)
        ):
            items = DB.FilteredElementCollector(d).OfCategory(cat).WhereElementIsNotElementType().ToElements()
            st.total_checked += len(items)
            wrong = [e for e in items if id_int(e.WorksetId) != gl_id]
            if not wrong:
                st.rows.append(LinkRow(None, u"{} ({} шт.)".format(plural, len(items)), u"Сводка по категории",
                                       st.gl_ws.Name, st.gl_ws.Name, st.gl_ws.Id, 'ok', u"Все в своём РН"))
            else:
                for e in wrong:
                    st.rows.append(LinkRow(e.Id, u"{}: {}".format(single, safe_name(e)), plural,
                                           ws_name_of(d, e.WorksetId, names_by_id), st.gl_ws.Name,
                                           st.gl_ws.Id, 'err', u"Ошибка РН"))
                    st.err_count += 1

    st.rows.sort(key=lambda r: (r.Kind != 'err', r.Kind != 'warn', r.LinkName))
    return st


def fix_links_and_levels(d, st):
    fixed, failed = 0, 0
    t = DB.Transaction(d, u"Авто-перенос связей, осей и уровней")
    t.Start()
    try:
        for row in st.rows:
            if row.Kind == 'err' and row.RevitId is not None and row.TargetWsId is not None:
                el = d.GetElement(row.RevitId)
                if el is not None and set_element_workset(el, id_int(row.TargetWsId)):
                    fixed += 1
                else:
                    failed += 1
        t.Commit()
        return fixed, failed
    except Exception:
        t.RollBack()
        raise


def is_valid_model_element(el):
    if el is None or el.ViewSpecific:
        return False
    cat = el.Category
    if cat is None:
        return False
    try:
        if cat.CategoryType != DB.CategoryType.Model:
            return False
    except Exception:
        return False

    cid = id_int(cat.Id)
    if cid in BLACKLIST_BIC:
        return False

    ws_id = el.WorksetId
    if ws_id is None or ws_id == DB.WorksetId.InvalidWorksetId:
        return False

    if isinstance(el, (DB.View, DB.ElementType, DB.Sketch, DB.SketchPlane, DB.CurveElement)):
        return False

    return True


def collect_audit_elements(d, uidoc_inst, whole_model, names_by_id, open_by_id):
    if whole_model:
        collector = DB.FilteredElementCollector(d).WhereElementIsNotElementType().WhereElementIsViewIndependent()
    else:
        v3d = ensure_active_3d_view(uidoc_inst, d)
        if v3d is not None:
            collector = DB.FilteredElementCollector(d, v3d.Id).WhereElementIsNotElementType().WhereElementIsViewIndependent()
        else:
            collector = DB.FilteredElementCollector(d).WhereElementIsNotElementType().WhereElementIsViewIndependent()

    rows = []
    for el in collector:
        if not is_valid_model_element(el):
            continue
        wid = id_int(el.WorksetId)
        if wid not in names_by_id:
            continue

        ws_name = names_by_id[wid]
        is_open = open_by_id.get(wid, True)
        cat_name = el.Category.Name or u"—"

        fam_type_name = u"Системный элемент"
        try:
            type_id = el.GetTypeId()
            el_type = d.GetElement(type_id) if type_id and type_id != DB.ElementId.InvalidElementId else None
            names = []
            if el_type:
                if hasattr(el_type, 'FamilyName') and el_type.FamilyName:
                    names.append(el_type.FamilyName)
                t_name = safe_name(el_type)
                if t_name and t_name != u"<без имени>":
                    names.append(t_name)
            if not names:
                e_name = safe_name(el)
                if e_name:
                    names.append(e_name)
            if names:
                fam_type_name = u" / ".join(names)
        except Exception:
            pass

        rows.append(ElementAuditRow(el.Id, cat_name, fam_type_name, ws_name, is_open))

    rows.sort(key=lambda x: (x.IsWsOpen, x.Category, x.FamilyType, x.Workset))
    return rows


def collect_pin_rows(d, names_by_id):
    rows = []
    categories = [
        (u"Ось", DB.BuiltInCategory.OST_Grids),
        (u"Уровень", DB.BuiltInCategory.OST_Levels),
        (u"Базовая точка", DB.BuiltInCategory.OST_ProjectBasePoint),
        (u"Точка съемки", DB.BuiltInCategory.OST_SharedBasePoint),
    ]
    for label, bic in categories:
        for el in DB.FilteredElementCollector(d).OfCategory(bic).WhereElementIsNotElementType().ToElements():
            rows.append(PinRow(label, safe_name(el), el, ws_name_of(d, el.WorksetId, names_by_id)))

    for link in DB.FilteredElementCollector(d).OfClass(DB.RevitLinkInstance).ToElements():
        rows.append(PinRow(u"Связь RVT", get_link_name(d, link), link, ws_name_of(d, link.WorksetId, names_by_id)))
    return rows


def collect_purge_groups(d, names_by_id):
    """Ленивый сбор данных для раздела «Очистка модели» (вызывается ТОЛЬКО при переходе на вкладку)."""
    # 1. ИМПОРТЫ DWG (САПР)
    dwg_items = []
    for imp in DB.FilteredElementCollector(d).OfClass(DB.ImportInstance).ToElements():
        if imp.IsLinked:
            continue
        imp_name = safe_name(imp)
        try:
            t_el = d.GetElement(imp.GetTypeId())
            if t_el:
                imp_name = safe_name(t_el)
        except Exception:
            pass

        if imp.ViewSpecific and imp.OwnerViewId != DB.ElementId.InvalidElementId:
            v_el = d.GetElement(imp.OwnerViewId)
            loc_info = u"Вид (2D): {}".format(safe_name(v_el) if v_el else u"—")
        else:
            loc_info = u"Модель (3D) • РН: {}".format(ws_name_of(d, imp.WorksetId, names_by_id))

        dwg_items.append(PurgeItemRow(
            imp.Id, 'dwg', u"Импорты САПР (DWG)", imp_name, loc_info,
            u"Внедренный DWG", 'warn', default_checked=False, can_show=True
        ))
    dwg_items.sort(key=lambda x: (x.Name.lower(), x.InfoText.lower()))

    # 2. ИЗОБРАЖЕНИЯ И PDF
    pdf_img_items = []
    img_views_by_type = {}
    for inst in DB.FilteredElementCollector(d).OfClass(DB.ImageInstance).ToElements():
        tid = id_int(inst.GetTypeId())
        if tid not in img_views_by_type:
            img_views_by_type[tid] = []
        v_name = u"Вид"
        try:
            if inst.OwnerViewId and inst.OwnerViewId != DB.ElementId.InvalidElementId:
                v_el = d.GetElement(inst.OwnerViewId)
                if v_el:
                    v_name = safe_name(v_el)
        except Exception:
            pass
        img_views_by_type[tid].append(v_name)

    for img_type in DB.FilteredElementCollector(d).OfClass(DB.ImageType).ToElements():
        tid = id_int(img_type.Id)
        views_list = img_views_by_type.get(tid, [])
        cnt = len(views_list)
        t_name = safe_name(img_type)
        is_pdf = t_name.lower().endswith(u".pdf")
        cat_label = u"Изображения и PDF"

        if cnt == 0:
            pdf_img_items.append(PurgeItemRow(
                img_type.Id, 'pdf_img', cat_label, t_name,
                u"Тип: {} • Нигде не размещено в проекте (0 экз. на видах и листах)".format(u"PDF" if is_pdf else u"Картинка"),
                u"Не размещено (0)", 'err', default_checked=True, can_show=False
            ))
        else:
            sample_views = u", ".join(views_list[:2])
            if cnt > 2:
                sample_views += u" и ещё {}".format(cnt - 2)
            pdf_img_items.append(PurgeItemRow(
                img_type.Id, 'pdf_img', cat_label, t_name,
                u"Тип: {} • Размещено ({} шт.): {}".format(u"PDF" if is_pdf else u"Картинка", cnt, sample_views),
                u"Размещено ({})".format(cnt), 'ok', default_checked=False, can_show=False
            ))
    pdf_img_items.sort(key=lambda x: (x.Kind != 'err', x.Name.lower()))

    # 3. РАЗРЕЗЫ НЕ НА ЛИСТАХ (только ViewType.Section)
    section_items = []
    placed_view_ids = set()
    for vp in DB.FilteredElementCollector(d).OfClass(DB.Viewport).ToElements():
        placed_view_ids.add(id_int(vp.ViewId))

    for v in DB.FilteredElementCollector(d).OfClass(DB.ViewSection).ToElements():
        if v.IsTemplate:
            continue
        if v.ViewType == DB.ViewType.Section and id_int(v.Id) not in placed_view_ids:
            section_items.append(PurgeItemRow(
                v.Id, 'sections', u"Разрезы (не на листах)", safe_name(v),
                u"Разрез нигде не размещен (нет ни на одном листе)",
                u"Не на листе", 'warn', default_checked=False, can_show=True
            ))
    section_items.sort(key=lambda x: x.Name.lower())

    # 4. НЕИСПОЛЬЗУЕМЫЕ ЗАГРУЖАЕМЫЕ СЕМЕЙСТВА
    family_items = []
    used_family_ids = set()
    for fi in DB.FilteredElementCollector(d).OfClass(DB.FamilyInstance).WhereElementIsNotElementType().ToElements():
        try:
            if fi.Symbol and fi.Symbol.Family:
                used_family_ids.add(id_int(fi.Symbol.Family.Id))
        except Exception:
            pass

    for fam in DB.FilteredElementCollector(d).OfClass(DB.Family).ToElements():
        try:
            if fam.IsInPlace or not fam.IsEditable:
                continue
            fid = id_int(fam.Id)
            if fid not in used_family_ids:
                has_any_inst = False
                sym_ids = list(fam.GetFamilySymbolIds())
                for sym_id in sym_ids:
                    if DB.FilteredElementCollector(d).WherePasses(DB.FamilyInstanceFilter(d, sym_id)).GetElementCount() > 0:
                        has_any_inst = True
                        break
                if has_any_inst:
                    continue
                cat_n = fam.FamilyCategory.Name if fam.FamilyCategory else u"Прочие семейства"
                family_items.append(PurgeItemRow(
                    fam.Id, 'families', u"Семейства: {}".format(cat_n), safe_name(fam),
                    u"Категория: {} • Типоразмеров: {} • Экземпляров в модели: 0".format(cat_n, len(sym_ids)),
                    u"Не используется", 'warn', default_checked=True, can_show=False
                ))
        except Exception:
            pass
    family_items.sort(key=lambda x: (x.CategoryLabel.lower(), x.Name.lower()))

    # 5. НЕИСПОЛЬЗУЕМЫЕ ГРУППЫ И ФИЛЬТРЫ ВИДОВ
    groups_filters_items = []
    for gt in DB.FilteredElementCollector(d).OfClass(DB.GroupType).ToElements():
        try:
            if gt.Groups.Size == 0:
                cat_n = gt.Category.Name if gt.Category else u"Группы"
                groups_filters_items.append(PurgeItemRow(
                    gt.Id, 'groups', u"Группы и фильтры видов", safe_name(gt),
                    u"{} (0 размещенных экземпляров в модели)".format(cat_n),
                    u"Пустая группа", 'warn', default_checked=True, can_show=False
                ))
        except Exception:
            pass

    used_filter_ids = set()
    for v in DB.FilteredElementCollector(d).OfClass(DB.View).ToElements():
        try:
            for fid in v.GetFilters():
                used_filter_ids.add(id_int(fid))
        except Exception:
            pass

    for pf in DB.FilteredElementCollector(d).OfClass(DB.ParameterFilterElement).ToElements():
        if id_int(pf.Id) not in used_filter_ids:
            groups_filters_items.append(PurgeItemRow(
                pf.Id, 'groups', u"Группы и фильтры видов", safe_name(pf),
                u"Фильтр вида не применен ни к одному виду или шаблону видов",
                u"Не используется", 'warn', default_checked=True, can_show=False
            ))
    groups_filters_items.sort(key=lambda x: (x.Status, x.Name.lower()))

    groups = []

    g_dwg = PurgeCategoryGroup(
        'dwg',
        u"1. Импорты САПР (DWG)",
        u"Кликните по шапке, чтобы раскрыть список всех DWG, показать нужный в модели (👁) и отметить лишние",
        is_expanded=(len(dwg_items) > 0 and len(dwg_items) <= 25),
        use_subgroups=False
    )
    g_dwg.set_items(dwg_items)
    groups.append(g_dwg)

    g_img = PurgeCategoryGroup(
        'pdf_img',
        u"2. Изображения и PDF-файлы",
        u"Показаны все загруженные картинки и PDF — неразмещенные файлы выделены красным вверху списка",
        is_expanded=True,
        use_subgroups=False
    )
    g_img.set_items(pdf_img_items)
    groups.append(g_img)

    g_sec = PurgeCategoryGroup(
        'sections',
        u"3. Разрезы (не размещенные ни на одном листе)",
        u"Только разрезы, которые нигде не размещены на листах (планы, фасады и виды на листах не затрагиваются)",
        is_expanded=False,
        use_subgroups=False
    )
    g_sec.set_items(section_items)
    groups.append(g_sec)

    g_fam = PurgeCategoryGroup(
        'families',
        u"4. Неиспользуемые загружаемые семейства (по категориям)",
        u"Дерево категорий семейств: раскройте нужную категорию или используйте фильтр категорий сверху",
        is_expanded=True,
        use_subgroups=True
    )
    g_fam.set_items(family_items)
    groups.append(g_fam)

    g_grp = PurgeCategoryGroup(
        'groups',
        u"5. Неиспользуемые группы и фильтры видов",
        u"Типы групп с 0 вхождений и фильтры, не назначенные ни одному виду или шаблону",
        is_expanded=False,
        use_subgroups=False
    )
    g_grp.set_items(groups_filters_items)
    groups.append(g_grp)

    return groups


def evaluate_model_health(d, link_state, pin_rows, names_by_id):
    """Единая взвешенная оценка здоровья модели (0..100%) с формированием раскрывающихся списков проблем."""
    cards = []

    # 1. ПРЕДУПРЕЖДЕНИЯ REVIT (WARNINGS) — по каждому предупреждению отдельно
    warnings_count = 0
    warning_leafs = []
    try:
        all_warnings = list(d.GetWarnings())
        warnings_count = len(all_warnings)
        for idx, w in enumerate(all_warnings[:250]):
            desc_text = (w.GetDescriptionText() or u"Предупреждение Revit").strip()
            desc_one_line = re.sub(r"\s+", u" ", desc_text)
            f_ids = list(w.GetFailingElements())
            el_names = []
            for eid in f_ids[:3]:
                el = d.GetElement(eid)
                if el is not None:
                    cat_s = el.Category.Name if el.Category else u"Элемент"
                    el_names.append(u"{} «{}» [{}]".format(cat_s, safe_name(el), id_int(eid)))
                else:
                    el_names.append(u"ID {}".format(id_int(eid)))
            if len(f_ids) > 3:
                el_names.append(u"и ещё {} шт.".format(len(f_ids) - 3))
            info_s = u"Элементы: " + (u", ".join(el_names) if el_names else u"—")
            warning_leafs.append(HealthIssueLeafRow(
                u"Предупреждение #{}: {}".format(idx + 1, desc_one_line),
                info_s,
                u"Предупреждение",
                'warn',
                f_ids
            ))
    except Exception:
        pass

    if warnings_count == 0:
        w_penalty = 0
        w_kind = 'ok'
    else:
        w_penalty = min(30, max(1, int(round(warnings_count / 15.0))))
        w_kind = 'err' if warnings_count >= 150 else 'warn'

    cards.append(HealthGroupCard(
        'warnings',
        u"1. Предупреждения модели Revit (Warnings)",
        u"Вес до 30%: желтые ошибки Revit (пересечения, дублирование, некорректные зависимости). Раскройте список, чтобы выделить нужные предупреждения.",
        warnings_count,
        u"-{}%".format(w_penalty) if w_penalty > 0 else u"0%",
        w_kind,
        warning_leafs
    ))

    # 2. СВЯЗИ, ОСИ И УРОВНИ В ЧУЖИХ РН
    wrong_ws_leafs = []
    for r in link_state.rows:
        if r.Kind == 'err' and r.RevitId is not None:
            wrong_ws_leafs.append(HealthIssueLeafRow(
                r.LinkName,
                u"{} • Текущий РН: «{}» ➔ Должен быть: «{}»".format(r.SubText, r.CurrentWS, r.TargetWS),
                u"Чужой РН",
                'err',
                [r.RevitId]
            ))

    err_ws_cnt = link_state.err_count
    total_ws_checked = max(1, link_state.total_checked)
    if err_ws_cnt == 0:
        ws_penalty = 0
        ws_kind = 'ok'
    else:
        ratio_ws = float(err_ws_cnt) / float(total_ws_checked)
        ws_penalty = min(25, max(2, int(round(ratio_ws * 25.0))))
        ws_kind = 'err' if ws_penalty >= 12 else 'warn'

    cards.append(HealthGroupCard(
        'wrong_ws',
        u"2. Связи, оси и уровни в чужих рабочих наборах",
        u"Вес до 25%: координационные элементы, лежащие не в своих целевых РН (ошибок: {} из {}).".format(
            err_ws_cnt, link_state.total_checked),
        err_ws_cnt,
        u"-{}%".format(ws_penalty) if ws_penalty > 0 else u"0%",
        ws_kind,
        wrong_ws_leafs
    ))

    # 3. НЕЗАКРЕПЛЕННЫЕ ЭЛЕМЕНТЫ КООРДИНАЦИИ (PIN)
    unpinned_leafs = []
    for r in pin_rows:
        if not r.IsPinned:
            unpinned_leafs.append(HealthIssueLeafRow(
                u"{}: {}".format(r.Category, r.Name),
                u"Рабочий набор: «{}» • Элемент не закреплен (Unpinned)".format(r.Workset),
                u"Не закреплено",
                'warn',
                [r.RevitId]
            ))

    unpinned_cnt = len(unpinned_leafs)
    total_pin_cnt = max(1, len(pin_rows))
    if unpinned_cnt == 0:
        pin_penalty = 0
        pin_kind = 'ok'
    else:
        ratio_pin = float(unpinned_cnt) / float(total_pin_cnt)
        pin_penalty = min(25, max(1, int(round(ratio_pin * 25.0))))
        pin_kind = 'err' if pin_penalty >= 12 else 'warn'

    cards.append(HealthGroupCard(
        'unpinned',
        u"3. Незакрепленные оси, уровни, связи и точки координат",
        u"Вес до 25%: элементы координации, которые могут быть случайно сдвинуты (не закреплено: {} из {}).".format(
            unpinned_cnt, len(pin_rows)),
        unpinned_cnt,
        u"-{}%".format(pin_penalty) if pin_penalty > 0 else u"0%",
        pin_kind,
        unpinned_leafs
    ))

    # 4. КОНТЕКСТНЫЕ СЕМЕЙСТВА (IN-PLACE)
    in_place_leafs = []
    for fi in DB.FilteredElementCollector(d).OfClass(DB.FamilyInstance).WhereElementIsNotElementType().ToElements():
        try:
            if fi.Symbol and fi.Symbol.Family and fi.Symbol.Family.IsInPlace:
                cat_n = fi.Category.Name if fi.Category else u"Модель в контексте"
                ws_n = ws_name_of(d, fi.WorksetId, names_by_id)
                in_place_leafs.append(HealthIssueLeafRow(
                    safe_name(fi.Symbol.Family),
                    u"Категория: {} • РН: «{}»".format(cat_n, ws_n),
                    u"В контексте",
                    'warn',
                    [fi.Id]
                ))
        except Exception:
            pass

    ip_cnt = len(in_place_leafs)
    if ip_cnt == 0:
        ip_penalty = 0
        ip_kind = 'ok'
    else:
        ip_penalty = min(20, ip_cnt * 2)
        ip_kind = 'err' if ip_penalty >= 10 else 'warn'

    cards.append(HealthGroupCard(
        'in_place',
        u"4. Контекстные семейства (Модель в контексте)",
        u"Вес до 20%: контекстная геометрия не кэшируется как стандартные семейства и замедляет открытие 3D-видов и экспорт NWC.",
        ip_cnt,
        u"-{}%".format(ip_penalty) if ip_penalty > 0 else u"0%",
        ip_kind,
        in_place_leafs
    ))

    total_penalty = w_penalty + ws_penalty + pin_penalty + ip_penalty
    score = max(0, min(100, 100 - total_penalty))
    return score, cards


# ==========================================================
# 4. XAML — СОВРЕМЕННЫЙ ИНТЕРФЕЙС С КАСТОМНЫМИ ПОЛЗУНКАМИ (SCROLLBAR)
# ==========================================================
XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Аудит модели" Width="1240" Height="820" MinWidth="1060" MinHeight="660"
        WindowStartupLocation="CenterScreen" WindowStyle="None" AllowsTransparency="True"
        Background="Transparent" ResizeMode="CanResizeWithGrip"
        FontFamily="Segoe UI" FontSize="14" Foreground="#FF2B2118">
    <Window.Resources>
        <BooleanToVisibilityConverter x:Key="BoolToVis"/>
        <SolidColorBrush x:Key="Accent" Color="#FFFF7A1A"/>
        <LinearGradientBrush x:Key="AccentGrad" StartPoint="0,0" EndPoint="0,1">
            <GradientStop Color="#FFFF9440" Offset="0"/>
            <GradientStop Color="#FFFF6A00" Offset="1"/>
        </LinearGradientBrush>
        <LinearGradientBrush x:Key="GlassFill" StartPoint="0,0" EndPoint="1,1">
            <GradientStop Color="#E6FFFFFF" Offset="0"/>
            <GradientStop Color="#B8FFFFFF" Offset="1"/>
        </LinearGradientBrush>

        <!-- ======================================================= -->
        <!-- КАСТОМНЫЕ ПОЛЗУНКИ ПРОКРУТКИ (SCROLLBAR) В СТИЛЕ КНОПОК -->
        <!-- ======================================================= -->
        <Style x:Key="ScrollThumbStyle" TargetType="Thumb">
            <Setter Property="SnapsToDevicePixels" Value="True"/>
            <Setter Property="OverridesDefaultStyle" Value="True"/>
            <Setter Property="IsTabStop" Value="False"/>
            <Setter Property="Focusable" Value="False"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="Thumb">
                        <Border Name="thumbBd"
                                Background="{StaticResource AccentGrad}"
                                BorderBrush="#80FFFFFF"
                                BorderThickness="1"
                                CornerRadius="5"
                                Opacity="0.72"/>
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

        <!-- ======================================================= -->
        <!-- КАРТОЧКИ И КНОПКИ                                       -->
        <!-- ======================================================= -->
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

        <Style x:Key="FilterPillToggle" TargetType="ToggleButton">
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="FontWeight" Value="SemiBold"/>
            <Setter Property="FontSize" Value="13"/>
            <Setter Property="Height" Value="38"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ToggleButton">
                        <Border Name="bd" Background="#CCFFFFFF" CornerRadius="19" BorderBrush="#66FF7A1A" BorderThickness="1" Padding="14,0,12,0">
                            <Grid>
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <ContentPresenter Grid.Column="0" VerticalAlignment="Center" HorizontalAlignment="Left"/>
                                <Path Grid.Column="1" Data="M0,0 L4.5,4.5 L9,0" Stroke="#FFFF7A1A" StrokeThickness="2" VerticalAlignment="Center" Margin="8,2,0,0"/>
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
                </Setter.Value>
            </Setter>
        </Style>

        <!-- СТИЛЬ ПРОЗРАЧНОЙ КНОПКИ-ШАПКИ ДЛЯ РАСКРЫТИЯ ВСЕЙ КАРТОЧКИ -->
        <Style x:Key="HeaderExpandToggle" TargetType="ToggleButton">
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="Focusable" Value="False"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="ToggleButton">
                        <Border Background="Transparent">
                            <ContentPresenter VerticalAlignment="Center" HorizontalAlignment="Stretch"/>
                        </Border>
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
                            <ToggleButton Name="ToggleButton"
                                          Focusable="False"
                                          ClickMode="Press"
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
                                                <TextBlock Grid.Column="0" Text="{Binding Path=Tag, RelativeSource={RelativeSource AncestorType=ComboBox}, FallbackValue='Целевой РН:'}" Foreground="#FF8C7A6B" FontSize="12" FontWeight="SemiBold" VerticalAlignment="Center" Margin="0,0,8,0"/>
                                                <TextBlock Grid.Column="1"
                                                           Text="{Binding Path=SelectedItem.Name, RelativeSource={RelativeSource AncestorType=ComboBox}, FallbackValue='Выберите...'}"
                                                           Foreground="#FF2B2118"
                                                           FontSize="13"
                                                           FontWeight="Bold"
                                                           VerticalAlignment="Center"
                                                           TextTrimming="CharacterEllipsis"/>
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

                            <Popup Name="Popup"
                                   Placement="Top"
                                   VerticalOffset="-6"
                                   IsOpen="{TemplateBinding IsDropDownOpen}"
                                   AllowsTransparency="True"
                                   Focusable="False"
                                   PopupAnimation="Slide">
                                <Border Name="DropDownBorder"
                                        Background="#FFFFFFFF"
                                        BorderBrush="#66FF7A1A"
                                        BorderThickness="1"
                                        CornerRadius="16"
                                        Padding="6"
                                        MinWidth="{TemplateBinding ActualWidth}"
                                        MaxHeight="320">
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

        <Style TargetType="DataGrid">
            <Setter Property="Background" Value="Transparent"/>
            <Setter Property="Foreground" Value="#FF2B2118"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="RowHeight" Value="48"/>
            <Setter Property="ColumnHeaderHeight" Value="38"/>
            <Setter Property="GridLinesVisibility" Value="None"/>
            <Setter Property="HeadersVisibility" Value="Column"/>
            <Setter Property="SelectionUnit" Value="FullRow"/>
            <Setter Property="CanUserResizeRows" Value="False"/>
            <Setter Property="CanUserSortColumns" Value="True"/>
            <Setter Property="RowBackground" Value="Transparent"/>
            <Setter Property="AlternatingRowBackground" Value="Transparent"/>
        </Style>

        <Style TargetType="DataGridColumnHeader">
            <Setter Property="Background" Value="Transparent"/>
            <Setter Property="Foreground" Value="#FF8C7A6B"/>
            <Setter Property="FontWeight" Value="Bold"/>
            <Setter Property="FontSize" Value="11"/>
            <Setter Property="Padding" Value="12,0"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="Cursor" Value="Hand"/>
            <Setter Property="HorizontalContentAlignment" Value="Left"/>
            <Style.Triggers>
                <Trigger Property="IsMouseOver" Value="True">
                    <Setter Property="Foreground" Value="#FFFF6A00"/>
                </Trigger>
            </Style.Triggers>
        </Style>

        <Style TargetType="DataGridCell">
            <Setter Property="Background" Value="Transparent"/>
            <Setter Property="BorderThickness" Value="0"/>
            <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="DataGridCell">
                        <Grid Background="Transparent" VerticalAlignment="Center">
                            <ContentPresenter VerticalAlignment="Center"/>
                        </Grid>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
            <Style.Triggers>
                <Trigger Property="IsSelected" Value="True">
                    <Setter Property="Foreground" Value="#FF2B2118"/>
                </Trigger>
            </Style.Triggers>
        </Style>

        <Style TargetType="DataGridRow">
            <Setter Property="Background" Value="Transparent"/>
            <Setter Property="Margin" Value="0,2"/>
            <Setter Property="Template">
                <Setter.Value>
                    <ControlTemplate TargetType="DataGridRow">
                        <Border Name="RowCard" Background="#B3FFFFFF" BorderBrush="#66FFFFFF" BorderThickness="1" CornerRadius="12" Margin="2,2" Padding="4,2">
                            <SelectiveScrollingGrid>
                                <SelectiveScrollingGrid.ColumnDefinitions>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="*"/>
                                </SelectiveScrollingGrid.ColumnDefinitions>
                                <SelectiveScrollingGrid.RowDefinitions>
                                    <RowDefinition Height="*"/>
                                    <RowDefinition Height="Auto"/>
                                </SelectiveScrollingGrid.RowDefinitions>
                                <DataGridCellsPresenter Grid.Column="1" ItemsPanel="{TemplateBinding ItemsPanel}" SnapsToDevicePixels="{TemplateBinding SnapsToDevicePixels}"/>
                            </SelectiveScrollingGrid>
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="RowCard" Property="Background" Value="#FFFFFFFF"/>
                                <Setter TargetName="RowCard" Property="BorderBrush" Value="#66FF7A1A"/>
                            </Trigger>
                            <Trigger Property="IsSelected" Value="True">
                                <Setter TargetName="RowCard" Property="Background" Value="#FFFFF1E6"/>
                                <Setter TargetName="RowCard" Property="BorderBrush" Value="#FFFF7A1A"/>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Setter.Value>
            </Setter>
        </Style>

        <DataTemplate x:Key="StatusCell">
            <Border CornerRadius="10" Padding="10,4" Margin="8,0" HorizontalAlignment="Left" VerticalAlignment="Center">
                <Border.Style>
                    <Style TargetType="Border">
                        <Setter Property="Background" Value="#26FF9500"/>
                        <Style.Triggers>
                            <DataTrigger Binding="{Binding Kind}" Value="ok"><Setter Property="Background" Value="#2634C759"/></DataTrigger>
                            <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Background" Value="#26FF3B30"/></DataTrigger>
                        </Style.Triggers>
                    </Style>
                </Border.Style>
                <TextBlock Text="{Binding Status}" FontSize="12" FontWeight="SemiBold">
                    <TextBlock.Style>
                        <Style TargetType="TextBlock">
                            <Setter Property="Foreground" Value="#FFC25E00"/>
                            <Style.Triggers>
                                <DataTrigger Binding="{Binding Kind}" Value="ok"><Setter Property="Foreground" Value="#FF1E8E3E"/></DataTrigger>
                                <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Foreground" Value="#FFC62828"/></DataTrigger>
                            </Style.Triggers>
                        </Style>
                    </TextBlock.Style>
                </TextBlock>
            </Border>
        </DataTemplate>

        <!-- ШАБЛОН СТРОКИ ЭЛЕМЕНТА В ОЧИСТКЕ МОДЕЛИ -->
        <DataTemplate x:Key="PurgeLeafItemTemplate">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="135"/>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="95"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>

                <CheckBox Grid.Column="0" IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" VerticalAlignment="Center" Margin="2,0,10,0" Cursor="Hand"/>

                <Border Grid.Column="1" CornerRadius="8" Padding="8,3" HorizontalAlignment="Left" VerticalAlignment="Center">
                    <Border.Style>
                        <Style TargetType="Border">
                            <Setter Property="Background" Value="#26FF9500"/>
                            <Style.Triggers>
                                <DataTrigger Binding="{Binding Kind}" Value="ok"><Setter Property="Background" Value="#2634C759"/></DataTrigger>
                                <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Background" Value="#26FF3B30"/></DataTrigger>
                            </Style.Triggers>
                        </Style>
                    </Border.Style>
                    <TextBlock Text="{Binding Status}" FontSize="11" FontWeight="SemiBold">
                        <TextBlock.Style>
                            <Style TargetType="TextBlock">
                                <Setter Property="Foreground" Value="#FFC25E00"/>
                                <Style.Triggers>
                                    <DataTrigger Binding="{Binding Kind}" Value="ok"><Setter Property="Foreground" Value="#FF1E8E3E"/></DataTrigger>
                                    <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Foreground" Value="#FFC62828"/></DataTrigger>
                                </Style.Triggers>
                            </Style>
                        </TextBlock.Style>
                    </TextBlock>
                </Border>

                <StackPanel Grid.Column="2" VerticalAlignment="Center" Margin="8,0">
                    <TextBlock Text="{Binding Name}" FontWeight="SemiBold" FontSize="13" TextTrimming="CharacterEllipsis"/>
                    <TextBlock Text="{Binding InfoText}" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis"/>
                </StackPanel>

                <TextBlock Grid.Column="3" Text="{Binding IdText}" VerticalAlignment="Center" FontSize="11" Foreground="#FF8C7A6B" HorizontalAlignment="Right" Margin="0,0,8,0"/>

                <Button Grid.Column="4"
                        Content="👁 Показать"
                        Tag="{Binding RevitId}"
                        Visibility="{Binding CanShow, Converter={StaticResource BoolToVis}}"
                        Style="{StaticResource GlassButton}"
                        Height="28"
                        Padding="10,0"
                        FontSize="11"
                        ToolTip="Выделить и показать этот элемент или разрез в Revit"/>
            </Grid>
        </DataTemplate>

        <!-- ШАБЛОН СТРОКИ ПРОБЛЕМЫ В РАЗДЕЛЕ ЗДОРОВЬЕ МОДЕЛИ -->
        <DataTemplate x:Key="HealthLeafItemTemplate">
            <Grid>
                <Grid.ColumnDefinitions>
                    <ColumnDefinition Width="Auto"/>
                    <ColumnDefinition Width="130"/>
                    <ColumnDefinition Width="*"/>
                    <ColumnDefinition Width="105"/>
                    <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>

                <CheckBox Grid.Column="0" IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" VerticalAlignment="Center" Margin="2,0,10,0" Cursor="Hand"/>

                <Border Grid.Column="1" CornerRadius="8" Padding="8,3" HorizontalAlignment="Left" VerticalAlignment="Center">
                    <Border.Style>
                        <Style TargetType="Border">
                            <Setter Property="Background" Value="#26FF9500"/>
                            <Style.Triggers>
                                <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Background" Value="#26FF3B30"/></DataTrigger>
                            </Style.Triggers>
                        </Style>
                    </Border.Style>
                    <TextBlock Text="{Binding Status}" FontSize="11" FontWeight="SemiBold">
                        <TextBlock.Style>
                            <Style TargetType="TextBlock">
                                <Setter Property="Foreground" Value="#FFC25E00"/>
                                <Style.Triggers>
                                    <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Foreground" Value="#FFC62828"/></DataTrigger>
                                </Style.Triggers>
                            </Style>
                        </TextBlock.Style>
                    </TextBlock>
                </Border>

                <StackPanel Grid.Column="2" VerticalAlignment="Center" Margin="8,0">
                    <TextBlock Text="{Binding Name}" FontWeight="SemiBold" FontSize="13" TextTrimming="CharacterEllipsis" ToolTip="{Binding Name}"/>
                    <TextBlock Text="{Binding InfoText}" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis" ToolTip="{Binding InfoText}"/>
                </StackPanel>

                <TextBlock Grid.Column="3" Text="{Binding IdText}" VerticalAlignment="Center" FontSize="11" Foreground="#FF8C7A6B" HorizontalAlignment="Right" Margin="0,0,8,0"/>

                <Button Grid.Column="4"
                        Content="👁 Показать"
                        Tag="{Binding ElementIds}"
                        Visibility="{Binding CanShow, Converter={StaticResource BoolToVis}}"
                        Style="{StaticResource GlassButton}"
                        Height="28"
                        Padding="10,0"
                        FontSize="11"
                        ToolTip="Выделить и показать элементы этой проблемы в модели"/>
            </Grid>
        </DataTemplate>
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
                            <TextBlock Grid.Row="0" Text="АУДИТ МОДЕЛИ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navLinks" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Связи, оси и уровни"/>
                                <RadioButton Name="nav3D" GroupName="nav" Style="{StaticResource NavItem}" Content="Аудит элементов"/>
                                <RadioButton Name="navCSV" GroupName="nav" Style="{StaticResource NavItem}" Content="Создание РН"/>
                                <RadioButton Name="navPin" GroupName="nav" Style="{StaticResource NavItem}" Content="Закрепление"/>
                                <RadioButton Name="navPurge" GroupName="nav" Style="{StaticResource NavItem}" Content="Очистка модели"/>
                                <RadioButton Name="navHealth" GroupName="nav" Style="{StaticResource NavItem}" Content="Здоровье модели"/>
                            </StackPanel>

                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="ИНДЕКС ЗДОРОВЬЯ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                    <TextBlock Name="tbSidebarScore" Text="100%" FontSize="24" FontWeight="Bold" Foreground="#FF1E8E3E" Margin="0,2,0,0"/>
                                    <TextBlock Name="tbSidebarDoc" Text="Модель" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis"/>
                                </StackPanel>
                            </Border>
                        </Grid>
                    </Border>

                    <!-- ПРАВАЯ РАБОЧАЯ ОБЛАСТЬ -->
                    <Grid Grid.Column="2">

                        <!-- ========================================== -->
                        <!-- РАЗДЕЛ 1: СВЯЗИ, ОСИ И УРОВНИ             -->
                        <!-- ========================================== -->
                        <Grid Name="pageLinks" Visibility="Visible">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <Grid Grid.Row="0" Margin="4,0,4,12">
                                <StackPanel>
                                    <TextBlock Text="Связи, оси и уровни" FontSize="26" FontWeight="Bold"/>
                                    <TextBlock Text="Автоматическая проверка соответствия рабочих наборов по ключевым словам" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0"/>
                                </StackPanel>
                            </Grid>
                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="12,10">
                                <DataGrid Name="dgLinks" AutoGenerateColumns="False" IsReadOnly="True">
                                    <DataGrid.Columns>
                                        <DataGridTemplateColumn Header="СТАТУС" Width="145" SortMemberPath="Status" CellTemplate="{StaticResource StatusCell}"/>
                                        <DataGridTemplateColumn Header="ЭЛЕМЕНТ КООРДИНАЦИИ" Width="*" SortMemberPath="LinkName">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <StackPanel VerticalAlignment="Center" Margin="10,0">
                                                        <TextBlock Text="{Binding LinkName}" FontWeight="SemiBold" FontSize="14" TextTrimming="CharacterEllipsis"/>
                                                        <TextBlock Text="{Binding SubText}" FontSize="11" Foreground="#FF8C7A6B"/>
                                                    </StackPanel>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>
                                        <DataGridTemplateColumn Header="МАРШРУТ РАБОЧЕГО НАБОРА (ТЕКУЩИЙ ➔ ЦЕЛЕВОЙ)" Width="360" SortMemberPath="CurrentWS">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <StackPanel Orientation="Horizontal" VerticalAlignment="Center" Margin="10,0">
                                                        <Border Background="#14000000" CornerRadius="8" Padding="8,4">
                                                            <TextBlock Text="{Binding CurrentWS}" FontSize="12" FontWeight="SemiBold" MaxWidth="140" TextTrimming="CharacterEllipsis"/>
                                                        </Border>
                                                        <TextBlock Text="➔" Margin="8,0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="Bold"/>
                                                        <Border Background="#1FFF7A1A" CornerRadius="8" Padding="8,4">
                                                            <TextBlock Text="{Binding TargetWS}" FontSize="12" FontWeight="SemiBold" Foreground="#FFC24E00" MaxWidth="150" TextTrimming="CharacterEllipsis"/>
                                                        </Border>
                                                    </StackPanel>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>
                                    </DataGrid.Columns>
                                </DataGrid>
                            </Border>
                            <Grid Grid.Row="2" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbLinksStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold"/>
                                <Button Name="btnRefreshLinks" Grid.Column="1" Content="Обновить" Style="{StaticResource GlassButton}" Margin="0,0,10,0"/>
                                <Button Name="btnFixLinks" Grid.Column="2" Content="Исправить ошибки автоматически" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                        <!-- ================================================================ -->
                        <!-- РАЗДЕЛ 2: АУДИТ ЭЛЕМЕНТОВ (4 КОЛОНКИ + КНОПКА ОБНОВИТЬ СВЕРХУ)   -->
                        <!-- ================================================================ -->
                        <Grid Name="page3D" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>

                            <Grid Grid.Row="0" Margin="4,0,4,10">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <StackPanel Grid.Column="0">
                                    <TextBlock Text="Аудит элементов модели" FontSize="26" FontWeight="Bold"/>
                                    <TextBlock Text="Сортировка по любой колонке (клик по шапке), выпадающие фильтры по Категории и РН" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0"/>
                                </StackPanel>
                                <!-- Оранжевая кнопка «Обновить элементы» слева от галочки «Сканировать всю модель» -->
                                <Button Name="btnRefreshAudit" Grid.Column="1" Content="Обновить элементы" Style="{StaticResource PrimaryButton}" Margin="0,0,10,0" VerticalAlignment="Center"/>
                                <Border Grid.Column="2" Background="#CCFFFFFF" CornerRadius="19" Padding="14,0" Height="38" BorderBrush="#66FF7A1A" BorderThickness="1" VerticalAlignment="Center">
                                    <CheckBox Name="chkScanWholeModel" IsChecked="True" Content="Сканировать всю модель (вкл. закрытые РН)" FontWeight="SemiBold" VerticalAlignment="Center" VerticalContentAlignment="Center" Cursor="Hand"/>
                                </Border>
                            </Grid>

                            <!-- ПАНЕЛЬ ФИЛЬТРОВ: ПОИСК + ВЫПАДАЮЩИЙ ФИЛЬТР КАТЕГОРИЙ + ВЫПАДАЮЩИЙ ФИЛЬТР РН -->
                            <Grid Grid.Row="1" Margin="0,0,0,10">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="10"/>
                                    <ColumnDefinition Width="255"/>
                                    <ColumnDefinition Width="10"/>
                                    <ColumnDefinition Width="255"/>
                                    <ColumnDefinition Width="10"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>

                                <!-- Текстовый поиск -->
                                <Border Grid.Column="0" Background="#CCFFFFFF" CornerRadius="19" Padding="14,0" Height="38" BorderBrush="#66FF7A1A" BorderThickness="1">
                                    <Grid>
                                        <Grid.ColumnDefinitions>
                                            <ColumnDefinition Width="Auto"/>
                                            <ColumnDefinition Width="*"/>
                                        </Grid.ColumnDefinitions>
                                        <TextBlock Grid.Column="0" Text="🔍 Поиск:" Foreground="#FF8C7A6B" FontSize="12" FontWeight="SemiBold" VerticalAlignment="Center" Margin="0,0,8,0"/>
                                        <TextBox Name="tbSearchAudit" Grid.Column="1" Background="Transparent" BorderThickness="0" VerticalContentAlignment="Center" FontSize="13" ToolTip="Введите часть имени категории, семейства, типа или рабочего набора"/>
                                    </Grid>
                                </Border>

                                <!-- Выпадающий мультифильтр по КАТЕГОРИИ с галочкой «Выбрать все» -->
                                <Grid Grid.Column="2">
                                    <ToggleButton Name="btnAuditCatFilter" Style="{StaticResource FilterPillToggle}">
                                        <TextBlock Name="tbAuditCatFilterLabel" Text="Категории: Все" FontWeight="Bold" FontSize="13" TextTrimming="CharacterEllipsis"/>
                                    </ToggleButton>
                                    <Popup Name="popAuditCatFilter"
                                           PlacementTarget="{Binding ElementName=btnAuditCatFilter}"
                                           Placement="Bottom"
                                           VerticalOffset="4"
                                           StaysOpen="False"
                                           AllowsTransparency="True"
                                           PopupAnimation="Slide">
                                        <Border Background="#FFFFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Padding="12" Width="310" MaxHeight="380">
                                            <Grid>
                                                <Grid.RowDefinitions>
                                                    <RowDefinition Height="Auto"/>
                                                    <RowDefinition Height="*"/>
                                                </Grid.RowDefinitions>
                                                <Border Grid.Row="0" Background="#FFFDF4EA" CornerRadius="10" Padding="8,6" Margin="0,0,0,8" BorderBrush="#40FF7A1A" BorderThickness="1">
                                                    <CheckBox Name="chkAuditCatSelectAll" IsChecked="True" Content="Выбрать все категории" FontWeight="Bold" FontSize="12" Foreground="#FFC24E00" Cursor="Hand"/>
                                                </Border>
                                                <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                                                    <ItemsControl Name="icAuditCatFilters">
                                                        <ItemsControl.ItemTemplate>
                                                            <DataTemplate>
                                                                <CheckBox Content="{Binding DisplayText}" IsChecked="{Binding IsChecked, Mode=TwoWay}" Margin="4,4" FontSize="12" FontWeight="SemiBold" Cursor="Hand"/>
                                                            </DataTemplate>
                                                        </ItemsControl.ItemTemplate>
                                                    </ItemsControl>
                                                </ScrollViewer>
                                            </Grid>
                                        </Border>
                                    </Popup>
                                </Grid>

                                <!-- Выпадающий мультифильтр по РАБОЧЕМУ НАБОРУ с галочкой «Выбрать все» -->
                                <Grid Grid.Column="4">
                                    <ToggleButton Name="btnAuditWsFilter" Style="{StaticResource FilterPillToggle}">
                                        <TextBlock Name="tbAuditWsFilterLabel" Text="Рабочие наборы: Все" FontWeight="Bold" FontSize="13" TextTrimming="CharacterEllipsis"/>
                                    </ToggleButton>
                                    <Popup Name="popAuditWsFilter"
                                           PlacementTarget="{Binding ElementName=btnAuditWsFilter}"
                                           Placement="Bottom"
                                           VerticalOffset="4"
                                           StaysOpen="False"
                                           AllowsTransparency="True"
                                           PopupAnimation="Slide">
                                        <Border Background="#FFFFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Padding="12" Width="310" MaxHeight="380">
                                            <Grid>
                                                <Grid.RowDefinitions>
                                                    <RowDefinition Height="Auto"/>
                                                    <RowDefinition Height="*"/>
                                                </Grid.RowDefinitions>
                                                <Border Grid.Row="0" Background="#FFFDF4EA" CornerRadius="10" Padding="8,6" Margin="0,0,0,8" BorderBrush="#40FF7A1A" BorderThickness="1">
                                                    <CheckBox Name="chkAuditWsSelectAll" IsChecked="True" Content="Выбрать все рабочие наборы" FontWeight="Bold" FontSize="12" Foreground="#FFC24E00" Cursor="Hand"/>
                                                </Border>
                                                <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                                                    <ItemsControl Name="icAuditWsFilters">
                                                        <ItemsControl.ItemTemplate>
                                                            <DataTemplate>
                                                                <CheckBox Content="{Binding DisplayText}" IsChecked="{Binding IsChecked, Mode=TwoWay}" Margin="4,4" FontSize="12" FontWeight="SemiBold" Cursor="Hand"/>
                                                            </DataTemplate>
                                                        </ItemsControl.ItemTemplate>
                                                    </ItemsControl>
                                                </ScrollViewer>
                                            </Grid>
                                        </Border>
                                    </Popup>
                                </Grid>

                                <Button Name="btnResetAuditFilters" Grid.Column="6" Content="Сбросить фильтры" Style="{StaticResource GlassButton}" Padding="14,0"/>
                            </Grid>

                            <!-- ТАБЛИЦА АУДИТА ЭЛЕМЕНТОВ: РОВНО 4 ОТДЕЛЬНЫЕ КОЛОНКИ С СОРТИРОВКОЙ -->
                            <Border Grid.Row="2" Style="{StaticResource GlassCard}" Padding="12,10">
                                <DataGrid Name="dgAudit" AutoGenerateColumns="False" IsReadOnly="False" CanUserSortColumns="True">
                                    <DataGrid.Columns>
                                        <DataGridTemplateColumn Width="44" CanUserSort="False">
                                            <DataGridTemplateColumn.Header>
                                                <CheckBox Name="chkAuditSelectAll" ToolTip="Выбрать / снять все элементы в таблице" Cursor="Hand"/>
                                            </DataGridTemplateColumn.Header>
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <CheckBox IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" HorizontalAlignment="Center" VerticalAlignment="Center" Cursor="Hand"/>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>

                                        <DataGridTemplateColumn Header="КАТЕГОРИЯ ⇅" Width="220" SortMemberPath="Category">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <TextBlock Text="{Binding Category}" FontWeight="Bold" FontSize="13" VerticalAlignment="Center" Margin="10,0" TextTrimming="CharacterEllipsis"/>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>

                                        <DataGridTemplateColumn Header="СЕМЕЙСТВО И ТИП ⇅" Width="*" SortMemberPath="FamilyType">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <TextBlock Text="{Binding FamilyType}" FontSize="13" Foreground="#FF4A3B2F" VerticalAlignment="Center" Margin="10,0" TextTrimming="CharacterEllipsis"/>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>

                                        <DataGridTemplateColumn Header="РАБОЧИЙ НАБОР ⇅" Width="250" SortMemberPath="Workset">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <Border CornerRadius="8" Padding="10,4" HorizontalAlignment="Left" VerticalAlignment="Center" Margin="10,0">
                                                        <Border.Style>
                                                            <Style TargetType="Border">
                                                                <Setter Property="Background" Value="#14000000"/>
                                                                <Style.Triggers>
                                                                    <DataTrigger Binding="{Binding WsStateKind}" Value="closed">
                                                                        <Setter Property="Background" Value="#26FF3B30"/>
                                                                    </DataTrigger>
                                                                </Style.Triggers>
                                                            </Style>
                                                        </Border.Style>
                                                        <TextBlock Text="{Binding WorksetBadge}" FontSize="12" FontWeight="SemiBold"/>
                                                    </Border>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>
                                    </DataGrid.Columns>
                                </DataGrid>
                            </Border>

                            <!-- НИЖНЯЯ ПАНЕЛЬ АУДИТА ЭЛЕМЕНТОВ -->
                            <Grid Grid.Row="3" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbAuditStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextTrimming="CharacterEllipsis" Margin="0,0,10,0"/>
                                <Button Name="btnIsolateSelected" Grid.Column="1" Content="Изолировать на 3D" Style="{StaticResource GlassButton}" Margin="0,0,8,0"/>
                                <Button Name="btnResetIsolation" Grid.Column="2" Content="Сбросить" Style="{StaticResource GlassButton}" Margin="0,0,12,0"/>
                                <ComboBox Name="cmbTargetWs" Tag="Целевой РН:" Grid.Column="3" Width="280" Style="{StaticResource PillComboBox}" Margin="0,0,10,0" ToolTip="Выберите целевой рабочий набор для переноса отмеченных элементов"/>
                                <Button Name="btnMoveSelectedWs" Grid.Column="4" Content="Перенести в РН" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                        <!-- ============================================================ -->
                        <!-- РАЗДЕЛ 3: СОЗДАНИЕ РН ПО CSV + ЭКСПОРТ ШАБЛОНА И РН ПРОЕКТА  -->
                        <!-- ============================================================ -->
                        <Grid Name="pageCSV" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <Grid Grid.Row="0" Margin="4,0,4,12">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <StackPanel Grid.Column="0">
                                    <TextBlock Text="Создание рабочих наборов из CSV" FontSize="26" FontWeight="Bold"/>
                                    <TextBlock Text="Формат строк: Имя рабочего набора ; Ключевое слово для поиска связи (необязательно)" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0"/>
                                </StackPanel>
                                <Button Name="btnDownloadCsvTemplate" Grid.Column="1" Content="Скачать шаблон РН" Style="{StaticResource GlassButton}" Margin="0,0,8,0" VerticalAlignment="Center"/>
                                <Button Name="btnExportProjectWsCsv" Grid.Column="2" Content="Скачать список РН из проекта" Style="{StaticResource GlassButton}" VerticalAlignment="Center"/>
                            </Grid>
                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="16">
                                <TextBox Name="tbCsvInput" AcceptsReturn="True" VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Auto" Background="Transparent" BorderThickness="0" FontFamily="Consolas" FontSize="14" TextWrapping="Wrap"/>
                            </Border>
                            <Grid Grid.Row="2" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbCsvStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold"/>
                                <Button Name="btnLoadCsvFile" Grid.Column="1" Content="Загрузить CSV файл..." Style="{StaticResource GlassButton}" Margin="0,0,10,0"/>
                                <Button Name="btnCreateWsFromCsv" Grid.Column="2" Content="Создать рабочие наборы" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                        <!-- ========================================== -->
                        <!-- РАЗДЕЛ 4: ЗАКРЕПЛЕНИЕ (PIN)               -->
                        <!-- ========================================== -->
                        <Grid Name="pagePin" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <StackPanel Grid.Row="0" Margin="4,0,4,10">
                                <TextBlock Text="Контроль закрепления элементов" FontSize="26" FontWeight="Bold"/>
                                <TextBlock Text="Проверка закрепления (Pin) осей, уровней, связей RVT и базовых точек координат" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0"/>
                            </StackPanel>
                            <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="4,0,4,10">
                                <CheckBox Name="chkPinGrids" Content="Оси" IsChecked="True" FontWeight="SemiBold" Margin="0,0,16,0" Cursor="Hand"/>
                                <CheckBox Name="chkPinLevels" Content="Уровни" IsChecked="True" FontWeight="SemiBold" Margin="0,0,16,0" Cursor="Hand"/>
                                <CheckBox Name="chkPinLinks" Content="Связи RVT" IsChecked="True" FontWeight="SemiBold" Margin="0,0,16,0" Cursor="Hand"/>
                                <CheckBox Name="chkPinPoints" Content="Точки координат" IsChecked="True" FontWeight="SemiBold" Margin="0,0,24,0" Cursor="Hand"/>
                                <CheckBox Name="chkPinOnlyUnpinned" Content="Показывать только незакрепленные" IsChecked="False" FontWeight="SemiBold" Foreground="#FFC24E00" Cursor="Hand"/>
                            </StackPanel>
                            <Border Grid.Row="2" Style="{StaticResource GlassCard}" Padding="12,10">
                                <DataGrid Name="dgPins" AutoGenerateColumns="False" IsReadOnly="False">
                                    <DataGrid.Columns>
                                        <DataGridTemplateColumn Width="44" CanUserSort="False">
                                            <DataGridTemplateColumn.Header>
                                                <CheckBox Name="chkPinSelectAll" Cursor="Hand"/>
                                            </DataGridTemplateColumn.Header>
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <CheckBox IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" HorizontalAlignment="Center" VerticalAlignment="Center" Cursor="Hand"/>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>
                                        <DataGridTemplateColumn Header="СТАТУС ⇅" Width="155" SortMemberPath="Status" CellTemplate="{StaticResource StatusCell}"/>
                                        <DataGridTemplateColumn Header="КАТЕГОРИЯ ⇅" Width="170" SortMemberPath="Category">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <TextBlock Text="{Binding Category}" FontWeight="Bold" FontSize="13" VerticalAlignment="Center" Margin="8,0"/>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>
                                        <DataGridTemplateColumn Header="ИМЯ ЭЛЕМЕНТА ⇅" Width="*" SortMemberPath="Name">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <TextBlock Text="{Binding Name}" FontWeight="SemiBold" FontSize="13" VerticalAlignment="Center" Margin="8,0" TextTrimming="CharacterEllipsis"/>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>
                                        <DataGridTemplateColumn Header="РАБОЧИЙ НАБОР ⇅" Width="220" SortMemberPath="Workset">
                                            <DataGridTemplateColumn.CellTemplate>
                                                <DataTemplate>
                                                    <TextBlock Text="{Binding Workset}" VerticalAlignment="Center" Margin="8,0" FontSize="12" FontWeight="SemiBold"/>
                                                </DataTemplate>
                                            </DataGridTemplateColumn.CellTemplate>
                                        </DataGridTemplateColumn>
                                    </DataGrid.Columns>
                                </DataGrid>
                            </Border>
                            <Grid Grid.Row="3" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbPinStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold"/>
                                <Button Name="btnRefreshPins" Grid.Column="1" Content="Обновить" Style="{StaticResource GlassButton}" Margin="0,0,8,0"/>
                                <Button Name="btnUnpinSelected" Grid.Column="2" Content="Открепить выбранные" Style="{StaticResource GlassButton}" Margin="0,0,8,0"/>
                                <Button Name="btnPinSelected" Grid.Column="3" Content="Закрепить выбранные (Pin)" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                        <!-- ========================================================================= -->
                        <!-- РАЗДЕЛ 5: ОЧИСТКА МОДЕЛИ (МУЛЬТИФИЛЬТР ВСЕХ КАТЕГОРИЙ С ЧЕКБОКСАМИ + ДЕРЕВО)-->
                        <!-- ========================================================================= -->
                        <Grid Name="pagePurge" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>

                            <Grid Grid.Row="0" Margin="4,0,4,10">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <StackPanel Grid.Column="0">
                                    <TextBlock Text="Очистка модели (Удалить неиспользуемое)" FontSize="26" FontWeight="Bold"/>
                                    <TextBlock Text="Выбирайте нужные категории через мультифильтр сверху или раскрывайте разделы кликом по заголовку" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0"/>
                                </StackPanel>
                                <StackPanel Grid.Column="1" Orientation="Horizontal" VerticalAlignment="Center">
                                    <Button Name="btnPurgeExpandAll" Content="Развернуть все" Style="{StaticResource GlassButton}" Height="34" Padding="14,0" Margin="0,0,6,0"/>
                                    <Button Name="btnPurgeCollapseAll" Content="Свернуть все" Style="{StaticResource GlassButton}" Height="34" Padding="14,0"/>
                                </StackPanel>
                            </Grid>

                            <!-- ПАНЕЛЬ ПОИСКА + ВЫПАДАЮЩИЙ МУЛЬТИФИЛЬТР ВСЕХ КАТЕГОРИЙ С ЧЕКБОКСАМИ -->
                            <Grid Grid.Row="1" Margin="0,0,0,10">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="230"/>
                                    <ColumnDefinition Width="8"/>
                                    <ColumnDefinition Width="275"/>
                                    <ColumnDefinition Width="8"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>

                                <!-- Поиск по имени -->
                                <Border Grid.Column="0" Background="#CCFFFFFF" CornerRadius="19" Padding="12,0" Height="38" BorderBrush="#66FF7A1A" BorderThickness="1">
                                    <Grid>
                                        <Grid.ColumnDefinitions>
                                            <ColumnDefinition Width="Auto"/>
                                            <ColumnDefinition Width="*"/>
                                        </Grid.ColumnDefinitions>
                                        <TextBlock Grid.Column="0" Text="🔍" VerticalAlignment="Center" Margin="2,0,6,0"/>
                                        <TextBox Name="tbSearchPurge" Grid.Column="1" Background="Transparent" BorderThickness="0" VerticalContentAlignment="Center" FontSize="13" ToolTip="Поиск по имени файла DWG, картинки, разреза или семейства"/>
                                    </Grid>
                                </Border>

                                <!-- Выпадающий мультифильтр ВСЕХ КАТЕГОРИЙ очистки с галочкой «Выбрать все» -->
                                <Grid Grid.Column="2">
                                    <ToggleButton Name="btnPurgeCatFilter" Style="{StaticResource FilterPillToggle}">
                                        <TextBlock Name="tbPurgeCatFilterLabel" Text="Категории: Все" FontWeight="Bold" FontSize="13" TextTrimming="CharacterEllipsis"/>
                                    </ToggleButton>
                                    <Popup Name="popPurgeCatFilter"
                                           PlacementTarget="{Binding ElementName=btnPurgeCatFilter}"
                                           Placement="Bottom"
                                           VerticalOffset="4"
                                           StaysOpen="False"
                                           AllowsTransparency="True"
                                           PopupAnimation="Slide">
                                        <Border Background="#FFFFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Padding="12" Width="340" MaxHeight="400">
                                            <Grid>
                                                <Grid.RowDefinitions>
                                                    <RowDefinition Height="Auto"/>
                                                    <RowDefinition Height="*"/>
                                                </Grid.RowDefinitions>
                                                <Border Grid.Row="0" Background="#FFFDF4EA" CornerRadius="10" Padding="8,6" Margin="0,0,0,8" BorderBrush="#40FF7A1A" BorderThickness="1">
                                                    <CheckBox Name="chkPurgeCatSelectAll" IsChecked="True" Content="Выбрать все категории" FontWeight="Bold" FontSize="12" Foreground="#FFC24E00" Cursor="Hand"/>
                                                </Border>
                                                <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                                                    <ItemsControl Name="icPurgeCatFilters">
                                                        <ItemsControl.ItemTemplate>
                                                            <DataTemplate>
                                                                <CheckBox Content="{Binding DisplayText}" IsChecked="{Binding IsChecked, Mode=TwoWay}" Margin="4,4" FontSize="12" FontWeight="SemiBold" Cursor="Hand"/>
                                                            </DataTemplate>
                                                        </ItemsControl.ItemTemplate>
                                                    </ItemsControl>
                                                </ScrollViewer>
                                            </Grid>
                                        </Border>
                                    </Popup>
                                </Grid>

                                <Border Grid.Column="4" Background="#CCFFFFFF" CornerRadius="19" Padding="12,0" Height="38" BorderBrush="#80FFFFFF" BorderThickness="1">
                                    <CheckBox Name="chkPurgeOnlyUnused" Content="Скрыть используемые" IsChecked="False" FontWeight="SemiBold" VerticalContentAlignment="Center" Cursor="Hand" ToolTip="Скрыть используемые картинки и PDF"/>
                                </Border>

                                <StackPanel Grid.Column="6" Orientation="Horizontal">
                                    <Button Name="btnPurgeSelectUnused" Content="Отметить неиспользуемые" Style="{StaticResource GlassButton}" Height="38" Padding="14,0" Margin="0,0,6,0"/>
                                    <Button Name="btnPurgeClearChecks" Content="Снять все" Style="{StaticResource GlassButton}" Height="38" Padding="14,0"/>
                                </StackPanel>
                            </Grid>

                            <!-- ОСНОВНОЙ СПИСОК РАЗДЕЛОВ ОЧИСТКИ (КЛИК ПО ВСЕЙ ШАПКЕ РАСКРЫВАЕТ СПИСОК) -->
                            <Border Grid.Row="2" Style="{StaticResource GlassCard}" Padding="14,12">
                                <ScrollViewer VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Disabled">
                                    <ItemsControl Name="icPurgeGroups">
                                        <ItemsControl.ItemTemplate>
                                            <DataTemplate>
                                                <Border Background="#D9FFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Margin="0,0,4,10" Padding="12,10">
                                                    <StackPanel>
                                                        <!-- ШАПКА РАЗДЕЛА ОЧИСТКИ -->
                                                        <Grid>
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="Auto"/>
                                                                <ColumnDefinition Width="Auto"/>
                                                                <ColumnDefinition Width="*"/>
                                                                <ColumnDefinition Width="Auto"/>
                                                                <ColumnDefinition Width="Auto"/>
                                                            </Grid.ColumnDefinitions>

                                                            <CheckBox Grid.Column="0" IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" VerticalAlignment="Center" Margin="4,0,10,0" Cursor="Hand" ToolTip="Выбрать / снять все элементы в этом разделе"/>

                                                            <ToggleButton Grid.Column="1" IsChecked="{Binding IsExpanded, Mode=TwoWay}" Width="28" Height="28" Cursor="Hand" Margin="0,0,8,0" ToolTip="Раскрыть / свернуть раздел">
                                                                <ToggleButton.Template>
                                                                    <ControlTemplate TargetType="ToggleButton">
                                                                        <Border Background="#1FFF7A1A" CornerRadius="8">
                                                                            <TextBlock Name="arrow" Text="▶" FontSize="11" Foreground="#FFFF6A00" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                                                                        </Border>
                                                                        <ControlTemplate.Triggers>
                                                                            <Trigger Property="IsChecked" Value="True">
                                                                                <Setter TargetName="arrow" Property="Text" Value="▼"/>
                                                                            </Trigger>
                                                                        </ControlTemplate.Triggers>
                                                                    </ControlTemplate>
                                                                </ToggleButton.Template>
                                                            </ToggleButton>

                                                            <!-- Клик по заголовку также раскрывает/сворачивает раздел -->
                                                            <ToggleButton Grid.Column="2" IsChecked="{Binding IsExpanded, Mode=TwoWay}" Style="{StaticResource HeaderExpandToggle}">
                                                                <StackPanel VerticalAlignment="Center">
                                                                    <TextBlock Text="{Binding Title}" FontSize="15" FontWeight="Bold" Foreground="#FF2B2118"/>
                                                                    <TextBlock Text="{Binding Subtitle}" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis"/>
                                                                </StackPanel>
                                                            </ToggleButton>

                                                            <Border Grid.Column="3" Background="#14000000" CornerRadius="10" Padding="10,4" VerticalAlignment="Center" Margin="8,0">
                                                                <TextBlock Text="{Binding UnusedBadge}" FontSize="11" FontWeight="SemiBold" Foreground="#FF6E5D4F"/>
                                                            </Border>

                                                            <Border Grid.Column="4" Background="#1FFF7A1A" CornerRadius="10" Padding="10,4" VerticalAlignment="Center">
                                                                <TextBlock Text="{Binding CountBadge}" FontSize="12" FontWeight="Bold" Foreground="#FFC24E00"/>
                                                            </Border>
                                                        </Grid>

                                                        <!-- ТЕЛО РАЗДЕЛА -->
                                                        <StackPanel Visibility="{Binding IsExpanded, Converter={StaticResource BoolToVis}}" Margin="28,10,0,2">

                                                            <!-- 1) ПЛОСКИЙ СПИСОК -->
                                                            <ListBox ItemsSource="{Binding Items}"
                                                                     Visibility="{Binding FlatVisibility}"
                                                                     ItemTemplate="{StaticResource PurgeLeafItemTemplate}"
                                                                     MaxHeight="270"
                                                                     Background="Transparent"
                                                                     BorderThickness="0"
                                                                     ScrollViewer.HorizontalScrollBarVisibility="Disabled"
                                                                     VirtualizingStackPanel.IsVirtualizing="True"
                                                                     VirtualizingStackPanel.VirtualizationMode="Recycling">
                                                                <ListBox.ItemContainerStyle>
                                                                    <Style TargetType="ListBoxItem">
                                                                        <Setter Property="Padding" Value="0"/>
                                                                        <Setter Property="Margin" Value="0,2"/>
                                                                        <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
                                                                        <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
                                                                        <Setter Property="Template">
                                                                            <Setter.Value>
                                                                                <ControlTemplate TargetType="ListBoxItem">
                                                                                    <Border Name="itemBd" Background="#B3FFF9F2" BorderBrush="#408C7A6B" BorderThickness="1" CornerRadius="10" Padding="10,6" Margin="0,2">
                                                                                        <ContentPresenter/>
                                                                                    </Border>
                                                                                    <ControlTemplate.Triggers>
                                                                                        <Trigger Property="IsMouseOver" Value="True">
                                                                                            <Setter TargetName="itemBd" Property="Background" Value="#FFFFFFFF"/>
                                                                                            <Setter TargetName="itemBd" Property="BorderBrush" Value="#80FF7A1A"/>
                                                                                        </Trigger>
                                                                                    </ControlTemplate.Triggers>
                                                                                </ControlTemplate>
                                                                            </Setter.Value>
                                                                        </Setter>
                                                                    </Style>
                                                                </ListBox.ItemContainerStyle>
                                                            </ListBox>

                                                            <!-- 2) ДЕРЕВО ПОДГРУПП ПО КАТЕГОРИЯМ СЕМЕЙСТВ -->
                                                            <ItemsControl ItemsSource="{Binding SubGroups}"
                                                                          Visibility="{Binding SubGroupsVisibility}">
                                                                <ItemsControl.ItemTemplate>
                                                                    <DataTemplate>
                                                                        <Border Background="#CCFFF6EC" BorderBrush="#4DFF7A1A" BorderThickness="1" CornerRadius="12" Margin="0,3" Padding="10,7">
                                                                            <StackPanel>
                                                                                <Grid>
                                                                                    <Grid.ColumnDefinitions>
                                                                                        <ColumnDefinition Width="Auto"/>
                                                                                        <ColumnDefinition Width="Auto"/>
                                                                                        <ColumnDefinition Width="*"/>
                                                                                        <ColumnDefinition Width="Auto"/>
                                                                                    </Grid.ColumnDefinitions>

                                                                                    <CheckBox Grid.Column="0" IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" VerticalAlignment="Center" Margin="2,0,8,0" Cursor="Hand" ToolTip="Выбрать / снять всю категорию семейств"/>

                                                                                    <ToggleButton Grid.Column="1" IsChecked="{Binding IsExpanded, Mode=TwoWay}" Width="24" Height="24" Cursor="Hand" Margin="0,0,8,0">
                                                                                        <ToggleButton.Template>
                                                                                            <ControlTemplate TargetType="ToggleButton">
                                                                                                <Border Background="#1FFF7A1A" CornerRadius="6">
                                                                                                    <TextBlock Name="subArrow" Text="▸" FontSize="12" FontWeight="Bold" Foreground="#FFFF6A00" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                                                                                                </Border>
                                                                                                <ControlTemplate.Triggers>
                                                                                                    <Trigger Property="IsChecked" Value="True">
                                                                                                        <Setter TargetName="subArrow" Property="Text" Value="▾"/>
                                                                                                    </Trigger>
                                                                                                </ControlTemplate.Triggers>
                                                                                            </ControlTemplate>
                                                                                        </ToggleButton.Template>
                                                                                    </ToggleButton>

                                                                                    <ToggleButton Grid.Column="2" IsChecked="{Binding IsExpanded, Mode=TwoWay}" Style="{StaticResource HeaderExpandToggle}">
                                                                                        <TextBlock Text="{Binding CategoryName}" FontWeight="Bold" FontSize="13" VerticalAlignment="Center" Foreground="#FF2B2118"/>
                                                                                    </ToggleButton>

                                                                                    <Border Grid.Column="3" Background="#1FFF7A1A" CornerRadius="8" Padding="8,2" VerticalAlignment="Center">
                                                                                        <TextBlock Text="{Binding CountBadge}" FontSize="11" FontWeight="Bold" Foreground="#FFC24E00"/>
                                                                                    </Border>
                                                                                </Grid>

                                                                                <ListBox ItemsSource="{Binding Items}"
                                                                                         Visibility="{Binding IsExpanded, Converter={StaticResource BoolToVis}}"
                                                                                         ItemTemplate="{StaticResource PurgeLeafItemTemplate}"
                                                                                         MaxHeight="220"
                                                                                         Margin="26,6,0,2"
                                                                                         Background="Transparent"
                                                                                         BorderThickness="0"
                                                                                         ScrollViewer.HorizontalScrollBarVisibility="Disabled"
                                                                                         VirtualizingStackPanel.IsVirtualizing="True"
                                                                                         VirtualizingStackPanel.VirtualizationMode="Recycling">
                                                                                    <ListBox.ItemContainerStyle>
                                                                                        <Style TargetType="ListBoxItem">
                                                                                            <Setter Property="Padding" Value="0"/>
                                                                                            <Setter Property="Margin" Value="0,1"/>
                                                                                            <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
                                                                                            <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
                                                                                            <Setter Property="Template">
                                                                                                <Setter.Value>
                                                                                                    <ControlTemplate TargetType="ListBoxItem">
                                                                                                        <Border Name="subItemBd" Background="#E6FFFFFF" BorderBrush="#338C7A6B" BorderThickness="1" CornerRadius="8" Padding="8,5" Margin="0,1">
                                                                                                            <ContentPresenter/>
                                                                                                        </Border>
                                                                                                        <ControlTemplate.Triggers>
                                                                                                            <Trigger Property="IsMouseOver" Value="True">
                                                                                                                <Setter TargetName="subItemBd" Property="Background" Value="#FFFFFFFF"/>
                                                                                                                <Setter TargetName="subItemBd" Property="BorderBrush" Value="#80FF7A1A"/>
                                                                                                            </Trigger>
                                                                                                        </ControlTemplate.Triggers>
                                                                                                    </ControlTemplate>
                                                                                                </Setter.Value>
                                                                                            </Setter>
                                                                                        </Style>
                                                                                    </ListBox.ItemContainerStyle>
                                                                                </ListBox>
                                                                            </StackPanel>
                                                                        </Border>
                                                                    </DataTemplate>
                                                                </ItemsControl.ItemTemplate>
                                                            </ItemsControl>

                                                        </StackPanel>
                                                    </StackPanel>
                                                </Border>
                                            </DataTemplate>
                                        </ItemsControl.ItemTemplate>
                                    </ItemsControl>
                                </ScrollViewer>
                            </Border>

                            <Grid Grid.Row="3" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbPurgeStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold"/>
                                <Button Name="btnRefreshPurge" Grid.Column="1" Content="Пересканировать" Style="{StaticResource GlassButton}" Margin="0,0,8,0"/>
                                <Button Name="btnDeletePurgeSelected" Grid.Column="2" Content="Удалить отмеченные элементы" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                        <!-- ========================================================================= -->
                        <!-- РАЗДЕЛ 6: ЗДОРОВЬЕ МОДЕЛИ (РАСКРЫВАЮЩИЕСЯ СПИСКИ ПРОБЛЕМ С ЧЕКБОКСАМИ)   -->
                        <!-- ========================================================================= -->
                        <Grid Name="pageHealth" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <Grid Grid.Row="0" Margin="4,0,4,12">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <StackPanel Grid.Column="0">
                                    <TextBlock Text="Диагностика здоровья модели" FontSize="26" FontWeight="Bold"/>
                                    <TextBlock Text="Если по критерию есть штраф — раскройте список, отметьте нужные проблемы галочками и выделите их в модели" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0"/>
                                </StackPanel>
                                <Border Grid.Column="1" Background="#CCFFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Padding="16,8" VerticalAlignment="Center">
                                    <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
                                        <TextBlock Text="ИТОГОВЫЙ ИНДЕКС:" FontSize="12" FontWeight="Bold" Foreground="#FF8C7A6B" VerticalAlignment="Center" Margin="0,0,10,0"/>
                                        <TextBlock Name="tbHealthBigScore" Text="100%" FontSize="22" FontWeight="Bold" Foreground="#FF1E8E3E" VerticalAlignment="Center"/>
                                    </StackPanel>
                                </Border>
                            </Grid>

                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="14,12">
                                <ScrollViewer VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Disabled">
                                    <ItemsControl Name="icHealthGroups">
                                        <ItemsControl.ItemTemplate>
                                            <DataTemplate>
                                                <Border Background="#D9FFFFFF" BorderBrush="#66FF7A1A" BorderThickness="1" CornerRadius="16" Margin="0,0,4,10" Padding="14,12">
                                                    <StackPanel>
                                                        <Grid>
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="Auto"/>
                                                                <ColumnDefinition Width="Auto"/>
                                                                <ColumnDefinition Width="175"/>
                                                                <ColumnDefinition Width="*"/>
                                                                <ColumnDefinition Width="Auto"/>
                                                            </Grid.ColumnDefinitions>

                                                            <!-- Галочка и кнопка раскрытия видны ТОЛЬКО если есть проблемы (HasIssuesVisibility) -->
                                                            <CheckBox Grid.Column="0"
                                                                      Visibility="{Binding HasIssuesVisibility}"
                                                                      IsChecked="{Binding IsChecked, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}"
                                                                      VerticalAlignment="Center"
                                                                      Margin="2,0,10,0"
                                                                      Cursor="Hand"
                                                                      ToolTip="Выбрать / снять все проблемы в этой группе"/>

                                                            <ToggleButton Grid.Column="1"
                                                                          Visibility="{Binding HasIssuesVisibility}"
                                                                          IsChecked="{Binding IsExpanded, Mode=TwoWay}"
                                                                          Width="28" Height="28"
                                                                          Cursor="Hand"
                                                                          Margin="0,0,10,0"
                                                                          ToolTip="Раскрыть / свернуть список проблем">
                                                                <ToggleButton.Template>
                                                                    <ControlTemplate TargetType="ToggleButton">
                                                                        <Border Background="#1FFF7A1A" CornerRadius="8">
                                                                            <TextBlock Name="hArrow" Text="▶" FontSize="11" Foreground="#FFFF6A00" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                                                                        </Border>
                                                                        <ControlTemplate.Triggers>
                                                                            <Trigger Property="IsChecked" Value="True">
                                                                                <Setter TargetName="hArrow" Property="Text" Value="▼"/>
                                                                            </Trigger>
                                                                        </ControlTemplate.Triggers>
                                                                    </ControlTemplate>
                                                                </ToggleButton.Template>
                                                            </ToggleButton>

                                                            <!-- Статус и штраф -->
                                                            <Border Grid.Column="2" CornerRadius="10" Padding="10,4" Margin="0,0,10,0" HorizontalAlignment="Left" VerticalAlignment="Center">
                                                                <Border.Style>
                                                                    <Style TargetType="Border">
                                                                        <Setter Property="Background" Value="#26FF9500"/>
                                                                        <Style.Triggers>
                                                                            <DataTrigger Binding="{Binding Kind}" Value="ok"><Setter Property="Background" Value="#2634C759"/></DataTrigger>
                                                                            <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Background" Value="#26FF3B30"/></DataTrigger>
                                                                        </Style.Triggers>
                                                                    </Style>
                                                                </Border.Style>
                                                                <TextBlock Text="{Binding Status}" FontSize="12" FontWeight="SemiBold">
                                                                    <TextBlock.Style>
                                                                        <Style TargetType="TextBlock">
                                                                            <Setter Property="Foreground" Value="#FFC25E00"/>
                                                                            <Style.Triggers>
                                                                                <DataTrigger Binding="{Binding Kind}" Value="ok"><Setter Property="Foreground" Value="#FF1E8E3E"/></DataTrigger>
                                                                                <DataTrigger Binding="{Binding Kind}" Value="err"><Setter Property="Foreground" Value="#FFC62828"/></DataTrigger>
                                                                            </Style.Triggers>
                                                                        </Style>
                                                                    </TextBlock.Style>
                                                                </TextBlock>
                                                            </Border>

                                                            <ToggleButton Grid.Column="3" IsChecked="{Binding IsExpanded, Mode=TwoWay}" Style="{StaticResource HeaderExpandToggle}">
                                                                <StackPanel VerticalAlignment="Center">
                                                                    <TextBlock Text="{Binding Title}" FontSize="15" FontWeight="Bold" Foreground="#FF2B2118"/>
                                                                    <TextBlock Text="{Binding Description}" FontSize="11" Foreground="#FF6E5D4F" TextWrapping="Wrap"/>
                                                                </StackPanel>
                                                            </ToggleButton>

                                                            <Border Grid.Column="4" Background="#1FFF7A1A" CornerRadius="10" Padding="10,4" VerticalAlignment="Center" Margin="10,0,0,0">
                                                                <TextBlock Text="{Binding CountBadge}" FontSize="12" FontWeight="Bold" Foreground="#FFC24E00"/>
                                                            </Border>
                                                        </Grid>

                                                        <!-- РАСКРЫВАЮЩИЙСЯ СПИСОК ПРОБЛЕМ -->
                                                        <ListBox ItemsSource="{Binding Items}"
                                                                 Visibility="{Binding BodyVisibility}"
                                                                 ItemTemplate="{StaticResource HealthLeafItemTemplate}"
                                                                 MaxHeight="250"
                                                                 Margin="32,10,0,2"
                                                                 Background="Transparent"
                                                                 BorderThickness="0"
                                                                 ScrollViewer.HorizontalScrollBarVisibility="Disabled"
                                                                 VirtualizingStackPanel.IsVirtualizing="True"
                                                                 VirtualizingStackPanel.VirtualizationMode="Recycling">
                                                            <ListBox.ItemContainerStyle>
                                                                <Style TargetType="ListBoxItem">
                                                                    <Setter Property="Padding" Value="0"/>
                                                                    <Setter Property="Margin" Value="0,2"/>
                                                                    <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
                                                                    <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
                                                                    <Setter Property="Template">
                                                                        <Setter.Value>
                                                                            <ControlTemplate TargetType="ListBoxItem">
                                                                                <Border Name="hItemBd" Background="#B3FFF9F2" BorderBrush="#408C7A6B" BorderThickness="1" CornerRadius="10" Padding="10,6" Margin="0,2">
                                                                                    <ContentPresenter/>
                                                                                </Border>
                                                                                <ControlTemplate.Triggers>
                                                                                    <Trigger Property="IsMouseOver" Value="True">
                                                                                        <Setter TargetName="hItemBd" Property="Background" Value="#FFFFFFFF"/>
                                                                                        <Setter TargetName="hItemBd" Property="BorderBrush" Value="#80FF7A1A"/>
                                                                                    </Trigger>
                                                                                </ControlTemplate.Triggers>
                                                                            </ControlTemplate>
                                                                        </Setter.Value>
                                                                    </Setter>
                                                                </Style>
                                                            </ListBox.ItemContainerStyle>
                                                        </ListBox>
                                                    </StackPanel>
                                                </Border>
                                            </DataTemplate>
                                        </ItemsControl.ItemTemplate>
                                    </ItemsControl>
                                </ScrollViewer>
                            </Border>

                            <Grid Grid.Row="2" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbHealthStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold"/>
                                <Button Name="btnSelectHealthElements" Grid.Column="1" Content="Выделить выбранные в модели" Style="{StaticResource GlassButton}" Margin="0,0,8,0"/>
                                <Button Name="btnRefreshHealth" Grid.Column="2" Content="Обновить диагностику" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                    </Grid>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>
"""


# ==========================================================
# 5. ОБРАБОТЧИК EXTERNAL EVENT ДЛЯ БЕЗОПАСНЫХ ТРАНЗАКЦИЙ REVIT
# ==========================================================
class ActionEventHandler(IExternalEventHandler):
    def __init__(self):
        self.window = None
        self.action = None
        self.payload = None

    def Execute(self, uiapp):
        if self.window is None or not self.action:
            return
        try:
            self.window.run_revit_action(self.action, self.payload)
        except Exception as ex:
            forms.alert(u"Ошибка выполнения операции:\n{}".format(ex))

    def GetName(self):
        return "RevitModelAuditActionHandler"


# ==========================================================
# 6. КОНТРОЛЛЕР ОКНА
# ==========================================================
class AuditWindow(object):
    def __init__(self):
        self.win = XamlReader.Parse(XAML_UI)
        try:
            helper = WindowInteropHelper(self.win)
            helper.Owner = Process.GetCurrentProcess().MainWindowHandle
        except Exception:
            pass

        self.ext_handler = ActionEventHandler()
        self.ext_handler.window = self
        self.ext_event = ExternalEvent.Create(self.ext_handler)

        self._bind_controls()

        self.worksets = []
        self.names_by_id = {}
        self.open_by_id = {}
        self.links_state = LinksState()
        self.all_audit_rows = []
        self.cat_filter_items = []
        self.ws_filter_items = []
        self.all_pin_rows = []
        self.purge_groups = []
        self.purge_cat_filter_items = []
        self.health_cards = []

        # Флаги ленивой загрузки
        self._audit_loaded = False
        self._pin_loaded = False
        self._purge_loaded = False
        self._health_loaded = False
        self._suppress_filter_events = False

        self.tbSidebarDoc.Text = doc.Title
        self.tbCsvInput.Text = DEFAULT_CSV_TEMPLATE.strip()
        self.refresh_worksets_cache()
        self.refresh_links_view()
        self.refresh_health_view()

    def _bind_controls(self):
        names = [
            'TitleBar', 'btnCloseDot', 'btnMinDot', 'btnMaxDot',
            'navLinks', 'nav3D', 'navCSV', 'navPin', 'navPurge', 'navHealth',
            'tbSidebarScore', 'tbSidebarDoc',
            'pageLinks', 'page3D', 'pageCSV', 'pagePin', 'pagePurge', 'pageHealth',
            'dgLinks', 'tbLinksStatus', 'btnRefreshLinks', 'btnFixLinks',
            'btnRefreshAudit', 'chkScanWholeModel', 'tbSearchAudit',
            'btnAuditCatFilter', 'tbAuditCatFilterLabel', 'popAuditCatFilter', 'chkAuditCatSelectAll', 'icAuditCatFilters',
            'btnAuditWsFilter', 'tbAuditWsFilterLabel', 'popAuditWsFilter', 'chkAuditWsSelectAll', 'icAuditWsFilters',
            'btnResetAuditFilters', 'dgAudit', 'chkAuditSelectAll', 'tbAuditStatus', 'btnIsolateSelected', 'btnResetIsolation',
            'cmbTargetWs', 'btnMoveSelectedWs',
            'btnDownloadCsvTemplate', 'btnExportProjectWsCsv', 'tbCsvInput', 'tbCsvStatus', 'btnLoadCsvFile', 'btnCreateWsFromCsv',
            'chkPinGrids', 'chkPinLevels', 'chkPinLinks', 'chkPinPoints', 'chkPinOnlyUnpinned',
            'dgPins', 'chkPinSelectAll', 'tbPinStatus', 'btnRefreshPins', 'btnUnpinSelected', 'btnPinSelected',
            'btnPurgeExpandAll', 'btnPurgeCollapseAll', 'tbSearchPurge',
            'btnPurgeCatFilter', 'tbPurgeCatFilterLabel', 'popPurgeCatFilter', 'chkPurgeCatSelectAll', 'icPurgeCatFilters',
            'chkPurgeOnlyUnused', 'btnPurgeSelectUnused', 'btnPurgeClearChecks', 'icPurgeGroups',
            'tbPurgeStatus', 'btnRefreshPurge', 'btnDeletePurgeSelected',
            'tbHealthBigScore', 'icHealthGroups', 'tbHealthStatus', 'btnSelectHealthElements', 'btnRefreshHealth'
        ]
        for n in names:
            setattr(self, n, self.win.FindName(n))

        self.TitleBar.MouseLeftButtonDown += self.on_title_drag
        self.btnCloseDot.Click += lambda s, e: self.win.Close()
        self.btnMinDot.Click += lambda s, e: setattr(self.win, 'WindowState', WindowState.Minimized)
        self.btnMaxDot.Click += self.on_toggle_max

        for nav in (self.navLinks, self.nav3D, self.navCSV, self.navPin, self.navPurge, self.navHealth):
            nav.Checked += self.on_nav_changed

        # Раздел 1: Связи, оси и уровни
        self.btnRefreshLinks.Click += lambda s, e: (self.refresh_links_view(), self.refresh_health_view())
        self.btnFixLinks.Click += lambda s, e: self.trigger_action('fix_links')
        self.dgLinks.MouseDoubleClick += self.on_link_double_click

        # Раздел 2: Аудит элементов (оранжевая кнопка Обновить элементы + выпадающие мультифильтры с галочкой Выбрать все)
        self.btnRefreshAudit.Click += lambda s, e: self.refresh_audit_data()
        self.chkScanWholeModel.Click += lambda s, e: self.refresh_audit_data()
        self.tbSearchAudit.TextChanged += lambda s, e: self.apply_audit_filters()
        self.btnAuditCatFilter.Click += lambda s, e: setattr(self.popAuditCatFilter, 'IsOpen', bool(self.btnAuditCatFilter.IsChecked))
        self.btnAuditWsFilter.Click += lambda s, e: setattr(self.popAuditWsFilter, 'IsOpen', bool(self.btnAuditWsFilter.IsChecked))
        self.popAuditCatFilter.Closed += lambda s, e: setattr(self.btnAuditCatFilter, 'IsChecked', False)
        self.popAuditWsFilter.Closed += lambda s, e: setattr(self.btnAuditWsFilter, 'IsChecked', False)
        self.chkAuditCatSelectAll.Click += lambda s, e: self.set_all_audit_cat_filters(bool(self.chkAuditCatSelectAll.IsChecked))
        self.chkAuditWsSelectAll.Click += lambda s, e: self.set_all_audit_ws_filters(bool(self.chkAuditWsSelectAll.IsChecked))
        self.btnResetAuditFilters.Click += self.on_reset_audit_filters
        self.chkAuditSelectAll.Click += self.on_audit_check_all
        self.dgAudit.MouseDoubleClick += self.on_audit_double_click
        self.btnIsolateSelected.Click += lambda s, e: self.trigger_action('isolate_audit')
        self.btnResetIsolation.Click += lambda s, e: self.trigger_action('reset_isolation')
        self.btnMoveSelectedWs.Click += lambda s, e: self.trigger_action('move_audit_ws')

        # Раздел 3: CSV (шаблон + выгрузка РН из проекта)
        self.btnDownloadCsvTemplate.Click += self.on_download_csv_template
        self.btnExportProjectWsCsv.Click += self.on_export_project_ws_csv
        self.btnLoadCsvFile.Click += self.on_load_csv_file
        self.btnCreateWsFromCsv.Click += lambda s, e: self.trigger_action('create_ws_csv')

        # Раздел 4: Закрепление
        for chk in (self.chkPinGrids, self.chkPinLevels, self.chkPinLinks, self.chkPinPoints, self.chkPinOnlyUnpinned):
            chk.Click += lambda s, e: self.apply_pin_filters()
        self.chkPinSelectAll.Click += self.on_pin_check_all
        self.btnRefreshPins.Click += lambda s, e: (self.refresh_pin_data(), self.refresh_health_view())
        self.btnPinSelected.Click += lambda s, e: self.trigger_action('set_pin_state', True)
        self.btnUnpinSelected.Click += lambda s, e: self.trigger_action('set_pin_state', False)
        self.dgPins.MouseDoubleClick += self.on_pin_double_click

        # Раздел 5: Очистка модели (выпадающий мультифильтр ВСЕХ категорий с галочкой Выбрать все)
        self.btnPurgeExpandAll.Click += lambda s, e: self.set_all_purge_expanded(True)
        self.btnPurgeCollapseAll.Click += lambda s, e: self.set_all_purge_expanded(False)
        self.tbSearchPurge.TextChanged += lambda s, e: self.apply_purge_filters()
        self.btnPurgeCatFilter.Click += lambda s, e: setattr(self.popPurgeCatFilter, 'IsOpen', bool(self.btnPurgeCatFilter.IsChecked))
        self.popPurgeCatFilter.Closed += lambda s, e: setattr(self.btnPurgeCatFilter, 'IsChecked', False)
        self.chkPurgeCatSelectAll.Click += lambda s, e: self.set_all_purge_cat_filters(bool(self.chkPurgeCatSelectAll.IsChecked))
        self.chkPurgeOnlyUnused.Click += lambda s, e: self.apply_purge_filters()
        self.btnPurgeSelectUnused.Click += self.on_purge_select_unused
        self.btnPurgeClearChecks.Click += self.on_purge_clear_all_checks
        self.btnRefreshPurge.Click += lambda s, e: self.refresh_purge_data()
        self.btnDeletePurgeSelected.Click += lambda s, e: self.trigger_action('delete_purge_items')

        # Привязываем обработчик кнопки «👁 Показать» внутри шаблонов очистки и здоровья
        self.win.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self.on_routed_button_click))

        # Раздел 6: Здоровье модели (раскрывающиеся карточки с чекбоксами)
        self.btnRefreshHealth.Click += lambda s, e: (self.refresh_links_view(), self.refresh_pin_data(), self.refresh_health_view())
        self.btnSelectHealthElements.Click += self.on_select_health_elements

    def on_routed_button_click(self, sender, e):
        src = e.OriginalSource
        if getattr(src, 'Content', None) == u"👁 Показать" and hasattr(src, 'Tag'):
            tag = src.Tag
            if isinstance(tag, DB.ElementId):
                self.show_element_in_revit(tag)
            elif isinstance(tag, list) and len(tag) > 0:
                self.show_multiple_elements_in_revit(tag)

    def show_element_in_revit(self, el_id):
        if el_id is None or el_id == DB.ElementId.InvalidElementId:
            return
        try:
            el = doc.GetElement(el_id)
            if el is None:
                return
            if isinstance(el, DB.View) and not el.IsTemplate:
                uidoc.ActiveView = el
                return
            if hasattr(el, 'ViewSpecific') and el.ViewSpecific and el.OwnerViewId != DB.ElementId.InvalidElementId:
                owner_view = doc.GetElement(el.OwnerViewId)
                if isinstance(owner_view, DB.View):
                    uidoc.ActiveView = owner_view
            uidoc.Selection.SetElementIds(List[DB.ElementId]([el_id]))
            uidoc.ShowElements(el_id)
        except Exception:
            pass

    def show_multiple_elements_in_revit(self, el_ids):
        valid_ids = [eid for eid in el_ids if eid is not None and eid != DB.ElementId.InvalidElementId]
        if not valid_ids:
            return
        try:
            uidoc.Selection.SetElementIds(List[DB.ElementId](valid_ids[:200]))
            uidoc.ShowElements(List[DB.ElementId](valid_ids[:50]))
        except Exception:
            pass

    def trigger_action(self, action_name, payload=None):
        self.ext_handler.action = action_name
        self.ext_handler.payload = payload
        self.ext_event.Raise()

    def on_title_drag(self, sender, e):
        if e.ClickCount == 2:
            self.on_toggle_max(sender, e)
        else:
            try:
                self.win.DragMove()
            except Exception:
                pass

    def on_toggle_max(self, sender, e):
        if self.win.WindowState == WindowState.Maximized:
            self.win.WindowState = WindowState.Normal
        else:
            self.win.WindowState = WindowState.Maximized

    def on_nav_changed(self, sender, e):
        if not hasattr(self, 'pageLinks') or self.pageLinks is None:
            return
        self.pageLinks.Visibility = Visibility.Visible if self.navLinks.IsChecked else Visibility.Collapsed
        self.page3D.Visibility = Visibility.Visible if self.nav3D.IsChecked else Visibility.Collapsed
        self.pageCSV.Visibility = Visibility.Visible if self.navCSV.IsChecked else Visibility.Collapsed
        self.pagePin.Visibility = Visibility.Visible if self.navPin.IsChecked else Visibility.Collapsed
        self.pagePurge.Visibility = Visibility.Visible if self.navPurge.IsChecked else Visibility.Collapsed
        self.pageHealth.Visibility = Visibility.Visible if self.navHealth.IsChecked else Visibility.Collapsed

        if self.nav3D.IsChecked and not self._audit_loaded:
            self.refresh_audit_data()
        elif self.navPin.IsChecked and not self._pin_loaded:
            self.refresh_pin_data()
        elif self.navPurge.IsChecked and not self._purge_loaded:
            self.refresh_purge_data()
        elif self.navHealth.IsChecked:
            self.refresh_health_view()

    def refresh_worksets_cache(self):
        self.worksets = get_user_worksets(doc)
        self.names_by_id, self.open_by_id = ws_info_maps(self.worksets)
        opts = [WorksetOption(w) for w in sorted(self.worksets, key=lambda x: x.Name)]
        self.cmbTargetWs.ItemsSource = opts
        if opts and self.cmbTargetWs.SelectedIndex < 0:
            self.cmbTargetWs.SelectedIndex = 0

    # ----------------------------------------------------------
    # РАЗДЕЛ 1: СВЯЗИ, ОСИ И УРОВНИ
    # ----------------------------------------------------------
    def refresh_links_view(self):
        self.refresh_worksets_cache()
        self.links_state = scan_links(doc, self.worksets)
        self.dgLinks.ItemsSource = self.links_state.rows
        if self.links_state.err_count == 0:
            self.tbLinksStatus.Text = u"Все проверенные связи, оси и уровни находятся в корректных РН"
            self.btnFixLinks.IsEnabled = False
        else:
            self.tbLinksStatus.Text = u"Найдено несоответствий рабочих наборов: {}".format(self.links_state.err_count)
            self.btnFixLinks.IsEnabled = True

    def on_link_double_click(self, sender, e):
        row = self.dgLinks.SelectedItem
        if row and row.RevitId:
            self.show_element_in_revit(row.RevitId)

    # ----------------------------------------------------------
    # РАЗДЕЛ 2: АУДИТ ЭЛЕМЕНТОВ (4 КОЛОНКИ + 2 МУЛЬТИФИЛЬТРА С ГАЛОЧКОЙ ВЫБРАТЬ ВСЕ)
    # ----------------------------------------------------------
    def refresh_audit_data(self):
        self._audit_loaded = True
        self.refresh_worksets_cache()
        whole_model = bool(self.chkScanWholeModel.IsChecked)
        self.all_audit_rows = collect_audit_elements(doc, uidoc, whole_model, self.names_by_id, self.open_by_id)

        cat_counts = {}
        ws_counts = {}
        ws_closed = {}
        for r in self.all_audit_rows:
            cat_counts[r.Category] = cat_counts.get(r.Category, 0) + 1
            ws_counts[r.Workset] = ws_counts.get(r.Workset, 0) + 1
            ws_closed[r.Workset] = not r.IsWsOpen

        self.cat_filter_items = []
        for cname in sorted(cat_counts.keys()):
            fi = FilterItem(cname, True, u"({})".format(cat_counts[cname]))
            fi.add_PropertyChanged(self.on_audit_filter_item_changed)
            self.cat_filter_items.append(fi)
        self.icAuditCatFilters.ItemsSource = self.cat_filter_items
        self.chkAuditCatSelectAll.IsChecked = True

        self.ws_filter_items = []
        for wname in sorted(ws_counts.keys()):
            prefix = u"🔒 " if ws_closed.get(wname, False) else u""
            fi = FilterItem(wname, True, u"{}({})".format(prefix, ws_counts[wname]))
            fi.add_PropertyChanged(self.on_audit_filter_item_changed)
            self.ws_filter_items.append(fi)
        self.icAuditWsFilters.ItemsSource = self.ws_filter_items
        self.chkAuditWsSelectAll.IsChecked = True

        self.apply_audit_filters()

    def on_audit_filter_item_changed(self, sender, args):
        if not self._suppress_filter_events:
            self.apply_audit_filters()

    def set_all_audit_cat_filters(self, state):
        self._suppress_filter_events = True
        try:
            for i in self.cat_filter_items:
                i.set_silent(state)
        finally:
            self._suppress_filter_events = False
        self.apply_audit_filters()

    def set_all_audit_ws_filters(self, state):
        self._suppress_filter_events = True
        try:
            for i in self.ws_filter_items:
                i.set_silent(state)
        finally:
            self._suppress_filter_events = False
        self.apply_audit_filters()

    def on_reset_audit_filters(self, sender, e):
        self._suppress_filter_events = True
        try:
            self.tbSearchAudit.Text = u""
            for i in self.cat_filter_items:
                i.set_silent(True)
            for i in self.ws_filter_items:
                i.set_silent(True)
        finally:
            self._suppress_filter_events = False
        self.apply_audit_filters()

    def update_audit_filter_badges(self):
        total_c = len(self.cat_filter_items)
        sel_c = sum(1 for i in self.cat_filter_items if i.IsChecked)
        self.chkAuditCatSelectAll.IsChecked = (total_c > 0 and sel_c == total_c)
        if sel_c == 0:
            self.tbAuditCatFilterLabel.Text = u"Категории: Не выбрано (0)"
        elif sel_c == total_c:
            self.tbAuditCatFilterLabel.Text = u"Категории: Все ({})".format(total_c)
        elif sel_c == 1:
            one_name = [i.Name for i in self.cat_filter_items if i.IsChecked][0]
            self.tbAuditCatFilterLabel.Text = u"Кат.: {}".format(one_name)
        else:
            self.tbAuditCatFilterLabel.Text = u"Категории: {} из {}".format(sel_c, total_c)

        total_w = len(self.ws_filter_items)
        sel_w = sum(1 for i in self.ws_filter_items if i.IsChecked)
        self.chkAuditWsSelectAll.IsChecked = (total_w > 0 and sel_w == total_w)
        if sel_w == 0:
            self.tbAuditWsFilterLabel.Text = u"РН: Не выбрано (0)"
        elif sel_w == total_w:
            self.tbAuditWsFilterLabel.Text = u"Рабочие наборы: Все ({})".format(total_w)
        elif sel_w == 1:
            one_ws = [i.Name for i in self.ws_filter_items if i.IsChecked][0]
            self.tbAuditWsFilterLabel.Text = u"РН: {}".format(one_ws)
        else:
            self.tbAuditWsFilterLabel.Text = u"Рабочие наборы: {} из {}".format(sel_w, total_w)

    def apply_audit_filters(self):
        self.update_audit_filter_badges()
        active_cats = {i.Name for i in self.cat_filter_items if i.IsChecked}
        active_ws = {i.Name for i in self.ws_filter_items if i.IsChecked}
        q = (self.tbSearchAudit.Text or u"").strip().lower()

        filtered = []
        for r in self.all_audit_rows:
            if r.Category not in active_cats or r.Workset not in active_ws:
                continue
            if q and (q not in r.Category.lower() and q not in r.FamilyType.lower() and q not in r.Workset.lower()):
                continue
            filtered.append(r)

        self.dgAudit.ItemsSource = filtered
        closed_cnt = sum(1 for r in filtered if not r.IsWsOpen)
        self.tbAuditStatus.Text = u"Показано элементов: {} из {} (в закрытых РН 🔒: {})".format(
            len(filtered), len(self.all_audit_rows), closed_cnt
        )

    def on_audit_check_all(self, sender, e):
        val = bool(self.chkAuditSelectAll.IsChecked)
        items = self.dgAudit.ItemsSource or []
        for r in items:
            r.IsChecked = val

    def on_audit_double_click(self, sender, e):
        row = self.dgAudit.SelectedItem
        if row and row.RevitId:
            self.show_element_in_revit(row.RevitId)

    # ----------------------------------------------------------
    # РАЗДЕЛ 3: СОЗДАНИЕ РН ПО CSV + СКАЧАТЬ ШАБЛОН / СПИСОК РН
    # ----------------------------------------------------------
    def on_download_csv_template(self, sender, e):
        path = forms.save_file(file_ext='csv', default_name=u"Шаблон_РН.csv", title=u"Сохранить шаблон рабочих наборов CSV")
        if path:
            try:
                write_utf8_csv(path, DEFAULT_CSV_TEMPLATE)
                self.tbCsvInput.Text = DEFAULT_CSV_TEMPLATE.strip()
                self.tbCsvStatus.Text = u"Шаблон РН успешно сохранен: {}".format(os.path.basename(path))
            except Exception as ex:
                forms.alert(u"Не удалось сохранить файл:\n{}".format(ex))

    def on_export_project_ws_csv(self, sender, e):
        self.refresh_worksets_cache()
        lines = [u"Рабочий набор;Ключевое слово связи"]
        for ws in sorted(self.worksets, key=lambda x: x.Name):
            kw = u""
            if LINK_WS_MARKER in ws.Name.lower() or ws.Name in CUSTOM_RULES:
                kw = extract_keyword(ws.Name) or u""
            lines.append(u"{};{}".format(ws.Name, kw))
        csv_text = u"\n".join(lines) + u"\n"

        default_fn = u"РН_{}.csv".format(re.sub(r'[\\/:*?"<>|]', u'_', doc.Title or u"Проект"))
        path = forms.save_file(file_ext='csv', default_name=default_fn, title=u"Сохранить список РН из проекта")
        if path:
            try:
                write_utf8_csv(path, csv_text)
                self.tbCsvInput.Text = csv_text.strip()
                self.tbCsvStatus.Text = u"Выгружено {} РН из проекта в файл: {}".format(len(self.worksets), os.path.basename(path))
            except Exception as ex:
                forms.alert(u"Не удалось сохранить файл:\n{}".format(ex))

    def on_load_csv_file(self, sender, e):
        path = forms.pick_file(file_ext='csv', title=u"Выберите CSV файл рабочих наборов")
        if path and os.path.exists(path):
            self.tbCsvInput.Text = read_text_file(path)
            self.tbCsvStatus.Text = u"Загружен файл: {}".format(os.path.basename(path))

    # ----------------------------------------------------------
    # РАЗДЕЛ 4: ЗАКРЕПЛЕНИЕ
    # ----------------------------------------------------------
    def refresh_pin_data(self):
        self._pin_loaded = True
        self.refresh_worksets_cache()
        self.all_pin_rows = collect_pin_rows(doc, self.names_by_id)
        self.apply_pin_filters()

    def apply_pin_filters(self):
        allowed = set()
        if self.chkPinGrids.IsChecked:
            allowed.add(u"Ось")
        if self.chkPinLevels.IsChecked:
            allowed.add(u"Уровень")
        if self.chkPinLinks.IsChecked:
            allowed.add(u"Связь RVT")
        if self.chkPinPoints.IsChecked:
            allowed.add(u"Базовая точка")
            allowed.add(u"Точка съемки")

        only_unpinned = bool(self.chkPinOnlyUnpinned.IsChecked)
        filtered = [
            r for r in self.all_pin_rows
            if r.Category in allowed and (not only_unpinned or not r.IsPinned)
        ]
        self.dgPins.ItemsSource = filtered
        unpinned_cnt = sum(1 for r in filtered if not r.IsPinned)
        self.tbPinStatus.Text = u"Всего в списке: {} | Не закреплено: {}".format(len(filtered), unpinned_cnt)

    def on_pin_check_all(self, sender, e):
        val = bool(self.chkPinSelectAll.IsChecked)
        for r in (self.dgPins.ItemsSource or []):
            r.IsChecked = val

    def on_pin_double_click(self, sender, e):
        row = self.dgPins.SelectedItem
        if row and row.RevitId:
            self.show_element_in_revit(row.RevitId)

    # ----------------------------------------------------------
    # РАЗДЕЛ 5: ОЧИСТКА МОДЕЛИ (МУЛЬТИФИЛЬТР ВСЕХ КАТЕГОРИЙ С ЧЕКБОКСАМИ)
    # ----------------------------------------------------------
    def refresh_purge_data(self):
        self._purge_loaded = True
        self.refresh_worksets_cache()
        self.purge_groups = collect_purge_groups(doc, self.names_by_id)
        for g in self.purge_groups:
            g.OnGroupSelectionChanged = self.update_purge_status_text

        # Собираем ВСЕ категории (и основные разделы DWG / PDF / Разрезы / Группы, и каждую категорию семейств)
        cat_counts = {}
        cat_order = []
        for g in self.purge_groups:
            if g.UseSubGroups:
                for sg in g._all_subgroups:
                    cname = sg.CategoryName
                    if cname not in cat_counts:
                        cat_order.append(cname)
                        cat_counts[cname] = 0
                    cat_counts[cname] += len(sg._all_items)
            else:
                cname = g.Title.split(u". ", 1)[-1] if u". " in g.Title else g.Title
                if g._all_items:
                    cname = g._all_items[0].CategoryLabel
                else:
                    cname = {
                        'dwg': u"Импорты САПР (DWG)",
                        'pdf_img': u"Изображения и PDF",
                        'sections': u"Разрезы (не на листах)",
                        'groups': u"Группы и фильтры видов"
                    }.get(g.Key, g.Title)
                if cname not in cat_counts:
                    cat_order.append(cname)
                    cat_counts[cname] = 0
                cat_counts[cname] += len(g._all_items)

        self.purge_cat_filter_items = []
        for cname in cat_order:
            fi = FilterItem(cname, True, u"({})".format(cat_counts[cname]))
            fi.add_PropertyChanged(self.on_purge_cat_filter_changed)
            self.purge_cat_filter_items.append(fi)

        self.icPurgeCatFilters.ItemsSource = self.purge_cat_filter_items
        self.chkPurgeCatSelectAll.IsChecked = True
        self.apply_purge_filters()

    def on_purge_cat_filter_changed(self, sender, args):
        if not self._suppress_filter_events:
            self.apply_purge_filters()

    def set_all_purge_cat_filters(self, state):
        self._suppress_filter_events = True
        try:
            for i in self.purge_cat_filter_items:
                i.set_silent(state)
        finally:
            self._suppress_filter_events = False
        self.apply_purge_filters()

    def update_purge_filter_badge(self):
        total = len(self.purge_cat_filter_items)
        sel = sum(1 for i in self.purge_cat_filter_items if i.IsChecked)
        self.chkPurgeCatSelectAll.IsChecked = (total > 0 and sel == total)
        if sel == 0:
            self.tbPurgeCatFilterLabel.Text = u"Категории: Не выбрано (0)"
        elif sel == total:
            self.tbPurgeCatFilterLabel.Text = u"Категории: Все ({})".format(total)
        elif sel == 1:
            one_name = [i.Name for i in self.purge_cat_filter_items if i.IsChecked][0]
            self.tbPurgeCatFilterLabel.Text = one_name
        else:
            self.tbPurgeCatFilterLabel.Text = u"Категории: {} из {}".format(sel, total)

    def apply_purge_filters(self):
        if not self._purge_loaded or self._suppress_filter_events:
            return
        self.update_purge_filter_badge()
        q = self.tbSearchPurge.Text or u""
        only_unused = bool(self.chkPurgeOnlyUnused.IsChecked)

        active_cats = {i.Name for i in self.purge_cat_filter_items if i.IsChecked}
        is_all_selected = (len(active_cats) == len(self.purge_cat_filter_items))

        default_cat_by_key = {
            'dwg': u"Импорты САПР (DWG)",
            'pdf_img': u"Изображения и PDF",
            'sections': u"Разрезы (не на листах)",
            'groups': u"Группы и фильтры видов"
        }

        visible_groups = []
        for g in self.purge_groups:
            if g.UseSubGroups:
                g.apply_filter(q, only_unused, allowed_categories=active_cats)
                if g.VisibleCount > 0 or (is_all_selected and any(c.startswith(u"Семейства:") for c in active_cats)):
                    if not is_all_selected or q.strip():
                        g.IsExpanded = True
                        for sg in g.SubGroups:
                            sg.IsExpanded = True
                    visible_groups.append(g)
            else:
                g_cat = default_cat_by_key.get(g.Key, g.Title)
                if g_cat not in active_cats:
                    continue
                g.apply_filter(q, only_unused, allowed_categories=active_cats)
                if not is_all_selected or q.strip():
                    g.IsExpanded = True
                visible_groups.append(g)

        self.icPurgeGroups.ItemsSource = visible_groups
        self.update_purge_status_text()

    def set_all_purge_expanded(self, state):
        st = bool(state)
        for g in self.purge_groups:
            g.IsExpanded = st
            if g.UseSubGroups:
                for sg in g.SubGroups:
                    sg.IsExpanded = st

    def on_purge_select_unused(self, sender, e):
        for g in self.purge_groups:
            for it in g._all_items:
                if g.Key in ('dwg', 'sections'):
                    it.set_checked_silent(False)
                else:
                    it.set_checked_silent(it.Kind != 'ok')
            g.on_child_checked_changed()
        self.update_purge_status_text()

    def on_purge_clear_all_checks(self, sender, e):
        for g in self.purge_groups:
            for it in g._all_items:
                it.set_checked_silent(False)
            g.on_child_checked_changed()
        self.update_purge_status_text()

    def update_purge_status_text(self):
        total_items = sum(g.TotalCount for g in self.purge_groups)
        checked_items = sum(g.CheckedCount for g in self.purge_groups)
        self.tbPurgeStatus.Text = u"Всего найдено элементов по 5 разделам: {} | Отмечено к удалению: {}".format(
            total_items, checked_items
        )
        self.btnDeletePurgeSelected.IsEnabled = (checked_items > 0)

    # ----------------------------------------------------------
    # РАЗДЕЛ 6: ЗДОРОВЬЕ МОДЕЛИ (РАСКРЫВАЮЩИЕСЯ КАРТОЧКИ ПРОБЛЕМ)
    # ----------------------------------------------------------
    def refresh_health_view(self):
        self._health_loaded = True
        if not self._pin_loaded:
            self.all_pin_rows = collect_pin_rows(doc, self.names_by_id)
            self._pin_loaded = True
            self.apply_pin_filters()

        score, cards = evaluate_model_health(doc, self.links_state, self.all_pin_rows, self.names_by_id)
        for c in cards:
            c.OnSelectionChanged = self.update_health_status_text
        self.health_cards = cards
        self.icHealthGroups.ItemsSource = cards

        score_str = u"{}%".format(score)
        self.tbSidebarScore.Text = score_str
        self.tbHealthBigScore.Text = score_str
        self.update_health_status_text()

    def update_health_status_text(self):
        checked_issues = sum(c.CheckedCount for c in self.health_cards)
        total_issues = sum(c.Count for c in self.health_cards)
        self.tbHealthStatus.Text = u"Всего замечаний по 4 показателям: {} | Отмечено галочками для выделения: {}".format(
            total_issues, checked_issues
        )

    def on_select_health_elements(self, sender, e):
        selected_ids = []
        seen = set()
        for c in self.health_cards:
            for it in c._all_items:
                if it.IsChecked:
                    for eid in it.ElementIds:
                        val = id_int(eid)
                        if val not in seen:
                            seen.add(val)
                            selected_ids.append(eid)

        if not selected_ids:
            forms.alert(u"Раскройте нужный показатель здоровья и отметьте галочками предупреждения или элементы, которые нужно выделить в модели.")
            return
        self.show_multiple_elements_in_revit(selected_ids)

    # ----------------------------------------------------------
    # ВЫПОЛНЕНИЕ ТРАНЗАКЦИЙ В КОНТЕКСТЕ REVIT API
    # ----------------------------------------------------------
    def run_revit_action(self, action, payload):
        if action == 'fix_links':
            fixed, failed = fix_links_and_levels(doc, self.links_state)
            self.refresh_links_view()
            self.refresh_health_view()
            forms.alert(u"Перенесено элементов: {}\nНе удалось перенести: {}".format(fixed, failed))

        elif action == 'isolate_audit':
            items = [r.RevitId for r in (self.dgAudit.ItemsSource or []) if r.IsChecked]
            if not items and self.dgAudit.SelectedItem:
                items = [self.dgAudit.SelectedItem.RevitId]
            if not items:
                forms.alert(u"Отметьте элементы галочками для изоляции на 3D-виде.")
                return
            v3d = ensure_active_3d_view(uidoc, doc)
            if v3d is None:
                forms.alert(u"Не найден 3D-вид для изоляции элементов.")
                return
            t = DB.Transaction(doc, u"Изоляция элементов на 3D")
            t.Start()
            try:
                v3d.IsolateElementsTemporary(List[DB.ElementId](items))
                t.Commit()
                uidoc.Selection.SetElementIds(List[DB.ElementId](items))
            except Exception:
                t.RollBack()
                raise

        elif action == 'reset_isolation':
            v3d = ensure_active_3d_view(uidoc, doc)
            if v3d is not None:
                t = DB.Transaction(doc, u"Сброс временной изоляции")
                t.Start()
                try:
                    v3d.DisableTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate)
                    t.Commit()
                except Exception:
                    t.RollBack()

        elif action == 'move_audit_ws':
            target_opt = self.cmbTargetWs.SelectedItem
            if target_opt is None:
                forms.alert(u"Выберите целевой рабочий набор в списке.")
                return
            rows = [r for r in (self.dgAudit.ItemsSource or []) if r.IsChecked]
            if not rows:
                forms.alert(u"Отметьте галочками элементы для переноса.")
                return
            moved, failed = 0, 0
            t = DB.Transaction(doc, u"Перенос элементов в РН")
            t.Start()
            try:
                for r in rows:
                    el = doc.GetElement(r.RevitId)
                    if el is not None and set_element_workset(el, target_opt.IdInt):
                        moved += 1
                    else:
                        failed += 1
                t.Commit()
            except Exception:
                t.RollBack()
                raise
            self.refresh_audit_data()
            forms.alert(u"Успешно перенесено в «{}»: {}\nНедоступно для изменения: {}".format(
                target_opt.Name, moved, failed))

        elif action == 'create_ws_csv':
            rows = parse_rules_csv(self.tbCsvInput.Text or u"")
            if not rows:
                forms.alert(u"Введите список рабочих наборов или загрузите CSV-файл.")
                return
            existing = {norm_name(w.Name) for w in get_user_worksets(doc)}
            created = 0
            t = DB.Transaction(doc, u"Создание РН по шаблону CSV")
            t.Start()
            try:
                for ws_name, kw in rows:
                    if any(ch in ws_name for ch in BAD_WS_CHARS):
                        continue
                    if norm_name(ws_name) not in existing and DB.WorksetTable.IsWorksetNameUnique(doc, ws_name):
                        DB.Workset.Create(doc, ws_name)
                        existing.add(norm_name(ws_name))
                        created += 1
                    if kw:
                        CUSTOM_RULES[ws_name] = kw
                t.Commit()
            except Exception:
                t.RollBack()
                raise
            self.refresh_links_view()
            self.refresh_health_view()
            self.tbCsvStatus.Text = u"Создано новых рабочих наборов: {}".format(created)

        elif action == 'set_pin_state':
            pin_val = bool(payload)
            rows = [r for r in (self.dgPins.ItemsSource or []) if r.IsChecked]
            if not rows:
                forms.alert(u"Отметьте элементы галочками.")
                return
            changed = 0
            t = DB.Transaction(doc, u"Изменение закрепления элементов")
            t.Start()
            try:
                for r in rows:
                    el = doc.GetElement(r.RevitId)
                    if el is not None:
                        try:
                            el.Pinned = pin_val
                            changed += 1
                        except Exception:
                            pass
                t.Commit()
            except Exception:
                t.RollBack()
                raise
            self.refresh_pin_data()
            self.refresh_health_view()
            self.tbPinStatus.Text = u"Обновлено состояние закрепления для {} элементов".format(changed)

        elif action == 'delete_purge_items':
            rows_to_delete = []
            for g in self.purge_groups:
                for it in g._all_items:
                    if it.IsChecked:
                        rows_to_delete.append(it)
            if not rows_to_delete:
                forms.alert(u"Отметьте галочками элементы для удаления.")
                return

            deleted, failed = 0, 0
            t = DB.Transaction(doc, u"Очистка выбранных элементов модели")
            t.Start()
            try:
                for r in rows_to_delete:
                    try:
                        doc.Delete(r.RevitId)
                        deleted += 1
                    except Exception:
                        failed += 1
                t.Commit()
            except Exception:
                t.RollBack()
                raise

            self.refresh_purge_data()
            self.refresh_health_view()
            forms.alert(u"Удалено выбранных элементов: {}\nЗащищено системой / не удалено: {}".format(deleted, failed))

    def show(self):
        self.win.Show()


# ==========================================================
# 7. ЗАПУСК ПЛАГИНА
# ==========================================================
ui = AuditWindow()
ui.show()

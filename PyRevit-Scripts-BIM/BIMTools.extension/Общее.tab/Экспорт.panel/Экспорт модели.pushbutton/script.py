# -*- coding: utf-8 -*-
"""Пакетный экспорт моделей Revit в NWC и/или IFC.
Режимы: только NWC, только IFC или оба формата сразу. Папки и имя 3D-вида можно задать один раз для всех моделей,
имя файла подставляется из имени RVT («2992_АР.rvt» → «2992_АР»), его можно изменить. Раздел «Настройки экспорта» повторяет окна
экспорта NWC и IFC в Revit; настройки сохраняются в файл и загружаются из него.
Модели открываются в фоне (совместные — с отсоединением от центральной), вид для экспорта ищется по имени
или создаётся временно. Каждый запуск пишет журнал. Режим «Позже» ждёт заданного времени (Revit в это время занят).
Оформление — в стиле окна «Аудит модели» (оранжевая палитра, левое меню по вкладкам).
"""
__title__ = u"Экспорт\nмодели"
__author__ = u"Daniil Antonenko"

import clr
import csv
import io
import os
import re
import json
import datetime
import time

clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')
clr.AddReference('System.Windows.Forms')

import System
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Globalization import CultureInfo
from System.Windows import (Visibility, WindowState, RoutedEventHandler, Thickness, FontWeights,
                            HorizontalAlignment, VerticalAlignment, GridLength)
from System.Windows.Controls import CheckBox, ComboBox, TextBlock, TextBox, Button, Grid, ColumnDefinition, StackPanel, Orientation
from System.Windows.Controls.Primitives import ButtonBase
from System.Windows.Markup import XamlReader
from System.Windows.Interop import WindowInteropHelper
from System.Diagnostics import Process
from Microsoft.Win32 import OpenFileDialog, SaveFileDialog
from System.Windows.Forms import FolderBrowserDialog, DialogResult
from pyrevit import revit, DB, forms

app = revit.doc.Application

# ==========================================================
# 1. КОНСТАНТЫ, НАСТРОЙКИ ФОРМАТОВ И МОДЕЛЬ ДАННЫХ
# ==========================================================
# Имя файла пресетов прежнее, чтобы сохранённые раньше пресеты NWC продолжали открываться
PRESET_FILE = os.path.join(os.getenv('APPDATA'), 'BimTools_NwcPresets.json')
LOG_DIR = os.path.join(os.getenv('APPDATA'), 'BimTools_ExportLogs')

MODE_NWC = 'nwc'
MODE_IFC = 'ifc'
MODE_BOTH = 'both'
MODE_TITLES = {MODE_NWC: u"NWC", MODE_IFC: u"IFC", MODE_BOTH: u"NWC + IFC"}
DEFAULT_VIEW_NAME = u"Navisworks"


def _group(label):
    return (None, 'group', label, None, None)


def _bool(key, label, default=False):
    return (key, 'bool', label, default, None)


def _choice(key, label, default, choices):
    return (key, 'choice', label, default, choices)


def _number(key, label, default):
    return (key, 'number', label, default, None)


def _file(key, label):
    return (key, 'file', label, u"", None)


# Настройки NWC — как в окне «Параметры экспорта Navisworks» в Revit (ключ = свойство NavisworksExportOptions)
NWC_FIELDS = [
    _group(u"Экспорт"),
    _bool('ExportLinks', u"Преобразовать связанные файлы"),
    _bool('DivideFileIntoLevels', u"Разделить файл на уровни", True),
    _bool('ExportRoomGeometry', u"Экспорт геометрии помещения"),
    _group(u"Преобразование"),
    _bool('ExportParts', u"Преобразовать элементы-части"),
    _bool('ExportElementIds', u"Преобразовать идентификаторы элементов", True),
    _choice('Parameters', u"Преобразовать параметры элементов", 'All',
            [(u"Нет", 'None'), (u"Элементы", 'Elements'), (u"Все", 'All')]),
    _bool('ConvertElementProperties', u"Преобразовать свойства элементов"),
    _bool('ConvertLinkedCADFormats', u"Преобразовать связанные файлы САПР", True),
    _bool('ConvertLights', u"Преобразовать источники света"),
    _bool('ExportUrls', u"Преобразовать URL-адреса", True),
    _bool('ExportRoomAsAttribute', u"Преобразовать помещение как атрибут", True),
    _bool('FindMissingMaterials', u"Пытаться найти отсутствующие материалы", True),
    _group(u"Координаты и геометрия"),
    _choice('Coordinates', u"Координаты", 'Shared', [(u"Общие", 'Shared'), (u"Внутренние", 'Internal')]),
    _number('FacetingFactor', u"Коэффициент фасетизации", 1.0),
]

# Настройки IFC — как во вкладках окна «Изменить настройку» экспорта IFC в Revit.
# Ключи вне IFC_SPECIAL передаются в IFC-экспортёр через AddOption (имена опций модуля revit-ifc).
IFC_FIELDS = [
    _group(u"Общие"),
    _choice('Version', u"Версия IFC", 'IFC2x3CV2',
            [(u"IFC2x3 Coordination View 2.0", 'IFC2x3CV2'), (u"IFC2x3 Coordination View", 'IFC2x3'),
             (u"IFC4 Reference View", 'IFC4RV'), (u"IFC4 Design Transfer View", 'IFC4DTV'), (u"IFC4", 'IFC4')]),
    _choice('SpaceBoundaries', u"Границы помещений", 0, [(u"Нет", 0), (u"1-й уровень", 1), (u"2-й уровень", 2)]),
    _choice('SitePlacement', u"Координатная основа", 0,
            [(u"Общие координаты", 0), (u"Точка съёмки", 1), (u"Базовая точка проекта", 2), (u"Внутренние координаты", 3)]),
    _bool('WallAndColumnSplitting', u"Разделять стены, колонны и воздуховоды по уровням"),
    _group(u"Дополнительное содержимое"),
    _bool('Export2DElements', u"Экспортировать 2D-элементы видов в плане"),
    _bool('ExportLinkedFiles', u"Экспортировать связанные файлы как отдельные IFC"),
    _bool('VisibleElementsOfCurrentView', u"Экспортировать только элементы, видимые на 3D-виде", True),
    _bool('ExportRoomsInView', u"Экспортировать помещения в 3D-видах"),
    _group(u"Наборы свойств"),
    _bool('ExportInternalRevitPropertySets', u"Экспортировать наборы свойств Revit"),
    _bool('ExportIFCCommonPropertySets', u"Экспортировать общие наборы свойств IFC", True),
    _bool('ExportBaseQuantities', u"Экспортировать базовые количества"),
    _bool('ExportSchedulesAsPsets', u"Экспортировать спецификации как наборы свойств"),
    _file('ExportUserDefinedPsetsFileName', u"Пользовательские наборы свойств (файл .txt)"),
    _file('ExportUserDefinedParameterMappingFileName', u"Таблица сопоставления параметров (файл .txt)"),
    _file('FamilyMappingFile', u"Сопоставление категорий IFC (файл .txt)"),
    _group(u"Уровень детализации"),
    _choice('TessellationLevelOfDetail', u"Детализация геометрии", 0.5,
            [(u"Очень низкая", 0.25), (u"Низкая", 0.5), (u"Средняя", 0.75), (u"Высокая", 1.0)]),
    _group(u"Расширенные"),
    _bool('ExportPartsAsBuildingElements', u"Экспортировать части как элементы здания"),
    _bool('ExportSolidModelRep', u"Разрешить смешанное представление «Тело»"),
    _bool('UseActiveViewGeometry', u"Использовать вид при создании геометрии"),
    _bool('UseFamilyAndTypeNameForReference', u"Использовать имя семейства и типа для ссылки"),
    _bool('Use2DRoomBoundaryForVolume', u"Использовать 2D-границы помещений для объёма"),
    _bool('IncludeSiteElevation', u"Включить отметку IfcSite в локальное размещение"),
    _bool('ExportBoundingBox', u"Экспортировать габаритный контейнер"),
    _bool('UseOnlyTriangulation', u"Сохранять тесселированную геометрию как триангуляцию"),
    _bool('UseTypeNameOnlyForIfcType', u"Использовать только имя типа для IfcType"),
    _bool('UseVisibleRevitNameAsEntityName', u"Использовать видимое имя Revit как имя IFCEntity"),
]
# Файловые опции IFC: путь к файлу -> флаг, который включает опцию
IFC_FILE_OPTIONS = {
    'ExportUserDefinedPsetsFileName': 'ExportUserDefinedPsets',
    'ExportUserDefinedParameterMappingFileName': 'ExportUserDefinedParameterMapping',
}
IFC_SPECIAL = set(['Version', 'SpaceBoundaries', 'WallAndColumnSplitting', 'ExportBaseQuantities',
                   'FamilyMappingFile', 'VisibleElementsOfCurrentView']) | set(IFC_FILE_OPTIONS.keys())


class ExportError(Exception):
    pass


class Option(object):
    """Пункт выпадающего списка (PillComboBox показывает свойство Name)."""
    def __init__(self, name, value):
        self.Name = name
        self.Value = value

    def ToString(self):
        return self.Name

    def __str__(self):
        return self.Name


class FormatSettings(object):
    """Значения настроек формата по описанию полей (NWC_FIELDS / IFC_FIELDS)."""
    def __init__(self, title, fields, defaults=None):
        self.title = title
        self.fields = [f for f in fields if f[1] != 'group']
        self.defaults = dict((f[0], f[3]) for f in self.fields)
        self.defaults.update(defaults or {})
        self.values = dict(self.defaults)

    def get(self, key):
        return self.values.get(key, self.defaults.get(key))

    def reset(self):
        self.values = dict(self.defaults)

    def to_dict(self):
        return dict(self.values)

    def load(self, data):
        """Сбрасывает к значениям по умолчанию и применяет сохранённые (неизвестные ключи игнорируются)."""
        self.reset()
        for key, kind, _, _, _ in self.fields:
            if key not in data:
                continue
            val = data[key]
            try:
                if kind == 'bool':
                    val = bool(val)
                elif kind == 'number':
                    val = float(val)
                elif kind == 'file':
                    val = val or u""
            except Exception:
                continue
            self.values[key] = val


def revit_nwc_defaults():
    """Значения по умолчанию берём у самого Revit — как в его окне экспорта NWC."""
    out = {}
    try:
        opts = DB.NavisworksExportOptions()
    except Exception:
        return out
    for key, kind, _, _, _ in NWC_FIELDS:
        if key is None:
            continue
        try:
            val = getattr(opts, key)
            out[key] = str(val) if kind == 'choice' else (float(val) if kind == 'number' else bool(val))
        except Exception:
            pass
    return out


def option_text(value):
    """Значение опции IFC строкой; дробные числа — с точкой независимо от языка Windows."""
    if isinstance(value, float):
        return System.Convert.ToString(value, CultureInfo.InvariantCulture)
    return u"{}".format(value)


def apply_nwc_settings(settings, opts):
    """Переносит настройки в NavisworksExportOptions. Возвращает ключи, которых нет в этой версии API."""
    skipped = []
    for key, kind, _, _, _ in settings.fields:
        val = settings.get(key)
        try:
            if key == 'Coordinates':
                opts.Coordinates = getattr(DB.NavisworksCoordinates, val)
            elif key == 'Parameters':
                opts.Parameters = getattr(DB.NavisworksParameters, val)
            elif kind == 'number':
                setattr(opts, key, float(val))
            else:
                setattr(opts, key, bool(val))
        except Exception:
            skipped.append(key)
    return skipped


def apply_ifc_settings(settings, opts, view):
    opts.FileVersion = getattr(DB.IFCVersion, settings.get('Version'))
    opts.SpaceBoundaryLevel = int(settings.get('SpaceBoundaries'))
    opts.WallAndColumnSplitting = bool(settings.get('WallAndColumnSplitting'))
    opts.ExportBaseQuantities = bool(settings.get('ExportBaseQuantities'))
    if settings.get('FamilyMappingFile'):
        opts.FamilyMappingFile = settings.get('FamilyMappingFile')
    if settings.get('VisibleElementsOfCurrentView') and view is not None:
        opts.FilterViewId = view.Id
        opts.AddOption("VisibleElementsOfCurrentView", "true")
    for file_key, flag_key in IFC_FILE_OPTIONS.items():
        path = settings.get(file_key)
        opts.AddOption(flag_key, "true" if path else "false")
        if path:
            opts.AddOption(file_key, path)
    for key, kind, _, _, _ in settings.fields:
        if key in IFC_SPECIAL:
            continue
        val = settings.get(key)
        opts.AddOption(key, ("true" if val else "false") if kind == 'bool' else option_text(val))


class RunSettings(object):
    def __init__(self):
        self.Mode = MODE_NWC
        self.RunLater = False
        self.TargetTime = u"23:00"
        self.WorksetKeyword = u"связь"
        self.CreateView = True
        # Общие для всех моделей папки и имя 3D-вида
        self.SameNwcFolder = True
        self.NwcFolder = u""
        self.SameIfcFolder = True
        self.IfcFolder = u""
        self.SameView = True
        self.ViewName = DEFAULT_VIEW_NAME


nwc_settings = FormatSettings(u"NWC", NWC_FIELDS, revit_nwc_defaults())
ifc_settings = FormatSettings(u"IFC", IFC_FIELDS)
run_settings = RunSettings()


def auto_file_name(rvt_path):
    """Имя файла экспорта по умолчанию — имя RVT без расширения: «2992_АР.rvt» → «2992_АР»."""
    return os.path.splitext(os.path.basename(rvt_path or u""))[0].strip()


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


def _notify_property(name):
    attr = '_' + name

    def fget(self):
        return getattr(self, attr)

    def fset(self, value):
        if getattr(self, attr) != value:
            setattr(self, attr, value)
            self.raise_changed(name)
    return property(fget, fset)


class ExportTask(NotifyBase):
    NwcFolder = _notify_property('NwcFolder')
    IfcFolder = _notify_property('IfcFolder')
    ViewName = _notify_property('ViewName')
    NwcVis = _notify_property('NwcVis')
    IfcVis = _notify_property('IfcVis')
    ViewVis = _notify_property('ViewVis')

    def __init__(self, rvt_path=u"", file_name=u"", nwc_folder=u"", ifc_folder=u"", view_name=u""):
        NotifyBase.__init__(self)
        self._RvtPath = u""
        self._FileName = u""
        self._auto_name = True  # имя файла подставляется само, пока пользователь его не изменит
        self._NwcFolder = nwc_folder or u""
        self._IfcFolder = ifc_folder or u""
        self._ViewName = view_name or DEFAULT_VIEW_NAME
        self._NwcVis = Visibility.Collapsed
        self._IfcVis = Visibility.Collapsed
        self._ViewVis = Visibility.Collapsed
        self.RvtPath = rvt_path or u""
        if file_name:
            self.FileName = file_name

    @property
    def RvtPath(self):
        return self._RvtPath

    @RvtPath.setter
    def RvtPath(self, value):
        value = value or u""
        if value == self._RvtPath:
            return
        self._RvtPath = value
        self.raise_changed('RvtPath')
        self.refresh_auto_name()

    @property
    def FileName(self):
        return self._FileName

    @FileName.setter
    def FileName(self, value):
        value = (value or u"").strip()
        auto = auto_file_name(self._RvtPath)
        # Пустое поле или совпадение с автоименем — снова автоматический режим
        self._auto_name = not value or value == auto
        value = value or auto
        if value != self._FileName:
            self._FileName = value
        self.raise_changed('FileName')

    def refresh_auto_name(self):
        """Пересчитывает имя файла после смены RVT, если его не меняли вручную."""
        if self._auto_name:
            auto = auto_file_name(self._RvtPath)
            if auto != self._FileName:
                self._FileName = auto
                self.raise_changed('FileName')

    def is_empty(self):
        return not (self.RvtPath or self.NwcFolder or self.IfcFolder)

    def to_dict(self):
        return {"RvtPath": self.RvtPath, "FileName": self.FileName, "NwcFolder": self.NwcFolder,
                "IfcFolder": self.IfcFolder, "ViewName": self.ViewName}


# Итоговые значения для модели с учётом галочек «одна папка / одно имя вида для всех»
def task_nwc_folder(task):
    return run_settings.NwcFolder if run_settings.SameNwcFolder else task.NwcFolder


def task_ifc_folder(task):
    return run_settings.IfcFolder if run_settings.SameIfcFolder else task.IfcFolder


def task_view_name(task):
    return (run_settings.ViewName if run_settings.SameView else task.ViewName or u"").strip()


def task_file_name(task):
    return (task.FileName or u"").strip() or auto_file_name(task.RvtPath)


def load_presets():
    if os.path.exists(PRESET_FILE):
        try:
            with open(PRESET_FILE, 'rb') as f:
                return json.loads(f.read().decode('utf-8'))
        except Exception:
            pass
    return {}


def save_presets(data):
    with open(PRESET_FILE, 'wb') as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=4).encode('utf-8'))


def mode_has_nwc(mode):
    return mode in (MODE_NWC, MODE_BOTH)


def mode_has_ifc(mode):
    return mode in (MODE_IFC, MODE_BOTH)


def pick_folder(description):
    dialog = FolderBrowserDialog()
    dialog.Description = description
    if dialog.ShowDialog() == DialogResult.OK:
        return dialog.SelectedPath
    return None


# ==========================================================
# 2. ДИАЛОГИ В СТИЛЕ ОКНА АУДИТА (вместо стандартных TaskDialog)
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

# Окно экспорта — владелец диалогов (задаётся при его создании), чтобы диалог был поверх и по центру
dialog_owner = None


def _run_dialog(text, kind, title, details, ask, limit, input_default, details_header):
    xaml = XAML_DIALOG
    for token, color in DIALOG_PALETTES.get(kind, DIALOG_PALETTES['warn']).items():
        xaml = xaml.replace(token, color)
    dlg = XamlReader.Parse(xaml)
    result = [False]

    tb_title = dlg.FindName('tbTitle')
    tb_title.Text = title or (u"Подтверждение" if ask else (u"Внимание" if kind == 'warn' else u"Экспорт модели"))
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


def ask_text(text, default=u"", title=None):
    """Диалог ввода строки. Возвращает введённый текст или None при отмене."""
    ok, value = _run_dialog(text, 'info', title or u"Экспорт модели", None, True, 0, default or u"", None)
    value = (value or u"").strip()
    return value if ok and value else None


# ==========================================================
# 3. XAML — ОБЩИЕ РЕСУРСЫ (СТИЛИ ОКНА АУДИТА) И ГЛАВНОЕ ОКНО
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

XAML_MAIN = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Экспорт модели" Width="1180" Height="820" MinWidth="980" MinHeight="660"
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
                            <TextBlock Grid.Row="0" Text="ЭКСПОРТ МОДЕЛИ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navModels" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Модели и папки"/>
                                <RadioButton Name="navSettings" GroupName="nav" Style="{StaticResource NavItem}" Content="Настройки экспорта"/>
                                <RadioButton Name="navRun" GroupName="nav" Style="{StaticResource NavItem}" Content="Запуск и расписание"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="ФОРМАТ ЭКСПОРТА" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                    <TextBlock Name="tbSidebarMode" Text="NWC" FontSize="24" FontWeight="Bold" Foreground="#FFFF6A00" Margin="0,2,0,0"/>
                                    <TextBlock Name="tbSidebarCount" Text="Моделей: 0" FontSize="11" Foreground="#FF8C7A6B"/>
                                </StackPanel>
                            </Border>
                        </Grid>
                    </Border>

                    <!-- ПРАВАЯ РАБОЧАЯ ОБЛАСТЬ -->
                    <Grid Grid.Column="2">
                        <Grid.RowDefinitions>
                            <RowDefinition Height="*"/>
                            <RowDefinition Height="Auto"/>
                        </Grid.RowDefinitions>

                        <!-- ========================================== -->
                        <!-- РАЗДЕЛ 1: МОДЕЛИ И ПАПКИ                  -->
                        <!-- ========================================== -->
                        <Grid Name="pageModels" Grid.Row="0" Visibility="Visible">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <Grid Grid.Row="0" Margin="4,0,4,12">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <StackPanel Grid.Column="0">
                                    <TextBlock Text="Модели для экспорта" FontSize="26" FontWeight="Bold"/>
                                    <TextBlock Text="Формат, общие папки и список моделей. Имя файла подставляется из имени RVT" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
                                </StackPanel>
                                <ComboBox Name="cmbPresets" Grid.Column="1" Style="{StaticResource PillComboBox}" Tag="Пресет:" Width="240" Margin="12,0,8,0" VerticalAlignment="Center"/>
                                <Button Name="btnSavePreset" Grid.Column="2" Content="Сохранить" Style="{StaticResource GlassButton}" Margin="0,0,8,0" VerticalAlignment="Center"/>
                                <Button Name="btnDeletePreset" Grid.Column="3" Content="Удалить" Style="{StaticResource GlassButton}" VerticalAlignment="Center"/>
                            </Grid>

                            <!-- ВЫБОР РЕЖИМА -->
                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="14,10" Margin="0,0,0,12">
                                <Grid>
                                    <Grid.ColumnDefinitions>
                                        <ColumnDefinition Width="Auto"/>
                                        <ColumnDefinition Width="*"/>
                                        <ColumnDefinition Width="Auto"/>
                                    </Grid.ColumnDefinitions>
                                    <StackPanel Grid.Column="0" Orientation="Horizontal">
                                        <TextBlock Text="Формат:" FontWeight="Bold" VerticalAlignment="Center" Margin="4,0,12,0"/>
                                        <RadioButton Name="rbModeNwc" GroupName="mode" Style="{StaticResource ModePill}" Content="NWC" IsChecked="True"/>
                                        <RadioButton Name="rbModeIfc" GroupName="mode" Style="{StaticResource ModePill}" Content="IFC"/>
                                        <RadioButton Name="rbModeBoth" GroupName="mode" Style="{StaticResource ModePill}" Content="NWC + IFC"/>
                                    </StackPanel>
                                    <Button Name="btnGoSettings" Grid.Column="2" Content="Настройки экспорта →" Style="{StaticResource GlassButton}"/>
                                </Grid>
                            </Border>

                            <!-- ОБЩИЕ ДЛЯ ВСЕХ МОДЕЛЕЙ ПАПКИ И ИМЯ ВИДА -->
                            <Border Grid.Row="2" Style="{StaticResource GlassCard}" Padding="16,12" Margin="0,0,0,12">
                                <StackPanel>
                                    <Grid Name="gridSameNwc" Margin="0,0,0,8">
                                        <Grid.ColumnDefinitions>
                                            <ColumnDefinition Width="300"/>
                                            <ColumnDefinition Width="*"/>
                                            <ColumnDefinition Width="Auto"/>
                                        </Grid.ColumnDefinitions>
                                        <CheckBox Name="chkSameNwc" Content="Одна папка NWC для всех моделей" FontWeight="SemiBold" VerticalAlignment="Center"/>
                                        <TextBox Name="txtSameNwc" Grid.Column="1" Style="{StaticResource PillTextBox}" IsEnabled="{Binding IsChecked, ElementName=chkSameNwc}"/>
                                        <Button Name="btnSameNwc" Grid.Column="2" Style="{StaticResource IconButton}" Content="..." Margin="8,0,0,0"
                                                IsEnabled="{Binding IsChecked, ElementName=chkSameNwc}" ToolTip="Выбрать папку для NWC"/>
                                    </Grid>
                                    <Grid Name="gridSameIfc" Margin="0,0,0,8">
                                        <Grid.ColumnDefinitions>
                                            <ColumnDefinition Width="300"/>
                                            <ColumnDefinition Width="*"/>
                                            <ColumnDefinition Width="Auto"/>
                                        </Grid.ColumnDefinitions>
                                        <CheckBox Name="chkSameIfc" Content="Одна папка IFC для всех моделей" FontWeight="SemiBold" VerticalAlignment="Center"/>
                                        <TextBox Name="txtSameIfc" Grid.Column="1" Style="{StaticResource PillTextBox}" IsEnabled="{Binding IsChecked, ElementName=chkSameIfc}"/>
                                        <Button Name="btnSameIfc" Grid.Column="2" Style="{StaticResource IconButton}" Content="..." Margin="8,0,0,0"
                                                IsEnabled="{Binding IsChecked, ElementName=chkSameIfc}" ToolTip="Выбрать папку для IFC"/>
                                    </Grid>
                                    <Grid>
                                        <Grid.ColumnDefinitions>
                                            <ColumnDefinition Width="300"/>
                                            <ColumnDefinition Width="*"/>
                                        </Grid.ColumnDefinitions>
                                        <CheckBox Name="chkSameView" Content="Одно имя 3D-вида для всех моделей" FontWeight="SemiBold" VerticalAlignment="Center"/>
                                        <TextBox Name="txtSameView" Grid.Column="1" Style="{StaticResource PillTextBox}" Width="280" HorizontalAlignment="Left"
                                                 IsEnabled="{Binding IsChecked, ElementName=chkSameView}"/>
                                    </Grid>
                                </StackPanel>
                            </Border>

                            <!-- СПИСОК МОДЕЛЕЙ: КАРТОЧКИ (ПОЛЯ ПАПОК И ВИДА ВИДНЫ, ЕСЛИ НЕ ЗАДАНЫ ОБЩИЕ) -->
                            <Border Grid.Row="3" Style="{StaticResource GlassCard}" Padding="12,12,6,12">
                                <ScrollViewer VerticalScrollBarVisibility="Auto">
                                    <ItemsControl Name="icModels" Margin="0,0,6,0">
                                        <ItemsControl.ItemTemplate>
                                            <DataTemplate>
                                                <Border Background="#B3FFFFFF" BorderBrush="#66FFFFFF" BorderThickness="1" CornerRadius="16" Padding="14,10" Margin="0,0,0,10">
                                                    <StackPanel>
                                                        <Grid Margin="0,0,0,8">
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="120"/>
                                                                <ColumnDefinition Width="*"/>
                                                                <ColumnDefinition Width="92"/>
                                                            </Grid.ColumnDefinitions>
                                                            <TextBlock Text="Модель RVT" Foreground="#FF8C7A6B" FontWeight="SemiBold" FontSize="12" VerticalAlignment="Center"/>
                                                            <TextBox Grid.Column="1" Style="{StaticResource PillTextBox}" Text="{Binding RvtPath, Mode=TwoWay}"/>
                                                            <StackPanel Grid.Column="2" Orientation="Horizontal" HorizontalAlignment="Right">
                                                                <Button Style="{StaticResource IconButton}" Content="..." Tag="browse_rvt" ToolTip="Выбрать файл модели"/>
                                                                <Button Style="{StaticResource RemoveButton}" Content="✕" Tag="remove" Margin="8,0,0,0"/>
                                                            </StackPanel>
                                                        </Grid>
                                                        <Grid Margin="0,0,0,8">
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="120"/>
                                                                <ColumnDefinition Width="280"/>
                                                                <ColumnDefinition Width="*"/>
                                                            </Grid.ColumnDefinitions>
                                                            <TextBlock Text="Имя файла" Foreground="#FF8C7A6B" FontWeight="SemiBold" FontSize="12" VerticalAlignment="Center"/>
                                                            <TextBox Grid.Column="1" Style="{StaticResource PillTextBox}" Text="{Binding FileName, Mode=TwoWay}"/>
                                                            <TextBlock Grid.Column="2" Text="расширение .nwc / .ifc добавится само" Foreground="#FF8C7A6B" FontSize="12" VerticalAlignment="Center" Margin="12,0,0,0" TextTrimming="CharacterEllipsis"/>
                                                        </Grid>
                                                        <Grid Margin="0,0,0,8" Visibility="{Binding NwcVis}">
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="120"/>
                                                                <ColumnDefinition Width="*"/>
                                                                <ColumnDefinition Width="92"/>
                                                            </Grid.ColumnDefinitions>
                                                            <TextBlock Text="Папка NWC" Foreground="#FF8C7A6B" FontWeight="SemiBold" FontSize="12" VerticalAlignment="Center"/>
                                                            <TextBox Grid.Column="1" Style="{StaticResource PillTextBox}" Text="{Binding NwcFolder, Mode=TwoWay}"/>
                                                            <Button Grid.Column="2" Style="{StaticResource IconButton}" Content="..." Tag="browse_nwc" HorizontalAlignment="Left" Margin="8,0,0,0" ToolTip="Выбрать папку для NWC"/>
                                                        </Grid>
                                                        <Grid Margin="0,0,0,8" Visibility="{Binding IfcVis}">
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="120"/>
                                                                <ColumnDefinition Width="*"/>
                                                                <ColumnDefinition Width="92"/>
                                                            </Grid.ColumnDefinitions>
                                                            <TextBlock Text="Папка IFC" Foreground="#FF8C7A6B" FontWeight="SemiBold" FontSize="12" VerticalAlignment="Center"/>
                                                            <TextBox Grid.Column="1" Style="{StaticResource PillTextBox}" Text="{Binding IfcFolder, Mode=TwoWay}"/>
                                                            <Button Grid.Column="2" Style="{StaticResource IconButton}" Content="..." Tag="browse_ifc" HorizontalAlignment="Left" Margin="8,0,0,0" ToolTip="Выбрать папку для IFC"/>
                                                        </Grid>
                                                        <Grid Visibility="{Binding ViewVis}">
                                                            <Grid.ColumnDefinitions>
                                                                <ColumnDefinition Width="120"/>
                                                                <ColumnDefinition Width="*"/>
                                                            </Grid.ColumnDefinitions>
                                                            <TextBlock Text="Имя 3D-вида" Foreground="#FF8C7A6B" FontWeight="SemiBold" FontSize="12" VerticalAlignment="Center"/>
                                                            <TextBox Grid.Column="1" Style="{StaticResource PillTextBox}" Text="{Binding ViewName, Mode=TwoWay}" Width="280" HorizontalAlignment="Left"/>
                                                        </Grid>
                                                    </StackPanel>
                                                </Border>
                                            </DataTemplate>
                                        </ItemsControl.ItemTemplate>
                                    </ItemsControl>
                                </ScrollViewer>
                            </Border>

                            <Grid Grid.Row="4" Margin="4,14,4,0">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbModelsStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
                                <Button Name="btnImportCsv" Grid.Column="1" Content="Импорт CSV" Style="{StaticResource GlassButton}" Margin="0,0,8,0"/>
                                <Button Name="btnExportCsv" Grid.Column="2" Content="Экспорт CSV" Style="{StaticResource GlassButton}" Margin="0,0,8,0"/>
                                <Button Name="btnAddModel" Grid.Column="3" Content="+ Добавить модель" Style="{StaticResource GlassButton}"/>
                            </Grid>
                        </Grid>

                        <!-- ========================================== -->
                        <!-- РАЗДЕЛ 2: НАСТРОЙКИ ЭКСПОРТА (КАК В REVIT) -->
                        <!-- ========================================== -->
                        <Grid Name="pageSettings" Grid.Row="0" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                            </Grid.RowDefinitions>
                            <Grid Grid.Row="0" Margin="4,0,4,12">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                    <ColumnDefinition Width="Auto"/>
                                </Grid.ColumnDefinitions>
                                <StackPanel Grid.Column="0">
                                    <TextBlock Text="Настройки экспорта" FontSize="26" FontWeight="Bold"/>
                                    <TextBlock Text="Те же параметры, что в окнах экспорта NWC и IFC в Revit. Применяются ко всем моделям" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
                                </StackPanel>
                                <Button Name="btnSettingsLoad" Grid.Column="1" Content="Загрузить из файла" Style="{StaticResource GlassButton}" Margin="12,0,8,0" VerticalAlignment="Center"/>
                                <Button Name="btnSettingsSave" Grid.Column="2" Content="Сохранить в файл" Style="{StaticResource GlassButton}" Margin="0,0,8,0" VerticalAlignment="Center"/>
                                <Button Name="btnSettingsReset" Grid.Column="3" Content="По умолчанию" Style="{StaticResource GlassButton}" VerticalAlignment="Center"/>
                            </Grid>
                            <StackPanel Grid.Row="1" Orientation="Horizontal" Margin="4,0,4,12">
                                <RadioButton Name="rbTabNwc" GroupName="settingsTab" Style="{StaticResource ModePill}" Content="Настройки NWC" IsChecked="True"/>
                                <RadioButton Name="rbTabIfc" GroupName="settingsTab" Style="{StaticResource ModePill}" Content="Настройки IFC"/>                                <TextBlock Name="tbSettingsHint" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontSize="12" Margin="8,0,0,0" TextWrapping="Wrap"/>
                            </StackPanel>
                            <Border Grid.Row="2" Style="{StaticResource GlassCard}" Padding="20,16,10,16">
                                <ScrollViewer VerticalScrollBarVisibility="Auto">
                                    <StackPanel Margin="0,0,10,0">
                                        <StackPanel Name="spNwcSettings"/>
                                        <StackPanel Name="spIfcSettings" Visibility="Collapsed"/>                                    </StackPanel>
                                </ScrollViewer>
                            </Border>
                        </Grid>

                        <!-- ========================================== -->
                        <!-- РАЗДЕЛ 3: ЗАПУСК И РАСПИСАНИЕ             -->
                        <!-- ========================================== -->
                        <Grid Name="pageRun" Grid.Row="0" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                            </Grid.RowDefinitions>
                            <StackPanel Grid.Row="0" Margin="4,0,4,12">
                                <TextBlock Text="Запуск и расписание" FontSize="26" FontWeight="Bold"/>
                                <TextBlock Text="Когда запускать экспорт и как открывать модели" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0"/>
                            </StackPanel>
                            <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                                <StackPanel Margin="0,0,6,0">
                                    <Border Style="{StaticResource GlassCard}" Padding="18,14" Margin="0,0,0,12">
                                        <StackPanel>
                                            <TextBlock Text="Время запуска" FontSize="16" FontWeight="Bold" Margin="0,0,0,10"/>
                                            <StackPanel Orientation="Horizontal">
                                                <RadioButton Name="rbNow" GroupName="runtime" Style="{StaticResource ModePill}" Content="Сейчас" IsChecked="True"/>
                                                <RadioButton Name="rbLater" GroupName="runtime" Style="{StaticResource ModePill}" Content="Позже, в"/>
                                                <TextBox Name="txtTime" Style="{StaticResource PillTextBox}" Text="23:00" Width="90"
                                                         HorizontalContentAlignment="Center" IsEnabled="{Binding IsChecked, ElementName=rbLater}"/>
                                            </StackPanel>
                                            <Border Name="bdLaterWarning" Visibility="Collapsed" Margin="0,12,0,0" CornerRadius="12"
                                                    Background="#1AE8452C" BorderBrush="#80E8452C" BorderThickness="1" Padding="14,10">
                                                <TextBlock TextWrapping="Wrap" Foreground="#FFA8241A" FontWeight="SemiBold"
                                                           Text="Внимание: до времени запуска и во время экспорта Revit будет занят и не будет отвечать. Запускайте отложенный экспорт в конце рабочего дня, сохраните открытые модели и не закрывайте Revit."/>
                                            </Border>
                                        </StackPanel>
                                    </Border>

                                    <Border Style="{StaticResource GlassCard}" Padding="18,14" Margin="0,0,0,12">
                                        <StackPanel>
                                            <TextBlock Text="Открытие моделей" FontSize="16" FontWeight="Bold" Margin="0,0,0,10"/>
                                            <Grid>
                                                <Grid.ColumnDefinitions>
                                                    <ColumnDefinition Width="Auto"/>
                                                    <ColumnDefinition Width="240"/>
                                                </Grid.ColumnDefinitions>
                                                <TextBlock Text="Не загружать рабочие наборы, в имени которых есть:" FontWeight="SemiBold" VerticalAlignment="Center" Margin="0,0,12,0"/>
                                                <TextBox Name="txtWorksetKey" Grid.Column="1" Style="{StaticResource PillTextBox}"/>
                                            </Grid>
                                            <TextBlock Text="Например, «связь» — модели откроются без связей, экспорт пойдёт быстрее. Пустое поле — открыть все рабочие наборы."
                                                       FontSize="12" Foreground="#FF8C7A6B" TextWrapping="Wrap" Margin="0,6,0,12"/>
                                            <CheckBox Name="chkCreateView" FontWeight="SemiBold" Content="Создавать временный 3D-вид, если вида с указанным именем нет"/>
                                            <TextBlock Text="Временный вид скрывает линии и связи RVT и удаляется после экспорта — модели не изменяются."
                                                       FontSize="12" Foreground="#FF8C7A6B" TextWrapping="Wrap" Margin="22,4,0,0"/>
                                        </StackPanel>
                                    </Border>

                                    <Border Style="{StaticResource GlassCard}" Padding="18,14">
                                        <StackPanel>
                                            <TextBlock Text="Журнал экспорта" FontSize="16" FontWeight="Bold" Margin="0,0,0,10"/>
                                            <TextBlock Text="Каждый запуск записывает журнал (время, модели, ошибки, закрытые диалоги Revit) в папку:" TextWrapping="Wrap"/>
                                            <Grid Margin="0,8,0,0">
                                                <Grid.ColumnDefinitions>
                                                    <ColumnDefinition Width="*"/>
                                                    <ColumnDefinition Width="Auto"/>
                                                </Grid.ColumnDefinitions>
                                                <TextBlock Name="tbLogDir" FontSize="12" Foreground="#FFC24E00" FontWeight="SemiBold" VerticalAlignment="Center" TextTrimming="CharacterEllipsis"/>
                                                <Button Name="btnOpenLogDir" Grid.Column="1" Content="Открыть папку" Style="{StaticResource GlassButton}" Margin="12,0,0,0"/>
                                            </Grid>
                                        </StackPanel>
                                    </Border>
                                </StackPanel>
                            </ScrollViewer>
                        </Grid>

                        <!-- НИЖНЯЯ ПАНЕЛЬ: ЗАПУСК -->
                        <Grid Grid.Row="1" Margin="4,14,4,0">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="*"/>
                                <ColumnDefinition Width="Auto"/>
                                <ColumnDefinition Width="Auto"/>
                            </Grid.ColumnDefinitions>
                            <TextBlock Name="tbFooterStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold"/>
                            <Button Name="btnCancel" Grid.Column="1" Content="Отмена" Style="{StaticResource GlassButton}" Margin="0,0,10,0"/>
                            <Button Name="btnRun" Grid.Column="2" Content="Запустить экспорт" Style="{StaticResource PrimaryButton}"/>
                        </Grid>
                    </Grid>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>"""


# ==========================================================
# 4. ГЛАВНОЕ ОКНО
# ==========================================================
class ExportWindow(object):
    def __init__(self):
        self.win = XamlReader.Parse(XAML_MAIN.replace('__RESOURCES__', XAML_RESOURCES))
        global dialog_owner
        dialog_owner = self.win
        try:
            WindowInteropHelper(self.win).Owner = Process.GetCurrentProcess().MainWindowHandle
        except Exception:
            pass
        self.accepted = False
        self.selected_tasks = []
        self._suppress = False
        self.setting_controls = []
        self._bind_controls()

        self.tasks = ObservableCollection[object]()
        self.icModels.ItemsSource = self.tasks
        self.tasks.Add(self.new_task())

        self.build_settings_panel(self.spNwcSettings, nwc_settings, NWC_FIELDS)
        self.build_settings_panel(self.spIfcSettings, ifc_settings, IFC_FIELDS)
        self.refresh_settings_ui()

        self.presets_db = load_presets()
        self.update_preset_combo()
        self.apply_run_settings_to_ui()
        self.tbLogDir.Text = LOG_DIR
        self.on_settings_tab_changed()
        self.on_mode_changed()

    def _bind_controls(self):
        names = [
            'TitleBar', 'btnCloseDot', 'btnMinDot', 'btnMaxDot',
            'navModels', 'navSettings', 'navRun', 'pageModels', 'pageSettings', 'pageRun',
            'tbSidebarMode', 'tbSidebarCount',
            'cmbPresets', 'btnSavePreset', 'btnDeletePreset',
            'rbModeNwc', 'rbModeIfc', 'rbModeBoth', 'btnGoSettings',
            'gridSameNwc', 'chkSameNwc', 'txtSameNwc', 'btnSameNwc',
            'gridSameIfc', 'chkSameIfc', 'txtSameIfc', 'btnSameIfc',
            'chkSameView', 'txtSameView',
            'icModels', 'tbModelsStatus', 'btnImportCsv', 'btnExportCsv', 'btnAddModel',
            'btnSettingsLoad', 'btnSettingsSave', 'btnSettingsReset', 'rbTabNwc', 'rbTabIfc', 'tbSettingsHint',
            'spNwcSettings', 'spIfcSettings',
            'rbNow', 'rbLater', 'txtTime', 'bdLaterWarning', 'txtWorksetKey', 'chkCreateView',
            'tbLogDir', 'btnOpenLogDir', 'tbFooterStatus', 'btnCancel', 'btnRun'
        ]
        for n in names:
            setattr(self, n, self.win.FindName(n))

        self.TitleBar.MouseLeftButtonDown += self.on_title_drag
        self.btnCloseDot.Click += lambda s, e: self.win.Close()
        self.btnMinDot.Click += lambda s, e: setattr(self.win, 'WindowState', WindowState.Minimized)
        self.btnMaxDot.Click += self.on_toggle_max

        for nav in (self.navModels, self.navSettings, self.navRun):
            nav.Checked += self.on_nav_changed
        for rb in (self.rbModeNwc, self.rbModeIfc, self.rbModeBoth):
            rb.Checked += lambda s, e: self.on_mode_changed()
        for rb in (self.rbNow, self.rbLater):
            rb.Checked += lambda s, e: self.on_runtime_changed()
        for rb in (self.rbTabNwc, self.rbTabIfc):
            rb.Checked += lambda s, e: self.on_settings_tab_changed()
        for chk in (self.chkSameNwc, self.chkSameIfc, self.chkSameView):
            chk.Click += lambda s, e: self.update_task_visibility()

        self.btnGoSettings.Click += self.on_go_settings
        self.btnSameNwc.Click += lambda s, e: self.pick_shared_folder(self.txtSameNwc, u"NWC")
        self.btnSameIfc.Click += lambda s, e: self.pick_shared_folder(self.txtSameIfc, u"IFC")
        self.btnAddModel.Click += self.on_add_model
        self.btnImportCsv.Click += self.on_import_csv
        self.btnExportCsv.Click += self.on_export_csv
        self.btnSavePreset.Click += self.on_save_preset
        self.btnDeletePreset.Click += self.on_delete_preset
        self.cmbPresets.SelectionChanged += self.on_preset_selected
        self.btnSettingsLoad.Click += self.on_settings_load
        self.btnSettingsSave.Click += self.on_settings_save
        self.btnSettingsReset.Click += self.on_settings_reset
        self.btnOpenLogDir.Click += self.on_open_log_dir
        self.btnCancel.Click += lambda s, e: self.win.Close()
        self.btnRun.Click += self.on_run

        # Кнопки «...» и «✕» внутри карточек моделей
        self.win.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(self.on_routed_button_click))

    # ----------------------------------------------------------
    # ОКНО, НАВИГАЦИЯ, РЕЖИМ
    # ----------------------------------------------------------
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
        if self.pageModels is None:
            return
        self.pageModels.Visibility = Visibility.Visible if self.navModels.IsChecked else Visibility.Collapsed
        self.pageSettings.Visibility = Visibility.Visible if self.navSettings.IsChecked else Visibility.Collapsed
        self.pageRun.Visibility = Visibility.Visible if self.navRun.IsChecked else Visibility.Collapsed

    def on_go_settings(self, sender, e):
        # Открываем вкладку настроек того формата, который выбран (для «NWC + IFC» — NWC)
        (self.rbTabIfc if self.current_mode() == MODE_IFC else self.rbTabNwc).IsChecked = True
        self.navSettings.IsChecked = True

    def current_mode(self):
        if self.rbModeIfc.IsChecked:
            return MODE_IFC
        if self.rbModeBoth.IsChecked:
            return MODE_BOTH
        return MODE_NWC

    def set_mode(self, mode):
        {MODE_NWC: self.rbModeNwc, MODE_IFC: self.rbModeIfc, MODE_BOTH: self.rbModeBoth}.get(
            mode, self.rbModeNwc).IsChecked = True

    def on_mode_changed(self):
        mode = self.current_mode()
        self.gridSameNwc.Visibility = Visibility.Visible if mode_has_nwc(mode) else Visibility.Collapsed
        self.gridSameIfc.Visibility = Visibility.Visible if mode_has_ifc(mode) else Visibility.Collapsed
        self.update_task_visibility()

    def apply_task_visibility(self, task):
        mode = self.current_mode()
        task.NwcVis = Visibility.Visible if mode_has_nwc(mode) and not self.chkSameNwc.IsChecked else Visibility.Collapsed
        task.IfcVis = Visibility.Visible if mode_has_ifc(mode) and not self.chkSameIfc.IsChecked else Visibility.Collapsed
        task.ViewVis = Visibility.Collapsed if self.chkSameView.IsChecked else Visibility.Visible

    def update_task_visibility(self):
        for t in self.tasks:
            self.apply_task_visibility(t)
        self.update_status()

    def on_runtime_changed(self):
        self.bdLaterWarning.Visibility = Visibility.Visible if self.rbLater.IsChecked else Visibility.Collapsed
        self.update_status()

    def update_status(self):
        mode_title = MODE_TITLES[self.current_mode()]
        count = sum(1 for t in self.tasks if not t.is_empty())
        self.tbSidebarMode.Text = mode_title
        self.tbSidebarCount.Text = u"Моделей: {}".format(count)
        self.tbModelsStatus.Text = u"Моделей в списке: {} • Формат: {}".format(len(self.tasks), mode_title)
        when = u"в {}".format(self.txtTime.Text) if self.rbLater.IsChecked else u"сразу"
        self.tbFooterStatus.Text = u"Экспорт {} • моделей: {} • запуск {}".format(mode_title, count, when)

    def pick_shared_folder(self, textbox, fmt):
        folder = pick_folder(u"Выберите папку для сохранения {} (для всех моделей)".format(fmt))
        if folder:
            textbox.Text = folder

    # ----------------------------------------------------------
    # СПИСОК МОДЕЛЕЙ
    # ----------------------------------------------------------
    def new_task(self, rvt=u"", file_name=u"", nwc=u"", ifc=u"", view=u""):
        t = ExportTask(rvt, file_name, nwc, ifc, view)
        self.apply_task_visibility(t)
        return t

    def on_add_model(self, sender, e):
        self.tasks.Add(self.new_task())
        self.update_status()

    def on_routed_button_click(self, sender, e):
        src = e.OriginalSource
        tag = getattr(src, 'Tag', None)
        if tag not in ('browse_rvt', 'browse_nwc', 'browse_ifc', 'remove'):
            return
        task = src.DataContext
        if not isinstance(task, ExportTask):
            return
        if tag == 'remove':
            self.tasks.Remove(task)
            if self.tasks.Count == 0:
                self.tasks.Add(self.new_task())
        elif tag == 'browse_rvt':
            dialog = OpenFileDialog()
            dialog.Filter = u"Модели Revit (*.rvt)|*.rvt"
            dialog.Multiselect = True
            if dialog.ShowDialog():
                files = list(dialog.FileNames)
                task.RvtPath = files[0]
                # Выбрано несколько файлов — остальные добавляются отдельными моделями
                for path in files[1:]:
                    self.tasks.Add(self.new_task(path))
        elif tag == 'browse_nwc':
            folder = pick_folder(u"Выберите папку для сохранения NWC")
            if folder:
                task.NwcFolder = folder
        elif tag == 'browse_ifc':
            folder = pick_folder(u"Выберите папку для сохранения IFC")
            if folder:
                task.IfcFolder = folder
        self.update_status()

    def on_export_csv(self, sender, e):
        dialog = SaveFileDialog()
        dialog.Filter = u"CSV (*.csv)|*.csv"
        dialog.FileName = "ExportConfig.csv"
        if not dialog.ShowDialog():
            return
        cols = ["RvtPath", "FileName", "NwcFolder", "IfcFolder", "ViewName"]
        try:
            with open(dialog.FileName, 'wb') as f:
                f.write('\xef\xbb\xbf')
                writer = csv.writer(f, delimiter=';')
                writer.writerow(cols)
                for t in self.tasks:
                    data = t.to_dict()
                    writer.writerow([(data[c] or u"").encode('utf-8') for c in cols])
            show_message(u"Список моделей выгружен в CSV.", kind='info')
        except Exception as ex:
            show_message(u"Ошибка сохранения CSV:\n{}".format(ex))

    def on_import_csv(self, sender, e):
        dialog = OpenFileDialog()
        dialog.Filter = u"CSV (*.csv)|*.csv"
        if not dialog.ShowDialog():
            return
        try:
            with open(dialog.FileName, 'rb') as f:
                if f.read(3) != '\xef\xbb\xbf':
                    f.seek(0)
                rows = [[c.decode('utf-8') for c in row] for row in csv.reader(f, delimiter=';')]
        except Exception as ex:
            show_message(u"Ошибка чтения CSV:\n{}".format(ex))
            return
        header = [h.strip() for h in rows[0]] if rows else []
        if "RvtPath" not in header:
            show_message(u"В первой строке CSV нет столбца RvtPath. Выгрузите шаблон кнопкой «Экспорт CSV».")
            return
        # Столбцы ищем по названию — так читаются и старые CSV из NWC-плагина (RvtPath;NwcFolder;ViewName)
        self.tasks.Clear()
        for row in rows[1:]:
            data = dict(zip(header, row))
            if not (data.get("RvtPath") or u"").strip():
                continue
            self.tasks.Add(self.new_task(data.get("RvtPath", u""), data.get("FileName", u""), data.get("NwcFolder", u""),
                                         data.get("IfcFolder", u""), data.get("ViewName", u"")))
        if self.tasks.Count == 0:
            self.tasks.Add(self.new_task())
        # Если в CSV заданы свои папки или виды — используем их вместо общих
        if any(t.NwcFolder for t in self.tasks):
            self.chkSameNwc.IsChecked = False
        if any(t.IfcFolder for t in self.tasks):
            self.chkSameIfc.IsChecked = False
        if "ViewName" in header:
            self.chkSameView.IsChecked = False
        self.update_task_visibility()

    # ----------------------------------------------------------
    # НАСТРОЙКИ ЭКСПОРТА (ПОЛЯ СТРОЯТСЯ ПО NWC_FIELDS / IFC_FIELDS)
    # ----------------------------------------------------------
    def build_settings_panel(self, panel, settings, fields):
        first = True
        for key, kind, label, _, choices in fields:
            if kind == 'group':
                tb = TextBlock()
                tb.Text = label
                tb.FontSize = 16
                tb.FontWeight = FontWeights.Bold
                tb.Margin = Thickness(0, 0 if first else 18, 0, 8)
                panel.Children.Add(tb)
                first = False
                continue

            if kind == 'bool':
                ctl = CheckBox()
                ctl.Content = label
                ctl.FontWeight = FontWeights.SemiBold
                ctl.Margin = Thickness(0, 5, 0, 5)
                panel.Children.Add(ctl)
            elif kind == 'choice':
                ctl = ComboBox()
                ctl.Style = self.win.FindResource('PillComboBox')
                ctl.Tag = label + u":"
                ctl.Width = 560
                ctl.HorizontalAlignment = HorizontalAlignment.Left
                ctl.Margin = Thickness(0, 4, 0, 4)
                ctl.ItemsSource = [Option(n, v) for n, v in choices]
                panel.Children.Add(ctl)
            elif kind == 'number':
                row = StackPanel()
                row.Orientation = Orientation.Horizontal
                row.Margin = Thickness(0, 4, 0, 4)
                lbl = TextBlock()
                lbl.Text = label
                lbl.FontWeight = FontWeights.SemiBold
                lbl.VerticalAlignment = VerticalAlignment.Center
                lbl.Margin = Thickness(0, 0, 12, 0)
                ctl = TextBox()
                ctl.Style = self.win.FindResource('PillTextBox')
                ctl.Width = 110
                row.Children.Add(lbl)
                row.Children.Add(ctl)
                panel.Children.Add(row)
            else:  # file
                lbl = TextBlock()
                lbl.Text = label
                lbl.FontWeight = FontWeights.SemiBold
                lbl.Margin = Thickness(0, 8, 0, 4)
                grid = Grid()
                grid.ColumnDefinitions.Add(ColumnDefinition())
                col = ColumnDefinition()
                col.Width = GridLength.Auto
                grid.ColumnDefinitions.Add(col)
                ctl = TextBox()
                ctl.Style = self.win.FindResource('PillTextBox')
                btn = Button()
                btn.Style = self.win.FindResource('IconButton')
                btn.Content = u"..."
                btn.Margin = Thickness(8, 0, 0, 0)
                Grid.SetColumn(btn, 1)
                grid.Children.Add(ctl)
                grid.Children.Add(btn)
                panel.Children.Add(lbl)
                panel.Children.Add(grid)
                btn.Click += self._make_browse_handler(ctl)
            self._bind_setting(settings, key, kind, ctl)
            self.setting_controls.append((settings, key, kind, ctl))

    def _make_browse_handler(self, textbox):
        def handler(sender, e):
            dialog = OpenFileDialog()
            dialog.Filter = u"Текстовые файлы (*.txt)|*.txt|Все файлы (*.*)|*.*"
            if dialog.ShowDialog():
                textbox.Text = dialog.FileName
        return handler

    def _bind_setting(self, settings, key, kind, ctl):
        """Изменение поля сразу записывается в настройки формата."""
        def on_changed(sender, e):
            if self._suppress:
                return
            if kind == 'bool':
                settings.values[key] = bool(ctl.IsChecked)
            elif kind == 'choice':
                if ctl.SelectedItem is not None:
                    settings.values[key] = ctl.SelectedItem.Value
            elif kind == 'file':
                settings.values[key] = (ctl.Text or u"").strip()

        def on_number_lost_focus(sender, e):
            try:
                settings.values[key] = float((ctl.Text or u"").replace(u",", u"."))
            except ValueError:
                pass
            self.refresh_settings_ui()

        if kind == 'bool':
            ctl.Click += on_changed
        elif kind == 'choice':
            ctl.SelectionChanged += on_changed
        elif kind == 'file':
            ctl.TextChanged += on_changed
        else:
            ctl.LostFocus += on_number_lost_focus

    def refresh_settings_ui(self):
        self._suppress = True
        try:
            for settings, key, kind, ctl in self.setting_controls:
                val = settings.get(key)
                if kind == 'bool':
                    ctl.IsChecked = bool(val)
                elif kind == 'choice':
                    ctl.SelectedItem = None
                    for it in ctl.ItemsSource:
                        if it.Value == val:
                            ctl.SelectedItem = it
                    if ctl.SelectedItem is None:
                        ctl.SelectedIndex = 0
                elif kind == 'number':
                    ctl.Text = option_text(float(val))
                else:
                    ctl.Text = val or u""
        finally:
            self._suppress = False

    def current_settings(self):
        return ifc_settings if self.rbTabIfc.IsChecked else nwc_settings

    def on_settings_tab_changed(self):
        if self.spNwcSettings is None:
            return
        is_ifc = bool(self.rbTabIfc.IsChecked)
        self.spNwcSettings.Visibility = Visibility.Collapsed if is_ifc else Visibility.Visible
        self.spIfcSettings.Visibility = Visibility.Visible if is_ifc else Visibility.Collapsed
        self.tbSettingsHint.Text = (
            u"Набор опций зависит от версии модуля IFC для Revit. Фаза — фаза 3D-вида." if is_ifc
            else u"По умолчанию — значения Revit. Связи не закрывайте ключом на вкладке «Запуск», если их экспортируете.")

    def on_settings_save(self, sender, e):
        dialog = SaveFileDialog()
        dialog.Filter = u"Настройки экспорта (*.json)|*.json"
        dialog.FileName = u"Настройки_экспорта.json"
        if not dialog.ShowDialog():
            return
        try:
            with open(dialog.FileName, 'wb') as f:
                data = {"Nwc": nwc_settings.to_dict(), "Ifc": ifc_settings.to_dict()}
                f.write(json.dumps(data, ensure_ascii=False, indent=4).encode('utf-8'))
            show_message(u"Настройки NWC и IFC сохранены в файл.", kind='info')
        except Exception as ex:
            show_message(u"Не удалось сохранить настройки:\n{}".format(ex))

    def on_settings_load(self, sender, e):
        dialog = OpenFileDialog()
        dialog.Filter = u"Настройки экспорта (*.json)|*.json"
        if not dialog.ShowDialog():
            return
        try:
            with open(dialog.FileName, 'rb') as f:
                data = json.loads(f.read().decode('utf-8'))
        except Exception as ex:
            show_message(u"Не удалось прочитать файл настроек:\n{}".format(ex))
            return
        loaded = []
        if isinstance(data.get("Nwc"), dict):
            nwc_settings.load(data["Nwc"])
            loaded.append(u"NWC")
        if isinstance(data.get("Ifc"), dict):
            ifc_settings.load(data["Ifc"])
            loaded.append(u"IFC")
        if not loaded:
            show_message(u"В файле нет настроек NWC или IFC.")
            return
        self.refresh_settings_ui()
        show_message(u"Загружены настройки: {}.".format(u", ".join(loaded)), kind='info')

    def on_settings_reset(self, sender, e):
        settings = self.current_settings()
        if not show_message(u"Сбросить настройки {} к значениям по умолчанию?".format(settings.title), ask=True):
            return
        settings.reset()
        self.refresh_settings_ui()

    # ----------------------------------------------------------
    # ПРЕСЕТЫ
    # ----------------------------------------------------------
    def update_preset_combo(self, select_name=None):
        items = [Option(n, n) for n in sorted(self.presets_db.keys())]
        self.cmbPresets.ItemsSource = items
        for it in items:
            if it.Value == select_name:
                self.cmbPresets.SelectedItem = it

    def read_run_settings_from_ui(self):
        run_settings.Mode = self.current_mode()
        run_settings.RunLater = bool(self.rbLater.IsChecked)
        run_settings.TargetTime = (self.txtTime.Text or u"").strip()
        run_settings.WorksetKeyword = (self.txtWorksetKey.Text or u"").strip()
        run_settings.CreateView = bool(self.chkCreateView.IsChecked)
        run_settings.SameNwcFolder = bool(self.chkSameNwc.IsChecked)
        run_settings.NwcFolder = (self.txtSameNwc.Text or u"").strip()
        run_settings.SameIfcFolder = bool(self.chkSameIfc.IsChecked)
        run_settings.IfcFolder = (self.txtSameIfc.Text or u"").strip()
        run_settings.SameView = bool(self.chkSameView.IsChecked)
        run_settings.ViewName = (self.txtSameView.Text or u"").strip()

    def apply_run_settings_to_ui(self):
        self.set_mode(run_settings.Mode)
        if run_settings.RunLater:
            self.rbLater.IsChecked = True
        else:
            self.rbNow.IsChecked = True
        self.txtTime.Text = run_settings.TargetTime
        self.txtWorksetKey.Text = run_settings.WorksetKeyword
        self.chkCreateView.IsChecked = run_settings.CreateView
        self.chkSameNwc.IsChecked = run_settings.SameNwcFolder
        self.txtSameNwc.Text = run_settings.NwcFolder
        self.chkSameIfc.IsChecked = run_settings.SameIfcFolder
        self.txtSameIfc.Text = run_settings.IfcFolder
        self.chkSameView.IsChecked = run_settings.SameView
        self.txtSameView.Text = run_settings.ViewName
        self.on_runtime_changed()
        self.on_mode_changed()

    def on_save_preset(self, sender, e):
        current = self.cmbPresets.SelectedItem
        name = ask_text(u"Введите имя пресета:", current.Name if current else u"", u"Сохранение пресета")
        if not name:
            return
        self.read_run_settings_from_ui()
        self.presets_db[name] = {
            "Mode": run_settings.Mode,
            "RunLater": run_settings.RunLater,
            "TargetTime": run_settings.TargetTime,
            "WorksetKeyword": run_settings.WorksetKeyword,
            "CreateNavisworksView": run_settings.CreateView,
            "SameNwcFolder": run_settings.SameNwcFolder,
            "SharedNwcFolder": run_settings.NwcFolder,
            "SameIfcFolder": run_settings.SameIfcFolder,
            "SharedIfcFolder": run_settings.IfcFolder,
            "SameView": run_settings.SameView,
            "SharedViewName": run_settings.ViewName,
            "Nwc": nwc_settings.to_dict(),
            "Ifc": ifc_settings.to_dict(),
            "Paths": [t.to_dict() for t in self.tasks if not t.is_empty()]
        }
        try:
            save_presets(self.presets_db)
        except Exception as ex:
            show_message(u"Не удалось сохранить пресет:\n{}".format(ex))
            return
        self.update_preset_combo(name)
        show_message(u"Пресет «{}» сохранён.".format(name), kind='info')

    def on_delete_preset(self, sender, e):
        selected = self.cmbPresets.SelectedItem
        if selected is None:
            show_message(u"Сначала выберите пресет для удаления.")
            return
        if not show_message(u"Удалить пресет «{}»?".format(selected.Name), ask=True):
            return
        del self.presets_db[selected.Value]
        save_presets(self.presets_db)
        self.update_preset_combo()

    def on_preset_selected(self, sender, e):
        selected = self.cmbPresets.SelectedItem
        if selected is None or selected.Value not in self.presets_db:
            return
        data = self.presets_db[selected.Value]
        # Старые пресеты NWC-плагина хранили настройки NWC на верхнем уровне и не знали об общих папках
        nwc_settings.load(data.get("Nwc") or data)
        ifc_settings.load(data.get("Ifc") or {})
        self.refresh_settings_ui()
        run_settings.Mode = data.get("Mode", MODE_NWC)
        run_settings.RunLater = bool(data.get("RunLater", False))
        run_settings.TargetTime = data.get("TargetTime", u"23:00")
        run_settings.WorksetKeyword = data.get("WorksetKeyword", u"связь")
        run_settings.CreateView = bool(data.get("CreateNavisworksView", True))
        run_settings.SameNwcFolder = bool(data.get("SameNwcFolder", False))
        run_settings.NwcFolder = data.get("SharedNwcFolder", u"")
        run_settings.SameIfcFolder = bool(data.get("SameIfcFolder", False))
        run_settings.IfcFolder = data.get("SharedIfcFolder", u"")
        run_settings.SameView = bool(data.get("SameView", False))
        run_settings.ViewName = data.get("SharedViewName", DEFAULT_VIEW_NAME)

        self.tasks.Clear()
        for p in data.get("Paths", []):
            self.tasks.Add(ExportTask(p.get("RvtPath", u""), p.get("FileName", u""), p.get("NwcFolder", u""),
                                      p.get("IfcFolder", u""), p.get("ViewName", u"")))
        if self.tasks.Count == 0:
            self.tasks.Add(ExportTask())
        self.apply_run_settings_to_ui()

    def on_open_log_dir(self, sender, e):
        try:
            if not os.path.isdir(LOG_DIR):
                os.makedirs(LOG_DIR)
            Process.Start(LOG_DIR)
        except Exception as ex:
            show_message(u"Не удалось открыть папку журнала:\n{}".format(ex))

    # ----------------------------------------------------------
    # ПРОВЕРКА И ЗАПУСК
    # ----------------------------------------------------------
    def validate(self, tasks, mode):
        errors = []
        if not tasks:
            return [u"Добавьте хотя бы одну модель для экспорта."]
        if mode_has_nwc(mode) and not DB.OptionalFunctionalityUtils.IsNavisworksExporterAvailable():
            errors.append(u"Экспортёр Navisworks не установлен в Revit (NWC Export Utility).")
        if mode_has_ifc(mode) and not DB.OptionalFunctionalityUtils.IsIFCAvailable():
            errors.append(u"Экспорт IFC недоступен в этой установке Revit.")
        for key in IFC_FILE_OPTIONS.keys() + ['FamilyMappingFile']:
            path = ifc_settings.get(key)
            if mode_has_ifc(mode) and path and not os.path.isfile(path):
                errors.append(u"Настройки IFC: файл не найден — {}".format(path))

        seen = {}
        for i, t in enumerate(tasks):
            num = i + 1
            if not t.RvtPath or not os.path.isfile(t.RvtPath):
                errors.append(u"Модель {}: файл не найден — {}".format(num, t.RvtPath or u"путь не указан"))
            elif not t.RvtPath.lower().endswith(u".rvt"):
                errors.append(u"Модель {}: это не файл .rvt — {}".format(num, t.RvtPath))
            if not task_view_name(t):
                errors.append(u"Модель {}: не указано имя 3D-вида".format(num))
            name = task_file_name(t)
            if not name or re.search(r'[\\/:*?"<>|]', name):
                errors.append(u"Модель {}: недопустимое имя файла «{}»".format(num, name))
            for has, folder, ext in ((mode_has_nwc(mode), task_nwc_folder(t), u"NWC"),
                                     (mode_has_ifc(mode), task_ifc_folder(t), u"IFC")):
                if not has:
                    continue
                if not folder or not os.path.isdir(folder):
                    errors.append(u"Модель {}: папка {} не найдена — {}".format(num, ext, folder or u"путь не указан"))
                    continue
                key = (ext, os.path.normcase(os.path.abspath(folder)), name.lower())
                if key in seen:
                    errors.append(u"Модели {} и {}: одинаковое имя файла {} «{}» в одной папке — второй перезапишет первый".format(
                        seen[key], num, ext, name))
                else:
                    seen[key] = num
        return errors

    def on_run(self, sender, e):
        self.read_run_settings_from_ui()
        tasks = [t for t in self.tasks if not t.is_empty()]
        mode = run_settings.Mode
        errors = self.validate(tasks, mode)

        if run_settings.RunLater:
            m = re.match(r"^(\d{1,2}):(\d{2})$", run_settings.TargetTime)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                errors.append(u"Неверное время запуска. Формат ЧЧ:ММ, например 23:00.")

        if errors:
            show_message(u"Экспорт не запущен: исправьте ошибки.", details=errors,
                         details_header=u"Ошибок: {}".format(len(errors)))
            return

        if run_settings.RunLater:
            if not show_message(
                    u"Экспорт начнётся в {}. До этого времени и во время экспорта Revit будет занят и не будет "
                    u"отвечать. Продолжить?".format(run_settings.TargetTime), ask=True):
                return

        self.selected_tasks = tasks
        self.accepted = True
        self.win.Close()

    def show(self):
        self.win.ShowDialog()
        return self.accepted


# ==========================================================
# 5. ЭКСПОРТ: ОТКРЫТИЕ МОДЕЛЕЙ, ВИД, NWC, IFC, ЖУРНАЛ
# ==========================================================
class ExportLog(object):
    def __init__(self):
        self.path = None
        try:
            if not os.path.isdir(LOG_DIR):
                os.makedirs(LOG_DIR)
            self.path = os.path.join(LOG_DIR, u"export_{}.txt".format(datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")))
        except Exception:
            pass

    def write(self, text):
        if not self.path:
            return
        try:
            with io.open(self.path, 'a', encoding='utf-8') as f:
                f.write(u"[{}] {}\n".format(datetime.datetime.now().strftime("%H:%M:%S"), text))
        except Exception:
            pass


def find_3d_view(d, view_name):
    for v in DB.FilteredElementCollector(d).OfClass(DB.View3D).ToElements():
        if not v.IsTemplate and v.Name == view_name:
            return v
    return None


def create_clean_navisworks_view(background_doc, view_name):
    view_family_type = None
    for vft in DB.FilteredElementCollector(background_doc).OfClass(DB.ViewFamilyType).ToElements():
        if vft.ViewFamily == DB.ViewFamily.ThreeDimensional:
            view_family_type = vft
            break

    if not view_family_type:
        return None

    with DB.Transaction(background_doc, u"Создание вида для экспорта") as t:
        t.Start()
        new_view = DB.View3D.CreateIsometric(background_doc, view_family_type.Id)
        new_view.Name = view_name

        lines_cat = DB.Category.GetCategory(background_doc, DB.BuiltInCategory.OST_Lines)
        if lines_cat and new_view.CanCategoryBeHidden(lines_cat.Id):
            new_view.SetCategoryHidden(lines_cat.Id, True)

        links_cat = DB.Category.GetCategory(background_doc, DB.BuiltInCategory.OST_RvtLinks)
        if links_cat and new_view.CanCategoryBeHidden(links_cat.Id):
            new_view.SetCategoryHidden(links_cat.Id, True)

        new_view.DetailLevel = DB.ViewDetailLevel.Fine
        t.Commit()
        return new_view


def delete_temp_view(d, view_id):
    t = DB.Transaction(d, u"Удаление временного вида экспорта")
    t.Start()
    try:
        d.Delete(view_id)
        t.Commit()
    except Exception:
        t.RollBack()


def open_background(path, workset_key):
    """Открывает модель в фоне: совместную — с отсоединением от центральной и закрытыми РН по ключу."""
    info = DB.BasicFileInfo.Extract(path)
    file_ver = getattr(info, 'Format', None) or getattr(info, 'SavedInVersion', u"")
    m = re.search(r"(20\d\d)", u"{}".format(file_ver))
    if m and int(m.group(1)) > int(app.VersionNumber):
        raise ExportError(u"файл сохранён в Revit {}, а запущен Revit {}".format(m.group(1), app.VersionNumber))

    model_path = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(path)
    opts = DB.OpenOptions()
    if info.IsWorkshared:
        opts.DetachFromCentralOption = DB.DetachFromCentralOption.DetachAndPreserveWorksets
        key = (workset_key or u"").strip().lower()
        if key:
            config = DB.WorksetConfiguration(DB.WorksetConfigurationOption.OpenAllWorksets)
            to_close = List[DB.WorksetId]()
            for wp in DB.WorksharingUtils.GetUserWorksetInfo(model_path):
                if key in wp.Name.lower():
                    to_close.Add(wp.Id)
            if to_close.Count > 0:
                config.Close(to_close)
            opts.SetOpenWorksetsConfiguration(config)
    return app.OpenDocumentFile(model_path, opts)


def export_nwc(d, task, view, file_name, log):
    opts = DB.NavisworksExportOptions()
    opts.ExportScope = DB.NavisworksExportScope.View
    opts.ViewId = view.Id
    skipped = apply_nwc_settings(nwc_settings, opts)
    if skipped:
        log.write(u"NWC: в этой версии Revit нет настроек {}".format(u", ".join(skipped)))
    d.Export(task_nwc_folder(task), file_name + u".nwc", opts)


def export_ifc(d, task, view, file_name, log):
    opts = DB.IFCExportOptions()
    apply_ifc_settings(ifc_settings, opts, view)
    # Экспорт IFC в Revit выполняется внутри транзакции; изменения откатываются, модель не меняется
    t = DB.Transaction(d, u"Экспорт IFC")
    t.Start()
    try:
        d.Export(task_ifc_folder(task), file_name + u".ifc", opts)
    finally:
        if t.GetStatus() == DB.TransactionStatus.Started:
            t.RollBack()


def export_task(task, log, stats, errors):
    file_name = task_file_name(task)
    view_name = task_view_name(task)
    active = revit.doc
    is_active = bool(active.PathName) and os.path.normcase(active.PathName) == os.path.normcase(task.RvtPath)
    d = active if is_active else open_background(task.RvtPath, run_settings.WorksetKeyword)
    log.write(u"Открыта модель: {}{}".format(task.RvtPath, u" (активный документ)" if is_active else u""))
    temp_view_id = None
    try:
        view = find_3d_view(d, view_name)
        if view is None:
            if not run_settings.CreateView:
                raise ExportError(u"вид «{}» не найден".format(view_name))
            view = create_clean_navisworks_view(d, view_name)
            if view is None:
                raise ExportError(u"не удалось создать вид «{}»".format(view_name))
            temp_view_id = view.Id
            log.write(u"Создан временный вид «{}»".format(view_name))

        for has, fmt, func in ((mode_has_nwc(run_settings.Mode), u"NWC", export_nwc),
                               (mode_has_ifc(run_settings.Mode), u"IFC", export_ifc)):
            if not has:
                continue
            try:
                func(d, task, view, file_name, log)
                stats[fmt] += 1
                log.write(u"{}: готово — {}".format(fmt, file_name))
            except Exception as ex:
                errors.append(u"{} • {}: {}".format(file_name, fmt, ex))
                log.write(u"{}: ОШИБКА — {}: {}".format(fmt, file_name, ex))
    finally:
        if is_active:
            if temp_view_id is not None:
                delete_temp_view(d, temp_view_id)
        else:
            d.Close(False)


def wait_until(target_time_text):
    """Ожидание времени запуска. Возвращает False, если пользователь отменил ожидание."""
    hours, minutes = [int(x) for x in target_time_text.split(':')]
    now = datetime.datetime.now()
    target = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if target < now:
        target += datetime.timedelta(days=1)

    with forms.ProgressBar(title=u'Ожидание таймера...', cancellable=True) as pb:
        while datetime.datetime.now() < target:
            if pb.cancelled:
                return False
            remaining = target - datetime.datetime.now()
            pb.title = u'Экспорт начнётся в {}. Осталось: {}'.format(target_time_text, str(remaining).split('.')[0])
            time.sleep(1)
    return True


def run_export(tasks):
    if run_settings.RunLater and not wait_until(run_settings.TargetTime):
        show_message(u"Ожидание отменено, экспорт не выполнялся.", kind='info')
        return

    log = ExportLog()
    mode_title = MODE_TITLES[run_settings.Mode]
    log.write(u"Старт экспорта: {} • моделей: {}".format(mode_title, len(tasks)))
    stats = {u"NWC": 0, u"IFC": 0}
    errors = []

    # Автоматически закрываем диалоги Revit и удаляем предупреждения, чтобы ночной запуск не остановился
    def on_dialog_showing(sender, args):
        try:
            log.write(u"Автоматически закрыт диалог Revit: {}".format(args.DialogId))
            args.OverrideResult(1)
        except Exception:
            pass

    def on_failures_processing(sender, args):
        try:
            args.GetFailuresAccessor().DeleteAllWarnings()
        except Exception:
            pass

    uiapp = __revit__
    uiapp.DialogBoxShowing += on_dialog_showing
    app.FailuresProcessing += on_failures_processing
    cancelled = False
    try:
        with forms.ProgressBar(title=u'Экспорт {}...'.format(mode_title), cancellable=True) as pb:
            for index, task in enumerate(tasks):
                if pb.cancelled:
                    cancelled = True
                    log.write(u"Экспорт прерван пользователем")
                    break
                pb.update_progress(index, len(tasks))
                try:
                    export_task(task, log, stats, errors)
                except Exception as ex:
                    name = os.path.basename(task.RvtPath)
                    errors.append(u"{}: {}".format(name, ex))
                    log.write(u"ОШИБКА — {}: {}".format(name, ex))
    finally:
        uiapp.DialogBoxShowing -= on_dialog_showing
        app.FailuresProcessing -= on_failures_processing

    parts = []
    if mode_has_nwc(run_settings.Mode):
        parts.append(u"NWC: {} из {}".format(stats[u"NWC"], len(tasks)))
    if mode_has_ifc(run_settings.Mode):
        parts.append(u"IFC: {} из {}".format(stats[u"IFC"], len(tasks)))
    summary = u"Экспорт завершён{}. {}".format(u" (прерван)" if cancelled else u"", u" • ".join(parts))
    log.write(summary)
    if log.path:
        summary += u"\nЖурнал: {}".format(log.path)

    if errors:
        show_message(summary, kind='warn', title=u"Отчёт об экспорте", details=errors,
                     details_header=u"Ошибок: {}".format(len(errors)))
    else:
        show_message(summary, kind='info', title=u"Отчёт об экспорте")


# ==========================================================
# 6. ЗАПУСК ПЛАГИНА
# ==========================================================
window = ExportWindow()
if window.show():
    run_export(window.selected_tasks)

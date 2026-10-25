# -*- coding: utf-8 -*-
"""Подготовка проекта к армированию (pyRevit / IronPython 2.7).

Разделы окна:
  Параметры       — привязка АрмЧисло, АрмМарка, АрмКонструкция из ФОП пользователя к арматуре
                    и группам модели (как в «Схемах СС»: недостающие определения можно дописать в ФОП).
  Формы стержней  — таблица ролей «прямой / Г / П / хомут / шпилька / лягушка → форма проекта».
                    Пустая роль — автоподбор формы при создании стержня.
  Нормы           — просмотр таблицы norms.json с источниками и пометками «сверить».
"""
__title__ = u"Подготовка\nпроекта"
__author__ = u"Daniil Antonenko"
__doc__ = u"Параметры КЖ из ФОП, таблица форм стержней по ролям, просмотр таблицы норм"

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import os
import traceback

from System.Diagnostics import Process
from System.Windows.Controls import SelectionChangedEventHandler
from System.Windows.Controls.Primitives import Selector
from Microsoft.Win32 import OpenFileDialog

import sys
# pyRevit держит модули lib в памяти движка между запусками кнопок: без сброса после обновления
# кнопка может взять старую версию kzh (ImportError: cannot import name ...)
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from kzh import norms, params, settings
from kzh import rebar as kr
from kzh import ui
from kzh.ui import show_message, Option

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document
app = __revit__.Application

TXT_AUTO = u"Автоподбор"


class ShapeRow(object):
    """Строка таблицы ролей: роль, выбранная форма (Options/SelectedIndex для ComboBox), состояние."""

    def __init__(self, role, title, name, shape_names):
        self.Role = role
        self.Title = title
        self.Options = [Option(TXT_AUTO, u"")] + [Option(n, n) for n in shape_names]
        self.Value = name
        values = [o.Value for o in self.Options]
        self.missing = bool(name) and name not in values
        if self.missing:
            # Форма из настроек есть только в другом проекте — показываем её, чтобы не потерять выбор
            self.Options.append(Option(u"{} (нет в проекте)".format(name), name))
            values.append(name)
        self.SelectedIndex = values.index(name) if name in values else 0
        self.update_status()

    def update_status(self):
        if not self.Value:
            text, kind = u"Автоподбор", 'warn'
        elif self.missing:
            text, kind = u"Нет в проекте", 'err'
        else:
            text, kind = u"Есть в проекте", 'ok'
        self.StatusText = text
        self.StatusBg, self.StatusFg = params.STATUS_COLORS[kind]


class NormRow(object):
    def __init__(self, row):
        self.Title, self.Values, self.Source, verify = row
        kind = 'warn' if verify else 'ok'
        self.StatusText = u"Сверить" if verify else u"Проверено"
        self.StatusBg, self.StatusFg = params.STATUS_COLORS[kind]


XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Подготовка проекта КЖ" Width="1060" Height="720" MinWidth="900" MinHeight="600"
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
__TITLEBAR__
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
                            <TextBlock Grid.Row="0" Text="КЖ • ПОДГОТОВКА" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navParams" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Параметры"/>
                                <RadioButton Name="navShapes" GroupName="nav" Style="{StaticResource NavItem}" Content="Формы стержней"/>
                                <RadioButton Name="navNorms" GroupName="nav" Style="{StaticResource NavItem}" Content="Нормы"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="ПАРАМЕТРЫ КЖ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                    <TextBlock Name="tbSidebarParams" Text="—" FontSize="24" FontWeight="Bold" Foreground="#FFFF6A00" Margin="0,2,0,0"/>
                                    <TextBlock Name="tbSidebarDoc" Text="Модель" FontSize="11" Foreground="#FF8C7A6B" TextTrimming="CharacterEllipsis"/>
                                </StackPanel>
                            </Border>
                        </Grid>
                    </Border>

                    <!-- ПРАВАЯ РАБОЧАЯ ОБЛАСТЬ -->
                    <Grid Grid.Column="2">

                        <!-- РАЗДЕЛ 1: ПАРАМЕТРЫ -->
                        <Grid Name="pageParams" Visibility="Visible">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <StackPanel Grid.Row="0" Margin="4,0,4,12">
                                <TextBlock Text="Параметры проекта" FontSize="26" FontWeight="Bold"/>
                                <TextBlock Text="АрмЧисло и параметры каркасов из вашего ФОП — к арматуре и группам модели одной кнопкой. В спецификации: Количество × АрмЧисло" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
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
                                            <ColumnDefinition Width="80"/>
                                            <ColumnDefinition Width="100"/>
                                            <ColumnDefinition Width="80"/>
                                            <ColumnDefinition Width="220"/>
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
                                                                <ColumnDefinition Width="80"/>
                                                                <ColumnDefinition Width="100"/>
                                                                <ColumnDefinition Width="80"/>
                                                                <ColumnDefinition Width="220"/>
                                                            </Grid.ColumnDefinitions>
                                                            <StackPanel VerticalAlignment="Center">
                                                                <TextBlock Text="{Binding Name}" FontWeight="SemiBold"/>
                                                                <TextBlock Text="{Binding Description}" FontSize="11" Foreground="#FF8C7A6B" TextWrapping="Wrap"/>
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

                        <!-- РАЗДЕЛ 2: ФОРМЫ СТЕРЖНЕЙ -->
                        <Grid Name="pageShapes" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <StackPanel Grid.Row="0" Margin="4,0,4,12">
                                <TextBlock Text="Формы стержней" FontSize="26" FontWeight="Bold"/>
                                <TextBlock Text="Какую форму из шаблона проекта брать для каждой роли. «Автоподбор» — Revit подберёт подходящую форму проекта или один раз создаст новую" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
                            </StackPanel>
                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="14,10,6,10">
                                <ScrollViewer VerticalScrollBarVisibility="Auto">
                                    <ItemsControl Name="icShapes" Margin="0,0,6,0">
                                        <ItemsControl.ItemTemplate>
                                            <DataTemplate>
                                                <Border Background="#B3FFFFFF" BorderBrush="#66FFFFFF" BorderThickness="1" CornerRadius="12" Padding="12,8" Margin="0,0,0,6">
                                                    <Grid>
                                                        <Grid.ColumnDefinitions>
                                                            <ColumnDefinition Width="*"/>
                                                            <ColumnDefinition Width="300"/>
                                                            <ColumnDefinition Width="160"/>
                                                        </Grid.ColumnDefinitions>
                                                        <TextBlock Text="{Binding Title}" FontWeight="SemiBold" VerticalAlignment="Center"/>
                                                        <ComboBox Grid.Column="1" Style="{StaticResource PillComboBox}" Tag="Форма:"
                                                                  ItemsSource="{Binding Options}" SelectedIndex="{Binding SelectedIndex, Mode=OneTime}"/>
                                                        <Border Grid.Column="2" Background="{Binding StatusBg}" CornerRadius="9" Padding="10,4" Margin="12,0,0,0" HorizontalAlignment="Left" VerticalAlignment="Center">
                                                            <TextBlock Text="{Binding StatusText}" Foreground="{Binding StatusFg}" FontSize="12" FontWeight="SemiBold"/>
                                                        </Border>
                                                    </Grid>
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
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbShapesStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
                                <Button Name="btnSaveShapes" Grid.Column="1" Content="Сохранить таблицу ролей" Style="{StaticResource PrimaryButton}"/>
                            </Grid>
                        </Grid>

                        <!-- РАЗДЕЛ 3: НОРМЫ -->
                        <Grid Name="pageNorms" Visibility="Collapsed">
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <StackPanel Grid.Row="0" Margin="4,0,4,12">
                                <TextBlock Text="Нормы" FontSize="26" FontWeight="Bold"/>
                                <TextBlock Text="Значения из lib/kzh/norms.json: СП 63.13330.2018 (изм. 1, 2), ГОСТ 34028-2016. Правятся в файле без изменения кода" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
                            </StackPanel>
                            <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="14,10,6,10">
                                <ScrollViewer VerticalScrollBarVisibility="Auto">
                                    <ItemsControl Name="icNorms" Margin="0,0,6,0">
                                        <ItemsControl.ItemTemplate>
                                            <DataTemplate>
                                                <Border Background="#B3FFFFFF" BorderBrush="#66FFFFFF" BorderThickness="1" CornerRadius="12" Padding="12,8" Margin="0,0,0,6">
                                                    <Grid>
                                                        <Grid.ColumnDefinitions>
                                                            <ColumnDefinition Width="*"/>
                                                            <ColumnDefinition Width="110"/>
                                                        </Grid.ColumnDefinitions>
                                                        <StackPanel>
                                                            <TextBlock Text="{Binding Title}" FontWeight="SemiBold" TextWrapping="Wrap"/>
                                                            <TextBlock Text="{Binding Values}" FontSize="12" TextWrapping="Wrap" Margin="0,2,0,0"/>
                                                            <TextBlock Text="{Binding Source}" FontSize="11" Foreground="#FF8C7A6B" TextWrapping="Wrap" Margin="0,2,0,0"/>
                                                        </StackPanel>
                                                        <Border Grid.Column="1" Background="{Binding StatusBg}" CornerRadius="9" Padding="10,4" Margin="12,0,0,0" HorizontalAlignment="Left" VerticalAlignment="Top">
                                                            <TextBlock Text="{Binding StatusText}" Foreground="{Binding StatusFg}" FontSize="12" FontWeight="SemiBold"/>
                                                        </Border>
                                                    </Grid>
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
                                </Grid.ColumnDefinitions>
                                <TextBlock Name="tbNormsStatus" Grid.Column="0" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
                                <Button Name="btnOpenNorms" Grid.Column="1" Content="Открыть norms.json" Style="{StaticResource GlassButton}"/>
                            </Grid>
                        </Grid>
                    </Grid>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>"""

CONTROL_NAMES = (
    "tbSidebarParams", "tbSidebarDoc",
    "txtFop", "btnBrowseFop", "icParams", "tbParamsStatus", "btnCheckParams", "btnPrepare",
    "icShapes", "tbShapesStatus", "btnSaveShapes",
    "icNorms", "tbNormsStatus", "btnOpenNorms",
)
NAV_PAGES = (("navParams", "pageParams"), ("navShapes", "pageShapes"), ("navNorms", "pageNorms"))


class PrepareWindow(ui.KzhWindow):
    def __init__(self):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES, NAV_PAGES)
        self.tbSidebarDoc.Text = doc.Title
        self.txtFop.Text = settings.load("project").get("fop") or app.SharedParametersFilename or u""

        self.btnBrowseFop.Click += self.on_browse_fop
        self.txtFop.LostFocus += lambda s, e: self.remember_fop()
        self.btnCheckParams.Click += lambda s, e: self.refresh_params()
        self.btnPrepare.Click += self.on_prepare
        self.icShapes.AddHandler(Selector.SelectionChangedEvent, SelectionChangedEventHandler(self.on_shape_changed))
        self.btnSaveShapes.Click += self.on_save_shapes
        self.btnOpenNorms.Click += self.on_open_norms

        self.refresh_params()
        self.refresh_shapes()
        self.refresh_norms()

    # ----------------------------------------------------------
    # ПАРАМЕТРЫ
    # ----------------------------------------------------------
    def fop_path(self):
        return (self.txtFop.Text or u"").strip()

    def remember_fop(self):
        data = settings.load("project")
        data["fop"] = self.fop_path()
        settings.save("project", data)

    def on_browse_fop(self, sender, args):
        dialog = OpenFileDialog()
        dialog.Filter = u"Файл общих параметров (*.txt)|*.txt|Все файлы (*.*)|*.*"
        if dialog.ShowDialog():
            self.txtFop.Text = dialog.FileName
            self.remember_fop()
            self.refresh_params()

    def refresh_params(self):
        rows, cats, refused, fop_error = params.analyze_params(doc, app, self.fop_path())
        self.icParams.ItemsSource = rows
        bound = sum(1 for r in rows if r.state == 'ok')
        self.tbSidebarParams.Text = u"{} из {}".format(bound, len(rows))
        status = u"Привязано полностью: {} из {} • Категории: {}".format(
            bound, len(rows), u", ".join(c.Name for c in cats) or u"—")
        if refused:
            status += u" • Revit не разрешает: {}".format(u", ".join(refused))
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
        try:
            params.prepare_params(doc, app, fop)
        except Exception:
            show_message(u"Ошибка подготовки параметров:\n{}".format(traceback.format_exc()), title=u"Ошибка")
        self.refresh_params()
        self.refresh_shapes()
        missing = [r for r in self.shape_rows if r.missing]
        auto = [r for r in self.shape_rows if not r.Value]
        if missing:
            show_message(u"Формы из таблицы ролей не найдены в проекте — для этих ролей будет автоподбор. "
                         u"Загрузите формы в проект или выберите другие в разделе «Формы стержней».",
                         details=[u"{} — {}".format(r.Title, r.Value) for r in missing],
                         details_header=u"Нет в проекте: {}".format(len(missing)))
        elif auto:
            show_message(u"Формы стержней: для {} ролей включён автоподбор.".format(len(auto)), kind='info',
                         title=u"Формы стержней", details=[r.Title for r in auto],
                         details_header=u"Автоподбор: {}".format(len(auto)))

    # ----------------------------------------------------------
    # ФОРМЫ СТЕРЖНЕЙ
    # ----------------------------------------------------------
    def refresh_shapes(self):
        self.shape_names = sorted(kr.shapes_by_name(doc))
        roles = settings.shape_roles()
        self.shape_rows = [ShapeRow(key, title, roles.get(key, u""), self.shape_names)
                           for key, title in settings.SHAPE_ROLES]
        self.icShapes.ItemsSource = self.shape_rows
        self.update_shapes_status()

    def update_shapes_status(self):
        rows = self.shape_rows
        self.tbShapesStatus.Text = u"Форм в проекте: {} • Задано: {} • Автоподбор: {} • Нет в проекте: {}".format(
            len(self.shape_names), sum(1 for r in rows if r.Value and not r.missing),
            sum(1 for r in rows if not r.Value), sum(1 for r in rows if r.missing))

    def on_shape_changed(self, sender, args):
        """Выбор формы в строке: значки состояния обновятся после сохранения, строка итогов — сразу."""
        combo = args.OriginalSource
        row = getattr(combo, 'DataContext', None)
        opt = getattr(combo, 'SelectedItem', None)
        if not isinstance(row, ShapeRow) or opt is None:
            return
        row.Value = opt.Value
        row.SelectedIndex = combo.SelectedIndex
        row.missing = bool(opt.Value) and opt.Value not in self.shape_names
        self.update_shapes_status()

    def on_save_shapes(self, sender, args):
        settings.save_shape_roles(dict((r.Role, r.Value) for r in self.shape_rows))
        self.refresh_shapes()
        self.tbShapesStatus.Text = u"Таблица ролей сохранена • " + self.tbShapesStatus.Text

    # ----------------------------------------------------------
    # НОРМЫ
    # ----------------------------------------------------------
    def refresh_norms(self):
        rows = [NormRow(r) for r in norms.summary_rows()]
        self.icNorms.ItemsSource = rows
        info = norms.data().get('_info', {})
        self.tbNormsStatus.Text = u"Проверено по открытым источникам: {} • Сверить: {} из {}".format(
            info.get('checked', u"—"), sum(1 for r in rows if r.StatusText == u"Сверить"), len(rows))

    def on_open_norms(self, sender, args):
        try:
            Process.Start(norms.NORMS_FILE)
        except Exception as ex:
            show_message(u"Не удалось открыть файл:\n{}\n{}".format(norms.NORMS_FILE, ex))


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: подготовка выполняется в проекте, а не в семействе.")
        return
    PrepareWindow().show_modal()


try:
    main()
except Exception:
    show_message(u"Ошибка окна подготовки проекта:\n{}".format(traceback.format_exc()), title=u"Ошибка")

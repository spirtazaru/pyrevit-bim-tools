# -*- coding: utf-8 -*-
"""Линии КЖ (pyRevit / IronPython 2.7).

Окно со стилями линий: какие стили использованы на активном виде (сколько линий каждого) или все стили
проекта; вес пера, образец, цвет. Вес можно поменять и применить (с подтверждением списком).
Кнопка «Создать стили КЖ» добавляет стили КЖ_* с весами по ГОСТ 2.303 / 21.501 (существующие не меняет).
Вес — номер пера Revit: толщина на печати зависит от масштаба вида и таблицы «Толщины линий» проекта.
"""
__title__ = u"Линии"
__doc__ = u"Стили и толщины линий на виде: просмотр, правка веса, стили КЖ по ГОСТ"

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import traceback

from System.Windows.Controls import ComboBox, SelectionChangedEventHandler

import sys
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from pyrevit import DB
from kzh import lines, ui
from kzh.ui import show_message

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

WEIGHTS = [u"{}".format(i) for i in range(1, 17)]

XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Линии КЖ" Width="980" Height="700" MinWidth="760" MinHeight="480"
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
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="Auto"/>
                        <RowDefinition Height="*"/>
                        <RowDefinition Height="Auto"/>
                    </Grid.RowDefinitions>
                    <StackPanel Margin="4,0,4,12">
                        <TextBlock Text="Линии на виде" FontSize="26" FontWeight="Bold"/>
                        <TextBlock Name="tbNote" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"
                                   Text="Стили линий детализации и модели на активном виде. Вес — номер пера Revit (1–16): толщина на печати зависит от масштаба вида и таблицы «Толщины линий» (Управление → Дополнительные параметры). Стили КЖ — вверху списка."/>
                    </StackPanel>
                    <Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="16,10" Margin="0,0,0,12">
                        <WrapPanel>
                            <RadioButton Name="rbView" GroupName="scope" IsChecked="True" Style="{StaticResource ModePill}" Content="Стили на активном виде" Margin="0,0,8,0"/>
                            <RadioButton Name="rbAll" GroupName="scope" Style="{StaticResource ModePill}" Content="Все стили проекта" Margin="0,0,8,0"/>
                        </WrapPanel>
                    </Border>
                    <Border Grid.Row="2" Style="{StaticResource GlassCard}" Padding="14,10,6,10">
                        <Grid>
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                            </Grid.RowDefinitions>
                            <Grid Margin="12,4,18,8">
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="*"/><ColumnDefinition Width="90"/><ColumnDefinition Width="160"/>
                                    <ColumnDefinition Width="60"/><ColumnDefinition Width="110"/>
                                </Grid.ColumnDefinitions>
                                <TextBlock Text="СТИЛЬ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                <TextBlock Grid.Column="1" Text="ЛИНИЙ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                <TextBlock Grid.Column="2" Text="ОБРАЗЕЦ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                <TextBlock Grid.Column="3" Text="ЦВЕТ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                <TextBlock Grid.Column="4" Text="ВЕС ПЕРА" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                            </Grid>
                            <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
                                <ItemsControl Name="icLines" Margin="0,0,6,0">
                                    <ItemsControl.ItemTemplate>
                                        <DataTemplate>
                                            <Border Background="#B3FFFFFF" BorderBrush="#66FFFFFF" BorderThickness="1" CornerRadius="12" Padding="12,4" Margin="0,0,0,6">
                                                <Grid>
                                                    <Grid.ColumnDefinitions>
                                                        <ColumnDefinition Width="*"/><ColumnDefinition Width="90"/><ColumnDefinition Width="160"/>
                                                        <ColumnDefinition Width="60"/><ColumnDefinition Width="110"/>
                                                    </Grid.ColumnDefinitions>
                                                    <TextBlock Text="{Binding Name}" FontSize="13" FontWeight="SemiBold" VerticalAlignment="Center" TextTrimming="CharacterEllipsis" ToolTip="{Binding Name}"/>
                                                    <TextBlock Grid.Column="1" Text="{Binding Count}" FontSize="13" VerticalAlignment="Center"/>
                                                    <TextBlock Grid.Column="2" Text="{Binding Pattern}" FontSize="13" VerticalAlignment="Center" TextTrimming="CharacterEllipsis"/>
                                                    <Border Grid.Column="3" Width="22" Height="14" HorizontalAlignment="Left" BorderBrush="#66000000" BorderThickness="1" Background="{Binding Color}"/>
                                                    <ComboBox Grid.Column="4" Style="{StaticResource PillComboBox}" Width="90" HorizontalAlignment="Left"
                                                              ItemsSource="{Binding Weights}" SelectedItem="{Binding WeightText, Mode=OneTime}"/>
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
                            <ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/><ColumnDefinition Width="Auto"/>
                        </Grid.ColumnDefinitions>
                        <TextBlock Name="tbStatus" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
                        <Button Name="btnStyles" Grid.Column="1" Content="Создать стили КЖ" Style="{StaticResource GlassButton}" Margin="10,0,0,0"/>
                        <Button Name="btnApply" Grid.Column="2" Content="Применить веса" Style="{StaticResource PrimaryButton}" Margin="10,0,0,0"/>
                    </Grid>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>"""

CONTROL_NAMES = ("tbNote", "rbView", "rbAll", "icLines", "tbStatus", "btnStyles", "btnApply")


class LinesWindow(ui.KzhWindow):
    def __init__(self):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES)
        self.rows = []
        self.rbView.Checked += lambda s, e: self.refresh()
        self.rbAll.Checked += lambda s, e: self.refresh()
        self.btnStyles.Click += self.on_styles
        self.btnApply.Click += self.on_apply
        # Привязка к объектам IronPython односторонняя — выбор веса забирается из события
        self.icLines.AddHandler(ComboBox.SelectionChangedEvent, SelectionChangedEventHandler(self.on_weight))
        self.refresh()

    def refresh(self):
        view = doc.ActiveView
        self.rows = lines.view_line_rows(doc, view) if self.rbView.IsChecked else lines.all_style_rows(doc)
        for r in self.rows:
            r.Weights = WEIGHTS
            r.WeightText = u"{}".format(r.weight)
        self.icLines.ItemsSource = self.rows
        missing = lines.missing_styles(doc)
        scope = u"Вид «{}»: стилей {}".format(view.Name, len(self.rows)) if self.rbView.IsChecked else \
            u"Стилей в проекте: {}".format(len(self.rows))
        self.tbStatus.Text = scope + (u" • стилей КЖ нет: {}".format(len(missing)) if missing else u" • стили КЖ есть")
        self.btnStyles.IsEnabled = bool(missing)

    def on_weight(self, sender, args):
        box = args.OriginalSource
        row = getattr(box, 'DataContext', None)
        if isinstance(row, lines.LineRow) and box.SelectedItem is not None:
            row.new_weight = int(u"{}".format(box.SelectedItem))
            n = sum(1 for r in self.rows if r.new_weight != r.weight)
            self.tbStatus.Text = u"Изменено весов: {} — нажмите «Применить веса»".format(n)

    def on_styles(self, sender, args):
        missing = lines.missing_styles(doc)
        if not missing:
            return
        info = dict((s[0], s) for s in lines.KZH_STYLES)
        details = [u"{} — вес {}, {}".format(n, info[n][1], info[n][3]) for n in missing]
        if not show_message(u"Создать стили линий КЖ ({} шт.)? Существующие стили не меняются.".format(len(missing)),
                            ask=True, kind='info', details=details, details_header=u"Новые стили"):
            return
        t = DB.Transaction(doc, u"КЖ: стили линий")
        t.Start()
        try:
            lines.ensure_styles(doc)
            t.Commit()
        except Exception:
            t.RollBack()
            raise
        self.refresh()

    def on_apply(self, sender, args):
        changed = [r for r in self.rows if r.new_weight != r.weight]
        if not changed:
            show_message(u"Веса не менялись: выберите новый вес в строке стиля.", kind='info')
            return
        details = [u"{}: {} → {}".format(r.Name, r.weight, r.new_weight) for r in changed]
        if not show_message(u"Изменить вес пера у {} стилей линий? Изменится вид всех линий этих стилей "
                            u"во всём проекте.".format(len(changed)), ask=True, details=details,
                            details_header=u"Стилей: {}".format(len(changed))):
            return
        t = DB.Transaction(doc, u"КЖ: веса линий")
        t.Start()
        try:
            n = lines.apply_weights(changed)
            t.Commit()
        except Exception:
            t.RollBack()
            raise
        self.refresh()
        self.tbStatus.Text = u"Изменено стилей: {} • ".format(n) + self.tbStatus.Text


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: окно «Линии» работает в проекте.")
        return
    LinesWindow().show_modal()


try:
    main()
except Exception:
    show_message(u"Ошибка окна «Линии»:\n{}".format(traceback.format_exc()), title=u"Ошибка")

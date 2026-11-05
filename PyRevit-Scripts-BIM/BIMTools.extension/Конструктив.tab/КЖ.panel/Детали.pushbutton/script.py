# -*- coding: utf-8 -*-
"""Конфигуратор деталей КЖ (pyRevit / IronPython 2.7).

Библиотека гнутых стержней — хомуты, ромбы, шпильки, П, Г, лягушки, корыта: тип выбирается плиткой,
размеры правятся прямо на эскизе (графа ведомости деталей по ГОСТ 21.501). Библиотека хранится
в настройках плагина и доступна в любом проекте; хомуты, ромбы и шпильки ставятся щелчками
по стержням в окнах «Колонны» и «Балки».
"""
__title__ = u"Детали"
__author__ = u"Daniil Antonenko"
__doc__ = u"Конфигуратор деталей: хомуты, ромбы, шпильки, П, Г, лягушки, корыта — с размерами для ведомости деталей"

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import copy
import traceback

from System import Action

from System.Windows import Thickness, HorizontalAlignment, VerticalAlignment, FontStyles
from System.Windows.Controls import Canvas, RadioButton, StackPanel, TextBlock, TextBox, Orientation
from System.Windows.Input import Key
from System.Windows.Media import PointCollection
from System.Windows.Shapes import Polyline
from System.Windows import Point

import sys
# pyRevit держит модули lib в памяти движка между запусками кнопок: без сброса после обновления
# кнопка может взять старую версию kzh (ImportError: cannot import name ...)
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from kzh import details as kdet
from kzh import drawing as kd
from kzh import norms, ui
from kzh.ui import show_message, Option, select_option, selected_value, parse_number, number_text

XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Конфигуратор деталей" Width="1240" Height="760" MinWidth="1000" MinHeight="620"
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
                        <ColumnDefinition Width="250"/>
                        <ColumnDefinition Width="14"/>
                        <ColumnDefinition Width="*"/>
                        <ColumnDefinition Width="14"/>
                        <ColumnDefinition Width="270"/>
                    </Grid.ColumnDefinitions>

                    <!-- БИБЛИОТЕКА -->
                    <Border Grid.Column="0" Style="{StaticResource GlassCard}" Padding="14">
                        <Grid>
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <TextBlock Text="КЖ • БИБЛИОТЕКА ДЕТАЛЕЙ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="4,4,0,10"/>
                            <ListBox Name="lbLibrary" Grid.Row="1" Background="Transparent" BorderThickness="0"
                                     ScrollViewer.HorizontalScrollBarVisibility="Disabled"/>
                            <WrapPanel Grid.Row="2" Margin="0,10,0,0">
                                <Button Name="btnNew" Content="+ Новая" Style="{StaticResource GlassButton}" Margin="0,0,6,6"/>
                                <Button Name="btnCopy" Content="Копия" Style="{StaticResource GlassButton}" Margin="0,0,6,6"/>
                                <Button Name="btnDelete" Content="Удалить" Style="{StaticResource GlassButton}" Margin="0,0,6,6"/>
                            </WrapPanel>
                            <TextBlock Grid.Row="3" FontSize="11" Foreground="#FF8C7A6B" TextWrapping="Wrap"
                                       Text="Библиотека хранится в плагине, а не в проекте: она доступна в любом проекте."/>
                        </Grid>
                    </Border>

                    <!-- ТИП И ЭСКИЗ -->
                    <Grid Grid.Column="2">
                        <Grid.RowDefinitions>
                            <RowDefinition Height="Auto"/>
                            <RowDefinition Height="Auto"/>
                            <RowDefinition Height="*"/>
                            <RowDefinition Height="Auto"/>
                        </Grid.RowDefinitions>
                        <StackPanel Margin="4,0,4,10">
                            <TextBlock Text="Конфигуратор деталей" FontSize="26" FontWeight="Bold"/>
                            <TextBlock Text="Выберите тип, затем щёлкните по размеру на эскизе и введите значение. Пустое поле у хомута, ромба и шпильки — «по месту»: размер берётся из сечения элемента"
                                       FontSize="13" Foreground="#FF8C7A6B" TextWrapping="Wrap" Margin="0,2,0,0"/>
                        </StackPanel>
                        <WrapPanel Name="pnlKinds" Grid.Row="1" Margin="0,0,0,10"/>
                        <Border Grid.Row="2" Background="White" BorderBrush="#FF000000" BorderThickness="1" Margin="0,0,0,0">
                            <Grid>
                                <Grid.ColumnDefinitions>
                                    <ColumnDefinition Width="150"/>
                                    <ColumnDefinition Width="*"/>
                                    <ColumnDefinition Width="180"/>
                                </Grid.ColumnDefinitions>
                                <Border BorderBrush="#FF000000" BorderThickness="0,0,1,0">
                                    <TextBlock Name="tbPos" FontSize="17" TextWrapping="Wrap" TextAlignment="Center" Margin="8,0,8,0"
                                               HorizontalAlignment="Center" VerticalAlignment="Center"/>
                                </Border>
                                <Canvas Name="cnvSketch" Grid.Column="1" ClipToBounds="True"/>
                                <StackPanel Grid.Column="2" VerticalAlignment="Center" Margin="8,0,14,0">
                                    <TextBlock Name="tbBar" FontSize="17" HorizontalAlignment="Center"/>
                                    <Border BorderBrush="#FF000000" BorderThickness="0,1,0,0" Margin="0,3,0,3"/>
                                    <TextBlock Name="tbLength" FontSize="17" HorizontalAlignment="Center"/>
                                </StackPanel>
                            </Grid>
                        </Border>
                        <TextBlock Name="tbStatus" Grid.Row="3" Margin="4,10,4,0" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
                    </Grid>

                    <!-- ПАРАМЕТРЫ -->
                    <Border Grid.Column="4" Style="{StaticResource GlassCard}" Padding="16">
                        <Grid>
                            <Grid.RowDefinitions>
                                <RowDefinition Height="Auto"/>
                                <RowDefinition Height="*"/>
                                <RowDefinition Height="Auto"/>
                            </Grid.RowDefinitions>
                            <StackPanel>
                                <TextBlock Text="ПАРАМЕТРЫ ДЕТАЛИ" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="0,0,0,10"/>
                                <TextBlock Text="Имя" Style="{StaticResource FieldLabel}"/>
                                <TextBox Name="txtName" Style="{StaticResource PillTextBox}" Margin="0,4,0,8"/>
                                <Grid Margin="0,0,0,8">
                                    <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="110"/></Grid.ColumnDefinitions>
                                    <TextBlock Text="Диаметр, мм" Style="{StaticResource FieldLabel}"/>
                                    <ComboBox Name="cbD" Grid.Column="1" Style="{StaticResource PillComboBox}"/>
                                </Grid>
                                <Grid Margin="0,0,0,8">
                                    <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="110"/></Grid.ColumnDefinitions>
                                    <TextBlock Text="Класс" Style="{StaticResource FieldLabel}"/>
                                    <ComboBox Name="cbSteel" Grid.Column="1" Style="{StaticResource PillComboBox}"/>
                                </Grid>
                                <Grid Name="rowClose" Margin="0,0,0,8">
                                    <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="130"/></Grid.ColumnDefinitions>
                                    <TextBlock Text="Замыкание" Style="{StaticResource FieldLabel}"/>
                                    <ComboBox Name="cbClose" Grid.Column="1" Style="{StaticResource PillComboBox}"/>
                                </Grid>
                                <Grid Margin="0,0,0,8">
                                    <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="110"/></Grid.ColumnDefinitions>
                                    <TextBlock Text="Крюки" Style="{StaticResource FieldLabel}"/>
                                    <ComboBox Name="cbHook" Grid.Column="1" Style="{StaticResource PillComboBox}"/>
                                </Grid>
                                <Grid Margin="0,0,0,4">
                                    <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="110"/></Grid.ColumnDefinitions>
                                    <TextBlock Text="Длина крюка, мм" Style="{StaticResource FieldLabel}"/>
                                    <TextBox Name="txtHookLen" Grid.Column="1" Style="{StaticResource PillTextBox}"/>
                                </Grid>
                                <TextBlock FontSize="11" Foreground="#FF8C7A6B" TextWrapping="Wrap" Margin="0,0,0,10"
                                           Text="Пусто — по типу крюка Revit. Длина крюка в Revit задаётся у типа стержня: она применится ко всем стержням этого диаметра с таким крюком."/>
                                <TextBlock Name="tbMass" FontWeight="SemiBold"/>
                            </StackPanel>
                            <Button Name="btnSave" Grid.Row="2" Content="Сохранить деталь" Style="{StaticResource PrimaryButton}"/>
                        </Grid>
                    </Border>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>"""

CONTROL_NAMES = ("lbLibrary", "btnNew", "btnCopy", "btnDelete", "pnlKinds", "tbPos", "cnvSketch", "tbBar", "tbLength",
                 "tbStatus", "txtName", "cbD", "cbSteel", "rowClose", "cbClose", "cbHook", "txtHookLen",
                 "tbMass", "btnSave")
GOST_TEXT = kd.FONT


def kind_icon(kind):
    """Значок типа: эскиз детали, вписанный в 46×22 px."""
    pts, _ = kdet.sketch(kdet.new_detail(kind))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w, h = max(max(xs) - min(xs), 1.0), max(max(ys) - min(ys), 1.0)
    k = min(40.0 / w, 16.0 / h)
    cnv = Canvas()
    cnv.Width, cnv.Height = 46.0, 22.0
    pl = Polyline()
    coll = PointCollection()
    for x, y in pts:
        coll.Add(Point(3 + (x - min(xs)) * k + (40 - w * k) / 2.0, 19 - (y - min(ys)) * k - (16 - h * k) / 2.0))
    pl.Points = coll
    pl.Stroke = kd.brush(kd.BLACK)
    pl.StrokeThickness = 1.6
    cnv.Children.Add(pl)
    return cnv


class DetailsWindow(ui.KzhWindow):
    def __init__(self):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES)
        self.items = kdet.load_library()
        self.det = None
        self.dirty = False
        self._loading = False
        self.dim_boxes = {}

        self.cbD.ItemsSource = [Option(u"{}".format(d), d) for d in norms.diameters()]
        self.cbSteel.ItemsSource = [Option(s, s) for s in norms.steel_classes()]
        self.cbClose.ItemsSource = [Option(u"крюки", "hooks"), Option(u"нахлёст", "overlap")]
        self.cbHook.ItemsSource = [Option(u"нет" if a == 0 else u"{}°".format(a), a) for a in kdet.HOOK_ANGLES]

        self.kind_buttons = {}
        for kind, title, _, _ in kdet.KINDS:
            rb = RadioButton()
            rb.GroupName = "kind"
            rb.Style = self.window.FindResource("ModePill")
            rb.Height = 58.0
            rb.MinWidth = 76.0
            rb.Margin = Thickness(0, 0, 6, 6)
            sp = StackPanel()
            sp.Children.Add(kind_icon(kind))
            tb = TextBlock()
            tb.Text = title
            tb.FontSize = 12.0
            tb.FontFamily = GOST_TEXT
            tb.FontStyle = FontStyles.Italic
            tb.HorizontalAlignment = HorizontalAlignment.Center
            sp.Children.Add(tb)
            rb.Content = sp
            rb.Tag = kind
            rb.Checked += self.on_kind
            self.pnlKinds.Children.Add(rb)
            self.kind_buttons[kind] = rb

        for ctl in (self.txtName, self.txtHookLen):
            ctl.TextChanged += lambda s, e: self.on_param()
        for cb in (self.cbD, self.cbSteel, self.cbClose, self.cbHook):
            cb.SelectionChanged += lambda s, e: self.on_param()
        self.lbLibrary.SelectionChanged += self.on_select
        self.cnvSketch.SizeChanged += lambda s, e: self.draw()
        self.btnNew.Click += self.on_new
        self.btnCopy.Click += self.on_copy
        self.btnDelete.Click += self.on_delete
        self.btnSave.Click += self.on_save
        self.window.Closing += self.on_closing
        self.refresh_list(0)

    # ----------------------------------------------------------
    # БИБЛИОТЕКА
    # ----------------------------------------------------------
    def refresh_list(self, index):
        self._loading = True
        self.lbLibrary.ItemsSource = [Option(kdet.title(d), i) for i, d in enumerate(self.items)]
        self._loading = False
        self.lbLibrary.SelectedIndex = min(index, len(self.items) - 1) if self.items else -1

    def on_select(self, sender, args):
        if self._loading or self.lbLibrary.SelectedIndex < 0:
            return
        self.det = self.items[self.lbLibrary.SelectedIndex]
        self.load_detail()

    def load_detail(self):
        d = self.det
        self._loading = True
        try:
            self.kind_buttons[d["kind"]].IsChecked = True
            self.txtName.Text = d["name"]
            select_option(self.cbD, d["d"])
            select_option(self.cbSteel, d["steel"])
            select_option(self.cbClose, d.get("close") or "hooks")
            select_option(self.cbHook, d.get("hook") or 0)
            self.txtHookLen.Text = number_text(d.get("hook_len"))
        finally:
            self._loading = False
        self.update_visibility()
        self.draw()

    def update_visibility(self):
        from System.Windows import Visibility
        self.rowClose.Visibility = Visibility.Visible if self.det["kind"] == "stirrup" else Visibility.Collapsed

    def on_kind(self, sender, args):
        if self._loading or self.det is None:
            return
        kind = sender.Tag
        if kind == self.det["kind"]:
            return
        fresh = kdet.new_detail(kind)
        self.det["kind"] = kind
        self.det["dims"] = fresh["dims"]
        self.det["hook"] = fresh["hook"]
        self.dirty = True
        self.load_detail()

    def on_param(self):
        if self._loading or self.det is None:
            return
        d = self.det
        d["name"] = (self.txtName.Text or u"").strip() or d["name"]
        d["d"] = selected_value(self.cbD, d["d"])
        d["steel"] = selected_value(self.cbSteel, d["steel"])
        d["close"] = selected_value(self.cbClose, "hooks")
        d["hook"] = selected_value(self.cbHook, 0)
        d["hook_len"] = parse_number(self.txtHookLen.Text, None)
        self.dirty = True
        self.draw()

    def on_new(self, sender, args):
        self.items.append(kdet.new_detail("stirrup"))
        self.dirty = True
        self.refresh_list(len(self.items) - 1)

    def on_copy(self, sender, args):
        if self.det is None:
            return
        c = copy.deepcopy(self.det)
        c["name"] = c["name"] + u" (копия)"
        self.items.append(c)
        self.dirty = True
        self.refresh_list(len(self.items) - 1)

    def on_delete(self, sender, args):
        if self.det is None:
            return
        if not show_message(u"Удалить деталь «{}» из библиотеки?".format(self.det["name"]), ask=True):
            return
        i = self.items.index(self.det)
        self.items.remove(self.det)
        self.det = None
        self.dirty = True
        self.refresh_list(max(0, i - 1))

    def on_save(self, sender, args):
        names = [d["name"] for d in self.items]
        dup = sorted(set(n for n in names if names.count(n) > 1))
        if dup:
            show_message(u"Имена деталей должны быть разными: по имени деталь выбирают в окнах колонн и балок.",
                         details=dup, details_header=u"Повторяются: {}".format(len(dup)))
            return
        kdet.save_library(self.items)
        self.dirty = False
        self.refresh_list(self.lbLibrary.SelectedIndex)
        self.tbStatus.Text = u"Библиотека сохранена: деталей {}".format(len(self.items))

    def on_closing(self, sender, args):
        if self.dirty and show_message(u"Сохранить изменения библиотеки деталей?", ask=True):
            self.on_save(None, None)

    # ----------------------------------------------------------
    # ЭСКИЗ: ГРАФА ВЕДОМОСТИ ДЕТАЛЕЙ С РАЗМЕРАМИ-ПОЛЯМИ
    # ----------------------------------------------------------
    def draw(self):
        if self.det is None:
            return
        d = self.det
        pts, labels = kdet.sketch(d)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        m = max(w, h) * 0.12 + 20
        sk = kd.Sketch(self.cnvSketch, (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m), pad=40)
        sk.poly(pts, kd.MAIN, 2.0)
        self.dim_boxes = {}
        titles = dict((k, t) for k, t, _ in kdet.KIND_DIMS[d["kind"]])
        for x, y, key, vertical in labels:
            val = d["dims"].get(key)
            box = TextBox()
            box.Text = number_text(val) if val is not None else u""
            box.Tag = key
            box.FontFamily = GOST_TEXT
            box.FontStyle = FontStyles.Italic
            box.FontSize = 15.0
            box.MinWidth = 46.0
            box.Height = 26.0
            box.HorizontalContentAlignment = HorizontalAlignment.Center
            box.VerticalContentAlignment = VerticalAlignment.Center
            box.BorderThickness = Thickness(0, 0, 0, 1)
            box.BorderBrush = kd.brush("#FFFF7A1A")
            box.Background = kd.brush("#00FFFFFF")
            box.ToolTip = u"{} — {}".format(titles.get(key, key), u"пусто: по месту" if key in ("W", "H") or d["kind"] == "pin" else u"мм")
            if val is None:
                box.Text = u""
                box.ToolTip = u"{}: по месту (из сечения элемента)".format(titles.get(key, key))
            box.LostFocus += self.on_dim
            box.KeyDown += self.on_dim_key
            px, py = sk.X(x), sk.Y(y)
            Canvas.SetLeft(box, px - 23 + (-30 if vertical else 0))
            Canvas.SetTop(box, py - 30 if not vertical else py - 13)
            self.cnvSketch.Children.Add(box)
            if val is None:
                hint = TextBlock()
                hint.Text = u"(по месту)"
                hint.FontFamily = GOST_TEXT
                hint.FontStyle = FontStyles.Italic
                hint.FontSize = 11.0
                hint.IsHitTestVisible = False
                Canvas.SetLeft(hint, px - 30 + (-30 if vertical else 0))
                Canvas.SetTop(hint, (py - 30 if not vertical else py - 13) + 4)
                self.cnvSketch.Children.Add(hint)
            self.dim_boxes[key] = box
        self.tbPos.Text = d["name"]
        self.tbPos.FontFamily = GOST_TEXT
        self.tbPos.FontStyle = FontStyles.Italic
        hook = u", крюки {}°".format(d["hook"]) if d.get("hook") else u""
        self.tbBar.Text = u"⌀{} {}".format(d["d"], d["steel"])
        L = kdet.length(d)
        self.tbLength.Text = u"L = {:.0f}".format(float(L)) if L is not None else u"L — по месту"
        for tb in (self.tbBar, self.tbLength):
            tb.FontFamily = GOST_TEXT
            tb.FontStyle = FontStyles.Italic
        mass = kdet.mass(d)
        self.tbMass.Text = (u"Масса: {:.2f} кг{}".format(float(mass), hook) if mass is not None
                            else u"Масса — по месту{}".format(hook))

    def on_dim_key(self, sender, args):
        if args.Key == Key.Enter:
            self.on_dim(sender, args)

    def on_dim(self, sender, args):
        if self.det is None:
            return
        key = sender.Tag
        val = parse_number(sender.Text, None)
        if val is not None and val <= 0:
            val = None
        if val is None and key not in ("W", "H") and self.det["kind"] != "pin":
            val = dict((k, v) for k, _, v in kdet.KIND_DIMS[self.det["kind"]]).get(key)
        if self.det["dims"].get(key) != val:
            self.det["dims"][key] = val
            self.dirty = True
            self.window.Dispatcher.BeginInvoke(Action(self.draw))


def main():
    DetailsWindow().show_modal()


try:
    main()
except Exception:
    show_message(u"Ошибка конфигуратора деталей:\n{}".format(traceback.format_exc()), title=u"Ошибка")

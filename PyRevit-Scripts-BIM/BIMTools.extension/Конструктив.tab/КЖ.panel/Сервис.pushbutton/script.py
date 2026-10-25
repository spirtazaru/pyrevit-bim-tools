# -*- coding: utf-8 -*-
"""Сервис КЖ (pyRevit / IronPython 2.7).

Разделы окна:
  Проверка    — стержни вне бетона, защитный слой меньше нормы, шаг набора больше нормы; выделение в модели.
  Ведомость   — марки АрмМарка (одинаковые стержни — одна марка) и расход стали
                «конструкция × класс × диаметр» с учётом АрмЧисло; выгрузка в CSV.
  Обновление  — основы, изменённые после армирования (по отпечатку геометрии): повторный запуск
                кнопки армирования для отмеченных.
Область: выбранные основы или стержни; ничего не выбрано — вся модель.
"""
__title__ = u"Сервис"
__author__ = u"Daniil Antonenko"
__doc__ = u"Проверка армирования, марки и ведомость расхода стали, обновление арматуры изменённых элементов"

import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import traceback

from System.Collections.Generic import List
from System.Windows import RoutedEventHandler
from System.Windows.Controls.Primitives import ToggleButton
from Microsoft.Win32 import SaveFileDialog

import sys
# pyRevit держит модули lib в памяти движка между запусками кнопок: без сброса после обновления
# кнопка может взять старую версию kzh (ImportError: cannot import name ...)
for _name in [n for n in sys.modules if n == "kzh" or n.startswith("kzh.")]:
    del sys.modules[_name]

from pyrevit import DB
from kzh import norms, params, service
from kzh import ui
from kzh.ui import show_message, Option

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

KIND_TITLES = {'out': u"Вне бетона", 'cover': u"Защитный слой", 'spacing': u"Шаг"}


class IssueRow(object):
    """Одинаковые замечания одной основы — одна строка с числом элементов арматуры."""

    def __init__(self, issues):
        first = issues[0]
        self.Host = first.host_title
        self.Text = first.text
        self.Count = u"{}".format(len(issues))
        self.StatusText = KIND_TITLES[first.kind]
        self.StatusBg, self.StatusFg = params.STATUS_COLORS['err' if first.kind == 'out' else 'warn']


def issue_rows(issues):
    groups, order = {}, []
    for i in issues:
        key = (i.kind, i.host_title, i.text)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)
    kinds = ('out', 'cover', 'spacing')
    return [IssueRow(groups[k]) for k in sorted(order, key=lambda k: (kinds.index(k[0]), k[1], k[2]))]


def _row_list(name, columns, cells):
    """XAML списка карточек-строк: заголовки колонн и шаблон строки. columns — [(ширина, заголовок)]."""
    defs = u"".join(u'<ColumnDefinition Width="{}"/>'.format(w) for w, _ in columns)
    head = u"".join(u'<TextBlock Grid.Column="{}" Text="{}" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B"/>'
                    .format(i, t) for i, (_, t) in enumerate(columns))
    return u"""<Border Grid.Row="2" Style="{{StaticResource GlassCard}}" Padding="14,10,6,10">
    <Grid>
        <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
        </Grid.RowDefinitions>
        <Grid Margin="12,4,18,8">
            <Grid.ColumnDefinitions>{defs}</Grid.ColumnDefinitions>
            {head}
        </Grid>
        <ScrollViewer Grid.Row="1" VerticalScrollBarVisibility="Auto">
            <ItemsControl Name="{name}" Margin="0,0,6,0">
                <ItemsControl.ItemTemplate>
                    <DataTemplate>
                        <Border Background="#B3FFFFFF" BorderBrush="#66FFFFFF" BorderThickness="1" CornerRadius="12" Padding="12,6" Margin="0,0,0,6">
                            <Grid>
                                <Grid.ColumnDefinitions>{defs}</Grid.ColumnDefinitions>
                                {cells}
                            </Grid>
                        </Border>
                    </DataTemplate>
                </ItemsControl.ItemTemplate>
            </ItemsControl>
        </ScrollViewer>
    </Grid>
</Border>""".format(defs=defs, head=head, name=name, cells=cells)


def _pill(col, text="StatusText"):
    return (u'<Border Grid.Column="{}" Background="{{Binding StatusBg}}" CornerRadius="9" Padding="10,4" '
            u'HorizontalAlignment="Left" VerticalAlignment="Center"><TextBlock Text="{{Binding {}}}" '
            u'Foreground="{{Binding StatusFg}}" FontSize="12" FontWeight="SemiBold"/></Border>').format(col, text)


def _cell(col, binding, bold=False, extra=u""):
    return (u'<TextBlock Grid.Column="{}" Text="{{Binding {}}}" FontSize="13" VerticalAlignment="Center" '
            u'Margin="0,0,12,0" TextTrimming="CharacterEllipsis"{}{}/>').format(col, binding, u' FontWeight="SemiBold"' if bold else u"", extra)


def _page(name, visible, title, note, toolbar, body, footer):
    return u"""<Grid Name="{name}" Visibility="{vis}">
    <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
    </Grid.RowDefinitions>
    <StackPanel Grid.Row="0" Margin="4,0,4,12">
        <TextBlock Text="{title}" FontSize="26" FontWeight="Bold"/>
        <TextBlock Text="{note}" FontSize="13" Foreground="#FF8C7A6B" Margin="0,2,0,0" TextWrapping="Wrap"/>
    </StackPanel>
    {toolbar}
    {body}
    {footer}
</Grid>""".format(name=name, vis=u"Visible" if visible else u"Collapsed", title=title, note=note,
                  toolbar=toolbar, body=body, footer=footer)


def _footer(status, buttons):
    cols = u"".join(u'<ColumnDefinition Width="Auto"/>' for _ in buttons)
    btns = u"".join(u'<Button Name="{}" Grid.Column="{}" Content="{}" Style="{{StaticResource {}}}" Margin="10,0,0,0"/>'
                    .format(n, i + 1, t, s) for i, (n, t, s) in enumerate(buttons))
    return u"""<Grid Grid.Row="3" Margin="4,14,4,0">
    <Grid.ColumnDefinitions><ColumnDefinition Width="*"/>{cols}</Grid.ColumnDefinitions>
    <TextBlock Name="{status}" VerticalAlignment="Center" Foreground="#FF8C7A6B" FontWeight="SemiBold" TextWrapping="Wrap"/>
    {btns}
</Grid>""".format(cols=cols, status=status, btns=btns)


PAGE_CHECK = _page(
    u"pageCheck", True, u"Проверка армирования",
    u"Стержни вне бетона (выход в соседний бетонный элемент — не ошибка), защитный слой сбоку стержня меньше "
    u"нормы СП 63 для выбранных условий, шаг рабочих стержней стен и плит больше нормы",
    u"""<Border Grid.Row="1" Style="{StaticResource GlassCard}" Padding="16,10" Margin="0,0,0,12">
    <StackPanel Orientation="Horizontal">
        <TextBlock Text="Условия эксплуатации:" FontWeight="SemiBold" VerticalAlignment="Center" Margin="0,0,12,0"/>
        <ComboBox Name="cbCondition" Style="{StaticResource PillComboBox}" Width="460"/>
    </StackPanel>
</Border>""",
    _row_list(u"icIssues", [(u"300", u"ОСНОВА"), (u"*", u"ЗАМЕЧАНИЕ"), (u"110", u"ЭЛЕМЕНТОВ"), (u"150", u"ПРОВЕРКА")],
              _cell(0, u"Host", True, extra=u' ToolTip="{Binding Host}"') + _cell(1, u"Text", extra=u' ToolTip="{Binding Text}"') + _cell(2, u"Count") + _pill(3)),
    _footer(u"tbCheckStatus", [(u"btnSelectIssues", u"Выделить в модели", u"GlassButton"),
                               (u"btnCheck", u"Проверить", u"PrimaryButton")]))

PAGE_SCHEDULE = _page(
    u"pageSchedule", False, u"Марки и ведомость",
    u"Одинаковые стержни (конструкция, диаметр, класс, форма, длина) получают одну марку АрмМарка; нумерация — "
    u"в пределах конструкции. Количество и масса — с учётом АрмЧисло",
    u"",
    _row_list(u"icSchedule", [(u"*", u"КОНСТРУКЦИЯ"), (u"110", u"КЛАСС"), (u"90", u"ДИАМЕТР"), (u"110", u"КОЛ-ВО, ШТ"),
                              (u"110", u"ДЛИНА, М"), (u"110", u"МАССА, КГ")],
              _cell(0, u"Part", extra=u' FontWeight="{Binding FontWeight}"') + _cell(1, u"Class") + _cell(2, u"D") +
              _cell(3, u"Count") + _cell(4, u"Length") + _cell(5, u"Mass", extra=u' FontWeight="{Binding FontWeight}"')),
    _footer(u"tbScheduleStatus", [(u"btnCsv", u"Выгрузить CSV", u"GlassButton"),
                                  (u"btnMarks", u"Присвоить марки", u"PrimaryButton")]))

PAGE_UPDATE = _page(
    u"pageUpdate", False, u"Обновление армирования",
    u"Основы, у которых после армирования изменились размеры, тип или проёмы. Для отмеченных запускается кнопка "
    u"армирования (она предложит удалить старую арматуру и армировать заново с сохранёнными настройками). "
    u"Арматуру узлов у этих элементов затем обновите кнопкой «Узлы»",
    u"",
    _row_list(u"icHosts", [(u"40", u""), (u"*", u"ОСНОВА"), (u"170", u"КАТЕГОРИЯ"), (u"80", u"СТЕРЖНЕЙ"),
                           (u"90", u"КНОПКА"), (u"250", u"СОСТОЯНИЕ")],
              u'<CheckBox Grid.Column="0" IsChecked="{Binding IsChecked, Mode=OneTime}" Style="{StaticResource PillCheck}" VerticalAlignment="Center"/>' +
              _cell(1, u"Title", True, extra=u' ToolTip="{Binding Title}"') + _cell(2, u"Category") + _cell(3, u"Rebar") + _cell(4, u"Button") + _pill(5)),
    _footer(u"tbUpdateStatus", [(u"btnUpdate", u"Армировать отмеченные заново", u"PrimaryButton")]))

XAML_UI = u"""<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Сервис КЖ" Width="1180" Height="760" MinWidth="960" MinHeight="600"
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
                            <TextBlock Grid.Row="0" Text="КЖ • СЕРВИС" FontSize="11" FontWeight="Bold" Foreground="#FF8C7A6B" Margin="8,4,0,12"/>
                            <StackPanel Grid.Row="1">
                                <RadioButton Name="navCheck" GroupName="nav" IsChecked="True" Style="{StaticResource NavItem}" Content="Проверка"/>
                                <RadioButton Name="navSchedule" GroupName="nav" Style="{StaticResource NavItem}" Content="Марки и ведомость"/>
                                <RadioButton Name="navUpdate" GroupName="nav" Style="{StaticResource NavItem}" Content="Обновление"/>
                            </StackPanel>
                            <Border Grid.Row="2" Background="#FFFDF4EA" BorderBrush="#40FF7A1A" BorderThickness="1" CornerRadius="14" Padding="12,10">
                                <StackPanel>
                                    <TextBlock Text="ЭЛЕМЕНТОВ АРМАТУРЫ" FontSize="10" FontWeight="Bold" Foreground="#FF8C7A6B"/>
                                    <TextBlock Name="tbSidebarCount" Text="—" FontSize="24" FontWeight="Bold" Foreground="#FFFF6A00" Margin="0,2,0,0"/>
                                    <TextBlock Name="tbSidebarScope" Text="" FontSize="11" Foreground="#FF8C7A6B" TextWrapping="Wrap"/>
                                </StackPanel>
                            </Border>
                        </Grid>
                    </Border>

                    <!-- ПРАВАЯ РАБОЧАЯ ОБЛАСТЬ -->
                    <Grid Grid.Column="2">
__PAGES__
                    </Grid>
                </Grid>
            </Grid>
        </Border>
    </Grid>
</Window>""".replace(u"__PAGES__", PAGE_CHECK + PAGE_SCHEDULE + PAGE_UPDATE)

CONTROL_NAMES = (
    "tbSidebarCount", "tbSidebarScope",
    "cbCondition", "icIssues", "tbCheckStatus", "btnSelectIssues", "btnCheck",
    "icSchedule", "tbScheduleStatus", "btnCsv", "btnMarks",
    "icHosts", "tbUpdateStatus", "btnUpdate",
)
NAV_PAGES = (("navCheck", "pageCheck"), ("navSchedule", "pageSchedule"), ("navUpdate", "pageUpdate"))


class ServiceWindow(ui.KzhWindow):
    def __init__(self, rebars, scope_text):
        ui.KzhWindow.__init__(self, XAML_UI, CONTROL_NAMES, NAV_PAGES)
        self.rebars = rebars
        self.issues = []
        self.update_rows = None         # отмеченные основы для повторного армирования (после закрытия окна)
        self.tbSidebarCount.Text = u"{}".format(len(rebars))
        self.tbSidebarScope.Text = scope_text
        self.cbCondition.ItemsSource = [Option(u"{} — {} мм".format(t, c), k) for k, t, c in norms.cover_conditions()]
        self.cbCondition.SelectedIndex = 0
        self.tbCheckStatus.Text = u"Нажмите «Проверить»: проверка идёт по первому и последнему стержню каждого набора"

        self.btnCheck.Click += self.on_check
        self.btnSelectIssues.Click += self.on_select_issues
        self.btnMarks.Click += self.on_marks
        self.btnCsv.Click += self.on_csv
        self.btnUpdate.Click += self.on_update
        # Отметки строк: привязка к объектам IronPython односторонняя — состояние забирается из события
        self.icHosts.AddHandler(ToggleButton.CheckedEvent, RoutedEventHandler(self.on_host_check))
        self.icHosts.AddHandler(ToggleButton.UncheckedEvent, RoutedEventHandler(self.on_host_check))

        self.refresh_schedule()
        self.refresh_hosts()

    # ----------------------------------------------------------
    # ПРОВЕРКА
    # ----------------------------------------------------------
    def on_check(self, sender, args):
        condition = ui.selected_value(self.cbCondition, "indoor_normal")
        self.issues = service.check_rebar(doc, self.rebars, condition)
        self.icIssues.ItemsSource = issue_rows(self.issues)
        bad = len(set(i.rebar_id.IntegerValue for i in self.issues))
        counts = [u"{}: {}".format(KIND_TITLES[k], sum(1 for i in self.issues if i.kind == k))
                  for k in ('out', 'cover', 'spacing')]
        self.tbCheckStatus.Text = (u"Проверено элементов арматуры: {} • с замечаниями: {} • {}".format(
            len(self.rebars), bad, u" • ".join(counts)) if self.issues else
            u"Проверено элементов арматуры: {} • замечаний нет".format(len(self.rebars)))

    def on_select_issues(self, sender, args):
        if not self.issues:
            show_message(u"Замечаний нет: сначала нажмите «Проверить».", kind='info')
            return
        ids = List[DB.ElementId]()
        for i_id in sorted(set(i.rebar_id.IntegerValue for i in self.issues)):
            ids.Add(DB.ElementId(i_id))
        uidoc.Selection.SetElementIds(ids)
        self.window.Close()

    # ----------------------------------------------------------
    # МАРКИ И ВЕДОМОСТЬ
    # ----------------------------------------------------------
    def refresh_schedule(self):
        self.rows, self.total = service.schedule(doc, self.rebars)
        self.icSchedule.ItemsSource = self.rows
        self.marks = service.plan_marks(doc, self.rebars)
        self.tbScheduleStatus.Text = u"Всего стали: {:.1f} кг • марок: {}{}".format(
            self.total, len(self.marks), u"" if service.mark_param_ready(self.rebars) or not self.rebars
            else u" • АрмМарка не привязан — «Подготовка проекта»")

    def on_marks(self, sender, args):
        if not self.rebars:
            return
        if not service.mark_param_ready(self.rebars):
            show_message(u"Параметр АрмМарка не привязан к арматуре. Привяжите его кнопкой «Подготовка проекта».")
            return
        lines = []
        for key in sorted(self.marks, key=lambda k: (k[0], int(self.marks[k][0]))):
            mark, rbs = self.marks[key]
            lines.append(u"{} • марка {}: ⌀{:.0f} {} {}, длина {} мм — элементов {}".format(
                key[0], mark, key[1], key[2], key[3], key[4], len(rbs)))
        if not show_message(u"Записать марки АрмМарка в {} элементов арматуры?".format(len(self.rebars)), ask=True,
                            details=lines, details_header=u"Марок: {}".format(len(self.marks))):
            return
        t = DB.Transaction(doc, u"КЖ: марки арматуры")
        t.Start()
        try:
            changed = service.apply_marks(doc, self.marks)
            t.Commit()
        except Exception:
            t.RollBack()
            raise
        self.tbScheduleStatus.Text = u"Марки записаны: изменено элементов {} • ".format(changed) + self.tbScheduleStatus.Text

    def on_csv(self, sender, args):
        dialog = SaveFileDialog()
        dialog.Filter = u"CSV (разделитель «;») (*.csv)|*.csv"
        dialog.FileName = u"Ведомость стали — {}.csv".format(doc.Title)
        if not dialog.ShowDialog():
            return
        try:
            service.write_csv(dialog.FileName, self.rows, self.total)
            self.tbScheduleStatus.Text = u"Выгружено: {}".format(dialog.FileName)
        except Exception as ex:
            show_message(u"Не удалось записать файл:\n{}".format(ex))

    # ----------------------------------------------------------
    # ОБНОВЛЕНИЕ
    # ----------------------------------------------------------
    def refresh_hosts(self):
        self.host_rows = service.host_rows(doc, self.rebars)
        self.icHosts.ItemsSource = self.host_rows
        n = dict((s, sum(1 for r in self.host_rows if r.state == s)) for s in ('changed', 'none', 'ok'))
        self.tbUpdateStatus.Text = u"Основ с арматурой: {} • изменены: {} • без отпечатка: {} • без изменений: {}".format(
            len(self.host_rows), n['changed'], n['none'], n['ok'])

    def on_host_check(self, sender, args):
        box = args.OriginalSource
        row = getattr(box, 'DataContext', None)
        if isinstance(row, service.HostRow):
            row.IsChecked = bool(box.IsChecked)

    def on_update(self, sender, args):
        rows = [r for r in self.host_rows if r.IsChecked and r.cmd]
        if not rows:
            show_message(u"Отметьте основы, которые нужно армировать заново.", kind='info')
            return
        lines = [u"{} — кнопка «{}»".format(r.Title, r.Button) for r in rows]
        if not show_message(u"Армировать заново {} основ? Для каждой группы откроется окно кнопки армирования "
                            u"с этими элементами; старую арматуру она удалит только после подтверждения.".format(len(rows)),
                            ask=True, details=lines, details_header=u"Основ: {}".format(len(rows))):
            return
        self.update_rows = rows
        self.window.Close()


def rerun_buttons(rows):
    """Запуск кнопок армирования по группам: выделение основ группы и команда кнопки."""
    from pyrevit.loader import sessionmgr
    groups = []
    for r in rows:
        for g in groups:
            if g[0] == r.cmd:
                g[2].append(r.host.Id)
                break
        else:
            groups.append((r.cmd, r.Button, [r.host.Id]))
    failed = []
    for cmd, title, ids in groups:
        uidoc.Selection.SetElementIds(List[DB.ElementId](ids))
        try:
            sessionmgr.execute_command(cmd)
        except Exception as ex:
            failed.append(u"«{}»: {}".format(title, ex))
    if failed:
        show_message(u"Не удалось запустить кнопки армирования.", details=failed)


def main():
    if doc.IsFamilyDocument:
        show_message(u"Откройте проект: сервис КЖ работает в проекте, а не в семействе.")
        return
    selected = list(uidoc.Selection.GetElementIds())
    rebars = service.collect_rebar(doc, selected)
    if not rebars:
        show_message(u"Арматуры нет{}.".format(u" у выбранных элементов" if selected else u" в модели"), kind='info')
        return
    scope = u"Выбрано элементов: {}".format(len(selected)) if selected else u"Вся модель • {}".format(doc.Title)
    win = ServiceWindow(rebars, scope)
    win.show_modal()
    if win.update_rows:
        rerun_buttons(win.update_rows)


try:
    main()
except Exception:
    show_message(u"Ошибка сервиса КЖ:\n{}".format(traceback.format_exc()), title=u"Ошибка")

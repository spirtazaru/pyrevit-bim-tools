# -*- coding: utf-8 -*-
"""Редактор сечения в стиле ЛИРА: рисунок сечения и поля ввода вокруг него с выносками к стержням.

Используется окнами армирования колонн и балок. Рисунок — kzh.drawing.Sketch на одном Canvas,
поля и выноски — на втором Canvas поверх него (поля создаются один раз, выноски перерисовываются).

Группы полей задаются кодом окна: build(groups), где группа — (ключ, сторона, [части]) — одна строка полей,
или (ключ, сторона, {"title": заголовок, "note": пояснение, "rows": [[части], [части]]}) — карточка: заголовок,
короткое пояснение и несколько строк полей. Выноска одна на группу: от внутренней кромки колонки полей к точке
рисунка, поэтому выноски не пересекают поля (карточки одной колонки выравниваются по ширине).
Часть: ("num", ключ_параметра, ширина), ("diam", ключ_параметра), ("combo", ключ_параметра, [Option]),
("button", ключ, подпись) — кнопка (окно подписывается на self.buttons[ключ].Click), ("panel", ключ) — пустая
панель, которую окно заполняет само (self.panels[ключ], например список поставленных деталей),
("hint", ключ_параметра) — серая подпись, которую окно меняет через set_hint (например, значение по норме
для пустого поля), или просто текст-подпись. Сторона: "L" — слева, "R" — справа, "B" — снизу, "T" — сверху
(снизу и сверху — рядами под целями выносок, с переносом, если не помещаются по ширине).
"""
import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

import System
from System import Double
from System.Windows import HorizontalAlignment, Size, Thickness, Visibility, VerticalAlignment
from System.Windows import CornerRadius, TextWrapping
from System.Windows.Controls import Border, Button, Canvas, ComboBox, Orientation, StackPanel, TextBlock, TextBox

from kzh import drawing as kd
from kzh import norms
from kzh.ui import Option, number_text, parse_number, select_option, selected_value

LEADER = "#FFFF7A1A"      # выноски от карточек полей — элемент интерфейса, не чертежа: оранжевые
GAP = 8.0             # минимальный зазор между полями одной стороны, px
EDGE = 10.0           # отступ полей от края холста, px
NOTE_W = 250.0        # ширина пояснения в карточке, px
LABEL_W = 104.0       # ширина подписи строки в карточке (поля строк — в одну колонку), px


def _measure(panel):
    """Размер поля заново: после скрытия пояснений или смены ширины прежний DesiredSize может остаться старым."""
    child = getattr(panel, 'Child', None)
    if child is not None:
        child.InvalidateMeasure()
    panel.InvalidateMeasure()
    panel.Measure(Size(Double.PositiveInfinity, Double.PositiveInfinity))


class SectionEditor(object):
    """owner — окно (для стилей), cnv_draw — холст рисунка, cnv_fields — холст полей поверх него."""

    def __init__(self, owner, cnv_draw, cnv_fields, on_change):
        self.owner = owner
        self.cnv_draw = cnv_draw
        self.cnv_fields = cnv_fields
        self.on_change = on_change
        self.groups = {}       # ключ группы -> (сторона, StackPanel)
        self.inputs = {}       # ключ параметра -> (вид, элемент)
        self.hints = {}        # ключ параметра -> серая подпись
        self.titles = {}       # ключ карточки -> заголовок
        self.notes = {}        # ключ карточки -> пояснение (скрывается, если колонка не помещается по высоте)
        self.buttons = {}      # ключ -> Button
        self.panels = {}       # ключ -> StackPanel, заполняемая окном
        self._leaders = []
        self._loading = False

    # ----------------------------------------------------------
    # ПОСТРОЕНИЕ ПОЛЕЙ
    # ----------------------------------------------------------
    def _style(self, name):
        return self.owner.FindResource(name)

    def _label(self, text):
        tb = TextBlock()
        tb.Text = text
        tb.FontSize = 12.0
        tb.VerticalAlignment = VerticalAlignment.Center
        tb.Margin = Thickness(4, 0, 4, 0)
        tb.Foreground = kd.brush("#FF2B2118")
        return tb

    def _num(self, key, width):
        tb = TextBox()
        tb.Style = self._style('PillTextBox')
        tb.Width = width
        tb.Height = 30.0
        tb.FontSize = 12.0
        tb.Padding = Thickness(4, 0, 4, 0)
        tb.HorizontalContentAlignment = HorizontalAlignment.Center
        tb.TextChanged += lambda s, e: self._changed()
        self.inputs[key] = ("num", tb)
        return tb

    def _combo(self, key, options, width):
        cb = ComboBox()
        cb.Style = self._style('PillComboBox')
        cb.Width = width
        cb.Height = 30.0
        cb.ItemsSource = options
        cb.SelectionChanged += lambda s, e: self._changed()
        self.inputs[key] = ("combo", cb)
        return cb

    def _row(self, parts, side, stack, card):
        """Строка полей в stack; подсказки ("hint", ключ) — отдельной строкой под ней."""
        row = StackPanel()
        row.Orientation = Orientation.Horizontal
        if card:
            row.Margin = Thickness(0, 4, 0, 0)
        stack.Children.Add(row)
        first = True
        for part in parts:
            if isinstance(part, tuple):
                kind = part[0]
                if kind == "num":
                    row.Children.Add(self._num(part[1], part[2] if len(part) > 2 else 52.0))
                elif kind == "hint":
                    hint = self._label(u"")
                    hint.FontSize = 11.0
                    hint.Margin = Thickness(4, 1, 4, 0)
                    hint.HorizontalAlignment = HorizontalAlignment.Right if (side == "L" and not card) else HorizontalAlignment.Left
                    hint.Foreground = kd.brush("#FF6B5B4E" if card else LEADER)
                    if card:
                        hint.TextWrapping = TextWrapping.Wrap
                        hint.MaxWidth = NOTE_W + 40
                    self.hints[part[1]] = hint
                    stack.Children.Add(hint)
                elif kind == "button":
                    btn = Button()
                    btn.Style = self._style('GlassButton')
                    btn.Content = part[2]
                    btn.Height = 32.0
                    btn.FontSize = 12.0
                    self.buttons[part[1]] = btn
                    row.Children.Add(btn)
                elif kind == "panel":
                    panel = StackPanel()
                    panel.Margin = Thickness(0, 2, 0, 0)
                    self.panels[part[1]] = panel
                    stack.Children.Add(panel)
                elif kind == "diam":
                    opts = [Option(u"{}".format(d), d) for d in norms.diameters()]
                    row.Children.Add(self._combo(part[1], opts, 84.0))
                else:
                    row.Children.Add(self._combo(part[1], part[2], part[3] if len(part) > 3 else 170.0))
            else:
                label = self._label(part)
                if card and first and len(parts) > 1:
                    label.Margin = Thickness(0, 0, 4, 0)
                    label.MinWidth = LABEL_W
                row.Children.Add(label)
            first = False

    def build(self, groups):
        """groups: [(ключ, сторона, части или карточка)]. Вызывается один раз для каждого набора полей."""
        for key, side, parts in groups:
            if isinstance(parts, dict):
                panel = Border()
                panel.CornerRadius = CornerRadius(12)
                panel.BorderThickness = Thickness(1)
                panel.BorderBrush = kd.brush("#66FF7A1A")
                panel.Background = kd.brush("#FAFFFCF8")
                panel.Padding = Thickness(10, 6, 10, 8)
                stack = StackPanel()
                panel.Child = stack
                title = self._label(parts["title"])
                title.Margin = Thickness(0)
                title.FontSize = 12.5
                title.FontWeight = System.Windows.FontWeights.Bold
                stack.Children.Add(title)
                self.titles[key] = title
                if parts.get("note"):
                    note = self._label(parts["note"])
                    note.Margin = Thickness(0, 1, 0, 0)
                    note.FontSize = 10.5
                    note.TextWrapping = TextWrapping.Wrap
                    note.MaxWidth = NOTE_W
                    note.HorizontalAlignment = HorizontalAlignment.Left
                    note.Foreground = kd.brush("#FF8C7A6B")
                    stack.Children.Add(note)
                    self.notes[key] = note
                for parts_row in parts["rows"]:
                    self._row(parts_row, side, stack, True)
            else:
                panel = StackPanel()          # строка полей и (если есть) подсказка под ней
                panel.Background = kd.brush("#F2FFFFFF")
                self._row(parts, side, panel, False)
            self.cnv_fields.Children.Add(panel)
            self.groups[key] = (side, panel)

    def set_title(self, key, text):
        if key in self.titles:
            self.titles[key].Text = text

    def set_note(self, key, text):
        if key in self.notes:
            self.notes[key].Text = text

    def set_hint(self, key, text):
        if key in self.hints:
            self.hints[key].Text = text
            # Пустая подсказка не занимает места (в карточках они отдельными строками)
            self.hints[key].Visibility = Visibility.Visible if text else Visibility.Collapsed

    def show_only(self, keys):
        for key, (side, panel) in self.groups.items():
            panel.Visibility = Visibility.Visible if key in keys else Visibility.Collapsed

    # ----------------------------------------------------------
    # ЗНАЧЕНИЯ
    # ----------------------------------------------------------
    def _changed(self):
        if not self._loading:
            self.on_change()

    def load(self, values):
        self._loading = True
        try:
            for key, (kind, ctl) in self.inputs.items():
                if key not in values:
                    continue
                if kind == "num":
                    ctl.Text = number_text(values[key])
                else:
                    select_option(ctl, values[key])
        finally:
            self._loading = False

    def read(self, values, keys):
        """Значения полей keys -> values (числа — float или None)."""
        for key in keys:
            kind, ctl = self.inputs[key]
            values[key] = parse_number(ctl.Text) if kind == "num" else selected_value(ctl)

    # ----------------------------------------------------------
    # РАЗМЕЩЕНИЕ ПОЛЕЙ И ВЫНОСКИ
    # ----------------------------------------------------------
    def pad(self, top=48.0, bottom=70.0, gap=36.0):
        """Поля рисунка (слева, сверху, справа, снизу) под самые широкие видимые поля каждой стороны.
        Поля сверху ("T") и снизу ("B") в несколько рядов увеличивают отступ рисунка на высоту рядов."""
        widths = {"L": 0.0, "R": 0.0}
        for side, panel in self.groups.values():
            if side in widths and panel.Visibility == Visibility.Visible:
                panel.Width = Double.NaN
                _measure(panel)
                widths[side] = max(widths[side], panel.DesiredSize.Width)
        # Карточки колонки — одной ширины: выноски выходят с общей кромки и не пересекают соседние поля
        for side, panel in self.groups.values():
            if side in widths and panel.Visibility == Visibility.Visible and isinstance(panel, Border):
                panel.Width = widths[side]
                _measure(panel)
        rows_t = self._rows("T")
        rows_b = self._rows("B")
        if rows_t:
            top = max(top, EDGE + sum(r[1] for r in rows_t) + GAP * (len(rows_t) - 1) + gap)
        if len(rows_b) > 1:
            bottom = max(bottom, EDGE + sum(r[1] for r in rows_b) + GAP * (len(rows_b) - 1) + gap)
        return (EDGE + widths["L"] + gap, top, EDGE + widths["R"] + gap, bottom)

    def _rows(self, side, items=None):
        """Ряды полей стороны "T"/"B" с переносом по ширине холста: [(элементы, высота ряда)].
        items — [(цель, панель, выноска)] в порядке слева направо; без items — все видимые поля стороны."""
        if items is None:
            items = []
            for s, panel in self.groups.values():
                if s == side and panel.Visibility == Visibility.Visible:
                    _measure(panel)
                    items.append((None, panel, False))
        avail = max(self.cnv_fields.ActualWidth - 2 * EDGE, 100.0)
        rows, cur, width = [], [], 0.0
        for it in items:
            pw = it[1].DesiredSize.Width
            if cur and width + GAP + pw > avail:
                rows.append(cur)
                cur, width = [], 0.0
            width += (GAP if cur else 0.0) + pw
            cur.append(it)
        if cur:
            rows.append(cur)
        return [(r, max(it[1].DesiredSize.Height for it in r)) for r in rows]


    # ----------------------------------------------------------
    def place(self, targets):
        """targets: {ключ группы: (x, y) или None} — точки рисунка (px), к которым ведут выноски
        (None — поле без выноски: указывать не на что; (x, y, False) — поле на высоте y без выноски).
        Поля каждой стороны выстраиваются без наложений."""
        for ln in self._leaders:
            self.cnv_fields.Children.Remove(ln)
        self._leaders = []
        w = self.cnv_fields.ActualWidth
        h = self.cnv_fields.ActualHeight
        if w < 60 or h < 60:
            return
        by_side = {"L": [], "R": [], "B": [], "T": []}
        for key, pt in targets.items():
            if key in self.groups:
                side, panel = self.groups[key]
                if panel.Visibility == Visibility.Visible:
                    _measure(panel)
                    if pt is None:
                        by_side[side].append(((w / 2.0, h / 2.0), panel, False))
                    else:
                        # (x, y) — выноска к точке; (x, y, False) — только место по высоте, без выноски
                        by_side[side].append(((pt[0], pt[1]), panel, len(pt) < 3 or pt[2]))
        for side in ("L", "R"):
            # Колонка не помещается по высоте — пояснения карточек скрываются, остаются заголовки и поля
            keys = [k for k, (sd, pn) in self.groups.items() if sd == side and k in self.notes]
            for k in keys:
                self.notes[k].Visibility = Visibility.Visible
            for _, panel, _ in by_side[side]:
                _measure(panel)
            total = sum(it[1].DesiredSize.Height for it in by_side[side]) + GAP * max(len(by_side[side]) - 1, 0)
            if total > h - 2 * EDGE:
                for k in keys:
                    self.notes[k].Visibility = Visibility.Collapsed
                for _, panel, _ in by_side[side]:
                    _measure(panel)
            # По высоте цели; при равной высоте — слева по возрастанию x, справа по убыванию (выноски не перекрещиваются)
            sign = 1.0 if side == "L" else -1.0
            items = sorted(by_side[side], key=lambda it: (round(it[0][1], 1), sign * it[0][0]))
            # Сверху вниз — без наложений; если низ не поместился — второй проход снизу вверх
            ys, y_next = [], EDGE
            for (tx, ty), panel, has_leader in items:
                ph = panel.DesiredSize.Height
                ys.append(max(ty - ph / 2.0, y_next))
                y_next = ys[-1] + ph + GAP
            y_limit = h - EDGE
            for k in range(len(items) - 1, -1, -1):
                ph = items[k][1].DesiredSize.Height
                ys[k] = max(EDGE, min(ys[k], y_limit - ph))
                y_limit = ys[k] - GAP
            # Третий проход сверху вниз: верхние поля, упёршиеся в край, не должны наезжать друг на друга
            y_next = EDGE
            for k in range(len(items)):
                ys[k] = max(ys[k], y_next)
                y_next = ys[k] + items[k][1].DesiredSize.Height + GAP
            for ((tx, ty), panel, has_leader), y in zip(items, ys):
                pw, ph = panel.DesiredSize.Width, panel.DesiredSize.Height
                x = EDGE if side == "L" else w - pw - EDGE
                Canvas.SetLeft(panel, x)
                Canvas.SetTop(panel, y)
                edge_x = x + pw if side == "L" else x
                if has_leader:
                    self._leader([(edge_x, y + ph / 2.0), (edge_x + (12 if side == "L" else -12), y + ph / 2.0), (tx, ty)])
        # Сверху и снизу — ряды слева направо по положению целей, с переносом; снизу последний ряд — у края
        for side in ("T", "B"):
            rows = self._rows(side, sorted(by_side[side], key=lambda it: it[0][0]))
            heights = [rh for _, rh in rows]
            for k, (row, rh) in enumerate(rows):
                if side == "T":
                    y_row = EDGE + sum(heights[:k]) + GAP * k
                else:
                    y_row = h - EDGE - sum(heights[k:]) - GAP * (len(rows) - 1 - k)
                xs, x_next = [], EDGE
                for (tx, ty), panel, has_leader in row:
                    pw = panel.DesiredSize.Width
                    xs.append(max(tx - pw / 2.0, x_next))
                    x_next = xs[-1] + pw + GAP
                over = x_next - GAP - (w - EDGE)
                if over > 0:
                    xs = [max(EDGE, x - over) for x in xs]
                for ((tx, ty), panel, has_leader), x in zip(row, xs):
                    pw, ph = panel.DesiredSize.Width, panel.DesiredSize.Height
                    y = y_row if side == "T" else y_row + rh - ph
                    Canvas.SetLeft(panel, x)
                    Canvas.SetTop(panel, y)
                    if has_leader:
                        self._leader([(x + pw / 2.0, y + ph if side == "T" else y), (tx, ty)])

    def _leader(self, pts):
        from System.Windows import Point
        from System.Windows.Media import PointCollection
        from System.Windows.Shapes import Ellipse, Polyline
        pl = Polyline()
        coll = PointCollection()
        for x, y in pts:
            coll.Add(Point(x, y))
        pl.Points = coll
        pl.Stroke = kd.brush(LEADER)
        pl.StrokeThickness = 1.0
        self.cnv_fields.Children.Insert(0, pl)
        self._leaders.append(pl)
        dot = Ellipse()
        dot.Width = dot.Height = 5.0
        dot.Fill = kd.brush(LEADER)
        Canvas.SetLeft(dot, pts[-1][0] - 2.5)
        Canvas.SetTop(dot, pts[-1][1] - 2.5)
        self.cnv_fields.Children.Insert(0, dot)
        self._leaders.append(dot)


class ZoneEditor(SectionEditor):
    """Зоны хомутов снизу вверх на виде по высоте: строка на зону (длина, шаг S, Ø, удалить) и кнопка «+ зона».
    Пустая длина — зона занимает остаток высоты (такая зона может быть только одна)."""

    def __init__(self, owner, cnv_draw, cnv_fields, on_change):
        SectionEditor.__init__(self, owner, cnv_draw, cnv_fields, on_change)
        self.zones = []
        self.btn_add = Button()
        self.btn_add.Style = self._style('GlassButton')
        self.btn_add.Content = u"+ зона"
        self.btn_add.Height = 30.0
        self.btn_add.Click += lambda s, e: self._on_add()
        self.cnv_fields.Children.Add(self.btn_add)
        self.groups["zone_add"] = ("R", self.btn_add)

    def set_zones(self, zones):
        """Пересоздаёт строки полей под список зон [{"len": мм или None, "step": мм, "d": мм}]."""
        for key in [k for k in self.groups if k.startswith("zone_") and k != "zone_add"]:
            self.cnv_fields.Children.Remove(self.groups.pop(key)[1])
        for key in [k for k in self.inputs if k.startswith("z_")]:
            del self.inputs[key]
        self.zones = [dict(z) for z in zones]
        groups = []
        for i in range(len(self.zones)):
            groups.append((u"zone_{}".format(i), "R", [
                u"Зона {}".format(i + 1), ("num", u"z_len_{}".format(i), 58), u"мм  S",
                ("num", u"z_step_{}".format(i), 48), u"Ø", ("diam", u"z_d_{}".format(i)), ("hint", u"z_{}".format(i))]))
        self.build(groups)
        for i in range(len(self.zones)):
            btn = Button()
            btn.Style = self._style('GlassButton')
            btn.Content = u"✕"
            btn.Width = btn.Height = 30.0
            btn.Padding = Thickness(0)
            btn.ToolTip = u"Удалить зону"
            btn.Click += lambda s, e, i=i: self._on_remove(i)
            self.groups[u"zone_{}".format(i)][1].Children[0].Children.Add(btn)
        values = {}
        for i, z in enumerate(self.zones):
            values[u"z_len_{}".format(i)] = z.get("len")
            values[u"z_step_{}".format(i)] = z.get("step")
            values[u"z_d_{}".format(i)] = z.get("d")
        self.load(values)

    def read_zones(self):
        """Поля -> self.zones. Возвращает текст ошибки или None."""
        values = {}
        keys = []
        for i in range(len(self.zones)):
            keys += [u"z_len_{}".format(i), u"z_step_{}".format(i), u"z_d_{}".format(i)]
        self.read(values, keys)
        fill = 0
        for i, z in enumerate(self.zones):
            z["len"] = values[u"z_len_{}".format(i)]
            z["step"] = values[u"z_step_{}".format(i)]
            z["d"] = values[u"z_d_{}".format(i)]
            if z["len"] is None:
                fill += 1
            elif z["len"] <= 0:
                return u"Зона {}: длина должна быть больше нуля (или пусто — остаток высоты).".format(i + 1)
            if not z["step"] or z["step"] <= 0:
                return u"Зона {}: укажите шаг хомутов больше нуля.".format(i + 1)
        if fill > 1:
            return u"Пустая длина (остаток высоты) может быть только у одной зоны."
        return None

    def _on_add(self):
        self.read_zones()
        last = self.zones[-1] if self.zones else {"step": 200.0, "d": 8}
        self.zones.append({"len": 500.0, "step": last.get("step") or 200.0, "d": last.get("d") or 8})
        self.set_zones(self.zones)
        self.on_change()

    def _on_remove(self, index):
        self.read_zones()
        if len(self.zones) > 1:
            del self.zones[index]
            self.set_zones(self.zones)
            self.on_change()

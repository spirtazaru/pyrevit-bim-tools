# -*- coding: utf-8 -*-
"""Установка деталей из конфигуратора щелчками по стержням сечения — общее для окон «Колонны» и «Балки».

Окно добавляет в SECTION_FIELDS карточку CARD (ключ "details"), создаёт DetailPicker и после каждой
перерисовки сечения вызывает show(sketch, bars, items): bars — стержни сечения [(x, y, Ø)] в координатах
рисунка, items — список поставленных деталей участка ({"name", "bars": [номера]}), который хранится в настройках.
"""
import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System.Windows import Thickness, VerticalAlignment
from System.Windows.Controls import Button, Dock, DockPanel, TextBlock

from kzh import details as kdet
from kzh import drawing as kd
from kzh.ui import Option, selected_value, show_message

CARD = ("details", "R", {
    "title": u"Детали в сечении",
    "note": u"Щёлкните по стержням на рисунке, выберите деталь из библиотеки «Детали» и нажмите «Поставить». "
            u"Хомут и ромб охватывают выбранные стержни, шпилька — между двумя.",
    "rows": [[("combo", "det_pick", [], 240)],
             [("button", "det_add", u"Поставить по выбранным"), ("button", "det_clear", u"Снять выбор")],
             [("panel", "det_list")], [("hint", "det")]]})


class DetailPicker(object):
    """window — WPF-окно (стили кнопок), editor — SectionEditor с карточкой CARD, canvas — холст рисунка,
    get_items() -> список деталей текущего участка (изменяется на месте), refresh() — перерисовать окно."""

    def __init__(self, window, editor, canvas, library, get_items, refresh):
        self.window = window
        self.editor = editor
        self.canvas = canvas
        self.library = library
        self.get_items = get_items
        self.refresh = refresh
        self.selected = []
        self.sketch = None
        self.bars = []
        placeable = [d for d in library if d["kind"] in kdet.PLACEABLE]
        pick = editor.inputs["det_pick"][1]
        pick.ItemsSource = [Option(kdet.title(d), d["name"]) for d in placeable]
        pick.SelectedIndex = 0 if placeable else -1
        editor.buttons["det_add"].Click += self.on_add
        editor.buttons["det_clear"].Click += self.on_clear
        canvas.Background = kd.brush("#01FFFFFF")         # прозрачный фон ловит щелчки
        canvas.MouseLeftButtonDown += self.on_click

    def reset(self):
        """Смена группы или участка — выбор стержней сбрасывается."""
        self.selected = []

    def draw(self, sk, bars, items):
        """Рисует поставленные детали и выделенные стержни. Возвращает точку выноски карточки (px) или None."""
        self.sketch, self.bars = sk, bars
        placed, notes = kdet.placed(items, bars, self.library)
        for det, pts, closed, _ in placed:
            sk.poly(pts, kd.STIRRUP, 2.0)
        for i in self.selected:
            if i < len(bars):
                sk.ring(bars[i][0], bars[i][1], max(bars[i][2] / 2.0 * sk.k + 5.0, 8.0))
        self._fill(items, placed, notes)
        if not placed:
            return None
        q = max(placed[-1][1], key=lambda p: p[0])
        return sk.X(q[0]), sk.Y(q[1])

    def _fill(self, items, placed, notes):
        panel = self.editor.panels["det_list"]
        panel.Children.Clear()
        for det, pts, closed, k in placed:
            row = DockPanel()
            row.Margin = Thickness(0, 2, 0, 0)
            btn = Button()
            btn.Content = u"×"
            btn.Width = 28.0
            btn.Height = 26.0
            btn.Padding = Thickness(0)
            btn.Style = self.window.FindResource("GlassButton")
            btn.Click += (lambda idx: (lambda s, e: self.on_remove(idx)))(k)
            DockPanel.SetDock(btn, Dock.Right)
            row.Children.Add(btn)
            tb = TextBlock()
            tb.Text = u"{} • стержней {}".format(kdet.title(det), len(items[k]["bars"]))
            tb.FontSize = 11.5
            tb.VerticalAlignment = VerticalAlignment.Center
            row.Children.Add(tb)
            panel.Children.Add(row)
        hint = u"Выбрано стержней: {}".format(len(self.selected)) if self.selected else u"Щёлкните по стержням на рисунке"
        if notes:
            hint += u" • " + u"; ".join(notes)
        self.editor.set_hint("det", hint)

    def on_click(self, sender, args):
        sk = self.sketch
        if sk is None or not self.bars:
            return
        pos = args.GetPosition(self.canvas)
        best, dist = None, None
        for i, (x, y, d) in enumerate(self.bars):
            dd = ((sk.X(x) - pos.X) ** 2 + (sk.Y(y) - pos.Y) ** 2) ** 0.5
            if dd <= max(14.0, d / 2.0 * sk.k + 4.0) and (dist is None or dd < dist):
                best, dist = i, dd
        if best is None:
            return
        if best in self.selected:
            self.selected.remove(best)
        else:
            self.selected.append(best)
        self.refresh()

    def on_clear(self, sender, args):
        self.selected = []
        self.refresh()

    def on_add(self, sender, args):
        det = kdet.find(self.library, selected_value(self.editor.inputs["det_pick"][1]))
        if det is None:
            show_message(u"В библиотеке нет хомутов, ромбов и шпилек. Добавьте их кнопкой «Детали».")
            return
        _, _, err = kdet.place_in_section(det, [self.bars[i] for i in self.selected if i < len(self.bars)])
        if err:
            show_message(u"Деталь «{}»: {}.".format(det["name"], err))
            return
        self.get_items().append({"name": det["name"], "bars": list(self.selected)})
        self.selected = []
        self.refresh()

    def on_remove(self, index):
        items = self.get_items()
        if 0 <= index < len(items):
            del items[index]
        self.refresh()

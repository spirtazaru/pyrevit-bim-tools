# -*- coding: utf-8 -*-
"""Предпросмотр армирования в окнах КЖ: рисунок сечения и вида по высоте/длине на WPF Canvas.

Координаты рисунка — миллиметры модели, ось Y вверх; Sketch вписывает их в размер холста.
Оформление — по СПДС (ГОСТ 21.501, ГОСТ 2.303, ГОСТ 2.307): всё чёрное, контур бетона — тонкая линия,
арматура — сплошная основная, стержни в сечении — залитые круги, размеры с засечками 45°,
шрифт чертёжный наклонный. Роли MAIN/CORNER/SECOND/STIRRUP оставлены для вызовов кнопок,
на чертеже они не различаются цветом.
"""
import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System import Double
from System.Windows import Point, Size, FontWeights, FontStyles, Rect
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Media import (SolidColorBrush, ColorConverter, DoubleCollection, PointCollection, RotateTransform,
                                  FontFamily, DrawingBrush, GeometryDrawing, LineGeometry, Pen, TileMode,
                                  BrushMappingMode, PenLineJoin, PenLineCap)
from System.Windows.Shapes import Ellipse, Line, Polygon, Polyline

# Роли линий в вызовах кнопок. Значения — ключи ролей; на чертеже все роли рисуются чёрным (см. ink)
BLACK = "#FF000000"
CONCRETE = "#FFFFFFFF"
CONCRETE_EDGE = "#FF000001"    # контур бетона — тонкая линия
SLAB = "#FFFFFFFE"
MAIN = "#FF000002"             # основные стержни
CORNER = "#FF000003"           # угловые стержни в сечении
SECOND = "#FF000004"           # промежуточные стержни
STIRRUP = "#FF000005"          # хомуты и шпильки
DIM = BLACK                    # размеры и подписи
WARN = "#FFE8452C"             # текст ошибок в окне (не элемент чертежа)
HATCH = "hatch"                # заливка разрезанного элемента — штриховка 45°
CUT = "#FFE6E2DE"              # заливка, которой кнопки помечают разрезанный элемент (колонна на плане узла)
ROLES = (CONCRETE_EDGE, MAIN, CORNER, SECOND, STIRRUP)

# Толщины линий (px) по ГОСТ 2.303: тонкая — контур бетона, размеры; основная — арматура
THIN = 0.8
MAIN_LINE = 2.0
# Шрифт чертёжный (ГОСТ 2.304): если на компьютере нет GOST/ISOCPEUR — обычный наклонный
FONT = FontFamily("GOST A, GOST type A, ISOCPEUR, Arial")

_brushes = {}


def dashes():
    dc = DoubleCollection()
    dc.Add(4.0)
    dc.Add(3.0)
    return dc


def brush(color):
    if color not in _brushes:
        if color == HATCH:
            _brushes[color] = _hatch()
        else:
            _brushes[color] = SolidColorBrush(ColorConverter.ConvertFromString(color))
    return _brushes[color]


def _hatch():
    """Штриховка сечения 45° тонкими линиями с шагом 6 px."""
    pen = Pen(SolidColorBrush(ColorConverter.ConvertFromString(BLACK)), 0.6)
    gd = GeometryDrawing(None, pen, LineGeometry(Point(0, 6), Point(6, 0)))
    b = DrawingBrush(gd)
    b.TileMode = TileMode.Tile
    b.Viewport = Rect(0, 0, 6, 6)
    b.ViewportUnits = BrushMappingMode.Absolute
    b.Viewbox = Rect(0, 0, 6, 6)
    b.ViewboxUnits = BrushMappingMode.Absolute
    return b


def _fill(color):
    """Заливки чертежа: разрезанный элемент — штриховка, полупрозрачные зоны — без заливки, остальное — белое."""
    if color is None:
        return None
    if color == HATCH:
        return HATCH
    if color.startswith("#") and len(color) == 9 and int(color[1:3], 16) < 0x80:
        return None
    return HATCH if color.upper() == CUT else CONCRETE


def ink(color):
    """Цвет на чертеже: роли и размеры — чёрные; WARN остаётся (текст ошибки)."""
    return BLACK if color in ROLES or color == DIM else color


def weight(color, thick):
    """Толщина по роли: контур бетона — тонкая; арматура — основная, если вызов просит заметную линию
    (тонкие линии видов — частые хомуты, сетки на плане — остаются тонкими)."""
    if color == CONCRETE_EDGE:
        return THIN
    if color in ROLES:
        return MAIN_LINE if thick >= 1.4 else max(THIN, thick)
    return thick


class Sketch(object):
    """Рисунок в Canvas. bounds — (xmin, ymin, xmax, ymax) в мм; рисунок центрируется в области
    холста за вычетом полей pad: число (одинаково со всех сторон) или (слева, сверху, справа, снизу)."""

    def __init__(self, canvas, bounds, pad=34, min_width_px=None, min_height_px=None):
        """min_width_px — для высоких элементов (колонны): по горизонтали масштаб растягивается, чтобы ширина
        рисунка была не меньше этого числа пикселей; min_height_px — то же по вертикали для длинных (балки)."""
        canvas.Children.Clear()
        self.canvas = canvas
        w, h = canvas.ActualWidth, canvas.ActualHeight
        pl, pt, pr, pb = pad if isinstance(pad, tuple) else (pad, pad, pad, pad)
        aw, ah = w - pl - pr, h - pt - pb
        self.ok = aw > 40 and ah > 40
        x0, y0, x1, y1 = bounds
        dx, dy = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        self.k = min(aw / dx, ah / dy) if self.ok else 1.0
        self.kx = self.k
        if min_width_px and self.ok and dx * self.k < min_width_px:
            self.kx = min(min_width_px / dx, aw / dx)
        if min_height_px and self.ok and dy * self.k < min_height_px:
            self.k = min(min_height_px / dy, ah / dy)
        self.ox = pl + (aw - dx * self.kx) / 2.0 - x0 * self.kx
        self.oy = pt + (ah + dy * self.k) / 2.0 + y0 * self.k

    def X(self, x):
        return self.ox + x * self.kx

    def Y(self, y):
        return self.oy - y * self.k

    def _add(self, shape):
        self.canvas.Children.Add(shape)
        return shape

    def line(self, x0, y0, x1, y1, color=MAIN, thick=1.5, dash=False):
        if not self.ok:
            return
        ln = Line()
        ln.X1, ln.Y1, ln.X2, ln.Y2 = self.X(x0), self.Y(y0), self.X(x1), self.Y(y1)
        ln.Stroke = brush(ink(color))
        ln.StrokeThickness = weight(color, thick)
        if dash:
            ln.StrokeDashArray = dashes()
        self._add(ln)

    def poly(self, pts, color=MAIN, thick=1.5, fill=None, closed=False, dash=False):
        if not self.ok or len(pts) < 2:
            return
        shape = Polygon() if closed else Polyline()
        coll = PointCollection()
        for x, y in pts:
            coll.Add(Point(self.X(x), self.Y(y)))
        shape.Points = coll
        shape.Stroke = brush(ink(color))
        shape.StrokeThickness = weight(color, thick)
        shape.StrokeLineJoin = PenLineJoin.Round
        if _fill(fill) is not None:
            shape.Fill = brush(_fill(fill))
        if dash:
            shape.StrokeDashArray = dashes()
        self._add(shape)

    def rect(self, x0, y0, x1, y1, color=CONCRETE_EDGE, thick=1.5, fill=None, dash=False):
        self.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], color, thick, fill, True, dash)

    def circle(self, x, y, r, fill=MAIN, min_px=3.0):
        if not self.ok:
            return
        rp = max(r * min(self.k, self.kx), min_px)
        e = Ellipse()
        e.Width = e.Height = 2 * rp
        e.Fill = brush(ink(fill))
        Canvas.SetLeft(e, self.X(x) - rp)
        Canvas.SetTop(e, self.Y(y) - rp)
        self._add(e)

    def text(self, x, y, s, color=DIM, size=11.0, bold=False, align="center", dx=0.0, dy=0.0, vertical=False):
        """Подпись в точке (x, y): align — center | left | right относительно точки, dx/dy — сдвиг в пикселях.
        vertical — текст снизу вверх, центр — в точке (для вертикальных размеров)."""
        if not self.ok:
            return
        tb = TextBlock()
        tb.Text = s
        tb.FontSize = size + 1.0
        tb.FontFamily = FONT
        tb.FontStyle = FontStyles.Italic
        tb.Foreground = brush(ink(color))
        if bold:
            tb.FontWeight = FontWeights.Bold
        tb.Measure(Size(Double.PositiveInfinity, Double.PositiveInfinity))
        tw, th = tb.DesiredSize.Width, tb.DesiredSize.Height
        if vertical:
            tb.RenderTransformOrigin = Point(0.5, 0.5)
            tb.RenderTransform = RotateTransform(-90)
            Canvas.SetLeft(tb, self.X(x) + dx - tw / 2.0)
            Canvas.SetTop(tb, self.Y(y) + dy - th / 2.0)
            self._add(tb)
            return
        left = self.X(x) + dx - (tw / 2.0 if align == "center" else (tw if align == "right" else 0.0))
        Canvas.SetLeft(tb, left)
        Canvas.SetTop(tb, self.Y(y) + dy - th / 2.0)
        self._add(tb)

    def _px_line(self, x0, y0, x1, y1, color=DIM, thick=THIN):
        """Отрезок в пикселях холста (для засечек и выносных линий постоянного размера)."""
        ln = Line()
        ln.X1, ln.Y1, ln.X2, ln.Y2 = x0, y0, x1, y1
        ln.Stroke = brush(color)
        ln.StrokeThickness = thick
        self._add(ln)

    def tick(self, x, y, size_px=4.0):
        """Засечка размера под 45° (как на чертежах СПДС)."""
        if self.ok:
            px, py = self.X(x), self.Y(y)
            self._px_line(px - size_px, py + size_px, px + size_px, py - size_px, DIM, 1.3)

    def dim_line(self, x0, y0, x1, y1):
        """Размерная линия без подписи прямо по точкам: засечки 45° и выход линии за засечки на 3 px."""
        if not self.ok:
            return
        ax, ay, bx, by = self.X(x0), self.Y(y0), self.X(x1), self.Y(y1)
        length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 or 1.0
        ux, uy = (bx - ax) / length * 3.0, (by - ay) / length * 3.0
        self._px_line(ax - ux, ay - uy, bx + ux, by + uy, DIM, THIN)
        self.tick(x0, y0)
        self.tick(x1, y1)

    def cover_dim(self, x0, y0, x1, y1, label, left=False):
        """Короткий размер (защитный слой) прямо по точкам с подписью: у вертикального — справа, у горизонтального — сверху;
        left — подпись слева от начала размера (снаружи сечения, чтобы не столкнуться с соседней подписью)."""
        if not self.ok:
            return
        self.dim_line(x0, y0, x1, y1)
        if left:
            self.text(min(x0, x1), y0, label, size=10.5, align="right", dx=-6)
        elif abs(self.X(x1) - self.X(x0)) < abs(self.Y(y1) - self.Y(y0)):
            self.text(x0, (y0 + y1) / 2.0, label, size=10.5, align="left", dx=7)
        else:
            self.text((x0 + x1) / 2.0, y0, label, size=10.5, dy=-10)

    def dim(self, x0, y0, x1, y1, label, offset_px=14.0, outside=None):
        """Размер между точками: выносные линии, размерная линия со сдвигом наружу, засечки 45° и подпись.
        Горизонтальный — сверху (outside="below" — снизу), вертикальный — слева (outside="right" — справа)."""
        if not self.ok:
            return
        horizontal = abs(self.Y(y1) - self.Y(y0)) < abs(self.X(x1) - self.X(x0))
        kk = self.k if horizontal else self.kx     # сдвиг наружу — поперёк размерной линии
        off = offset_px / kk
        ext = 3.0 / kk            # выносная линия заходит за размерную на 3 px
        if horizontal:
            below = outside == "below"
            yy = min(y0, y1) - off if below else max(y0, y1) + off
            for x, y in ((x0, y0), (x1, y1)):
                self.line(x, y, x, yy - ext if below else yy + ext, DIM, THIN)
            self.dim_line(x0, yy, x1, yy)
            self.text((x0 + x1) / 2.0, yy, label, dy=9 if below else -9)
        else:
            right = outside == "right"
            xx = max(x0, x1) + off if right else min(x0, x1) - off
            for x, y in ((x0, y0), (x1, y1)):
                self.line(x, y, xx + ext if right else xx - ext, y, DIM, THIN)
            self.dim_line(xx, y0, xx, y1)
            self.text(xx, (y0 + y1) / 2.0, label, dx=8 if right else -8, vertical=True)

    def leader(self, x, y, top, bottom=u"", shelf_px=46.0, dx_px=26.0, dy_px=-22.0):
        """Выноска позиции по СПДС: от точки (x, y) наклонная линия к полке; над полкой — номер позиции,
        под полкой — шаг или количество. Возвращает (x, y) полки в пикселях."""
        if not self.ok:
            return None
        px, py = self.X(x), self.Y(y)
        qx, qy = px + dx_px, py + dy_px
        right = dx_px >= 0
        ex = qx + (shelf_px if right else -shelf_px)
        self._px_line(px, py, qx, qy, DIM, THIN)
        self._px_line(qx, qy, ex, qy, DIM, THIN)
        dot = Ellipse()
        dot.Width = dot.Height = 3.0
        dot.Fill = brush(BLACK)
        Canvas.SetLeft(dot, px - 1.5)
        Canvas.SetTop(dot, py - 1.5)
        self._add(dot)
        mid = (qx + ex) / 2.0
        self._label_px(mid, qy - 8.0, top, 12.0)
        if bottom:
            self._label_px(mid, qy + 8.0, bottom, 10.0)
        return qx, qy

    def _label_px(self, cx, cy, s, size):
        tb = TextBlock()
        tb.Text = s
        tb.FontSize = size
        tb.FontFamily = FONT
        tb.FontStyle = FontStyles.Italic
        tb.Foreground = brush(BLACK)
        tb.Measure(Size(Double.PositiveInfinity, Double.PositiveInfinity))
        Canvas.SetLeft(tb, cx - tb.DesiredSize.Width / 2.0)
        Canvas.SetTop(tb, cy - tb.DesiredSize.Height / 2.0)
        self._add(tb)

    def title(self, x, y, s, size=15.0):
        """Обозначение сечения или вида («1 – 1») с подчёркиванием, центр — в точке (x, y)."""
        if not self.ok:
            return
        tb = TextBlock()
        tb.Text = s
        tb.FontSize = size
        tb.FontFamily = FONT
        tb.FontStyle = FontStyles.Italic
        tb.Foreground = brush(BLACK)
        tb.Measure(Size(Double.PositiveInfinity, Double.PositiveInfinity))
        w, h = tb.DesiredSize.Width, tb.DesiredSize.Height
        Canvas.SetLeft(tb, self.X(x) - w / 2.0)
        Canvas.SetTop(tb, self.Y(y) - h / 2.0)
        self._add(tb)
        self._px_line(self.X(x) - w / 2.0 - 2, self.Y(y) + h / 2.0, self.X(x) + w / 2.0 + 2, self.Y(y) + h / 2.0, DIM, THIN)

    def break_line(self, x0, y0, x1, y1):
        """Линия обрыва (ГОСТ 2.303) от (x0, y0) до (x1, y1) с изломом посередине. Контур элемента под ней
        закрашивается белым: в месте обрыва видна только линия обрыва."""
        if not self.ok:
            return
        ax, ay, bx, by = self.X(x0), self.Y(y0), self.X(x1), self.Y(y1)
        self._px_line(ax, ay, bx, by, CONCRETE, 2.5)
        length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 or 1.0
        ux, uy = (bx - ax) / length, (by - ay) / length
        nx, ny = -uy, ux
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        pts = [(ax - ux * 4, ay - uy * 4), (mx - ux * 5, my - uy * 5), (mx - ux * 2 + nx * 6, my - uy * 2 + ny * 6),
               (mx + ux * 2 - nx * 6, my + uy * 2 - ny * 6), (mx + ux * 5, my + uy * 5), (bx + ux * 4, by + uy * 4)]
        shape = Polyline()
        coll = PointCollection()
        for qx, qy in pts:
            coll.Add(Point(qx, qy))
        shape.Points = coll
        shape.Stroke = brush(BLACK)
        shape.StrokeThickness = THIN
        self._add(shape)

    def ring(self, x, y, r_px, color="#FFFF7A1A"):
        """Пунктирное кольцо вокруг точки — выделение в окне (элемент интерфейса, не чертежа)."""
        if not self.ok:
            return
        e = Ellipse()
        e.Width = e.Height = 2 * r_px
        e.Stroke = brush(color)
        e.StrokeThickness = 1.6
        e.StrokeDashArray = dashes()
        e.IsHitTestVisible = False
        Canvas.SetLeft(e, self.X(x) - r_px)
        Canvas.SetTop(e, self.Y(y) - r_px)
        self._add(e)

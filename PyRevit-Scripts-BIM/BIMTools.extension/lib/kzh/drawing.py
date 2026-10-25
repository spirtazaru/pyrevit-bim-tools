# -*- coding: utf-8 -*-
"""Предпросмотр армирования в окнах КЖ: рисунок сечения и вида по высоте/длине на WPF Canvas.

Координаты рисунка — миллиметры модели, ось Y вверх; Sketch вписывает их в размер холста.
"""
import clr
clr.AddReference('PresentationFramework')
clr.AddReference('PresentationCore')
clr.AddReference('WindowsBase')

from System import Double
from System.Windows import Point, Size, FontWeights
from System.Windows.Controls import Canvas, TextBlock
from System.Windows.Media import SolidColorBrush, ColorConverter, DoubleCollection, PointCollection, RotateTransform
from System.Windows.Shapes import Ellipse, Line, Polygon, Polyline

# Палитра предпросмотра: чертёжная — бетон белый с чёрным контуром, размеры и выноски чёрные
CONCRETE = "#FFFFFFFF"
CONCRETE_EDGE = "#FF000000"
SLAB = "#FFFFFFFF"
MAIN = "#FF2B2118"         # основные стержни на видах
CORNER = "#FF2E9E4F"       # угловые стержни в сечении (как в ЛИРА)
SECOND = "#FFFF7A1A"       # промежуточные стержни
STIRRUP = "#FF2A6FDB"      # хомуты и шпильки
DIM = "#FF000000"          # размеры и подписи
WARN = "#FFE8452C"

_brushes = {}


def dashes():
    dc = DoubleCollection()
    dc.Add(4.0)
    dc.Add(3.0)
    return dc


def brush(color):
    if color not in _brushes:
        _brushes[color] = SolidColorBrush(ColorConverter.ConvertFromString(color))
    return _brushes[color]


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
        ln.Stroke = brush(color)
        ln.StrokeThickness = thick
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
        shape.Stroke = brush(color)
        shape.StrokeThickness = thick
        if fill is not None:
            shape.Fill = brush(fill)
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
        e.Fill = brush(fill)
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
        tb.FontSize = size
        tb.Foreground = brush(color)
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

    def _px_line(self, x0, y0, x1, y1, color=DIM, thick=1.0):
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
            self._px_line(px - size_px, py + size_px, px + size_px, py - size_px, DIM, 1.4)

    def dim_line(self, x0, y0, x1, y1):
        """Размерная линия без подписи прямо по точкам: засечки 45° и выход линии за засечки на 3 px."""
        if not self.ok:
            return
        ax, ay, bx, by = self.X(x0), self.Y(y0), self.X(x1), self.Y(y1)
        length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 or 1.0
        ux, uy = (bx - ax) / length * 3.0, (by - ay) / length * 3.0
        self._px_line(ax - ux, ay - uy, bx + ux, by + uy, DIM, 1.0)
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
                self.line(x, y, x, yy - ext if below else yy + ext, DIM, 0.8)
            self.dim_line(x0, yy, x1, yy)
            self.text((x0 + x1) / 2.0, yy, label, dy=9 if below else -9)
        else:
            right = outside == "right"
            xx = max(x0, x1) + off if right else min(x0, x1) - off
            for x, y in ((x0, y0), (x1, y1)):
                self.line(x, y, xx + ext if right else xx - ext, y, DIM, 0.8)
            self.dim_line(xx, y0, xx, y1)
            self.text(xx, (y0 + y1) / 2.0, label, dx=8 if right else -8, vertical=True)

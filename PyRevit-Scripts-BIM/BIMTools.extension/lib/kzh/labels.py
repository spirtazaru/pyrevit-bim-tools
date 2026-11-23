# -*- coding: utf-8 -*-
"""Раскладка подписей-позиций на чертеже без пересечений (чистая геометрия, без Revit API).

Подписи стоят снаружи рисунка: продольные стержни — колонкой справа, поперечные (хомуты, шпильки) — рядом
сверху; размеры — слева и снизу, поэтому подписи с ними не встречаются. Внутри колонки (ряда) подписи
упорядочены по положению цели и раздвинуты не меньше чем на высоту (ширину) подписи с зазором — подписи
не налезают друг на друга, а выноски одной колонки не перекрещиваются (порядок целей и полок совпадает).
Координаты — в мм модели; размеры подписи задаются в тех же единицах.
"""


class Label(object):
    """Подпись: target — точка выноски (x, y), text, w × h — габарит текста на полке.
    После layout(): shelf — начало полки (x, y), side — "R" или "T"."""

    def __init__(self, target, text, w, h):
        self.target = target
        self.text = text
        self.w = w
        self.h = h
        self.shelf = None
        self.side = None

    def box(self):
        """Габарит подписи (текст над полкой): (x0, y0, x1, y1)."""
        x, y = self.shelf
        return (x, y, x + self.w, y + self.h)


def _spread(values, sizes, gap, lo):
    """Позиции по возрастанию от желаемых values: не меньше lo и без наложений (size + gap)."""
    out, nxt = [], lo
    for v, s in zip(values, sizes):
        p = max(v, nxt)
        out.append(p)
        nxt = p + s + gap
    return out


def layout(labels, box, margin, gap):
    """Раскладывает подписи вокруг прямоугольника рисунка box = (x0, y0, x1, y1).
    side у подписи задан заранее ("R" — колонка справа, "T" — ряд сверху). Возвращает labels."""
    x0, y0, x1, y1 = box
    # Цель ряда сверху правее целей колонки — выноска пересекла бы выноски колонки: такая подпись — в колонку
    rx = [l.target[0] for l in labels if l.side == "R"]
    for l in labels:
        if l.side == "T" and rx and l.target[0] >= min(rx) - gap / 2.0:
            l.side = "R"
    right = sorted([l for l in labels if l.side == "R"], key=lambda l: l.target[1])
    top = sorted([l for l in labels if l.side == "T"], key=lambda l: l.target[0])
    # Колонка справа: полка на высоте цели, вверх — без наложений; не ниже низа рисунка
    ys = _spread([l.target[1] for l in right], [l.h for l in right], gap, y0)
    for l, y in zip(right, ys):
        l.shelf = (x1 + margin, y)
    # Ряд сверху: над рисунком и над колонкой справа (колонка может уйти выше рисунка)
    y_top = max([y1] + [l.box()[3] for l in right]) + margin
    xs = _spread([l.target[0] - l.w / 2.0 for l in top], [l.w for l in top], gap, x0)
    # Выноски ряда — левее начала выносок колонки (они идут от целей вправо): тогда они не пересекаются.
    # Цели ряда вызывающий выбирает левее целей колонки; середины полок, если ушли правее, — сдвигом ряда влево
    if right and top:
        limit = min(l.target[0] for l in right) - gap / 2.0
        over = max(x + l.w / 2.0 for l, x in zip(top, xs)) - limit
        if over > 0:
            xs = [x - over for x in xs]
    for l, x in zip(top, xs):
        l.shelf = (x, y_top)
    return labels


def overlaps(a, b):
    """Пересекаются ли прямоугольники (x0, y0, x1, y1)."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _cross(p1, p2, p3, p4):
    d = lambda a, b, c: (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2, d3, d4 = d(p3, p4, p1), d(p3, p4, p2), d(p1, p2, p3), d(p1, p2, p4)
    return d1 * d2 < 0 and d3 * d4 < 0


def leader(label):
    """Выноска: от цели к ближнему концу полки (у колонки справа — левый конец, у ряда сверху — низ середины)."""
    x, y = label.shelf
    if label.side == "R":
        return (label.target, (x, y))
    return (label.target, (x + label.w / 2.0, y))


def conflicts(labels):
    """Пересечения подписей между собой и выносок между собой: [(i, j, что)] — для проверки раскладки."""
    out = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            if overlaps(labels[i].box(), labels[j].box()):
                out.append((i, j, u"подписи"))
            a, b = leader(labels[i]), leader(labels[j])
            if _cross(a[0], a[1], b[0], b[1]):
                out.append((i, j, u"выноски"))
    return out

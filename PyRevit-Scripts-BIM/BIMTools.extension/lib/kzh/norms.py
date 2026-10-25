# -*- coding: utf-8 -*-
"""Нормативные значения КЖ из norms.json (СП 63.13330.2018, ГОСТ 34028-2016).

Значения лежат в norms.json рядом с модулем, чтобы обновлять их без правки кода.
Все длины на входе и выходе — в миллиметрах. Чистый Python, без Revit API.
"""
import json
import math
import os

NORMS_FILE = os.path.join(os.path.dirname(__file__), 'norms.json')
_cache = {}


def data():
    if 'data' not in _cache:
        with open(NORMS_FILE, 'rb') as f:
            _cache['data'] = json.loads(f.read().decode('utf-8'))
    return _cache['data']


def section(key):
    return data()[key]


def source(key):
    """Документ и пункт для подписи в окне: «СП 63…, п. … (сверить)»."""
    sec = data()[key]
    text = sec.get('source', u"")
    return text + (u" (сверить)" if sec.get('verify') else u"")


def ceil_to(value, step=10.0):
    return math.ceil(value / step - 1e-9) * step


def floor_to(value, step=50.0):
    return max(step, math.floor(value / step + 1e-9) * step)


# ----------------------------------------------------------
# СОРТАМЕНТ, КЛАССЫ
# ----------------------------------------------------------
def diameters():
    return sorted(int(k) for k in section('diameters')['values'])


def mass_per_m(d):
    return section('diameters')['values'].get(u"{}".format(int(d)))


def steel_classes():
    return sorted(section('steel')['values'])


def concrete_classes():
    return sorted(section('concrete')['values'], key=lambda k: int(k[1:]))


def cover_conditions():
    """[(ключ, подпись, мм)] в порядке возрастания защитного слоя."""
    vals = section('cover')['values']
    return sorted(((k, v['title'], v['mm']) for k, v in vals.items()), key=lambda x: x[2])


# ----------------------------------------------------------
# ЗАЩИТНЫЙ СЛОЙ
# ----------------------------------------------------------
def cover_main(condition, d):
    """Минимальный защитный слой рабочей арматуры диаметром d."""
    sec = section('cover')
    c = sec['values'][condition]['mm']
    if sec['not_less_than_diameter']['value']:
        c = max(c, d)
    return c


def cover_constructive(condition, d):
    """Минимальный защитный слой конструктивной (поперечной) арматуры диаметром d."""
    sec = section('cover')
    c = sec['values'][condition]['mm'] - sec['constructive_reduction']['value']
    if sec['not_less_than_diameter']['value']:
        c = max(c, d)
    return c


# ----------------------------------------------------------
# АНКЕРОВКА И НАХЛЁСТ
# ----------------------------------------------------------
def base_anchorage(d, steel, concrete):
    """l0,an = Rs·d / (4·Rbond), Rbond = eta1·eta2·Rbt (As,cal/As,ef = 1)."""
    st = section('steel')['values'][steel]
    rbt = section('concrete')['values'][concrete]
    eta2 = section('anchorage')['eta2']['d_max_32' if d <= 32 else 'd_over_32']
    return st['Rs'] * d / (4.0 * st['eta1'] * eta2 * rbt)


def anchorage_length(d, steel, concrete, compressed=False):
    sec = section('anchorage')
    l0 = base_anchorage(d, steel, concrete)
    length = sec['alpha']['compression' if compressed else 'tension'] * l0
    m = sec['min']
    return ceil_to(max(length, m['k_l0an'] * l0, m['k_d'] * d, m['mm']))


def lap_length(d, steel, concrete, compressed=False):
    sec = section('lap')
    l0 = base_anchorage(d, steel, concrete)
    alpha = sec['alpha']['compression' if compressed else 'tension']
    m = sec['min']
    return ceil_to(max(alpha * l0, m['k_alpha_l0an'] * alpha * l0, m['k_d'] * d, m['mm']))


def bend_diameter(d):
    """Диаметр оправки загиба, мм."""
    vals = section('bend')['values']
    return d * (vals['d_less_20'] if d < 20 else vals['d_from_20'])


def crank_rise(offset):
    """Высота отгиба выпуска при смещении offset (уклон 1:slope)."""
    return offset * section('crank')['slope']


# ----------------------------------------------------------
# КОЛОННЫ
# ----------------------------------------------------------
def column_rule(key):
    return section('column')[key]


def column_stirrup_spacing(d_long, dense=False):
    """Наибольший шаг хомутов колонны, мм: min(k·d, предел)."""
    rule = section('column')['stirrup_spacing_dense' if dense else 'stirrup_spacing']
    return min(rule['k_d'] * d_long, rule['mm'])


def column_stirrup_d_min(d_long):
    rule = section('column')['stirrup_d_min']
    return max(rule['k_d'] * d_long, rule['mm'])


def column_ties_needed(face_mm, bars_on_face):
    """Нужны ли шпильки для промежуточных стержней грани."""
    rule = section('column')['ties']
    return face_mm > rule['face_mm'] or bars_on_face > rule['bars']


def corbel_stirrup_spacing(h):
    """Наибольший шаг горизонтальных хомутов консоли высотой h, мм."""
    rule = section('corbel')['stirrup_spacing']
    return min(rule['k_h'] * h, rule['mm'])


# ----------------------------------------------------------
# БАЛКИ
# ----------------------------------------------------------
def beam_rule(key):
    return section('beam')[key]


def beam_stirrup_spacing(h0, support=True):
    """Наибольший шаг хомутов балки, мм: у опоры или в середине пролёта."""
    rule = section('beam')['stirrup_spacing_support' if support else 'stirrup_spacing_middle']
    return min(rule['k_h0'] * h0, rule['mm'])


def beam_stirrup_d_min(h):
    rule = section('beam')['stirrup_d_min']
    return rule['mm_high'] if h > rule['h_mm'] else rule['mm']


def beam_side_bars(h, cover_top, cover_bottom):
    """Число конструктивных стержней у каждой боковой грани (0 — не нужны)."""
    rule = section('beam')['side_bars']
    if h <= rule['h_mm']:
        return 0
    free = h - cover_top - cover_bottom
    return max(1, int(math.ceil(free / float(rule['spacing_mm']) - 1e-9)) - 1)


# ----------------------------------------------------------
# СТЕНЫ И ПЛИТЫ
# ----------------------------------------------------------
def wall_rule(key):
    return section('wall')[key]


def wall_spacing_max(t, vertical=True):
    """Наибольший шаг вертикальных (min(2t, 400)) или горизонтальных (400) стержней стены, мм."""
    if vertical:
        rule = section('wall')['vert_spacing_max']
        return min(rule['k_t'] * t, rule['mm'])
    return section('wall')['hor_spacing_max']['mm']


def slab_rule(key):
    return section('slab')[key]


def slab_spacing_max(h):
    """Наибольший шаг рабочих стержней плиты толщиной h, мм."""
    rule = section('slab')['spacing_max']
    if h <= rule['h_mm']:
        return rule['mm_thin']
    return min(rule['k_h'] * h, rule['mm'])


def node_rule(key):
    return section('node')[key]


def stair_rule(key):
    return section('stair')[key]


def opening_bars(n_cut, key='wall'):
    """Дополнительных стержней с каждой стороны проёма при n_cut прерванных (того же диаметра)."""
    rule = section(key)['opening_bars']
    return max(rule['min'], int(math.ceil(n_cut * rule['k_cut'] - 1e-9)))


# ----------------------------------------------------------
# ТАБЛИЦА ДЛЯ ОКНА «НОРМЫ»
# ----------------------------------------------------------
def summary_rows():
    """[(раздел, значения текстом, источник, нужно ли сверить)] — для просмотра в окне."""
    rows = []

    def add(key, text):
        sec = section(key)
        rows.append((sec.get('title', key), text, sec.get('source', u""), bool(sec.get('verify'))))

    add('diameters', u", ".join(u"{}".format(d) for d in diameters()) + u" мм")
    add('steel', u"; ".join(u"{} — Rs {} МПа, η1 {}".format(k, v['Rs'], v['eta1'])
                            for k, v in sorted(section('steel')['values'].items())))
    add('concrete', u"; ".join(u"{} — {}".format(k, section('concrete')['values'][k]) for k in concrete_classes()))
    add('cover', u"; ".join(u"{} — {} мм".format(t, mm) for _, t, mm in cover_conditions()) +
        u". Конструктивная арматура: −{} мм".format(section('cover')['constructive_reduction']['value']))
    a = section('anchorage')
    add('anchorage', u"α: растяжение {}, сжатие {}; не менее {}·l0,an, {}d, {} мм".format(
        a['alpha']['tension'], a['alpha']['compression'], a['min']['k_l0an'], a['min']['k_d'], a['min']['mm']))
    l = section('lap')
    add('lap', u"α: растяжение {}, сжатие {}; не менее {}·α·l0,an, {}d, {} мм".format(
        l['alpha']['tension'], l['alpha']['compression'], l['min']['k_alpha_l0an'], l['min']['k_d'], l['min']['mm']))
    b = section('bend')['values']
    add('bend', u"d < 20 мм — {}d; d ≥ 20 мм — {}d".format(b['d_less_20'], b['d_from_20']))
    add('crank', u"Уклон 1:{}".format(section('crank')['slope']))
    cb = section('corbel')
    add('corbel', u"; ".join(cb[k]['note'] for k in sorted(cb) if isinstance(cb[k], dict) and 'note' in cb[k]))
    beam = section('beam')
    add('beam', u"; ".join(beam[k]['note'] for k in sorted(beam) if isinstance(beam[k], dict) and 'note' in beam[k]))
    col = section('column')
    add('column', u"; ".join(col[k]['note'] for k in sorted(col) if isinstance(col[k], dict) and 'note' in col[k]))
    for key in ('wall', 'slab', 'node', 'stair'):
        sec = section(key)
        add(key, u"; ".join(sec[k]['note'] for k in sorted(sec) if isinstance(sec[k], dict) and 'note' in sec[k]))
    return rows

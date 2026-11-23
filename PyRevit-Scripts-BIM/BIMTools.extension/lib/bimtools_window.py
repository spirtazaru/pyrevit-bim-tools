# -*- coding: utf-8 -*-
"""Общее поведение окон BIMTools (КЖ, Аудит, Экспорт, Схемы СС):

- окно не больше рабочей области экрана (ноутбуки 1366×768 и масштаб 125–150 %);
- размер, положение и открытый раздел меню запоминаются и восстанавливаются при следующем открытии
  (в %APPDATA%\\BimTools_windows.json; если монитор отключили — окно открывается по центру);
- Esc закрывает окно (по желанию: в немодальных окнах с долгой работой не включается);
- владелец окна — главное окно Revit (окно не уходит под Revit).
"""
import json
import os

import System
from System.Windows import WindowState

STATE_FILE = os.path.join(os.getenv('APPDATA') or u"", 'BimTools_windows.json')


def _load():
    try:
        with open(STATE_FILE, 'rb') as f:
            data = json.loads(f.read().decode('utf-8'))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    try:
        with open(STATE_FILE, 'wb') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'))
    except Exception:
        pass


def revit_window_handle():
    """Главное окно Revit. Process.MainWindowHandle может указать на только что закрытое окно, поэтому —
    из UIApplication."""
    try:
        from pyrevit import HOST_APP
        return HOST_APP.uiapp.MainWindowHandle
    except Exception:
        from System.Diagnostics import Process
        return Process.GetCurrentProcess().MainWindowHandle


def fit_to_screen(window):
    area = System.Windows.SystemParameters.WorkArea
    window.Width = min(window.Width, area.Width - 20)
    window.Height = min(window.Height, area.Height - 20)
    window.MinWidth = min(window.MinWidth, window.Width)
    window.MinHeight = min(window.MinHeight, window.Height)


def setup(window, key=None, nav=(), esc_closes=False):
    """Подключает поведение к WPF-окну до показа. nav — RadioButton меню разделов (запоминается выбранный).
    key — имя окна в файле состояний (по умолчанию заголовок окна)."""
    key = key or window.Title or u"window"
    fit_to_screen(window)
    _restore(window, key, nav)
    window.Closing += lambda s, e: _remember(window, key, nav)
    if esc_closes:
        def on_key(sender, args):
            from System.Windows.Input import Key
            # Событие всплывающее: раскрытый список или поле обрабатывают Esc сами и помечают Handled
            if args.Key == Key.Escape and not args.Handled:
                args.Handled = True
                window.Close()
        window.KeyDown += on_key


def _restore(window, key, nav):
    try:
        g = _load().get(key) or {}
        p = System.Windows.SystemParameters
        area = p.WorkArea
        if g.get("w") and g.get("h"):
            window.Width = max(window.MinWidth, min(float(g["w"]), area.Width - 20))
            window.Height = max(window.MinHeight, min(float(g["h"]), area.Height - 20))
        x, y = g.get("x"), g.get("y")
        vx, vy = p.VirtualScreenLeft, p.VirtualScreenTop
        if x is not None and y is not None and vx - 50 <= x <= vx + p.VirtualScreenWidth - 200 \
                and vy <= y <= vy + p.VirtualScreenHeight - 100:
            window.WindowStartupLocation = System.Windows.WindowStartupLocation.Manual
            window.Left, window.Top = float(x), float(y)
        for rb in nav:
            if rb is not None and rb.Name == g.get("page"):
                rb.IsChecked = True
    except Exception:
        pass


def _remember(window, key, nav):
    try:
        data = _load()
        box = window.RestoreBounds if window.WindowState != WindowState.Normal else None
        data[key] = {
            "w": box.Width if box is not None else window.ActualWidth,
            "h": box.Height if box is not None else window.ActualHeight,
            "x": box.Left if box is not None else window.Left,
            "y": box.Top if box is not None else window.Top,
            "page": next((rb.Name for rb in nav if rb is not None and rb.IsChecked), None),
        }
        _save(data)
    except Exception:
        pass

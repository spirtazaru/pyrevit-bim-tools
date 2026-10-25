# -*- coding: utf-8 -*-
"""Общий сценарий армирования элементов: подтверждение со списком, удаление старой арматуры
(отдельное подтверждение), создание недостающих типов стержней, транзакция с откатом, отчёт.

План элемента — dict: host (элемент-основа), title, specs (список rebar.BarSpec), summary, notes.
"""
import math

from System.Collections.Generic import List
from pyrevit import DB

from kzh import element_name, params, service
from kzh import rebar as kr
from kzh.ui import show_message


def find_hook(doc, name):
    if not name:
        return None
    return next((h for h in kr.hook_types(doc) if element_name(h) == name), None)


def run(doc, plans, skipped, warnings, steel_long, steel_trans, hooks, part_name, what, existing=None):
    """what — подписи: {"many": u"колонн", "title": u"Армирование колонн", "done": u"Армировано колонн",
    "failed": u"Не армирована", "armed": u"уже армирована"}.
    existing(plan) -> [ElementId] — какая арматура считается «уже созданной» для плана (по умолчанию — вся
    арматура основы; для узлов — только стержни этого узла). plan["comment"] — пометка в «Комментарии» стержней."""
    skipped = list(skipped)
    if not plans:
        show_message(u"Армировать нечего.", details=skipped, details_header=u"Пропущено: {}".format(len(skipped)))
        return

    # Элементы с уже созданной арматурой: удалить и армировать заново — только после подтверждения
    find = existing or (lambda p: kr.host_rebar_ids(p["host"]))
    existing = [(p, find(p)) for p in plans]
    existing = [(p, ids) for p, ids in existing if ids]
    to_delete = []
    if existing:
        lines = [u"{} — элементов арматуры: {}".format(p["title"], len(ids)) for p, ids in existing]
        if show_message(u"У {} {} уже есть арматура. Удалить её и армировать заново?\n"
                        u"«Нет» — эти элементы будут пропущены.".format(len(existing), what["many"]),
                        ask=True, details=lines, details_header=u"Будет удалено у {}: {}".format(what["many"], len(existing))):
            for _, ids in existing:
                to_delete.extend(ids)
        else:
            skip = set(id(p) for p, _ in existing)       # по плану, а не по основе: узлы делят одну стену
            skipped.extend(u"{} — {}".format(p["title"], what["armed"]) for p, _ in existing)
            plans = [p for p in plans if id(p) not in skip]
            if not plans:
                return

    all_specs = [s for p in plans for s in p["specs"]]
    types = kr.needed_bar_types(doc, all_specs, steel_long, steel_trans)

    lines = [u"Внимание: {}".format(w) for w in warnings]
    for p in plans:
        lines.append(p["summary"] + u"".join(u"; внимание: {}".format(n) for n in p["notes"]))
    # Защитный слой основы (настройка Revit) больше, чем у стержней, — Revit сдвинул бы их внутрь
    covered = set()
    for p in plans:
        if p.get("min_cover") is None:
            try:
                p["min_cover"] = kr.min_lateral_cover(p["host"], p["specs"])
            except Exception:
                p["min_cover"] = None
        key = p["host"].Id.IntegerValue
        if p.get("min_cover") is None or key in covered:
            continue
        covered.add(key)
        ch = kr.host_cover_changes(doc, p["host"], p["min_cover"])
        if ch:
            lines.append(u"Защитный слой основы Revit у «{}»: {:.0f} → {:.0f} мм (иначе Revit сдвигает стержни к своему "
                         u"защитному слою)".format(p["title"], float(max(c for _, c in ch)), float(math.floor(p["min_cover"] + 1e-6))))
    for key in sorted(k for k, bt in types.items() if bt is None):
        lines.append(u"Будет создан тип стержня: {}".format(kr.bar_type_name(*key)))
    if to_delete:
        lines.append(u"Будет удалено элементов арматуры: {}".format(len(to_delete)))
    lines.extend(u"Пропуск: {}".format(s) for s in skipped)
    if not show_message(u"{}: {}?".format(what["title"], len(plans)), ask=True,
                        details=lines, details_header=u"Элементов: {}".format(len(plans))):
        return

    t, collector = kr.start_transaction(doc, u"КЖ: " + what["title"].lower())
    failed, created, quantity = [], 0, 0
    try:
        if to_delete:
            doc.Delete(List[DB.ElementId](to_delete))
        kr.create_missing_bar_types(doc, types)
        shapes = kr.role_shapes(doc)
        for plan in plans:
            st = DB.SubTransaction(doc)
            st.Start()
            try:
                if plan.get("min_cover") is not None:
                    kr.lower_host_cover(doc, plan["host"], plan["min_cover"])
                bars, q = kr.build_specs(doc, plan["host"], plan["specs"], types, steel_long, steel_trans,
                                         shapes, hooks)
                for rb, spec in zip(bars, plan["specs"]):
                    params.set_values(rb, {params.PRM_COUNT: spec.arm_count, params.PRM_PART: part_name})
                    if plan.get("comment"):
                        rb.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS).Set(plan["comment"])
                try:
                    service.store_signature(plan["host"])   # для поиска основ, изменённых после армирования
                except Exception:
                    pass
                st.Commit()
                created += len(bars)
                quantity += q
            except Exception as ex:
                st.RollBack()
                failed.append(u"{} — {}".format(plan["title"], ex))
        t.Commit()
    except Exception:
        if t.GetStatus() == DB.TransactionStatus.Started:
            t.RollBack()
        raise

    details = [u"{}: {}".format(what["failed"], f) for f in failed]
    details += [u"Пропуск: {}".format(s) for s in skipped]
    details += [u"Revit: {}".format(m) for m in collector.messages]
    text = u"{}: {} из {}. Создано элементов арматуры: {} (стержней: {}).".format(
        what["done"], len(plans) - len(failed), len(plans), created, quantity)
    show_message(text, kind='warn' if failed else 'info', title=what["title"], details=details or None,
                 details_header=u"Замечания: {}".format(len(details)))

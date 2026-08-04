"""Ночная очередь и её разбор в начале смены.

Проблема, которую этот модуль делает видимой. Ночью смены нет, но обращения
приходят. SLA по ним начинает тикать не в момент прихода, а с открытием рабочего
окна — Омнидеск считает `first_response_speed` в рабочем времени. Значит в 10:00
у всей ночной пачки часы стартуют ОДНОВРЕМЕННО, и уложиться в SLA может только
первая горстка: остальные просрочены по построению, сколько бы оператор ни
старался.

Дальше начинается второй эффект, менее очевидный. Пока оператор разбирает ночную
пачку, приходят НОВЫЕ обращения — уже внутри рабочего окна, с честно тикающими
часами. Ответить на них раньше, чем разгребётся ночь, физически нельзя, но в
статистике они выглядят как обычные дневные просрочки. Разрыв бывает
кратным: проверьте на своих данных, прежде чем судить по такой просрочке.

Поэтому обращения делятся на три вида по моменту прихода:
  * `night`  — пришли вне рабочего окна, часы стартуют с открытием смены;
  * `drain`  — пришли в окне, но пока ещё разбиралась ночная очередь;
  * `normal` — пришли в окне после разбора.

Момент «ночная очередь разобрана» НЕ угадывается константой, а считается по
данным: это ответ на последнее ночное обращение. Фиксированные «первые два часа»
врали бы в обе стороны: разбор занимает то десяток минут, то несколько часов —
это зависит от объёма ночного потока и числа людей на смене.

Вердикты это НЕ меняет: как и всплеск нагрузки, вид прихода — контекст для
руководителя, а не автоматическое оправдание. Решение остаётся за ним.

Данные берутся из того же прохода по обращениям, что и остальной отчёт
(нужны только `created_at` и `first_response_speed`), поэтому лишних запросов
к API модуль не делает.
"""
import collections
import datetime as dt

# Рабочее окно берём из аудита, а не объявляем своё: третья независимая копия
# 10:00–22:00 разъехалась бы при первой правке, и три метрики начали бы считаться
# по разным суткам. Расхождение имеющихся двух ловит selftest.
from audit_critical import WORK_START_H as WORK_START, WORK_END_H as WORK_END

NIGHT, DRAIN, NORMAL = "night", "drain", "normal"

KIND_LABELS = {
    NIGHT: "ночная очередь",
    DRAIN: "разбор ночной очереди",
    NORMAL: "обычное время",
}


def clock_start(created, work_start=WORK_START, work_end=WORK_END):
    """Когда по обращению начинают тикать часы SLA.

    Внутри окна — момент прихода. Вне окна — ближайшее открытие смены: именно
    поэтому ночная пачка стартует одновременно.
    """
    if work_start <= created.hour < work_end:
        return created
    day = created if created.hour < work_start else created + dt.timedelta(days=1)
    return day.replace(hour=work_start, minute=0, second=0, microsecond=0)


def drain_end(cases, work_start=WORK_START, work_end=WORK_END):
    """Когда в каждый день была разобрана ночная очередь.

    `cases` — последовательность (время прихода, первый ответ в минутах или None).
    Возвращает {дата: момент ответа на последнее ночное обращение}.

    Обращения без ответа пропускаем: по ним момент разбора неизвестен, а
    подставлять вместо него «конец дня» значило бы растянуть окно разбора на
    всю смену из-за одного забытого обращения.
    """
    last = {}
    for created, speed in cases:
        if speed is None:
            continue
        if work_start <= created.hour < work_end:
            continue                       # дневное, ночную очередь не образует
        start = clock_start(created, work_start, work_end)
        answered = start + dt.timedelta(minutes=speed)
        key = start.date()
        if key not in last or answered > last[key]:
            last[key] = answered
    return last


def arrival_kind(created, ends, work_start=WORK_START, work_end=WORK_END):
    """Вид обращения по моменту прихода: night / drain / normal."""
    if not (work_start <= created.hour < work_end):
        return NIGHT
    end = ends.get(created.date())
    if end is not None and created <= end:
        return DRAIN
    return NORMAL


def summary(cases, sla_min, work_start=WORK_START, work_end=WORK_END):
    """Сводка «сколько просрочек порождено ночной очередью».

    `cases` — (время прихода, первый ответ в минутах или None) по ВСЕЙ команде:
    ночную очередь разбирает смена целиком, и делить её по сотрудникам нельзя.

    Возвращает по каждому виду число обращений, просрочек и долю, плюс
    статистику по длительности разбора.
    """
    ends = drain_end(cases, work_start, work_end)
    per = collections.defaultdict(lambda: {"cases": 0, "violations": 0})
    for created, speed in cases:
        if speed is None:
            continue
        kind = arrival_kind(created, ends, work_start, work_end)
        per[kind]["cases"] += 1
        if speed > sla_min:
            per[kind]["violations"] += 1

    drains = sorted(
        (end - end.replace(hour=work_start, minute=0, second=0, microsecond=0)
         ).total_seconds() / 60.0
        for end in ends.values())
    out = {"kinds": {}, "days_measured": len(drains)}
    for kind in (NIGHT, DRAIN, NORMAL):
        v = per.get(kind) or {"cases": 0, "violations": 0}
        out["kinds"][kind] = {
            "label": KIND_LABELS[kind],
            "cases": v["cases"],
            "violations": v["violations"],
            "rate": round(v["violations"] / v["cases"], 3) if v["cases"] else None,
        }
    if drains:
        mid = drains[len(drains) // 2]
        out["drain_minutes"] = {"median": round(mid),
                                "min": round(min(drains)),
                                "max": round(max(drains))}
    total_viol = sum(k["violations"] for k in out["kinds"].values())
    queue_viol = (out["kinds"][NIGHT]["violations"]
                  + out["kinds"][DRAIN]["violations"])
    out["violations_total"] = total_viol
    out["violations_from_queue"] = queue_viol
    out["queue_share"] = round(queue_viol / total_viol, 3) if total_viol else None
    return out

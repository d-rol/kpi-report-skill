"""Этап 1, хелпер №3: командная метрика «без ответственного» (предложение, 3.2).

Что это. Опережающий индикатор риска (командный, не привязан к сотруднику):
сколько обращений ждут взятия в работу и как долго уже ждут. Считается по
`staff_id == 0` (нет ответственного) прямо из списка обращений — вскрывать
сообщения не нужно.

Исключение из тревожной метрики (3.2). Часть обращений «без ответственного» —
это штатное закрытие переоткрытых «спасибо»-обращений (клиент пишет «спасибо»,
сотрудник закрывает без ответа, чтобы не плодить переоткрытия). Такие исключаем.
Признак «был ли хотя бы один ответ сотрудника» берём бесплатно из списка:
`first_response_speed != '-'` ⇒ ответ был. Итоговые корзины для staff_id=0:
  * был ответ (first_response_speed задан)           -> штатное, ИСКЛЮЧАЕМ
  * ответа не было, статус closed/spam/deleted        -> тихое закрытие («спасибо»/спам), ИСКЛЮЧАЕМ
  * ответа не было, статус активный (open/…)          -> ЖДЁТ ВЗЯТИЯ = тревожная метрика (риск)

Важно про период. «Ждёт взятия» осмысленно только как ЖИВОЙ снимок: в прошлом
периоде все обращения уже разобраны/закрыты, поэтому там staff_id=0 остаётся
почти только у «спасибо»-закрытий. Поэтому:
  * без --from/--to  -> живой снимок текущей очереди (риск): количество + возраст;
  * с --from/--to    -> разбор staff_id=0 за период по корзинам (в основном чтобы
                        показать, что и сколько исключается).

Запуск:
  python no_responsible.py                 # живой снимок очереди «без ответственного»
  python no_responsible.py --from "2026-07-12 00:00:00" --to "2026-07-18 23:59:59"
  python no_responsible.py --json
"""
import sys
import json
import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
from collections import Counter

from omni_client import OmniClient

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

ACTIVE_STATUSES = ("open", "waiting")          # ещё в работе / ждут
CLOSED_STATUSES = ("closed", "spam", "deleted")  # завершены

# С какого возраста взятое в работу обращение без первого ответа считается
# забытым. Сутки — заведомо больше любой рабочей смены: всё, что дольше, уже
# нельзя объяснить «занят прямо сейчас».
FORGOTTEN_MIN_AGE_HOURS = 24.0


def had_reply(case):
    spd = case.get("first_response_speed")
    return spd not in (None, "-", "")


def age_hours(case, now_utc):
    try:
        return (now_utc - parsedate_to_datetime(case["created_at"])).total_seconds() / 3600.0
    except Exception:
        return None


def live_snapshot(client):
    """Текущая очередь без ответственного: активные тикеты, staff_id=0, без ответа."""
    now = dt.datetime.now(dt.timezone.utc)
    waiting = []
    for case in client.iter_cases(status="open", show_first_response_time=True):
        if case.get("staff_id") in (0, None, "0") and not had_reply(case):
            waiting.append({
                "case_number": case.get("case_number"),
                "case_id": case.get("case_id"),
                "age_hours": round(age_hours(case, now) or 0.0, 1),
                "subject": (case.get("subject") or "").strip()[:60],
            })
    waiting.sort(key=lambda x: -x["age_hours"])
    ages = [w["age_hours"] for w in waiting]
    return {
        "waiting_count": len(waiting),
        "oldest_hours": max(ages) if ages else 0.0,
        "median_hours": round(sorted(ages)[len(ages) // 2], 1) if ages else 0.0,
        "cases": waiting,
    }


def forgotten_in_work(client, min_age_hours=FORGOTTEN_MIN_AGE_HOURS):
    """Живой снимок «забытых в работе»: обращение ВЗЯТО (есть ответственный,
    staff_id != 0), но первого ответа до сих пор нет и оно висит дольше порога.

    Это не то же, что «без ответственного» (там staff_id=0, ждёт взятия). Здесь
    оператор уже владеет обращением, но забыл ответить — личная зона риска. Пока
    на такое не ответили, в SLA первого ответа оно не попадает, поэтому нужен
    отдельный опережающий индикатор, сгруппированный по ответственному.
    """
    now = dt.datetime.now(dt.timezone.utc)
    by_staff = {}
    staff = client.staff_map()
    for case in client.iter_cases(status="open", show_first_response_time=True):
        sid = case.get("staff_id")
        if sid in (0, None, "0") or had_reply(case):
            continue
        age = age_hours(case, now) or 0.0
        if age < min_age_hours:
            continue
        by_staff.setdefault(sid, []).append({
            "case_number": case.get("case_number"),
            "case_id": case.get("case_id"),
            "staff": staff.get(sid, sid),
            "age_hours": round(age, 1),
            "subject": (case.get("subject") or "").strip()[:60],
        })
    for lst in by_staff.values():
        lst.sort(key=lambda x: -x["age_hours"])
    return {
        "min_age_hours": min_age_hours,
        "total": sum(len(v) for v in by_staff.values()),
        "by_staff": {str(k): v for k, v in by_staff.items()},
    }


def period_breakdown(client, from_time, to_time):
    """Разбор staff_id=0 за период по корзинам (риск vs исключаемые)."""
    now = dt.datetime.now(dt.timezone.utc)
    buckets = {"waiting_risk": [], "silent_close": [], "had_reply": []}
    total_noresp = 0
    for case in client.iter_cases(from_time=from_time, to_time=to_time,
                                  show_first_response_time=True):
        if case.get("staff_id") not in (0, None, "0"):
            continue
        total_noresp += 1
        st = case.get("status")
        if had_reply(case):
            key = "had_reply"
        elif st in CLOSED_STATUSES:
            key = "silent_close"
        else:
            key = "waiting_risk"
        buckets[key].append({
            "case_number": case.get("case_number"),
            "status": st,
            "age_hours": round(age_hours(case, now) or 0.0, 1),
        })
    return {
        "no_responsible_total": total_noresp,
        "waiting_risk": len(buckets["waiting_risk"]),
        "excluded_silent_close": len(buckets["silent_close"]),
        "excluded_had_reply": len(buckets["had_reply"]),
        "waiting_risk_cases": buckets["waiting_risk"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_time", help="начало периода 'YYYY-MM-DD HH:MM:SS' (МСК)")
    ap.add_argument("--to", dest="to_time", help="конец периода")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # Живой снимок очереди не кэшируем (меняется); период — можно, но данные списка
    # и так дёшевы (~10 запросов), поэтому кэш здесь не подключаем.
    client = OmniClient()

    live = live_snapshot(client)
    period = None
    if args.from_time and args.to_time:
        period = period_breakdown(client, args.from_time, args.to_time)

    if args.json:
        print(json.dumps({"live": live, "period": period}, ensure_ascii=False, indent=2))
        return

    print("\n«Без ответственного» — живая очередь (риск прямо сейчас):")
    print(f"  ждут взятия в работу: {live['waiting_count']}")
    if live["waiting_count"]:
        print(f"  самый старый: {live['oldest_hours']} ч, медиана возраста: {live['median_hours']} ч")
        for w in live["cases"][:15]:
            print(f"    #{w['case_number']}  {w['age_hours']} ч  «{w['subject']}»")

    if period:
        print(f"\nЗа период {args.from_time} — {args.to_time}: обращений без ответственного всего {period['no_responsible_total']}")
        print(f"  ждали взятия (риск):                 {period['waiting_risk']}")
        print(f"  ИСКЛЮЧЕНО — тихие закрытия («спасибо»/спам): {period['excluded_silent_close']}")
        print(f"  ИСКЛЮЧЕНО — был ответ сотрудника (штатное):  {period['excluded_had_reply']}")


if __name__ == "__main__":
    main()

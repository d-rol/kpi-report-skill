"""Этап 1, хелпер №2: аудит критичных нарушений SLA — системное или личное.

Зачем. Omnidesk считает SLA первого ответа по ТЕКУЩЕМУ ответственному и
переназначает ответственного в момент ответа. Поэтому оператор, который берёт
зависшее в общей очереди обращение и сразу отвечает, «наследует» чужую/ничью
задержку и получает нарушение SLA, хотя лично он ответил мгновенно.

Что делает аудит. По каждому критичному нарушению (см. sla_violations.py)
поднимаем историю (changelog) и сообщения и считаем «удержание» — сколько
оператор реально держал обращение с момента, как стал ответственным, до своего
первого ответа (reply_staff):

  held = рабочие минуты (10:00–22:00 МСК) между самым ранним назначением
         ответственного и первым reply_staff (та же база, что нативный SLA Омни)

Вердикт:
  * есть назначение перед ответом И held <= SLA (15 мин)
      -> нарушение УНАСЛЕДОВАНО (задержка — это время в очереди, не вина
         отвечавшего). Если до назначения ответственного не было (old=0) —
         «системное / без ответственного»; если был другой оператор —
         «от другого оператора». Отвечавшему в плюс: «разобрал зависшее».
  * назначения перед ответом нет ИЛИ held > SLA
      -> нарушение ЛИЧНОЕ: оператор владел обращением и всё равно затянул.

Метрика «критичные до/после аудита» = (всего критичных) vs (личных).
Разница показывает системные провалы очереди, а не личную вину.

Осторожно с лимитом API: на каждое критичное обращение — 2 запроса (changelog
+ messages). При 48 критичных за неделю это ~100 запросов; при лимите 20/мин
это ~5 минут. Используй --cache: повторный прогон читает с диска.

Запуск:
  python audit_critical.py --from "2026-07-12 00:00:00" --to "2026-07-18 23:59:59" --cache
  python audit_critical.py --days 7 --cache        # (кэш не сработает: см. omni_client)
  python audit_critical.py --from ... --to ... --cache --json
"""
import sys
import json
import argparse
import datetime as dt
from email.utils import parsedate_to_datetime

from omni_client import OmniClient
from sla_violations import parse_speed, classify

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

HELD_SLA_MIN = 15.0  # если оператор ответил в пределах SLA с момента владения — не его вина
# Личное нарушение с небольшим удержанием — пограничное: оператор владел дольше SLA,
# но недолго (часто авто-упавший чат в момент параллельной загрузки). Такие не судим
# автоматически, а выносим руководителю на ручную оценку (со ссылкой на обращение).
BORDERLINE_HELD_MAX = 45.0

# Рабочее окно МСК — совпадает с «рабочим временем», настроенным в Омнидеске
# (команда на линии каждый день по ротации 2-на-2, поэтому окно ежедневное).
# Удержание считаем ТОЛЬКО внутри этого окна, чтобы оно было в той же базе, что
# нативный first_response_speed Омнидеска: иначе обращение, назначенное вечером и
# отвеченное утром, набирало бы стенные ~12 ч ночью и ошибочно шло в «личные».
WORK_START_H = 10
WORK_END_H = 22


def business_minutes(start, end):
    """Минуты между start и end в пределах рабочего окна WORK_START_H..WORK_END_H."""
    if end <= start:
        return 0.0
    total = 0.0
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        lo = max(start, day.replace(hour=WORK_START_H))
        hi = min(end, day.replace(hour=WORK_END_H))
        if hi > lo:
            total += (hi - lo).total_seconds() / 60.0
        day += dt.timedelta(days=1)
    return total


def first_reply_staff_ts(client, case_id):
    """Таймстемп первого настоящего ответа сотрудника (message_type == reply_staff)."""
    data = client.get(f"cases/{case_id}/messages.json")
    msgs = [data[k]["message"] for k in data if k.isdigit()]
    for m in msgs:
        if m.get("message_type") == "reply_staff":
            return parsedate_to_datetime(m["created_at"]), m.get("staff_id")
    return None, None


# Смена ответственного бывает двух видов: ручная/правило (event=staff) и
# авто-подхват чата (event=fixed_chat, 0->staff_id, done_by=omnidesk) — так
# Омнидеск назначает оператора на входящий Telegram/чат. Оба считаем назначением.
ASSIGN_EVENTS = ("staff", "fixed_chat")


def assignment_events_before(client, case_id, before_ts):
    """Все события назначения ответственного не позже before_ts, по времени.

    Каждое событие: (ts, old_value, new_value, done_by). new_value — кто стал
    владельцем после события; по этой цепочке восстанавливаем таймлайн владения.
    """
    data = client.get(f"cases/{case_id}/changelog.json")
    cl = data.get("changelog", []) or []
    events = []
    for r in cl:
        if r.get("event") not in ASSIGN_EVENTS:
            continue
        ts = parsedate_to_datetime(r["created_at"])
        if before_ts is not None and ts > before_ts:
            continue
        events.append((ts, str(r.get("old_value")), str(r.get("value")), r.get("done_by")))
    events.sort(key=lambda e: e[0])
    return events


def answerer_ownership(events, answerer_id):
    """Финальный НЕПРЕРЫВНЫЙ блок владения отвечающего перед его ответом.

    Зачем не «самое раннее назначение». Раньше удержание считалось от первого
    назначения до ответа. Это правильно ловит перештамп ответственного за 1-2 сек
    до ответа (Омнидеск переставляет владельца в момент reply — если брать
    последнее назначение, удержание схлопывается в ~0 и личное нарушение выглядит
    унаследованным). НО оно ломается при НАСТОЯЩЕЙ передаче владения: если оператор
    А держал обращение часами, а ответил оператор Б (стал владельцем перед ответом),
    «самое раннее назначение» = момент подхвата оператором А, и всё его удержание
    вешается на Б.

    Решение: удержание отвечающего = его ФИНАЛЬНЫЙ непрерывный блок владения
    (перештампы на того же оператора схлопываются — new_value не меняется). Всё,
    что было ДО этого блока (владел другой оператор / очередь), к отвечающему не
    относится: он «разобрал зависшее», а держал кто-то до него.

    Возвращает (block_start_ts, prior_owner, done_by):
      * block_start_ts — когда отвечающий стал владельцем в своём финальном блоке;
      * prior_owner    — кто владел непосредственно перед блоком ('0' = очередь,
                         иначе staff_id оператора, который держал/передал);
    либо None, если отвечающий не был финальным владельцем (ответил по чужому).
    """
    if not events:
        return None
    aid = str(answerer_id)
    if events[-1][2] != aid:          # финальный владелец — не отвечающий
        return None
    i = len(events) - 1
    while i > 0 and events[i - 1][2] == aid:   # схлопываем перештампы на него же
        i -= 1
    block = events[i]
    prior_owner = events[i - 1][2] if i > 0 else block[1]  # владелец до блока / old_value
    return block[0], prior_owner, block[3]


def audit_case(client, case):
    """Возвращает dict с вердиктом аудита по одному критичному обращению."""
    cid = case["case_id"]
    reply_ts, reply_staff = first_reply_staff_ts(client, cid)

    verdict = {
        "case_number": case["case_number"],
        "case_id": cid,
        "staff": case["staff"],
        "first_response_min": case["first_response_min"],
        # Направление обращения — нужно калибровочной грации (calibration.py),
        # чтобы отделить нарушения молодых продуктов от обычных.
        "group_id": case.get("group_id"),
        "borderline": False,
    }

    if reply_ts is None:
        # Критичное нарушение, но настоящего ответа сотрудника нет — аномалия.
        # Помечаем личным, но пограничным: нужен взгляд руководителя.
        verdict.update(kind="personal", reason="нет reply_staff (аномалия)",
                       held_min=None, borderline=True)
        return verdict

    events = assignment_events_before(client, cid, reply_ts)
    own = answerer_ownership(events, reply_staff)

    if own is None:
        if not events:
            # Ответственный не менялся — оператор владел с начала. Чисто личное.
            verdict.update(kind="personal", reason="владел с начала (без смены ответственного)",
                           held_min=None)
            return verdict
        # Ответил по обращению, где финальный владелец — не он (чужое). Унаследовано.
        prior = events[-1][2]
        source = "systemic_noresp" if prior == "0" else "from_other"
        verdict.update(
            kind=source,
            reason=("из общей очереди (без ответственного)" if prior == "0"
                    else f"от оператора {prior}"),
            held_min=0.0,
            neglected_by=(None if prior == "0" else prior),
        )
        return verdict

    block_start, prior_owner, done_by = own
    held_min = business_minutes(block_start, reply_ts)

    if held_min <= HELD_SLA_MIN:
        source = "systemic_noresp" if prior_owner == "0" else "from_other"
        verdict.update(
            kind=source,
            reason=("из общей очереди (без ответственного)" if prior_owner == "0"
                    else f"передано от оператора {prior_owner}"),
            held_min=round(held_min, 1),
            assigned_by=done_by,
            neglected_by=(None if prior_owner == "0" else prior_owner),
        )
    else:
        auto_chat = prior_owner == "0"  # авто-подхват чата из очереди, не ручной клейм
        verdict.update(
            kind="personal",
            reason=f"держал {held_min:.0f} мин до ответа",
            held_min=round(held_min, 1),
            assigned_by=done_by,
            auto_chat=auto_chat,
            borderline=held_min <= BORDERLINE_HELD_MAX,
        )
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int)
    ap.add_argument("--from", dest="from_time")
    ap.add_argument("--to", dest="to_time")
    ap.add_argument("--cache", action="store_true",
                    help="читать/писать ответы API в scripts/cache (нужен фиксированный --from/--to)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.days:
        now = dt.datetime.now()
        from_time = (now - dt.timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
        to_time = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        from_time, to_time = args.from_time, args.to_time
    if not from_time or not to_time:
        ap.error("укажи либо --days N, либо --from и --to")

    client = OmniClient(cache=args.cache)
    staff = client.staff_map()

    # 1) Собираем критичные нарушения тем же способом, что и sla_violations.
    critical = []
    for case in client.iter_cases(from_time=from_time, to_time=to_time,
                                   show_first_response_time=True):
        spd = parse_speed(case.get("first_response_speed"))
        if spd is None:
            continue
        cat, excess = classify(spd)
        if cat != "critical":
            continue
        sid = case.get("staff_id")
        critical.append({
            "case_number": case.get("case_number"),
            "case_id": case.get("case_id"),
            "staff": staff.get(sid, sid),
            "first_response_min": round(spd, 1),
        })

    # 2) Аудируем каждое.
    verdicts = [audit_case(client, c) for c in critical]

    kinds = {"systemic_noresp": [], "from_other": [], "personal": []}
    for v in verdicts:
        kinds[v["kind"]].append(v)

    # «Разобрал зависшее» — в плюс тому, кто ответил (по унаследованным).
    resolved_by = {}
    for v in kinds["systemic_noresp"] + kinds["from_other"]:
        resolved_by[v["staff"]] = resolved_by.get(v["staff"], 0) + 1

    total = len(verdicts)
    personal = len(kinds["personal"])
    result = {
        "period": {"from": from_time, "to": to_time},
        "critical_total": total,
        "critical_personal_after_audit": personal,
        "inherited_total": total - personal,
        "by_kind": {k: len(v) for k, v in kinds.items()},
        "resolved_stale_by": resolved_by,
        "personal_cases": kinds["personal"],
        "inherited_cases": kinds["systemic_noresp"] + kinds["from_other"],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"\nПериод: {from_time} — {to_time}")
    print(f"Критичных нарушений SLA (до аудита): {total}")
    print(f"  унаследованные (не вина отвечавшего): {total - personal}")
    print(f"     из общей очереди / без ответственного: {len(kinds['systemic_noresp'])}")
    print(f"     от другого оператора:                  {len(kinds['from_other'])}")
    print(f"  ЛИЧНЫЕ (владел и затянул) — после аудита: {personal}")
    if resolved_by:
        print("\n«Разобрал зависшее» (в плюс, бонус 3.1):")
        for who, n in sorted(resolved_by.items(), key=lambda x: -x[1]):
            print(f"    {who}: {n}")
    if kinds["personal"]:
        print("\nЛичные критичные нарушения (сюда смотреть в первую очередь):")
        for v in sorted(kinds["personal"], key=lambda x: -x["first_response_min"]):
            print(f"    #{v['case_number']}  {v['staff']}  первый ответ {v['first_response_min']} мин  "
                  f"({v['reason']})")


if __name__ == "__main__":
    main()

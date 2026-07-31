"""Этап 2, хелпер: скользящая база нагрузки (3.3 предложения).

Считает средний поток обращений по «(день недели, час)» за последние N недель и
сравнивает с ним фактическую нагрузку выбранного периода. Нужно, чтобы отличать
«оператор провалил SLA» от «в этот час пришло втрое больше обычного».

Про делитель «сотрудников онлайн»
---------------------------------
Поток делится на число одновременно работающих операторов. Делитель не
константа: `--online auto` (по умолчанию) берёт число операторов на смене из
`shifts.py`, который отличает смену от «заглянул на минуту». Это важно, если у
вас бывает, что оператор вне смены заходит закрыть пару сторонних задач: считать
его полноценной сменой — значит вдвое занизить нагрузку того, кто смену реально
работал. Явное число тоже принимается (`--online 2`) и печатается в выводе,
чтобы метрика не поехала молча.

Ретроспективного лога «кто был онлайн» Omnidesk не даёт, поэтому историю статусов
мы сознательно не собираем (это потребовало бы фонового сборщика/крона, а крон в
этом проекте запрещён — период и сотрудник всегда приходят из запроса). Смена
выводится из следа работы, а не из статусов.

База считается по окну, которое ЗАКАНЧИВАЕТСЯ перед началом сравниваемого периода
— иначе период сравнивался бы сам с собой.

Про нижнюю границу пригодных данных (DATA_START / --since)
----------------------------------------------------------
⚠️ Грабли, на которые легко наступить. Если вы переезжали в Omnidesk постепенно
(раньше переписка шла в соцсетях или почте напрямую, а в систему попадала лишь
часть), то ранние обращения — это **не «низкий поток», а неполнота переезда**.
Окно базы, дотянувшееся до периода переезда, даёт ложный вывод «поток вырос в
разы»: вырос не поток, а покрытие системой. Дальше по этому мнимому росту
принимаются решения о порогах SLA — и все они будут неверными.

Поэтому есть нижняя граница пригодных данных. Окно базы обрезается по ней **до**
запроса к API, а если после обрезки остаётся меньше `MIN_BASELINE_DAYS` дней,
хелпер честно говорит «базы пока нет» вместо того, чтобы напечатать красивое, но
бессмысленное число. Если период целиком раньше границы, окно схлопывается и в
API мы не идём вовсе.

По умолчанию границы нет (`DATA_START = None`) — тул не знает вашей истории
внедрения. **Если переезд был, поставьте дату**, с которой Omnidesk стал
основным каналом: разово — флагом `--since`, постоянно — прописав её в
`DATA_START` ниже. Проверить себя просто: если недельный поток в начале истории
аккаунта в разы ниже текущего, а роста бизнеса в разы не было — это переезд, а
не рост.

Запуск:
  python load_baseline.py --weeks 4 --cache
  python load_baseline.py --from "2026-07-12 00:00:00" --to "2026-07-18 23:59:59" --weeks 4 --cache
  python load_baseline.py --from "..." --to "..." --json
"""
import sys
import json
import argparse
import collections
import datetime as dt
from email.utils import parsedate_to_datetime

import shifts
from omni_client import OmniClient

# Windows-консоль по умолчанию не UTF-8 — иначе кириллица превращается в кракозябры.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

MSK = dt.timezone(dt.timedelta(hours=3))
WORK_START, WORK_END = 10, 22        # рабочее окно 10:00–22:00 МСК (как в аудите SLA)
WD_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
FMT = "%Y-%m-%d %H:%M:%S"

# Дата, с которой данные в Omnidesk пригодны для статистики. None = границы нет.
# Ставьте сюда дату, с которой Omnidesk стал вашим основным каналом, если до неё
# шёл постепенный переезд: иначе неполнота переезда прочитается как рост потока.
# Пример: DATA_START = dt.datetime(2026, 7, 15, 0, 0, 0, tzinfo=MSK)
# См. блок «Про нижнюю границу пригодных данных» в докстринге модуля.
DATA_START = None
# Меньше этого числа суток — база статистически пустая, честнее не считать.
MIN_BASELINE_DAYS = 7.0


def bucket_key(weekday, hour):
    """Ключ корзины «день недели × час» — строка «Пн-14», а не кортеж.

    Внутри счётчиков удобнее кортеж (weekday, hour), но в JSON кортеж ключом
    не сериализуется, а --json нужен для сборки в отчёт. Поэтому наружу (в
    baseline) корзины уходят строками, и любое чтение per_bucket обязано
    идти через этот хелпер — иначе .get(кортеж) молча вернёт 0.
    """
    return f"{WD_NAMES[weekday]}-{hour:02d}"


def parse_bucket_key(key):
    """Обратно из «Пн-14» в (weekday, hour)."""
    name, _, hour = key.rpartition("-")
    return WD_NAMES.index(name), int(hour)


def parse_created(value):
    """created_at приходит в RFC 2822: 'Wed, 15 Jul 2026 15:51:25 +0300'."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(MSK)
    except (TypeError, ValueError):
        return None


def _bucket_occurrences(start, end):
    """Сколько раз каждый (день недели, час) реально встретился в окне [start, end).

    Считаем точно по календарю, а не «N недель» — окно может быть неполным
    (аккаунт молодой, данных меньше, чем просим), и тогда деление на N завысило бы базу.
    """
    occ = collections.Counter()
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur < end:
        occ[(cur.weekday(), cur.hour)] += 1
        cur += dt.timedelta(hours=1)
    return occ


def _collect(client, start, end):
    """Считает обращения по (день недели, час).

    Возвращает (все, с реальным ответом, самое раннее обращение окна).
    """
    total = collections.Counter()
    answered = collections.Counter()
    by_week = collections.Counter()
    earliest = None
    for case in client.iter_cases(from_time=start.strftime(FMT),
                                  to_time=end.strftime(FMT),
                                  show_first_response_time=True):
        created = parse_created(case.get("created_at"))
        if created is None:
            continue
        if earliest is None or created < earliest:
            earliest = created
        key = (created.weekday(), created.hour)
        total[key] += 1
        iso = created.isocalendar()
        by_week[(iso[0], iso[1])] += 1
        if case.get("first_response_speed") not in (None, "-", ""):
            answered[key] += 1
    return total, answered, earliest, by_week


def build_baseline(client, end, weeks=4, online=1, since=None):
    """Средний поток на (день недели, час) за `weeks` недель до `end`.

    Окно обрезается снизу дважды:
      * по `since` (DATA_START) — до этой даты Омнидеск не был основным каналом,
        и низкие цифры там означают неполноту переезда, а не спокойный поток;
      * по первому реальному обращению в окне — на случай, если данных нет даже
        после `since`.
    Без первой обрезки база уезжает вниз и любой обычный день выглядит всплеском.
    """
    since = since or DATA_START
    start = end - dt.timedelta(weeks=weeks)
    # Обрезаем ДО запроса: тянуть из API заведомо негодный период незачем.
    # since = None означает «границы нет» (переезда не было или она не задана).
    clipped_by_since = bool(since and start < since)
    if since:
        start = max(start, since)
    collapsed = start >= end
    if collapsed:
        # Окно схлопнулось: отчётный период целиком раньше начала пригодных
        # данных, база для него не существует. Ходить в API нельзя — Омнидеск
        # на перевёрнутом диапазоне (from > to) отвечает 400. Возвращаем
        # честно пустую базу, ниже её поймает флаг insufficient.
        total = answered = by_week = collections.Counter()
        start = effective_start = end
    else:
        total, answered, earliest, by_week = _collect(client, start, end)
        effective_start = max(start, earliest) if earliest else start
    occ = _bucket_occurrences(effective_start, end)
    per_bucket = {}
    for key, times in occ.items():
        if not times:
            continue
        per_bucket[bucket_key(*key)] = {
            "avg_cases": round(total.get(key, 0) / times, 3),
            "avg_answered": round(answered.get(key, 0) / times, 3),
            "samples": times,
        }
    # Сглаженный профиль по часу суток. Крест (день недели × час) за 3-4 недели даёт
    # всего 3-4 наблюдения на корзину — этого мало, и сравнение с ним рождает
    # бессмысленные «x25 против 1.0». Профиль по часу суток агрегирует все дни
    # недели, наблюдений в 7 раз больше, поэтому всплески считаем по нему.
    hour_total = collections.Counter()
    hour_occ = collections.Counter()
    for (wd, hour), times in occ.items():
        hour_occ[hour] += times
        hour_total[hour] += total.get((wd, hour), 0)
    per_hour = {h: (hour_total[h] / hour_occ[h]) for h in hour_occ if hour_occ[h]}
    week_days = _week_days_in_window(effective_start, end)

    days = max(0.0, (end - effective_start).total_seconds() / 86400.0)
    return {
        "window": {
            "from": start.strftime(FMT), "to": end.strftime(FMT), "weeks": weeks,
            "effective_from": effective_start.strftime(FMT),
            "effective_days": round(days, 1),
            "truncated": effective_start > start,
            # Окно упёрлось в дату начала пригодных данных, а не в первое
            # обращение — значит запрошено больше истории, чем существует.
            "clipped_by_data_start": clipped_by_since,
            # Окно схлопнулось в точку: отчётный период целиком раньше data_start.
            "collapsed": collapsed,
            "data_start": since.strftime(FMT) if since else None,
            # Базы фактически нет: считать средние не на чем.
            "insufficient": days < MIN_BASELINE_DAYS,
            "min_days": MIN_BASELINE_DAYS,
        },
        "online_staff": online,
        "cases_total": sum(total.values()),
        "answered_total": sum(answered.values()),
        "per_bucket": per_bucket,
        "per_hour": {h: round(v, 2) for h, v in sorted(per_hour.items())},
        "weekly": {f"{y}-W{w:02d}": n for (y, w), n in sorted(by_week.items())},
        "weekly_per_day": {f"{y}-W{w:02d}": round(n / week_days[(y, w)], 1)
                           for (y, w), n in sorted(by_week.items())
                           if week_days.get((y, w))},
        "last_week_cases": _last_week_rate(by_week, week_days) * 7,
        "growth": _growth(by_week, week_days),
    }


def _last_week_rate(by_week, week_days, min_days=2.0):
    """Среднесуточный поток последней вменяемой недели окна (не огрызка)."""
    for key in sorted(by_week, reverse=True):
        d = week_days.get(key, 0.0)
        if d >= min_days:
            return by_week[key] / d
    return 0.0


def _week_days_in_window(start, end):
    """Сколько дней каждой ISO-недели реально попало в окно (может быть дробным).

    Крайние недели окно почти всегда режет посередине. Если сравнивать их сырыми
    счётчиками, неполная неделя выглядит провалом — поэтому нормируем на дни.
    """
    days = collections.Counter()
    cur = start
    while cur < end:
        nxt = (cur + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        nxt = min(nxt, end)
        iso = cur.isocalendar()
        days[(iso[0], iso[1])] += (nxt - cur).total_seconds() / 86400.0
        cur = nxt
    return days


def _growth(by_week, week_days, min_days=2.0):
    """Во сколько раз вырос СРЕДНЕСУТОЧНЫЙ поток от первой недели окна к последней.

    Нужно, чтобы честно предупредить: при растущем потоке плоское среднее за 4
    недели систематически ниже текущей реальности, и сравнение с ним всегда будет
    показывать «нагрузка выше обычного» — метрика превратится в вечно красную лампу.
    Недели, от которых в окне остался огрызок (< min_days), в расчёт не берём.
    """
    rates = []
    for key in sorted(by_week):
        d = week_days.get(key, 0.0)
        if d >= min_days:
            rates.append(by_week[key] / d)
    if len(rates) < 2 or not rates[0]:
        return None
    return round(rates[-1] / rates[0], 2)


def compare(client, from_time, to_time, baseline):
    """Факт периода против базы: сколько ожидали, сколько пришло, где всплески."""
    start = dt.datetime.strptime(from_time, FMT).replace(tzinfo=MSK)
    end = dt.datetime.strptime(to_time, FMT).replace(tzinfo=MSK)
    total, answered, _, _ = _collect(client, start, end)
    occ = _bucket_occurrences(start, end)
    per = baseline["per_bucket"]
    online = baseline["online_staff"] or 1

    expected = sum(per.get(bucket_key(*k), {}).get("avg_cases", 0.0) * n
                   for k, n in occ.items())
    actual = sum(total.values())
    by_day = collections.defaultdict(lambda: {"actual": 0, "expected": 0.0})
    by_hour = collections.defaultdict(lambda: {"actual": 0, "expected": 0.0})
    for key, n in occ.items():
        wd, hour = key
        exp = per.get(bucket_key(wd, hour), {}).get("avg_cases", 0.0) * n
        by_day[wd]["expected"] += exp
        by_hour[hour]["expected"] += exp
    for key, cnt in total.items():
        wd, hour = key
        by_day[wd]["actual"] += cnt
        by_hour[hour]["actual"] += cnt

    # Часы с самым заметным отклонением — то, на что смотреть при разборе просрочек.
    # Считаем против сглаженного профиля по часу суток (см. build_baseline): крест
    # «день недели × час» слишком разрежен и даёт ложные всплески на 2-3 обращениях.
    per_hour = baseline.get("per_hour", {})
    spikes = []
    for key, n in occ.items():
        wd, hour = key
        exp = per_hour.get(hour, per_hour.get(str(hour), 0.0)) * n
        act = total.get(key, 0)
        # Порог по абсолютному числу: 3 обращения ночью — не всплеск, а шум.
        if act >= 5 and exp > 0 and act / exp >= 1.5:
            spikes.append({
                "weekday": WD_NAMES[wd], "hour": hour,
                "actual": act, "expected": round(exp, 1),
                "ratio": round(act / exp, 2),
            })
    spikes.sort(key=lambda x: -x["ratio"])

    hours_span = sum(occ.values()) or 1
    work_hours = sum(n for (wd, h), n in occ.items() if WORK_START <= h < WORK_END) or 1
    work_actual = sum(c for (wd, h), c in total.items() if WORK_START <= h < WORK_END)

    # При растущем потоке сравнение со средним за 4 недели малоинформативно —
    # рядом даём сравнение с последней неделей базы, приведённое к длине периода.
    days = max(1e-9, (end - start).total_seconds() / 86400.0)
    last_week = baseline.get("last_week_cases") or 0
    exp_last_week = last_week / 7.0 * days
    return {
        "period": {"from": from_time, "to": to_time, "days": round(days, 2)},
        "online_staff": online,
        "actual_cases": actual,
        "actual_answered": sum(answered.values()),
        "expected_cases": round(expected, 1),
        "ratio": round(actual / expected, 2) if expected else None,
        "vs_last_week": {
            "expected_cases": round(exp_last_week, 1),
            "ratio": round(actual / exp_last_week, 2) if exp_last_week else None,
        },
        "load_per_operator": {
            "cases_per_work_hour": round(work_actual / work_hours / online, 2),
            "cases_per_hour_overall": round(actual / hours_span / online, 2),
            "work_window": f"{WORK_START}:00–{WORK_END}:00 МСК",
        },
        "by_weekday": {WD_NAMES[wd]: {"actual": v["actual"], "expected": round(v["expected"], 1)}
                       for wd, v in sorted(by_day.items())},
        "by_hour": {h: {"actual": v["actual"], "expected": round(v["expected"], 1)}
                    for h, v in sorted(by_hour.items())},
        "spikes": spikes[:10],
    }


def bar(value, peak, width=22):
    if peak <= 0:
        return ""
    filled = int(round(width * value / peak))
    return "█" * filled + "░" * (width - filled)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_time", help="начало сравниваемого периода 'YYYY-MM-DD HH:MM:SS' (МСК)")
    ap.add_argument("--to", dest="to_time", help="конец сравниваемого периода")
    ap.add_argument("--weeks", type=int, default=4, help="глубина базы в неделях (по умолчанию 4)")
    ap.add_argument("--online", default="auto",
                    help="сколько операторов работает одновременно: число или "
                         "'auto' (по умолчанию) — вывести из графика смен, "
                         "не считая тех, кто заглянул мимо смены")
    ap.add_argument("--since", default=DATA_START.strftime(FMT) if DATA_START else None,
                    help="нижняя граница пригодных данных 'YYYY-MM-DD HH:MM:SS' "
                         + (f"(по умолчанию {DATA_START.strftime('%Y-%m-%d')} — "
                            "до этого Омнидеск не был основным каналом)"
                            if DATA_START else
                            "(по умолчанию границы нет; поставьте дату, если был "
                            "постепенный переезд в Омнидеск)"))
    ap.add_argument("--json", action="store_true", help="вывести результат в JSON")
    ap.add_argument("--cache", action="store_true", help="читать/писать ответы API в scripts/cache")
    args = ap.parse_args()

    client = OmniClient(cache=args.cache)
    since = (dt.datetime.strptime(args.since, FMT).replace(tzinfo=MSK)
             if args.since else None)

    if args.from_time and args.to_time:
        base_end = dt.datetime.strptime(args.from_time, FMT).replace(tzinfo=MSK)
    else:
        base_end = dt.datetime.now(MSK)
    baseline = build_baseline(client, base_end, weeks=args.weeks,
                              online=1, since=since)

    # Делитель нагрузки. По регламенту онлайн один оператор, но второй иногда
    # заходит закрыть сторонние задачи — если считать его сменой, нагрузка
    # основного оператора делится пополам на ровном месте. Поэтому по умолчанию
    # число операторов берём из графика смен (shifts.py), а не с потолка.
    online_note = None
    if str(args.online).lower() == "auto":
        r_from = args.from_time or baseline["window"]["effective_from"]
        r_to = args.to_time or baseline["window"]["to"]
        rst = shifts.roster(client, r_from, r_to)
        avg = shifts.avg_online(rst)
        if avg:
            baseline["online_staff"] = avg
            off = sum(r["cases"] for d in rst["days"].values() for r in d["visitors"])
            online_note = (f"из графика смен: {avg} (мимо смены закрыто {off} "
                           "обращений — в делитель не пошли)")
        else:
            baseline["online_staff"] = 1
            online_note = "график смен не определился — считаем по одному оператору"
        baseline["shifts"] = rst
    else:
        baseline["online_staff"] = int(args.online)
        online_note = "задано вручную"

    result = {"baseline": baseline}
    if args.from_time and args.to_time and not baseline["window"]["insufficient"]:
        # Сравнение с пустой базой дало бы «ожидали 0, пришло 300, x∞» —
        # цифру, которая выглядит как вывод, но им не является. Поэтому при
        # insufficient секции comparison просто нет: потребитель (report.py)
        # обязан это заметить, а не отрендерить пустышку.
        result["comparison"] = compare(client, args.from_time, args.to_time, baseline)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    b = baseline["window"]
    ds = (b["data_start"] or "")[:10]
    if b["collapsed"]:
        print(f"\nБаза нагрузки: пусто — весь запрошенный период раньше {ds}"
              if ds else "\nБаза нагрузки: пусто — окно схлопнулось")
    else:
        print(f"\nБаза нагрузки: {b['from']} — {b['to']} ({b['weeks']} нед)")
    if b["clipped_by_data_start"]:
        print(f"  ⓘ окно обрезано по {ds} — раньше этой даты Омнидеск ещё не был "
              "основным каналом (шёл переезд),")
        print("    и низкие цифры там означали бы неполноту переезда, а не спокойный поток")
    if b["truncated"]:
        print(f"  ⚠ данных раньше {b['effective_from']} нет — база считается "
              f"по {b['effective_days']} дн, а не по {b['weeks']} нед")
    print(f"  обращений в базе: {baseline['cases_total']} "
          f"(с реальным ответом: {baseline['answered_total']})")
    print(f"  операторов онлайн одновременно: {baseline['online_staff']}"
          + (f" — {online_note}" if online_note else ""))

    if b["insufficient"]:
        print(f"\n⚠ БАЗЫ ПОКА НЕТ: пригодных данных всего {b['effective_days']} дн "
              f"(нужно минимум {b['min_days']:.0f}).")
        print("  Сравнивать не с чем — любые «выше/ниже обычного» на таком объёме")
        print("  были бы выдумкой. Вернитесь к этому, когда накопится история"
              + (f" с {ds}." if ds else "."))
        return

    cmp_ = result.get("comparison")
    if not cmp_:
        # Без периода показываем сам профиль базы — когда обычно приходит нагрузка.
        per_hour = collections.Counter()
        for key, v in baseline["per_bucket"].items():
            per_hour[parse_bucket_key(key)[1]] += v["avg_cases"]
        peak = max(per_hour.values()) if per_hour else 0
        print("\nСредний профиль по часам (обращений/час, среднее за неделю):")
        for h in range(24):
            v = per_hour.get(h, 0.0)
            mark = " " if WORK_START <= h < WORK_END else "·"
            print(f"  {h:02d}:00{mark} {bar(v, peak)} {v:.1f}")
        print("\n(«·» — вне рабочего окна 10:00–22:00, там обращения копятся в очереди)")
        return

    growth = baseline.get("growth")
    weekly = baseline.get("weekly_per_day") or {}
    if weekly:
        print("\nПоток по неделям (обращений в сутки — крайние недели окна неполные,")
        print("поэтому сравниваем среднесуточный темп, а не сырые суммы):")
        peak_w = max(weekly.values())
        raw = baseline.get("weekly", {})
        for name, v in weekly.items():
            print(f"  {name} {bar(v, peak_w)} {v:>5.1f}/сут  (всего {raw.get(name, 0)})")
    if growth and growth >= 1.3:
        print(f"\n⚠ Поток РАСТЁТ: последняя неделя базы в {growth}x больше первой.")
        print("  Плоское среднее за 4 недели при таком росте систематически занижено,")
        print("  и сравнение с ним будет всегда показывать «выше обычного».")
        print("  Ориентируйтесь на строку «против последней недели» ниже.")

    print(f"\nПериод: {cmp_['period']['from']} — {cmp_['period']['to']}")
    ratio = cmp_["ratio"]
    verdict = "—"
    if ratio is not None:
        verdict = ("ВЫШЕ обычного" if ratio >= 1.2 else
                   "НИЖЕ обычного" if ratio <= 0.8 else "в пределах обычного")
    print(f"  обращений фактически: {cmp_['actual_cases']} "
          f"(ожидали по среднему за {b['weeks']} нед: {cmp_['expected_cases']}) → {verdict}"
          + (f", x{ratio}" if ratio is not None else ""))
    vlw = cmp_["vs_last_week"]
    if vlw["ratio"] is not None:
        v2 = ("выше" if vlw["ratio"] >= 1.2 else
              "ниже" if vlw["ratio"] <= 0.8 else "на уровне")
        print(f"  против последней недели базы: ожидали {vlw['expected_cases']} → "
              f"{v2}, x{vlw['ratio']}  ← более честное сравнение при росте")
    lpo = cmp_["load_per_operator"]
    print(f"  нагрузка на оператора: {lpo['cases_per_work_hour']} обращений/час "
          f"в рабочее окно {lpo['work_window']}")

    print("\nПо дням недели (факт / ожидание):")
    peak = max((v["actual"] for v in cmp_["by_weekday"].values()), default=0)
    for name, v in cmp_["by_weekday"].items():
        print(f"  {name} {bar(v['actual'], peak)} {v['actual']:>4} / {v['expected']:.1f}")

    if cmp_["spikes"]:
        print("\nЧасы с всплеском (факт заметно выше базы) — сюда смотреть при разборе просрочек:")
        for s in cmp_["spikes"]:
            print(f"  {s['weekday']} {s['hour']:02d}:00 — {s['actual']} против {s['expected']} (x{s['ratio']})")
    else:
        print("\nВсплесков выше базы не найдено.")


if __name__ == "__main__":
    main()

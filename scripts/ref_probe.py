"""Замер разброса нагрузки — сырьё для решения о норме (`load_reference.json`).

Зачем отдельный хелпер. Норму нельзя вывести автоматически (иначе она станет
тем же скользящим средним, от которого мы уходим), но и ставить её пальцем в
небо нельзя. Между этими крайностями и живёт этот скрипт: он считает разброс и
печатает ПРЕДЛОЖЕНИЕ, а решение принимает человек и записывает в конфиг руками.

Почему он в репозитории, а не разовый. Пересмотр нормы — редкое событие (смена
состава команды, запуск продукта, заметный сдвиг потока), и между пересмотрами
проходят месяцы. Замер, сделанный разово и выброшенный, в следующий раз будет
написан заново чуть иначе — и тогда новое число нельзя будет сравнить со старым:
непонятно, изменились данные или методика. Здесь важна не сложность кода, а то,
что метод один и тот же.

Что считает:
  * разброс потока день к дню, по дню недели и неделя к неделе;
  * нагрузку на оператора в рабочем окне: медиана, среднее, σ, квартили;
  * полные 7-дневные блоки от начала окна — по ним видно, бывает ли тяжёлой
    неделя целиком (на этот вопрос подённая статистика не отвечает);
  * предложение «значение + коридор» и проверку, сколько недель истории это
    предложение пометило бы как необычные.

Делитель — из графика смен (`shifts.py`), по каждому дню отдельно: заглянувший
закрыть пару задач не должен делить нагрузку основного оператора пополам.

Чего он НЕ делает: не пишет в конфиг. Число ставит руководитель — так же, как
переопределения грации, с датой и автором.

Запуск:
  python scripts/ref_probe.py --cache
  python scripts/ref_probe.py --from "2026-07-15 00:00:00" --to "2026-08-11 23:59:59" --cache
  python scripts/ref_probe.py --cache --json
"""
import sys
import json
import argparse
import collections
import statistics as st
import datetime as dt

import shifts
import load_baseline as lb
from omni_client import OmniClient

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# Минимум для решения о норме. Четыре полные недели — не круглое число ради
# красоты: на двух наблюдениях недельный разброс не оценивается, а только
# кажется маленьким. На живых данных одной поддержки по двум неделям выходило
# x1.14, по четырём — x1.30, то есть первая оценка занижала разброс вдвое.
# Меньше этого срока хелпер числа печатает, но предложение не даёт.
MIN_PROBE_DAYS = 28


def collect(client, start, end):
    """Обращения по дням: всего и внутри рабочего окна.

    Один проход по списку обращений, деталей не тянем — замер должен быть
    дешёвым, иначе его не будут делать.
    """
    per_day = collections.Counter()
    per_day_work = collections.Counter()
    total = 0
    for case in client.iter_cases(from_time=start.strftime(lb.FMT),
                                  to_time=end.strftime(lb.FMT)):
        created = lb.parse_created(case.get("created_at"))
        if created is None:
            continue
        total += 1
        per_day[created.date()] += 1
        if lb.WORK_START <= created.hour < lb.WORK_END:
            per_day_work[created.date()] += 1
    return per_day, per_day_work, total


def online_by_day(client, start, end):
    """Сколько операторов держали смену в каждый день окна.

    По дням, а не средним по периоду: нагрузка считается посуточно, и день с
    настоящей двойной сменой нельзя делить так же, как обычный.
    """
    roster = shifts.roster(client, start.strftime(lb.FMT), end.strftime(lb.FMT))
    out = {}
    for day, info in roster["days"].items():
        out[day] = len(info.get("on_shift") or []) or 1
    return out, roster


def summarize(per_day, per_day_work, online):
    """Вся арифметика замера. Клиент сюда не приходит — это чистая функция,
    поэтому её можно проверить на подставных числах, а не на живом API."""
    days = sorted(per_day)
    if not days:
        return None
    counts = [per_day[d] for d in days]
    loads = []
    for d in days:
        div = online.get(d.strftime("%Y-%m-%d"), 1) or 1
        loads.append(per_day_work[d] / (lb.WORK_END - lb.WORK_START) / div)

    by_weekday = collections.defaultdict(list)
    for d, v in zip(days, loads):
        by_weekday[d.weekday()].append(v)
    wd = {lb.WD_NAMES[k]: round(sum(v) / len(v), 2) for k, v in sorted(by_weekday.items())}

    # Полные 7-дневные блоки от начала окна. Именно блоки, а не ISO-недели:
    # окно почти никогда не начинается с понедельника, и ISO-недели по краям
    # оказались бы огрызками, которые пришлось бы выбрасывать.
    blocks = []
    for i in range(len(days) // 7):
        chunk = days[i * 7:(i + 1) * 7]
        if len(chunk) < 7 or (chunk[-1] - chunk[0]).days != 6:
            continue          # разрыв в данных: неделя неполная, в расчёт не берём
        idx = [days.index(c) for c in chunk]
        blocks.append({
            "from": chunk[0].isoformat(), "to": chunk[-1].isoformat(),
            "cases": sum(per_day[c] for c in chunk),
            "per_day": round(sum(per_day[c] for c in chunk) / 7, 1),
            "load": round(sum(loads[j] for j in idx) / 7, 2),
        })

    q = sorted(loads)
    out = {
        "days": len(days),
        "from": days[0].isoformat(), "to": days[-1].isoformat(),
        "cases_total": sum(counts),
        "per_day": {"min": min(counts), "max": max(counts),
                    "median": st.median(counts),
                    "mean": round(sum(counts) / len(counts), 1),
                    "spread": round(max(counts) / min(counts), 2) if min(counts) else None},
        "load": {"min": round(min(loads), 2), "max": round(max(loads), 2),
                 "median": round(st.median(loads), 2),
                 "mean": round(sum(loads) / len(loads), 2),
                 "sd": round(st.pstdev(loads), 2),
                 "sd_pct": round(st.pstdev(loads) / (sum(loads) / len(loads)) * 100),
                 "q25": round(q[len(q) // 4], 2), "q75": round(q[3 * len(q) // 4], 2),
                 "work_window": f"{lb.WORK_START}:00–{lb.WORK_END}:00 МСК"},
        "by_weekday": wd,
        "weekday_spread": (round(max(wd.values()) / min(wd.values()), 2)
                           if wd and min(wd.values()) else None),
        "weeks": blocks,
        "week_spread": None,
    }
    if len(blocks) >= 2:
        lv = [b["load"] for b in blocks]
        out["week_spread"] = round(max(lv) / min(lv), 2) if min(lv) else None
    return out


def suggest(summary):
    """Предложение: значение и коридор. Не решение и не запись в конфиг.

    Значение — середина между медианой и средним, округлённая до 0.5. Округление
    не косметика: число до второго знака выглядит как вывод машины, хотя это
    решение человека, и каждый следующий замер двигал бы его на пустом месте.
    Коридор — квартили, наружу до ближайшей половины: сузишь — станет вечно
    красной лампой, расширишь — не сработает никогда.
    """
    if not summary or summary["days"] < MIN_PROBE_DAYS:
        return None
    load = summary["load"]
    value = round((load["median"] + load["mean"]) / 2 * 2) / 2
    lo = int(load["q25"] * 2) / 2                      # вниз до 0.5
    hi = -(-load["q75"] * 2 // 1) / 2                  # вверх до 0.5
    flagged = [w for w in summary["weeks"] if w["load"] > hi or w["load"] < lo]
    return {
        "value": value, "normal_from": lo, "normal_to": hi,
        # Проверка предложения на той же истории: если коридор красит больше
        # половины недель, он слишком узок — это шум, а не сигнал.
        "weeks_total": len(summary["weeks"]),
        "weeks_flagged": len(flagged),
        "flagged": [f"{w['from']}..{w['to']} — {w['load']}" for w in flagged],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="from_time", help="начало окна 'YYYY-MM-DD HH:MM:SS' (МСК)")
    ap.add_argument("--to", dest="to_time", help="конец окна; по умолчанию — вчера 23:59:59")
    ap.add_argument("--days", type=int, default=MIN_PROBE_DAYS,
                    help=f"длина окна, если не задан --from (по умолчанию {MIN_PROBE_DAYS})")
    ap.add_argument("--since", default=(lb.DATA_START.strftime(lb.FMT)
                                        if lb.DATA_START else None),
                    help="нижняя граница пригодных данных (по умолчанию DATA_START)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cache", action="store_true", help="читать/писать ответы API в scripts/cache")
    args = ap.parse_args()

    # Окно по умолчанию кончается вчера: сегодняшний день неполный, и его
    # четверть суток занизила бы и поток, и нагрузку.
    if args.to_time:
        end = dt.datetime.strptime(args.to_time, lb.FMT).replace(tzinfo=lb.MSK)
    else:
        today = dt.datetime.now(lb.MSK).replace(hour=0, minute=0, second=0, microsecond=0)
        end = today - dt.timedelta(seconds=1)
    if args.from_time:
        start = dt.datetime.strptime(args.from_time, lb.FMT).replace(tzinfo=lb.MSK)
    else:
        start = (end + dt.timedelta(seconds=1)) - dt.timedelta(days=args.days)

    since = (dt.datetime.strptime(args.since, lb.FMT).replace(tzinfo=lb.MSK)
             if args.since else None)
    clipped = bool(since and start < since)
    if since:
        start = max(start, since)

    client = OmniClient(cache=args.cache)
    per_day, per_day_work, total = collect(client, start, end)
    online, roster = online_by_day(client, start, end)
    summary = summarize(per_day, per_day_work, online)
    proposal = suggest(summary)
    current = lb.load_reference()

    if args.json:
        print(json.dumps({"window": {"from": start.strftime(lb.FMT),
                                     "to": end.strftime(lb.FMT),
                                     "clipped_by_data_start": clipped},
                          "summary": summary, "suggestion": proposal,
                          "current": current},
                         ensure_ascii=False, indent=2, default=str))
        return

    if not summary:
        print(f"\nВ окне {start:%Y-%m-%d}..{end:%Y-%m-%d} обращений нет — мерить нечего.")
        return

    s = summary
    print(f"\nЗамер нагрузки: {s['from']}..{s['to']} ({s['days']} дн, "
          f"{s['cases_total']} обращений)")
    if clipped:
        print(f"  ⓘ окно обрезано снизу по границе пригодных данных "
              f"({since:%Y-%m-%d}) — раньше неё цифры означали бы неполноту "
              "переезда, а не спокойный поток")
    pd = s["per_day"]
    print(f"\nПоток по дням: мин {pd['min']}, макс {pd['max']}, "
          f"медиана {pd['median']}, среднее {pd['mean']}  →  разброс x{pd['spread']}")

    ld = s["load"]
    print(f"\nНагрузка на оператора в окно {ld['work_window']}:")
    print(f"  медиана {ld['median']}, среднее {ld['mean']}, "
          f"σ {ld['sd']} ({ld['sd_pct']}% от среднего)")
    print(f"  мин {ld['min']}, макс {ld['max']}, квартили {ld['q25']}–{ld['q75']}")

    print(f"\nПо дню недели (обращений/час на оператора)"
          + (f", разброс x{s['weekday_spread']}:" if s["weekday_spread"] else ":"))
    print("  " + "   ".join(f"{k} {v}" for k, v in s["by_weekday"].items()))
    print("  ⓘ Из-за этого разброса норму применяют к периоду ЦЕЛИКОМ: подённое")
    print("    сравнение сделало бы одни дни недели вечным перегрузом, другие простоем.")

    if s["weeks"]:
        print("\nПолные недели окна (то, чего не видно в подённой статистике):")
        for w in s["weeks"]:
            print(f"  {w['from']}..{w['to']}: {w['cases']:>4} обращений, "
                  f"{w['per_day']:>5.1f}/сут, нагрузка {w['load']}")
        if s["week_spread"]:
            print(f"  разброс между неделями: x{s['week_spread']}")

    if current:
        print(f"\nТекущая норма в конфиге: {current['value']}"
              + (f" (обычно {current['normal_from']}–{current['normal_to']})"
                 if current["normal_from"] is not None else "")
              + (f", решение {current['by']}" if current.get("by") else "")
              + (f" от {current['decided']}" if current.get("decided") else ""))

    if not proposal:
        print(f"\n⚠ ПРЕДЛОЖЕНИЯ НЕ БУДЕТ: пригодных данных {s['days']} дн, "
              f"нужно минимум {MIN_PROBE_DAYS}.")
        print("  На таком окне недельный разброс не оценивается — он только")
        print("  кажется маленьким, потому что наблюдений одно-два. Числа выше")
        print("  смотреть можно, ставить по ним норму — нет.")
        return

    print(f"\nПРЕДЛОЖЕНИЕ: норма {proposal['value']} обращений/час на оператора, "
          f"коридор {proposal['normal_from']}–{proposal['normal_to']}")
    print(f"  На этой же истории коридор пометил бы {proposal['weeks_flagged']} "
          f"неделю(-и) из {proposal['weeks_total']}"
          + (":" if proposal["flagged"] else " — то есть сработал бы редко."))
    for f in proposal["flagged"]:
        print(f"    {f}")
    if proposal["weeks_total"] and proposal["weeks_flagged"] > proposal["weeks_total"] / 2:
        print("  ⚠ Больше половины недель вне коридора — он слишком узок и станет")
        print("    вечно красной лампой. Расширьте границы или посмотрите, не")
        print("    случилось ли в окне чего-то разового.")

    print("\nЭто ПРЕДЛОЖЕНИЕ, а не решение: хелпер в конфиг ничего не пишет.")
    print(f"  Решение ставит руководитель — впишите в scripts/{lb.CONFIG_NAME}")
    print("  значение, коридор, дату и автора. Норма, которая пересчитывает себя")
    print("  сама, — это то же скользящее среднее, от которого мы уходим.")


if __name__ == "__main__":
    main()

"""График смен: кто реально работал в этот день, а кто «заглянул».

Зачем. Обычно смену держит один оператор, но время от времени другой заходит
закрыть пару сторонних задач, не выходя на смену. Если считать нагрузку на
оператора, деля дневной поток на число людей, у которых в этот день есть хоть
одно обращение, то день с таким «заглянувшим» внезапно делится на двоих, и
нагрузка того, кто смену реально работал, падает вдвое на ровном месте. Это не
поправка на реальность, а её искажение: человек, закрывший 13 обращений из 156,
смену не работал.

Как определяется смена. Не на слово и не руками — из данных, по доле дня.
На реальных данных (месяц, 30 дней) картина оказалась на редкость чистой:

    доля оператора, который держал смену : 84 … 100 %
    доля заглянувшего                    :  1 …  16 %

Между 16 % и 84 % — пусто, ни одного дня. То есть «был на смене» и «заглянул»
разделяются долей дня практически без серой зоны. Порог берём посередине
пустоты (30 %), а не у края: так он переживёт и день, когда основной оператор
приболел и сделал меньше обычного, и день, когда заглянувший задержался.
На своих данных стоит проверить, что промежуток тоже пустой, и при
необходимости сдвинуть порог в `shifts.json` — но ориентируйтесь именно на
наличие пустоты, а не на удобство конкретного числа.

Порог по доле, а не по числу обращений, — сознательно. Абсолютные числа
заглянувших доходили до 16 обращений в день, и любой абсолютный порог либо
резал бы тихие дни, либо пропускал бы шумные. Доля устойчива к размеру дня.

Настоящие двойные смены механизм не ломает: если двое реально делят день, оба
получают долю выше 30 % и оба считаются на смене.

Ручные переопределения — в `shifts.json` (формат ниже), на случай когда день
надо поправить по знанию, которого в данных нет (отпуск, подмена, обучение).
Как и в calibration.py, решение пишется с датой и автором: график смен влияет
на нагрузку, а нагрузка money-adjacent.

Известное упрощение: день оператору засчитывается по обращениям, СОЗДАННЫМ в
этот день, у которых есть первый ответ и проставлен ответственный. Ответ мог
уйти на следующий день, а ответственный мог смениться позже. Для вопроса «кто
держал смену» этого сигнала достаточно (разрыв между 16 % и 84 % показывает,
что шум не мешает), для поштучного разбора — нет; там смотрите changelog
конкретного обращения.

Запуск:
  python shifts.py --from "2026-07-15 00:00:00" --to "2026-07-30 23:59:59" --cache
  python shifts.py --from ... --to ... --cache --json
"""
import os
import sys
import json
import argparse
import collections
import datetime as dt
from email.utils import parsedate_to_datetime

from omni_client import OmniClient

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

MSK = dt.timezone(dt.timedelta(hours=3))
FMT = "%Y-%m-%d %H:%M:%S"
CONFIG_NAME = "shifts.json"

# Доля дня, начиная с которой считаем, что человек работал смену.
# 0.30 лежит в пустом промежутке между наблюдёнными 16 % и 84 % (см. докстринг).
DEFAULT_SHARE = 0.30
# Ниже этого числа обращений за день доля становится шумной (2 из 4 — это 50 %,
# но это не смена). Такие дни помечаем low_confidence и в среднее не берём.
MIN_DAY_CASES = 10


def config_path(path=None):
    return path or os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


def load_config(path=None):
    """Читает shifts.json. Отсутствия файла достаточно для работы: механизм
    рассчитан на то, что график выводится из данных, а не ведётся руками."""
    p = config_path(path)
    cfg = {"share_threshold": DEFAULT_SHARE, "min_day_cases": MIN_DAY_CASES,
           "exclude_staff": [], "overrides": {}}
    if not os.path.exists(p):
        return cfg
    with open(p, encoding="utf-8") as f:
        cfg.update(json.load(f))
    cfg.setdefault("share_threshold", DEFAULT_SHARE)
    cfg.setdefault("min_day_cases", MIN_DAY_CASES)
    cfg.setdefault("exclude_staff", [])
    cfg.setdefault("overrides", {})
    return cfg


def _day_counts(client, start, end, exclude=()):
    """{'YYYY-MM-DD': Counter{staff_id: обращений}} за окно."""
    exclude = {int(x) for x in exclude}
    by_day = collections.defaultdict(collections.Counter)
    for case in client.iter_cases(from_time=start.strftime(FMT),
                                  to_time=end.strftime(FMT),
                                  show_first_response_time=True):
        sid = case.get("staff_id")
        if not sid or int(sid) in exclude:
            continue
        # Без первого ответа обращение не говорит о том, что человек работал:
        # ответственный мог быть проставлен маршрутизацией, а не человеком.
        if case.get("first_response_speed") in (None, "-", ""):
            continue
        created = case.get("created_at")
        if not created:
            continue
        try:
            day = parsedate_to_datetime(created).astimezone(MSK)
        except (TypeError, ValueError):
            continue
        by_day[day.strftime("%Y-%m-%d")][int(sid)] += 1
    return by_day


def roster(client, from_time, to_time, cfg=None):
    """График смен по дням: кто на смене, кто заглянул.

    Возвращает {'days': {дата: {...}}, 'staff': {...}, порог и настройки}.
    """
    cfg = cfg if cfg is not None else load_config()
    share_min = cfg["share_threshold"]
    min_cases = cfg["min_day_cases"]
    overrides = cfg["overrides"]

    start = dt.datetime.strptime(from_time, FMT).replace(tzinfo=MSK)
    end = dt.datetime.strptime(to_time, FMT).replace(tzinfo=MSK)
    by_day = _day_counts(client, start, end, exclude=cfg["exclude_staff"])

    days = {}
    for day in sorted(by_day):
        counts = by_day[day]
        total = sum(counts.values())
        on_shift, visitors = [], []
        for sid, n in counts.most_common():
            share = n / total if total else 0.0
            row = {"staff_id": sid, "cases": n, "share": round(share, 3)}
            (on_shift if share >= share_min else visitors).append(row)
        entry = {
            "date": day,
            "total": total,
            "on_shift": [r["staff_id"] for r in on_shift],
            "on_shift_detail": on_shift,
            "visitors": visitors,
            "source": "auto",
            "note": None,
            # Слишком тихий день: доля на таких числах ничего не доказывает.
            "low_confidence": total < min_cases,
        }
        ov = overrides.get(day)
        if ov is not None:
            # Ручное решение бьёт вывод из данных — как и в calibration.py.
            forced = [int(x) for x in ov.get("on_shift", [])]
            moved = [r for r in on_shift + visitors if r["staff_id"] not in forced]
            entry["on_shift"] = forced
            entry["on_shift_detail"] = [r for r in on_shift + visitors
                                        if r["staff_id"] in forced]
            entry["visitors"] = moved
            entry["source"] = "override"
            entry["low_confidence"] = False
            note = ov.get("note")
            decided, by = ov.get("decided"), ov.get("by")
            if note and (decided or by):
                note = f"{note} (решение {by or '?'}, {decided or 'без даты'})"
            entry["note"] = note
        days[day] = entry

    return {
        "from": from_time, "to": to_time,
        "share_threshold": share_min,
        "min_day_cases": min_cases,
        "days": days,
    }


def avg_online(rst):
    """Среднее число операторов на смене за период — делитель для нагрузки.

    Дни без внятного сигнала (low_confidence, никого выше порога) в среднее не
    берём: они бы тянули делитель к нулю или к единице без основания. Если
    внятных дней нет вовсе — возвращаем None, и вызывающий обязан это заметить,
    а не подставить молча 1.
    """
    counts = [len(d["on_shift"]) for d in rst["days"].values()
              if d["on_shift"] and not d["low_confidence"]]
    if not counts:
        return None
    return round(sum(counts) / len(counts), 3)


def shift_days(rst, staff_id):
    """Дни, когда конкретный оператор был на смене (не «заглянул»)."""
    sid = int(staff_id)
    return [d for d, v in sorted(rst["days"].items()) if sid in v["on_shift"]]


def visitor_days(rst, staff_id):
    """Дни, когда оператор заглянул мимо смены — и сколько обращений закрыл."""
    sid = int(staff_id)
    out = []
    for day, v in sorted(rst["days"].items()):
        for r in v["visitors"]:
            if r["staff_id"] == sid:
                out.append({"date": day, "cases": r["cases"], "share": r["share"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_time", required=True,
                    help="начало периода 'YYYY-MM-DD HH:MM:SS' (МСК)")
    ap.add_argument("--to", dest="to_time", required=True, help="конец периода")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--config", help="путь к shifts.json")
    args = ap.parse_args()

    client = OmniClient(cache=args.cache)
    cfg = load_config(args.config)
    rst = roster(client, args.from_time, args.to_time, cfg=cfg)

    if args.json:
        print(json.dumps(rst, ensure_ascii=False, indent=2))
        return

    names = client.staff_map()

    def name(sid):
        return str(names.get(sid, sid))

    print(f"\nГрафик смен: {rst['from']} — {rst['to']}")
    print(f"  на смене = доля дня ≥ {rst['share_threshold']:.0%}; "
          f"дни тише {rst['min_day_cases']} обращений считаем несудимыми\n")
    print(f"  {'день':<12}{'всего':>6}  {'на смене':<28}заглянули")
    print("  " + "-" * 74)
    for day, v in sorted(rst["days"].items()):
        on = ", ".join(f"{name(r['staff_id'])} {r['share']:.0%}"
                       for r in v["on_shift_detail"]) or "—"
        vis = ", ".join(f"{name(r['staff_id'])} {r['cases']}" for r in v["visitors"]) or "—"
        mark = "?" if v["low_confidence"] else ("*" if v["source"] == "override" else " ")
        print(f" {mark}{day:<12}{v['total']:>6}  {on:<28}{vis}")
        if v["note"]:
            print(f"     {v['note']}")

    print("\n  «?» — день слишком тихий, вывод о смене ненадёжен; «*» — ручное решение")

    avg = avg_online(rst)
    print(f"\nСреднее операторов на смене: "
          f"{avg if avg is not None else 'не определить — нет внятных дней'}")

    # Сколько нагрузки мы бы приписали «второму оператору», если бы считали
    # смену по факту любого обращения. Это и есть цена вопроса.
    total_vis = sum(r["cases"] for v in rst["days"].values() for r in v["visitors"])
    vis_days = sum(1 for v in rst["days"].values() if v["visitors"])
    total_all = sum(v["total"] for v in rst["days"].values())
    if total_all:
        print(f"Мимо смены закрыто: {total_vis} обращений за {vis_days} дн "
              f"({100.0 * total_vis / total_all:.1f}% потока) — "
              "именно это не должно делить нагрузку основного оператора")
    for sid in sorted({s for v in rst["days"].values() for s in v["on_shift"]}):
        print(f"  {name(sid)}: смен {len(shift_days(rst, sid))}, "
              f"заглядываний {len(visitor_days(rst, sid))}")


if __name__ == "__main__":
    main()

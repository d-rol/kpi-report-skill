"""Этап 1, хелпер №1: нарушения SLA первого ответа с делением на лёгкие/критичные.

Порог SLA первого ответа = 15 мин (значение по умолчанию — при желании поменяйте
константы ниже под свой SLA). Категории по АБСОЛЮТНОМУ времени первого ответа:
  лёгкое   — 15..20 мин   (пробили SLA, но не сильно)
  критичное— больше 20 мин (решение команды: критичным считаем ответ дольше 20 мин)

Время первого ответа берём из детального тикета (first_response_speed, в минутах).
В списке cases.json этого поля нет, поэтому детали тянем по каждому тикету периода.

Запуск:
  python sla_violations.py --days 7
  python sla_violations.py --from "2026-07-12 00:00:00" --to "2026-07-18 23:59:59"
  python sla_violations.py --days 7 --sample 200     # ограничить число тикетов (быстрый прогон)
"""
import sys
import time
import json
import argparse
import datetime as dt

from omni_client import OmniClient

# Windows-консоль по умолчанию не UTF-8 — иначе кириллица превращается в кракозябры.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

SLA_MINUTES = 15.0
CRITICAL_MINUTES = 20.0   # первый ответ дольше 20 мин => критично


def classify(first_response_min):
    """Возвращает (категория, превышение_мин) или (None, 0) если нарушения нет.

    Две категории: 'light' (15..20 мин) и 'critical' (>20 мин). 'medium' больше
    не используется — оставлен пустым в корзинах для обратной совместимости
    вызывающего кода.
    """
    if first_response_min <= SLA_MINUTES:
        return None, 0.0
    excess = first_response_min - SLA_MINUTES
    if first_response_min <= CRITICAL_MINUTES:
        return "light", excess
    return "critical", excess


def parse_speed(value):
    """first_response_speed приходит строкой минут ('1.2') или '-' если ответа не было."""
    if value in (None, "-", ""):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="период = последние N дней от текущего момента")
    ap.add_argument("--from", dest="from_time", help="начало периода 'YYYY-MM-DD HH:MM:SS' (МСК)")
    ap.add_argument("--to", dest="to_time", help="конец периода 'YYYY-MM-DD HH:MM:SS' (МСК)")
    ap.add_argument("--sample", type=int, default=0, help="ограничить число обрабатываемых тикетов (для быстрого прогона)")
    ap.add_argument("--json", action="store_true", help="вывести результат в JSON")
    ap.add_argument("--cache", action="store_true",
                    help="читать/писать ответы API в scripts/cache (для тестов, без лишних запросов)")
    args = ap.parse_args()

    if args.days:
        now = dt.datetime.now()
        from_time = (now - dt.timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
        to_time = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        from_time = args.from_time
        to_time = args.to_time
    if not from_time or not to_time:
        ap.error("укажи либо --days N, либо --from и --to")

    client = OmniClient(cache=args.cache)
    staff = client.staff_map()

    # Тянем тикеты периода одним проходом по списку с show_first_response_time=true —
    # скорость первого ответа приходит прямо в списке, детали по тикетам не нужны.
    buckets = {"light": [], "medium": [], "critical": []}
    no_response = 0
    within_sla = 0
    total_cases = 0
    for case in client.iter_cases(from_time=from_time, to_time=to_time,
                                   show_first_response_time=True):
        total_cases += 1
        spd = parse_speed(case.get("first_response_speed"))
        if spd is None:
            no_response += 1
            continue
        cat, excess = classify(spd)
        if cat is None:
            within_sla += 1
            continue
        sid = case.get("staff_id")
        buckets[cat].append({
            "case_number": case.get("case_number"),
            "case_id": case.get("case_id"),
            "staff": staff.get(sid, sid),
            "first_response_min": round(spd, 1),
            "excess_min": round(excess, 1),
        })
        if args.sample and total_cases >= args.sample:
            break
    total_viol = sum(len(v) for v in buckets.values())
    result = {
        "period": {"from": from_time, "to": to_time},
        "total_cases": total_cases,
        "answered_within_sla": within_sla,
        "no_first_response": no_response,
        "violations_total": total_viol,
        "violations_by_category": {k: len(v) for k, v in buckets.items()},
        "critical_cases": buckets["critical"],
        "medium_cases": buckets["medium"],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"\nПериод: {from_time} — {to_time}")
    print(f"Тикетов в периоде: {total_cases}")
    print(f"  ответ в рамках SLA (<=15 мин): {within_sla}")
    print(f"  без первого ответа (нет данных): {no_response}")
    denom = within_sla + total_viol
    pct = (100.0 * total_viol / denom) if denom else 0.0
    print(f"  нарушений SLA первого ответа: {total_viol} ({pct:.1f}% от ответивших)")
    print(f"    лёгкие   (15–20 мин): {len(buckets['light'])}")
    print(f"    КРИТИЧНЫЕ (>20 мин):  {len(buckets['critical'])}")
    if buckets["critical"]:
        print("\nКритичные случаи (кандидаты на аудит по changelog):")
        for c in sorted(buckets["critical"], key=lambda x: -x["first_response_min"]):
            print(f"    #{c['case_number']}  {c['staff']}  первый ответ {c['first_response_min']} мин "
                  f"(превышение {c['excess_min']} мин)")


if __name__ == "__main__":
    main()

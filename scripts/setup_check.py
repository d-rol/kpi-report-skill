"""Проверка установки перед первым отчётом: что готово, что надо решить.

Зачем. Метрики считаются одинаково на любой машине, но несколько вещей зависят
от КОНКРЕТНОЙ установки, и неверные они не падают с ошибкой — они дают
правдоподобное неверное число. Самая дорогая из них: если вы переезжали в
Omnidesk постепенно, ранняя история занижена неполнотой переезда, и база
нагрузки покажет мнимый рост в разы. Мы на это уже наступали.

Поэтому скрипт не «чинит всё сам», а делит проверки на три исхода:
  ГОТОВО  — можно не трогать;
  ВОПРОС  — нужно ваше решение, автоматика его принять не вправе;
  СМОТРИТЕ — работает на значениях по умолчанию, но стоит свериться со своей
             реальностью (регламент, график смен, пороги SLA).

Запросов к API немного: справочники сотрудников, групп и кастомных полей плюс
два счётчика обращений (первая и последняя неделя истории) — счётчики берутся
из `total_count`, без обхода страниц.

Запуск:  python scripts/setup_check.py
         python scripts/setup_check.py --json
"""
import os
import sys
import json
import argparse
import datetime as dt
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import shifts
import topics
import calibration
import load_baseline
import audit_critical
import sla_violations
from omni_client import OmniClient

OK, ASK, LOOK = "ГОТОВО", "ВОПРОС", "СМОТРИТЕ"

# Во сколько раз поток первой недели должен отличаться от последней, чтобы
# заподозрить постепенный переезд, а не рост бизнеса. Порог грубый нарочно:
# он не решает, а поднимает вопрос — решение всё равно за человеком.
MIGRATION_RATIO = 2.0


def migration_suspected(early_per_day, recent_per_day, ratio=MIGRATION_RATIO):
    """Похоже ли начало истории на неполноту переезда, а не на спокойный поток.

    Отдельная чистая функция, потому что это единственное место скрипта, где
    есть суждение: всё остальное — сбор фактов. Её и проверяет selftest.
    """
    if not early_per_day or not recent_per_day:
        return False
    return recent_per_day / early_per_day >= ratio


def _count(client, frm, to):
    """Сколько обращений в окне — из total_count, без обхода страниц (1 запрос)."""
    data = client.get("cases.json", {"limit": 1, "page": 1,
                                     "from_time": frm, "to_time": to})
    try:
        return int(data.get("total_count") or 0)
    except (TypeError, ValueError):
        return 0


def _account_start(client):
    """Дата самого первого обращения в аккаунте (1 запрос)."""
    for case in client.iter_cases(sort="created_at_asc"):
        d = load_baseline.parse_created(case.get("created_at"))
        return d
    return None


def collect(client):
    checks = []

    def add(status, title, detail, action=None):
        checks.append({"status": status, "title": title, "detail": detail,
                       "action": action})

    # --- 1. Нижняя граница пригодных данных: главный источник тихой ошибки ---
    start = _account_start(client)
    if start is None:
        add(ASK, "Нижняя граница пригодных данных",
            "Обращений в аккаунте не нашлось — проверьте доступ и период.")
    else:
        week = dt.timedelta(days=7)
        early = _count(client, start.strftime(load_baseline.FMT),
                       (start + week).strftime(load_baseline.FMT)) / 7.0
        now = dt.datetime.now(load_baseline.MSK)
        recent = _count(client, (now - week).strftime(load_baseline.FMT),
                        now.strftime(load_baseline.FMT)) / 7.0
        cur = load_baseline.DATA_START
        detail = (f"История с {start:%Y-%m-%d}. Поток первой недели "
                  f"{early:.1f}/сут, последней — {recent:.1f}/сут.")
        if cur:
            add(OK, "Нижняя граница пригодных данных",
                detail + f" Граница задана: {cur:%Y-%m-%d}.")
        elif migration_suspected(early, recent):
            add(ASK, "Нижняя граница пригодных данных",
                detail + f" Разница в {recent / max(early, 1e-9):.1f} раза.",
                "Это рост бизнеса или постепенный переезд в Omnidesk? Если "
                "переезд — поставьте дату, с которой Omnidesk стал основным "
                "каналом, в DATA_START (scripts/load_baseline.py). Иначе база "
                "нагрузки покажет мнимый рост, и решения о порогах SLA будут "
                "приняты по артефакту.")
        else:
            add(OK, "Нижняя граница пригодных данных",
                detail + " Признаков постепенного переезда не видно.")

    # --- 2. Рабочее окно: из него считается и удержание, и ночная очередь ---
    add(LOOK, "Рабочее окно",
        f"Сейчас {audit_critical.WORK_START_H}:00–{audit_critical.WORK_END_H}:00 МСК.",
        "Оно должно совпадать с рабочим временем, настроенным в Omnidesk: по "
        "нему Omnidesk считает first_response_speed, а мы — удержание и ночную "
        "очередь. Разойдутся — обе цифры останутся правдоподобными и будут "
        "неверными. Меняется в scripts/audit_critical.py (WORK_START_H/WORK_END_H) "
        "и scripts/load_baseline.py (WORK_START/WORK_END), обязательно вместе.")

    # --- 3. Пороги SLA ---
    add(LOOK, "Пороги SLA",
        f"Нарушение — дольше {sla_violations.SLA_MINUTES:.0f} мин, критичное — "
        f"дольше {sla_violations.CRITICAL_MINUTES:.0f} мин.",
        "Это значения по умолчанию. Приведите их к своему SLA — от них зависит "
        "весь разбор. Полный список порогов: python scripts/settings.py")

    # --- 4. Сотрудники: боты в метриках операторов не нужны ---
    staff = client.staff_map()
    names = ", ".join(f"{n} ({i})" for i, n in sorted(staff.items(),
                                                      key=lambda x: str(x[1])))
    add(ASK, "Список операторов",
        f"Омнидеск знает: {names or '—'}.",
        "Кто из них живой оператор, а кто бот/системная учётка? Впишите живых "
        "в раздел «Разбери запрос» файла .claude/skills/kpi-report/SKILL.md — "
        "тогда имена будут узнаваться без уточняющих вопросов, а по ботам "
        "отчёт не будет строиться по ошибке.")

    # --- 5. Молодые направления: грация авто, но переезды — решение человека ---
    at = dt.datetime.now(calibration.MSK)
    st = calibration.grace_status(client, at=at, cfg=calibration.load_config())
    young = [g for g in st["groups"].values() if g["in_grace"]]
    if young:
        lst = ", ".join(f"{g['title']} ({g['age_days']:.0f} дн)" for g in young)
        add(ASK, "Молодые направления",
            f"Моложе {st['grace_weeks']} нед: {lst}.",
            "Все ли они действительно новые? Направление может быть формально "
            "новым, а на деле переездом старого потока — тогда грация ему не "
            "нужна. Переопределения: scripts/calibration.json "
            "(шаблон — calibration.example.json).")
    else:
        add(OK, "Молодые направления",
            f"Направлений моложе {st['grace_weeks']} нед нет — грация не нужна.")

    # --- 6. Тема обращения: разрез по темам без неё молчит ---
    tf = topics.topic_fields(client)
    if tf:
        titles = ", ".join(f["title"] for f in tf.values())
        add(OK, "Тема обращения", f"Найдены поля-классификаторы: {titles}.")
    else:
        add(LOOK, "Тема обращения",
            "Кастомного поля с названием вида «Тема обращения» на уровне "
            "обращения не нашлось.",
            "Разрез просрочек по темам будет пустым. Это не поломка: заведите "
            "такое поле в Omnidesk, когда понадобится — оно подхватится само, "
            "искать его по id не нужно.")

    # --- 7. Локальные настройки ---
    here = os.path.dirname(os.path.abspath(__file__))
    for name, what in ((shifts.CONFIG_NAME, "график смен: порог, боты, ручные правки дней"),
                       (calibration.CONFIG_NAME, "переопределения грации")):
        exists = os.path.exists(os.path.join(here, name))
        add(OK if exists else LOOK, f"Файл настроек {name}",
            "есть" if exists else "нет — работают значения по умолчанию",
            None if exists else f"Нужен, только если хотите поменять {what}. "
                                f"Шаблон: {name.replace('.json', '.example.json')}")

    # --- 8. Порог смены: значение по умолчанию выведено НЕ из ваших данных ---
    add(LOOK, "Порог смены",
        f"Доля дня {shifts.DEFAULT_SHARE:.0%} отделяет «держал смену» от «заглянул».",
        "Порог выведен из чужих данных. Прогоните python scripts/shifts.py "
        "--from ... --to ... --cache на своём месяце и убедитесь, что между "
        "долями есть пустой промежуток: если операторы регулярно делят день "
        "пополам, порог надо опускать, иначе делитель нагрузки поедет.")

    return checks


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cache", action="store_true",
                    help="читать/писать ответы API в scripts/cache")
    ap.add_argument("--env", dest="env_path", default=None)
    args = ap.parse_args()

    try:
        client = OmniClient(cache=args.cache, env_path=args.env_path)
    except Exception as e:
        print("Не удалось подключиться к Omnidesk: "
              f"{type(e).__name__}: {e}\n"
              "Проверьте .env (OMNIDESK_API, EMAIL, SUBDOMAIN) — без доступа "
              "остальные проверки бессмысленны.", file=sys.stderr)
        return 2

    checks = collect(client)
    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0

    print("ПРОВЕРКА УСТАНОВКИ\n")
    for c in checks:
        print(f"[{c['status']}] {c['title']}")
        print(f"    {c['detail']}")
        if c["action"]:
            for line in _wrap(c["action"], 74):
                print(f"    → {line}")
        print()

    asks = [c for c in checks if c["status"] == ASK]
    looks = [c for c in checks if c["status"] == LOOK]
    print(f"Требуют вашего решения: {len(asks)}. Стоит свериться: {len(looks)}.")
    if asks:
        print("Пока на них не ответили, отчёт всё равно считается — но по "
              "значениям по умолчанию, и часть цифр может быть неверной молча.")
    return 0


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    sys.exit(main())

"""Все референсные значения, на которые опирается расчёт — одним списком.

Зачем. Пороги разбросаны по шести модулям, и понять «на чём вообще стоит цифра
в отчёте» можно было только чтением кода. Статический список в документации эту
проблему не решает, а маскирует: он расходится с кодом при первой же правке, и
тогда врёт увереннее, чем его отсутствие. Поэтому значения здесь не переписаны
руками, а **читаются из самих модулей** — разойтись с расчётом они не могут.

Что показывает:
  * текущее значение и где оно лежит;
  * на что влияет — то есть что именно поедет в отчёте, если его тронуть;
  * можно ли менять и на каком основании.

Отдельный раздел — чего в расчёте СОЗНАТЕЛЬНО нет. Отсутствие правила или числа
это тоже решение, и не записанное оно выглядит как недоделка.

Запуск:  python scripts/settings.py
         python scripts/settings.py --json
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import shifts
import topics
import report
import ref_probe
import calibration
import load_baseline
import no_responsible
import setup_check
import sla_violations
import audit_critical

# Уровни «можно ли трогать» — по тому, чем обосновано число, а не по риску правки.
DECISION = "решение руководителя"   # money-adjacent: влияет на оценку людей
INSTALL = "под свою установку"      # зависит от вашего Омнидеска/регламента
DERIVED = "выведено из данных"      # менять только вместе с новым замером

# (модуль, имя атрибута, единица, на что влияет, основание)
GROUPS = [
    ("SLA первого ответа", [
        (sla_violations, "SLA_MINUTES", "мин",
         "Граница нарушения. Всё, что дольше — просрочка в отчёте.",
         DECISION),
        (sla_violations, "CRITICAL_MINUTES", "мин",
         "Граница лёгкое/критичное. Критичные проходят поштучный аудит, "
         "лёгкие идут как есть.",
         DECISION),
    ]),
    ("Аудит критичных случаев", [
        (audit_critical, "HELD_SLA_MIN", "мин",
         "Удержание, до которого нарушение считается УНАСЛЕДОВАННЫМ (оператор "
         "разобрал чужое зависшее). Выше — личное. Главный порог всего аудита.",
         DECISION),
        (audit_critical, "BORDERLINE_HELD_MAX", "мин",
         "До какого удержания личное нарушение считается ПОГРАНИЧНЫМ и уходит "
         "руководителю на ручную оценку вместо автоматического вердикта.",
         DECISION),
        (audit_critical, "WORK_START_H", "час МСК",
         "Начало рабочего окна. Удержание считается только внутри него — иначе "
         "ночь между вечерним назначением и утренним ответом шла бы в вину. "
         "Это же окно определяет, что считать ночной очередью (backlog.py).",
         INSTALL),
        (audit_critical, "WORK_END_H", "час МСК",
         "Конец рабочего окна (см. выше).",
         INSTALL),
    ]),
    ("Нагрузка", [
        (load_baseline, "DATA_START", "дата",
         "Нижняя граница ПРИГОДНЫХ данных. По умолчанию не задана. Если вы "
         "переезжали в Omnidesk постепенно — поставьте дату, с которой он стал "
         "основным каналом: иначе неполнота переезда прочитается как рост потока.",
         DERIVED),
        (load_baseline, "MIN_BASELINE_DAYS", "суток",
         "Минимум данных для базы. Меньше — отчёт отказывается сравнивать и "
         "пишет «сравнивать не с чем» вместо красивой бессмыслицы.",
         DERIVED),
        (load_baseline, "DEFAULT_BASELINE_WEEKS", "недель",
         "Глубина скользящей базы. Короче — шумит, длиннее — тянет устаревшую "
         "картину потока.",
         INSTALL),
        (load_baseline, "SPIKE_MIN_CASES", "обращений",
         "Сколько обращений в часе нужно, чтобы час вообще рассматривался как "
         "всплеск. Без этого 3 обращения ночью дают всплеск на пустом месте.",
         DERIVED),
        (load_baseline, "SPIKE_RATIO", "раз",
         "Во сколько раз выше обычного — всплеск. Такие часы помечаются у "
         "критичных случаев (вердикт при этом НЕ меняется).",
         DERIVED),
        (load_baseline, "WORK_START", "час МСК",
         "Рабочее окно для нагрузки на оператора. ВНИМАНИЕ: то же окно отдельно "
         "задано в audit_critical — менять надо оба, иначе удержание и нагрузка "
         "начнут считаться по разным суткам. Расхождение ловит selftest.",
         INSTALL),
        (load_baseline, "WORK_END", "час МСК",
         "Конец того же окна (см. выше).",
         INSTALL),
        (load_baseline, "SHORT_PERIOD_DAYS", "суток",
         "Короче этого периода сравнение с нормой сопровождается оговоркой: на "
         "двух-трёх днях состав дней недели перевешивает сам сигнал.",
         DERIVED),
    ]),
    ("Замер нормы (ref_probe.py)", [
        (ref_probe, "MIN_PROBE_DAYS", "суток",
         "Минимум данных, при котором замер вообще предлагает норму. Меньше — "
         "печатает числа, но предложения не даёт: недельный разброс на одном-двух "
         "наблюдениях не оценивается, а только кажется маленьким.",
         DERIVED),
    ]),
    ("График смен (делитель нагрузки)", [
        (shifts, "DEFAULT_SHARE", "доля дня",
         "Порог «держал смену» против «заглянул закрыть пару задач». Ниже "
         "порога человек в делитель нагрузки не идёт.",
         DERIVED),
        (shifts, "MIN_DAY_CASES", "обращений",
         "Ниже этого потока за день доли ненадёжны, день помечается таким.",
         DERIVED),
    ]),
    ("Калибровочная грация молодых направлений", [
        (calibration, "DEFAULT_GRACE_WEEKS", "недель",
         "Сколько недель с запуска направление считается молодым. Его нарушения "
         "показываются отдельной строкой и НЕ вычитаются.",
         DECISION),
    ]),
    ("Темы обращений", [
        (topics, "MIN_COVERAGE", "доля",
         "Ниже этой разметки отчёт прямо предупреждает, что порядок тем ещё "
         "может перевернуться.",
         DERIVED),
        (report, "TOPIC_ROWS_LIMIT", "строк",
         "Сколько тем показывать. Скрытое число называется вслух, полный "
         "список остаётся в --json.",
         INSTALL),
    ]),
    ("Проверка установки", [
        (setup_check, "MIGRATION_RATIO", "раз",
         "Во сколько раз поток последней недели должен превышать первую, "
         "чтобы setup_check заподозрил постепенный переезд в Omnidesk и "
         "спросил про нижнюю границу данных. Порог только поднимает "
         "вопрос, сам ничего не решает.",
         DERIVED),
    ]),
    ("Очередь и забытые обращения", [
        (no_responsible, "FORGOTTEN_MIN_AGE_HOURS", "часов",
         "С какого возраста взятое в работу обращение без первого ответа "
         "попадает в «Забытые в работе».",
         DECISION),
    ]),
]

# Чего в расчёте нет — и почему. Отсутствие числа это тоже решение.
ABSENT = [
    ("Правило «норматив соблюдён / не соблюдён»",
     "Сознательно нет: считаем только метрики, решение за руководителем."),
    ("Композитный KPI-рейтинг одним числом",
     "Сознательно нет — по той же причине."),
    ("Оценка качества ответов",
     "Требует включённых клиентских оценок в Omnidesk, а на практике ещё и "
     "переработки регламента ведения диалога оператором."),
    ("Расписание/крон",
     "Запрещено архитектурно: период и сотрудник всегда приходят из запроса — "
     "у сотрудников бывает разный график выплат."),
]

CONFIGS = [
    ("shifts.json", shifts.CONFIG_NAME,
     "порог смены, исключения ботов, ручные правки конкретных дней"),
    ("calibration.json", calibration.CONFIG_NAME,
     "переопределения грации: направление формально новое, но это переезд старого"),
    ("load_reference.json", load_baseline.CONFIG_NAME,
     "фиксированная норма нагрузки: значение, коридор, дата и автор решения"),
]


def collect():
    out = {"groups": [], "absent": [], "configs": []}
    for title, items in GROUPS:
        rows = []
        for mod, attr, unit, effect, basis in items:
            rows.append({
                "name": attr,
                "value": getattr(mod, attr),
                "unit": unit,
                "source": f"{mod.__name__}.py",
                "effect": effect,
                "basis": basis,
            })
        out["groups"].append({"title": title, "items": rows})
    out["absent"] = [{"what": w, "why": y} for w, y in ABSENT]
    # Норма нагрузки живёт в конфиге, а не в модуле: её ставит руководитель, и
    # у неё есть дата и автор. Читаем её так же живьём, как константы, — список,
    # переписанный руками, разошёлся бы с конфигом при первой правке.
    out["reference"] = load_baseline.load_reference()
    for label, name, what in CONFIGS:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        out["configs"].append({"file": label, "present": os.path.exists(path),
                               "controls": what})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    args = ap.parse_args()

    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return

    print("РЕФЕРЕНСНЫЕ ЗНАЧЕНИЯ РАСЧЁТА")
    print("Значения прочитаны из модулей — этот список не может разойтись с кодом.\n")
    for g in data["groups"]:
        print(f"▸ {g['title']}")
        for it in g["items"]:
            # Единицу не приписываем к «не задана» — получилось бы «не задана дата».
            raw = fmt_value(it['value'])
            val = raw if it['value'] is None else f"{raw} {it['unit']}".strip()
            print(f"    {it['name']:<24} {val:<22} [{it['basis']}]  {it['source']}")
            for line in wrap(it["effect"], 78):
                print(f"        {line}")
        print()

    ref = data.get("reference")
    print("▸ Норма нагрузки (ответ на «тяжёлый ли период вообще»)")
    if ref:
        corridor = ""
        if ref["normal_from"] is not None and ref["normal_to"] is not None:
            corridor = f", обычно {ref['normal_from']}–{ref['normal_to']}"
        who = ", ".join(x for x in (ref.get("by"), ref.get("decided")) if x)
        print(f"    {ref['value']} обращений/час на оператора{corridor}"
              f"   [{DECISION}]  {load_baseline.CONFIG_NAME}")
        if who:
            print(f"        решение: {who}"
                  + (f"; замер {ref['measured_on']}" if ref.get("measured_on") else ""))
        for line in wrap(ref.get("note") or "", 78):
            print(f"        {line}")
        print("        Сравнение по периоду ЦЕЛИКОМ, не по дням. Автовывода нет "
              "сознательно:")
        print("        число, пересчитывающее себя по последним неделям, — это то "
              "же скользящее")
        print("        среднее, от которого мы уходим.")
    else:
        print("    не задана — отчёт отвечает только «необычен ли поток "
              "относительно недавнего»,")
        print("    но НЕ «тяжёлый ли период вообще». Задать: скопировать "
              f"{load_baseline.CONFIG_NAME[:-5]}.example.json")
        print(f"    в {load_baseline.CONFIG_NAME} и поставить свои числа.")
    print()

    print("▸ Файлы настроек (не обязательны — без них работают значения по умолчанию)")
    for c in data["configs"]:
        mark = "есть" if c["present"] else "нет"
        print(f"    {c['file']:<24} {mark:<22} {c['controls']}")
    print()

    print("▸ Чего в расчёте НЕТ (и это решение, а не недоделка)")
    for a in data["absent"]:
        print(f"    {a['what']}")
        for line in wrap(a["why"], 78):
            print(f"        {line}")
    print()
    print(f"Основания: [{DECISION}] — влияет на оценку людей, менять согласованно; "
          f"\n           [{INSTALL}] — зависит от вашего Омнидеска и регламента; "
          f"\n           [{DERIVED}] — менять только вместе с новым замером.")


def fmt_value(v):
    """Дату печатаем днём: полный timestamp с зоной ломает колонку и ничего не
    добавляет — граница пригодных данных задаётся именно днём."""
    if v is None:
        return "не задана"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


def wrap(text, width):
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
    main()

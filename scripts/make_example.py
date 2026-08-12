"""Собирает образец отчёта на ВЫДУМАННЫХ данных — в examples/.

Зачем генератор, а не файл руками. Образец, положенный руками, расходится с
рендером при первой же правке формата, и человек начинает ориентироваться на то,
чего код уже не делает; устаревший образец хуже, чем никакого. Плюс образец «из
настоящего отчёта, только вычищенный» опасен: один пропущенный номер обращения
или поддомен — и в публичном репозитории лежат чужие данные.

Здесь данных для вычистки нет вовсе: клиент подставной, обращения выдуманы,
имена условные. При этом отчёт собирается НАСТОЯЩИМ `report.gather()` и
рендерится настоящими рендерами — то есть образец не может показать формат,
которого код не производит.

Детерминированность. Живая очередь и «забытые в работе» считают возраст от
текущего момента, поэтому после сборки эти числа заменяются фиксированными:
иначе образец менялся бы каждый день и проверка на расхождение падала бы на
ровном месте. Всё остальное — как есть.

Запуск:  python scripts/make_example.py           # записать examples/
         python scripts/make_example.py --check   # сверить, не устарел ли
"""
import os
import sys
import argparse
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import shifts
import report
import calibration
import load_baseline
import report_html

MSK = load_baseline.MSK
FMT = load_baseline.FMT

STAFF = {101: "Оператор А", 102: "Оператор Б", 0: "без ответственного"}
SUBJECT_ID = 101
PERIOD = ("2026-03-02 00:00:00", "2026-03-08 23:59:59")
HISTORY_FROM = dt.datetime(2026, 2, 1, tzinfo=MSK)   # база нагрузки: >7 дней до периода

GROUPS = [
    {"group_id": 1, "group_title": "Продукт А", "created_at": "Mon, 01 Sep 2025 10:00:00 +0300"},
    {"group_id": 2, "group_title": "Продукт Б", "created_at": "Mon, 16 Feb 2026 10:00:00 +0300"},
]
TOPIC_FIELD = {
    "field_id": 4101, "title": "Тема обращения (продукт А)", "field_type": "select",
    "field_level": "case",
    "field_data": {"1": "Не работает", "2": "Возврат денег",
                   "3": "Оплата не прошла", "4": "Как пользоваться"},
}
# Поле уровня клиента — в образце нарочно: показывает, что такие в разрез не идут.
USER_FIELD = {"field_id": 4103, "title": "Баланс", "field_type": "textarea",
              "field_level": "user", "field_data": {}}


def rfc(d):
    return d.strftime("%a, %d %b %Y %H:%M:%S +0300")


def _case(cid, num, when, speed, staff_id, group_id=1, topic=None,
          status="closed", subject="Вопрос клиента"):
    c = {"case_id": cid, "case_number": num, "created_at": rfc(when),
         "staff_id": staff_id, "group_id": group_id, "status": status,
         "subject": subject,
         "first_response_speed": "-" if speed is None else f"{speed:.1f}"}
    if topic:
        c["custom_fields"] = {"cf_4101": topic}
    return c


def build_cases():
    """Выдуманный поток: ровная история для базы + показательная неделя."""
    cases, cid = [], 1000

    # История до периода — нужна скользящей базе нагрузки.
    day = HISTORY_FROM
    while day < dt.datetime(2026, 3, 2, tzinfo=MSK):
        for hour in (9, 11, 14, 17, 20):
            cid += 1
            cases.append(_case(cid, f"100-{cid}", day.replace(hour=hour),
                               6.0, 101 if hour % 2 else 102,
                               topic=str(hour % 4 + 1)))
        day += dt.timedelta(days=1)

    # Неделя отчёта. Часть — в норме, часть — нарушения разного рода.
    start = dt.datetime(2026, 3, 2, tzinfo=MSK)
    for d in range(7):
        base = start + dt.timedelta(days=d)
        for hour in (11, 13, 15, 18, 20):       # в норме, дневные
            cid += 1
            cases.append(_case(cid, f"200-{cid}", base.replace(hour=hour), 5.0,
                               SUBJECT_ID, topic=str(hour % 4 + 1)))
        cid += 1                                 # ночное: часы стартуют в 10:00
        cases.append(_case(cid, f"300-{cid}", base.replace(hour=4), 38.0,
                           SUBJECT_ID, topic="1"))
        cid += 1                                 # пришло, пока разбирали ночь
        cases.append(_case(cid, f"350-{cid}", base.replace(hour=10, minute=20),
                           22.0, SUBJECT_ID, topic="4"))
        cid += 1                                 # лёгкое нарушение днём
        cases.append(_case(cid, f"400-{cid}", base.replace(hour=16), 17.0,
                           SUBJECT_ID, topic="2"))
        cid += 1                                 # из молодого направления
        cases.append(_case(cid, f"500-{cid}", base.replace(hour=12), 8.0,
                           SUBJECT_ID, group_id=2, topic="3"))
    return cases, cid


# Критичные случаи с историей: каждый показывает свой вердикт аудита.
CRITICAL = [
    # (id, номер, приход, скорость, тема, события changelog, ответ)
    (9001, "901-9001", dt.datetime(2026, 3, 3, 4, 20, tzinfo=MSK), 52.0, "1",
     [("fixed_chat", "0", "101", "omnidesk", dt.datetime(2026, 3, 3, 10, 51, tzinfo=MSK))],
     dt.datetime(2026, 3, 3, 10, 52, tzinfo=MSK)),          # унаследовано, ночная очередь
    (9002, "902-9002", dt.datetime(2026, 3, 4, 13, 5, tzinfo=MSK), 96.0, "2",
     [("staff", "0", "101", "staff_101", dt.datetime(2026, 3, 4, 13, 10, tzinfo=MSK))],
     dt.datetime(2026, 3, 4, 14, 41, tzinfo=MSK)),          # личное: держал 91 мин
    (9003, "903-9003", dt.datetime(2026, 3, 5, 15, 30, tzinfo=MSK), 31.0, "3",
     [("fixed_chat", "0", "101", "rule_4242", dt.datetime(2026, 3, 5, 15, 35, tzinfo=MSK))],
     dt.datetime(2026, 3, 5, 16, 1, tzinfo=MSK)),           # пограничное: авто-чат, 26 мин
    (9004, "904-9004", dt.datetime(2026, 3, 6, 11, 0, tzinfo=MSK), 74.0, "4",
     [("staff", "0", "102", "staff_102", dt.datetime(2026, 3, 6, 11, 5, tzinfo=MSK)),
      ("staff", "102", "101", "staff_101", dt.datetime(2026, 3, 6, 12, 8, tzinfo=MSK))],
     dt.datetime(2026, 3, 6, 12, 14, tzinfo=MSK)),          # передано от другого
]

# Живые обращения без ответа — для «забытых в работе» и очереди риска.
LIVE = [
    _case(9101, "911-9101", dt.datetime(2026, 3, 1, 12, 0, tzinfo=MSK), None,
          SUBJECT_ID, status="open", subject="Скриншот не открывается"),
    _case(9102, "912-9102", dt.datetime(2026, 3, 6, 9, 0, tzinfo=MSK), None,
          0, status="open", subject="Не приходит письмо"),
]


class FakeOmni:
    """Подставной клиент: отдаёт ровно ту форму данных, что настоящий."""

    # Из него собирается ссылка на обращение — в образце поддомен условный.
    subdomain = "example"

    def __init__(self):
        self.cases, _ = build_cases()
        for cid, num, when, speed, topic, _ev, _rep in CRITICAL:
            self.cases.append(_case(cid, num, when, speed, SUBJECT_ID, topic=topic))
        self.crit = {c[0]: c for c in CRITICAL}

    def iter_cases(self, from_time=None, to_time=None, status=None,
                   show_first_response_time=False, sort=None):
        if status == "open":
            for c in LIVE:
                yield c
            return
        s = dt.datetime.strptime(from_time, FMT).replace(tzinfo=MSK) if from_time else None
        e = dt.datetime.strptime(to_time, FMT).replace(tzinfo=MSK) if to_time else None
        for c in self.cases:
            when = load_baseline.parse_created(c["created_at"])
            if (s and when < s) or (e and when > e):
                continue
            yield c

    def staff_map(self):
        return dict(STAFF)

    def custom_fields_map(self):
        return {"4101": TOPIC_FIELD, "4103": USER_FIELD}

    def _answered_at(self, cid):
        """Когда по обращению ушёл первый ответ: приход + скорость Омнидеска.

        Ночные считаются от открытия смены — так же, как их считает Омнидеск.
        """
        c = next((x for x in self.cases if x["case_id"] == cid), None)
        if c is None or c["first_response_speed"] == "-":
            return None
        came = load_baseline.parse_created(c["created_at"])
        start = came if 10 <= came.hour < 22 else came.replace(
            hour=10, minute=0, second=0, microsecond=0)
        return start + dt.timedelta(minutes=float(c["first_response_speed"]))

    def get(self, path, params=None):
        if path.startswith("stats_leaderboard"):
            return {"0": {"staff": {"staff_id": SUBJECT_ID, "staff_name": STAFF[SUBJECT_ID],
                                    "first_response_time": 222, "response_time": 78,
                                    "total_number_of_responses": 486, "closed_cases": 352,
                                    "first_response_sla_violated": "18.9%"}}}
        if path == "groups.json":
            return {str(i): {"group": g} for i, g in enumerate(GROUPS)}
        if "/changelog.json" in path:
            cid = int(path.split("/")[1])
            item = self.crit.get(cid)
            if not item:
                # Типовой случай: обращение висело ничьим, оператора назначил
                # Омнидеск в момент ответа. Так выглядит разбор общей очереди.
                ts = self._answered_at(cid)
                return {"changelog": [] if ts is None else [
                    {"event": "fixed_chat", "old_value": "0", "value": str(SUBJECT_ID),
                     "done_by": "omnidesk",
                     "created_at": rfc(ts - dt.timedelta(minutes=1))}]}
            return {"changelog": [
                {"event": ev, "old_value": old, "value": new, "done_by": by,
                 "created_at": rfc(ts)} for ev, old, new, by, ts in item[5]]}
        if "/messages.json" in path:
            cid = int(path.split("/")[1])
            item = self.crit.get(cid)
            if not item:
                ts = self._answered_at(cid)
                return {} if ts is None else {
                    "0": {"message": {"message_type": "reply_staff",
                                      "created_at": rfc(ts),
                                      "staff_id": SUBJECT_ID}}}
            return {"0": {"message": {"message_type": "reply_staff",
                                      "created_at": rfc(item[6]),
                                      "staff_id": SUBJECT_ID}}}
        return {}


# Возраст живых обращений считается от «сейчас», поэтому в образце его
# фиксируем: иначе файл менялся бы каждый день и --check падал бы впустую.
FROZEN_LIVE = {"oldest_hours": 148.5, "median_hours": 148.5}
FROZEN_FORGOTTEN_HOURS = 148.5


def build_report():
    # Настройки установки прибиваем к стенду: образец должен собираться
    # одинаково в любой копии проекта, а не наследовать локальные файлы.
    load_baseline.DATA_START = None
    # Норма нагрузки — настройка установки: где-то задана, где-то нет.
    # Прибиваем к стенду, иначе образец собирался бы по-разному в разных
    # копиях и selftest ловил бы мнимое устаревание.
    load_baseline.load_reference = lambda path=None: {
        "value": 7.5, "normal_from": 6.5, "normal_to": 9.0,
        "decided": "2026-08-12", "by": "руководитель",
        "measured_on": "четыре полные недели", "note": None}
    shifts.load_config = lambda path=None: {"share_threshold": 0.30,
                                            "min_day_cases": 10,
                                            "exclude_staff": [], "overrides": {}}
    calibration.load_config = lambda path=None: {"grace_weeks": 4, "auto_detect": True,
                                                 "overrides": {}}
    client = FakeOmni()
    r = report.gather(client, SUBJECT_ID, STAFF[SUBJECT_ID], PERIOD[0], PERIOD[1],
                      want_team=True)
    r["case_url_tpl"] = "https://example.omnidesk.ru/staff/cases/record/{case_number}"

    live = (r.get("team_no_responsible") or {}).get("live")
    if live:
        live.update(FROZEN_LIVE)
        for w in live.get("waiting", []):
            w["age_hours"] = FROZEN_LIVE["oldest_hours"]
    for lst in (r.get("forgotten_in_work") or {}).get("by_staff", {}).values():
        for item in lst:
            item["age_hours"] = FROZEN_FORGOTTEN_HOURS
    return r


# Предупреждение ВИДИМОЕ, а не в HTML-комментарии: на GitHub комментарий
# скрывается при рендере, и человек увидел бы отчёт с цифрами без единого
# признака, что они выдуманы. Ровно та тихая неправда, которой проект избегает.
HEADER = (
    "> **Это образец на выдуманных данных.** Обращения, имена и цифры "
    "условные — файл собран подставным клиентом, а не вычищен из чьего-то "
    "настоящего отчёта.\n>\n"
    "> Собирается автоматически: `python scripts/make_example.py`. Править "
    "руками нельзя — расхождение с рендером ловит `selftest.py`.\n\n")

# Тот же смысл для HTML: его открывают отдельно от репозитория, и там тоже
# должно быть сразу видно, что данные ненастоящие.
HTML_BANNER = (
    "<div class=card style='border-color:var(--warn)'>"
    "<h2 style='color:var(--warn)'>Образец на выдуманных данных</h2>"
    "<p class=hint>Обращения, имена и цифры условные — страница собрана "
    "подставным клиентом (<code>scripts/make_example.py</code>), а не вычищена "
    "из настоящего отчёта. Показывает формат, а не чью-то статистику.</p></div>")


def render_all():
    r = build_report()
    return {
        "report_manager.md": HEADER + report.render_manager(r) + "\n",
        "report_personal.md": HEADER + report.render_personal(r) + "\n",
        "report_manager.html": report_html.render_html(r, "manager").replace(
            "<div class=wrap>", "<div class=wrap>" + HTML_BANNER, 1),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="не писать, а сверить: не устарели ли файлы в examples/")
    ap.add_argument("--dir", default=None, help="куда писать (по умолчанию ../examples)")
    args = ap.parse_args()

    root = args.dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
    files = render_all()

    if args.check:
        stale = []
        for name, body in files.items():
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as f:
                    have = f.read()
            except FileNotFoundError:
                stale.append(f"{name}: нет файла")
                continue
            if have.replace("\r\n", "\n") != body.replace("\r\n", "\n"):
                stale.append(f"{name}: расходится с текущим рендером")
        if stale:
            print("Образцы устарели:", file=sys.stderr)
            for s in stale:
                print(f"  - {s}", file=sys.stderr)
            print("Пересоберите: python scripts/make_example.py", file=sys.stderr)
            return 1
        print("Образцы совпадают с текущим рендером.")
        return 0

    os.makedirs(root, exist_ok=True)
    for name, body in files.items():
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        print(f"записан {os.path.join(root, name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

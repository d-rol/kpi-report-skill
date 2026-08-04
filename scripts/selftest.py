"""Оффлайн-самопроверка хелперов: без сети, без .env, без кэша.

Зачем отдельный файл. Проверить «а не сломалось ли» на живом Омнидеске дорого
(rate limit, 20 запросов/мин) и невоспроизводимо: данные меняются, вчерашний
эталон сегодня не сходится. Поэтому логика, которую легко сломать молча,
проверяется на подставных данных — прогон занимает секунду и не требует доступа
к API. Это НЕ замена сверке с живым Омнидеском, а её дешёвая нижняя граница:
здесь ловятся регрессии в арифметике и в пограничных ветках.

Что проверяем (каждый пункт — реально сломанное или почти сломанное место):
  * ключи корзин «день недели × час» — кортеж в JSON не сериализуется, поэтому
    наружу они уходят строками; чтение по кортежу молча вернуло бы 0;
  * нижняя граница пригодных данных: без границы / с границей / граница позже
    периода (окно схлопывается, и в API идти нельзя — на перевёрнутом диапазоне
    Омнидеск отвечает 400);
  * отказ считать базу, когда пригодных дней меньше минимума;
  * график смен: заглянувший не считается сменой, настоящая двойная смена
    считается, тихий день помечается ненадёжным, ручное переопределение бьёт
    вывод из данных;
  * калибровочная грация: авто-детект по возрасту и переопределения в обе
    стороны;
  * контекст нагрузки в отчёте: «базы нет» обязано читаться иначе, чем «поток
    был как обычно» — иначе отчёт врёт в пользу спокойной картины;
  * аудит критичных: чат, упавший на оператора, и чат, взятый им из очереди
    руками, различаются только полем done_by — перепутать их значит отправить
    руководителю на ручной разбор случай, где разбирать нечего.

Запуск:  python scripts/selftest.py
Код возврата 0 — всё сошлось, 1 — есть падения (годится для CI).
"""
import os
import sys
import json
import tempfile
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import shifts
import calibration
import load_baseline as lb
import report
import audit_critical
import topics
import settings
import no_responsible
import sla_violations

MSK = lb.MSK
FAILED = []
PASSED = 0


def check(name, got, want):
    global PASSED
    if got == want:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}\n         получили: {got!r}\n         ожидали:  {want!r}")


def section(title):
    print(f"\n{title}")


# --------------------------------------------------------------------------
# Подставные клиенты. Отдают ровно ту форму данных, что настоящий OmniClient.
# --------------------------------------------------------------------------
def rfc(d):
    """Омнидеск отдаёт created_at в RFC 2822 — подделываем в том же формате."""
    return d.strftime("%a, %d %b %Y %H:%M:%S +0300")


class CaseStub:
    """Клиент с заданным списком обращений; фильтрует по окну, как настоящий."""

    def __init__(self, cases):
        self.cases = cases
        self.calls = 0

    def iter_cases(self, from_time, to_time, **kw):
        self.calls += 1
        s = dt.datetime.strptime(from_time, lb.FMT)
        e = dt.datetime.strptime(to_time, lb.FMT)
        for c in self.cases:
            created = dt.datetime.strptime(c["_created"], lb.FMT)
            if s <= created <= e:
                yield {k: v for k, v in c.items() if not k.startswith("_")}

    def staff_map(self):
        return {1: "Оператор А", 2: "Оператор Б", 99: "Бот"}


def steady_cases(start, days, per_day=4, staff_id=1):
    """Ровный поток: per_day обращений в сутки, все с ответом.

    Первое обращение суток кладём ровно в 00:00. Это не косметика: окно базы
    начинается с ПЕРВОГО реального обращения (`effective_start`), поэтому поток,
    стартующий в 10 утра, укоротил бы окно на 10 часов и все проверки «сколько
    дней в базе» поехали бы на 0.4 дня.
    """
    out = []
    step = 24 // per_day
    for d in range(days):
        for i in range(per_day):
            t = start + dt.timedelta(days=d, hours=i * step)
            out.append({"_created": t.strftime(lb.FMT), "created_at": rfc(t),
                        "case_id": len(out) + 1, "staff_id": staff_id,
                        "first_response_speed": "00:05:00"})
    return out


class GroupStub:
    def __init__(self, groups):
        self._groups = groups

    def get(self, path, params=None):
        return {str(i): {"group": g} for i, g in enumerate(self._groups)}


# --------------------------------------------------------------------------
section("Ключи корзин «день недели × час»")
# --------------------------------------------------------------------------
# Кортеж (weekday, hour) в JSON ключом не сериализуется, поэтому наружу корзины
# уходят строками. Если round-trip сломается, per_bucket начнёт молча отдавать
# нули, и «ожидали 0 обращений» будет выглядеть как настоящий вывод.
check("bucket_key(0, 14) -> строка", lb.bucket_key(0, 14), "Пн-14")
check("round-trip всех 168 корзин",
      all(lb.parse_bucket_key(lb.bucket_key(wd, h)) == (wd, h)
          for wd in range(7) for h in range(24)),
      True)
check("ключ сериализуется в JSON",
      json.loads(json.dumps({lb.bucket_key(3, 9): 1})), {"Чт-09": 1})

# --------------------------------------------------------------------------
section("Нижняя граница пригодных данных")
# --------------------------------------------------------------------------
END = dt.datetime(2026, 7, 29, tzinfo=MSK)
cases = steady_cases(dt.datetime(2026, 6, 25, tzinfo=MSK), 40)

# Проверяем МЕХАНИЗМ границы, а не дату из конкретной установки. DATA_START —
# настройка проекта: где-то None, где-то дата переезда. Без этой строчки тест
# «без границы» в такой установке молча стал бы тестом «с границей» и перестал
# бы проверять то, что заявлено. Границу дальше задаём явно, аргументом since.
lb.DATA_START = None

# Границы нет — берём всё окно.
b = lb.build_baseline(CaseStub(cases), END, weeks=4)
check("без границы: окно 28 дн", b["window"]["effective_days"], 28.0)
check("без границы: не обрезано", b["window"]["clipped_by_data_start"], False)
# Данные покрывают всё окно, упираться не во что.
check("без границы: окно не подрезано по первому обращению",
      b["window"]["truncated"], False)
check("без границы: база годная", b["window"]["insufficient"], False)
check("без границы: data_start отсутствует", b["window"]["data_start"], None)

# Граница внутри окна — окно обрезается, дней мало, база непригодна.
b = lb.build_baseline(CaseStub(cases), END, weeks=4,
                      since=dt.datetime(2026, 7, 25, tzinfo=MSK))
check("граница внутри окна: обрезано", b["window"]["clipped_by_data_start"], True)
check("граница внутри окна: осталось 4 дн", b["window"]["effective_days"], 4.0)
check("граница внутри окна: база непригодна (< 7 дн)",
      b["window"]["insufficient"], True)

# Граница даёт ровно минимум — база уже годится (проверяем саму границу отсечки).
b = lb.build_baseline(CaseStub(cases), END, weeks=4,
                      since=dt.datetime(2026, 7, 22, tzinfo=MSK))
check("ровно 7 дн: база годная", b["window"]["insufficient"], False)

# Граница позже конца окна — окно схлопывается. Главное: в API не ходим,
# иначе Омнидеск получит from > to и ответит 400.
stub = CaseStub(cases)
b = lb.build_baseline(stub, dt.datetime(2026, 7, 20, tzinfo=MSK), weeks=4,
                      since=dt.datetime(2026, 7, 25, tzinfo=MSK))
check("окно схлопнулось: флаг collapsed", b["window"]["collapsed"], True)
check("окно схлопнулось: база непригодна", b["window"]["insufficient"], True)
check("окно схлопнулось: обращений нет", b["cases_total"], 0)
check("окно схлопнулось: В API НЕ ХОДИЛИ", stub.calls, 0)
check("окно схлопнулось: корзины пустые", b["per_bucket"], {})
check("окно схлопнулось: JSON сериализуется",
      isinstance(json.dumps(b, ensure_ascii=False), str), True)

# --------------------------------------------------------------------------
section("График смен")
# --------------------------------------------------------------------------
def day_cases(day, counts):
    """counts = {staff_id: сколько обращений в этот день}."""
    out = []
    for sid, n in counts.items():
        for i in range(n):
            t = dt.datetime(2026, 7, day, 10, 0) + dt.timedelta(minutes=i)
            out.append({"_created": t.strftime(lb.FMT), "created_at": rfc(t),
                        "case_id": f"{day}-{sid}-{i}", "staff_id": sid,
                        "first_response_speed": "00:05:00"})
    return out


CFG = {"share_threshold": 0.30, "min_day_cases": 10,
       "exclude_staff": [], "overrides": {}}

# Типичный день: один держит смену, второй заглянул закрыть пару задач.
c = day_cases(6, {1: 100, 2: 8})
r = shifts.roster(CaseStub(c), "2026-07-06 00:00:00", "2026-07-06 23:59:59", cfg=dict(CFG))
d = r["days"]["2026-07-06"]
check("заглянувший НЕ считается сменой", d["on_shift"], [1])
check("заглянувший попал в visitors", [v["staff_id"] for v in d["visitors"]], [2])
check("делитель нагрузки = 1, а не 2", shifts.avg_online(r), 1.0)

# Настоящая двойная смена: механизм не должен её терять.
c = day_cases(7, {1: 60, 2: 55})
r = shifts.roster(CaseStub(c), "2026-07-07 00:00:00", "2026-07-07 23:59:59", cfg=dict(CFG))
check("реальная двойная смена засчитана",
      sorted(r["days"]["2026-07-07"]["on_shift"]), [1, 2])
check("делитель для двойной смены = 2", shifts.avg_online(r), 2.0)

# Тихий день: 3 из 5 — формально 60%, но доказывать этим нечего.
c = day_cases(8, {1: 3, 2: 2})
r = shifts.roster(CaseStub(c), "2026-07-08 00:00:00", "2026-07-08 23:59:59", cfg=dict(CFG))
check("тихий день помечен ненадёжным",
      r["days"]["2026-07-08"]["low_confidence"], True)
check("ненадёжный день не идёт в делитель", shifts.avg_online(r), None)

# Ровно на пороге: доля == порогу считается сменой (граница включительно).
c = day_cases(9, {1: 70, 2: 30})
r = shifts.roster(CaseStub(c), "2026-07-09 00:00:00", "2026-07-09 23:59:59", cfg=dict(CFG))
check("доля ровно 30% = на смене",
      sorted(r["days"]["2026-07-09"]["on_shift"]), [1, 2])

# Исключённые (боты, системные учётки) не могут «выйти на смену».
c = day_cases(10, {99: 90, 1: 20})
cfg_ex = dict(CFG, exclude_staff=[99])
r = shifts.roster(CaseStub(c), "2026-07-10 00:00:00", "2026-07-10 23:59:59", cfg=cfg_ex)
check("бот исключён из смен", r["days"]["2026-07-10"]["on_shift"], [1])

# Ручное переопределение бьёт вывод из данных (отпуск, подмена).
c = day_cases(11, {1: 100, 2: 8})
cfg_ov = dict(CFG, overrides={"2026-07-11": {
    "on_shift": [2], "note": "подмена", "decided": "2026-07-12", "by": "рук"}})
r = shifts.roster(CaseStub(c), "2026-07-11 00:00:00", "2026-07-11 23:59:59", cfg=cfg_ov)
d = r["days"]["2026-07-11"]
check("переопределение бьёт данные", d["on_shift"], [2])
check("переопределение помечено как ручное", d["source"], "override")
check("в примечании сохранены автор и дата решения",
      d["note"], "подмена (решение рук, 2026-07-12)")
check("вытесненный ушёл в visitors",
      [v["staff_id"] for v in d["visitors"]], [1])

# Отсутствие конфига — не ошибка: механизм работает на умолчаниях.
missing = os.path.join(tempfile.gettempdir(), "нет-такого-shifts.json")
cfg = shifts.load_config(missing)
check("без конфига берутся умолчания",
      (cfg["share_threshold"], cfg["overrides"]), (shifts.DEFAULT_SHARE, {}))

# --------------------------------------------------------------------------
section("Калибровочная грация")
# --------------------------------------------------------------------------
AT = dt.datetime(2026, 7, 18, 23, 59, 59, tzinfo=MSK)
gs = GroupStub([
    {"group_id": 1, "group_title": "Старое", "created_at": rfc(dt.datetime(2026, 1, 1))},
    {"group_id": 2, "group_title": "Молодое", "created_at": rfc(dt.datetime(2026, 7, 15))},
])
cfg = {"grace_weeks": 4, "auto_detect": True, "overrides": {}}
st = calibration.grace_status(gs, at=AT, cfg=cfg)
check("старая группа без грации", st["groups"][1]["in_grace"], False)
check("молодая группа в грации по авто-детекту", st["groups"][2]["in_grace"], True)
check("источник решения — авто", st["groups"][2]["source"], "auto")
check("выбраны только молодые", calibration.graced_group_ids(st), {2})

# Переопределение снимает грацию с формально молодой группы.
cfg_off = {"grace_weeks": 4, "auto_detect": True,
           "overrides": {"2": {"in_grace": False, "note": "переезд старого потока",
                               "decided": "2026-07-31", "by": "рук"}}}
st = calibration.grace_status(gs, at=AT, cfg=cfg_off)
check("переопределение снимает грацию", st["groups"][2]["in_grace"], False)
check("видно, что решение ручное", st["groups"][2]["source"], "override")
check("в примечании автор и дата",
      st["groups"][2]["note"], "переезд старого потока (решение рук, 2026-07-31)")

# И наоборот — выдаёт грацию старой группе.
cfg_on = {"grace_weeks": 4, "auto_detect": True,
          "overrides": {"1": {"in_grace": True, "note": "перезапуск направления"}}}
st = calibration.grace_status(gs, at=AT, cfg=cfg_on)
check("переопределение выдаёт грацию старой группе", st["groups"][1]["in_grace"], True)

# Возраст считается на КОНЕЦ периода: отчёт за июль, пересобранный в сентябре,
# должен видеть июльскую картину, иначе старый отчёт перестанет воспроизводиться.
late = calibration.grace_status(gs, at=dt.datetime(2026, 9, 30, tzinfo=MSK), cfg=cfg)
check("в сентябре июльская группа уже не в грации",
      late["groups"][2]["in_grace"], False)
check("а на конец июля — была",
      calibration.grace_status(gs, at=AT, cfg=cfg)["groups"][2]["in_grace"], True)

# Деление обращений по грации ничего не выкидывает.
st = calibration.grace_status(gs, at=AT, cfg=cfg)
normal, graced = calibration.split_cases(
    [{"case_id": 1, "group_id": 1}, {"case_id": 2, "group_id": 2},
     {"case_id": 3, "group_id": 2}], st)
check("обращения делятся, а не теряются",
      (len(normal), len(graced)), (1, 2))

# --------------------------------------------------------------------------
section("Контекст нагрузки в отчёте")
# --------------------------------------------------------------------------
# Смены здесь выводятся из тех же подставных данных, но конфиг лежит вне репозитория
# и в разных установках разный. Прибиваем его к стенду — иначе тест мерил бы не
# механизм, а чужие настройки.
shifts.load_config = lambda path=None: dict(CFG)

lb_cases = steady_cases(dt.datetime(2026, 6, 25, tzinfo=MSK), 40, per_day=12)

# Период целиком раньше пригодных данных: база не собирается.
stub = CaseStub(lb_cases)
ld = lb.context(stub, "2026-07-01 00:00:00", "2026-07-07 23:59:59",
                since=dt.datetime(2026, 7, 15, tzinfo=MSK))
check("нет базы: available=False", ld["available"], False)
check("нет базы: В API НЕ ХОДИЛИ", stub.calls, 0)
check("нет базы: чисел, которых нет, не выдумываем",
      [k for k in ("actual_cases", "ratio", "online_staff") if k in ld], [])

# Главное свойство всей ветки: «сравнивать не с чем» обязано звучать иначе, чем
# «поток был как обычно». Молчание или ноль здесь прочитались бы как второе —
# и отчёт, от которого зависят деньги, соврал бы в пользу спокойной картины.
line = "\n".join(report.load_lines(ld))
check("нет базы: причина названа вслух", "сравнивать не с чем" in line, True)
check("нет базы: не притворяемся, что поток обычный",
      ("x1" in line or "обращений/час" in line), False)

# База есть — считаем и печатаем цифры.
ld = lb.context(CaseStub(lb_cases), "2026-07-22 00:00:00", "2026-07-28 23:59:59")
check("база есть: available=True", ld["available"], True)
check("база есть: делитель выведен из смен", ld["online_staff"], 1.0)
# Ровный поток сам с собой сходится: 12/сут в базе → 12/сут в периоде.
check("ровный поток: ожидание = факту", ld["ratio"], 1.0)
check("ровный поток: всплесков нет", ld["spike_buckets"], {})
check("ключи всплесков читаются parse_bucket_key",
      all(lb.parse_bucket_key(k) for k in ld["spike_buckets"]), True)
line = "\n".join(report.load_lines(ld))
check("база есть: цифры в строке", "обращений/час" in line, True)

# В личном отчёте нагрузки нет вообще — load_lines обязан промолчать, а не упасть.
check("нагрузки нет (personal): строк нет", report.load_lines(None), [])

# Пометка всплеска — только контекст рядом со случаем, вердикт не трогает.
check("пометка всплеска", report.spike_note({"spike_ratio": 2.4}),
      " · час всплеска x2.4")
check("без всплеска — пусто", report.spike_note({"held_min": 40}), "")

# --------------------------------------------------------------------------
section("Аудит критичных: сам взял или чат упал")
# --------------------------------------------------------------------------
# Омнидеск пишет одно и то же событие `fixed_chat 0 -> оператор` и когда чат
# падает на человека сам, и когда человек берёт его из очереди руками. Разница
# видна только в done_by. Пока её не учитывали, осознанный клейм уезжал в
# «пограничные» — то есть на ручной разбор к руководителю вместо личных.
CRIT_CASE = {"case_number": "1-1", "case_id": 1, "staff": "Оператор А",
             "first_response_min": 29.0, "group_id": 1}


class AuditStub:
    """Отдаёт messages.json и changelog.json по одному обращению."""

    def __init__(self, reply_at, reply_staff, events):
        self.reply_at, self.reply_staff, self.events = reply_at, reply_staff, events

    def get(self, path, params=None):
        if path.endswith("messages.json"):
            return {"0": {"message": {"message_type": "reply_staff",
                                      "created_at": rfc(self.reply_at),
                                      "staff_id": self.reply_staff}}}
        return {"changelog": [{"event": ev, "created_at": rfc(ts), "old_value": old,
                               "value": new, "done_by": by}
                              for ts, ev, old, new, by in self.events]}


def audited(assigned_at, done_by, reply_at, old="0", new="1"):
    ev = [(assigned_at, "fixed_chat", old, new, done_by)]
    return audit_critical.audit_case(AuditStub(reply_at, 1, ev), CRIT_CASE)

D = dt.datetime(2026, 7, 21, tzinfo=MSK)
HELD_21 = (D.replace(hour=15, minute=50), D.replace(hour=16, minute=11))
HELD_70 = (D.replace(hour=15, minute=0), D.replace(hour=16, minute=10))

# Чат упал сам — оператор его не выбирал: пограничное, руководителю на оценку.
v = audited(HELD_21[0], "omnidesk", HELD_21[1])
check("упавший чат: личное", v["kind"], "personal")
check("упавший чат: помечен авто-чатом", v["auto_chat"], True)
check("упавший чат: пограничное", v["borderline"], True)
check("удержание в рабочих минутах", v["held_min"], 21.0)

# Назначило правило — оператор тоже ни при чём.
check("назначение правилом: пограничное",
      audited(HELD_21[0], "rule_10042", HELD_21[1])["borderline"], True)

# Взял из очереди сам — судить нечего, это чистое личное.
v = audited(HELD_21[0], "staff_1", HELD_21[1])
check("взял сам: личное", v["kind"], "personal")
check("взял сам: не авто-чат", v["auto_chat"], False)
check("взял сам: не пограничное", v["borderline"], False)

# Чужой клейм на то же обращение авто-чатом быть не перестаёт.
check("взял не отвечавший: всё ещё авто-чат",
      audited(HELD_21[0], "staff_2", HELD_21[1])["auto_chat"], True)

# Долгое удержание пограничным не становится независимо от того, кто назначил.
check("долго держал после авто-чата: не пограничное",
      audited(HELD_70[0], "omnidesk", HELD_70[1])["borderline"], False)

# Ответил в пределах SLA с момента владения — вина не его, и вопроса «сам взял
# или упало» не возникает вовсе.
v = audited(D.replace(hour=16, minute=5), "staff_1", D.replace(hour=16, minute=11))
check("ответил в SLA: унаследовано", v["kind"], "systemic_noresp")
check("ответил в SLA: авто-чат не размечаем", "auto_chat" in v, False)

# --------------------------------------------------------------------------
section("Темы обращений")
# --------------------------------------------------------------------------
# Главный риск разреза по темам — неполная разметка. Классификатор включают
# позже, чем начинают работать, и «возвратов 3%» одинаково выглядит и когда
# возвратов мало, и когда размечено мало. Поэтому проверяем не столько
# арифметику, сколько то, что охват нельзя не заметить.
FIELDS_RAW = {
    "4101": {"field_id": 4101, "title": "Тема обращения (продукт А)",
              "field_type": "select", "field_level": "case",
              "field_data": {"1": "Не работает", "2": "Возврат денег"}},
    # Поле уровня КЛИЕНТА с похожим названием — в разрез попадать не должно:
    # оно описывает человека, а не обращение.
    "4102": {"field_id": 4102, "title": "Тема обращения клиента",
              "field_type": "select", "field_level": "user",
              "field_data": {"1": "VIP"}},
    "4103": {"field_id": 4103, "title": "Баланс", "field_type": "textarea",
              "field_level": "user", "field_data": {}},
}


class FieldsStub:
    def custom_fields_map(self):
        return FIELDS_RAW


tf = topics.topic_fields(FieldsStub())
check("справочник: взяли только поле уровня обращения", sorted(tf), ["4101"])
check("тема расшифрована из ключа",
      topics.case_topic({"custom_fields": {"cf_4101": "2"}}, tf), "Возврат денег")
check("темы нет -> None", topics.case_topic({"custom_fields": {}}, tf), None)
check("поле клиента темой не считается",
      topics.case_topic({"custom_fields": {"cf_4102": "1"}}, tf), None)
# Вариант удалили из справочника после того, как его проставили: обращение
# терять нельзя, отдаём сырое значение.
check("неизвестный вариант не теряется",
      topics.case_topic({"custom_fields": {"cf_4101": "9"}}, tf), "9")

# 10 обращений: 6 размечены (2 темы), 4 без темы — и среди безтемных есть просрочка.
# Строка = (тема, просрочка ли, по вине ли). У «Не работает» 2 просрочки, но по
# вине только 1 — вторая унаследована из очереди.
rows = ([("Не работает", True, True)] + [("Не работает", True, False)]
        + [("Не работает", False, False)] * 2
        + [("Возврат денег", True, False)] + [("Возврат денег", False, False)]
        + [(None, True, True)] + [(None, False, False)] * 3)
s = topics.summary(rows)
check("охват считается от всех обращений", s["coverage"], 0.6)
check("низкий охват поднимает флаг", s["low_coverage"], True)
check("просрочки без темы не теряются", s["untagged_violations"], 1)
check("просрочки по вине без темы не теряются", s["untagged_personal"], 1)
# Доля внутри темы, а не доля темы в потоке: 2 просрочки из 4 обращений темы.
check("доля просрочек внутри темы", s["topics"][0]["violation_rate"], 0.5)
# Главное свойство разреза: унаследованная просрочка НЕ идёт в вину. Если бы
# считали одной колонкой, чужая очередь читалась бы как провал оператора.
check("унаследованное не попало в вину", s["topics"][0]["personal"], 1)
check("всего просрочек по теме считается отдельно",
      s["topics"][0]["violations"], 2)
check("доля по вине меньше доли всех просрочек",
      s["topics"][0]["personal_rate"], 0.25)
check("сортировка по просрочкам ПО ВИНЕ", [x["topic"] for x in s["topics"]],
      ["Не работает", "Возврат денег"])
check("безтемные в список тем не попали", len(s["topics"]), 2)

# Тема, где просрочки есть, но все унаследованные: по вине ноль, и это должно
# быть видно, а не выглядеть как отсутствие просрочек.
only_inherited = topics.summary([("Очередь", True, False)] * 3
                                + [("Очередь", False, False)] * 7)
check("тема без личной вины: по вине 0", only_inherited["topics"][0]["personal"], 0)
check("тема без личной вины: просрочки видны",
      only_inherited["topics"][0]["violations"], 3)
check("тема без личной вины: строка не пропала",
      "Очередь" in "\n".join(report.topic_lines(only_inherited)), True)

line = "\n".join(report.topic_lines(s))
check("охват назван в рендере", "размечено 6 из 10 обращений, 60%" in line, True)
check("при низком охвате — оговорка вслух", "Разметка неполная" in line, True)

# Полный охват: оговорки быть не должно, иначе она обесценится.
s_full = topics.summary([("Не работает", True, True), ("Не работает", False, False)])
check("полный охват: флага нет", s_full["low_coverage"], False)
check("полный охват: без оговорки",
      "Разметка неполная" in "\n".join(report.topic_lines(s_full)), False)

# Ничего не размечено — это НЕ «тем не было». Должно звучать по-другому.
s_none = topics.summary([(None, True, True)] * 5)
line = "\n".join(report.topic_lines(s_none))
check("нет разметки: сказано словами", "тем не проставил" in line, True)
check("нет разметки: не выдаём пустой топ", "```" in line, False)
# В личном отчёте разреза нет вообще — рендер обязан промолчать, а не упасть.
check("тем нет (personal): строк нет", report.topic_lines(None), [])

# Обрезка списка обязана быть видимой: молча показанные N строк читаются как
# «вот все темы», и хвост исчезает бесследно.
many = topics.summary([(f"Тема {i}", True, True) for i in range(12)])
line = "\n".join(report.topic_lines(many, limit=8))
check("обрезка названа вслух", "из 12 с просрочками" in line, True)
check("без обрезки — молчим", "Показаны" in "\n".join(report.topic_lines(s_full)), False)

# --------------------------------------------------------------------------
section("Список референсных значений")
# --------------------------------------------------------------------------
# settings.py показывает, на чём стоит расчёт. Ценность у него ровно одна —
# он не должен отставать от кода. Две проверки ниже и есть эта гарантия:
# первая ловит переименованную константу, вторая — добавленную и не внесённую
# в список. Без второй список тихо станет неполным, а неполный список врёт
# увереннее, чем его отсутствие.
cfg = settings.collect()
listed = {(i["source"], i["name"])
          for g in cfg["groups"] for i in g["items"]}
check("список собирается и значения читаются", len(listed) >= 15, True)

# Технические константы (таймзона, форматы, имена файлов, регулярки) настройками
# не являются — их в списке быть не должно, иначе он утонет в шуме.
NOT_SETTINGS = {"MSK", "FMT", "WD_NAMES", "CONFIG_NAME", "ASSIGN_EVENTS",
                "ACTIVE_STATUSES", "CLOSED_STATUSES", "TOPIC_RE", "REQUIRED_KEYS"}
missed = []
for mod in (sla_violations, audit_critical, lb, shifts, calibration, topics,
            no_responsible):
    for name in dir(mod):
        if not name.isupper() or name in NOT_SETTINGS:
            continue
        if (f"{mod.__name__}.py", name) not in listed:
            missed.append(f"{mod.__name__}.{name}")
check("новых порогов мимо списка нет", missed, [])

# Рабочее окно задано в ДВУХ модулях независимо. Если развести их, удержание
# в аудите и нагрузка на оператора начнут считаться по разным суткам, и обе
# цифры останутся правдоподобными — поэтому расхождение ловим тестом.
check("рабочее окно совпадает в аудите и нагрузке",
      (audit_critical.WORK_START_H, audit_critical.WORK_END_H),
      (lb.WORK_START, lb.WORK_END))

# --------------------------------------------------------------------------
print(f"\n{'=' * 60}")
if FAILED:
    print(f"ПРОВАЛЕНО {len(FAILED)} из {PASSED + len(FAILED)}:")
    for n in FAILED:
        print(f"  - {n}")
    sys.exit(1)
print(f"Все проверки пройдены: {PASSED}")
sys.exit(0)

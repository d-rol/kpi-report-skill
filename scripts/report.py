"""Этап 1, п.6: сборка KPI-отчёта по ОДНОМУ сотруднику за ПРОИЗВОЛЬНЫЙ период.

По запросу, не по расписанию (у сотрудников разный график выплат — см. память
проекта). Склеивает три хелпера: скорость (нативный лидерборд), нарушения SLA
с делением лёгкое/среднее/критичное (sla_violations), аудит критичных случаев
(audit_critical), командную очередь «без ответственного» (no_responsible).

Два уровня видимости — ФИКСИРОВАННОЕ требование из предложения руководителю,
не опция:
  --view personal  — что видит сотрудник: скорость первых ответов (медиана),
                     скорость всех ответов, реальный SLA (только личные
                     критичные, без унаследованных), «разобрал зависшее» (справочно).
  --view manager   — всё выше + сырой SLA Омнидеска для контраста, критичные
                     до/после аудита с обоснованием по каждому случаю, командная
                     метрика «без ответственного», аудиторский след.

Правило норматива («соблюдён/не соблюдён») СОЗНАТЕЛЬНО не считаем — только метрики
(решение команды; вернёмся, когда накопятся данные для калибровки).

Источники метрик:
  * скорость (медиана первого ответа, скорость всех ответов) и СЫРОЙ SLA — из
    stats_leaderboard.json с from_time/to_time (нативно, совпадает с интерфейсом;
    сверено вручную на нескольких периодах — цифры совпадают с лидербордом);
  * деление нарушений и АУДИРОВАННЫЙ SLA — из списка обращений + changelog
    (наша логика, точная атрибуция).

Запуск:
  python report.py --staff "Имя Сотрудника" --from "2026-07-12 00:00:00" --to "2026-07-18 23:59:59" --view personal
  python report.py --staff 10001 --from "..." --to "..." --view manager --cache
  python report.py --staff "Имя Сотрудника" --from "..." --to "..." --json
"""
import sys
import json
import argparse
import datetime as dt

from omni_client import OmniClient
from sla_violations import parse_speed, classify, SLA_MINUTES
from audit_critical import audit_case
from no_responsible import live_snapshot, period_breakdown, forgotten_in_work
from calibration import grace_status, load_config, MSK, FMT as CAL_FMT
# Импортируем модулем, а не именами: внутри gather() локальная переменная `lb` —
# это лидерборд, и короткий алиас молча затенил бы модуль.
import load_baseline
import topics as topics_mod
import backlog

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# Сколько тем показывать в разрезе просрочек. Длинный хвост тем с одной
# просрочкой не помогает решению, а вытесняет главное; скрытое число называется
# в рендере вслух, полный список остаётся в --json.
TOPIC_ROWS_LIMIT = 8


def resolve_staff(staff_map, who):
    """--staff может быть id или имя (регистронезависимо, по подстроке)."""
    who = str(who).strip()
    if who.isdigit() and int(who) in staff_map:
        return int(who), staff_map[int(who)]
    for sid, name in staff_map.items():
        if name.lower() == who.lower():
            return sid, name
    for sid, name in staff_map.items():
        if who.lower() in name.lower():
            return sid, name
    raise SystemExit(f"Не нашёл сотрудника «{who}». Известные: "
                     + ", ".join(f"{n} ({i})" for i, n in staff_map.items()))


def min_or_none(seconds):
    try:
        return round(float(seconds) / 60.0, 1)
    except (TypeError, ValueError):
        return None


def gather(client, staff_id, staff_name, from_time, to_time, want_team):
    # 1) Нативный лидерборд за произвольный период — скорость + сырой SLA.
    lb = client.get("stats_leaderboard.json",
                    {"from_time": from_time, "to_time": to_time})
    row = next((lb[k]["staff"] for k in lb if k.isdigit()
                and lb[k]["staff"]["staff_id"] == staff_id), {})

    # 2) Наш проход по обращениям сотрудника: деление нарушений SLA.
    # Справочник тем тянем один раз на весь прогон: он общий для всех обращений,
    # и запрашивать его в цикле значило бы сжечь rate limit на одну и ту же
    # страницу. Один запрос на отчёт.
    topic_fields = topics_mod.topic_fields(client) if want_team else {}
    # case_id -> тема, по ВСЕМ отвеченным обращениям. Сводку собираем не здесь,
    # а после аудита: «по вине или нет» для критичных известно только оттуда.
    topic_of = {}
    # Приходы по ВСЕЙ команде: ночную очередь разбирает смена целиком, по одному
    # сотруднику её не посчитать. Собираем в том же проходе и ДО фильтра по
    # сотруднику — отдельный проход стоил бы ещё столько же запросов к API.
    team_arrivals = []
    buckets = {"light": [], "medium": [], "critical": []}
    answered = 0
    for case in client.iter_cases(from_time=from_time, to_time=to_time,
                                  show_first_response_time=True):
        spd = parse_speed(case.get("first_response_speed"))
        if want_team:
            came = load_baseline.parse_created(case.get("created_at"))
            if came is not None:
                team_arrivals.append((came, spd))
        if case.get("staff_id") != staff_id:
            continue
        if spd is None:
            continue
        answered += 1
        cat, excess = classify(spd)
        # Тему собираем по ВСЕМ отвеченным обращениям, а не только по просроченным:
        # без знаменателя «сколько всего обращений этой темы» топ просрочек
        # покажет просто самые частые темы, а не самые тяжёлые.
        if topic_fields:
            topic_of[case.get("case_id")] = topics_mod.case_topic(case, topic_fields)
        if cat is None:
            continue
        buckets[cat].append({
            "case_number": case.get("case_number"),
            "case_id": case.get("case_id"),
            "staff": staff_name,
            "first_response_min": round(spd, 1),
            # Нужен для калибровочной грации: из какого направления обращение.
            "group_id": case.get("group_id"),
            # Нужен, чтобы отметить случаи, пришедшие в час всплеска нагрузки.
            "created_at": case.get("created_at"),
        })

    # 3) Аудит критичных этого сотрудника: личное vs унаследованное.
    verdicts = [audit_case(client, c) for c in buckets["critical"]]
    personal = [v for v in verdicts if v["kind"] == "personal"]
    inherited = [v for v in verdicts if v["kind"] != "personal"]

    # Аудит знает только staff_id, поэтому в вердикте остаётся «передано от
    # оператора 12345». Руководителю нужен человек, а не число — подставляем имя
    # здесь, где справочник уже есть. Сам staff_id сохраняем в neglected_by:
    # он машиночитаемый ключ, имя может измениться.
    names = client.staff_map()
    for v in verdicts:
        nb = v.get("neglected_by")
        if nb is None:
            continue
        # staff_id приходит то строкой, то числом — ищем по обоим видам ключа.
        keys = {nb, str(nb)}
        if str(nb).isdigit():
            keys.add(int(nb))
        nm = next((names[k] for k in keys if k in names), None)
        if nm:
            v["neglected_by_name"] = nm
            v["reason"] = f"передано от: {nm}"

    # 3.5) Контекст нагрузки: поток в этот период был обычный или нет.
    # Только для manager: нагрузка — метрика командная, а в личном отчёте она
    # читалась бы как заготовленное оправдание. Заодно не платим за неё те ~2
    # минуты API, когда сотрудник смотрит свой отчёт.
    load = None
    if want_team:
        load = load_baseline.context(client, from_time, to_time)
        # Помечаем случаи, пришедшие в час всплеска. Это НЕ смягчает вердикт —
        # решение по-прежнему за руководителем; просто рядом видно, что в этот
        # час поток был кратно выше обычного.
        spike_at = load.get("spike_buckets") or {}
        for v in verdicts:
            created = next((c.get("created_at") for c in buckets["critical"]
                            if c["case_id"] == v.get("case_id")), None)
            d = load_baseline.parse_created(created) if created else None
            if d is None:
                continue
            ratio = spike_at.get(load_baseline.bucket_key(d.weekday(), d.hour))
            if ratio:
                v["spike_ratio"] = ratio

        # Вид прихода: ночная очередь / её разбор / обычное время. Как и всплеск,
        # вердикт НЕ меняет — но без пометки «держал 40 минут» читается одинаково
        # и в спокойный день, и когда оператор в этот момент разгребал ночь.
        drain_ends = backlog.drain_end(team_arrivals)
        for v in verdicts:
            created = next((c.get("created_at") for c in buckets["critical"]
                            if c["case_id"] == v.get("case_id")), None)
            d = load_baseline.parse_created(created) if created else None
            if d is None:
                continue
            kind = backlog.arrival_kind(d, drain_ends)
            if kind != backlog.NORMAL:
                v["arrival"] = kind

    # Реальный % просрочек: сырой (всё >15 мин, сходится с Омнидеском) vs
    # аудированный (унаследованное из очереди убрано). Лёгкие (15–20 мин) не
    # аудируются поштучно — это мелкие личные превышения, идут как есть.
    light_n = len(buckets["light"])
    crit_total = len(buckets["critical"])
    crit_personal = len(personal)
    all_viol = light_n + crit_total
    personal_viol = light_n + crit_personal

    def pct(n):
        return round(100.0 * n / answered, 1) if answered else 0.0

    sla_percent = {
        "answered": answered,
        "all_violations": {"count": all_viol, "pct": pct(all_viol),
                           "light": light_n, "critical": crit_total},
        "personal_after_audit": {"count": personal_viol, "pct": pct(personal_viol),
                                 "light": light_n, "critical": crit_personal},
    }

    result = {
        "staff": staff_name,
        "staff_id": staff_id,
        "period": {"from": from_time, "to": to_time},
        "speed": {
            "first_response_median_min": min_or_none(row.get("first_response_time")),
            "all_responses_median_min": min_or_none(row.get("response_time")),
            "total_responses": row.get("total_number_of_responses"),
            "closed_cases": row.get("closed_cases"),
        },
        "sla_raw_omnidesk": {
            "first_response_sla_violated": row.get("first_response_sla_violated"),
            "response_sla_violated": row.get("response_sla_violated"),
        },
        "sla_audited": {
            "answered": answered,
            "light": len(buckets["light"]),
            "critical_total": len(buckets["critical"]),
            "critical_personal": len(personal),
            "critical_inherited": len(inherited),
        },
        "sla_percent": sla_percent,
        "resolved_stale": len(inherited),   # «разобрал зависшее» = критичные, что на деле были из очереди
        "personal_critical_cases": personal,
        "inherited_critical_cases": inherited,
        "case_url_tpl": f"https://{client.subdomain}.omnidesk.ru/staff/cases/record/{{case_number}}",
    }
    # 4) Калибровочная грация: были ли в периоде молодые направления и сколько
    # нарушений пришло из них. Возраст групп считаем на КОНЕЦ периода, иначе
    # старый отчёт, пересобранный позже, потеряет грацию и перестанет сходиться.
    result["calibration"] = calibration_block(
        client, from_time, to_time, buckets["light"] + buckets["critical"], personal)

    if want_team:
        result["load"] = load
        # Разрез по темам — менеджерский: он отвечает на вопрос «какие вопросы
        # команда не тянет», а это про обучение и базу знаний, не про сотрудника.
        # Лёгкие (15–20 мин) целиком идут в «по вине»: их не аудируют поштучно,
        # это мелкие личные превышения. Из критичных — только те, что аудит
        # признал личными; унаследованные из очереди в «по вине» не идут.
        blamed = ({c["case_id"] for c in buckets["light"]}
                  | {v["case_id"] for v in personal})
        all_viol = ({c["case_id"] for c in buckets["light"]}
                    | {c["case_id"] for c in buckets["critical"]})
        result["topics"] = topics_mod.summary(
            [(t, cid in all_viol, cid in blamed) for cid, t in topic_of.items()]
        ) if topic_fields else None
        result["backlog"] = backlog.summary(team_arrivals, SLA_MINUTES)
        result["team_no_responsible"] = {
            "live": live_snapshot(client),
            "period": period_breakdown(client, from_time, to_time),
        }
        result["forgotten_in_work"] = forgotten_in_work(client)
    return result


def calibration_block(client, from_time, to_time, all_violations, personal_critical):
    """Молодые направления периода + сколько нарушений пришло именно из них.

    Ничего не вычитает: грация — это пометка «здесь метрика ещё не показательна»,
    а не автоматическая амнистия. Решение остаётся за руководителем (правила
    pass/fail в проекте сознательно нет).
    """
    try:
        at = dt.datetime.strptime(to_time, CAL_FMT).replace(tzinfo=MSK)
    except (TypeError, ValueError):
        at = None
    status = grace_status(client, at=at, cfg=load_config())
    graced = {gid for gid, g in status["groups"].items() if g["in_grace"]}
    block = {
        "grace_weeks": status["grace_weeks"],
        "evaluated_at": status["at"],
        "groups_in_grace": [status["groups"][g] for g in sorted(graced)],
        "violations_from_grace": 0,
        "personal_critical_from_grace": 0,
        "cases": [],
    }
    if not graced:
        return block
    hit = [c for c in all_violations if c.get("group_id") in graced]
    titles = {gid: status["groups"][gid]["title"] for gid in graced}
    block["violations_from_grace"] = len(hit)
    block["personal_critical_from_grace"] = sum(
        1 for c in personal_critical if c.get("group_id") in graced)
    block["cases"] = [{
        "case_number": c["case_number"],
        "group": titles.get(c.get("group_id")),
        "first_response_min": c["first_response_min"],
    } for c in hit]
    return block


def fmt_min(v):
    return "—" if v is None else f"{v} мин"


def bar(pct, width=28, fill="█", empty="░"):
    """Пропорциональная полоса из Unicode-блоков — ровно выглядит в моноширинном чате."""
    pct = max(0.0, min(100.0, float(pct or 0)))
    n = int(round(pct / 100.0 * width))
    return fill * n + empty * (width - n)


def case_link(r, v):
    """Markdown-ссылка на обращение в Омнидеске (кликается прямо из чата)."""
    tpl = r.get("case_url_tpl")
    num = v["case_number"]
    return f"[#{num}]({tpl.format(case_number=num)})" if tpl else f"#{num}"


def spike_note(v):
    """Пометка «обращение пришло в час всплеска» — контекст, а не смягчение.

    Вердикт не меняем: решение за руководителем. Но без этой пометки строка
    «держал 40 минут» читается одинаково и в спокойный час, и в час с двойным
    потоком, а это разные ситуации.
    """
    ratio = v.get("spike_ratio")
    return f" · час всплеска x{ratio}" if ratio else ""


def render_personal(r):
    s = r["speed"]
    sp = r["sla_percent"]
    pers = sp["personal_after_audit"]
    inh = r["sla_audited"]["critical_inherited"]
    out = []
    out.append(f"## Личный отчёт — {r['staff']}")
    out.append(f"`{r['period']['from'][:10]} → {r['period']['to'][:10]}`\n")

    out.append("**Скорость** (медианы)")
    out.append("```")
    out.append(f"Первый ответ   {fmt_min(s['first_response_median_min']):>8}")
    out.append(f"Все ответы     {fmt_min(s['all_responses_median_min']):>8}")
    out.append("```")

    out.append("**Реальный SLA первого ответа**")
    out.append("```")
    out.append(f"по вашей вине  {bar(pers['pct'])}  {pers['pct']}%  ({pers['count']} из {sp['answered']})")
    out.append("```")
    out.append(f"из этих {pers['count']}: лёгких (15–20 мин) — **{pers['light']}**, "
               f"критичных (>20 мин) — **{pers['critical']}**")
    if inh:
        out.append(f"\n> Ещё **{inh}** критичных исключены аудитом — это время в общей "
                   f"очереди до вас, не ваша задержка.")

    out.append(f"\n**Разобрал зависшее: {r['resolved_stale']}**  \n"
               "_обращений из общей очереди, которые вы подхватили и закрыли — в плюс (справочно)._")
    return "\n".join(out)


def load_lines(load):
    """Контекст нагрузки — одна строка, не блок таблиц.

    Полная картина (профиль по часам, разбивка по дням) живёт в load_baseline.py;
    в отчёте по одному сотруднику ей не место — иначе персональный отчёт
    превращается в командную панель.
    """
    if not load:
        return []
    if not load.get("available"):
        # «Базы нет» и «поток как обычно» — разные утверждения. Молчание тут
        # прочиталось бы как второе, поэтому причину печатаем явно.
        return [f"\n**Нагрузка:** сравнивать не с чем — {load.get('reason')}. "
                "Проценты выше от этого не меняются, но контекста «поток был "
                "обычный или нет» у отчёта пока нет."]

    lpo = load.get("load_per_operator") or {}
    vs_week = (load.get("vs_last_week") or {}).get("ratio")
    parts = [f"\n**Нагрузка:** пришло {load['actual_cases']} обращений "
             f"против обычных {load['expected_cases']} (x{load['ratio']} к среднему"]
    if vs_week:
        parts.append(f", x{vs_week} к последней неделе базы")
    parts.append(f"). {lpo.get('cases_per_work_hour')} обращений/час на "
                 f"{load['online_staff']} оператора в окно {lpo.get('work_window')}.")
    line = "".join(parts)
    if load.get("clipped_by_data_start"):
        line += (f"  \n_База короткая — {load['baseline_days']} дн вместо "
                 f"{load['baseline_weeks']} нед: раньше Омнидеск не был основным "
                 "каналом. Сравнение ориентировочное._")
    return [line]


def arrival_note(v):
    """Пометка «пришло в ночную очередь / в её разбор» — контекст, не смягчение.

    Без неё просрочка, физически неизбежная (часы всей ночной пачки стартуют
    в 10:00 одновременно), выглядит так же, как просрочка в спокойный час.
    """
    kind = v.get("arrival")
    return f" · {backlog.KIND_LABELS[kind]}" if kind else ""


def backlog_lines(b):
    """Сколько просрочек порождено ночной очередью, а не работой в смене."""
    if not b or not b.get("violations_total"):
        return []
    k = b["kinds"]
    night, drain, normal = k["night"], k["drain"], k["normal"]
    share = round((b.get("queue_share") or 0) * 100)
    out = [f"\n**Ночная очередь:** из {b['violations_total']} просрочек команды "
           f"**{b['violations_from_queue']}** ({share}%) порождены ею, а не "
           f"работой в смене."]
    out.append("```")
    out.append(f"{'когда пришло':<24} {'обр':>5} {'просрочек':>10}  доля")
    for row in (night, drain, normal):
        rate = round((row["rate"] or 0) * 100)
        out.append(f"{row['label']:<24} {row['cases']:>5} {row['violations']:>10}  "
                   f"{bar(rate, width=12)} {rate}%")
    out.append("```")
    d = b.get("drain_minutes") or {}
    if d:
        out.append(
            f"_Ночью смены нет, и часы SLA по всей ночной пачке стартуют в "
            f"{backlog.WORK_START}:00 одновременно — уложиться в норматив может "
            f"только первая горстка. Пока её разбирают (медиана "
            f"{d.get('median')} мин, максимум {d.get('max')} мин), приходят новые "
            f"обращения: ответить на них раньше физически нельзя, и просрочек "
            f"среди них {round((drain['rate'] or 0) * 100)}% против "
            f"{round((normal['rate'] or 0) * 100)}% в обычное время. "
            f"Момент разбора считается по данным, а не задан константой._")
    return out


def topic_lines(t, limit=TOPIC_ROWS_LIMIT):
    """Разрез просрочек по темам обращений.

    Печатаем долю просрочек ВНУТРИ темы, а не долю темы в потоке: первая
    отвечает на вопрос «что даётся тяжелее», вторая — «чего просто больше».
    Охват называем всегда: при неполной разметке порядок тем может быть не
    окончательным, и руководитель должен видеть это рядом с числами, а не
    узнать потом.
    """
    if not t:
        return []
    if not t.get("cases_tagged"):
        return ["\n**Темы обращений:** классификатор за этот период тем не "
                "проставил — разреза нет. (Это не «тем не было»: тема ставится "
                "автоматикой, и на части периода её может не быть вовсе.)"]
    pct = round(t["coverage"] * 100)
    out = [f"\n**Просрочки по темам** (размечено {t['cases_tagged']} из "
           f"{t['cases_total']} обращений, {pct}%)"]
    if t.get("low_coverage"):
        out.append(f"_Разметка неполная. Доли внутри тем считаются от размеченных; "
                   f"порядок тем на таком охвате ещё может измениться. Просрочек "
                   f"без темы: {t['untagged_violations']}._")
    rows_all = [x for x in t["topics"] if x["violations"]]
    rows = rows_all[:limit]
    if not rows:
        out.append("Ни одной просрочки в размеченных обращениях.")
        return out
    width = max([len(x["topic"]) for x in rows] + [len("тема")])
    out.append("```")
    # Две колонки, а не одна: «всего» — сколько по теме ждали клиенты (это про
    # очередь), «по вине» — что относится к самому оператору. Полоса рисуется
    # по вине: смешать их значило бы выдать чужую очередь за его просрочки.
    out.append(f"{'тема':<{width}}  обр  всего  по вине")
    for x in rows:
        share = round((x["personal_rate"] or 0) * 100)
        out.append(f"{x['topic']:<{width}}  {x['cases']:>3}  {x['violations']:>5}  "
                   f"{x['personal']:>3}  {bar(share, width=12)} {share}%")
    out.append("```")
    out.append("_«всего» — все просрочки по теме, включая унаследованные из "
               "очереди; «по вине» — после аудита. Полоса — доля по вине "
               "внутри темы._")
    # Обрезали список — говорим об этом. Молча показанные 8 строк читаются как
    # «вот все темы с просрочками», и хвост исчезает бесследно.
    if len(rows_all) > len(rows):
        out.append(f"_Показаны {len(rows)} тем из {len(rows_all)} с просрочками "
                   "(полный список — флаг `--json`)._")
    return out


def render_manager(r):
    s, raw = r["speed"], r["sla_raw_omnidesk"]
    sla, sp = r["sla_audited"], r["sla_percent"]
    allv, pers = sp["all_violations"], sp["personal_after_audit"]
    raw_pct = raw.get("first_response_sla_violated") or "—"
    out = []
    out.append(f"## Отчёт руководителю — {r['staff']}")
    out.append(f"`{r['period']['from'][:10]} → {r['period']['to'][:10]}`\n")

    out.append("**Скорость** (нативные медианы Омнидеска)")
    out.append("```")
    out.append(f"Первый ответ   {fmt_min(s['first_response_median_min']):>8}")
    out.append(f"Все ответы     {fmt_min(s['all_responses_median_min']):>8}")
    out.append(f"Ответов {s['total_responses'] or 0} · закрыто {s['closed_cases'] or 0}")
    out.append("```")

    out.append("**SLA первого ответа: сырой → реальный**")
    out.append("```")
    out.append(f"Омнидеск (сырой)   {bar(_num(raw_pct))}  {raw_pct}")
    out.append(f"Реально по вине    {bar(pers['pct'])}  {pers['pct']}%")
    out.append("```")
    out.append(f"Всего просрочек >15 мин: **{allv['count']}** ({allv['pct']}%) — "
               f"лёгких {allv['light']}, критичных {allv['critical']}.  \n"
               f"По вине сотрудника после аудита: **{pers['count']}** "
               f"(лёгких {pers['light']}, критичных **{pers['critical']}**). "
               f"Критичные **{allv['critical']} → {pers['critical']}** личных "
               f"(унаследовано из очереди {sla['critical_inherited']}).")

    out.extend(load_lines(r.get("load")))
    out.extend(backlog_lines(r.get("backlog")))
    out.extend(topic_lines(r.get("topics")))

    personal = r["personal_critical_cases"]
    clear = sorted([v for v in personal if not v.get("borderline")],
                   key=lambda x: -x["first_response_min"])
    border = sorted([v for v in personal if v.get("borderline")],
                    key=lambda x: -x["first_response_min"])

    if clear and border:
        # Иначе руководитель читает «личных 4», считает строки в таблице ниже и
        # видит 2 — расхождение, которого нет: пограничные тоже личные, просто
        # вынесены отдельно, потому что вердикт по ним за ним, а не за нами.
        out.append(f"\nИз этих **{pers['critical']}**: однозначных {len(clear)}, "
                   f"пограничных {len(border)} — обе таблицы ниже.")

    if clear:
        out.append("\n### Личные критичные — смотреть сюда")
        out.append("| Обращение | Первый ответ | Держал | Что было |")
        out.append("|---|--:|--:|---|")
        for v in clear:
            held = "—" if v.get("held_min") is None else f"{v['held_min']} мин"
            out.append(f"| {case_link(r, v)} | {v['first_response_min']} мин | {held} "
                       f"| {v['reason']}{spike_note(v)}{arrival_note(v)} |")

    if border:
        out.append("\n### Требует вашей оценки — пограничные")
        out.append("_Оператор владел дольше SLA, но недолго (часто авто-упавший чат при "
                   "параллельной загрузке). Автоматика не судит — откройте по ссылке или "
                   "спросите сотрудника._")
        out.append("| Обращение | Первый ответ | Держал | Что было |")
        out.append("|---|--:|--:|---|")
        for v in border:
            held = "—" if v.get("held_min") is None else f"{v['held_min']} мин"
            note = (v["reason"] + (" · авто-чат" if v.get("auto_chat") else "")
                    + spike_note(v) + arrival_note(v))
            out.append(f"| {case_link(r, v)} | {v['first_response_min']} мин | {held} | {note} |")

    if r["inherited_critical_cases"]:
        inh = sorted(r["inherited_critical_cases"], key=lambda x: -x["first_response_min"])
        out.append(f"\n<details><summary>Унаследованные критичные — {len(inh)} шт "
                   f"(не в вину; «разобрал зависшее»)</summary>\n")
        out.append("_Омнидеск записал ожидание на того, кто ответил, хотя во время "
                   "ожидания обращение было ничьим. Аудит смотрит, когда оператор "
                   "стал владельцем: если он ответил в пределах SLA с этого момента "
                   "— задержка не его. «Держал 0.0 мин» = взял и ответил сразу._")
        # Откуда они взялись. Без этой строки десятки одинаковых «из общей очереди»
        # не отвечают на главный вопрос — почему обращение столько ждало.
        kinds = {}
        for v in inh:
            k = v.get("arrival") or backlog.NORMAL
            kinds[k] = kinds.get(k, 0) + 1
        parts = [f"{backlog.KIND_LABELS[k]} — {n}"
                 for k, n in sorted(kinds.items(), key=lambda x: -x[1])]
        out.append(f"\nОткуда пришли: {', '.join(parts)}.\n")
        out.append("| Обращение | Первый ответ | Держал | Источник |")
        out.append("|---|--:|--:|---|")
        for v in inh[:12]:
            held = "—" if v.get("held_min") is None else f"{v['held_min']} мин"
            out.append(f"| {case_link(r, v)} | {v['first_response_min']} мин | {held} "
                       f"| {v['reason']}{arrival_note(v)} |")
        if len(inh) > 12:
            out.append(f"\n…и ещё {len(inh) - 12} (полный список — флаг `--json`).")
        out.append("\n</details>")

    # Калибровочная грация: показываем только когда в периоде реально были
    # молодые направления — иначе это лишний шум в каждом отчёте.
    cal = r.get("calibration") or {}
    if cal.get("groups_in_grace"):
        names = ", ".join(f"**{g['title']}** ({g['age_days']:.0f} дн)"
                          for g in cal["groups_in_grace"])
        out.append(f"\n### Калибровочная грация — молодые направления")
        out.append(f"_Моложе {cal['grace_weeks']} нед на конец периода: {names}. "
                   "Метрики по ним ещё не показательны: нет шаблонов и базы знаний, "
                   "поток непредсказуем. Нарушения показаны, но не вычтены — "
                   "решение за вами._")
        out.append(f"- Нарушений SLA пришло из этих направлений: "
                   f"**{cal['violations_from_grace']}** "
                   f"(из них личных критичных: **{cal['personal_critical_from_grace']}**)")

    fw = r.get("forgotten_in_work")
    if fw:
        mine = fw.get("by_staff", {}).get(str(r["staff_id"]), [])
        hrs = fw.get("min_age_hours", 24)
        out.append(f"\n### Забытые в работе — без ответа дольше {int(hrs)} ч (сейчас)")
        if mine:
            out.append("_Обращение уже взято этим сотрудником (есть ответственный), но "
                       "первого ответа так и нет. В SLA пока не попадает — висит незамеченным._")
            out.append("| Обращение | Висит | Тема |")
            out.append("|---|--:|---|")
            for w in mine:
                subj = (w.get("subject") or "").replace("|", "\\|")
                out.append(f"| {case_link(r, w)} | {w['age_hours']} ч | {subj} |")
        else:
            out.append(f"- У сотрудника таких нет. По команде всего: **{fw.get('total', 0)}**.")

    team = r.get("team_no_responsible")
    if team:
        live, per = team["live"], team["period"]
        out.append("\n### Команда: «без ответственного»")
        out.append(f"- Живая очередь риска сейчас: **{live['waiting_count']}** "
                   f"(старейшему {live['oldest_hours']} ч)")
        out.append(f"- За период без ответственного: **{per['no_responsible_total']}** → "
                   f"риск потери ответа **{per['waiting_risk']}** "
                   f"(тихих закрытий {per['excluded_silent_close']}, был ответ {per['excluded_had_reply']})")
    return "\n".join(out)


def _num(s):
    """'20.4%' -> 20.4; безопасно к None/мусору."""
    try:
        return float(str(s).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def auto_html_name(staff, from_time, to_time, view):
    """Имя файла для --html без пути: кто, за какой период, какой вид.

    Пробелы и пунктуация в имени заменяются на подчёркивания (буквы, в том числе
    кириллические, остаются). В имя идут ОБЕ границы периода и вид отчёта —
    иначе отчёты за разные недели молча перезаписывали бы друг друга.
    """
    slug = "".join(ch if ch.isalnum() else "_" for ch in staff).strip("_").lower()
    return f"kpi_{slug or 'staff'}_{from_time[:10]}_{to_time[:10]}_{view}.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staff", required=True, help="имя или staff_id сотрудника")
    ap.add_argument("--from", dest="from_time", required=True, help="'YYYY-MM-DD HH:MM:SS' (МСК)")
    ap.add_argument("--to", dest="to_time", required=True)
    ap.add_argument("--view", choices=["personal", "manager"], default="manager")
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH", nargs="?", const="",
                    help="дополнительно записать отчёт HTML-страницей. Без "
                         "пути имя собирается само: reports/<сотрудник>_<период>.html")
    args = ap.parse_args()

    client = OmniClient(cache=args.cache)
    staff_map = client.staff_map()
    staff_id, staff_name = resolve_staff(staff_map, args.staff)

    r = gather(client, staff_id, staff_name, args.from_time, args.to_time,
               want_team=(args.view == "manager"))

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    if args.html is not None:
        import os
        from report_html import render_html
        # Пустая строка = флаг дали без пути. Собираем имя сами, чтобы отчёты не
        # перезаписывали друг друга: в имени и сотрудник, и обе границы периода.
        if not args.html:
            args.html = os.path.join("reports", auto_html_name(
                staff_name, args.from_time, args.to_time, args.view))
        parent = os.path.dirname(os.path.abspath(args.html))
        os.makedirs(parent, exist_ok=True)
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(r, args.view))
        # В stderr, а не в stdout: HTML — это ДОПОЛНИТЕЛЬНЫЙ вывод, обычный
        # рендер ниже никуда не девается, и служебная строка не должна попадать
        # в него, если stdout перенаправляют в файл.
        print(f"HTML-отчёт записан: {args.html}", file=sys.stderr)
    print(render_manager(r) if args.view == "manager" else render_personal(r))


if __name__ == "__main__":
    main()

"""HTML-рендер KPI-отчёта поверх результата report.gather().

Зачем отдельный файл: текстовый рендер в чат остаётся в report.py, а здесь —
самодостаточная HTML-страница (инлайн-CSS, без внешних зависимостей и JS),
которую руководитель открывает в браузере, печатает или пересылает.

Тема адаптивная через CSS-переменные + prefers-color-scheme (светлая/тёмная —
подхватывается от системы/окружения, отдельного тумблера не нужно).

Два вида, как и в тексте:
  personal — компактный, для сотрудника: скорость, реальный личный SLA,
             «разобрал зависшее». Без менеджерского слоя.
  manager  — подробный: скорость, сырой vs реальный % просрочек с наглядной
             полосой, личные критичные, отдельный список «на ручную оценку»
             (пограничные, со ссылками на обращения), унаследованные свёрнуто,
             командная очередь.
"""
import html


def _esc(v):
    return html.escape(str(v), quote=True)


def _pct_of(n, total):
    return (100.0 * n / total) if total else 0.0


CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9;
  --card: #ffffff;
  --text: #1c2024;
  --muted: #6b7280;
  --border: #e5e7eb;
  --accent: #3b6ef5;
  --good: #12a150;
  --warn: #d99100;
  --bad: #e5484d;
  --neutral: #9aa2ad;
  --good-bg: #e8f6ee;
  --warn-bg: #fbf3e0;
  --bad-bg: #fdecec;
  --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1216;
    --card: #171b21;
    --text: #e6e9ee;
    --muted: #9aa2ad;
    --border: #262c34;
    --accent: #5b86f7;
    --good: #2fbd6a;
    --warn: #e0a83a;
    --bad: #f0666b;
    --neutral: #7c8593;
    --good-bg: #12291d;
    --warn-bg: #2a2313;
    --bad-bg: #2c1618;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 16px;
  background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 820px; margin: 0 auto; }
.head { margin-bottom: 20px; }
.head h1 { margin: 0 0 4px; font-size: 22px; font-weight: 650; letter-spacing: -.01em; }
.head .period { color: var(--muted); font-size: 13.5px; }
.head .badge {
  display: inline-block; margin-left: 8px; padding: 2px 9px; border-radius: 999px;
  font-size: 11.5px; font-weight: 600; letter-spacing: .02em; text-transform: uppercase;
  background: var(--good-bg); color: var(--good);
}
.head .badge.manager { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  padding: 18px 20px; margin-bottom: 16px; box-shadow: var(--shadow);
}
.card > h2 {
  margin: 0 0 14px; font-size: 12.5px; font-weight: 600; letter-spacing: .04em;
  text-transform: uppercase; color: var(--muted);
}
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; }
.stat { }
.stat .v { font-size: 26px; font-weight: 680; letter-spacing: -.02em; }
.stat .v small { font-size: 15px; font-weight: 550; color: var(--muted); margin-left: 2px; }
.stat .k { color: var(--muted); font-size: 13px; margin-top: 2px; }
.big { font-size: 34px; }
.hl-good { color: var(--good); } .hl-warn { color: var(--warn); }
.hl-bad { color: var(--bad); } .hl-accent { color: var(--accent); }

.bar { display: flex; height: 26px; border-radius: 8px; overflow: hidden; margin: 6px 0 12px; background: var(--border); }
.bar > span { display: block; height: 100%; }
.seg-good { background: var(--good); }
.seg-warn { background: var(--warn); }
.seg-bad  { background: var(--bad); }
.seg-neutral { background: var(--neutral); opacity: .55; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 13px; color: var(--muted); }
.legend .dot { display: inline-block; width: 9px; height: 9px; border-radius: 3px; margin-right: 6px; vertical-align: middle; }

.contrast { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
.contrast .from { color: var(--muted); text-decoration: line-through; font-size: 18px; }
.contrast .arrow { color: var(--muted); }
.contrast .to { font-size: 30px; font-weight: 680; color: var(--good); letter-spacing: -.02em; }
.contrast .note { color: var(--muted); font-size: 13px; }

table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
a.case { color: var(--accent); text-decoration: none; font-weight: 600; }
a.case:hover { text-decoration: underline; }
.pill { display: inline-block; padding: 1px 7px; border-radius: 6px; font-size: 11.5px; font-weight: 600; }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.bad { background: var(--bad-bg); color: var(--bad); }

.hint { color: var(--muted); font-size: 13px; margin: 2px 0 0; }
details { margin-top: 4px; }
details > summary {
  cursor: pointer; color: var(--muted); font-size: 13.5px; font-weight: 600;
  list-style: none; padding: 4px 0;
}
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "▸ "; }
details[open] > summary::before { content: "▾ "; }
.foot { color: var(--muted); font-size: 12px; text-align: center; margin-top: 22px; }
"""


def _doc(title, body):
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        "<meta name=viewport content=\"width=device-width, initial-scale=1\">"
        f"<title>{_esc(title)}</title><style>{CSS}</style></head>"
        f"<body><div class=wrap>{body}</div></body></html>"
    )


def _fmt_min(v):
    return "—" if v is None else f"{v}<small>мин</small>"


def _head(r, view):
    label = "Отчёт руководителю" if view == "manager" else "Личный отчёт"
    badge = ("<span class='badge manager'>руководитель</span>" if view == "manager"
             else "<span class=badge>личный</span>")
    p = r["period"]
    return (f"<div class=head><h1>{_esc(r['staff'])}{badge}</h1>"
            f"<div class=period>KPI поддержки · {_esc(p['from'])} — {_esc(p['to'])}</div></div>")


def _speed_card(r, extra=False):
    s = r["speed"]
    cells = [
        f"<div class=stat><div class=v>{_fmt_min(s['first_response_median_min'])}</div>"
        f"<div class=k>Первый ответ (медиана)</div></div>",
        f"<div class=stat><div class=v>{_fmt_min(s['all_responses_median_min'])}</div>"
        f"<div class=k>Все ответы (медиана)</div></div>",
    ]
    if extra:
        cells.append(f"<div class=stat><div class=v>{_esc(s.get('total_responses') or '—')}</div>"
                     f"<div class=k>Всего ответов</div></div>")
        cells.append(f"<div class=stat><div class=v>{_esc(s.get('closed_cases') or '—')}</div>"
                     f"<div class=k>Закрыто обращений</div></div>")
    return f"<div class=card><h2>Скорость</h2><div class=stats>{''.join(cells)}</div></div>"


def _sla_bar(r):
    """Одна полоса на всех отвеченных: в SLA / лёгкие / личные критичные / унаследованные."""
    sp = r["sla_percent"]
    answered = sp["answered"] or 1
    light = sp["all_violations"]["light"]
    crit_pers = sp["personal_after_audit"]["critical"]
    crit_inh = r["sla_audited"]["critical_inherited"]
    within = max(0, answered - light - crit_pers - crit_inh)
    segs = [
        ("seg-good", within), ("seg-warn", light),
        ("seg-bad", crit_pers), ("seg-neutral", crit_inh),
    ]
    bar = "".join(f"<span class='{c}' style='width:{_pct_of(n, answered):.2f}%'></span>"
                  for c, n in segs if n > 0)
    legend = (
        f"<span><span class='dot' style='background:var(--good)'></span>в SLA {within}</span>"
        f"<span><span class='dot' style='background:var(--warn)'></span>лёгкие 15–20 мин {light}</span>"
        f"<span><span class='dot' style='background:var(--bad)'></span>личные критичные {crit_pers}</span>"
        f"<span><span class='dot' style='background:var(--neutral)'></span>унаследованные {crit_inh}</span>"
    )
    return f"<div class=bar>{bar}</div><div class=legend>{legend}</div>"


def _case_row(r, v, with_link=True):
    tpl = r.get("case_url_tpl")
    num = _esc(v["case_number"])
    cell = (f"<a class=case href='{_esc(tpl.format(case_number=v['case_number']))}' target=_blank rel=noopener>#{num}</a>"
            if (with_link and tpl) else f"#{num}")
    held = "—" if v.get("held_min") is None else f"{v['held_min']} мин"
    return (f"<tr><td>{cell}</td><td class=num>{_esc(v['first_response_min'])} мин</td>"
            f"<td class=num>{_esc(held)}</td><td>{_esc(v['reason'])}</td></tr>")


def render_html(r, view="manager"):
    if view == "personal":
        return _doc(f"KPI — {r['staff']}", _render_personal(r))
    return _doc(f"KPI (руководитель) — {r['staff']}", _render_manager(r))


def _render_personal(r):
    sp = r["sla_percent"]
    pers = sp["personal_after_audit"]
    parts = [_head(r, "personal"), _speed_card(r, extra=False)]

    # Реальный SLA — только личное, без унаследованного времени очереди.
    inh = r["sla_audited"]["critical_inherited"]
    inh_note = (f"<p class=hint>Ещё {inh} критичных исключены — это время в общей "
                f"очереди до вас, не ваша задержка.</p>" if inh else "")
    parts.append(
        "<div class=card><h2>Реальный SLA первого ответа</h2>"
        f"<div class=stat><div class='v big hl-good'>{pers['pct']}%</div>"
        f"<div class=k>просрочек по вашей вине · {pers['count']} из {sp['answered']} ответов</div></div>"
        f"<p class=hint>Из них лёгкие (15–20 мин): {pers['light']} · "
        f"критичные (&gt;20 мин): {pers['critical']}</p>"
        f"{inh_note}</div>"
    )

    # «Разобрал зависшее» — в плюс.
    parts.append(
        "<div class=card><h2>Разобрал зависшее</h2>"
        f"<div class=stat><div class='v hl-accent'>{r['resolved_stale']}</div>"
        f"<div class=k>обращений из общей очереди, которые вы подхватили и закрыли</div></div>"
        "<p class=hint>Засчитывается в плюс — справочно.</p></div>"
    )
    parts.append("<div class=foot>Показатели скорости — медианы (устойчивы к выбросам).</div>")
    return "".join(parts)


def _render_manager(r):
    sp = r["sla_percent"]
    allv, pers = sp["all_violations"], sp["personal_after_audit"]
    raw = r["sla_raw_omnidesk"].get("first_response_sla_violated") or "—"
    parts = [_head(r, "manager"), _speed_card(r, extra=True)]

    # SLA: сырой vs реальный.
    parts.append(
        "<div class=card><h2>SLA первого ответа: сырой vs реальный</h2>"
        "<div class=contrast>"
        f"<span class=from>{_esc(raw)}</span><span class=arrow>→</span>"
        f"<span class=to>{pers['pct']}%</span>"
        f"<span class=note>реальных личных просрочек ({pers['count']} из {sp['answered']})</span>"
        "</div>"
        f"{_sla_bar(r)}"
        f"<p class=hint>Всего просрочек &gt;15 мин: {allv['count']} ({allv['pct']}%) — "
        f"лёгких {allv['light']}, критичных {allv['critical']}. "
        f"После аудита критичные {allv['critical']} → личных {pers['critical']} "
        f"(унаследовано из очереди: {r['sla_audited']['critical_inherited']}).</p></div>"
    )

    # Личные критичные — чисто личные (не пограничные).
    personal = r["personal_critical_cases"]
    clear = sorted([v for v in personal if not v.get("borderline")],
                   key=lambda x: -x["first_response_min"])
    border = sorted([v for v in personal if v.get("borderline")],
                    key=lambda x: -x["first_response_min"])

    if clear:
        rows = "".join(_case_row(r, v) for v in clear)
        # Иначе «личных критичных 4» в полосе против 2 строк в таблице читается как ошибка.
        split = (f"<p class=hint>Показаны {len(clear)} из {len(personal)} личных критичных; "
                 f"остальные {len(border)} — ниже, в «Требует вашей оценки».</p>"
                 if border else "")
        parts.append(
            "<div class=card><h2>Личные критичные — смотреть сюда</h2>"
            f"{split}"
            "<table><thead><tr><th>Обращение</th><th class=num>Первый ответ</th>"
            "<th class=num>Держал</th><th>Что было</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    # Пограничные — на ручную оценку руководителя, со ссылками.
    if border:
        rows = "".join(_case_row(r, v) for v in border)
        parts.append(
            "<div class=card><h2>Требует вашей оценки "
            "<span class='pill warn'>пограничные</span></h2>"
            "<p class=hint>Оператор владел обращением дольше SLA, но недолго — часто "
            "авто-упавший чат в момент параллельной загрузки. Автоматика не судит: "
            "откройте по ссылке или спросите сотрудника.</p>"
            "<table><thead><tr><th>Обращение</th><th class=num>Первый ответ</th>"
            "<th class=num>Держал</th><th>Что было</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )

    # Унаследованные — свёрнуто.
    inh = sorted(r["inherited_critical_cases"], key=lambda x: -x["first_response_min"])
    if inh:
        rows = "".join(_case_row(r, v, with_link=False) for v in inh[:12])
        more = (f"<p class=hint>…и ещё {len(inh) - 12} (полный список — флаг --json).</p>"
                if len(inh) > 12 else "")
        parts.append(
            "<div class=card><details><summary>Унаследованные критичные — "
            f"{len(inh)} шт (не в вину; «разобрал зависшее»)</summary>"
            "<table><thead><tr><th>Обращение</th><th class=num>Первый ответ</th>"
            "<th class=num>Держал</th><th>Источник</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>{more}</details></div>"
        )

    # Калибровочная грация — только если в периоде реально были молодые
    # направления, иначе лишний блок в каждом отчёте.
    cal = r.get("calibration") or {}
    if cal.get("groups_in_grace"):
        chips = "".join(
            f"<li><b>{_esc(g['title'])}</b> — {g['age_days']:.0f} дн "
            f"(создана {_esc(g['created_at'])})</li>"
            for g in cal["groups_in_grace"])
        rows = "".join(
            f"<tr><td>#{_esc(c['case_number'])}</td><td>{_esc(c['group'])}</td>"
            f"<td class=num>{_esc(c['first_response_min'])} мин</td></tr>"
            for c in sorted(cal.get("cases", []),
                            key=lambda x: -x["first_response_min"])[:15])
        more = (f"<p class=hint>…и ещё {len(cal['cases']) - 15} "
                "(полный список — флаг --json).</p>"
                if len(cal.get("cases", [])) > 15 else "")
        parts.append(
            "<div class=card><h2>Калибровочная грация "
            f"<span class='pill warn'>&lt; {_esc(cal['grace_weeks'])} нед</span></h2>"
            "<p class=hint>Направления моложе окна грации на конец периода. Метрики по ним "
            "ещё не показательны: нет шаблонов и базы знаний, поток непредсказуем. "
            "Нарушения показаны, но <b>не вычтены</b> — решение за руководителем.</p>"
            f"<ul>{chips}</ul>"
            f"<p>Нарушений SLA из этих направлений: <b>{_esc(cal['violations_from_grace'])}</b>, "
            f"из них личных критичных: <b>{_esc(cal['personal_critical_from_grace'])}</b>.</p>"
            + (("<table><thead><tr><th>Обращение</th><th>Направление</th>"
                "<th class=num>Первый ответ</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>" + more) if rows else "")
            + "</div>"
        )

    # Забытые в работе — взяты, но первого ответа так и нет дольше N часов.
    fw = r.get("forgotten_in_work")
    if fw:
        mine = fw.get("by_staff", {}).get(str(r["staff_id"]), [])
        hrs = int(fw.get("min_age_hours", 24))
        title = (f"<h2>Забытые в работе <span class='pill warn'>&gt; {hrs} ч</span></h2>")
        if mine:
            tpl = r.get("case_url_tpl")
            rows = []
            for w in sorted(mine, key=lambda x: -x["age_hours"]):
                num = _esc(w["case_number"])
                cell = (f"<a class=case href='{_esc(tpl.format(case_number=w['case_number']))}' "
                        f"target=_blank rel=noopener>#{num}</a>") if tpl else f"#{num}"
                rows.append(f"<tr><td>{cell}</td><td class=num>{_esc(w['age_hours'])} ч</td>"
                            f"<td>{_esc(w.get('subject') or '')}</td></tr>")
            parts.append(
                f"<div class=card>{title}"
                "<p class=hint>Обращение уже взято этим сотрудником (есть ответственный), "
                "но первого ответа так и нет. В SLA пока не попадает — висит незамеченным.</p>"
                "<table><thead><tr><th>Обращение</th><th class=num>Висит</th>"
                "<th>Тема</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div>"
            )
        else:
            parts.append(
                f"<div class=card>{title}<p class=hint>У сотрудника таких нет. "
                f"По команде всего: <b>{_esc(fw.get('total', 0))}</b>.</p></div>"
            )

    # Команда: без ответственного.
    team = r.get("team_no_responsible")
    if team:
        live, per = team["live"], team["period"]
        parts.append(
            "<div class=card><h2>Команда: «без ответственного»</h2>"
            "<div class=stats>"
            f"<div class=stat><div class=v>{_esc(live['waiting_count'])}</div>"
            f"<div class=k>в очереди риска сейчас (старейшему {_esc(live['oldest_hours'])} ч)</div></div>"
            f"<div class=stat><div class=v>{_esc(per['no_responsible_total'])}</div>"
            f"<div class=k>без ответственного за период</div></div>"
            f"<div class=stat><div class='v hl-{'bad' if per['waiting_risk'] else 'good'}'>{_esc(per['waiting_risk'])}</div>"
            f"<div class=k>риск потери ответа</div></div>"
            "</div>"
            f"<p class=hint>Исключено тихих закрытий: {_esc(per['excluded_silent_close'])}, "
            f"был ответ: {_esc(per['excluded_had_reply'])}.</p></div>"
        )

    parts.append("<div class=foot>Скорость — нативные медианы Омнидеска. "
                 "Аудированный SLA — по истории обращений (changelog).</div>")
    return "".join(parts)

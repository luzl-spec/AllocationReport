#!/usr/bin/env python3
"""LATAM Allocation report helper (sin llamadas de red: Claude hace las consultas
y las publicaciones con los conectores; este script hace todo lo demas).

  python3 build.py plan      -> fechas del sprint + consultas JQL numeradas
  python3 build.py sprint    -> lee work/counts.json, valida, escribe work/sprint.html
  python3 build.py aging     -> lee work/aging_raw.json, valida, escribe work/aging.html
  python3 build.py slack --aging ok|failed|na   -> imprime el mensaje de Slack

Variables de entorno opcionales (pruebas):
  TODAY=YYYY-MM-DD   simula la fecha de hoy
  FORCE_AGING=1      trata hoy como viernes
"""
import datetime as dt
import json
import os
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
SITE = CFG["site"]
PREFIX = "712020:"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
IPSET = '"Design","Dev In Progress","Dev Verification","Ready for testing","Test in progress"'
AGING_SINCE = "2026-01-01"
N_DEVS = sum(len(g["devs"]) for g in CFG["groups"])


# ---------- utilidades ----------
def fmt(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def enc(s):
    return quote(s, safe="")


def issues_url(jql):
    return f"https://{SITE}/issues/?jql={enc(jql)}"


def devs():
    """Itera (indice '01'.., grupo, nombre, accountId sin prefijo) en el orden fijo."""
    n = 0
    for g in CFG["groups"]:
        for name, acc in g["devs"]:
            n += 1
            yield f"{n:02d}", g, name, acc


def load(name):
    with open(os.path.join(WORK, name), encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    os.makedirs(WORK, exist_ok=True)
    with open(os.path.join(WORK, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def write(name, text):
    os.makedirs(WORK, exist_ok=True)
    with open(os.path.join(WORK, name), "w", encoding="utf-8") as f:
        f.write(text)


def link(url, text):
    return f'<a href="{url}">{text}</a>'


def dev_jqls(g, acc, S, E1):
    projs = g["projects"]
    proj = f"project = {projs[0]}" if len(projs) == 1 else f"project in ({', '.join(projs)})"
    base = f'{proj} AND issuetype in (Story, Bug) AND assignee = "{PREFIX}{acc}"'
    notdone = 'statusCategory != Done AND status != "Ready to Deploy"'
    return {
        "CL": f'{base} AND (statusCategory = Done OR status = "Ready to Deploy") AND updated >= "{S}" AND updated < "{E1}"',
        "CO": f'{base} AND {notdone} AND created < "{S}"',
        "IP": f'{base} AND {notdone} AND created >= "{S}" AND created < "{E1}"',
    }


def aging_jqls():
    ids = ",".join(f'"{PREFIX}{acc}"' for _, _, _, acc in devs())
    base = (f'project = CLCT AND issuetype in (Story,Bug) AND created >= "{AGING_SINCE}" '
            f'AND assignee in ({ids}) AND status in ({IPSET})')
    ch = f"status CHANGED TO ({IPSET})"
    return {
        "B1_0-1wk": f'{base} AND {ch} AND NOT {ch} BEFORE "-1w"',
        "B2_1-4wk": f'{base} AND {ch} BEFORE "-1w" AND NOT {ch} BEFORE "-4w"',
        "B3_over4wk": f'{base} AND {ch} BEFORE "-4w"',
        "UN_unaged": f"{base} AND NOT {ch}",
        "COUNT_all": base,
    }


# ---------- plan ----------
def cmd_plan():
    today = dt.date.fromisoformat(os.environ["TODAY"]) if os.environ.get("TODAY") else dt.date.today()
    anchor = dt.date.fromisoformat(CFG["anchor_start"])
    assert today >= anchor, "hoy es anterior al ancla del sprint"
    k = (today - anchor).days // CFG["sprint_days"]
    S = anchor + dt.timedelta(days=CFG["sprint_days"] * k)
    END = S + dt.timedelta(days=CFG["sprint_days"] - 1)
    E1 = S + dt.timedelta(days=CFG["sprint_days"])
    friday = today.isoweekday() == 5 or bool(os.environ.get("FORCE_AGING"))
    plan = {"today": today.isoformat(), "today_fmt": fmt(today), "S": S.isoformat(), "END": END.isoformat(),
            "E1": E1.isoformat(), "S_fmt": fmt(S), "END_fmt": fmt(END), "friday": friday}
    save("plan.json", plan)

    print(f"TODAY {plan['today']} | SPRINT {plan['S_fmt']} -> {plan['END_fmt']} | FRIDAY(aging) = {friday}")
    print(f"\n== PASO 1: {N_DEVS * 3} CONSULTAS DE CONTEO (searchResultMode=\"count\", usa issues.totalCount) ==")
    print("Formato de linea: <idx> <CL|CO|IP> <JQL>   (idx = desarrollador 01..%d)" % N_DEVS)
    for idx, g, _, acc in devs():
        for kind, jql in dev_jqls(g, acc, plan["S"], plan["E1"]).items():
            print(f"{idx} {kind} {jql}")
    print('\nGuarda los resultados en work/counts.json asi: {"01":[CL,CO,IP],"02":[CL,CO,IP],...} (32 claves)')
    if friday:
        print("\n== AGING (solo viernes): 5 consultas ==")
        print('Las 4 primeras con searchJiraIssuesUsingJql, fields ["assignee"], maxResults 100, SIN count, paginando si hasNextPage. '
              'La ultima (COUNT_all) con searchResultMode="count".')
        for name, jql in aging_jqls().items():
            print(f"{name}: {jql}")
        print('\nGuarda work/aging_raw.json asi: {"b1":{"<accountId>":[numeros de CLCT-N]},"b2":{...},"b3":{...},"un":{...},"count":<totalCount>}')
        print("(b1=0-1 week, b2=1-4 weeks, b3=over 4 weeks, un=sin edad. Solo el numero de cada clave CLCT-N.)")


# ---------- sprint ----------
def cmd_sprint():
    plan, counts = load("plan.json"), load("counts.json")
    S, E1 = plan["S"], plan["E1"]
    rows, names = [], []
    tot = {"CL": 0, "CO": 0, "IP": 0}
    by_mgr = {g["slack_label"]: 0 for g in CFG["groups"]}
    cur = None
    for idx, g, name, acc in devs():
        assert idx in counts, f"falta el desarrollador {idx} ({name}) en counts.json"
        cl, co, ip = counts[idx]
        assert all(isinstance(x, int) and x >= 0 for x in (cl, co, ip)), f"conteos invalidos para {name}: {counts[idx]}"
        if g is not cur:
            cur = g
            rows.append(f'<tr><td colspan="6"><strong>{g["header"]}</strong></td></tr>')
        jq = dev_jqls(g, acc, S, E1)
        total = cl + co + ip

        def cell(n, kind):
            return "—" if n == 0 else link(issues_url(jq[kind]), n)

        rows.append(f'<tr><td>{name}</td><td>{g["manager"]}</td><td>{cell(ip, "IP")}</td>'
                    f'<td>{cell(co, "CO")}</td><td>{cell(cl, "CL")}</td><td>{total}</td></tr>')
        names.append(name)
        tot["CL"] += cl
        tot["CO"] += co
        tot["IP"] += ip
        by_mgr[g["slack_label"]] += total
    assert len(names) == N_DEVS and len(set(names)) == N_DEVS, "filas/nombres duplicados"
    grand = tot["CL"] + tot["CO"] + tot["IP"]
    assert grand == sum(by_mgr.values()), "Total del parrafo != suma de la columna Total"

    head = (f'<p><strong>Sprint start:</strong> {plan["S_fmt"]} · <strong>Sprint end:</strong> {plan["END_fmt"]}</p>')
    desc = ('<p><strong>Closed in sprint</strong> = statusCategory = Done OR status = "Ready to Deploy", updated within the sprint dates. '
            f'<strong>Carried-over</strong> = not done (statusCategory != Done, status != "Ready to Deploy") and created before the sprint start: {tot["CO"]}. '
            f'<strong>In progress</strong> = not done and created within the sprint: {tot["IP"]}. '
            f'<strong>Total</strong> = closed ({tot["CL"]}) + carried-over ({tot["CO"]}) + in progress ({tot["IP"]}) = {grand} issues. '
            'DING Team counts include projects CLCT and DS.</p>')
    table = ('<table><thead><tr><th>Developer</th><th>Manager</th><th>🟡 In progress (created in sprint)</th>'
             '<th>🔵 Carried-over (created before sprint)</th><th>✅ Closed in sprint</th><th>Total</th></tr></thead><tbody>'
             + "".join(rows) + "</tbody></table>")
    write("sprint.html", head + desc + table)
    save("summary.json", {"S_fmt": plan["S_fmt"], "END_fmt": plan["END_fmt"], "total": grand, "rows": len(names),
                          "ip": tot["IP"], "co": tot["CO"], "cl": tot["CL"], "by_mgr": by_mgr})
    print(f'OK sprint.html | filas {len(names)}/{N_DEVS} | total {grand} | IP {tot["IP"]} CO {tot["CO"]} CL {tot["CL"]}')
    print(f'PUBLICAR: pageId={CFG["sprint_page"]["id"]} title="{CFG["sprint_page"]["title"]}" contentFormat=html '
          f'body=contenido literal de work/sprint.html (sin modificar ni abreviar). No cambies parentId.')


# ---------- aging ----------
def cmd_aging():
    plan, raw = load("plan.json"), load("aging_raw.json")
    norm = lambda a: a[len(PREFIX):] if a.startswith(PREFIX) else a
    per = {acc: {"b1": [], "b2": [], "b3": [], "un": []} for _, _, _, acc in devs()}
    seen, ignored = {}, 0
    for b in ("b1", "b2", "b3", "un"):
        for acc, nums in raw.get(b, {}).items():
            a = norm(acc)
            for n in nums:
                n = int(n)
                assert n not in seen, f"CLCT-{n} aparece en dos buckets ({seen[n]} y {b})"
                seen[n] = b
                if a in per:
                    per[a][b].append(n)
                else:
                    ignored += 1
    for v in per.values():
        for b in v:
            v[b].sort(reverse=True)
    E = sum(len(x) for v in per.values() for x in v.values())
    diff = abs(E - int(raw["count"]))
    assert diff <= 2, f"E={E} difiere de totalCount={raw['count']} en {diff} (>2): reintenta las consultas"
    A = sum(len(v["b1"]) for v in per.values())
    B = sum(len(v["b2"]) for v in per.values())
    C = sum(len(v["b3"]) for v in per.values())
    D = sum(len(v["un"]) for v in per.values())
    assert A + B + C + D == E

    def keys_link(nums):
        ks = [f"CLCT-{n}" for n in nums]
        return ks, issues_url(f"key in ({','.join(ks)})")

    def bcell(nums):
        if not nums:
            return "<td><p>—</p></td>"
        _, url = keys_link(nums)
        return f"<td><p>{link(url, len(nums))}</p></td>"

    rows, cur, sum_total = [], None, 0
    for idx, g, name, acc in devs():
        if g is not cur:
            cur = g
            label = "— DING Team —" if g["header"] == "DING Team" else f'👤 {g["header"]}'
            rows.append(f'<tr><td colspan="5"><p><strong>{label}</strong></p></td></tr>')
        v = per[acc]
        t = len(v["b1"]) + len(v["b2"]) + len(v["b3"]) + len(v["un"])
        sum_total += t
        if t:
            tj = (f'project = CLCT AND issuetype in (Story,Bug) AND assignee = "{PREFIX}{acc}" '
                  f'AND created >= "{AGING_SINCE}" AND status in ({IPSET})')
            tcell = f"<td><p>{link(issues_url(tj), t)}</p></td>"
        else:
            tcell = "<td><p>—</p></td>"
        rows.append(f"<tr><td><p>{name}</p></td>{bcell(v['b1'])}{bcell(v['b2'])}{bcell(v['b3'])}{tcell}</tr>")
    assert sum_total == E, "E != suma de la columna Total in progress"
    assert len(rows) - len(CFG["groups"]) == N_DEVS

    un_all = sorted((n for v in per.values() for n in v["un"]), reverse=True)
    note = ""
    if un_all:
        ks, url = keys_link(un_all)
        note = (f' Note: {len(un_all)} in-progress issues have no recorded status transition (age not available) '
                f'and are counted only in the total: {link(url, ", ".join(ks))}.')
    para = ('<p>Open Stories &amp; Bugs in progress (Design, Dev In Progress, Dev Verification, Ready for testing, Test in progress), '
            f'project CLCT, created since {AGING_SINCE}. Age = time since the <strong>first</strong> entry into any of these in-progress '
            "statuses, taken from each issue's status history. "
            f'Bucket links list the exact issues (snapshot of {plan["today_fmt"]}). '
            f'Parent page: {link(CFG["sprint_url"], CFG["sprint_page"]["title"])}. '
            f'Totals: 0–1 week {A} · 1–4 weeks {B} · over 4 weeks {C} · total {E}.{note}</p>')
    table = ('<table><thead><tr><th><p>Developer</p></th><th><p>0–1 week</p></th><th><p>1–4 weeks</p></th>'
             '<th><p>Over 4 weeks</p></th><th><p>Total in progress</p></th></tr></thead><tbody>'
             + "".join(rows) + "</tbody></table>")
    write("aging.html", para + table)
    save("aging_summary.json", {"A": A, "B": B, "C": C, "D": D, "E": E, "count_diff": diff, "ignored": ignored})
    print(f"OK aging.html | filas {N_DEVS} | 0-1w {A} · 1-4w {B} · >4w {C} · sin edad {D} · E {E} | diff vs count: {diff}")
    print(f'PUBLICAR: pageId={CFG["aging_page"]["id"]} title="{CFG["aging_page"]["title"]}" contentFormat=html '
          f'body=contenido literal de work/aging.html (sin modificar ni abreviar).')


# ---------- slack ----------
def cmd_slack():
    mode = "na"
    if "--aging" in sys.argv:
        mode = sys.argv[sys.argv.index("--aging") + 1]
    plan, s = load("plan.json"), load("summary.json")
    friday = plan["friday"]
    l1 = f'• Sprint: {s["S_fmt"]} → {s["END_fmt"]} · Total issues: {s["total"]} · Rows: {s["rows"]}/{N_DEVS}'
    if friday:
        l1 += " · Aging: updated" if mode == "ok" else " · Aging: failed"
    lines = ["✅ *LATAM Allocation updated*", l1,
             f'• Sprint totals: In progress {s["ip"]} · Carried-over {s["co"]} · Closed {s["cl"]}',
             "• By manager (Total): " + " · ".join(f"{k} {v}" for k, v in s["by_mgr"].items())]
    links = CFG["sprint_url"]
    if friday:
        if mode == "ok":
            a = load("aging_summary.json")
            txt = f'• Aging (in progress): 0–1 wk {a["A"]} · 1–4 wk {a["B"]} · >4 wk {a["C"]}'
            if a["D"]:
                txt += f' · no age {a["D"]}'
            if a["count_diff"]:
                txt += f' (Jira count check differed by {a["count_diff"]})'
            lines.append(txt)
        links += " · " + CFG["aging_url"]
    lines.append("• Links: " + links)
    print("\n".join(lines))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"plan": cmd_plan, "sprint": cmd_sprint, "aging": cmd_aging, "slack": cmd_slack}.get(
        cmd, lambda: sys.exit(__doc__))()

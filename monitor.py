#!/usr/bin/env python3
"""Monitoraggio idrometri del Serchio (CFR Toscana) con notifiche ntfy.

Come funziona
  Lo script gira ogni pochi minuti (vedi serchio.yml). La pagina del CFR crea
  la tabella con JavaScript, quindi viene aperta con un browser vero
  (Playwright) e si legge la riga di ogni stazione. Ogni nuova lettura (il CFR
  ne pubblica una ogni 15 minuti circa) viene salvata in data/history.json.
  Un controllo senza dati nuovi non salva e non notifica niente.

Quando arrivano le notifiche di andamento
  Solo quando il livello è "in zona": arriva a 0,5 m sotto la prima soglia (a
  Vecchiano 7,00 m, con la prima soglia a 7,50 m) oppure al valore
  "notifica_da" indicato in stations.json. Sotto quel livello i dati vengono
  registrati ma non arriva niente, anche se il livello sale o scende.
  In zona (e nella lettura in cui ne esce) valgono queste regole, per ogni
  idrometro:
    - livello in salita              -> una notifica a ogni nuova lettura
    - prima lettura in discesa       -> una notifica
    - discesa che continua           -> nessuna notifica
    - torna a salire dopo un calo    -> si ricomincia a notificare
    - salita che si ferma            -> una notifica ("livello stabile")
    - supera la prima o la seconda soglia -> notifica urgente
  Sopra la prima soglia le notifiche hanno priorità alta. Sopra la seconda,
  salita e livello stabile hanno priorità massima.

Notifiche che arrivano sempre, anche fuori zona
    - al primo avvio: "monitoraggio attivo", con icona e priorità secondo il
      livello del momento
    - dati non leggibili, idrometro mancante o dati fermi da più di un'ora:
      il silenzio non deve mai voler dire "tutto bene" per errore
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
URL = "https://www.cfr.toscana.it/monitoraggio/stazioni.php?type=idro"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

EPS = 0.02          # metri: variazioni più piccole contano come "stabile"
MAX_HISTORY = 600   # letture conservate (circa 6 giorni)
STALE_MIN = 60      # minuti senza nuovi dati prima di avvisare
CET = timezone(timedelta(hours=1))  # il CFR pubblica in ora solare

PAGE_STAMP_RE = re.compile(r"riferiti al (\d{2})/(\d{2})/(\d{4}) (\d{2})[.:](\d{2})")
ROW_STAMP_RE = re.compile(r"(\d{2})/(\d{2})\s+(\d{2})[.:](\d{2})")
NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


# ---------- utilità ----------

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def fm(x):
    return f"{x:.2f}".replace(".", ",")


def fd(x):
    return f"{x:+.2f}".replace(".", ",")


def send_ntfy(title, message, priority=3, tags=None):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC mancante, notifica non inviata:", title, "|", message)
        return
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    click = os.environ.get("DASHBOARD_URL", "").strip()
    if click:
        payload["click"] = click
    try:
        requests.post("https://ntfy.sh/", json=payload, timeout=20).raise_for_status()
    except Exception as exc:  # una notifica fallita non deve fermare il resto
        print("Invio notifica fallito:", exc)


# ---------- lettura dal CFR ----------

def cells_from_text(body, code):
    """Se la pagina non usa una vera tabella: spezzo il testo dopo il codice."""
    i = body.find(code)
    if i < 0:
        return None
    nxt = re.search(r"TOS\d{8}", body[i + len(code):])
    seg = body[i: i + len(code) + (nxt.start() if nxt else 600)]
    return [t for t in re.split(r"[\t\n]+", seg) if t.strip()]


def fetch_rows(stations):
    """Apre la pagina con un browser e ritorna (testo, html, {id: celle})."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(user_agent=UA, locale="it-IT").new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_function(
                "c => document.body.innerText.includes(c)",
                arg=stations[0]["id"], timeout=30000,
            )
        except Exception:
            pass
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        html = page.content()
        rows = {}
        for s in stations:
            loc = page.locator("tr", has_text=s["id"])
            cells = loc.last.locator("td,th").all_inner_texts() if loc.count() else None
            if not cells:
                cells = cells_from_text(body, s["id"])
            if cells:
                rows[s["id"]] = cells
        browser.close()
    return body, html, rows


def page_stamp(body):
    m = PAGE_STAMP_RE.search(body)
    if not m:
        return None
    d, mo, y, h, mi = map(int, m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=CET)


def parse_station(cells, ref_stamp):
    """Riga: codice, fiume, stazione, prov, zona, soglia2, soglia1, livello,
    portata, variazioni 15'/30'/1h, data e ora dell'ultimo dato."""
    cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
    cells = [c for c in cells if c]
    zone = None
    for i in range(1, len(cells)):
        if re.fullmatch(r"[A-Z]{2}", cells[i - 1]) and re.fullmatch(r"[A-Z]\d?", cells[i]):
            zone = i
            break
    if zone is None:
        return None
    tail = re.sub(r"[↑↓→↗↘]", " ", " ".join(cells[zone + 1:]))

    stamp = None
    m = ROW_STAMP_RE.search(tail)
    if m:
        d, mo, h, mi = map(int, m.groups())
        year = (ref_stamp or datetime.now(CET)).year
        stamp = datetime(year, mo, d, h, mi, tzinfo=CET)
        if ref_stamp and stamp > ref_stamp + timedelta(days=180):
            stamp = stamp.replace(year=year - 1)
        tail = tail.replace(m.group(0), " ")

    vals = []
    for tok in tail.split():
        if NUM_RE.fullmatch(tok):
            vals.append(float(tok.replace(",", ".")))
        elif tok == "-":
            vals.append(None)
    if len(vals) != 7 or vals[2] is None:
        return None
    s2, s1, level, flow, d15, d30, d60 = vals
    return {
        "level": level, "flow": flow, "soglie": sorted(x for x in (s1, s2) if x is not None),
        "var": {"15m": d15, "30m": d30, "1h": d60}, "stamp": stamp,
        "raw": " | ".join(cells),
    }


# ---------- logica ----------

def soglia_text(level, soglie):
    if len(soglie) != 2:
        return ""
    s1, s2 = soglie
    if level >= s2:
        return f"sopra la seconda soglia ({fm(s2)})"
    if level >= s1:
        return f"sopra la prima soglia ({fm(s1)}), alla seconda mancano {fm(s2 - level)} m"
    return f"alla prima soglia ({fm(s1)}) mancano {fm(s1 - level)} m"


def severity(level, soglie):
    """0 sotto la prima soglia, 1 tra le due, 2 sopra la seconda."""
    if len(soglie) != 2:
        return 0
    return 2 if level >= soglie[1] else 1 if level >= soglie[0] else 0


def register_failure(state, message):
    state["fail"] = state.get("fail", 0) + 1
    if state["fail"] == 1 or state["fail"] % 4 == 0:
        send_ntfy(
            "⚠️ Serchio: controllo non riuscito",
            message + " Controlla a mano sul sito del CFR.",
            priority=4,
            tags=["warning"],
        )


def process(body, html, rows, now):
    stations = load_json(ROOT / "stations.json")["stations"]
    state = load_json(DATA / "state.json", {})
    history = load_json(DATA / "history.json", [])
    latest = load_json(DATA / "latest.json", {})
    ref = page_stamp(body)

    parsed, bad = {}, []
    for s in stations:
        r = parse_station(rows.get(s["id"], []), ref) if s["id"] in rows else None
        if r:
            parsed[s["id"]] = r
        else:
            bad.append(s["name"])

    # nessuna stazione letta: avviso e file per capire cosa è cambiato
    if not parsed:
        DATA.mkdir(exist_ok=True)
        (DATA / "debug.txt").write_text(body[:30000], encoding="utf-8")
        (DATA / "debug.html").write_text(html[:200000], encoding="utf-8")
        register_failure(state, "Non trovo i livelli nella pagina del CFR.")
        latest.update({"ok": False, "checked": now.isoformat(), "error": "lettura non riuscita"})
        save_json(DATA / "latest.json", latest)
        save_json(DATA / "state.json", state)
        return

    state["fail"] = 0
    stamps = [r["stamp"] for r in parsed.values() if r["stamp"]] or ([ref] if ref else [])
    if not stamps:
        register_failure(state, "Non trovo l'orario dei dati.")
        save_json(DATA / "state.json", state)
        return
    stamp = max(stamps)
    stamp_iso = stamp.isoformat()

    key = ",".join(bad)
    if key and key != state.get("missing_notified"):
        send_ntfy(
            "⚠️ Serchio: idrometro non letto",
            "Non riesco a leggere: " + ", ".join(bad) + ". Controlla a mano sul sito del CFR.",
            priority=4,
            tags=["warning"],
        )
    state["missing_notified"] = key

    # nessun dato nuovo dall'ultima volta
    if stamp_iso == state.get("last_stamp"):
        age = (now - stamp).total_seconds() / 60
        if age > STALE_MIN and not state.get("stale_alerted"):
            send_ntfy(
                "⚠️ Serchio: dati fermi",
                f"Il CFR non pubblica nuovi dati dalle {stamp:%H:%M}. Controlla a mano.",
                priority=4,
                tags=["warning"],
            )
            state["stale_alerted"] = True
        latest.update({"ok": True, "checked": now.isoformat()})
        save_json(DATA / "latest.json", latest)
        save_json(DATA / "state.json", state)
        return
    state["stale_alerted"] = False

    first_run = "last_stamp" not in state
    levels = state.setdefault("levels", {})
    trends = state.setdefault("trends", {})

    events = []      # (tipo, testo)
    cards = []
    for s in stations:
        r = parsed.get(s["id"])
        if not r:
            continue
        level = r["level"]
        sog = r["soglie"]
        prev = levels.get(s["id"])
        prev_trend = trends.get(s["id"], "steady")
        delta = None
        trend = prev_trend
        extra = soglia_text(level, sog)
        extra = f" – {extra}" if extra else ""
        da = s.get("notifica_da")
        if da is None and len(sog) == 2:
            da = round(sog[0] - 0.5, 2)
        # in zona: livello arrivato a "da", o appena uscito da quella zona
        in_zone = da is None or level >= da or (prev is not None and prev >= da)
        ev = []

        if prev is not None:
            delta = round(level - prev, 3)
            for i, name in enumerate(("prima", "seconda")):
                if len(sog) == 2 and prev < sog[i] <= level:
                    ev.append(("soglia", f"🚨 {s['name']}: superata la {name} soglia ({fm(sog[i])} m), ora {fm(level)} m"))
            if delta > EPS:
                trend = "rising"
                ev.append(("up", f"▲ {s['name']}: {fm(level)} m ({fd(delta)}){extra}"))
            elif delta < -EPS:
                trend = "falling"
                if prev_trend != "falling":
                    ev.append(("down", f"▼ {s['name']}: {fm(level)} m ({fd(delta)}), primo calo{extra}"))
            else:
                if prev_trend == "rising":
                    trend = "steady"
                    ev.append(("flat", f"■ {s['name']}: {fm(level)} m, livello stabile{extra}"))

        if in_zone:
            events.extend(ev)

        levels[s["id"]] = level
        trends[s["id"]] = trend
        cards.append({
            "id": s["id"], "name": s["name"], "level": level, "delta": delta, "trend": trend,
            "flow": r["flow"], "soglie": sog, "var": r["var"], "notifica_da": da,
            "stamp": (r["stamp"] or stamp).isoformat(), "raw": r["raw"],
        })

    sev = max((severity(c["level"], c["soglie"]) for c in cards), default=0)
    icon = {0: "✅", 1: "⚠️", 2: "🚨"}[sev]

    if first_run:
        lines = []
        for c in cards:
            t = soglia_text(c["level"], c["soglie"])
            lines.append(f"{c['name']}: {fm(c['level'])} m" + (f" ({t})" if t else ""))
            if c["notifica_da"] is not None:
                lines.append(f"Avvisi di andamento da {fm(c['notifica_da'])} m")
        send_ntfy(
            f"{icon} Serchio: monitoraggio attivo",
            f"Dati delle {stamp:%H:%M}\n" + "\n".join(lines),
            priority={0: 3, 1: 4, 2: 5}[sev],
            tags=[{0: "white_check_mark", 1: "warning", 2: "rotating_light"}[sev]],
        )
    elif events:
        kinds = {k for k, _ in events}
        if "soglia" in kinds:
            title, prio, tags = "🚨 Serchio: soglia superata", 5, ["rotating_light"]
        elif "up" in kinds:
            title, prio, tags = ("🚨 Serchio: livello in salita", 5, ["rotating_light"]) if sev == 2 \
                else ("🔺 Serchio: livello in salita", 4, ["warning"])
        elif "down" in kinds:
            title, prio, tags = ("🔻 Serchio: in calo, ancora sopra soglia", 4, ["warning"]) if sev >= 1 \
                else ("🔻 Serchio: livello in calo", 3, ["arrow_down"])
        else:
            title, prio, tags = ("🚨 Serchio: livello stabile, sopra soglia", 5, ["rotating_light"]) if sev == 2 \
                else ("Serchio: livello stabile", 4 if sev == 1 else 3, ["information_source"])
        send_ntfy(
            title,
            "\n".join(t for _, t in events) + f"\nDati delle {stamp:%H:%M}",
            priority=prio,
            tags=tags,
        )

    state["last_stamp"] = stamp_iso
    history.append({"t": stamp_iso, "v": {c["id"]: c["level"] for c in cards},
                    "f": {c["id"]: c["flow"] for c in cards}})
    history = history[-MAX_HISTORY:]

    save_json(DATA / "history.json", history)
    save_json(DATA / "latest.json", {
        "ok": True,
        "updated": stamp_iso,
        "checked": now.isoformat(),
        "stations": cards,
        "missing": bad,
    })
    save_json(DATA / "state.json", state)


def main():
    now = datetime.now(timezone.utc)
    stations = load_json(ROOT / "stations.json")["stations"]
    try:
        body, html, rows = fetch_rows(stations)
    except Exception as exc:
        traceback.print_exc()
        state = load_json(DATA / "state.json", {})
        register_failure(state, f"Il sito del CFR non risponde ({type(exc).__name__}).")
        save_json(DATA / "state.json", state)
        latest = load_json(DATA / "latest.json", {})
        latest.update({"ok": False, "checked": now.isoformat(), "error": "sito non raggiungibile"})
        save_json(DATA / "latest.json", latest)
        return
    try:
        process(body, html, rows, now)
    except Exception:
        traceback.print_exc()
        send_ntfy("⚠️ Serchio: errore nello script",
                  "Lo script di monitoraggio ha avuto un errore. Controlla a mano sul sito del CFR.",
                  priority=4, tags=["warning"])
        sys.exit(1)


if __name__ == "__main__":
    main()
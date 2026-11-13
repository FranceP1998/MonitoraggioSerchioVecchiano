#!/usr/bin/env python3
"""Monitoraggio idrometri del Serchio (CFR Toscana) con notifiche ntfy.

Regole di notifica (per ogni idrometro, ad ogni nuova lettura del CFR):
  - livello in salita            -> notifica a ogni lettura (circa ogni 15 minuti)
  - prima lettura in discesa     -> una notifica
  - discesa che continua         -> nessuna notifica
  - livello che torna a salire   -> si ricomincia a notificare
  - salita che si ferma          -> una notifica ("livello stabile")
Se i dati non si leggono o restano fermi, arriva un avviso: il silenzio non
deve mai voler dire "tutto bene" per errore.
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
URL = "https://www.cfr.toscana.it/monitoraggio/stazioni.php?type=idro"

EPS = 0.02          # metri: variazioni più piccole contano come "stabile"
MAX_HISTORY = 600   # letture conservate (circa 6 giorni)
STALE_MIN = 60      # minuti senza nuovi dati prima di avvisare
CET = timezone(timedelta(hours=1))  # il CFR pubblica in ora solare

STAMP_RE = re.compile(r"riferiti al (\d{2})/(\d{2})/(\d{4}) (\d{2})[.:](\d{2})")
NUM_RE = re.compile(r"^-?\d+(?:[.,]\d+)?(?:\s*(?:m|mc/s|m3/s))?$", re.I)


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

def fetch_html():
    r = requests.get(
        URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (compatible; serchio-monitor)"},
    )
    r.raise_for_status()
    return r.text


def to_float(text):
    m = re.search(r"-?\d+(?:[.,]\d+)?", text)
    return float(m.group(0).replace(",", ".")) if m else None


def parse(html, stations):
    """Ritorna (orario dei dati, {id: {level, numbers, raw}})."""
    soup = BeautifulSoup(html, "html.parser")
    m = STAMP_RE.search(soup.get_text(" ", strip=True))
    stamp = None
    if m:
        d, mo, y, h, mi = map(int, m.groups())
        stamp = datetime(y, mo, d, h, mi, tzinfo=CET)

    readings = {}
    for s in stations:
        link = soup.find("a", href=re.compile(re.escape(s["id"])))
        row = link.find_parent("tr") if link else None
        if not row:
            continue
        numbers = []
        for cell in row.find_all(["td", "th"]):
            if link in cell.find_all("a"):
                continue
            text = cell.get_text(" ", strip=True)
            if NUM_RE.match(text):
                numbers.append(to_float(text))
        if numbers:
            readings[s["id"]] = {
                "level": numbers[0],
                "numbers": numbers,
                "raw": row.get_text(" | ", strip=True),
            }
    return stamp, readings


# ---------- logica ----------

def register_failure(state, message):
    state["fail"] = state.get("fail", 0) + 1
    if state["fail"] == 1 or state["fail"] % 4 == 0:
        send_ntfy(
            "⚠️ Serchio: controllo non riuscito",
            message + " Controlla a mano sul sito del CFR.",
            priority=4,
            tags=["warning"],
        )


def process(html, now):
    stations = load_json(ROOT / "stations.json")["stations"]
    state = load_json(DATA / "state.json", {})
    history = load_json(DATA / "history.json", [])
    latest = load_json(DATA / "latest.json", {})

    stamp, readings = parse(html, stations)

    # lettura impossibile: avvisa e salva la pagina per capire cosa è cambiato
    if stamp is None or not readings:
        DATA.mkdir(exist_ok=True)
        (DATA / "debug.html").write_text(html[:200000], encoding="utf-8")
        register_failure(state, "Non trovo i livelli nella pagina del CFR.")
        latest.update({"ok": False, "checked": now.isoformat(), "error": "lettura non riuscita"})
        save_json(DATA / "latest.json", latest)
        save_json(DATA / "state.json", state)
        return

    state["fail"] = 0
    stamp_iso = stamp.isoformat()

    # idrometri configurati ma non trovati nella pagina
    missing = [s["name"] for s in stations if s["id"] not in readings]
    key = ",".join(missing)
    if key and key != state.get("missing_notified"):
        send_ntfy(
            "⚠️ Serchio: idrometro non letto",
            "Non riesco a leggere: " + ", ".join(missing) + ". Controlla a mano sul sito del CFR.",
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
        r = readings.get(s["id"])
        if not r:
            continue
        level = r["level"]
        prev = levels.get(s["id"])
        prev_trend = trends.get(s["id"], "steady")
        delta = None
        trend = prev_trend

        if prev is not None:
            delta = round(level - prev, 3)
            if delta > EPS:
                trend = "rising"
                events.append(("up", f"▲ {s['name']}: {fm(level)} m ({fd(delta)})"))
            elif delta < -EPS:
                trend = "falling"
                if prev_trend != "falling":
                    events.append(("down", f"▼ {s['name']}: {fm(level)} m ({fd(delta)}), primo calo"))
            else:
                if prev_trend == "rising":
                    trend = "steady"
                    events.append(("flat", f"■ {s['name']}: {fm(level)} m, livello stabile"))
                # se era in calo o stabile, resta com'era

        levels[s["id"]] = level
        trends[s["id"]] = trend
        cards.append({
            "id": s["id"], "name": s["name"], "level": level,
            "delta": delta, "trend": trend, "numbers": r["numbers"], "raw": r["raw"],
        })

    if first_run:
        lines = [f"{c['name']}: {fm(c['level'])} m" for c in cards]
        send_ntfy(
            "✅ Serchio: monitoraggio attivo",
            f"Dati delle {stamp:%H:%M}\n" + "\n".join(lines)
            + "\nSe un valore non ti sembra un livello in metri, dimmelo.",
            priority=3,
            tags=["white_check_mark"],
        )
    elif events:
        kinds = {k for k, _ in events}
        if "up" in kinds:
            title, prio, tags = "🔺 Serchio: livello in salita", 4, ["warning"]
        elif "down" in kinds:
            title, prio, tags = "🔻 Serchio: livello in calo", 3, ["arrow_down"]
        else:
            title, prio, tags = "Serchio: livello stabile", 3, ["information_source"]
        send_ntfy(
            title,
            "\n".join(t for _, t in events) + f"\nDati delle {stamp:%H:%M}",
            priority=prio,
            tags=tags,
        )

    state["last_stamp"] = stamp_iso
    history.append({"t": stamp_iso, "v": {c["id"]: c["level"] for c in cards}})
    history = history[-MAX_HISTORY:]

    save_json(DATA / "history.json", history)
    save_json(DATA / "latest.json", {
        "ok": True,
        "updated": stamp_iso,
        "checked": now.isoformat(),
        "stations": cards,
        "missing": missing,
    })
    save_json(DATA / "state.json", state)


def main():
    now = datetime.now(timezone.utc)
    try:
        html = fetch_html()
    except Exception as exc:
        state = load_json(DATA / "state.json", {})
        register_failure(state, f"Il sito del CFR non risponde ({type(exc).__name__}).")
        save_json(DATA / "state.json", state)
        latest = load_json(DATA / "latest.json", {})
        latest.update({"ok": False, "checked": now.isoformat(), "error": "sito non raggiungibile"})
        save_json(DATA / "latest.json", latest)
        return
    try:
        process(html, now)
    except Exception:
        traceback.print_exc()
        send_ntfy("⚠️ Serchio: errore nello script",
                  "Lo script di monitoraggio ha avuto un errore. Controlla a mano sul sito del CFR.",
                  priority=4, tags=["warning"])
        sys.exit(1)


if __name__ == "__main__":
    main()

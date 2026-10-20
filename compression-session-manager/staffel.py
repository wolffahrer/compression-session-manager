#!/usr/bin/env python3
"""Staffel-Dienst zum Compression- and Session Manager: startet nach einer erschöpften Sitzung die Folgesitzung.

Ablauf: Das Plugin legt nach dem Zug, der auf „Sitzung erschöpft“ folgt, eine Anfrage in <staffel_ordner>/anfragen/ ab.
Dieser Dienst
  1. wartet, bis die alte Sitzung ruht,
  2. sucht das Abschlussprotokoll (`*_Abschluss.md` im Protokollordner, jünger als „erschöpft“),
  3. legt eine neue Sitzung „<Titel> · Teil N“ an und schickt ihr „Lies <Protokoll> und arbeite die nächsten Schritte ab“,
  4. stellt optional eine Brücken-Zieldatei von der alten auf die neue Sitzung um,
  5. meldet das Ergebnis über melde_befehl.
Er hält seine Verbindung zu `hermes serve` offen. Sitzungen ohne Verbindung räumt Hermes sonst als Waise ab.
In der Desktop-App erscheint die neue Sitzung in der Liste und kann jederzeit geöffnet werden.

Sicherungen: höchstens staffel_max_pro_tag Staffeln je Ursprungssitzung und Tag; ohne Protokoll keine Staffel
(15 min Frist, danach Meldung); nur Anfragen aus dem Plugin, nichts sonst.

Aufruf: python staffel.py [--einmal]  (mit dem Python von Hermes, braucht `websockets` und `yaml`)
Konfig: ~/.hermes/config.yaml → plugins.entries.compression-session-manager; Token: HERMES_DASHBOARD_SESSION_TOKEN
aus ~/.hermes/.env; URL: CSM_WS_URL (Standard ws://127.0.0.1:9119/api/ws). Lizenz: MIT.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import websockets

HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
NAME = "compression-session-manager"
CFG = {"ordner": "~/.hermes/uebergabe", "staffel_ordner": "~/.hermes/cache/csm_staffel", "staffel_max_pro_tag": 3,
       "melde_befehl": "", "melden": True, "brueckenziel_datei": ""}
FRIST_PROTOKOLL = 15 * 60
TEIL = re.compile(r"\s*·\s*Teil\s+(\d+)\s*$")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def lade_konfig():
    try:
        import yaml
        d = yaml.safe_load((HOME / "config.yaml").read_text(encoding="utf-8")) or {}
        e = ((d.get("plugins") or {}).get("entries") or {}).get(NAME) or {}
        CFG.update({k: v for k, v in e.items() if k in CFG})
    except Exception as e:  # noqa: BLE001
        log("Konfig nicht gelesen, Standardwerte:", e)


def pfad(k):
    return Path(os.path.expanduser(str(CFG[k])))


def token():
    for z in (HOME / ".env").read_text(encoding="utf-8").splitlines():
        if z.startswith("HERMES_DASHBOARD_SESSION_TOKEN="):
            return z.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("kein HERMES_DASHBOARD_SESSION_TOKEN in ~/.hermes/.env")


def melde(text):
    log("MELDUNG:", text)
    b = os.path.expanduser(str(CFG.get("melde_befehl") or ""))
    if CFG.get("melden") and b and os.path.exists(b):
        try:
            subprocess.run([b, text], timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass


class Draht:
    """Eine Websocket-Verbindung; Antworten per id, Ereignisse werden verworfen (müssen aber gelesen werden)."""

    def __init__(self, url):
        self.url, self.ws, self.offen, self.n = url, None, {}, 0

    async def verbinde(self):
        self.ws = await websockets.connect(self.url, max_size=None, ping_interval=30)
        asyncio.create_task(self._lies())

    async def _lies(self):
        try:
            async for raw in self.ws:
                for teil in str(raw).splitlines():
                    if teil.strip():
                        try:
                            m = json.loads(teil)
                        except ValueError:
                            continue
                        f = self.offen.pop(m.get("id"), None)
                        if f and not f.done():
                            f.set_result(m)
        except Exception as e:  # noqa: BLE001
            log("Verbindung zu:", e)
        for f in self.offen.values():
            if not f.done():
                f.set_exception(ConnectionError("Verbindung zu"))
        self.offen.clear()

    async def rpc(self, methode, params, timeout=60):
        self.n += 1
        rid = f"st{self.n}"
        f = asyncio.get_running_loop().create_future()
        self.offen[rid] = f
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": methode, "params": params}) + "\n")
        return await asyncio.wait_for(f, timeout)

    @property
    def lebt(self):
        return self.ws is not None and getattr(self.ws, "state", None) is not None and self.ws.state.name == "OPEN"


def zustand_laden():
    try:
        return json.loads((pfad("staffel_ordner") / "staffel_zustand.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"zaehler": {}, "gehalten": []}


def zustand_speichern(z):
    p = pfad("staffel_ordner") / "staffel_zustand.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    z["gehalten"] = [g for g in z.get("gehalten", []) if time.time() - g.get("zeit", 0) < 2 * 86400]
    heute = date.today().isoformat()
    z["zaehler"] = {k: v for k, v in z.get("zaehler", {}).items() if k.startswith(heute)}
    p.write_text(json.dumps(z, ensure_ascii=False, indent=1), encoding="utf-8")


def finde_protokoll(ordner, ab):
    kandidaten = [p for p in Path(ordner).glob("*_Abschluss.md") if p.stat().st_mtime >= ab - 120 and p.stat().st_size > 200]
    return max(kandidaten, key=lambda p: p.stat().st_mtime) if kandidaten else None


def verschiebe(anfrage, wohin, grund=""):
    ziel = anfrage.parent.parent / wohin
    ziel.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(anfrage.read_text(encoding="utf-8"))
        d["ergebnis"] = grund
        d["erledigt"] = time.time()
        (ziel / anfrage.name).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        anfrage.unlink()
    except Exception:  # noqa: BLE001
        anfrage.rename(ziel / anfrage.name)


def bruecke_umstellen(alt_key, neu_key):
    datei = str(CFG.get("brueckenziel_datei") or "")
    if not datei:
        return ""
    p = Path(os.path.expanduser(datei))
    try:
        s = p.read_text(encoding="utf-8")
        if alt_key and alt_key in s:
            p.write_text(s.replace(alt_key, neu_key), encoding="utf-8")
            return f" Brücke umgestellt ({p.name})."
    except Exception as e:  # noqa: BLE001
        return f" Brücke NICHT umgestellt: {e}"
    return ""


async def bearbeite(draht, anfrage):
    a = json.loads(anfrage.read_text(encoding="utf-8"))
    sid, alt_titel = a["session_id"], a.get("titel") or ""
    live = (await draht.rpc("session.active_list", {})).get("result", {}).get("sessions", [])
    alt = next((s for s in live if sid in (s.get("session_key"), s.get("stored_session_id"), s.get("id"))), None)
    if alt and alt.get("status") != "idle":
        return "wartet"                                   # alte Sitzung arbeitet noch
    alt_key = (alt or {}).get("session_key") or sid
    alt_titel = alt_titel or (alt or {}).get("title") or sid
    prot = finde_protokoll(a.get("ordner") or pfad("ordner"), float(a.get("erschoepft_zeit") or a["zeit"]))
    if not prot:
        if time.time() - float(a["zeit"]) < FRIST_PROTOKOLL:
            return "wartet"
        melde(f"[Staffel] Kein Abschlussprotokoll für „{alt_titel}“ gefunden, keine Folgesitzung. Bitte selbst übernehmen.")
        verschiebe(anfrage, "fehler", "kein Protokoll")
        return "fehler"
    m = TEIL.search(alt_titel)
    stamm = TEIL.sub("", alt_titel).strip() or alt_titel
    teil = (int(m.group(1)) if m else 1) + 1
    z = zustand_laden()
    schl = f"{date.today().isoformat()}|{stamm}"
    if z["zaehler"].get(schl, 0) >= int(CFG["staffel_max_pro_tag"]):
        melde(f"[Staffel] „{stamm}“ hat heute schon {z['zaehler'][schl]} Staffeln, Grenze erreicht. Keine Folgesitzung; "
              f"Protokoll: {prot}")
        verschiebe(anfrage, "fehler", "Tagesgrenze")
        return "fehler"
    r = await draht.rpc("session.create", {})
    neu = r.get("result") or {}
    nid, nkey = neu.get("session_id"), neu.get("stored_session_id") or neu.get("session_key")
    if not nid:
        melde(f"[Staffel] Folgesitzung für „{alt_titel}“ ließ sich nicht anlegen: {r.get('error')}")
        return "wartet"
    titel = f"{stamm} · Teil {teil}"
    await draht.rpc("session.title", {"session_id": nid, "title": titel})
    text = (f"[Staffel] Fortsetzung von „{alt_titel}“ (die Sitzung war nach 2 Kompressionen erschöpft). "
            f"Lies {prot} und arbeite die nächsten Schritte der Reihe nach ab. Starte nichts, was das Protokoll nicht nennt; "
            f"offene Entscheidungen des Menschen bleiben offen.")
    s = await draht.rpc("prompt.submit", {"session_id": nid, "text": text})
    if s.get("error"):
        melde(f"[Staffel] Startsatz an „{titel}“ abgewiesen: {s['error']}")
    z["zaehler"][schl] = z["zaehler"].get(schl, 0) + 1
    z["gehalten"].append({"id": nid, "key": nkey, "titel": titel, "zeit": time.time()})
    zustand_speichern(z)
    br = bruecke_umstellen(alt_key, nkey or "")
    melde(f"[Staffel] „{titel}“ gestartet, Protokoll {prot.name}.{br}")
    verschiebe(anfrage, "erledigt", f"gestartet {nkey}")
    return "ok"


async def lauf(einmal=False):
    lade_konfig()
    url = os.environ.get("CSM_WS_URL", "ws://127.0.0.1:9119/api/ws") + f"?token={token()}"
    ordner = pfad("staffel_ordner")
    (ordner / "anfragen").mkdir(parents=True, exist_ok=True)
    draht = Draht(url)
    while True:
        try:
            if not draht.lebt:
                # Bewusst kein session.resume alter Sitzungen nach einem Neuaufbau: das würde auch Sitzungen
                # wiederbeleben, die der Mensch geschlossen hat. Gespeichert bleiben sie ohnehin.
                await draht.verbinde()
                log("verbunden")
            (ordner / "herzschlag").write_text(str(time.time()))
            for anfrage in sorted((ordner / "anfragen").glob("*.json")):
                try:
                    log(anfrage.name, "→", await bearbeite(draht, anfrage))
                except (ConnectionError, asyncio.TimeoutError) as e:
                    log("Verbindungsproblem:", e)
                    break
                except Exception as e:  # noqa: BLE001
                    log("Fehler bei", anfrage.name, e)
                    verschiebe(anfrage, "fehler", f"Ausnahme: {e}")
        except Exception as e:  # noqa: BLE001
            log("Schleife:", e)
            draht.ws = None
        if einmal:
            return
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(lauf("--einmal" in sys.argv))

"""Compression- and Session Manager (compression-session-manager) – Hermes-Plugin.

Regel: Vor jeder Kontext-Kompression schreibt der Agent ein Übergabeprotokoll, damit nichts verloren geht. Nach der
2. Kompression gilt die Sitzung als erschöpft: Der Agent beendet den Schritt sauber und schreibt ein Abschlussprotokoll.
Läuft der Staffel-Dienst (`staffel.py`, Tandem zu diesem Plugin), startet danach automatisch eine Folgesitzung mit dem
Protokoll; sonst öffnet der Mensch sie selbst (der Server kann keine Desktop-Sitzung per `/new` öffnen).

Hooks:
  post_api_request       merkt sich die Plattform je Sitzung (Cron-Läufe werden ausgelassen)
  transform_tool_result  hängt den Hinweis an das nächste Werkzeugergebnis (kommt auch mitten in langen Zügen an)
  on_session_end         nach dem Zug, der auf „erschöpft“ folgt: Staffel-Anfrage für den Dienst ablegen
Fail-open: Jeder Fehler hier lässt das Ergebnis unverändert. Einstellungen: plugins.entries.compression-session-manager
in config.yaml (siehe README.md). Lizenz: MIT.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("hermes.plugins.compression-session-manager")

NAME = "compression-session-manager"
HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
ZUSTAND = HOME / "cache" / "compression_session_manager.json"
TEST = HOME / "cache" / "compression_session_manager_test.json"
STANDARD = {
    "ordner": "~/.hermes/uebergabe",          # wohin der Agent die Protokolle schreibt
    "melde_befehl": "",                       # optional: Programm, das eine Textzeile als Argument bekommt
    "anteil": 0.82,                           # Protokoll-Hinweis bei 82 % der Kompressionsschwelle
    "schwelle_fallback": 110000,
    "erschoepft_ab": 2,                       # nach so vielen Kompressionen: Sitzung erschöpft
    "ohne_plattformen": ["cron"],
    "melden": True,
    "staffel": True,                          # Staffel-Anfragen für staffel.py ablegen
    "staffel_ordner": "~/.hermes/cache/csm_staffel",
    "staffel_max_pro_tag": 3,                 # wird von staffel.py ausgewertet
    "brueckenziel_datei": "",                 # optional, von staffel.py ausgewertet (siehe README)
}
_lock = threading.Lock()
_plattform: dict[str, str] = {}
_cfg = dict(STANDARD)


def _pfad(schluessel):
    return Path(os.path.expanduser(str(_cfg.get(schluessel) or STANDARD[schluessel])))


def _lade():
    try:
        return json.loads(ZUSTAND.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _speichere(z):
    try:
        ZUSTAND.parent.mkdir(parents=True, exist_ok=True)
        grenze = time.time() - 3 * 86400          # alte Einträge (> 3 Tage) wegräumen
        z = {k: v for k, v in z.items() if v.get("zeit", 0) > grenze}
        tmp = ZUSTAND.with_suffix(".tmp")
        tmp.write_text(json.dumps(z, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(ZUSTAND)
    except Exception:  # noqa: BLE001
        logger.debug("%s: Zustand nicht gespeichert", NAME, exc_info=True)


def _sitzung(session_id):
    """(agent, gateway-session) zur session_id aus dem laufenden tui_gateway, sonst (None, None)."""
    try:
        from tui_gateway import server
        for s in list(server._sessions.values()):
            a = s.get("agent")
            if a is not None and getattr(a, "session_id", None) == session_id:
                return a, s
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _test():
    """Testweiche: TEST = {"sitzungen": [<session_id>], "anteil": 0.05, "zaehler_plus": 0}. Gilt nur für die genannten
    Sitzungen; zaehler_plus simuliert Kompressionen."""
    try:
        return json.loads(TEST.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _messwerte(session_id):
    agent, s = _sitzung(session_id)
    comp = getattr(agent, "context_compressor", None) if agent else None
    if comp is None:
        return None
    zaehler = int(getattr(comp, "compression_count", 0) or 0)
    try:
        from agent.context_breakdown import context_usage_fields
        benutzt = int(context_usage_fields(comp).get("context_used") or 0)
    except Exception:  # noqa: BLE001
        benutzt = int(getattr(comp, "last_prompt_tokens", 0) or 0)
    schwelle = int(getattr(comp, "threshold_tokens", 0) or 0) or _cfg["schwelle_fallback"]
    try:
        from tui_gateway.server import _session_live_title
        titel = _session_live_title(s, session_id) or ""
    except Exception:  # noqa: BLE001
        titel = str((s or {}).get("title") or "")
    t = _test()
    if session_id in (t.get("sitzungen") or []):
        zaehler += int(t.get("zaehler_plus") or 0)
    return {"zaehler": zaehler, "benutzt": benutzt, "schwelle": schwelle, "titel": titel, "sid": session_id}


def _anteil(m):
    t = _test()
    if m.get("sid") in (t.get("sitzungen") or []) and t.get("anteil") is not None:
        return float(t["anteil"])
    return float(_cfg["anteil"])


def _staffel_dienst_lebt():
    """staffel.py schreibt alle 30 s einen Herzschlag; älter als 2 min = Dienst läuft nicht."""
    try:
        return time.time() - (_pfad("staffel_ordner") / "herzschlag").stat().st_mtime < 120
    except Exception:  # noqa: BLE001
        return False


def _melde(text):
    befehl = os.path.expanduser(str(_cfg.get("melde_befehl") or ""))
    if not _cfg.get("melden") or not befehl or not os.path.exists(befehl):
        return
    try:
        subprocess.Popen([befehl, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:  # noqa: BLE001
        logger.debug("%s: Meldung fehlgeschlagen", NAME, exc_info=True)


def _hinweis_protokoll(m, k_naechste):
    pct = int(100 * m["benutzt"] / max(m["schwelle"], 1))
    b, sw = (f"{x:,}".replace(",", ".") for x in (m["benutzt"], m["schwelle"]))
    return (
        f"\n\n[Übergabe-Wächter] Kontext bei {b} von {sw} Token ({pct} % der Kompressionsschwelle). "
        f"Die {k_naechste}. Kompression steht bevor. Bevor du weitermachst: Schließe den laufenden Einzelschritt ab "
        f"(kein halbes Schreiben, kein offener Commit) und schreibe JETZT ein Übergabeprotokoll nach "
        f"`{_pfad('ordner')}/<JJJJ-MM-TT>_<Thema>_K{k_naechste}.md`. Inhalt: Auftrag im Wortlaut, Entscheidungen des Menschen, "
        f"Stand (Dateien mit absoluten Pfaden, Commits, Messwerte), offene Mängel, laufende Hintergrundprozesse, Unteraufträge "
        f"und Jobs mit IDs, Prüfbefehle für den Einstieg, nächste Schritte in Reihenfolge, Startsatz für eine neue Sitzung. "
        f"Nur Fakten, die du belegt hast, aber keinen belegten Fakt wegkürzen. Das Protokoll ist eine Sicherung: Du arbeitest "
        f"danach in DIESER Sitzung normal weiter, die Kompression läuft von allein. Eine neue Sitzung gibt es erst nach dem "
        f"Hinweis „erschöpft“."
    )


def _hinweis_erschoepft(m, automatisch):
    weiter = ("Danach endet dein Zug. Eine Folgesitzung startet automatisch mit diesem Protokoll und arbeitet weiter. "
              "Nenne in deiner Antwort nur den Pfad." if automatisch else
              "Gib dem Menschen danach in deiner Antwort den Startsatz: „Lies <Pfad> und arbeite die nächsten Schritte ab.“ "
              "Er öffnet die neue Sitzung selbst.")
    return (
        f"\n\n[Übergabe-Wächter] Diese Sitzung wurde {m['zaehler']}× komprimiert, damit ist sie erschöpft (Regel: nach der "
        f"2. Kompression neu starten). Beende den laufenden Schritt sauber, starte NICHTS Neues und schreibe das "
        f"Abschlussprotokoll nach `{_pfad('ordner')}/<JJJJ-MM-TT>_<Thema>_Abschluss.md` (Inhalt wie beim Übergabeprotokoll, "
        f"plus „Was in dieser Sitzung verloren gehen könnte“; der Dateiname MUSS auf `_Abschluss.md` enden). {weiter}"
    )


def _on_post_api_request(session_id="", platform="", **kwargs):
    if session_id:
        _plattform[session_id] = platform or ""


def _on_transform_tool_result(result=None, session_id="", **kwargs):
    try:
        if not session_id or not isinstance(result, str):
            return None
        if _plattform.get(session_id, "") in _cfg["ohne_plattformen"]:
            return None
        m = _messwerte(session_id)
        if m is None:
            return None
        with _lock:
            z = _lade()
            e = z.get(session_id, {"protokoll_vor": [], "erschoepft": False})
            e["zeit"] = time.time()
            e["titel"] = m["titel"]
            text = None
            if m["zaehler"] >= int(_cfg["erschoepft_ab"]) and not e.get("erschoepft"):
                auto = bool(_cfg.get("staffel")) and _staffel_dienst_lebt()
                text = _hinweis_erschoepft(m, auto)
                e.update(erschoepft=True, erschoepft_zeit=time.time(), staffel_auto=auto)
                _melde(f"[Übergabe-Wächter] Sitzung „{m['titel'] or session_id}“ ist {m['zaehler']}× komprimiert. "
                       + ("Abschlussprotokoll, danach startet die Folgesitzung automatisch." if auto else
                          "Der Agent schreibt das Abschlussprotokoll. Danach bitte eine neue Sitzung mit dem Startsatz öffnen."))
            else:
                naechste = m["zaehler"] + 1
                if m["benutzt"] >= _anteil(m) * m["schwelle"] and naechste not in e.get("protokoll_vor", []):
                    text = _hinweis_protokoll(m, naechste)
                    e.setdefault("protokoll_vor", []).append(naechste)
            z[session_id] = e
            _speichere(z)
        if text:
            logger.info("%s: Hinweis an %s (Kompressionen %s, %s/%s Token)", NAME, session_id,
                        m["zaehler"], m["benutzt"], m["schwelle"])
            return result + text
    except Exception:  # noqa: BLE001
        logger.debug("%s: Fehler, Ergebnis unverändert", NAME, exc_info=True)
    return None


def _on_session_end(session_id="", completed=False, interrupted=False, failed=False, **kwargs):
    """Zugende nach „erschöpft“: Anfrage für staffel.py ablegen (nur einmal, nur bei sauberem Ende)."""
    try:
        if not session_id or not _cfg.get("staffel") or interrupted or failed or not completed:
            return None
        with _lock:
            z = _lade()
            e = z.get(session_id)
            if not e or not e.get("erschoepft") or e.get("staffel_angefragt"):
                return None
            if not e.get("staffel_auto"):
                # Der Hinweis hat „Mensch öffnet selbst“ gesagt (Dienst lief in dem Moment nicht, z. B. eingefroren):
                # dann keine Anfrage, sonst gäbe es nach dem Auftauen eine zweite Übergabe.
                return None
            anfragen = _pfad("staffel_ordner") / "anfragen"
            anfragen.mkdir(parents=True, exist_ok=True)
            datei = anfragen / f"{int(time.time())}_{session_id}.json"
            datei.write_text(json.dumps({"session_id": session_id, "titel": e.get("titel", ""),
                                         "erschoepft_zeit": e.get("erschoepft_zeit", time.time()),
                                         "ordner": str(_pfad("ordner")), "zeit": time.time()},
                                        ensure_ascii=False), encoding="utf-8")
            e["staffel_angefragt"] = True
            z[session_id] = e
            _speichere(z)
        logger.info("%s: Staffel-Anfrage für %s abgelegt", NAME, session_id)
    except Exception:  # noqa: BLE001
        logger.debug("%s: Staffel-Anfrage fehlgeschlagen", NAME, exc_info=True)
    return None


def _lies_konfig(ctx):
    eintrag = None
    try:
        eintrag = (getattr(ctx, "config", None) or {}).get("plugins", {}).get("entries", {}).get(NAME)
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(eintrag, dict):
        try:
            import yaml
            d = yaml.safe_load((HOME / "config.yaml").read_text(encoding="utf-8")) or {}
            eintrag = ((d.get("plugins") or {}).get("entries") or {}).get(NAME)
        except Exception:  # noqa: BLE001
            eintrag = None
    if isinstance(eintrag, dict):
        _cfg.update({k: v for k, v in eintrag.items() if k in STANDARD})


def register(ctx):
    _lies_konfig(ctx)
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_hook("on_session_end", _on_session_end)

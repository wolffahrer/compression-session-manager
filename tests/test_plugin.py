"""Offline-Test des Plugins ohne Hermes: nachgebauter Gateway mit Fake-Agent. Aufruf: python tests/test_plugin.py"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

HIER = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(tmp)

tg = types.ModuleType("tui_gateway"); srv = types.ModuleType("tui_gateway.server"); tg.server = srv
comp = types.SimpleNamespace(compression_count=0, last_prompt_tokens=50000, context_length=131072, threshold_tokens=110000)
ag = types.SimpleNamespace(session_id="S1", context_compressor=comp)
srv._sessions = {"x": {"agent": ag, "title": "Testsitzung"}}
sys.modules["tui_gateway"] = tg; sys.modules["tui_gateway.server"] = srv

spec = importlib.util.spec_from_file_location("csm", HIER.parent / "compression-session-manager" / "__init__.py")
csm = importlib.util.module_from_spec(spec); spec.loader.exec_module(csm)
gemeldet = []
csm._melde = lambda t: gemeldet.append(t)


class Ctx:
    config = {"plugins": {"entries": {"compression-session-manager": {"staffel_ordner": str(tmp / "staffel")}}}}
    hooks = {}

    def register_hook(self, n, f):
        self.hooks[n] = f


c = Ctx(); csm.register(c)
T, ENDE = c.hooks["transform_tool_result"], c.hooks["on_session_end"]
fehler = 0


def pruefe(name, bedingung):
    global fehler
    print(("OK   " if bedingung else "FEHL ") + name)
    fehler += 0 if bedingung else 1


pruefe("50k, K0: kein Hinweis", T(result="ERG", session_id="S1") is None)
comp.last_prompt_tokens = 91000
r = T(result="ERG", session_id="S1")
pruefe("91k, K0: Hinweis K1", r and "1. Kompression steht bevor" in r and "91.000" in r)
pruefe("91k nochmal: still", T(result="ERG", session_id="S1") is None)
comp.compression_count, comp.last_prompt_tokens = 1, 95000
r = T(result="ERG", session_id="S1")
pruefe("95k, K1: Hinweis K2", r and "2. Kompression steht bevor" in r)
ENDE(session_id="S1", completed=True)
pruefe("vor „erschöpft“: keine Staffel-Anfrage", not list((tmp / "staffel" / "anfragen").glob("*.json")) if (tmp / "staffel" / "anfragen").exists() else True)

comp.compression_count, comp.last_prompt_tokens = 2, 25000
r = T(result="ERG", session_id="S1")
pruefe("K2 ohne Dienst: „erschöpft“, Mensch öffnet selbst", r and "erschöpft" in r and "öffnet die neue Sitzung selbst" in r)
pruefe("Meldung genau einmal", len(gemeldet) == 1)
pruefe("erschöpft nochmal: still", T(result="ERG", session_id="S1") is None)
ENDE(session_id="S1", completed=False, interrupted=True)
pruefe("abgebrochener Zug: keine Anfrage", not (tmp / "staffel" / "anfragen").exists() or not list((tmp / "staffel" / "anfragen").glob("*.json")))
ENDE(session_id="S1", completed=True)
anf = list((tmp / "staffel" / "anfragen").glob("*.json"))
pruefe("sauberes Zugende: genau eine Anfrage", len(anf) == 1 and json.loads(anf[0].read_text())["session_id"] == "S1")
ENDE(session_id="S1", completed=True)
pruefe("zweites Zugende: keine weitere Anfrage", len(list((tmp / "staffel" / "anfragen").glob("*.json"))) == 1)

# Sitzung S2 mit laufendem Dienst (Herzschlag) → Hinweis „startet automatisch“
ag2 = types.SimpleNamespace(session_id="S2", context_compressor=types.SimpleNamespace(
    compression_count=2, last_prompt_tokens=20000, context_length=131072, threshold_tokens=110000))
srv._sessions["y"] = {"agent": ag2, "title": "Zweite"}
(tmp / "staffel").mkdir(parents=True, exist_ok=True); (tmp / "staffel" / "herzschlag").write_text(str(time.time()))
r = T(result="ERG", session_id="S2")
pruefe("K2 mit Dienst: Folgesitzung startet automatisch", r and "startet automatisch" in r)

c.hooks["post_api_request"](session_id="S3", platform="cron")
srv._sessions["z"] = {"agent": types.SimpleNamespace(session_id="S3", context_compressor=types.SimpleNamespace(
    compression_count=5, last_prompt_tokens=100000, context_length=131072, threshold_tokens=110000))}
pruefe("Cron-Sitzung: still", T(result="ERG", session_id="S3") is None)
pruefe("unbekannte Sitzung: still", T(result="ERG", session_id="nix") is None)
pruefe("Ergebnis kein Text: still", T(result={"a": 1}, session_id="S1") is None)
print("\nALLES GRÜN" if not fehler else f"\n{fehler} FEHLER")
sys.exit(1 if fehler else 0)

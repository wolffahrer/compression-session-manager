[English](README.md) | **Deutsch**

<p align="center"><img src="assets/banner.png" alt="Compression- and Session Manager – Hermes-Agent-Plugin von KI SOUVERÄN" width="100%"></p>

# Compression- and Session Manager

Hermes-Plugin plus Staffel-Dienst gegen das Vergessen bei der Kontext-Kompression (`compression-session-manager`).

Lange Agenten-Sitzungen werden irgendwann komprimiert. Was dabei wegfällt, ist weg, und nach mehreren Kompressionen
arbeitet der Agent spürbar schlechter. Dieses Paket setzt eine einfache Regel durch und lässt den Agenten dabei
weiterarbeiten, auch wenn du nicht am Rechner bist:

1. **Vor jeder Kompression ein Übergabeprotokoll.** Bei 82 % der Kompressionsschwelle bekommt der Agent am nächsten
   Werkzeugergebnis einen Hinweis: Einzelschritt abschließen, Protokoll schreiben (Auftrag, Entscheidungen, Stand,
   offene Mängel, laufende Prozesse, nächste Schritte, Startsatz), dann weiterarbeiten.
2. **Nach der 2. Kompression ist die Sitzung erschöpft.** Der Agent beendet den Schritt sauber, startet nichts Neues
   und schreibt ein Abschlussprotokoll (`…_Abschluss.md`).
3. **Staffel:** Läuft der Staffel-Dienst (`staffel.py`), legt er danach automatisch eine Folgesitzung
   „<Titel> · Teil 2“ an und gibt ihr das Protokoll mit. Die Arbeit geht ohne Klick weiter. Ohne Dienst bekommst du
   den Startsatz und öffnest die neue Sitzung selbst (`/new` ist ein Befehl der Desktop-App, der Server kann ihn nicht
   auslösen).

```
Kontext 82 % ─► Hinweis ─► Übergabeprotokoll K1 ─► Kompression 1
Kontext 82 % ─► Hinweis ─► Übergabeprotokoll K2 ─► Kompression 2
nächstes Werkzeug ─► „erschöpft“ ─► Abschlussprotokoll ─► Zugende ─► Anfrage ─► staffel.py ─► „· Teil 2“ arbeitet weiter
```

Der Hinweis hängt am Werkzeugergebnis (`transform_tool_result`) und kommt deshalb auch mitten in langen Zügen an.
Die Schwelle liest das Plugin aus dem Kompressor der Sitzung, es passt also zu jedem Modell und jeder Kontextgröße.
Fehler im Plugin lassen alles unverändert (fail-open).

## Bestandteile

| Datei | Aufgabe |
|---|---|
| `compression-session-manager/__init__.py` | Plugin (Hooks `post_api_request`, `transform_tool_result`, `on_session_end`) |
| `compression-session-manager/staffel.py` | Staffel-Dienst: hält eine Verbindung zu `hermes serve`, startet Folgesitzungen |
| `compression-session-manager/csm-staffel.service` | systemd-User-Dienst für `staffel.py` |
| `tests/test_plugin.py` | Offline-Test der Plugin-Logik ohne Hermes |

## Einbau

```bash
cp -r compression-session-manager ~/.hermes/plugins/
cp ~/.hermes/plugins/compression-session-manager/csm-staffel.service ~/.config/systemd/user/
# in der .service-Datei ExecStart auf das Python von Hermes zeigen lassen (braucht websockets + yaml)
systemctl --user daemon-reload && systemctl --user enable --now csm-staffel.service
```

In `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - compression-session-manager
  entries:
    compression-session-manager:
      ordner: ~/.hermes/uebergabe     # hierhin schreibt der Agent die Protokolle
      anteil: 0.82                    # Hinweis bei 82 % der Kompressionsschwelle
      erschoepft_ab: 2                # nach so vielen Kompressionen: Sitzung erschöpft
      ohne_plattformen: [cron]        # Cron-Läufe auslassen
      melden: true
      melde_befehl: ""                # optional: Skript, das eine Textzeile bekommt (z. B. Telegram-Meldung)
      staffel: true                   # Folgesitzungen über staffel.py
      staffel_max_pro_tag: 3          # Staffeln je Titelstamm und lokalem Kalendertag (neu ab Mitternacht)
      brueckenziel_datei: ""          # optional: Datei, in der eine Sitzungs-ID steht und mitwandern soll
      deckel_warnung: true            # einmal je Sitzung warnen, wenn threshold_tokens ein großes Fenster kappt
      prune_anteil: 0                 # >0: lokale Modelle prunen ab diesem Anteil der Schwelle, Cloud-APIs nie
```

### Lokale Modelle und 1-Mio-API-Modelle nebeneinander

Das Plugin folgt der Kompressionsschwelle von Hermes. Hermes rechnet `Kontextfenster × compression.threshold`,
gedeckelt durch `compression.threshold_tokens`. Dieser Deckel gilt **global**: Ein `threshold_tokens: 110000` für ein
lokales 131K-Modell kappt auch ein API-Modell mit 1 Mio. Fenster bei 110K. Die Sitzung wird dann zweimal komprimiert
und weitergereicht, lange bevor ihr Fenster voll ist. Deshalb nur den Anteil nutzen und den Deckel weglassen:

```yaml
compression:
  threshold: 0.84          # lokal 131K → 110.100 · API-Modell 1 Mio → ~880K
  threshold_tokens: null   # kein absoluter Deckel
```

Hermes liest das für jede neue Sitzung, ein Neustart ist nicht nötig. Anteile je Modell gehören in
`compression.model_thresholds` (bei Fenstern unter 512K hebt Hermes alles unter 75 % an). Mit `deckel_warnung` schreibt
das Plugin einmal je Sitzung eine Warnung ins Log (und meldet über `melde_befehl`), wenn der Deckel die Schwelle unter
die Hälfte des Fensters drückt.

**Lokal prunen, in der Cloud nicht.** Auch `compression.proactive_prune_tokens` (alte Werkzeugergebnisse ohne LLM
entfernen) gilt global. Jeder Prune bricht den Prompt-Cache. Lokal kostet das nichts und spart Kompressionen, bei einer
Cloud-API kostet es Geld. Deshalb global auf `0` setzen und das Plugin je Sitzung entscheiden lassen, mit
`prune_anteil: 0.86`: Ist der Endpunkt des Modells lokal (Loopback, privates Netz, `localhost`, `*.local`), liegt die
Prune-Grenze bei 86 % der Kompressionsschwelle dieser Sitzung (lokal 131K → ~94.700). Bei jedem anderen Endpunkt bleibt
der Prune aus. Das Plugin bewertet das bei jedem API-Aufruf neu, ein Modellwechsel geht also mit. Die erste
Entscheidung je Sitzung steht im Log (`proactive_prune_tokens für <id> = … (lokal|Cloud, <base_url>)`). Ist das Plugin
nicht geladen, prunt nichts (sicher für die Kosten). Wie die Hinweise wirkt das für Sitzungen in `hermes serve`
(Desktop-Sitzungen und die Kanban-Worker, die serve startet). Ein eigenständiger `hermes`-CLI-Prozess behält den
globalen Wert.

Danach `hermes serve` neu starten (Plugins laden nur beim Start), und zwar wenn keine Sitzung arbeitet. Ein Neustart
bricht laufende Züge ab. Prüfen: `hermes plugins list | grep compression`, `systemctl --user status csm-staffel`.

## Sicherungen

- Der Staffel-Dienst startet nur auf eine Anfrage des Plugins, und nur nach einem **sauber beendeten** Zug
  (nicht nach Abbruch oder Fehler).
- **Ohne Abschlussprotokoll keine Staffel:** Der Dienst sucht `*_Abschluss.md`, das jünger als „erschöpft“ und
  größer als 200 Byte ist. Nach 15 Minuten ohne Protokoll meldet er sich und gibt auf.
- **Tagesgrenze** je Ursprungssitzung (Standard 3), damit nichts endlos läuft. Mit einem bezahlten API-Modell kostet
  jede Staffel echtes Geld. So wird gezählt:
  - Der Zähler hängt an *lokalem Kalenderdatum + Titelstamm*. Der Stamm ist der Sitzungstitel ohne den Zusatz
    ` · Teil N`. „IMP“, „IMP · Teil 2“ und „IMP · Teil 3“ zählen also auf denselben Stamm.
  - Es zählen nur Staffeln, die selbst gestartete Sitzung ist frei. Beim Standard 3 läuft ein Stamm an einem Tag als
    Ursprung plus Teil 2–4, also in vier Sitzungen.
  - Maßgeblich ist das lokale Datum des Rechners (`date.today()` in `staffel.py`). Um Mitternacht beginnt der Zähler
    neu. Eine Arbeit, die durch die Nacht läuft, bekommt vor Mitternacht bis zu 3 Staffeln und danach noch einmal 3.
  - Jede Sitzung trägt etwa zwei bis drei Kontextfenster: erstes Fenster, Kompression 1 (Protokoll-Hinweis), zweites
    Fenster, Kompression 2 („erschöpft“), dann das Abschlussprotokoll und die Staffel. Mit den Standardwerten und
    einer Nacht über Mitternacht sind das für eine Aufgabe bis zu 7 Sitzungen und etwa 14–21 Fenster.
  - Ist die Grenze erreicht, meldet der Dienst „Grenze erreicht“ mit dem Pfad zum Abschlussprotokoll und legt keine
    Folgesitzung an. Die nächste Sitzung öffnet dann der Mensch, oder er erhöht `staffel_max_pro_tag`.
- Der Dienst belebt keine geschlossenen Sitzungen wieder.
- Ob der Dienst läuft, erkennt das Plugin an einem Herzschlag (`<staffel_ordner>/herzschlag`). Danach richtet sich
  auch der Hinweis an den Agenten („startet automatisch“ oder „Mensch öffnet selbst“).

## Testen ohne 90.000 Token

Lege `~/.hermes/cache/compression_session_manager_test.json` an, z. B.
`{"sitzungen": ["<session_id>"], "anteil": 0.05, "zaehler_plus": 2}`. Nur für die genannten Sitzungen gilt dann 5 %
als Schwelle, und `zaehler_plus` simuliert Kompressionen (2 = „erschöpft“). `python tests/test_plugin.py` prüft die
Logik ganz ohne Hermes. Danach die Testdatei wieder löschen.

## Erprobt

Am 20.10.2026 in Hermes Desktop (`hermes serve`) mit lokalem Modell:
- Hinweis vor der 1. Kompression und „Sitzung erschöpft“ kamen an und wurden befolgt, die Telegram-Meldung ebenso.
- Ende-zu-Ende: erschöpfte Testsitzung → Abschlussprotokoll → Folgesitzung „· Teil 2“ startete ohne Klick, las das
  Protokoll und arbeitete den nächsten Schritt ab. Den `serve`-Neustart dazwischen überstand der Dienst.

## Grenzen

- Ein Zug ganz ohne Werkzeugaufrufe bekommt den Hinweis erst beim nächsten Werkzeugaufruf.
- Wie gut das Protokoll wird, hängt am Modell.
- Das Plugin liest den Kompressor über `tui_gateway.server._sessions` (Hermes Desktop/serve). In anderen
  Oberflächen findet es keine Sitzung und tut dann nichts.
- Eine Sitzung, die nur der Dienst hält, läuft bei einem `serve`-Neustart nicht automatisch weiter. Gespeichert
  bleibt sie, und sie lässt sich in der App öffnen.

## KI SOUVERÄN

Lokale, souveräne KI — Videos, Code und ein offenes Tagebuch. Jede Ebene verweist auf die beiden anderen.

- 🎥 **YouTube:** [@kisouveran](https://youtube.com/@kisouveran) — Videos & Reichweite
- 💻 **GitHub:** [github.com/wolffahrer](https://github.com/wolffahrer) — der Code dahinter
- 💬 **Telegram:** [t.me/lokale_ki](https://t.me/lokale_ki) — das Tagebuch von Vik & Besen

Lizenz: MIT

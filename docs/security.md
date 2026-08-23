# Sicherheit

## Harte Grenzen (nicht verhandelbar)

- **Kein Live-Trading, keine Orderplatzierung.** Es existiert kein Code-Pfad,
  der Orders erstellt, signiert oder sendet.
- **Keine Wallet-Anbindung, kein Signing, keine privaten Credentials.**
  Der REST-Client kennt ausschliesslich die oeffentlichen
  `/v1/info/*`-Endpunkte; es werden keine Auth-Header gesetzt.
- **Keine privaten Account-/Balance-/Positions-/Orderdaten.**
- **Telegram ist der einzige externe Output-Kanal** (ab Phase 6).
- Keine Aussagen ueber garantierte Performance, Gewinnwahrscheinlichkeit oder
  Erfolgsquote – weder in Code noch in Nachrichten.

## Secrets

- Secrets nur via `.env` (Repo-Root) / Environment; `.env` ist gitignored.
- `TELEGRAM_BOT_TOKEN` ist ein `SecretStr` und erscheint nie in `repr`/Logs.
- Der Logging-Stack redigiert Felder mit `token`, `secret`, `password`, `dsn`,
  `api_key`, `authorization` u. a. (`tests/test_logging.py`).
- Keine Secrets in Exceptions, Systemevents oder Telegram-Meldungen.

## Projektisolierung

- `.env` wird ausschliesslich aus dem Repository-Root geladen (`app/config.py`).
- Keine `sys.path`-Manipulation, keine `../`-Pfade, keine Fremdimporte –
  automatisiert geprueft in `tests/test_isolation.py`.
- Docker-Ressourcen sind `polysignal_`-praefixiert; DB
  `polysignal_intelligence`, User `polysignal_user`, Redis-Prefix `polysignal:`.

## WebSocket-Datenlayer (Phase 5)

- Der WS-Client verbindet sich ausschliesslich mit der oeffentlichen
  Marktdaten-URL und sendet ausser `sub`/`unsub` nichts - keine Auth,
  keine Keys, keine privaten Channels.
- Es werden nie vollstaendige Roh-Payloads geloggt - nur Channel-Namen,
  Groessen, Zaehler und Fehlerklassen.
- Ungueltige oder unplausible Events (NaN/Infinity, negative Preise,
  gekreuzte Buecher, Outlier) werden verworfen und niemals zu Marktstatus
  oder (spaeter) Signalen verarbeitet.
- Phase 5 enthaelt keinerlei Strategie-, Risiko-, Kosten- oder
  Telegram-Logik; ein Test (`tests/test_isolation.py`) erzwingt, dass der
  Datenlayer solche Module nicht importiert.

## Telegram-Layer (Phase 6)

- Telegram ist der **einzige externe Output-Kanal**; eingehend werden nur
  Admin-Kommandos verarbeitet (Long Polling, kein Webhook, kein offener Port).
- Autorisierung strikt ueber `TELEGRAM_ADMIN_USER_IDS` und den konfigurierten
  Gruppen-/Privat-Chat; nicht autorisierte Kommandos loesen keine
  Zustandsaenderung und keine Antwort aus, werden aber auditiert (nur
  User-ID, Chat-Typ, Kommando - keine weiteren personenbezogenen Daten).
- Der Bot-Token existiert nur als `SecretStr` und in der Request-URL des
  zentralen Clients; Fehlerobjekte tragen nur Fehlerklassen, Beschreibungen
  mit Token-/URL-Verdacht werden ersetzt. `/health`, `/status` und
  `/dashboard` geben weder Token noch Chat-IDs noch Roh-Payloads aus.
- Kein Kommando kann Trading ausloesen - es existiert kein Code-Pfad fuer
  Orders, Wallets oder private Polymarket-Daten.
- Formatter escapen jeden interpolierten Wert (HTML), kappen die Laenge und
  senden nie Stacktraces oder Konfigurationswerte in die Gruppe.
- Phase 6 erzeugt keine Handelssignale; Strategie-/Risiko-/Kostenmodule sind
  weiterhin leere Platzhalter (testseitig erzwungen).

## Marktselektions-Layer (Phase 7)

- Reine Daten-/Handelbarkeitsbewertung: keine Richtungs-, Entry-, Stop-,
  Hebel- oder Trade-Begriffe in Logik, Persistenz, Logs oder Telegram-
  Meldungen (testseitig erzwungen).
- Selektions-/Session-Meldungen sind optional (Default aus), laufen ueber
  die Phase-6-Delivery-Queue (Dedup + Rate Limit) und enthalten nie eine
  Handelsempfehlung; im globalen PAUSED-Modus werden sie unterdrueckt.
- Der lokale Handelskalender ist versioniert und offline - kein
  Laufzeit-Netzzugriff; unbekannte Zeitraeume blockieren Equity konservativ.
- Denylist schlaegt Allowlist; die Allowlist kann weder Session- noch
  Datenqualitaets- noch Policy-Blockaden umgehen.

## Strategy-Research-Layer (Phase 8)

- Phase 8 erzeugt ausschliesslich interne Research-Artefakte: **keine
  Trade-Signale, keine Telegram-Ausgabe** und keinerlei
  Entry-/Stop-/Target-/Hebel-/Positionsgroessen- oder Kostenwerte -
  weder in Feldern noch in Logs noch in API-Ausgaben. Ein AST-basierter
  Isolationstest verbietet dem Strategie-Layer zusaetzlich Importe von
  Telegram-, Risiko-, Kosten- und Netzwerk-Modulen (httpx/websockets).
- Alle Strategie-Eingaben sind oeffentliche Marktdaten aus der eigenen
  Persistenz/dem Cache; der Strategiepfad macht keine REST- oder
  WS-Aufrufe.
- Richtungswerte (`BULLISH`/`BEARISH`) sind Strukturklassifikationen und
  werden in der API explizit als "research classification" gekennzeichnet;
  jede Dashboard-Ausgabe traegt den Disclaimer "Research-Ausgabe. Kein
  Trade-Signal. Keine Renditeprognose.".
- Kein LLM und keine Heuristik ausserhalb der versionierten, deterministischen
  Regeln entscheidet ueber Kandidaten; jede Entscheidung ist ueber
  `strategy_version`, `config_hash`, `ruleset_hash` und die persistierten
  Candle-Fenster reproduzierbar.
- API-Sektionen des Strategie-Layers geben weder Secrets noch Konfigurations-
  Rohwerte aus (nur Name, Version, Hashes, Zaehler, Research-Felder).

## Risk-/Kosten-Layer (Phase 9)

- Erzeugt ausschliesslich interne Research-Eignungsbewertungen: **keine
  Orders, kein Live-Trading, keine Schluesselverwahrung, keine
  Signatur-/Transaktionspfade, keine privaten Account-/Balance-/
  Positionsdaten und keine echte Kontogroesse** - das virtuelle
  Referenzkonto ist reine Konfiguration. Ein AST-Isolationstest verbietet
  Risk-/Costs-Modulen Telegram-, Adapter- und Netzwerk-Imports sowie
  Order-/Wallet-Terminologie.
- Keine Telegram-Trade-Signale in Phase 9; keine Nachricht enthaelt
  Entry, Stop, Take Profit oder Hebel. `/status` gibt keine
  Referenzpreise aus; Modell-Level erscheinen nur lokal im Dashboard und
  sind als hypothetische Modellwerte markiert.
- Konservativ-by-default: fehlende Instrument-, Margin-, Fee-, Funding-
  oder Orderbuchdaten blockieren mit strukturiertem Code statt geschaetzt
  zu werden; das approximierte Margin-Modell existiert nur hinter einem
  expliziten Opt-in und wird prominent gekennzeichnet.
- Keine Formulierung verspricht Rendite, Trefferquote oder einen
  "optimalen Hebel"; der Eligibility Score ist ausdruecklich keine
  Gewinnwahrscheinlichkeit (testseitig abgesichert).
- Fee Schedules stammen ausschliesslich aus administrierter
  Konfiguration/Seed/Migration - nie aus unsicheren Laufzeitquellen.

## Betriebssicherheit

- Gewichteter Rate Limiter unterhalb des dokumentierten Polymarket-Budgets.
- Begrenzte Retries mit Backoff und Jitter (keine aggressiven Loops).
- Kill Switch via `APP_KILL_SWITCH` (spaeter zusaetzlich API/Telegram).
- Container laeuft als non-root User.

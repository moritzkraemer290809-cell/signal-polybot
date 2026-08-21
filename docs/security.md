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

## Betriebssicherheit

- Gewichteter Rate Limiter unterhalb des dokumentierten Polymarket-Budgets.
- Begrenzte Retries mit Backoff und Jitter (keine aggressiven Loops).
- Kill Switch via `APP_KILL_SWITCH` (spaeter zusaetzlich API/Telegram).
- Container laeuft als non-root User.

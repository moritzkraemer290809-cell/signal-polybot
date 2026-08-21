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

## Betriebssicherheit

- Gewichteter Rate Limiter unterhalb des dokumentierten Polymarket-Budgets.
- Begrenzte Retries mit Backoff und Jitter (keine aggressiven Loops).
- Kill Switch via `APP_KILL_SWITCH` (spaeter zusaetzlich API/Telegram).
- Container laeuft als non-root User.

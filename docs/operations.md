# Betrieb

## Start

```bash
cp .env.example .env   # anpassen
docker compose up --build -d
```

Migrationen laufen automatisch beim App-Start (`scripts/entrypoint.sh`).

## Health & Status

- `GET /health` – Prozess, PostgreSQL, Redis, Telegram-Konfiguration,
  WebSocket-Status, Discovery-Frische. HTTP 503 bei ausgefallener DB/Redis.
- `GET /status` – Botzustand (`RUNNING`/`PAUSED` via `APP_KILL_SWITCH`),
  Universum, offene Signale, letzte Refresh-Zeit.
- Docker-Healthchecks sind fuer App, PostgreSQL und Redis konfiguriert.

## Kill Switch

`APP_KILL_SWITCH=true` setzen und neu starten (spaeter zusaetzlich via API und
Telegram `/pause`). Pausiert die Signalerzeugung; Prozess und Endpoints bleiben
aktiv.

## Logging

Strukturiertes JSON auf stdout. Sensible Felder (Token, Passwoerter, DSNs)
werden automatisch redigiert (`app/observability/logging.py`).
Korrelations-IDs werden pro Signal-Lifecycle gebunden (ab Phase 10 durchgaengig).

## Troubleshooting

| Problem | Diagnose | Loesung |
|---------|----------|---------|
| App startet nicht, DB-Fehler | `docker compose logs polysignal_postgres` | `POSTGRES_PASSWORD`/`DATABASE_URL` konsistent? Volume `polysignal_pgdata` gesund? |
| `/health` → postgres unavailable | DB down oder falsche URL | Compose-Servicename `polysignal_postgres` in `DATABASE_URL` verwenden |
| `/health` → redis unavailable | `docker compose logs polysignal_redis` | `REDIS_URL` prueft `polysignal_redis:6379` |
| HTTP 429 vom Polymarket-REST | Log-Events `rest_retryable_status` | Budget `POLYMARKET_RATE_LIMIT_BUDGET_PER_MINUTE` senken |
| Discovery findet Symbol nicht | Log `configured_symbols_not_discovered` | Symbol in `UNIVERSE_*` pruefen; Markt ggf. nicht gelistet |
| WebSocket | – | ab Phase 5 |
| Telegram | – | ab Phase 6 (Bot-Token, Gruppen-ID, Admin-IDs in `.env`) |

## WebSocket-Betrieb (Phase 5)

- Eine zentrale Verbindung fuer alle Instrumente und Channels; Subscriptions
  folgen automatisch dem Discovery-Universum (`UniverseRefreshJob`).
- Kernparameter in `.env` (`POLYMARKET_WS_*`): Heartbeat
  (`PING_INTERVAL/PING_TIMEOUT`), Backoff (`RECONNECT_MIN/MAX/JITTER`),
  Fehlversuch-Fenster (`MAX_RECONNECT_ATTEMPTS`/`RECONNECT_WINDOW_SECONDS`),
  Queue-Groesse, Orderbuch-Tiefe, Persist-Batching. Konservative Defaults in
  `.env.example` sind fuer 2 Instrumente x 8 Channels ausgelegt.
- `POLYMARKET_WS_ENABLED=false` startet die App ohne Datenfeed (z. B. reine
  API-/DB-Wartung); `/health` zeigt dann `websocket: disabled`.
- Datenqualitaet je Instrument: `/status` -> `data_quality` (Status, Channel-
  Frische, Gruende). Regeln: siehe docs/architecture.md.
- Redis-/DB-Ausfall: Feed laeuft weiter; Cache/Buffer melden degraded und
  fliessen in den Qualitaetsstatus ein. Kein Prozessabsturz.

## Telegram-Betrieb (Phase 6)

### BotFather-Setup

1. In Telegram `@BotFather` oeffnen -> `/newbot` -> Namen und Username vergeben.
2. Den angezeigten Token in `.env` als `TELEGRAM_BOT_TOKEN` eintragen (nie committen).
3. Private Gruppe erstellen, den Bot einladen.
4. Gruppen-ID ermitteln: kurz `TELEGRAM_ENABLED=true` mit Token starten, eine
   Nachricht in die Gruppe schreiben und die ID aus den Logs/Updates lesen -
   oder einen Hilfsbot wie `@userinfobot` nutzen. Supergruppen-IDs sind
   negativ (z. B. `-1001234567890`).
5. `TELEGRAM_GROUP_ID` und die eigenen Admin-User-IDs in
   `TELEGRAM_ADMIN_USER_IDS` eintragen, dann `TELEGRAM_ENABLED=true` setzen.

### Verhalten

- **Long Polling, kein Webhook** (V1): es wird kein Port nach aussen geoeffnet.
- `/pause` stoppt neue nicht-kritische Deliveries und (ab Phase 7+) Scanner-
  Aktivitaet - nie Health Checks, Data Engine oder kritische Warnungen.
  Zustand persistent (`app_state`), ueberlebt Neustarts, idempotent.
  `APP_KILL_SWITCH=true` wirkt zusaetzlich und kann nur per `.env` geloest werden.
- `/resume` hebt den Runtime-Pause-Zustand auf (nicht den Kill Switch).
- Kommando-Antworten gehen direkt (rate-limited) an den anfragenden Chat;
  Broadcasts (Pause/Resume-Bestaetigung, Systemalerts, spaeter Signale)
  laufen immer ueber die persistente Queue.
- Cooldown fuer `/status`, `/health`, `/daily` pro Admin:
  `TELEGRAM_STATUS_COMMAND_COOLDOWN_SECONDS`.
- Nicht autorisierte Kommandos: keine Antwort, Audit-System-Event, Metrik.

## Backup & Recovery

Persistente Daten liegen in den Volumes `polysignal_pgdata` und
`polysignal_redisdata`. PostgreSQL-Dumps: `docker exec polysignal_postgres
pg_dump -U polysignal_user polysignal_intelligence > backup.sql`.

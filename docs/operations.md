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

## Backup & Recovery

Persistente Daten liegen in den Volumes `polysignal_pgdata` und
`polysignal_redisdata`. PostgreSQL-Dumps: `docker exec polysignal_postgres
pg_dump -U polysignal_user polysignal_intelligence > backup.sql`.

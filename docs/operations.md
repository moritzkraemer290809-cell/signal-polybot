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

## Marktselektion & Sessions (Phase 7)

- Zustand: `/health` -> `market_selection` (Subsystem-State, Job-Liveness,
  Watchlist-Zaehler, Kalenderversion); `/status` -> Sessions inkl. naechster
  Transition, aktive/pausierte Watchlist mit Gruenden; `/dashboard` ->
  letzte Entscheidungen mit Quality-Komponenten.
- Kalender-Update-Prozess (jaehrlich): offiziellen NYSE-Feiertagsplan
  pruefen, `app/sessions/us_equity_calendar_data.py` ergaenzen (FULL_HOLIDAYS
  + EARLY_CLOSES), VERSION und COVERAGE_MAX_YEAR anheben, dazu passend
  `EQUITY_CALENDAR_VERSION`/`EQUITY_CALENDAR_MAX_YEAR` in `.env` setzen,
  Kalender-Tests laufen lassen. Versions-Mismatch oder abgelaufene Abdeckung
  => `CALENDAR_UNAVAILABLE`, Equity wird konservativ blockiert.
- `EQUITY_CALENDAR_ENABLED=false` schaltet Equity-Analyse vollstaendig ab.
- Watchlist leer? `/dashboard` -> `recent_decisions[].reasons` zeigt die
  strukturierten Gruende (z. B. VOLUME_UNAVAILABLE, wenn der oeffentliche
  statistics-Channel kein Volumen liefert -> ggf.
  `MARKET_SELECTION_REQUIRE_VOLUME=false` setzen; bewusst konservativer
  Default).
- Globaler PAUSED-Modus: Data Engine und Selection laufen weiter, nur
  optionale Selektionsmeldungen entfallen.

## Strategy Research (Phase 8)

- Zustand: `/health` -> `strategy` (Subsystem-State HEALTHY/DEGRADED/
  UNAVAILABLE/DISABLED, Job-Liveness, aktive Kandidaten, Evaluations-
  zaehler); `/status` -> Strategiename/-version, `config_hash`, letzter
  Run, Regime/Struktur/letzte Rejection-Codes pro Instrument; `/dashboard`
  -> Kandidaten nach Zustand, letzte Kandidaten mit Score und Begruendung,
  aggregierte Ablehnungen. Alle Ausgaben tragen den Research-Disclaimer -
  es sind keine Trade-Signale.
- `STRATEGY_ENABLED=false` deaktiviert das Subsystem vollstaendig (App,
  Feed und Selektion laufen normal weiter).
- Der Job bewertet nur ACTIVE-Watchlist-Instrumente und nur, wenn eine neue
  geschlossene 5m-Candle vorliegt; `skipped_no_new_candle` im
  `/status`-Run-Summary ist daher normal.
- Keine Kandidaten? `/dashboard` -> `strategy.recent_rejections` zeigt die
  strukturierten Codes (z. B. `SETUP_SCORE_BELOW_THRESHOLD`,
  `MOMENTUM_INSUFFICIENT`, `REGIME_*`). `NO_TRADE`/leer ist ein korrektes
  Ergebnis, kein Fehler.
- Parameteraenderungen (Score-Gewichte, Schwellen, Regime-Regeln) aendern
  den `config_hash`; fuer nachvollziehbare Historie zusaetzlich
  `STRATEGY_VERSION` anheben. Ungueltige Konfiguration (Gewichtssumme
  != 100, fehlende Pflicht-Timeframes) verhindert den Start.
- Globaler PAUSED-Modus (`/pause`): keine neuen Kandidaten, bestehende
  expiren regulaer; Datenerfassung und Bewertungs-Skips laufen weiter.
- DB-Ausfall: `strategy`-Subsystem meldet DEGRADED,
  `strategy.persistence_failures` steigt; unpersistierte Kandidaten werden
  nie als erfolgreich gemeldet. Prozess laeuft weiter.
- Retention: Feature-Snapshots werden nach
  `STRATEGY_FEATURE_RETENTION_DAYS` (Default 14) aufgeraeumt; Kandidaten,
  Events und Rejections bleiben als Research-Historie erhalten.

## Risk & Cost Research (Phase 9)

- Zustand: `/health` -> `risk` (Risk-/Cost-Subsystem-State, Job-Liveness,
  aktive Fee Schedule, Plan-Zaehler nach Status, Rejections der letzten
  Stunde); `/status` -> Modellversionen, Config-Hashes,
  Fee-Schedule-Version ("assumption only"), letzter Run - ohne
  Referenzpreise; `/dashboard` -> Plan-Details mit Score-Zerlegung,
  Reason Codes und klar markierten Modell-Referenzleveln (nur lokal).
- `RISK_ENGINE_ENABLED=false` deaktiviert das Subsystem vollstaendig.
- Keine Plaene? `/dashboard` -> `risk.recent_rejections` zeigt die
  strukturierten Codes (z. B. `MARGIN_MODEL_UNAVAILABLE`,
  `NET_RR_BELOW_THRESHOLD`, `ORDERBOOK_DEPTH_INSUFFICIENT`). Eine
  Ablehnung ist ein korrektes, konservatives Ergebnis - kein Fehler.
- **Fee Schedule**: wird beim ersten Lauf aus der Konfiguration geseedet
  und aktiviert; Versionen sind immutable. Satzaenderung =>
  `COST_FEE_SCHEDULE_VERSION` anheben (neue Version, alte bleibt
  historisch). Die Saetze sind Annahmen und muessen vor Live-Betrieb
  gegen die offizielle Polymarket-Dokumentation validiert werden.
- **Margin-Daten fehlen** (haeufig, da oeffentliche Metadaten keine
  Margin-Raten liefern): Standard ist konservatives Blockieren. Nur wer
  das explizit will, setzt `RISK_ALLOW_APPROXIMATED_MARGIN_MODEL=true`
  (+ approx. Raten) - jeder betroffene Plan ist prominent als
  `APPROXIMATED` markiert.
- Konfigurationsaenderungen (Risk-/Cost-Settings) aendern die
  Config-Hashes: bestehende aktive Plaene werden superseded und neu
  bewertet; Historie bleibt vollstaendig erhalten.
- Globaler PAUSED-Modus: keine neuen ELIGIBLE-Plaene (`BOT_PAUSED`-
  Rejections), bestehende Historie bleibt; keine Telegram-Ausgabe.
- DB-Ausfall: `risk` meldet DEGRADED, unpersistierte Plaene werden nie
  als Erfolg gemeldet; Prozess, Feed und API laufen weiter.

## Internal Signal Lifecycle (Phase 10)

- Zustand: `/health` -> `signals` (Subsystem-State, Job-Liveness,
  Signal-Zaehler nach Zustand, Rejections der letzten Stunde);
  `/status` -> Zustaende, Zeitstempel, Modell-/Schema-Versionen, letzter
  Zyklus - **ohne jegliche Preisniveaus**; `/dashboard` -> volle Details
  inkl. Event-Historie, Updates, Modell-Referenzwerten ("Interne
  Modell-Referenzwerte - keine Handelsanweisung") und State-Machine-Karte,
  immer unter "Internal Research Lifecycle - kein Handelssignal, keine
  reale Position."
- `SIGNAL_LIFECYCLE_ENABLED=false` deaktiviert das Subsystem vollstaendig.
  `SIGNAL_LIFECYCLE_TELEGRAM_OUTPUT_ENABLED=true` wird vom
  Konfig-Validator mit Startabbruch abgelehnt - Phase 10 sendet nichts an
  Telegram.
- Keine Signale? `/dashboard` -> `signals.recent_rejections` zeigt die
  strukturierten Admission-Codes (z. B. `SESSION_NOT_ALLOWED`,
  `ORDERBOOK_NOT_FRESH`, `SIGNAL_DUPLICATE`). Eine Ablehnung ist ein
  korrektes, konservatives Ergebnis - kein Fehler.
- Terminale Zustaende (`TECHNICAL_EXIT`, `INVALIDATED`, `EXPIRED`,
  `SUPERSEDED`, `REJECTED`, `DATA_INVALID`) sind endgueltig; fortgesetzte
  Forschung erscheint als NEUES Signal mit neuer ID (Admission-Pfad).
- Globaler PAUSED-Modus: keine neuen Admissions, keine
  Entry-Bestaetigungen; Abschluss-Monitoring (Expiry/Supersede/DQ) laeuft
  weiter.
- DB-Ausfall: `signals` meldet DEGRADED; unpersistierte Transitionen
  werden nie als Erfolg gemeldet, abgelaufene Leases werden nach Neustart
  automatisch reklamiert. Redis wird von Phase 10 nicht benoetigt.
- Metriken (Praefix `signal.`): `signal.admissions`,
  `signal.admission_rejections.*`, `signal.transitions(.state)`,
  `signal.terminal.*`, `signal.state_version_conflicts`,
  `signal.invalid_transitions_suppressed`, `signal.updates(+_aggregated)`,
  `signal.monitor_cycles`, `signal.monitor_cycle_duration_seconds`,
  `signal.monitor_failures`, `signal.transition_persist_failures`,
  `signal.lease_contention`, `signal.active_count`,
  `signal.expired_leases`, `signal.overlap_skipped`. Details:
  [`docs/signal-lifecycle.md`](signal-lifecycle.md).

## Simulation & Backtesting (Phase 11)

- **Standardmaessig aus**: `SHADOW_MODE_ENABLED=false`,
  `BACKTEST_ENABLED=false`, `SIMULATION_EXPORT_ENABLED=false`. Erst nach
  bewusster lokaler Aktivierung laufen hypothetische Auswertungen.
- Zustand: `/health` -> `shadow_simulation` und `backtest`
  (Subsystem-State, Job-Liveness, laufende Laeufe, letzter Erfolg);
  `/status` -> Simulationen nach Status, letzte Laeufe, Modell-/
  Manifest-Versionen, Sample-Status; `/dashboard` -> beide
  Pflicht-Disclaimer ganz oben, Experimente, Laeufe, Simulationen mit
  Kostenaufloesung, Metriken samt Stichprobengrenze, ausgeschlossene
  Datenabschnitte.
- **Backtests laufen nie automatisch**: jeder Lauf wird explizit lokal
  angefordert und benoetigt ein vollstaendiges, immutables Manifest.
  Fehlt eine Pflichtangabe, wird der Lauf mit `MANIFEST_INVALID`
  abgelehnt.
- Datenluecken: ohne `BACKTEST_ALLOW_SEGMENTED_DATA` wird der Lauf
  abgelehnt (`DATA_GAP` / `BACKTEST_DATA_INCOMPLETE`); mit Segmentierung
  endet er als `COMPLETED_WITH_GAPS` und listet die ausgeschlossenen
  Intervalle. Fehlende Daten werden nie interpoliert.
- Lokale Eingabedateien liegen ausschliesslich unter
  `BACKTEST_ALLOWED_INPUT_DIRECTORY` (repository-relativ, ohne `..`).
- Keine Simulationen sichtbar? `/dashboard` ->
  `shadow_simulation.recent_rejections` zeigt die strukturierten Codes
  (z. B. `ENTRY_BOOK_STALE`, `FUNDING_UNAVAILABLE`,
  `ENTRY_SLIPPAGE_EXCESSIVE`). Eine Ablehnung ist ein korrektes,
  konservatives Ergebnis - kein Fehler.
- Globaler PAUSED-Modus: keine neuen simulierten Positionen.
- DB-Ausfall: Subsystem `DEGRADED`; eine nicht persistierte Simulation
  wird nie als abgeschlossen gemeldet. Redis wird von Phase 11 nicht
  benoetigt. Abgeschlossene Laeufe werden nie ueberschrieben.
- Wenige vollstaendige Simulationen: Metriken tragen
  `INSUFFICIENT_SAMPLE`, Quotenkennzahlen werden unterdrueckt - daraus
  darf keine Aussage ueber Setup-Qualitaet abgeleitet werden.
- Metriken (Auswahl): `shadow_simulations_created/completed/incomplete/
  rejected`, `shadow_simulation_entry_delay_seconds`,
  `backtest_runs_started/completed/rejected/failed`,
  `backtest_events_replayed`, `backtest_lookahead_guard_rejections`,
  `backtest_data_gap_intervals`, `simulation_dedupe_suppressed`,
  `simulation_costs_total_modeled`, `simulation_funding_unavailable`,
  `performance_metrics_insufficient_sample`. Details:
  [`docs/simulation-and-backtesting.md`](simulation-and-backtesting.md).

## Backup & Recovery

Persistente Daten liegen in den Volumes `polysignal_pgdata` und
`polysignal_redisdata`. PostgreSQL-Dumps: `docker exec polysignal_postgres
pg_dump -U polysignal_user polysignal_intelligence > backup.sql`.

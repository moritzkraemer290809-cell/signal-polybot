# Internal Signal Lifecycle (Phase 10)

> **Internal Research Lifecycle - kein Handelssignal, keine reale Position.**
> Alle Zustaende beschreiben die technische Entwicklung eines internen
> Research-Signals. Es gibt keine Order, keinen Fill, keine Position, keine
> Performance-Aussage und in Phase 10 keinerlei Telegram-Ausgabe.

## Zweck

Phase 10 macht aus einem **ELIGIBLE**-Plan der Phase 9 ein internes,
versioniertes Lifecycle-Objekt und ueberwacht dessen technische Entwicklung
gegen ausschliesslich **oeffentliche** Marktdaten (Mark-Preis, BBO,
GESCHLOSSENE 5m-Kerzen, Datenqualitaet, Sessions). Ergebnis sind
Zustandsuebergaenge, ein unveraenderlicher Event-Audit-Trail und ein
aggregierter Beobachtungs-Stream - nichts davon ist eine Handelsanweisung.

## State Machine (15 Zustaende, geschlossene Uebergangsmenge)

```
DRAFT ─▶ PENDING_ADMISSION ─▶ WATCHING_ENTRY ─▶ ENTRY_CONFIRMED ─▶ ACTIVE_RESEARCH
   │            │                   │                  │                 │
   ▼            ▼                   ▼                  ▼                 ▼
REJECTED   REJECTED/EXPIRED/   INVALIDATED/       INVALIDATED/     TARGET_1_REACHED ─▶ TARGET_2_REACHED ─▶ TRAILING_RESEARCH
           PAUSED/DATA_INVALID EXPIRED/           EXPIRED/               │ (u. alle Exits)     │ (auto ─▶ TRAILING)   │
                               SUPERSEDED/        SUPERSEDED/            ▼                     ▼                      ▼
                               PAUSED/            PAUSED/           TECHNICAL_EXIT/       TECHNICAL_EXIT/        TECHNICAL_EXIT/
                               DATA_INVALID/      DATA_INVALID      INVALIDATED/          EXPIRED/               EXPIRED/
                               REJECTED                             EXPIRED/SUPERSEDED/   SUPERSEDED/            SUPERSEDED/
                                                                    PAUSED/DATA_INVALID   PAUSED/DATA_INVALID    PAUSED/DATA_INVALID
```

- **Terminal (immutable, nie reaktivierbar):** `TECHNICAL_EXIT`,
  `INVALIDATED`, `EXPIRED`, `SUPERSEDED`, `REJECTED`, `DATA_INVALID`.
  Fortgesetzte Forschung erfordert IMMER ein neues Signal mit neuer ID.
- **`PAUSED` (dokumentierte Erweiterung):** nicht terminal, aber ruhend.
  Einzige Ausgaenge sind die Abschluss-Uebergaenge `EXPIRED`, `SUPERSEDED`,
  `DATA_INVALID`. Kein Wiedereinstieg in aktives Monitoring - Fortsetzung
  nur ueber ein neues Signal.
- **`INVALIDATED` ist nach `TARGET_2_REACHED`/`TRAILING_RESEARCH` illegal**;
  ein spaeterer Bruch des Invalidationslevels wird deterministisch als
  `TECHNICAL_EXIT` abgebildet.
- Alles NICHT explizit gelistete ist ein invalider Uebergang: er wird nie
  ausgefuehrt, als `INVALID_TRANSITION` aggregiert persistiert und geloggt.
- Die serialisierte Uebergangskarte ist im Dashboard einsehbar
  (`state_machine`-Block) - Quelle: `app/signals/state_machine.py`.

## Admission (kein Plan wird ungeprueft uebernommen)

Gates in fester Reihenfolge, jede Ablehnung erzeugt einen strukturierten
Code (aggregiert persistiert, nie ein Signal-Row-Flood):

`BOT_PAUSED`, `PLAN_NOT_ELIGIBLE`, `PLAN_EXPIRED`, `PLAN_SNAPSHOT_STALE`,
`CANDIDATE_NOT_CONFIRMED`, `CANDIDATE_SUPERSEDED`, `CANDIDATE_EXPIRED`,
`CANDIDATE_SNAPSHOT_STALE`, `WATCHLIST_NOT_ACTIVE`, `SESSION_NOT_ALLOWED`,
`MARKET_QUALITY_INSUFFICIENT`, `DATA_QUALITY_INSUFFICIENT`,
`ORDERBOOK_NOT_FRESH`, `INSTRUMENT_SNAPSHOT_STALE`, `SIGNAL_DUPLICATE`,
`ACTIVE_SIGNAL_LIMIT_REACHED`, `ADMISSION_CONFIGURATION_INVALID`,
`ADMISSION_ERROR` (+ Laufzeitcodes `INVALID_TRANSITION`,
`STATE_VERSION_CONFLICT`).

Eine Admission erzeugt das Signal direkt in `WATCHING_ENTRY` mit der
Ereigniskette `CREATED (v0) → ADMITTED (v1) → WATCHING_ENTRY (v2)` und
kopiert die technischen Referenzwerte des Plans als **unveraenderliche**
Snapshot-Felder. Ein abgelehnter Plan erzeugt keinerlei Lifecycle-Zustand
und veraendert Plan/Candidate nicht.

## Entry-Trigger (niemals ein Fill)

"Entry-Trigger erfuellt" heisst ausschliesslich: Marktbedingungen haben die
technische Referenz-Entry-Zone des Plans beruehrt/bestaetigt. Es bedeutet
NIE, dass jemand ausgefuehrt wurde oder eine Position existiert.

| Trigger | Bedingung (bullish; bearish gespiegelt) |
| --- | --- |
| `ZONE_TOUCH` | frisches BBO (konservative Seite: Ask) innerhalb der Zone ± Toleranz |
| `ZONE_TOUCH_AND_5M_CLOSE_CONFIRM` (Default) | geschlossene 5m-Kerze beruehrt die Zone UND schliesst darin |
| `RECLAIM_LEVEL_CLOSE_CONFIRM` (Sweep-Typen) | 5m-Close reclaimt den Anker-Level innerhalb der Zone |
| `RETEST_CONFIRM` (Continuation-Typen) | Kerze retestet die Zone und schliesst auf der validen Seite |
| `BREAKOUT_CLOSE_CONFIRM` (Breakout-Typen) | 5m-Close bestaetigt den Breakout-Level innerhalb der Zone |

- Alle Close-Trigger verlangen eine **GESCHLOSSENE** Kerze; offene Kerzen
  bestaetigen nie etwas.
- Kerzenbasierte Bestaetigungen tragen den expliziten Flag
  `CANDLE_APPROXIMATED_INTRABAR_ORDER` (Intrabar-Reihenfolge ist aus Kerzen
  nicht beweisbar).
- Keine Bestaetigung bei: global pausiert, Session blockiert, Datenqualitaet
  nicht ausreichend, Orderbuch-Resync, abgelaufenem Signal oder Preis auf
  der falschen Seite der Invalidation.
- Bestaetigter Entry laeuft als dokumentierte Kette
  `WATCHING_ENTRY → ENTRY_CONFIRMED → ACTIVE_RESEARCH` in EINEM Zyklus
  (zwei Events, ein finaler Zustand).

## Monitore & konservative Referenzen

Neun Monitore laufen pro Zyklus ueber einen unveraenderlichen Kontext:
Datenqualitaet, Plan/Candidate, Supersede, Expiry, Session, Invalidation,
Structure-Exit, Targets, Entry. Konservative Referenzquellen sind Mark-Preis,
BBO (fuer bullishe Exits die Bid-Seite, fuer bearishe die Ask-Seite) und
geschlossene 5m-Kerzen - **niemals Last-Preis allein, niemals stale Daten**.

- **Invalidation** (`CONSERVATIVE_COMBINED` Default): jede frische Quelle
  auf/jenseits des Levels genuegt; per Policy auf Mark/BBO/5m-Close oder
  Close-Bestaetigung einschraenkbar. Wortlaut immer "Signal technisch
  invalidiert - Forschungsthese verletzt", nie "Stop gefuellt".
- **Targets**: ausschliesslich die persistierten Referenz-Targets des Plans
  (nie erfunden); Target 2 wird vor Target 1 geprueft; bereits reflektierte
  Targets feuern nie erneut; nach Target 2 automatische Progression zu
  `TRAILING_RESEARCH` (INFO-Prioritaet). Keine Gewinn-/Fill-Aussage.
- **Structure-Exit**: nur bestaetigte Phase-8-Gegensignaturen (z. B.
  bearisher ChoCh nach bullishem Entry) oder - bei Breakout-Kontexten - ein
  bestaetigter 5m-Close zurueck in die Range (oberhalb der Invalidation).
- **Session**: Ende der erlaubten Session folgt der expliziten Policy
  (`EXPIRE` Default | `PAUSE` | `TECHNICAL_EXIT`); vor dem Entry degradiert
  `TECHNICAL_EXIT` konservativ zu `EXPIRE`. Crypto bleibt eigene
  24/7-Session-Familie.
- **Datenqualitaet**: Degradierung startet eine Grace-Periode
  (`SIGNAL_LIFECYCLE_DATA_DEGRADED_GRACE_SECONDS`, Default 120 s); danach
  endet das Signal als `DATA_INVALID` - nie als Exit-Fill. Stale Daten
  bestaetigen waehrenddessen weder Entry noch Target noch Invalidation.
- **Plan/Candidate**: Supersede/Expiry/Data-Invalid des Plans folgen einer
  klaren Zuordnung; nach dem Entry endet ein nicht mehr eligibler Plan als
  `DATA_INVALID` (dokumentierter Default, nie "Position geschlossen").
  Candidate-Expiry NACH dem Entry allein ist dokumentiert aktionslos
  (das Plan-/Signal-Expiry-Fenster regiert).
- **Expiry**: Signal-Expiry (= Plan-Fenster), maximale Entry-Wartezeit und
  maximale Forschungsdauer; Warnung als `EXPIRY_WARNING`-Update ab
  ≤ 300 s Restzeit; post-entry Policy `EXPIRE` (Default) oder
  `TECHNICAL_EXIT`.
- **Supersede** (`STRICT`): ein neuerer eligibler Plan im selben Kontext
  supersedet deterministisch; ein Richtungswechsel erzeugt NIE automatisch
  ein Gegen-Signal - der neue Kontext muss selbst durch die Admission und
  erhaelt eine eigene Signal-ID.

## Deterministische Ereignis-Prioritaet

Pro Zyklus und Signal gewinnt genau EIN finaler Zustand ueber die streng
distinkte Prioritaetskarte (konfigurierbar, validiert):

`DATA_INVALID (100) > INVALIDATED (90) > SUPERSEDED (80) > EXPIRED (70) >
PAUSED (65) > TECHNICAL_EXIT (60) > TARGET_2 (50) > TARGET_1 (40) >
ENTRY_CONFIRMED (30) > INFO (10)`

Ein Target kann eine gleichzeitig bestaetigte Invalidation nie ueberstimmen.
Ueberstimmte Beobachtungen gehen nicht verloren - sie werden als Updates/
Metadaten festgehalten. Der Validator erzwingt: exakt diese Schluessel,
streng distinkte Werte, `DATA_INVALID` an der Spitze, `INVALIDATED` ueber
`TARGET_2_REACHED`.

## SignalUpdates (Beobachtungs-Stream)

15 Update-Typen (`ADMITTED` … `LIFECYCLE_ERROR`), jede Meldung mit Praefix
"Research Lifecycle:". Wiederholte gleiche Updates innerhalb des
Dedupe-Fensters werden aggregiert (Idempotenzschluessel gebuckelt nach
Fenster), nicht dupliziert.

## Robustheit

- **Dedupe**: `active_key`-Unique-Constraint - hoechstens ein nicht-terminales
  Signal je Plan/Config-Kontext, auch unter Races. Terminale Signale geben
  den Slot frei; Fortsetzung nur als neues Signal.
- **Optimistic Locking**: jede Transition traegt `expected_state_version`
  (`UPDATE … WHERE state_version = expected AND state = from`); ein
  verlorenes Rennen ergibt `CONFLICT` (aggregiert als
  `STATE_VERSION_CONFLICT`), der Zustand des Gewinners bleibt bestehen.
- **Idempotente Events**: Idempotenzschluessel =
  `sha256(signal_id|event_type|to_state|expected_version)`; Wiederholungen
  nach Crash/Retry erzeugen nie doppelte Events.
- **Leases**: Worker claimen ein Signal vor der Auswertung
  (`lease_owner`/`lease_expires_at`); abgelaufene Leases sind reclaimebar
  (Crash-Recovery). Fremde frische Leases werden respektiert.
- **Restart-Recovery**: Zustand lebt ausschliesslich in PostgreSQL;
  `non_terminal()` nimmt nach Neustart alles wieder auf, ohne Events zu
  duplizieren.
- **DB-Ausfall**: Subsystem wird `DEGRADED`; eine nicht persistierte
  Transition wird NIE als erfolgreich gemeldet. Redis wird in Phase 10
  nicht benoetigt.
- **PAUSED-Modus (global)**: keine Admissions, keine Entry-Bestaetigungen;
  Abschluss-/Housekeeping-Monitore laufen weiter.

## Persistenz (Alembic `0006`)

| Tabelle | Inhalt |
| --- | --- |
| `signal_lifecycles` | aktueller Zustand je Signal (immutable Referenzwerte, `state_version`, Lease, `active_key`, Versionen/Hashes, Monitoring-Felder) |
| `signal_lifecycle_events` | unveraenderlicher Audit-Trail (Idempotenz-Unique, `from/to`, Prioritaet, Detail, Schema-Version) |
| `signal_updates` | aggregierter Beobachtungs-Stream (Idempotenz-Unique, Zaehler, first/last) |
| `signal_lifecycle_rejections` | aggregierte Admission-/Laufzeit-Ablehnungen (Kontext+Code+Zeitfenster+Modellversion unique) |

Jedes Signal traegt `lifecycle_model_name/version`, `lifecycle_config_hash`
sowie die Strategie-/Risk-/Cost-/Fee-Versionen des zugrunde liegenden Plans -
jede Entscheidung ist reproduzierbar zuordenbar.

## API & Dashboard

- `/health`: Subsystemstatus (`HEALTHY/DEGRADED/UNAVAILABLE/DISABLED`),
  Zustandszaehler, Ablehnungen der letzten Stunde.
- `/status`: Zustaende, Zeitstempel, Modell-/Schema-Versionen, letzte
  Zyklus-Zusammenfassung - **ohne jegliche Preisniveaus** (keine Entry-,
  Invalidations-, Target-Werte).
- `/dashboard` (lokal): vollstaendige Detailsicht inkl. Events, Updates und
  Modell-Referenzwerten, klar beschriftet als "Interne Modell-Referenzwerte -
  keine Handelsanweisung", plus serialisierte State-Machine. Disclaimer
  ueberall: *"Internal Research Lifecycle - kein Handelssignal, keine reale
  Position."*

## Metriken (Praefix `signal.`)

`signal.admissions`, `signal.admission_rejections.<code>`,
`signal.transitions`, `signal.transitions.<state>`,
`signal.terminal.<state>`, `signal.state_version_conflicts`,
`signal.invalid_transitions_suppressed`, `signal.updates`,
`signal.updates_aggregated`, `signal.monitor_cycles`,
`signal.monitor_cycle_duration_seconds`, `signal.monitor_failures`,
`signal.transition_persist_failures`, `signal.lease_contention`,
`signal.active_count` (Gauge), `signal.expired_leases` (Gauge),
`signal.overlap_skipped`, `signal.admission_paused_suppressed`.

## Konfiguration

Alle Schluessel mit Praefix `SIGNAL_` (siehe `.env.example`). Harte Regel:
`SIGNAL_LIFECYCLE_TELEGRAM_OUTPUT_ENABLED=false` - der Konfig-Validator
lehnt `true` mit einem Startabbruch ab. Phase 10 sendet keinerlei
Telegram-Nachrichten; ein spaeterer Output-Kanal erfordert eine eigene,
explizit freigegebene Phase.

## Was Phase 10 explizit NICHT tut

- Keine Orders, keine Fills, keine Positionen, keine Wallets, keine
  Signierung, keine privaten Account-/Balance-/Positions-/Orderdaten.
- Keine Telegram-Ausgabe (harte Konfigurationsregel).
- Keine neuen Strategie-/Level-Berechnungen - nur die persistierten
  Referenzwerte des Plans werden ueberwacht.
- Keine Performance-, Gewinn- oder Trefferquoten-Aussagen.
- Kein automatischer Richtungswechsel und keine Reaktivierung terminaler
  Signale.

## Uebergabe an Phase 11

Der Lifecycle ist der einzige Eingang der hypothetischen Simulation
(Phase 11): Shadow Mode uebernimmt nur Lifecycles mit bestaetigtem Entry
und modelliert daraus einen zeitverzoegerten Follower; die
Exit-Zeitpunkte stammen ausschliesslich aus den hier beschriebenen
Lifecycle-Ereignissen. Phase 11 veraendert keine Lifecycle-Regel und
erzeugt keine Zustaende - sie beobachtet nur. Details:
[`docs/simulation-and-backtesting.md`](simulation-and-backtesting.md).

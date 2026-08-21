# Strategie

Stand: Phase 8 (Strategy Research Foundation) implementiert.
Phase 8 erzeugt ausschliesslich **interne Research-Artefakte**
(`SetupCandidate` / `SetupRejection`) - **keine Trade-Signale**, keine
Telegram-Ausgabe, keine Entry-/Stop-/Target-/Hebel-/Positionsgroessen- oder
Kostenberechnung. Jede API-Darstellung traegt den Hinweis:
*"Research-Ausgabe. Kein Trade-Signal. Keine Renditeprognose."*

## Verbindliche Grundsaetze (ab Tag 1)

- Vollstaendig regelbasiert, messbar, testbar. **Kein LLM** entscheidet je ueber
  Long/Short, Entry, Stop, Exit oder Hebel.
- `NO_TRADE` ist eine korrekte Entscheidung; jede Ablehnung wird mit
  strukturierten Gruenden persistiert (aggregiert in `setup_rejections`).
- Ein hoher Score ueberschreibt niemals Session-, Datenqualitaets- oder
  Liquiditaetsregeln - harte Gates kommen immer zuerst.
- Kein Martingale, kein Averaging Down; ein Stop wird nie vom Entry weg
  verschoben (relevant ab Phase 9 - Phase 8 kennt gar keine Stops).
- Equity- und Crypto-Perps bleiben getrennte Strategiefamilien; Phase 8
  arbeitet nur auf Instrumenten mit `WATCHLIST_ACTIVE`.

## Research vs. Trade-Signal

| | Phase 8 (Research) | spaeter (Signal) |
|---|---|---|
| Artefakt | `SetupCandidate`, `SetupRejection` | Signal mit Entry/Stop/Target |
| Richtung | `BULLISH`/`BEARISH` als **Strukturklassifikation** | Handelsrichtung |
| Empfaenger | nur DB + interne API-Sektionen | Telegram-Gruppe |
| Zahlenwerte | Score 0-100, Konfidenz, referenzierte Level | Preis-/Risikoparameter |

Ein `SetupCandidate` traegt bewusst **keinerlei Trade-Parameter** - kein
Entry, kein Stop, kein Target, kein Hebel, keine Positionsgroesse, keine
Kosten. Das wird testseitig erzwungen (AST-Isolationstest + Feldnamen-Check
in `tests/test_isolation.py` und `tests/test_strategy_engine.py`).

## Versionierung & Reproduzierbarkeit

Jede Bewertung, jeder Snapshot, jede Regime-Zeile, jeder Kandidat und jede
Ablehnung referenziert:

- `strategy_name` (`market_structure_v1`) und `strategy_version` (`1.0.0`),
- `feature_schema_version` (`fs-1`),
- `config_hash` = SHA-256 ueber die kanonisch serialisierte, vollstaendige
  `StrategySettings`-Konfiguration,
- `ruleset_hash` ueber die Regel-Identitaet (`app/strategy/version.py`).

Regel- oder Parameteraenderungen erfordern eine neue Version; alte
Entscheidungen werden nie umgeschrieben. Zusaetzlich speichert jeder Kandidat
und jeder Feature-Snapshot die exakten Candle-Fenster
(`candle_window_metadata`: erster/letzter Timestamp + Anzahl pro Timeframe),
sodass jede Entscheidung nachtraeglich reproduzierbar ist.

## Anti-Look-Ahead (Candle-Disziplin)

`CandleSeries.build()` (`app/strategy/models.py`) ist das einzige Eingangstor
fuer Zeitreihen und erzwingt:

- Nur Candles mit `close_time <= as_of` - **offene Candles werden nie
  ausgewertet** (`OPEN_CANDLE_ONLY`).
- Strikte Sortierung + Deduplizierung (letzte Revision einer laufenden Candle
  gewinnt, aber nur bis Close).
- Gap-Erkennung: Abstand > `timeframe * STRATEGY_MAX_CANDLE_GAP_MULTIPLIER`
  => `CANDLE_GAP`, keine Bewertung, keine Interpolation.
- Mindesthistorie pro Timeframe (`STRATEGY_MIN_CANDLES_*`), Staleness-Check
  der letzten Candle. Zu wenig/zu alte Daten => `INSUFFICIENT_CANDLE_HISTORY`.

Der Evaluations-Job wertet ein Instrument zusaetzlich nur aus, wenn seit der
letzten Bewertung eine **neue geschlossene 5m-Candle** existiert (Trigger
"5m-Close", nie "Preis-Tick").

## Feature-Pipeline (deterministisch, `app/strategy/feature_pipeline.py`)

Pro Bewertung und Timeframe (1h/15m/5m, 1m optional):

1. **Swings** (`swing_detection.py`): Pivot-Hochs/-Tiefs mit `left/right`
   Bars, ATR-Mindestabstand; ein Pivot ist erst nach `right_bars`
   **geschlossenen** Folgecandles bestaetigt.
2. **Struktur** (`market_structure.py`): HH/HL/LH/LL/EQH/EQL-Relationen,
   BOS/ChoCh nur mit bestaetigten Closes jenseits des Referenz-Swings
   (`STRATEGY_STRUCTURE_BREAK_CONFIRMATION_CLOSES`) - **nie Wick-only**.
   Zustand: BULLISH / BEARISH / NEUTRAL / TRANSITIONAL /
   INSUFFICIENT_STRUCTURE.
3. **Liquiditaets-Pools** (`liquidity_pools.py`): Swing-Highs/-Lows, Equal
   Highs/Lows (Toleranz in bps), Range-Extreme (Rolling High/Low) mit
   Relevanz-Gewichtung.
4. **Sweeps** (`liquidity_sweeps.py`): Intrabar-Move ueber ein Level
   (`STRATEGY_SWEEP_MIN_OVERSHOOT_BPS`) + definierte Reaktion (Close zurueck).
5. **Reclaim/Rejection/Retest** (`reclaim_rejection.py`, `retest.py`):
   ausschliesslich Close-bestaetigt; Retest-Fenster in 5m-Candles.
6. **Volatilitaet** (`volatility.py`): ATR, True-Range-Perzentil (Midrank
   gegen die eigene Historie), Kompression/Expansion.
7. **Momentum/Volumen** (`momentum.py`, `volume_confirmation.py`): Rate of
   Change, Continuation-Zaehlung, RSI (nur Bestaetigung, nie Standalone),
   relatives Volumen gegen den eigenen Baseline-Durchschnitt.
8. **Orderbuch/Marktkontext** (`orderbook_features.py`, `market_context.py`):
   Spread, Depth-Imbalance, Mark/Index/Mid-Abweichung, Funding als Kontext.

Fehlende Daten werden **nie geschaetzt**: jede Feature-Gruppe traegt ein
explizites Validity-Flag; Pflicht-Features fehlen => `INSUFFICIENT_DATA`.

### Candle-basierte Intrabar-Grenzen (dokumentierte Limitation)

Auf Candle-Aufloesung ist die Intrabar-Reihenfolge nicht beobachtbar (z. B.
"erst Sweep, dann Reclaim" innerhalb derselben Candle). Sweep-Erkennung ist
daher **candle-approximiert** und traegt eine entsprechend gedeckelte
Konfidenz (max. 0.9, Detail-Text kennzeichnet die Approximation). Kein
Feature behauptet Tick-Genauigkeit.

## Regime-Engine (`app/strategy/regime.py`)

Genau ein primaeres Regime pro Instrument und Bewertung, mit Konfidenz,
Gruenden und verwendeten Features. Prioritaet:

1. `INSUFFICIENT_DATA` (Pflicht-Features fehlen)
2. `EVENT_RISK` (nur manuell konfigurierte Zeitfenster, Default aus)
3. `LOW_LIQUIDITY` (aus Selektions-/Datenqualitaet, nie aus Preisrichtung)
4. `HIGH_VOLATILITY` (True-Range-Perzentil >= Schwelle)
5. `BREAKOUT` (letzter **Close** ausserhalb der vorherigen Range - nie Wick)
6. `TREND_UP`/`TREND_DOWN` (Kaufman Efficiency Ratio + Driftrichtung;
   Widerspruch zu bestaetigter Gegenstruktur => `NO_TRADE`)
7. `RANGE` (niedrige Efficiency Ratio)
8. sonst `NO_TRADE` (unklar ist ein gueltiges Ergebnis)

Regime-Wechsel werden versioniert in `market_regimes` historisiert (nur bei
Aenderung - idempotente Zyklen erzeugen keine Duplikate).

## Setup-Kandidaten (sechs Klassen)

| Klasse | Kontext | Kernbedingungen |
|---|---|---|
| `BULLISH_SWEEP_REVERSAL` | Liquiditaet unter Level abgeraeumt | bestaetigter Sweep unter ein 15m-Level + Close-Reclaim + Retest nicht FAILED |
| `BEARISH_SWEEP_REVERSAL` | Liquiditaet ueber Level abgeraeumt | spiegelbildlich |
| `BULLISH_RECLAIM_CONTINUATION` | Trend-Fortsetzung long | 1h-Bias + 15m-BULLISH + bestaetigter 15m-BOS nach oben + 5m-Retest |
| `BEARISH_REJECTION_CONTINUATION` | Trend-Fortsetzung short | 1h-Bias + 15m-BEARISH + Close-bestaetigte Rejection am letzten 15m-Swing-High |
| `RANGE_BREAKOUT_UP` | Range/Breakout-Regime | bestaetigter Close ueber Range-High + Retest nicht FAILED |
| `RANGE_BREAKOUT_DOWN` | Range/Breakout-Regime | bestaetigter Close unter Range-Low + Retest nicht FAILED |

Pro Richtung ueberlebt nur der beste Kandidat einer Bewertung
(deterministische Sortierung nach Score, dann Typ).

## Scoring (0-100, konfigurierbar)

Gewichte (`STRATEGY_SCORE_WEIGHTS_JSON`, Summe muss exakt 100 sein - sonst
Startabbruch): `htf_bias` 20, `structure_15m` 20, `liquidity_event` 15,
`local_confirmation_5m` 15, `momentum` 10, `volume` 10, `market_context` 5,
`volatility_fit` 5. Jede Komponente liefert eine Fraktion 0..1 **plus
Begruendungstext**; der Kandidat speichert die komplette Zerlegung
(`score_components`). Mindestscore: `STRATEGY_MIN_SETUP_SCORE` (Default 75).

Harte Zusatz-Gates nach den Regeln (Konfiguration, nie Score-Kompensation):

- Momentum-Bestaetigung Pflicht (`STRATEGY_REQUIRE_MOMENTUM_CONFIRMATION`,
  Default an) => sonst `MOMENTUM_INSUFFICIENT`.
- Volumen-Bestaetigung optional (`STRATEGY_REQUIRE_VOLUME_CONFIRMATION`,
  Default aus) => sonst `VOLUME_INSUFFICIENT`.
- 1h **und** 15m gleichzeitig gegen den Kandidaten =>
  `HIGHER_TIMEFRAME_CONFLICT`.

## Kandidaten-Lifecycle & Dedupe

Zustaende: `DETECTED` -> `CONFIRMED` (zusaetzliche Bestaetigung, z. B.
Retest) -> terminal `REJECTED` / `EXPIRED` / `SUPERSEDED`.

- **Dedupe-Key**: SHA-256 ueber Instrument + Typ + Richtung + Referenzlevel +
  Timeframe + Strategieversion. In der DB haelt `active_key`
  (= Dedupe-Key solange aktiv, `NULL` sobald terminal) per Unique-Constraint
  auch unter Race-Bedingungen maximal einen aktiven Kandidaten pro Setup.
- Erneutes Auftreten innerhalb `STRATEGY_CANDIDATE_DEDUPE_SECONDS` nach
  terminalem Zustand wird unterdrueckt.
- `EXPIRED`: keine Bestaetigung innerhalb
  `STRATEGY_EXPIRE_AFTER_5M_CANDLES` geschlossener 5m-Candles, oder die
  Datenvalidierung endet (Gap/Quality) - dann werden aktive Kandidaten des
  Instruments sofort expired.
- `SUPERSEDED`: ein staerkerer **bestaetigter** Kandidat der Gegenrichtung
  loest einen schwaecheren aktiven ab.
- Obergrenze aktiver Kandidaten pro Instrument:
  `STRATEGY_MAX_ACTIVE_CANDIDATES_PER_INSTRUMENT`.
- Jede Transition erzeugt eine unveraenderliche Event-Zeile
  (`setup_candidate_events`) - Historie wird nie umgeschrieben.

## Ablehnungen (SetupRejection)

Strukturierte Codes (Auszug): `NOT_ON_ACTIVE_WATCHLIST`,
`SESSION_NOT_ALLOWED`, `BOT_PAUSED`, `INSUFFICIENT_CANDLE_HISTORY`,
`CANDLE_GAP`, `OPEN_CANDLE_ONLY`, `DATA_QUALITY_NOT_SUFFICIENT`,
`ORDERBOOK_NOT_FRESH`, `REGIME_*`, `HIGHER_TIMEFRAME_CONFLICT`,
`STRUCTURE_NOT_CONFIRMED`, `SWEEP_UNCONFIRMED`, `RECLAIM_UNCONFIRMED`,
`REJECTION_UNCONFIRMED`, `MOMENTUM_INSUFFICIENT`, `VOLUME_INSUFFICIENT`,
`SETUP_SCORE_BELOW_THRESHOLD`. Wiederholte identische Ablehnungen werden pro
(Instrument, Code, Kandidatentyp, Zeitfenster) **aggregiert** (Count +
first/last) - die DB flutet nie mit Pro-Candle-Zeilen.

## Scheduling & Fehlerverhalten

- `StrategyEvaluationJob` laeuft alle `STRATEGY_EVALUATION_REFRESH_SECONDS`
  (Default 20 s) mit Overlap-Lock und begrenzter Parallelitaet
  (`STRATEGY_MAX_CONCURRENT_EVALUATIONS`); Watchlist-/Universumswechsel
  triggern sofort. Nur DB-Candles + gecachte Marktdaten - keine REST-Calls.
- Globaler PAUSED-Modus: keine neuen Kandidaten (`BOT_PAUSED`-Rejection),
  bestehende laufen regulaer aus.
- DB-Ausfall: Subsystem DEGRADED, unpersistierte Kandidaten werden nie als
  erfolgreich gemeldet; Feed und API laufen weiter.

## Konfiguration

Alle Parameter unter `STRATEGY_*` in `.env.example` (Timeframes,
Mindesthistorie, Swing-/Struktur-/Sweep-/Retest-Parameter, Score-Gewichte,
Regime-Regeln, Dedupe/Expiry, Event-Risk-Fenster). Ungueltige Kombinationen
(Gewichtssumme != 100, fehlende Pflicht-Timeframes, Perzentile ausserhalb
0-100) brechen den Start ab - niemals stillschweigend gelockerte Regeln.

## Monitoring

`/health` -> `strategy` (Subsystem-State, Job-Liveness, aktive Kandidaten,
Evaluationszaehler); `/status` -> Strategiename/-version, `config_hash`,
letzte Runs, Regime + Struktur + letzte Rejections pro Instrument;
`/dashboard` -> Kandidaten-Zaehler nach Zustand, letzte Kandidaten mit
Score-Zerlegung und Begruendung, letzte aggregierte Ablehnungen - immer mit
Research-Disclaimer. Metriken u. a.: `strategy.evaluations_total`,
`strategy.setup_candidates_detected/confirmed/expired/superseded`,
`strategy.rejections.*`, `strategy.regime.*`,
`strategy.feature_data_gap_count`, `strategy.persistence_failures`.

# Risk- & Kosten-Engine (Phase 9)

Stand: Phase 9 implementiert.

> **Risk Research - kein Handelssignal.** Phase 9 erzeugt ausschliesslich
> interne `SignalEligibilityPlan`- und `RiskPlanRejection`-Artefakte. Kein
> Plan ist eine Order, eine Handelsempfehlung, eine Telegram-Nachricht oder
> eine Garantie. Es existiert keinerlei Kenntnis realer Konto-, Margin-
> oder Positionsdaten - alle Betraege sind hypothetische Referenzwerte.

## Abgrenzung: Eligibility Plan vs. Handelssignal

| | SignalEligibilityPlan (Phase 9) | Handelssignal (spaeter) |
|---|---|---|
| Zweck | technische Risiko-/Kosten-Eignungsbewertung eines Research-Candidates | Nachricht an die Telegram-Gruppe |
| Preise | interne Modell-Referenzwerte (nur lokal im Dashboard, klar markiert) | konkrete Signalparameter |
| Kontobasis | virtuelles Referenzkonto aus Konfiguration | - |
| Garantie | keine - konservative Modellpruefung | keine |

Telegram-Trade-Signale existieren in Phase 9 nicht; `/status` gibt keine
Entry-/Invalidation-/Target-/Mengen- oder Hebelpreise aus.

## Datenfluss

```text
Phase 8 CONFIRMED SetupCandidate
        v
RiskPlanEvaluationContextBuilder  (immutable Snapshots: Instrument-Metadaten,
        v                          Book/BBO, Funding, Levels, ATR, Fee Schedule)
evaluate_candidate (purer Kern)
  1. harte Gates (Pause, Watchlist, Session, Candidate-Status, Instrument,
     Daten, Orderbuch, Fee Schedule, Equity-Overnight)
  2. Reference Entry -> technische Invalidation -> Risikodistanz -> Targets
  3. Referenz-Sizing -> Leverage-Eignung + Margin-Modell + Liquidation Buffer
  4. Kosten-Engine (Fees, Slippage/VWAP, Funding, Netto-RR)
  5. harte Kosten-Gates -> Eligibility Score -> Plan oder Rejection
        v
risk_plans / risk_plan_events / risk_plan_rejections / cost_estimates /
instrument_risk_snapshots (PostgreSQL, versioniert, immutable Historie)
```

## Technische Invalidation & Referenz-Entry/-Targets

- **Invalidation**: strukturell unterhalb (bullish) bzw. oberhalb (bearish)
  des bestaetigten Candidate-Levels (Sweep-/Reclaim-/Struktur-/Range-Level),
  mit Sicherheitsbuffer aus `RISK_INVALIDATION_BUFFER_BPS` **plus**
  `RISK_INVALIDATION_BUFFER_ATR_MULTIPLE` x ATR(5m); tick-gerundet vom
  Entry weg. Nie das "letzte Candle-Low", nie unbestaetigte Pivots, nie
  ohne ATR-Kontext. Zu nah (< min bps / < min ATR-Multiple: normales
  Rauschen) oder zu weit (unverhaeltnismaessig) => Rejection. Dies ist eine
  interne Research-Risikodefinition, keine Stop-Empfehlung.
- **Reference Entry Zone**: am Candidate-Level verankert, konservativ
  Taker-orientiert bepreist (bullish: Ask, bearish: Bid) aus frischen
  BBO-Daten. Laeuft der Preis mehr als `RISK_MAX_ENTRY_CHASE_BPS` von der
  technischen Zone weg oder ist der Spread unbrauchbar => Rejection.
- **Reference Targets**: ausschliesslich vorhandene bestaetigte technische
  Level (Liquidity Pools, Swings, Range Boundaries, Equal-Level-Cluster)
  auf der profitablen Seite; niemals kuenstlich erzeugt, um ein CRV zu
  erreichen. Kein belastbares Level => Rejection.

## Virtuelles Referenzkonto & Sizing

```text
reference_cash_risk = RISK_VIRTUAL_REFERENCE_ACCOUNT_PUSD
                      * RISK_REFERENCE_RISK_PER_PLAN_PCT / 100
risk_per_unit       = |entry_reference - invalidation|
reference_quantity  = reference_cash_risk / risk_per_unit   (immer ABgerundet)
reference_notional  = reference_quantity * entry_reference
```

Nach dem Rounding wird das Risiko erneut geprueft (Rounding erhoeht nie das
Risiko). Verletzt das Boersen-Mindestnotional das Risikobudget, wird
abgelehnt - nie aufgestockt. `RISK_MAX_REFERENCE_NOTIONAL_PUSD` deckelt die
Referenzgroesse. Alles ist eine hypothetische Rechenreferenz.

## Leverage-Eignung (kein "optimaler Hebel")

Die Engine berechnet einen konservativen `LeverageSuitabilityRange`:
harter V1-Deckel **3x** (Konfiguration kann ihn nur senken), zusaetzlich
gedeckelt durch Instrument-Max-Leverage, Notional-Risk-Tier und die
Initial-Margin-Schranke; reduziert bei erhoehter Volatilitaet (ATR-Perzentil
>= 75), weitem Spread oder duennen Funding-Daten. Gewaehlt wird der
groesste Hebel <= Deckel, dessen Liquidationspuffer haelt - sonst Rejection.
Der Candidate-Score beeinflusst den Hebel nie.

## Margin-Modell & Liquidationspuffer (Grenzen!)

- Nur **hypothetisches Isolated-Margin-Research**; Cross Margin wird nicht
  modelliert.
- Ohne oeffentliche Initial-/Maintenance-Margin-Daten macht das Modell
  **keine Liquidationspreis-Aussage** und blockiert
  (`MARGIN_MODEL_UNAVAILABLE`). Ein approximiertes Modell existiert nur
  hinter `RISK_ALLOW_APPROXIMATED_MARGIN_MODEL=true` und markiert jeden
  Plan prominent mit `APPROXIMATED_MARGIN_MODEL`.
- Referenz ist der **Mark Price** (nie allein der Last Price); die
  Liquidationsschwelle nimmt den konservativeren Wert aus Entry- und
  Mark-Verankerung, verschoben um `RISK_MARGIN_STRESS_BUFFER_BPS` zum
  Entry hin.
- Der Liquidationspuffer verlangt, dass die technische Invalidation
  mindestens `RISK_MIN_LIQUIDATION_BUFFER_BPS` **und**
  `RISK_MIN_LIQUIDATION_BUFFER_ATR_MULTIPLE` x ATR vor der hypothetischen
  Liquidationsschwelle liegt. Er ist eine konservative Modellpruefung -
  **kein Garant gegen Liquidation**.

## Kosten-Engine

Verpflichtend fuer jeden Plan, alle Rechnungen auf **Notionalbasis**:

1. **Fee Schedule** (`fee_schedule_versions`): versionierte, administrierte
   Annahme (Seed aus Konfiguration; Default Maker 0.02 % / Taker 0.07 %,
   als Dezimalbrueche konfiguriert). Versionen sind immutable; eine
   Aenderung erzeugt eine neue Version und damit neue Plaene. **Die Saetze
   sind Annahmen und muessen vor Live-Betrieb gegen die offizielle
   Polymarket-Dokumentation validiert werden.** Ohne aktive Schedule:
   Rejection.
2. **Execution Assumptions** (versioniert, `taker-conservative-1`): Entry
   Taker, Reference Target Taker, Invalidation `STOP_STRESS_TAKER` mit
   zusaetzlichem Stress-Buffer. Maker-Annahmen nur per expliziter
   Konfiguration.
3. **Orderbuch-Slippage/VWAP**: Long-Entry laeuft die Ask-Seite, Long-Exit
   die Bid-Seite (Short spiegelbildlich) durch ein frisches Buch, begrenzt
   auf `COST_ORDERBOOK_MAX_LEVELS` und `COST_ORDERBOOK_MAX_DISTANCE_BPS`.
   Die relative Tiefen-Penalty plus Stress-Buffer wird auf den
   Referenzpreis des jeweiligen Legs angewandt - nie Mid-Price-Idealismus,
   nie eine Gutschrift. Nicht voll ausfuehrbare Referenzmengen => Rejection.
4. **Funding-Projektion**: konservativ max(aktuelle adverse Rate,
   `COST_FUNDING_CONSERVATIVE_PERCENTILE` der adversen Historie) ueber die
   technische Halteannahme (`COST_REFERENCE_HOLD_MINUTES`, keine Prognose),
   Intervalle aufgerundet, `next_funding_at` beruecksichtigt. Positive
   Rate kostet Longs, negative kostet Shorts; **guenstiges Funding wird nie
   als Ertrag eingeplant** (Floor 0). Fehlende Daten blockieren
   standardmaessig; nur kurze Intraday-Horizonte duerfen per Policy mit
   klar gekennzeichnetem Puffer modelliert werden.
   Equity-Overnight-Plaene sind standardmaessig blockiert.
5. **Netto-Erwartung**: `gross` = reine technische Distanz;
   `net` = gross -/+ Entry-/Exit-Fees, Entry-/Exit-Slippage, erwartete
   Funding-Kosten und konservativer Kostenpuffer (jede Reibung exakt
   einmal). `net_rr = net_target_pnl / net_invalidation_loss` pro
   Target-Stufe; massgeblich ist das erste (naechste) Target.

Harte Kosten-Gates (Score kann sie nie ueberstimmen):
`NET_TARGET_NON_POSITIVE`, `NET_RR_BELOW_THRESHOLD` (Default-Minimum
`RISK_MIN_NET_RR=1.8`), `COST_TO_RISK_EXCESSIVE` (Default max. 15 % des
technischen Risikos), `FUNDING_COST_EXCESSIVE`, `SLIPPAGE_EXCESSIVE`,
`ORDERBOOK_DEPTH_INSUFFICIENT`.

## Eligibility Score (0-100, getrennt vom Setup-Score)

Gewichte (`RISK_ELIGIBILITY_SCORE_WEIGHTS_JSON`, Summe exakt 100):
Invalidation-Qualitaet 20, Target-Qualitaet 15, Netto-CRV 25,
Ausfuehrbarkeit 15, Kosten-Qualitaet 10, Margin-/Liquidationspuffer 10,
Daten-/Modellvertrauen 5. Der Score ist **keine Gewinnwahrscheinlichkeit**;
harte Gates ueberstimmen ihn immer, und ein hoher Score ersetzt nie
fehlende Daten. Mindestscore: `RISK_ELIGIBILITY_MIN_SCORE` (Default 75).

## Plan-Lifecycle, Versionierung & Dedupe

- Zustaende: `ELIGIBLE` -> terminal `INELIGIBLE` / `EXPIRED` /
  `SUPERSEDED` / `DATA_INVALID`; unveraenderliche Historie in
  `risk_plan_events`.
- Jeder Plan/CostEstimate/jede Rejection referenziert Candidate-ID,
  Strategie-Version+Hash, Risk-/Cost-Modellversionen+Config-Hashes,
  Fee-Schedule-Version, Execution-Assumption-Version,
  Instrument-Snapshot-Version, `as_of` und die verwendeten
  Datenzeitstempel. Konfigurationsaenderungen erzeugen neue Plaene -
  bestehende Zeilen werden nie umgeschrieben.
- **Dedupe-Key** aus Candidate-ID, Modellversionen, Config-Hashes,
  Fee-Schedule-Version, Invalidation-/Target-Level und Entry-Basis
  (levelbasiert, damit Quote-Rauschen keine Plan-Flut erzeugt). Der
  `active_key`-Unique-Constraint verhindert doppelte aktive Plaene auch
  unter Races; ein materiell veraenderter Plan superseded den alten.
- Candidate laeuft ab / wird superseded / Datenvalidierung endet => der
  zugehoerige Plan wird auf `EXPIRED`/`SUPERSEDED`/`DATA_INVALID` gesetzt.
- Globaler PAUSED-Modus: keine neuen `ELIGIBLE`-Plaene (`BOT_PAUSED`),
  Historie bleibt, keine Telegram-Ausgabe.

## Scheduling & Fehlerverhalten

`RiskPlanEvaluationJob` (Default alle 20 s, Overlap-Lock, begrenzte
Parallelitaet) verarbeitet nur neue oder materiell aktualisierte
`CONFIRMED`-Candidates auf aktiver Watchlist; Universums-/Config-Wechsel
triggern sofort. DB-Ausfall => Subsystem DEGRADED, unpersistierte Plaene
werden nie als Erfolg gemeldet; Feed und API laufen weiter.

## Monitoring

`/health` -> `risk` (Risk-/Cost-Subsystem-State, Job-Liveness, aktive
Fee Schedule, Plan-Zaehler nach Status, Rejections der letzten Stunde);
`/status` -> Modellversionen, Config-Hashes, Fee-Schedule-Version
("assumption only"), letzter Run - **ohne Referenzpreise**; `/dashboard`
-> Plan-Details inkl. Score-Zerlegung, Reason Codes, Kosten-/Slippage-/
Funding-Zusammenfassung und (nur lokal, klar markiert) technischen
Modell-Referenzleveln, immer unter dem Kopf "Risk Research - kein
Handelssignal.". Metriken: `risk.plan_evaluations_total/skipped/failed`,
`risk.plans_eligible/expired/superseded`, `risk.plan_rejections.*`,
`risk.cost_estimates_created`, `risk.fee_schedule_unavailable`,
`risk.slippage_model_rejections`, `risk.funding_model_rejections`,
`risk.margin_model_rejections`, `risk.liquidation_buffer_rejections`,
`risk.net_rr_bucket.*`, `risk.cost_to_risk_bucket.*`,
`risk.plan_dedupe_suppressed`, `risk.plan_evaluation_duration_seconds`.

# Simulation & Backtesting (Phase 11)

> **Hypothetische Simulation / Shadow-Auswertung. Keine reale Ausfuehrung,
> keine reale Position und keine Garantie zukuenftiger Ergebnisse.**
>
> **Backtests verwenden historische Daten und modellierte Annahmen.
> Ergebnisse koennen durch Datenluecken, Gebuehrenannahmen, Slippage,
> Funding, Verzoegerung, Marktregimewechsel und nicht modellierte
> Ausfuehrungsfaktoren erheblich von realen Resultaten abweichen.**

Phase 11 fuehrt zwei strikt getrennte, rein hypothetische Analysemodi ein.
Beide erzeugen ausschliesslich Modellwerte: es gibt keine Order, keine
Ausfuehrung, keine Position, keine Wallet, kein Echtgeld und keine
Telegram-Ausgabe.

## Shadow Mode vs. Historical Backtest

| | Shadow Mode | Historical Backtest |
| --- | --- | --- |
| Eingang | live eintreffende, **persistierte** Phase-10-Lifecycles | historische, persistierte oeffentliche Daten |
| Zeitbasis | laufende Uhr | monotone Replay-Uhr ueber ein festes Fenster |
| Zweck | Wie haette ein **verzoegerter** Follower auf reale Lifecycles reagiert? | Wie verhalten sich die Regeln ueber einen historischen Zeitraum? |
| Ausloeser | Job-Zyklus (Default 15 s) | ausschliesslich explizit angeforderte lokale Laeufe |
| Gemeinsam | identische Delay-, Kosten-, Slippage-, Funding- und Lifecycle-Annahmen (derselbe pure Simulationskern) | |

Beide Modi sind **standardmaessig deaktiviert**
(`SHADOW_MODE_ENABLED=false`, `BACKTEST_ENABLED=false`), ebenso der Export
(`SIMULATION_EXPORT_ENABLED=false`).

## Datenfluss

```
Phasen 5-7   Datenqualitaet, Universum, Sessions, Watchlist
   |
Phase 8      Research SetupCandidate
   |
Phase 9      SignalEligibilityPlan (Referenzlevel, Referenzmenge, Kosten)
   |
Phase 10     Internal Research Lifecycle (ENTRY_CONFIRMED ... terminal)
   |
Phase 11     Follower-Delay -> modellierter Entry -> Lifecycle-Exit ->
             modellierter Exit -> Kosten/Funding -> hypothetische Metriken
   |
kein Live-Trading, keine Telegram-Trade-Ausgabe
```

## Follower-Delay-Modell

Ein hypothetischer Follower reagiert **nie** im Ereignismoment. Die
Verzoegerung startet am `ENTRY_CONFIRMED`-Zeitpunkt des Phase-10-Lifecycles:

| Modell | Verhalten |
| --- | --- |
| `FIXED_SECONDS` (V1-Default, 20 s) | konstante Verzoegerung |
| `UNIFORM_RANGE_SECONDS` | Gleichverteilung zwischen Min und Max, deterministischer Seed |
| `LOGNORMAL_DELAY` | Lognormalverteilung (mu/sigma), deterministischer Seed |
| `EVENT_TIMESTAMP_ONLY` | keine Verzoegerung (nur zur Referenzmessung) |

Zufallsmodelle leiten ihren Seed deterministisch aus Signal-ID und
State-Version ab - derselbe Lauf liefert dieselbe Verzoegerung. Persistiert
werden `event_reference_at`, `scheduled_entry_at`, das tatsaechliche
Modell-Instant, Modell und Parameter. Liegen nach Ablauf der Verzoegerung
keine frischen BBO-/Buchdaten vor, wird die Simulation abgelehnt
(`ENTRY_DELAY_DATA_UNAVAILABLE` / `ENTRY_BOOK_STALE`) - **nie** ein
idealisierter Entry zum urspruenglichen Signalpreis.

Im Backtest liegt die Kerzenaufloesung ueber der Delay-Granularitaet; wenn
der erste beobachtbare Zeitpunkt spaeter liegt als der geplante, traegt das
Ergebnis den Flag `ENTRY_INSTANT_APPROXIMATED_BY_REPLAY_CADENCE` und gilt
als `PARTIAL`.

## Book-Walk und Kosten

Modellierte Preise entstehen ausschliesslich durch einen konservativen
Book-Walk auf der **adversen** Seite:

- bullischer Entry konsumiert den Ask, bullischer Exit den Bid
- baerischer Entry konsumiert den Bid, baerischer Exit den Ask
- Tiefe wird aus dem oeffentlichen Buch entnommen (Level-/Distanzfenster
  aus der Phase-9-Konfiguration); reicht sie nicht, wird abgelehnt
- auf den VWAP kommt der konfigurierte Stress-Puffer, immer gegen die
  modellierte Position, nie als Verbesserung

Fee-, Slippage- und Funding-Logik stammen unveraendert aus der
Phase-9-Kostenengine (`app/costs/`) - Phase 11 dupliziert sie nicht.
Funding wird ueber die **tatsaechliche** modellierte Haltedauer projiziert;
fehlt es, wird je nach Policy abgelehnt oder klar als unvollstaendig
markiert - niemals still auf 0 gesetzt.

Ein verzoegerter Entry, der weiter als der konfigurierte Anteil der
technischen Risikodistanz vom Referenzpreis abweicht, wird als
`ENTRY_SLIPPAGE_EXCESSIVE` abgelehnt - Nachlaufen wird nicht modelliert.

## Exit-Gruende

Exits stammen **immer** aus Phase-10-Ereignissen, nie aus neuen Regeln:
`TARGET_1_OBSERVED`, `TARGET_2_OBSERVED`, `TECHNICAL_EXIT_CONDITION`,
`INVALIDATION_CONDITION`, `EXPIRED`, `SUPERSEDED`, `DATA_INVALID`,
`SESSION_POLICY_END`, `LIFECYCLE_TERMINAL`.

Ist der Exit nicht aus oeffentlichen Buchdaten modellierbar, gilt je nach
`SHADOW_UNMODELED_EXIT_POLICY` entweder `REJECT` oder der Status
`UNMODELED_EXIT`; letzterer zaehlt nicht als vollstaendige Simulation und
fliesst nicht in Ergebnis-Kennzahlen ein.

Eine hypothetische Teilreduktion an Referenz-Target 1 ist V1-seitig
deaktiviert und nur per explizitem Opt-in aktivierbar; sie bleibt als
Simulationsannahme gekennzeichnet.

## Kausales Replay und Look-ahead-Schutz

Die Replay-Reihenfolge ist versioniert (`rov-1`) und liegt in jedem
Manifest. Bei gleichem Zeitstempel gilt:

1. Session-/Kalender-Events
2. Instrument-/Market-Status
3. Datenqualitaets-/Buch-Updates
4. Ticker / BBO / Trades
5. **geschlossene** Kerzen
6. Strategy Evaluation
7. Risk Plan Evaluation
8. Lifecycle Monitoring
9. Simulations-/Delay-Events

Tie-Breaker innerhalb einer Kategorie: `(Symbol, Sequence)` - vollstaendig
deterministisch. Schutzmechanismen:

- die Replay-Uhr laeuft ausschliesslich vorwaerts; ein Rueckwaertssprung
  ist ein Fehler
- jeder Datenzugriff wird gegen den aktuellen Replay-Zeitpunkt geprueft;
  ein spaeter gestempelter Wert loest `LOOKAHEAD_GUARD_TRIGGERED` aus und
  bricht den Lauf ab
- eine Kerze wird erst **nach** ihrer Schlusszeit sichtbar; offene Kerzen
  bestaetigen nie etwas
- historische Regeln werden nach Betrachtung der Ergebnisse nicht
  angepasst - jede Aenderung erzeugt ein neues Manifest und einen neuen Lauf

## Datenanforderungen und Datenluecken

Vor dem Start prueft der Validator die konfigurierten Kanaele
(geschlossene Kerzen, BBO/Ticker, Orderbuch, Funding) ueber das gesamte
Fenster. Funding wird mit einer eigenen, intervallgerechten Luecken-Grenze
bewertet (Funding erscheint naturgemaess nur alle paar Stunden).

- fehlt ein Pflichtkanal: Lauf wird abgelehnt (`BACKTEST_DATA_INCOMPLETE`)
- Luecken ueber dem Limit: Ablehnung (`DATA_GAP`) oder - bei
  `BACKTEST_ALLOW_SEGMENTED_DATA=true` - Ausschluss der betroffenen
  Intervalle; der Lauf endet dann als `COMPLETED_WITH_GAPS` und
  bezeichnet sich ausdruecklich **nicht** als vollstaendiger Backtest
- ausgeschlossene Intervalle stehen im Manifest, im Run-Datensatz und im
  Report
- fehlende Daten werden nie durch idealisierte Mid-Preise ersetzt

## Lokale Eingabedaten

Neben dem repository-basierten Replay gibt es einen CSV-/JSON-Provider.
Er liest ausschliesslich Dateien unterhalb von
`BACKTEST_ALLOWED_INPUT_DIRECTORY` (repository-relativ, ohne `..`,
validiert beim Start). Schema, Zeitstempel, Duplikate und Reihenfolge
werden geprueft; fremde Projektdateien sind unerreichbar.

## Walk-Forward

Splits laufen zeitlich aufsteigend (`ROLLING` oder `EXPANDING`) mit
Train-, Validation- und Testfenster. Regeln:

- Konfigurationsauswahl **nur** aus Train- und Validation-Evidenz
- das Testfenster wird nie zur Auswahl herangezogen (defensiv gefiltert)
- V1 arbeitet mit einer explizit vorgegebenen, endlichen Kandidatenliste -
  keine freie Optimierung, kein Curve Fitting, kein ML-/RL-Training
- die Auswahl folgt einem dokumentierten, deterministischen Score aus
  Net-R (35 %), Drawdown (25 %), Datenvollstaendigkeit (20 %),
  Kostenanteil (10 %) und Stichprobengroesse (10 %); Gleichstand wird
  ueber den Konfigurationsschluessel aufgeloest - **nie** nach hoechster
  Rendite allein
- zu wenige vollstaendige Simulationen: `INSUFFICIENT_SAMPLE` statt
  Auswahl; Kandidaten und Rationale werden gespeichert

## Metriken, Drawdown und Unsicherheit

Alle Kennzahlen beschreiben hypothetische Simulationen und tragen
entsprechende Namen (`hypothetical_win_rate`, `profit_factor_hypothetical`,
`simulations_completed`, `average_net_r`, `max_drawdown_r`,
`costs_as_pct_of_risk`, `incomplete_data_rate`, `rejection_rate` u. a.).

Segmentiert wird mindestens nach Symbol, Asset-Klasse, Session-State,
Marktregime, Candidate-Typ, Research-Richtung, Setup-/Eligibility-Score-
Bucket, Strategy-/Risk-/Cost-/Fee-Version, Delay-Modell, Exit-Grund,
Datenvollstaendigkeit und Zeitraum.

Drawdown wird auf der modellierten R-Kurve berechnet; eine Prozentangabe
erscheint nur gegen das **virtuelle** Referenzkonto und nie als Aussage
ueber ein reales Konto. Ohne definiertes Referenzkonto bleibt die
R-Analyse verfuegbar, eine Prozentaussage entfaellt.

Der optionale Bootstrap laeuft nur mit festem Seed und oberhalb der
Mindeststichprobe und liefert Perzentil-Intervalle mit dem Hinweis:
*"Statistische Unsicherheitsabschaetzung auf Basis dieser hypothetischen
Stichprobe; keine Prognose zukuenftiger Ergebnisse."*

Unterhalb von `BACKTEST_MIN_COMPLETE_SIMULATIONS` gilt ein Metrik-Set als
`INSUFFICIENT_SAMPLE`: Quotenkennzahlen (Win Rate, Profit Factor,
Expectancy, Kostenanteil am Ergebnis) werden zurueckgehalten und es wird
ausdruecklich keine Aussage ueber Setup-Qualitaet getroffen.

## Experimente, Manifeste und Reproduzierbarkeit

Jeder Lauf haengt an einem **immutablen** Manifest mit Modellnamen und
-versionen, Konfigurations-Hashes (Simulation, Strategy, Risk, Cost,
Lifecycle), Fee-Schedule- und Execution-Assumption-Version, Quelle und
Datenversion, Universum, Asset-Klassen, Timeframes, Zeitfenster,
Ersteller, Seed, Replay-Ordering- und Clock-Version, Metrik- und
Disclaimer-Version sowie allen Simulationsparametern.

Aenderungen an Strategie, Risiko, Kosten, Fee Schedule, Funding-Policy,
Slippage-, Delay-Modell, Datenfiltern, Zeithorizont, Universum oder
Konfiguration erzeugen einen **neuen** Manifest-Hash und damit einen neuen
Lauf. Abgeschlossene Laeufe werden nie ueberschrieben oder geloescht.

Der Runner schreibt Checkpoints (Default alle 500 Events) und kann nach
Absturz oder Abbruch daraus fortsetzen, ohne ein Event doppelt anzuwenden.
Parallele Laeufe mit identischem Manifest-Kontext werden ueber einen
Dedupe-Key blockiert; ein Lauf kann kooperativ abgebrochen werden.

## Persistenz (Alembic `0007`)

12 Tabellen: `experiments`, `experiment_manifests`, `simulation_runs`,
`backtest_runs`, `simulated_positions`, `simulated_executions`,
`simulation_events`, `simulation_rejections`, `walk_forward_splits`,
`performance_metric_sets`, `performance_metric_values`,
`simulation_data_quality_summaries`.

## API und Dashboard

- `/health`: Shadow- und Backtest-Subsystem (`HEALTHY/DEGRADED/
  UNAVAILABLE/DISABLED`), Job-Liveness, laufende/unvollstaendige Laeufe,
  letzter Erfolg - Zahlen immer mit Disclaimer
- `/status`: aktive Simulationen nach Status, letzte Laeufe, Manifest-/
  Modellversionen, Datenvollstaendigkeitswarnungen, Sample-Status
- `/dashboard`: beide Pflicht-Disclaimer **ganz oben**, Experimente,
  Laeufe, Manifeste, hypothetische Simulationen mit Kostenaufloesung,
  Metriken inklusive Stichprobengrenze, Segmentierung, Drawdown in R bzw.
  virtuellem Referenzkonto, optionale Bootstrap-Unsicherheit,
  ausgeschlossene Datenabschnitte

Kein Endpunkt bietet Export-, Telegram- oder Trading-Aktionen an.

## Was Phase 11 explizit NICHT tut

- keine Live-Orders, keine Trading-API, keine Wallets, keine Signierung
- keine privaten Polymarket-Konto-, Balance-, Positions- oder Orderdaten
- keine automatische Ausfuehrung, kein Copy Trading
- keine automatische Parameteroptimierung auf Testdaten, kein Curve
  Fitting, kein ML-/RL-Training
- keine Rendite-, Trefferquoten- oder Profitabilitaetsgarantie
- keine Darstellung hypothetischer Ergebnisse als reale Performance
- keine nachtraegliche Aenderung historischer Ergebnisse ohne neues
  Manifest
- kein Nachkauf, kein Martingale, kein Averaging Down, kein Cross-Margin
- keine Steuer-, Waehrungs- oder Ein-/Auszahlungslogik
- keine Telegram-Trade-Ausgabe

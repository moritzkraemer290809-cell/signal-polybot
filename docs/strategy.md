# Strategie

> Phase 8–9 sind noch nicht implementiert. Dieses Dokument haelt die bereits
> verbindlichen Grundsaetze fest; die konkrete Umsetzung folgt phasenweise.

## Verbindliche Grundsaetze (ab Tag 1)

- Vollstaendig regelbasiert, messbar, testbar. **Kein LLM** entscheidet je ueber
  Long/Short, Entry, Stop, Exit oder Hebel.
- `NO_TRADE` ist eine korrekte Entscheidung; jeder abgelehnte Kandidat wird als
  `REJECTED`-Entscheidung mit strukturierten Gruenden persistiert
  (`strategy_decisions`).
- Ein hoher Score ueberschreibt niemals Kosten-, Risiko- oder
  Datenqualitaetsregeln.
- Equity- und Crypto-Perps sind getrennte Strategiefamilien mit getrennten
  Parametern.
- Kein Martingale, kein Averaging Down; ein Stop wird nie vom Entry weg
  verschoben.

## Bereits implementierte Bausteine

- Marktregime-, Rejection- und Lifecycle-Enums (`app/domain/enums.py`).
- Signal-Lifecycle-Zustandsmaschine als Datenmodell
  (`app/domain/lifecycle.py`) inkl. erlaubter Transitionen und Terminalzustaende.
- Persistenzschema fuer Regime, Entscheidungen, Scores und Kostenannahmen.

## Geplante Struktur (Phase 8)

Multi-Timeframe: 1h Regime/Bias → 15m Struktur → 5m Bestaetigung → 1m optionaler
Trigger. Scoring 0–100 mit konfigurierbarer Gewichtung, Mindestscore 75.
Details siehe Projekt-Brief; Umsetzung erst in Phase 8.

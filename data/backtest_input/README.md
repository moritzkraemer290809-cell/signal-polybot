# Lokales Backtest-Eingabeverzeichnis

Nur repository-lokale CSV-/JSON-Dateien fuer historische Replays. Der
Konfigurationsvalidator laesst ausschliesslich repository-relative Pfade
ohne `..` zu; ausserhalb dieses Verzeichnisses wird nichts gelesen.

Erwartete Spalten (CSV): `as_of`, `category`, `symbol`, optional `sequence`
plus kategorie-spezifische Felder. Alle Zeitstempel in UTC.

Hypothetische Simulation / Shadow-Auswertung. Keine reale Ausfuehrung,
keine reale Position und keine Garantie zukuenftiger Ergebnisse.

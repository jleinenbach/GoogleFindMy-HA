# Strukturierungsvorschlag: GoogleFindMy-HA

**Datum:** 2026-01-09
**Status:** Genehmigt - Bereit zur Umsetzung
**Ziel:** coordinator.py von 8.342 auf ~2.500 Zeilen reduzieren
**Branch:** `claude/review-bug-detection-KVUvG`

---

## SCHNELLÜBERSICHT FÜR FORTSETZUNG

> **Falls Kontext verloren:** Dieses Dokument enthält alle Informationen zur
> Fortsetzung der Refaktorisierung. Lese Abschnitt 5 (Phasen-Plan) und prüfe
> den Git-Log für den aktuellen Stand.

**Kernentscheidungen:**
1. **Option B: `coordinator.py` wird zum Package `coordinator/`**
2. `coordinator/main.py` enthält die Hauptklasse GoogleFindMyCoordinator
3. `coordinator/__init__.py` re-exportiert alles für Rückwärtskompatibilität
4. `coordinator/helpers/` enthält Pure Functions (von `coordinator_*.py`)
5. Operations-Klassen (Methoden mit `self`) kommen in `coordinator/*.py`
6. Externe Module (Auth/, NovaApi/, etc.) werden NICHT verändert

**Prüfkommando für aktuellen Stand:**
```bash
# Welche Phase ist abgeschlossen?
ls -la custom_components/googlefindmy/coordinator/
ls -la custom_components/googlefindmy/coordinator/helpers/

# Gibt es noch coordinator.py im Root?
ls custom_components/googlefindmy/coordinator.py 2>/dev/null

# Gibt es noch coordinator_*.py im Root?
ls custom_components/googlefindmy/coordinator_*.py 2>/dev/null

# Tests laufen?
python3.13 -m pytest tests/ -v --tb=short
```

---

## 1. Problemanalyse

### Aktuelle Situation

| Datei | Zeilen | Problem |
|-------|-------:|---------|
| coordinator.py | 8.342 | 171 Methoden, nicht vollständig lesbar |
| __init__.py | 8.313 | Ebenfalls sehr groß |
| eid_resolver.py | 2.363 | Groß, aber eigenständig funktional |

### Bestehende Helper-Module (zu verschieben nach coordinator/helpers/)

| Aktuell im Root | Zeilen | Funcs | Ziel |
|-----------------|-------:|------:|------|
| coordinator_registry.py | 742 | 16 | coordinator/helpers/registry.py |
| coordinator_cache.py | 620 | 15 | coordinator/helpers/cache.py |
| coordinator_identity.py | 410 | 10 | coordinator/helpers/identity.py |
| coordinator_subentry.py | 302 | 8 | coordinator/helpers/subentry.py |
| coordinator_update.py | 245 | 6 | coordinator/helpers/update.py |
| coordinator_stats.py | 181 | 3 | coordinator/stats.py |
| coordinator_geo.py | 163 | 4 | coordinator/helpers/geo.py |
| coordinator_polling.py | 99 | 3 | coordinator/helpers/polling.py |

---

## 2. Unverändert bleiben (externe Abhängigkeiten)

Diese Verzeichnisse stammen aus GoogleFindMyTools oder sind etablierte Strukturen:

- `Auth/` - Authentifizierung (extern)
- `NovaApi/` - Google Nova API (extern)
- `SpotApi/` - Google Spot API (extern)
- `FMDNCrypto/` - Kryptografie-Bibliothek (extern)
- `KeyBackup/` - Key-Backup-Logik (extern)
- `ProtoDecoders/` - Protobuf-Decoder (extern)

**Regel:** Keine Umbenennungen, keine Verschiebungen dieser Module.

---

## 3. Zielstruktur (Option B - Sauberes Package)

```
custom_components/googlefindmy/
├── coordinator/                 # coordinator.py wird zum PACKAGE (kein .py daneben!)
│   ├── __init__.py              # Re-exports: GoogleFindMyCoordinator + alle Operations
│   ├── main.py                  # GoogleFindMyCoordinator Hauptklasse (~2.500 Zeilen)
│   ├── registry.py              # RegistryOperations (11 Methoden, ~1.350 Zeilen)
│   ├── subentry.py              # SubentryOperations (17 Methoden, ~800 Zeilen)
│   ├── polling.py               # PollingOperations (6 Methoden, ~500 Zeilen)
│   ├── locate.py                # LocateOperations (~500 Zeilen)
│   ├── identity.py              # IdentityOperations (4 Methoden, ~770 Zeilen)
│   └── helpers/                 # Pure Functions (VERSCHOBEN von coordinator_*.py)
│       ├── __init__.py          # Re-exports aller Helper-Funktionen
│       ├── registry.py          # ← coordinator_registry.py (742 Zeilen, 16 Funcs)
│       ├── cache.py             # ← coordinator_cache.py (620 Zeilen, 15 Funcs)
│       ├── identity.py          # ← coordinator_identity.py (410 Zeilen, 10 Funcs)
│       ├── subentry.py          # ← coordinator_subentry.py (302 Zeilen, 8 Funcs)
│       ├── update.py            # ← coordinator_update.py (245 Zeilen, 6 Funcs)
│       ├── stats.py             # ← coordinator_stats.py (181 Zeilen, 3 Funcs)
│       ├── geo.py               # ← coordinator_geo.py (163 Zeilen, 4 Funcs)
│       └── polling.py           # ← coordinator_polling.py (99 Zeilen, 3 Funcs)
```

### Warum Option B?

| Aspekt | Option A (coordinator.py bleibt) | Option B (coordinator/ Package) |
|--------|----------------------------------|--------------------------------|
| Python-Konflikt | Namenskonflikt möglich (Modul vs Package) | Sauber: Ein Name = Ein Package |
| Import-Pfade | `from .coordinator import X` (Datei) | `from .coordinator import X` (Package __init__) |
| Monkeypatching | Kompliziert durch duale Pfade | Einfach: alles in __init__.py re-exportiert |
| Wartbarkeit | Zwei Konzepte mit gleichem Namen | Ein klares Package-Konzept |

### Unterschied: Operations-Klassen vs. Helpers

| Komponente | Typ | Beschreibung |
|------------|-----|--------------|
| `coordinator/*.py` | Operations-Klassen | Methoden die `self` brauchen (Zugriff auf Coordinator-State) |
| `coordinator/helpers/*.py` | Pure Functions | Zustandslose Funktionen (Input → Output, kein `self`) |

---

## 4. Auswirkungen auf coordinator.py

| Komponente | Vorher | Nachher |
|------------|-------:|--------:|
| Gesamt-Zeilen | 8.342 | ~2.500 (in main.py) |
| Methoden in Hauptklasse | 171 | ~50 |
| Operations-Module | 0 | 5 |
| Helper-Module im Root | 8 | 0 |
| Helper-Module in coordinator/helpers/ | 0 | 8 |

---

## 5. Phasen-Plan (Detailliert)

### Voraussetzungen (abgeschlossen)
- [x] 8 Helper-Module extrahiert und integriert (coordinator_*.py)
- [x] Tests für Helper-Funktionen vorhanden
- [x] Halluzinierte Funktionen entfernt
- [x] Alle Tests bestehen

### Phase 0: Package-Konversion (Risiko: Niedrig)
**Ziel:** coordinator.py → coordinator/main.py, coordinator_*.py → coordinator/helpers/*.py

**Schritte:**

1. Verzeichnis erstellen:
   ```bash
   mkdir -p custom_components/googlefindmy/coordinator/helpers
   ```

2. **coordinator.py → coordinator/main.py verschieben:**
   ```bash
   git mv custom_components/googlefindmy/coordinator.py \
          custom_components/googlefindmy/coordinator/main.py
   ```

3. **Helper-Dateien verschieben (MIT Umbenennung):**
   ```bash
   git mv custom_components/googlefindmy/coordinator_registry.py \
          custom_components/googlefindmy/coordinator/helpers/registry.py
   git mv custom_components/googlefindmy/coordinator_cache.py \
          custom_components/googlefindmy/coordinator/helpers/cache.py
   git mv custom_components/googlefindmy/coordinator_identity.py \
          custom_components/googlefindmy/coordinator/helpers/identity.py
   git mv custom_components/googlefindmy/coordinator_subentry.py \
          custom_components/googlefindmy/coordinator/helpers/subentry.py
   git mv custom_components/googlefindmy/coordinator_update.py \
          custom_components/googlefindmy/coordinator/helpers/update.py
   git mv custom_components/googlefindmy/coordinator_stats.py \
          custom_components/googlefindmy/coordinator/helpers/stats.py
   git mv custom_components/googlefindmy/coordinator_geo.py \
          custom_components/googlefindmy/coordinator/helpers/geo.py
   git mv custom_components/googlefindmy/coordinator_polling.py \
          custom_components/googlefindmy/coordinator/helpers/polling.py
   ```

4. **Interne Imports in verschobenen Helper-Dateien aktualisieren:**
   ```python
   # In helpers/cache.py - ALT:
   from .coordinator_geo import haversine_distance
   # NEU:
   from .geo import haversine_distance

   # In helpers/identity.py - ALT:
   from .coordinator_subentry import ensure_config_subentry_id
   # NEU:
   from .subentry import ensure_config_subentry_id

   # In helpers/registry.py - ALT:
   from .const import LEGACY_SERVICE_IDENTIFIER, ...
   # NEU:
   from ...const import LEGACY_SERVICE_IDENTIFIER, ...
   ```

5. **coordinator/helpers/__init__.py erstellen mit Re-exports:**
   ```python
   """Pure helper functions for GoogleFindMyCoordinator."""
   from __future__ import annotations

   from .registry import (
       extract_canonical_device_id,
       build_entity_unique_id_candidates,
       build_canonical_unique_id,
       match_entity_by_device_id,
       # ... alle 16 Funktionen + Konstanten
   )
   from .cache import (
       normalize_location_fields,
       preserve_metadata_fields,
       # ... alle 15 Funktionen
   )
   # ... weitere Module analog
   ```

6. **Imports in coordinator/main.py aktualisieren:**
   ```python
   # ALT:
   from .coordinator_registry import extract_canonical_device_id
   # NEU:
   from .helpers.registry import extract_canonical_device_id

   # ALT:
   from .api import GoogleFindMyAPI
   # NEU:
   from ..api import GoogleFindMyAPI
   ```

7. **coordinator/__init__.py erstellen (WICHTIG für Rückwärtskompatibilität):**
   ```python
   """Coordinator package for GoogleFindMy integration.

   This package contains the GoogleFindMyCoordinator class and related components.
   All public symbols are re-exported here for backwards compatibility.

   Usage (unchanged):
       from .coordinator import GoogleFindMyCoordinator
   """
   from __future__ import annotations

   # Re-export main coordinator class
   from .main import (
       GoogleFindMyCoordinator,
       DeviceIdentity,
       SemanticLabelRecord,
       SubentryMetadata,
       CacheProtocol,
       format_epoch_utc,
       normalize_epoch_seconds,
       _as_ha_attributes,
       get_recorder,
       _sync_get_last_gps_from_history,
       _FCM_FALLBACK_POLL_AFTER_S,
       _PREDICTION_BUFFER_S,
   )

   # Re-export stats classes from helpers
   from .helpers.stats import (
       ApiStatus,
       DiagnosticsBuffer,
       FcmStatus,
       StatusSnapshot,
   )

   # Re-export helpers subpackage
   from . import helpers

   # Re-exports für Test-Monkeypatching (gleiche Module wie main.py importiert)
   from ..api import GoogleFindMyAPI
   from homeassistant.helpers import device_registry as dr
   from homeassistant.helpers import entity_registry as er
   from homeassistant.helpers.event import async_call_later
   from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

   __all__ = [
       # Main class
       "GoogleFindMyCoordinator",
       "DeviceIdentity",
       "SemanticLabelRecord",
       "SubentryMetadata",
       "CacheProtocol",
       # Functions
       "format_epoch_utc",
       "normalize_epoch_seconds",
       "_as_ha_attributes",
       "get_recorder",
       # Stats
       "ApiStatus",
       "DiagnosticsBuffer",
       "FcmStatus",
       "StatusSnapshot",
       # For monkeypatching
       "GoogleFindMyAPI",
       "DataUpdateCoordinator",
       "dr",
       "er",
       "async_call_later",
       # Subpackage
       "helpers",
   ]
   ```

8. **Imports in Tests aktualisieren:**
   ```python
   # Meiste Tests: UNVERÄNDERT (dank Re-exports)!
   from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

   # Nur Helper-Tests ändern sich:
   # ALT:
   from custom_components.googlefindmy.coordinator_registry import ...
   # NEU:
   from custom_components.googlefindmy.coordinator.helpers.registry import ...
   ```

9. **Tests ausführen:**
   ```bash
   python3.13 -m pytest tests/ -v --tb=short
   ```

10. **Commit:**
    ```bash
    git add -A
    git commit -m "refactor: convert coordinator.py to coordinator/ package (Option B)"
    ```

**Erfolgskriterien Phase 0:** ✓ ABGESCHLOSSEN (Commit 20b1fa6)
- [x] coordinator.py existiert NICHT mehr im Root
- [x] coordinator/main.py enthält GoogleFindMyCoordinator
- [x] Alle 8 Helper-Dateien in coordinator/helpers/
- [x] Keine coordinator_*.py mehr im Root
- [x] `from .coordinator import GoogleFindMyCoordinator` funktioniert (via __init__.py)
- [x] Alle Tests bestehen

---

### Phase 1: Operations-Klassen erstellen (Risiko: Niedrig)
**Ziel:** Leere Operations-Klassen erstellen, Vererbung einrichten

**Schritte:**
1. Operations-Klassen erstellen (leer):
   ```python
   # coordinator/registry.py
   """Device registry operations for GoogleFindMyCoordinator."""
   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
       from .main import GoogleFindMyCoordinator

   class RegistryOperations:
       """Device registry operations (extracted methods)."""
       pass  # Methoden werden in Phase 2 hierher verschoben
   ```

2. Analog für: subentry.py, polling.py, locate.py, identity.py

3. coordinator/__init__.py aktualisieren:
   ```python
   # Zusätzlich zu bestehenden Re-exports:
   from .registry import RegistryOperations
   from .subentry import SubentryOperations
   from .polling import PollingOperations
   from .locate import LocateOperations
   from .identity import IdentityOperations
   ```

4. coordinator/main.py Klasse erweitern:
   ```python
   from .registry import RegistryOperations
   from .subentry import SubentryOperations
   from .polling import PollingOperations
   from .locate import LocateOperations
   from .identity import IdentityOperations

   class GoogleFindMyCoordinator(
       RegistryOperations,
       SubentryOperations,
       PollingOperations,
       LocateOperations,
       IdentityOperations,
       DataUpdateCoordinator,
   ):
       """Google Find My coordinator with modular organization."""
       # Alle bisherigen Methoden bleiben hier erstmal
   ```

5. Tests ausführen
6. Commit

**Erfolgskriterien Phase 1:** ✓ ABGESCHLOSSEN (Commit 63f0ec0)
- [x] 5 leere Operations-Klassen existieren
- [x] GoogleFindMyCoordinator erbt von allen 5
- [x] Alle Tests bestehen (keine Regression)

---

### Phase 2-6: Methoden-Extraktion (unverändert)

Die Phasen 2-6 bleiben identisch zur vorherigen Planung:

- **Phase 2:** RegistryOperations extrahieren (11 Methoden, ~1.682 Zeilen)
- **Phase 3:** SubentryOperations extrahieren (17 Methoden, ~766 Zeilen)
- **Phase 4:** PollingOperations extrahieren (6 Methoden, ~495 Zeilen)
- **Phase 5:** IdentityOperations extrahieren (4+ Methoden, ~776 Zeilen)
- **Phase 6:** LocateOperations extrahieren (~274+ Zeilen)

**Schritte pro Methode:**
1. Methode aus coordinator/main.py AUSSCHNEIDEN
2. In coordinator/registry.py (etc.) EINFÜGEN (in Operations-Klasse)
3. Type-Hint anpassen: `def method(self: "GoogleFindMyCoordinator", ...)`
4. Imports hinzufügen wenn nötig
5. Tests ausführen
6. Wenn Tests bestehen: Commit für diese Methode

---

### Phase 7: Cleanup (Risiko: Niedrig)
**Ziel:** Aufräumen und Dokumentation

**Schritte:**
1. Ungenutzte Imports in coordinator/main.py entfernen
2. Docstrings für alle Operations-Klassen vervollständigen
3. Test-Coverage prüfen (Ziel: >90%)
4. Diese RESTRUCTURING_PROPOSAL.md als abgeschlossen markieren

**Erfolgskriterien Phase 7:**
- [ ] coordinator/main.py bei ~2.500 Zeilen
- [ ] Keine Linter-Warnungen
- [ ] Alle Tests bestehen
- [ ] Dokumentation aktuell

---

## 6. Risiko-Bewertung

| Phase | Risiko | Begründung |
|-------|--------|------------|
| Phase 0: Package-Konversion | Niedrig | Python-saubere Struktur, Re-exports garantieren Kompatibilität |
| Phase 1: Operations-Infrastruktur | Niedrig | Leere Klassen, keine Funktionsänderung |
| Phase 2-6: Methoden-Extraktion | Mittel | Methodensignaturen bleiben gleich |
| Phase 7: Cleanup | Niedrig | Nur kosmetisch |

**Rollback-Strategie:** Git-Revert jederzeit möglich, da schrittweise Commits.

---

## 7. Test-Strategie

### Vor jeder Änderung:
```bash
python3.13 -m pytest tests/ -v --tb=short
```

### Nach jeder Methoden-Verschiebung:
```bash
# Spezifische Tests für geänderte Komponente
python3.13 -m pytest tests/test_coordinator*.py -v --tb=short

# Integrationstests
python3.13 -m pytest tests/test_integration*.py -v --tb=short
```

### Bei Fehlern:
1. SOFORT Git-Revert der letzten Änderung
2. Fehler analysieren
3. Korrigierten Ansatz versuchen

---

## 8. Import-Änderungen Referenz

### Für andere Module im Package (button.py, sensor.py, etc.):
```python
# UNVERÄNDERT dank Re-exports in coordinator/__init__.py:
from .coordinator import GoogleFindMyCoordinator
```

### Für coordinator/main.py (intern):
```python
# ALT (vor Phase 0):
from .coordinator_registry import extract_canonical_device_id
from .api import GoogleFindMyAPI

# NEU (nach Phase 0):
from .helpers.registry import extract_canonical_device_id
from ..api import GoogleFindMyAPI
```

### Für Tests:
```python
# Coordinator-Tests: UNVERÄNDERT
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

# Helper-Tests - ALT:
from custom_components.googlefindmy.coordinator_registry import (
    extract_canonical_device_id,
)

# Helper-Tests - NEU:
from custom_components.googlefindmy.coordinator.helpers.registry import (
    extract_canonical_device_id,
)
```

---

## 9. Checkliste für Fortsetzung

Wenn du diese Datei liest und den Kontext verloren hast:

1. **Aktuellen Stand prüfen:**
   ```bash
   git log --oneline -10
   ls -la custom_components/googlefindmy/coordinator/
   ls custom_components/googlefindmy/coordinator.py 2>/dev/null
   ls custom_components/googlefindmy/coordinator_*.py 2>/dev/null
   ```

2. **Tests ausführen:**
   ```bash
   python3.13 -m pytest tests/ -v --tb=short
   ```

3. **Welche Phase ist dran?**
   - Gibt es `coordinator.py` im Root? → Phase 0 noch offen
   - Gibt es `coordinator/main.py`? → Phase 0 abgeschlossen
   - Sind Operations-Klassen gefüllt? → Prüfe Zeilenzahl in coordinator/main.py

4. **Nächsten Schritt aus Phasen-Plan (Abschnitt 5) ausführen**

---

## 10. Nicht ändern

- Keine Umbenennungen von Auth/, NovaApi/, SpotApi/, FMDNCrypto/, KeyBackup/
- Keine Änderung der externen API-Strukturen
- `eid_resolver.py` bleibt im Root
- `fmdn_finder/` bleibt eigenständig

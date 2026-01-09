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
1. Option A: `coordinator.py` BLEIBT im Root
2. `coordinator/` Package enthält Operations-Klassen (Methoden mit `self`)
3. `coordinator/helpers/` enthält Pure Functions (von `coordinator_*.py`)
4. Externe Module (Auth/, NovaApi/, etc.) werden NICHT verändert

**Prüfkommando für aktuellen Stand:**
```bash
# Welche Phase ist abgeschlossen?
ls -la custom_components/googlefindmy/coordinator/
ls -la custom_components/googlefindmy/coordinator/helpers/

# Gibt es noch coordinator_*.py im Root?
ls custom_components/googlefindmy/coordinator_*.py 2>/dev/null

# Tests laufen?
pytest tests/ -v --tb=short
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
| coordinator_stats.py | 181 | 3 | coordinator/helpers/stats.py |
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

## 3. Zielstruktur (Option A - Transparent)

```
custom_components/googlefindmy/
├── coordinator.py               # BLEIBT - Hauptklasse + Kernlogik (~2.500 Zeilen)
├── coordinator/                 # NEU - Package für ausgelagerte Komponenten
│   ├── __init__.py              # Re-exports aller Operations-Klassen
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

### Unterschied: Operations-Klassen vs. Helpers

| Komponente | Typ | Beschreibung |
|------------|-----|--------------|
| `coordinator/*.py` | Operations-Klassen | Methoden die `self` brauchen (Zugriff auf Coordinator-State) |
| `coordinator/helpers/*.py` | Pure Functions | Zustandslose Funktionen (Input → Output, kein `self`) |

---

## 4. Auswirkungen auf coordinator.py

| Komponente | Vorher | Nachher |
|------------|-------:|--------:|
| Gesamt-Zeilen | 8.342 | ~2.500 |
| Methoden in Hauptklasse | 171 | ~50 |
| Operations-Module | 0 | 5 |
| Helper-Module im Root | 8 | 0 |
| Helper-Module in coordinator/helpers/ | 0 | 8 |

---

## 5. Phasen-Plan (Detailliert)

### ✅ Voraussetzungen (abgeschlossen)
- [x] 8 Helper-Module extrahiert und integriert (coordinator_*.py)
- [x] Tests für Helper-Funktionen vorhanden
- [x] Halluzinierte Funktionen entfernt
- [x] Alle Tests bestehen

### Phase 0: Helpers verschieben (Risiko: Niedrig)
**Ziel:** coordinator_*.py → coordinator/helpers/*.py

**Schritte:**
1. Verzeichnis erstellen:
   ```bash
   mkdir -p custom_components/googlefindmy/coordinator/helpers
   ```

2. `coordinator/helpers/__init__.py` erstellen mit Re-exports:
   ```python
   """Pure helper functions for GoogleFindMyCoordinator."""
   from .registry import (
       extract_canonical_device_id,
       build_entity_unique_id_candidates,
       build_canonical_unique_id,
       match_entity_by_device_id,
       # ... alle 16 Funktionen
   )
   from .cache import (
       normalize_location_fields,
       preserve_metadata_fields,
       should_clear_metadata_only_flag,
       # ... alle 15 Funktionen
   )
   # ... weitere Module
   ```

3. Dateien verschieben (MIT Umbenennung):
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

4. Imports in coordinator.py aktualisieren:
   ```python
   # ALT:
   from .coordinator_registry import extract_canonical_device_id

   # NEU:
   from .coordinator.helpers.registry import extract_canonical_device_id
   # ODER (mit Re-exports):
   from .coordinator.helpers import extract_canonical_device_id
   ```

5. Imports in Tests aktualisieren:
   ```python
   # ALT:
   from custom_components.googlefindmy.coordinator_registry import ...

   # NEU:
   from custom_components.googlefindmy.coordinator.helpers.registry import ...
   ```

6. `coordinator/__init__.py` erstellen (noch leer für Operations):
   ```python
   """Coordinator package for GoogleFindMy integration."""
   # Operations-Klassen werden in späteren Phasen hinzugefügt
   ```

7. Tests ausführen:
   ```bash
   pytest tests/ -v --tb=short
   ```

8. Commit:
   ```bash
   git add -A
   git commit -m "refactor: move coordinator helpers to coordinator/helpers/"
   ```

**Erfolgskriterien Phase 0:**
- [ ] Alle 8 Helper-Dateien in coordinator/helpers/
- [ ] Keine coordinator_*.py mehr im Root
- [ ] Alle Tests bestehen
- [ ] Import-Pfade funktionieren

---

### Phase 1: Package-Infrastruktur (Risiko: Niedrig)
**Ziel:** Leere Operations-Klassen erstellen, Vererbung einrichten

**Schritte:**
1. Operations-Klassen erstellen (leer):
   ```python
   # coordinator/registry.py
   """Device registry operations for GoogleFindMyCoordinator."""
   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
       from ..coordinator import GoogleFindMyCoordinator

   class RegistryOperations:
       """Device registry operations (extracted methods)."""
       pass  # Methoden werden in Phase 2 hierher verschoben
   ```

2. Analog für: subentry.py, polling.py, locate.py, identity.py

3. coordinator/__init__.py aktualisieren:
   ```python
   """Coordinator package for GoogleFindMy integration."""
   from .registry import RegistryOperations
   from .subentry import SubentryOperations
   from .polling import PollingOperations
   from .locate import LocateOperations
   from .identity import IdentityOperations

   __all__ = [
       "RegistryOperations",
       "SubentryOperations",
       "PollingOperations",
       "LocateOperations",
       "IdentityOperations",
   ]
   ```

4. coordinator.py Klasse erweitern:
   ```python
   from .coordinator import (
       RegistryOperations,
       SubentryOperations,
       PollingOperations,
       LocateOperations,
       IdentityOperations,
   )

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

**Erfolgskriterien Phase 1:**
- [ ] 5 leere Operations-Klassen existieren
- [ ] GoogleFindMyCoordinator erbt von allen 5
- [ ] Alle Tests bestehen (keine Regression)
- [ ] Import-Pfade funktionieren

---

### Phase 2: RegistryOperations extrahieren (Risiko: Mittel)
**Ziel:** Registry-Methoden von coordinator.py → coordinator/registry.py

**Methoden zu verschieben (11 Stück, ~1.682 Zeilen):**
1. `_ensure_registry_for_devices` (L3207, ~671 Zeilen) ⚠️ F-Grade
2. `_ensure_service_device_exists` (L2243, ~537 Zeilen) ⚠️ F-Grade
3. `_find_tracker_entity_entry` (L2781, ~275 Zeilen)
4. `_reindex_poll_targets_from_device_registry` (L3141, ~64 Zeilen)
5. `_call_device_registry_api` (L2114, ~44 Zeilen)
6. `_get_device_by_canonical_id`
7. `_update_device_registry_entry`
8. `_remove_device_registry_entry`
9. `_list_registered_devices`
10. `_sync_device_registry`
11. `_validate_registry_state`

**Schritte pro Methode:**
1. Methode aus coordinator.py AUSSCHNEIDEN
2. In coordinator/registry.py EINFÜGEN (in RegistryOperations Klasse)
3. Type-Hint anpassen: `def method(self: "GoogleFindMyCoordinator", ...)`
4. Imports hinzufügen wenn nötig
5. Tests ausführen
6. Wenn Tests bestehen: Commit für diese Methode

**WICHTIG:** Eine Methode nach der anderen! Nicht mehrere gleichzeitig.

**Erfolgskriterien Phase 2:**
- [ ] Alle 11 Registry-Methoden in coordinator/registry.py
- [ ] coordinator.py ~1.682 Zeilen kürzer
- [ ] Alle Tests bestehen
- [ ] Keine zirkulären Imports

---

### Phase 3: SubentryOperations extrahieren (Risiko: Mittel)
**Ziel:** Subentry-Methoden von coordinator.py → coordinator/subentry.py

**Methoden zu verschieben (17 Stück, ~766 Zeilen):**
1. `_refresh_subentry_index` (L1314, ~453 Zeilen) ⚠️ F-Grade
2. `_schedule_core_subentry_repair` (L1228, ~73 Zeilen)
3. `_build_core_subentry_definitions` (L1165, ~62 Zeilen)
4. `attach_subentry_manager` (L1109, ~31 Zeilen)
5. `detach_subentry_manager`
6. `_get_subentry_by_id`
7. `_list_subentries`
8. `_create_subentry`
9. `_update_subentry`
10. `_delete_subentry`
11. `_validate_subentry`
12. `_sync_subentries`
13. `_migrate_legacy_subentries`
14. `_repair_subentry_index`
15. `_rebuild_subentry_cache`
16. `_get_subentry_devices`
17. `_link_device_to_subentry`

**Schritte:** Analog zu Phase 2

**Erfolgskriterien Phase 3:**
- [ ] Alle 17 Subentry-Methoden in coordinator/subentry.py
- [ ] coordinator.py ~766 Zeilen kürzer
- [ ] Alle Tests bestehen

---

### Phase 4: PollingOperations extrahieren (Risiko: Mittel)
**Ziel:** Polling-Methoden von coordinator.py → coordinator/polling.py

**Methoden zu verschieben (6 Stück, ~495 Zeilen):**
1. `_async_start_poll_cycle` (L5879, ~449 Zeilen) ⚠️ F-Grade
2. `_get_predicted_poll_time` (L6879, ~27 Zeilen)
3. `is_polling`
4. `force_poll_due`
5. `last_poll_result`
6. `_schedule_next_poll`

**Erfolgskriterien Phase 4:**
- [ ] Alle 6 Polling-Methoden in coordinator/polling.py
- [ ] coordinator.py ~495 Zeilen kürzer
- [ ] Alle Tests bestehen

---

### Phase 5: IdentityOperations extrahieren (Risiko: Mittel)
**Ziel:** Identity-Methoden von coordinator.py → coordinator/identity.py

**Methoden zu verschieben (4+ Stück, ~776 Zeilen):**
1. `get_active_device_identities` (L4503, ~725 Zeilen) ⚠️ F-Grade
2. `_register_identity_key` (L7114, ~23 Zeilen)
3. `_normalize_identity_key`
4. `_validate_identity_key`

**Erfolgskriterien Phase 5:**
- [ ] Alle Identity-Methoden in coordinator/identity.py
- [ ] coordinator.py ~776 Zeilen kürzer
- [ ] Alle Tests bestehen

---

### Phase 6: LocateOperations extrahieren (Risiko: Mittel)
**Ziel:** Locate-Methoden von coordinator.py → coordinator/locate.py

**Methoden zu verschieben (~274+ Zeilen):**
1. `async_locate_device` (L7945, ~274 Zeilen)
2. Weitere `*location*` Methoden

**Erfolgskriterien Phase 6:**
- [ ] Alle Locate-Methoden in coordinator/locate.py
- [ ] Alle Tests bestehen

---

### Phase 7: Cleanup (Risiko: Niedrig)
**Ziel:** Aufräumen und Dokumentation

**Schritte:**
1. Ungenutzte Imports in coordinator.py entfernen
2. Docstrings für alle Operations-Klassen vervollständigen
3. Test-Coverage prüfen (Ziel: >90%)
4. Diese RESTRUCTURING_PROPOSAL.md als abgeschlossen markieren

**Erfolgskriterien Phase 7:**
- [ ] coordinator.py bei ~2.500 Zeilen
- [ ] Keine Linter-Warnungen
- [ ] Alle Tests bestehen
- [ ] Dokumentation aktuell

---

## 6. Risiko-Bewertung

| Phase | Risiko | Begründung |
|-------|--------|------------|
| Phase 0: Helpers verschieben | Niedrig | Nur Pfade ändern, keine Logik |
| Phase 1: Package-Infrastruktur | Niedrig | Leere Klassen, keine Funktionsänderung |
| Phase 2-6: Methoden-Extraktion | Mittel | Methodensignaturen bleiben gleich |
| Phase 7: Cleanup | Niedrig | Nur kosmetisch |

**Rollback-Strategie:** Git-Revert jederzeit möglich, da schrittweise Commits.

---

## 7. Test-Strategie

### Vor jeder Änderung:
```bash
pytest tests/ -v --tb=short
```

### Nach jeder Methoden-Verschiebung:
```bash
# Spezifische Tests für geänderte Komponente
pytest tests/test_coordinator*.py -v --tb=short

# Integrationstests
pytest tests/test_integration*.py -v --tb=short
```

### Bei Fehlern:
1. SOFORT Git-Revert der letzten Änderung
2. Fehler analysieren
3. Korrigierten Ansatz versuchen

---

## 8. Import-Änderungen Referenz

### Für coordinator.py (intern):
```python
# ALT (vor Phase 0):
from .coordinator_registry import extract_canonical_device_id

# NEU (nach Phase 0):
from .coordinator.helpers.registry import extract_canonical_device_id
```

### Für Tests:
```python
# ALT:
from custom_components.googlefindmy.coordinator_registry import (
    extract_canonical_device_id,
)

# NEU:
from custom_components.googlefindmy.coordinator.helpers.registry import (
    extract_canonical_device_id,
)
```

### Für externe Module (__init__.py, etc.):
```python
# UNVERÄNDERT - GoogleFindMyCoordinator bleibt exportiert:
from .coordinator import GoogleFindMyCoordinator
```

---

## 9. Checkliste für Fortsetzung

Wenn du diese Datei liest und den Kontext verloren hast:

1. **Aktuellen Stand prüfen:**
   ```bash
   git log --oneline -10
   ls -la custom_components/googlefindmy/coordinator/
   ls custom_components/googlefindmy/coordinator_*.py 2>/dev/null
   ```

2. **Tests ausführen:**
   ```bash
   pytest tests/ -v --tb=short
   ```

3. **Welche Phase ist dran?**
   - Gibt es `coordinator/helpers/`? → Phase 0 abgeschlossen
   - Gibt es `coordinator_*.py` im Root? → Phase 0 noch offen
   - Sind Operations-Klassen gefüllt? → Prüfe Zeilenzahl in coordinator.py

4. **Nächsten Schritt aus Phasen-Plan (Abschnitt 5) ausführen**

---

## 10. Nicht ändern

- Keine Umbenennungen von Auth/, NovaApi/, SpotApi/, FMDNCrypto/, KeyBackup/
- Keine Änderung der externen API-Strukturen
- `coordinator.py` BLEIBT im Root (Option A)
- `eid_resolver.py` bleibt im Root
- `fmdn_finder/` bleibt eigenständig

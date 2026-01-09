# Strukturierungsvorschlag: GoogleFindMy-HA

**Datum:** 2026-01-09
**Status:** Entwurf zur Diskussion
**Ziel:** coordinator.py von 8.342 auf ~2.500 Zeilen reduzieren

---

## 1. Problemanalyse

### Aktuelle Situation

| Datei | Zeilen | Problem |
|-------|-------:|---------|
| coordinator.py | 8.342 | 171 Methoden, nicht vollständig lesbar |
| __init__.py | 8.313 | Ebenfalls sehr groß |
| eid_resolver.py | 2.363 | Groß, aber eigenständig funktional |

### Bestehende Helper-Module (bereits extrahiert)

| Modul | Zeilen | Funcs | Status |
|-------|-------:|------:|--------|
| coordinator_registry.py | 742 | 16 | ✅ Integriert |
| coordinator_cache.py | 620 | 15 | ✅ Integriert |
| coordinator_identity.py | 410 | 10 | ✅ Integriert |
| coordinator_subentry.py | 302 | 8 | ✅ Integriert |
| coordinator_update.py | 245 | 6 | ✅ Integriert |
| coordinator_stats.py | 181 | 3 | ✅ Integriert |
| coordinator_geo.py | 163 | 4 | ✅ Integriert |
| coordinator_polling.py | 99 | 3 | ✅ Integriert |

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

## 3. Vorgeschlagene Änderungen

### 3.1 EID-Komponenten Analyse

**Aktuelle Struktur:**
```
eid_resolver.py              # EID-Auflösung (2.363 Zeilen) - im Root
fmdn_finder/                 # Bermuda FMDN-Finder (1.759 Zeilen)
├── bermuda_listener.py      # Nutzt eid_resolver
├── google_uploader.py
└── location_uploader.py
FMDNCrypto/eid_generator.py  # Krypto-Primitive (extern, nicht ändern)
```

**Abhängigkeiten:**
- `fmdn_finder/bermuda_listener.py` → importiert `eid_resolver`
- `eid_resolver.py` → importiert aus `FMDNCrypto/`
- Keine zirkulären Abhängigkeiten

**Empfehlung:** Struktur beibehalten
- `eid_resolver.py` bleibt im Root (wird von mehreren Stellen importiert)
- `fmdn_finder/` bleibt eigenständig (Bermuda-spezifisch)
- `FMDNCrypto/` unverändert (externe Bibliothek)

**Alternative (optional):** Falls gewünscht, könnte ein `eid/` Umbrella-Package erstellt werden:
```
eid/                         # Optionales Umbrella-Package
├── __init__.py              # Re-exports: EidResolver, fmdn_finder
├── resolver.py              # Verschoben von eid_resolver.py
└── fmdn_finder/             # Verschoben von fmdn_finder/
    └── ...
```
→ Dies wäre aber eine größere Umstrukturierung. **Empfehlung: Aktuell nicht umsetzen.**

### 3.2 Coordinator als Package strukturieren

**Problem:** `GoogleFindMyCoordinator` hat 171 Methoden in einer Klasse.

**Vorschlag:** Methoden-Gruppen in Operations-Klassen auslagern (Package-Struktur).

#### Detaillierte Modul-Zuordnung

**registry.py (11 Methoden, ~1.682 Zeilen)**
- `_ensure_registry_for_devices` (L3207, ~671 Zeilen) ⚠️ F-Grade
- `_ensure_service_device_exists` (L2243, ~537 Zeilen) ⚠️ F-Grade
- `_find_tracker_entity_entry` (L2781, ~275 Zeilen)
- `_reindex_poll_targets_from_device_registry` (L3141, ~64 Zeilen)
- `_call_device_registry_api` (L2114, ~44 Zeilen)
- ... und 6 weitere

**subentry.py (17 Methoden, ~766 Zeilen)**
- `_refresh_subentry_index` (L1314, ~453 Zeilen) ⚠️ F-Grade
- `_schedule_core_subentry_repair` (L1228, ~73 Zeilen)
- `_build_core_subentry_definitions` (L1165, ~62 Zeilen)
- `attach_subentry_manager` (L1109, ~31 Zeilen)
- ... und 13 weitere

**polling.py (6 Methoden, ~495 Zeilen)**
- `_async_start_poll_cycle` (L5879, ~449 Zeilen) ⚠️ F-Grade
- `_get_predicted_poll_time` (L6879, ~27 Zeilen)
- `is_polling`, `force_poll_due`, `last_poll_result`

**identity.py (4 Methoden, ~776 Zeilen)**
- `get_active_device_identities` (L4503, ~725 Zeilen) ⚠️ F-Grade
- `_register_identity_key` (L7114, ~23 Zeilen)
- `_normalize_identity_key*` Methoden

**locate.py (1+ Methoden, ~274+ Zeilen)**
- `async_locate_device` (L7945, ~274 Zeilen)
- Evtl. weitere `*location*` Methoden

#### Geplante Struktur (Option A - Transparent)

```
custom_components/googlefindmy/
├── coordinator.py               # BLEIBT - Hauptklasse + Kernlogik
└── coordinator/                 # NEU - Nur ausgelagerte Operations
    ├── __init__.py              # Re-exports
    ├── registry.py              # Device-Registry (~1.350 Zeilen)
    ├── subentry.py              # Subentry-Management (~800 Zeilen)
    ├── polling.py               # Polling-Logik (~500 Zeilen)
    ├── locate.py                # Locate-Funktionen (~500 Zeilen)
    └── identity.py              # Identity-Verwaltung (~770 Zeilen)
```

**Vorteile von Option A:**
- `coordinator.py` bleibt sichtbar im Root (keine "Magie")
- Klare Trennung: Hauptdatei vs. ausgelagerte Module
- Einfacher Rollback möglich

**Implementierung:**
```python
# coordinator/registry.py
"""Device registry operations for GoogleFindMyCoordinator."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..coordinator import GoogleFindMyCoordinator

class RegistryOperations:
    """Device registry operations."""

    async def _ensure_registry_for_devices(self: "GoogleFindMyCoordinator", ...):
        """Ensure all devices have registry entries."""
        ...

    async def _ensure_service_device_exists(self: "GoogleFindMyCoordinator", ...):
        """Ensure service device exists."""
        ...

# coordinator/__init__.py
"""Ausgelagerte Operations-Klassen für GoogleFindMyCoordinator."""
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

# coordinator.py (BLEIBT im Root, importiert aus coordinator/)
from .coordinator.registry import RegistryOperations
from .coordinator.subentry import SubentryOperations
from .coordinator.polling import PollingOperations
from .coordinator.locate import LocateOperations
from .coordinator.identity import IdentityOperations

class GoogleFindMyCoordinator(
    RegistryOperations,
    SubentryOperations,
    PollingOperations,
    LocateOperations,
    IdentityOperations,
    DataUpdateCoordinator,
):
    """Google Find My coordinator with modular organization."""
    # Initialisierung + ~50 Kernmethoden (~2.500 Zeilen)
```

**Hinweis:** Import-Pfade bleiben unverändert: `from .coordinator import GoogleFindMyCoordinator`

### 3.3 Bestehende Helper-Module beibehalten

Die bereits extrahierten `coordinator_*.py` Module bleiben unverändert:
- Sie enthalten **pure functions** (zustandslos)
- Die **Operations-Klassen** im `coordinator/` Package enthalten **Methoden** (brauchen `self`)
- Beide ergänzen sich

**Namenskonvention:**
- `coordinator_*.py` im Root = Pure Helper Functions
- `coordinator/*.py` im Package = Operations-Klassen (Methoden)

---

## 4. Auswirkungen auf coordinator.py

| Komponente | Vorher | Nachher |
|------------|-------:|--------:|
| Gesamt-Zeilen | 8.342 | ~2.500 |
| Methoden in Hauptklasse | 171 | ~50 |
| Operations-Module | 0 | 5 |
| Pure-Helper-Module | 8 | 8 |

---

## 5. Migrations-Plan

### Phase 1: Package-Infrastruktur (Risiko: Niedrig)
1. `coordinator/` Verzeichnis erstellen
2. `coordinator/__init__.py` mit leeren Re-exports anlegen
3. Leere Operations-Klassen in separaten Modulen anlegen
4. `coordinator.py` importiert aus `coordinator/` und erbt von Operations-Klassen
5. Tests verifizieren: Keine Regression

### Phase 2: RegistryOperations extrahieren (Risiko: Mittel)
1. `_ensure_registry_for_devices` → `coordinator/registry.py`
2. `_ensure_service_device_exists` → `coordinator/registry.py`
3. `_find_tracker_entity_entry` → `coordinator/registry.py`
4. Weitere `*registry*` Methoden
5. Tests durchführen

### Phase 3: SubentryOperations extrahieren (Risiko: Mittel)
1. `_refresh_subentry_index` → `coordinator/subentry.py`
2. Alle `*subentry*` Methoden
3. Tests durchführen

### Phase 4: Weitere Operations (Risiko: Mittel)
1. `PollingOperations` → `coordinator/polling.py`
2. `LocateOperations` → `coordinator/locate.py`
3. `IdentityOperations` → `coordinator/identity.py`
4. Nach jeder Extraktion: Tests

### Phase 5: Cleanup (Risiko: Niedrig)
1. Ungenutzte Imports entfernen
2. Docstrings für Operations-Klassen hinzufügen
3. Test-Coverage prüfen

---

## 6. Risiko-Bewertung

| Änderung | Risiko | Begründung |
|----------|--------|------------|
| Package-Infrastruktur | Niedrig | Import-Pfade bleiben kompatibel |
| Operations-Extraktion | Mittel | Methodensignaturen bleiben gleich |
| Helper-Module | Keins | Bereits integriert und getestet |

**Rollback-Strategie:** Git-Revert jederzeit möglich, da schrittweise Commits.

---

## 7. Entscheidungen

1. **Struktur:** Option A gewählt ✅
   - `coordinator.py` bleibt im Root
   - `coordinator/` enthält nur ausgelagerte Operations-Klassen

2. **Reihenfolge:** Welche Operations zuerst extrahieren?
   - **Empfehlung:** `RegistryOperations` (größte Komplexität, höchster Nutzen)

3. **Helper-Module:** Bestehende `coordinator_*.py` bleiben im Root
   - Pure Functions und Operations-Klassen bleiben getrennt

---

## 8. Nicht ändern

- Keine Umbenennungen bestehender Verzeichnisse
- Keine Verschiebung von Auth/, NovaApi/, SpotApi/, FMDNCrypto/, KeyBackup/
- Keine Änderung der externen API-Strukturen
- Helper-Module (coordinator_*.py) bleiben im Root

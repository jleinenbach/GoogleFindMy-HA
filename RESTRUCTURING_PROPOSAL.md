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

### 3.2 Coordinator-Methoden als Mixins auslagern

**Problem:** `GoogleFindMyCoordinator` hat 171 Methoden in einer Klasse.

**Vorschlag:** Methoden-Gruppen in Mixin-Klassen auslagern.

#### Detaillierte Mixin-Zuordnung

**RegistryMixin (11 Methoden, ~1.682 Zeilen)**
- `_ensure_registry_for_devices` (L3207, ~671 Zeilen) ⚠️ F-Grade
- `_ensure_service_device_exists` (L2243, ~537 Zeilen) ⚠️ F-Grade
- `_find_tracker_entity_entry` (L2781, ~275 Zeilen)
- `_reindex_poll_targets_from_device_registry` (L3141, ~64 Zeilen)
- `_call_device_registry_api` (L2114, ~44 Zeilen)
- ... und 6 weitere

**SubentryMixin (17 Methoden, ~766 Zeilen)**
- `_refresh_subentry_index` (L1314, ~453 Zeilen) ⚠️ F-Grade
- `_schedule_core_subentry_repair` (L1228, ~73 Zeilen)
- `_build_core_subentry_definitions` (L1165, ~62 Zeilen)
- `attach_subentry_manager` (L1109, ~31 Zeilen)
- ... und 13 weitere

**PollingMixin (6 Methoden, ~495 Zeilen)**
- `_async_start_poll_cycle` (L5879, ~449 Zeilen) ⚠️ F-Grade
- `_get_predicted_poll_time` (L6879, ~27 Zeilen)
- `is_polling`, `force_poll_due`, `last_poll_result`

**IdentityMixin (4 Methoden, ~776 Zeilen)**
- `get_active_device_identities` (L4503, ~725 Zeilen) ⚠️ F-Grade
- `_register_identity_key` (L7114, ~23 Zeilen)
- `_normalize_identity_key*` Methoden

**LocateMixin (1+ Methoden, ~274+ Zeilen)**
- `async_locate_device` (L7945, ~274 Zeilen)
- Evtl. weitere `*location*` Methoden

#### Geplante Mixin-Struktur

```
coordinator_mixins/               # NEUES Verzeichnis
├── __init__.py
├── subentry_mixin.py            # ~18 Methoden, ~800 Zeilen
├── registry_mixin.py            # ~8 Methoden, ~1.350 Zeilen
├── polling_mixin.py             # ~6 Methoden, ~500 Zeilen
├── locate_mixin.py              # ~5 Methoden, ~500 Zeilen
└── identity_mixin.py            # ~4 Methoden, ~770 Zeilen
```

**Implementierung:**
```python
# coordinator_mixins/registry_mixin.py
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..coordinator import GoogleFindMyCoordinator

class RegistryMixin:
    """Device registry operations mixin."""

    # Typ-Hints für self (wird zur Laufzeit GoogleFindMyCoordinator sein)
    hass: "HomeAssistant"
    config_entry: "ConfigEntry"

    async def _ensure_registry_for_devices(self: "GoogleFindMyCoordinator", ...):
        """Ensure all devices have registry entries."""
        ...

    async def _ensure_service_device_exists(self: "GoogleFindMyCoordinator", ...):
        """Ensure service device exists."""
        ...

# coordinator.py (neu, ~2.500 Zeilen)
from .coordinator_mixins import (
    RegistryMixin,
    SubentryMixin,
    PollingMixin,
    LocateMixin,
    IdentityMixin,
)

class GoogleFindMyCoordinator(
    RegistryMixin,
    SubentryMixin,
    PollingMixin,
    LocateMixin,
    IdentityMixin,
    DataUpdateCoordinator,
):
    """Google Find My coordinator with mixin-based organization."""
    # Nur noch Initialisierung + ~50 Kernmethoden
```

### 3.3 Bestehende Helper-Module beibehalten

Die bereits extrahierten `coordinator_*.py` Module bleiben unverändert:
- Sie enthalten **pure functions** (zustandslos)
- Die **Mixins** enthalten **Methoden** (brauchen `self`)
- Beide ergänzen sich

---

## 4. Auswirkungen auf coordinator.py

| Komponente | Vorher | Nachher |
|------------|-------:|--------:|
| Gesamt-Zeilen | 8.342 | ~2.500 |
| Methoden in Hauptklasse | 171 | ~50 |
| Mixin-Dateien | 0 | 5 |
| Pure-Helper-Module | 8 | 8 |

---

## 5. Migrations-Plan

### Phase 1: Mixin-Infrastruktur (Risiko: Niedrig)
1. `coordinator_mixins/` Verzeichnis erstellen
2. `__init__.py` mit leeren Mixin-Klassen anlegen
3. Coordinator erbt von Mixins (noch leer)
4. Tests verifizieren: Keine Regression

### Phase 2: RegistryMixin extrahieren (Risiko: Mittel)
1. `_ensure_registry_for_devices` → `registry_mixin.py`
2. `_ensure_service_device_exists` → `registry_mixin.py`
3. `_find_tracker_entity_entry` → `registry_mixin.py`
4. Weitere `*registry*` Methoden
5. Tests durchführen

### Phase 3: SubentryMixin extrahieren (Risiko: Mittel)
1. `_refresh_subentry_index` → `subentry_mixin.py`
2. Alle `*subentry*` Methoden
3. Tests durchführen

### Phase 4: Weitere Mixins (Risiko: Mittel)
1. `PollingMixin` - Polling-bezogene Methoden
2. `LocateMixin` - Locate-bezogene Methoden
3. `IdentityMixin` - Identity-bezogene Methoden
4. Nach jeder Extraktion: Tests

### Phase 5: Cleanup (Risiko: Niedrig)
1. Ungenutzte Imports in coordinator.py entfernen
2. Docstrings für Mixins hinzufügen
3. Test-Coverage prüfen

---

## 6. Risiko-Bewertung

| Änderung | Risiko | Begründung |
|----------|--------|------------|
| Mixin-Infrastruktur | Niedrig | Nur Vererbung, keine Code-Änderung |
| Mixin-Extraktion | Mittel | Methodensignaturen bleiben gleich |
| Helper-Module | Keins | Bereits integriert und getestet |

**Rollback-Strategie:** Git-Revert jederzeit möglich, da schrittweise Commits.

---

## 7. Entscheidungsfragen

1. **Mixin-Verzeichnis:** Sollen Mixins in `coordinator_mixins/` oder als `coordinator_*_mixin.py` im Root liegen?
   - **Empfehlung:** `coordinator_mixins/` (sauberere Trennung)

2. **Reihenfolge:** Welches Mixin zuerst?
   - **Empfehlung:** `RegistryMixin` (größte Komplexität, höchster Nutzen)

3. **Helper-Module:** Sollen bestehende `coordinator_*.py` Helper in Mixins integriert werden?
   - **Empfehlung:** Nein - Pure Functions und Methoden ergänzen sich

---

## 8. Nicht ändern

- Keine Umbenennungen bestehender Verzeichnisse
- Keine Verschiebung von Auth/, NovaApi/, SpotApi/, FMDNCrypto/, KeyBackup/
- Keine Änderung der externen API-Strukturen
- Helper-Module (coordinator_*.py) bleiben im Root

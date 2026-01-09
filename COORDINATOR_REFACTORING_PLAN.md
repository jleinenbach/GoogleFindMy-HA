# Coordinator.py Refactoring Plan

**Datei**: `custom_components/googlefindmy/coordinator.py`
**Aktuelle Größe**: 8761 Zeilen, 199 Methoden
**Zielgröße**: ~2000-2500 Zeilen im Hauptmodul
**Branch**: `claude/1.7.0-3-coordinator-refactoring` (basiert auf `1.7.0-3`)

> ⚠️ **WICHTIG**: Vor jeder Phase müssen die Tests gemäß [PRE_REFACTORING_TEST_PLAN.md](PRE_REFACTORING_TEST_PLAN.md) geschrieben und bestanden sein!

---

## 0. Lessons Learned (aus fehlgeschlagenem Versuch)

### Was schief ging:
1. **Branch-Basis**: Arbeit wurde auf veraltetem Branch durchgeführt (2339 Commits hinter `1.7.0-3`)
2. **Zwei getrennte Branches**: Phase 1-3 auf einem Branch, Phase 4 auf anderem - nicht synchronisiert
3. **Keine Integration mit coordinator.py**: Module wurden extrahiert, aber coordinator.py nicht angepasst

### Was funktioniert hat:
1. **TDD-Ansatz**: Tests VOR Extraktion schreiben funktioniert gut
2. **Hohe Testabdeckung**: 99% Coverage für extrahierte Module erreichbar
3. **Pure Functions**: Standalone-Hilfsfunktionen lassen sich gut extrahieren und testen

### Backup der vorherigen Arbeit:
Die extrahierten Module sind in `/tmp/refactoring-backup/` gesichert:
- `coordinator_geo.py` + Tests (53 Tests)
- `coordinator_stats.py` + Tests (58 Tests)
- `coordinator_cache.py` + Tests (53 Tests)
- `coordinator_registry.py` + Tests (74 Tests)

**Hinweis**: Diese Module wurden für eine ÄLTERE Version von coordinator.py geschrieben und müssen für 1.7.0-3 neu validiert/angepasst werden!

---

## 1. Analyse der aktuellen Struktur

### 1.1 Klassen und Top-Level-Funktionen (24)

| Zeile | Name | Typ | Beschreibung |
|-------|------|-----|--------------|
| 176 | `CacheProtocol` | Protocol | Cache-Interface |
| 255 | `_update_preserve_metadata` | Function | Metadata-Update |
| 280 | `_clamp` | Function | Value-Clamping |
| 291 | `DiagnosticsBuffer` | Class | Diagnostik-Puffer |
| 334 | `SubentryMetadata` | Dataclass | Subentry-Daten |
| 359-517 | Diverse Helpers | Functions | Parsing/Normalisierung |
| 585 | `_RecorderHistoryProxy` | Class | Recorder-Proxy |
| 681-723 | Status-Dataclasses | Classes | StatusSnapshot, ApiStatus, etc. |
| 723 | `DeviceIdentity` | Dataclass | Geräte-Identität |
| 767 | `GoogleFindMyCoordinator` | Class | Haupt-Koordinator |

### 1.2 Methoden-Kategorien im Koordinator

| Kategorie | Anzahl | Kandidat für Extraktion |
|-----------|--------|------------------------|
| Subentry-Management | 25 | ✅ `coordinator_subentry.py` |
| Device-Registry | 43 | ✅ `coordinator_registry.py` |
| Cache/Geo | 18 | ✅ `coordinator_geo.py` |
| Stats/Status | 27 | ✅ `coordinator_stats.py` |
| Identity/EID | 24 | ⚠️ Teilweise (EidResolver existiert) |
| Actions (Sound/Locate) | 10 | ⚠️ Evtl. `coordinator_actions.py` |
| Lifecycle (Setup/Shutdown) | 15 | ❌ Bleibt im Kern |
| Sonstige | ~37 | ❌ Bleibt im Kern |

### 1.3 Hoch-Komplexe Funktionen (F-Rating)

| Funktion | Komplexität | Kategorie | Extraktion |
|----------|-------------|-----------|------------|
| `_refresh_subentry_index` | 139 | Subentry | → `coordinator_subentry.py` |
| `get_active_device_identities` | 128 | Identity | → Vereinfachen |
| `_ensure_registry_for_devices` | 126 | Registry | → `coordinator_registry.py` |
| `_ensure_service_device_exists` | 108 | Registry | → `coordinator_registry.py` |
| `_async_update_data` | 100 | Kern | → Phasen extrahieren |
| `_async_start_poll_cycle` | 62 | Kern | → Vereinfachen |
| `_find_tracker_entity_entry` | 50 | Registry | → `coordinator_registry.py` |
| `update_device_cache` | 45 | Cache | → `coordinator_cache.py` |
| `async_locate_device` | 40 | Actions | → `coordinator_actions.py` |
| `_merge_with_existing_cache_row` | 37 | Cache | → `coordinator_cache.py` |

---

## 2. Geplante Module

### 2.1 `coordinator_subentry.py` (~600-800 Zeilen)

**Verantwortlichkeit**: Subentry-Verwaltung und -Index

**Zu extrahierende Methoden**:
```python
# Aus GoogleFindMyCoordinator
_refresh_subentry_index()           # Komplexität 139 - KRITISCH
_build_core_subentry_definitions()
_schedule_core_subentry_repair()
_cancel_pending_subentry_repair()
attach_subentry_manager()
_default_subentry_key()
async_wait_subentry_visibility_updates()
_group_snapshot_by_subentry()
_store_subentry_snapshots()
_resolve_subentry_key_for_feature()
get_subentry_key_for_feature()
get_subentry_metadata()
stable_subentry_identifier()
get_subentry_snapshot()
is_device_visible_in_subentry()
get_device_location_data_for_subentry()
get_device_last_seen_for_subentry()
```

**Zu extrahierende Klassen**:
```python
SubentryMetadata  # Dataclass
```

**Schnittstelle zum Koordinator**:
```python
class SubentryManager:
    def __init__(self, coordinator: GoogleFindMyCoordinator): ...
    def refresh_index(self, visible_devices: Sequence[...]) -> None: ...
    def get_metadata(self, subentry_key: str) -> SubentryMetadata | None: ...
    # ...
```

---

### 2.2 `coordinator_registry.py` (~800-1000 Zeilen)

**Verantwortlichkeit**: Device-Registry-Operationen

**Zu extrahierende Methoden**:
```python
# Aus GoogleFindMyCoordinator
_ensure_registry_for_devices()      # Komplexität 126 - KRITISCH
_ensure_service_device_exists()     # Komplexität 108 - KRITISCH
_find_tracker_entity_entry()        # Komplexität 50
find_tracker_entity_entry()
_ensure_device_name_cache()
_apply_pending_via_updates()
_sync_owner_index()
_reindex_poll_targets_from_device_registry()
_call_device_registry_api()
_device_registry_kwargs_need_legacy_retry()
_device_registry_build_legacy_kwargs()
_device_registry_config_subentry_kwarg_name()
_device_registry_allows_translation_update()
_extract_our_identifier()
_device_display_name()
```

**Nested Functions zu extrahieren** (aus `_ensure_registry_for_devices`):
```python
_normalize_tracker_subentry_id()
_resolve_tracker_subentry()
_normalized_name()
_track_hub_name()
_is_hub_device()
_resolve_hub_name()
_subentry_links()
_has_tracker_link()
_has_hub_link()
_remove_hub_link()
_update_device_with_kwargs()
_refresh_device_entry()
_heal_tracker_device_subentry()
```

**Schnittstelle**:
```python
class RegistryManager:
    def __init__(self, coordinator: GoogleFindMyCoordinator): ...
    def ensure_devices(self, devices: list[dict], ignored: set[str]) -> int: ...
    def ensure_service_device(self, entry: ConfigEntry) -> None: ...
    def find_tracker_entity(self, device_id: str) -> EntityRegistryEntry | None: ...
```

---

### 2.3 `coordinator_geo.py` (~200-300 Zeilen)

**Verantwortlichkeit**: Geografische Berechnungen

**Zu extrahierende Methoden**:
```python
_haversine_distance()
_apply_weighted_location_fusion()
_normalize_coords()
_is_significant_update()
_coerce_float()
_clamp()  # Top-level function
```

**Zu extrahierende Hilfsfunktionen**:
```python
_safe_accuracy()  # Nested in _apply_weighted_location_fusion
```

**Schnittstelle**:
```python
def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float: ...
def is_significant_update(old_data: dict, new_data: dict, threshold_m: float = 50) -> bool: ...
def normalize_coords(lat: Any, lon: Any) -> tuple[float, float] | None: ...
def apply_weighted_fusion(locations: list[dict]) -> dict: ...
```

---

### 2.4 `coordinator_cache.py` (~400-500 Zeilen)

**Verantwortlichkeit**: Location-Cache und Device-Daten

**Zu extrahierende Methoden**:
```python
update_device_cache()               # Komplexität 45
_merge_with_existing_cache_row()    # Komplexität 37
get_device_location_data()
prime_device_location_cache()
seed_device_last_seen()
_track_device_interval()
_persist_anchor_metadata()
get_device_last_seen()
get_device_display_name()
get_device_name_map()
is_device_present()
get_absent_device_ids()
purge_device()
_build_base_snapshot_entry()
_update_entry_from_cache()
_build_snapshot_from_cache()
```

**Schnittstelle**:
```python
class LocationCache:
    def __init__(self, coordinator: GoogleFindMyCoordinator): ...
    def update(self, device_id: str, new_data: dict, wall_now: float) -> bool: ...
    def get_location(self, device_id: str) -> dict | None: ...
    def get_last_seen(self, device_id: str) -> datetime | None: ...
```

---

### 2.5 `coordinator_stats.py` (~300-400 Zeilen)

**Verantwortlichkeit**: Statistiken und Metriken

**Zu extrahierende Methoden**:
```python
_async_load_stats()
_async_save_stats()
_async_save_sound_uuids()
_debounced_save_stats()
_schedule_stats_persist()
_increment_stat_on_loop()
increment_stat()
safe_update_metric()
get_metric()
_get_duration()
get_setup_duration_seconds()
get_fcm_acquire_duration_seconds()
get_last_poll_duration_seconds()
get_recent_errors()
_append_recent_error()
note_error()
_short_error_message()
```

**Zu extrahierende Klassen**:
```python
DiagnosticsBuffer
StatusSnapshot
ApiStatus
FcmStatus
```

**Schnittstelle**:
```python
class StatsManager:
    def __init__(self, coordinator: GoogleFindMyCoordinator): ...
    async def load(self) -> None: ...
    async def save(self) -> None: ...
    def increment(self, stat_name: str) -> None: ...
    def get_metric(self, key: str) -> float | None: ...
```

---

### 2.6 `coordinator_actions.py` (~300-400 Zeilen) - OPTIONAL

**Verantwortlichkeit**: Device-Aktionen (Locate, Sound)

**Zu extrahierende Methoden**:
```python
async_locate_device()               # Komplexität 40
async_play_sound()
async_stop_sound()
can_play_sound()
can_request_location()
_get_device_lock()
push_updated()
_api_push_ready()
_note_push_transport_problem()
```

---

## 3. Extraktions-Reihenfolge

### Phase 1: Geografische Utilities (Niedrigstes Risiko)
1. `coordinator_geo.py` erstellen
2. Standalone-Funktionen extrahieren
3. Tests schreiben
4. Imports im Koordinator anpassen

### Phase 2: Stats-Modul (Niedriges Risiko)
1. `coordinator_stats.py` erstellen
2. Dataclasses verschieben
3. Stats-Methoden extrahieren
4. Tests anpassen

### Phase 3: Cache-Modul (Mittleres Risiko)
1. `coordinator_cache.py` erstellen
2. Cache-Methoden extrahieren
3. Schnittstelle definieren
4. Integration testen

### Phase 4: Registry-Modul (Höheres Risiko)
1. `coordinator_registry.py` erstellen
2. Nested Functions in Top-Level umwandeln
3. Komplexe Methoden aufteilen
4. Extensive Tests

### Phase 5: Subentry-Modul (Höchstes Risiko)
1. `coordinator_subentry.py` erstellen
2. `_refresh_subentry_index` in Phasen aufteilen
3. State-Management anpassen
4. Integration sorgfältig testen

---

## 4. Komplexitäts-Reduktion innerhalb der Module

### 4.1 `_refresh_subentry_index` (139 → Ziel: <20)

**Strategie**: Aufteilen in Phasen
```python
def _refresh_subentry_index(self, visible_devices):
    # Phase 1: Collect subentry candidates
    candidates = self._collect_subentry_candidates(visible_devices)

    # Phase 2: Validate and filter
    validated = self._validate_subentry_candidates(candidates)

    # Phase 3: Build index
    self._build_subentry_index(validated)

    # Phase 4: Register devices
    self._register_devices_to_subentries(validated)

    # Phase 5: Update snapshots
    self._update_subentry_snapshots()
```

### 4.2 `_ensure_registry_for_devices` (126 → Ziel: <20)

**Strategie**: Nested Functions extrahieren + Phasen
```python
def _ensure_registry_for_devices(self, devices, ignored):
    ctx = RegistryContext(devices=devices, ignored=ignored, ...)

    # Phase 1: Normalize and validate
    normalized = self._normalize_device_entries(ctx)

    # Phase 2: Ensure tracker devices
    self._ensure_tracker_devices(ctx, normalized)

    # Phase 3: Heal subentry links
    self._heal_subentry_links(ctx)

    # Phase 4: Cleanup orphaned
    self._cleanup_orphaned_devices(ctx)

    return ctx.created_count
```

### 4.3 `get_active_device_identities` (128 → Ziel: <25)

**Strategie**: Helper-Funktionen extrahieren
```python
def get_active_device_identities(self):
    # Extract nested functions to module level
    identities = []
    for device in self._get_enabled_devices():
        identity = self._build_device_identity(device)
        if identity:
            identities.append(identity)
    return identities

def _build_device_identity(self, device: dict) -> DeviceIdentity | None:
    # Consolidated logic from nested functions
    ...
```

---

## 5. Abhängigkeiten und Import-Struktur

### 5.1 Import-Hierarchie
```
coordinator.py
├── coordinator_geo.py      (keine Abhängigkeiten)
├── coordinator_stats.py    (keine Abhängigkeiten)
├── coordinator_cache.py    (importiert coordinator_geo)
├── coordinator_registry.py (importiert coordinator_cache)
└── coordinator_subentry.py (importiert coordinator_registry)
```

### 5.2 Zirkuläre Imports vermeiden

**Problem**: Module müssen auf `GoogleFindMyCoordinator` zugreifen.

**Lösung**: Protocol-basierte Dependency Injection
```python
# coordinator_protocols.py
from typing import Protocol

class CoordinatorProtocol(Protocol):
    config_entry: ConfigEntry
    hass: HomeAssistant
    data: list[dict[str, Any]]
    # ...

# coordinator_registry.py
from .coordinator_protocols import CoordinatorProtocol

class RegistryManager:
    def __init__(self, coordinator: CoordinatorProtocol): ...
```

---

## 6. Testbarkeit

### 6.1 Neue Test-Dateien

| Modul | Test-Datei | Fokus |
|-------|------------|-------|
| `coordinator_geo.py` | `test_coordinator_geo.py` | Unit-Tests für Geo-Funktionen |
| `coordinator_stats.py` | `test_coordinator_stats.py` | Stats-Persistenz |
| `coordinator_cache.py` | `test_coordinator_cache.py` | Cache-Operationen |
| `coordinator_registry.py` | `test_coordinator_registry.py` | Registry-Integration |
| `coordinator_subentry.py` | `test_coordinator_subentry.py` | Subentry-Logik |

### 6.2 Property-Based Tests (Hypothesis)

```python
# test_coordinator_geo.py
from hypothesis import given, strategies as st

@given(
    lat1=st.floats(min_value=-90, max_value=90),
    lon1=st.floats(min_value=-180, max_value=180),
    lat2=st.floats(min_value=-90, max_value=90),
    lon2=st.floats(min_value=-180, max_value=180),
)
def test_haversine_symmetric(lat1, lon1, lat2, lon2):
    d1 = haversine_distance(lat1, lon1, lat2, lon2)
    d2 = haversine_distance(lat2, lon2, lat1, lon1)
    assert abs(d1 - d2) < 0.001  # Symmetric within tolerance
```

---

## 7. Metriken-Ziele

| Metrik | Vorher | Nachher | Verbesserung |
|--------|--------|---------|--------------|
| coordinator.py Zeilen | 8761 | ~2500 | -71% |
| Max. Komplexität | 139 | <30 | -78% |
| Durchschnittliche Komplexität | F (43) | C (<20) | -53% |
| Funktionen mit F-Rating | 7 | 0 | -100% |
| Testabdeckung | ? | >90% | ↑ |

---

## 8. Risiken und Mitigationen

| Risiko | Wahrscheinlichkeit | Impact | Mitigation |
|--------|-------------------|--------|------------|
| Zirkuläre Imports | Mittel | Hoch | Protocol-basierte DI |
| Regressions | Mittel | Hoch | Extensive Tests vor Refactoring |
| Performance-Einbußen | Niedrig | Mittel | Profiling nach Extraktion |
| State-Inkonsistenz | Mittel | Hoch | State in Koordinator zentralisieren |

---

## 9. Zeitschätzung

| Phase | Aufwand | Abhängigkeiten |
|-------|---------|----------------|
| Phase 1: coordinator_geo.py | ~2h | Keine |
| Phase 2: coordinator_stats.py | ~3h | Keine |
| Phase 3: coordinator_cache.py | ~4h | Phase 1 |
| Phase 4: coordinator_registry.py | ~6h | Phase 3 |
| Phase 5: coordinator_subentry.py | ~8h | Phase 4 |
| **Gesamt** | **~23h** | |

---

## 10. Nächste Schritte

1. [ ] Plan reviewen und genehmigen
2. [ ] `coordinator_protocols.py` erstellen (Interfaces)
3. [ ] Phase 1 starten: `coordinator_geo.py`
4. [ ] Tests für jedes neue Modul schreiben
5. [ ] CI-Integration sicherstellen
6. [ ] Schrittweise Extraktion mit Commits pro Phase

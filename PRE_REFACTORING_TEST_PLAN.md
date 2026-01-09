# Pre-Refactoring Test Plan

**Ziel**: Sicherstellen, dass alle kritischen Funktionen vor dem Refactoring ausreichend getestet sind, um Regressionen zu erkennen.

---

## 1. Aktuelle Test-Abdeckung

### 1.1 Vorhandene Coordinator-Tests (17 Dateien)

| Test-Datei | Tests | Fokus |
|------------|-------|-------|
| `test_coordinator.py` | ~15 | Basis-Funktionalität |
| `test_coordinator_device_registry.py` | 40 | Device-Registry ✅ |
| `test_coordinator_significance.py` | 10 | Signifikanz-Updates ✅ |
| `test_coordinator_subentry_repair.py` | ~8 | Subentry-Reparatur ✅ |
| `test_coordinator_subentry_visibility.py` | 3 | Sichtbarkeit |
| `test_coordinator_status.py` | ~12 | Status-Management |
| `test_coordinator_semantic_mappings.py` | ~8 | Semantic Labels |
| `test_coordinator_timeout.py` | ~6 | Timeout-Handling |
| `test_coordinator_snapshot.py` | ~5 | Snapshot-Building |
| Weitere... | ~30 | Diverse |

### 1.2 Abdeckung kritischer Funktionen

| Funktion | Komplexität | Tests vorhanden | Lücken |
|----------|-------------|-----------------|--------|
| `_refresh_subentry_index` | 139 | ⚠️ Teilweise | Edge-Cases fehlen |
| `get_active_device_identities` | 128 | ⚠️ Indirekt | Direkte Unit-Tests fehlen |
| `_ensure_registry_for_devices` | 126 | ✅ Gut | - |
| `_ensure_service_device_exists` | 108 | ✅ Gut | - |
| `_async_update_data` | 100 | ⚠️ Teilweise | Error-Paths |
| `update_device_cache` | 45 | ✅ Gut | - |
| `_haversine_distance` | ~5 | ✅ Vorhanden | Mehr Edge-Cases |
| `_is_significant_update` | ~15 | ✅ Gut | - |
| `_merge_with_existing_cache_row` | 37 | ⚠️ Teilweise | Fusion-Logik |

---

## 2. Erforderliche Tests vor Refactoring

### 2.1 Phase 1: `coordinator_geo.py` - NIEDRIG RISIKO

**Benötigte Tests**: `tests/test_coordinator_geo.py`

```python
# Haversine Distance Tests
def test_haversine_distance_same_point():
    """Distance between identical points should be 0."""

def test_haversine_distance_known_cities():
    """Verify with known distances (Berlin-Munich ~504km)."""

def test_haversine_distance_antipodal():
    """Maximum distance (antipodal points) ~20,000km."""

def test_haversine_distance_symmetry():
    """d(A,B) == d(B,A)."""

# Coordinate Normalization Tests
def test_normalize_coords_valid():
    """Valid lat/lon should pass through."""

def test_normalize_coords_out_of_range():
    """Lat > 90 or Lon > 180 should return None."""

def test_normalize_coords_string_input():
    """String numbers should be converted."""

def test_normalize_coords_none_handling():
    """None input should return None."""

# Significance Tests (bereits vorhanden, erweitern)
def test_significant_update_distance_threshold():
    """Movement > threshold is significant."""

def test_significant_update_accuracy_improvement():
    """Better accuracy is significant."""

def test_significant_update_same_location():
    """Same location with same accuracy is not significant."""

# Weighted Fusion Tests
def test_weighted_fusion_single_location():
    """Single location returns unchanged."""

def test_weighted_fusion_accuracy_weighting():
    """More accurate location has higher weight."""

def test_weighted_fusion_equal_accuracy():
    """Equal accuracy results in average."""
```

**Property-Based Tests (Hypothesis)**:
```python
@given(
    lat=st.floats(min_value=-90, max_value=90, allow_nan=False),
    lon=st.floats(min_value=-180, max_value=180, allow_nan=False),
)
def test_haversine_distance_to_self_is_zero(lat, lon):
    assert haversine_distance(lat, lon, lat, lon) == 0

@given(
    lat1=st.floats(min_value=-90, max_value=90, allow_nan=False),
    lon1=st.floats(min_value=-180, max_value=180, allow_nan=False),
    lat2=st.floats(min_value=-90, max_value=90, allow_nan=False),
    lon2=st.floats(min_value=-180, max_value=180, allow_nan=False),
)
def test_haversine_symmetric(lat1, lon1, lat2, lon2):
    d1 = haversine_distance(lat1, lon1, lat2, lon2)
    d2 = haversine_distance(lat2, lon2, lat1, lon1)
    assert abs(d1 - d2) < 0.001

@given(
    lat1=st.floats(min_value=-90, max_value=90, allow_nan=False),
    lon1=st.floats(min_value=-180, max_value=180, allow_nan=False),
    lat2=st.floats(min_value=-90, max_value=90, allow_nan=False),
    lon2=st.floats(min_value=-180, max_value=180, allow_nan=False),
)
def test_haversine_non_negative(lat1, lon1, lat2, lon2):
    assert haversine_distance(lat1, lon1, lat2, lon2) >= 0
```

---

### 2.2 Phase 2: `coordinator_stats.py` - NIEDRIG RISIKO

**Benötigte Tests**: `tests/test_coordinator_stats.py`

```python
# Metric Tests
def test_increment_stat_new_key():
    """New stat starts at 1."""

def test_increment_stat_existing():
    """Existing stat increments."""

def test_safe_update_metric():
    """Metric update works without errors."""

def test_get_metric_missing():
    """Missing metric returns None."""

# Duration Tests
def test_get_duration_valid():
    """Duration between two timestamps."""

def test_get_duration_missing_end():
    """Missing end returns None."""

# Persistence Tests
async def test_async_load_stats_new():
    """Fresh installation has empty stats."""

async def test_async_save_stats():
    """Stats are persisted correctly."""

async def test_debounced_save_stats():
    """Multiple saves are debounced."""

# Error Tracking Tests
def test_append_recent_error():
    """Error is added to list."""

def test_recent_errors_max_size():
    """Error list has max size limit."""

def test_note_error_with_exception():
    """Exception is properly formatted."""

# Diagnostics Buffer Tests
def test_diagnostics_buffer_add_warning():
    """Warning is categorized correctly."""

def test_diagnostics_buffer_add_error():
    """Error is categorized correctly."""

def test_diagnostics_buffer_to_dict():
    """Serialization works."""
```

---

### 2.3 Phase 3: `coordinator_cache.py` - MITTLERES RISIKO

**Benötigte Tests**: `tests/test_coordinator_cache.py`

```python
# Cache Update Tests (erweitern bestehende)
def test_update_cache_new_device():
    """New device is added to cache."""

def test_update_cache_existing_device():
    """Existing device is updated."""

def test_update_cache_stale_data_rejected():
    """Older timestamp is rejected."""

def test_update_cache_metadata_only():
    """Metadata-only update preserves location."""

# Merge Tests
def test_merge_preserves_newer_location():
    """Newer location wins."""

def test_merge_preserves_better_accuracy():
    """Better accuracy wins on same timestamp."""

def test_merge_fusion_overlapping():
    """Overlapping locations are fused."""

# Last Seen Tests
def test_get_device_last_seen_exists():
    """Returns datetime for known device."""

def test_get_device_last_seen_missing():
    """Returns None for unknown device."""

def test_seed_device_last_seen():
    """Seeding sets initial timestamp."""

# Presence Tests
def test_is_device_present_active():
    """Recently seen device is present."""

def test_is_device_present_stale():
    """Old device is not present."""

def test_get_absent_device_ids():
    """Returns list of stale devices."""

# Purge Tests
def test_purge_device():
    """Device is removed from all caches."""
```

---

### 2.4 Phase 4: `coordinator_registry.py` - HOHES RISIKO

**Benötigte Tests**: Erweitern `tests/test_coordinator_device_registry.py`

```python
# Service Device Tests
def test_ensure_service_device_creates():
    """Service device is created if missing."""

def test_ensure_service_device_updates():
    """Service device is updated if changed."""

def test_ensure_service_device_subentry_links():
    """Subentry links are maintained."""

# Tracker Device Tests
def test_ensure_registry_creates_tracker():
    """Tracker device is created."""

def test_ensure_registry_updates_tracker():
    """Tracker device is updated."""

def test_ensure_registry_skips_ignored():
    """Ignored devices are skipped."""

def test_ensure_registry_heals_subentry_links():
    """Broken subentry links are repaired."""

# Entity Lookup Tests
def test_find_tracker_entity_exists():
    """Returns entity for valid device."""

def test_find_tracker_entity_missing():
    """Returns None for unknown device."""

def test_find_tracker_entity_multiple_matches():
    """Handles multiple entity matches."""

# Hub Name Resolution Tests
def test_resolve_hub_name_from_device():
    """Hub name from device entry."""

def test_resolve_hub_name_from_coordinator():
    """Hub name from coordinator cache."""

def test_resolve_hub_name_fallback():
    """Fallback to entry data."""

# Legacy Compatibility Tests
def test_device_registry_legacy_kwargs():
    """Legacy HA versions get correct kwargs."""

def test_device_registry_modern_kwargs():
    """Modern HA versions use new kwargs."""
```

---

### 2.5 Phase 5: `coordinator_subentry.py` - HÖCHSTES RISIKO

**Benötigte Tests**: Erweitern `tests/test_coordinator_subentry_*.py`

```python
# Subentry Index Tests
def test_refresh_index_empty():
    """Empty device list creates empty index."""

def test_refresh_index_single_device():
    """Single device is indexed correctly."""

def test_refresh_index_multiple_subentries():
    """Multiple subentries are handled."""

def test_refresh_index_device_visibility():
    """Device visibility is respected."""

def test_refresh_index_provisional_ids_discarded():
    """Provisional IDs are not persisted."""

# Subentry Metadata Tests
def test_get_subentry_metadata_exists():
    """Returns metadata for valid key."""

def test_get_subentry_metadata_missing():
    """Returns None for unknown key."""

def test_stable_subentry_identifier():
    """Identifier is stable across refreshes."""

# Visibility Tests
def test_is_device_visible_in_subentry():
    """Visible device returns True."""

def test_is_device_not_visible():
    """Hidden device returns False."""

def test_get_device_location_for_subentry():
    """Location data for subentry."""

# Snapshot Tests
def test_group_snapshot_by_subentry():
    """Devices are grouped correctly."""

def test_store_subentry_snapshots():
    """Snapshots are persisted."""

def test_get_subentry_snapshot():
    """Snapshot is retrievable."""

# Core Subentry Definition Tests
def test_build_core_subentry_definitions():
    """Default subentries are created."""

def test_schedule_core_subentry_repair():
    """Missing subentries trigger repair."""

def test_cancel_pending_repair():
    """Pending repair can be cancelled."""
```

---

## 3. Test-Ausführungs-Strategie

### 3.1 Vor jedem Refactoring-Schritt

```bash
# 1. Alle bestehenden Tests müssen bestehen
python3.13 -m pytest tests/test_coordinator*.py -v

# 2. Coverage für betroffene Funktionen prüfen
python3.13 -m pytest tests/test_coordinator*.py \
    --cov=custom_components.googlefindmy.coordinator \
    --cov-report=term-missing

# 3. Neue Tests für zu extrahierende Funktionen schreiben
python3.13 -m pytest tests/test_coordinator_geo.py -v

# 4. Smoke-Test der Integration
python3.13 -m pytest tests/ -k "coordinator" --tb=short
```

### 3.2 Nach jedem Refactoring-Schritt

```bash
# 1. Alle Tests müssen weiterhin bestehen
python3.13 -m pytest tests/test_coordinator*.py -v

# 2. Neue Modul-Tests müssen bestehen
python3.13 -m pytest tests/test_coordinator_geo.py -v

# 3. Full CI-Check
ruff check . && mypy --strict custom_components/googlefindmy && pytest
```

---

## 4. Risiko-Matrix mit Test-Anforderungen

| Phase | Modul | Risiko | Min. Tests vor Start | Kritische Pfade |
|-------|-------|--------|---------------------|-----------------|
| 1 | coordinator_geo.py | Niedrig | 15 | Haversine, Normalize |
| 2 | coordinator_stats.py | Niedrig | 20 | Persistence, Metrics |
| 3 | coordinator_cache.py | Mittel | 25 | Update, Merge, Fusion |
| 4 | coordinator_registry.py | Hoch | 40 | Device-Creation, Links |
| 5 | coordinator_subentry.py | Sehr Hoch | 30 | Index-Refresh, Visibility |

---

## 5. Definition of Done (pro Phase)

- [ ] Alle vorhandenen Tests bestehen (0 Failures)
- [ ] Neue Unit-Tests für extrahierte Funktionen geschrieben
- [ ] Property-Based Tests für mathematische Funktionen
- [ ] Coverage für extrahiertes Modul > 90%
- [ ] Keine neuen mypy/ruff Errors
- [ ] Code-Review durchgeführt
- [ ] Integration-Test bestanden

---

## 6. Nächste Schritte

1. [ ] `tests/test_coordinator_geo.py` erstellen mit ~15 Tests
2. [ ] Property-Based Tests für Geo-Funktionen hinzufügen
3. [ ] Bestehende Tests ausführen als Baseline
4. [ ] Erst dann Phase 1 Extraktion starten

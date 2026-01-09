# Coordinator.py Refactoring Plan

**Datei**: `custom_components/googlefindmy/coordinator.py`
**Aktuelle Größe**: 8557 Zeilen (Stand: 2026-01-09)
**Zielgröße**: ~2000-2500 Zeilen im Hauptmodul
**Branch**: `claude/1.7.0-3-refactoring-KVUvG`

---

## ⚠️ KRITISCHE ANFORDERUNG: TDD MIT 100% TEST-COVERAGE

> **VOR jeder Refactoring-Phase müssen folgende Bedingungen erfüllt sein:**
>
> 1. **100% Test-Coverage** für alle zu extrahierenden Funktionen
> 2. **Risiko-fokussierte Tests**: Edge-Cases, Fehlerbehandlung, Grenzwerte
> 3. **Keine Extraktion ohne vorherige Tests**
>
> Siehe [PRE_REFACTORING_TEST_PLAN.md](PRE_REFACTORING_TEST_PLAN.md) für Details.

---

## Fortschritt

### ✅ Abgeschlossene Phasen (1-14)

| Phase | Modul/Funktion | Status | Tests | Komplexität |
|-------|----------------|--------|-------|-------------|
| 1 | `coordinator_geo.py` | ✅ Fertig | 52 Tests | A (Ø 2.5) |
| 2 | `coordinator_stats.py` | ✅ Fertig | 63 Tests | A (Ø 1.8) |
| 3 | `coordinator_cache.py` | ✅ Fertig | 48 Tests | A (Ø 5.0) |
| 4 | `coordinator_registry.py` | ✅ Fertig | 56 Tests | A (Ø 6.75) |
| 5 | `coordinator_subentry.py` | ✅ Fertig | 70 Tests | A (Ø 4.6) |
| 6 | `_refresh_subentry_index` → Helpers | ✅ Fertig | +35 Tests | B (Ø 5.1) |
| 7 | `get_active_device_identities` → `coordinator_identity.py` | ✅ Fertig | 96 Tests | B (Ø 6.2) |
| 8 | `_ensure_registry_for_devices` → `coordinator_registry.py` | ✅ Fertig | +42 Tests | B (Ø 5.9) |
| 9 | `_ensure_service_device_exists` → `coordinator_registry.py` | ✅ Fertig | +54 Tests | B (Ø 5.9) |
| 10 | `_async_update_data` → `coordinator_update.py` | ✅ Fertig | 52 Tests | A (Ø 5.0) |
| 11 | `_async_start_poll_cycle` → `coordinator_polling.py` | ✅ Fertig | 55 Tests | A (Ø 4.17) |
| 12 | `_find_tracker_entity_entry` → `coordinator_registry.py` | ✅ Fertig | 54 Tests | B (Ø 7.19) |
| 13 | `update_device_cache` → `coordinator_cache.py` | ✅ Fertig | 64 Tests | B (Ø 6.23) |
| 14 | `async_locate_device` → `coordinator_locate.py` | ✅ Fertig | 59 Tests | B (Ø 7.14) |

**Gesamt Phase 1-14: 800 Tests, 53 extrahierte Pure Functions, Durchschnittskomplexität A-B**

### 🚧 Ausstehende Phasen (15): Verbleibende High-Complexity Funktionen

| Phase | Funktion | Komplexität | Zielmodul | Status |
|-------|----------|-------------|-----------|--------|
| 15 | `_merge_with_existing_cache_row` | E (37) | Neue Helpers → `coordinator_cache.py` | ⏳ Pending |

---

## Aktuelle Komplexitäts-Verteilung (coordinator*.py)

```
Grade A (1-5):   154 (66.7%) ████████████████████
Grade B (6-10):   45 (19.5%) ██████
Grade C (11-20):  21 ( 9.1%) ███
Grade D (21-30):   1 ( 0.4%)
Grade E (31-40):   2 ( 0.9%)
Grade F (41+):     8 ( 3.5%) █
──────────────────────────────
Total:           231 Funktionen
```

---

## Phase 11: `_async_start_poll_cycle` (Komplexität 62 → Ziel: <20)

**Zeilen**: 6054-6497 (~443 Zeilen)
**Verantwortung**: Sequentielles Polling aller Geräte mit Throttling und Error-Handling

### 11.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_polling.py

class TestAsyncStartPollCycleRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: Concurrent Access
    def test_poll_cycle_lock_prevents_overlap(self):
        """Parallele Poll-Cycles werden verhindert."""

    def test_poll_cycle_releases_lock_on_error(self):
        """Lock wird bei Fehler freigegeben."""

    # RISIKO: Device-Iteration
    def test_poll_cycle_handles_empty_device_list(self):
        """Leere Device-Liste wird graceful behandelt."""

    def test_poll_cycle_skips_unavailable_devices(self):
        """Nicht erreichbare Geräte werden übersprungen."""

    def test_poll_cycle_continues_after_single_device_error(self):
        """Fehler bei einem Gerät stoppt nicht den gesamten Cycle."""

    # RISIKO: Throttling
    def test_poll_cycle_applies_cooldown(self):
        """Per-Device Cooldown wird angewendet."""

    def test_poll_cycle_respects_in_all_areas_hint(self):
        """in_all_areas (~10min throttle) wird respektiert."""

    def test_poll_cycle_respects_high_traffic_hint(self):
        """high_traffic (~5min throttle) wird respektiert."""

    # RISIKO: Location Updates
    def test_poll_cycle_updates_location_cache(self):
        """Location-Cache wird aktualisiert."""

    def test_poll_cycle_triggers_entity_updates(self):
        """Entity-Updates werden getriggert."""

    # RISIKO: FCM-Integration
    def test_poll_cycle_respects_fcm_ready_state(self):
        """Poll wartet auf FCM wenn nicht ready."""

    def test_poll_cycle_force_overrides_fcm_check(self):
        """Force-Flag überschreibt FCM-Check."""
```

### 11.2 Neues Modul: `coordinator_polling.py`

| Funktion | Komplexität | Beschreibung |
|----------|-------------|--------------|
| `calculate_device_cooldown` | A (~4) | Berechnet Cooldown basierend auf report_hint |
| `should_apply_throttle` | A (~3) | Prüft ob Throttling angewendet werden soll |
| `extract_report_hint` | A (~2) | Extrahiert report_hint aus Location-Response |
| `filter_pollable_devices` | B (~6) | Filtert Geräte die gepollt werden können |
| `build_poll_result_summary` | A (~4) | Erstellt Zusammenfassung des Poll-Cycles |
| `classify_poll_error` | B (~7) | Klassifiziert Fehlertypen für Retry-Logik |

### 11.3 Extraktion-Strategie

```python
# coordinator_polling.py - Pure Functions

def calculate_device_cooldown(
    report_hint: str | None,
    base_interval: float,
    min_cooldown: float = 60.0,
) -> float:
    """Berechnet Cooldown basierend auf Server-Hint."""
    if report_hint == "in_all_areas":
        return max(600.0, base_interval)  # ~10 min
    if report_hint == "high_traffic":
        return max(300.0, base_interval)  # ~5 min
    return min_cooldown

def filter_pollable_devices(
    devices: list[dict[str, Any]],
    cooldown_until: dict[str, float],
    now_mono: float,
) -> list[dict[str, Any]]:
    """Filtert Geräte deren Cooldown abgelaufen ist."""
    return [
        d for d in devices
        if now_mono >= cooldown_until.get(d["id"], 0.0)
    ]
```

---

## Phase 12: `_find_tracker_entity_entry` (Komplexität 50 → Ziel: <15)

**Zeilen**: 2714-2981 (~267 Zeilen)
**Verantwortung**: Findet Entity-Registry-Einträge für Tracker, migriert Legacy-IDs

### 12.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_registry_entity.py

class TestFindTrackerEntityEntryRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: ID-Auflösung
    def test_finds_entity_by_device_id(self):
        """Entity wird über Device-ID gefunden."""

    def test_finds_entity_by_registry_uuid(self):
        """Entity wird über Registry-UUID gefunden."""

    def test_handles_missing_entity(self):
        """Fehlende Entity gibt None zurück."""

    # RISIKO: Legacy-Migration
    def test_migrates_legacy_unique_id(self):
        """Legacy unique_id wird migriert."""

    def test_preserves_modern_unique_id(self):
        """Moderne unique_ids bleiben unverändert."""

    def test_handles_ambiguous_unique_ids(self):
        """Mehrdeutige IDs werden korrekt aufgelöst."""

    # RISIKO: Multi-Account
    def test_respects_entry_id_scope(self):
        """Nur Entities des aktuellen Entries werden gefunden."""

    def test_handles_cross_account_device_ids(self):
        """Gleiche Device-IDs in verschiedenen Accounts."""

    # RISIKO: Identifier-Parsing
    def test_extracts_identifier_from_device(self):
        """Identifier wird aus Device-Entry extrahiert."""

    def test_handles_malformed_identifiers(self):
        """Fehlerhafte Identifiers crashen nicht."""
```

### 12.2 Erweiterung von `coordinator_registry.py`

| Funktion | Komplexität | Beschreibung |
|----------|-------------|--------------|
| `extract_canonical_device_id` | B (~6) | Extrahiert kanonische Device-ID aus Registry |
| `build_entity_unique_id_candidates` | B (~7) | Generiert mögliche unique_id Varianten |
| `detect_legacy_unique_id` | A (~4) | Erkennt Legacy unique_id Format |
| `build_migration_unique_id` | A (~3) | Erstellt neue unique_id für Migration |

```python
# coordinator_registry.py - Neue Funktionen

def extract_canonical_device_id(
    registry_device: Any,
    domain: str,
) -> str | None:
    """Extrahiert kanonische Device-ID aus Registry-Entry."""
    identifiers = getattr(registry_device, "identifiers", set())
    for ident in identifiers:
        if isinstance(ident, tuple) and len(ident) == 2:
            if ident[0] == domain and not ident[1].startswith("integration_"):
                return ident[1]
    return None

def build_entity_unique_id_candidates(
    device_id: str,
    entry_id: str,
    entity_type: str,
) -> list[str]:
    """Generiert alle möglichen unique_id Varianten für Entity-Lookup."""
    return [
        f"{device_id}_{entity_type}",           # Modern
        f"{entry_id}:{device_id}_{entity_type}", # Namespaced
        f"{device_id}",                          # Legacy (tracker only)
    ]
```

---

## Phase 13: `update_device_cache` (Komplexität 45 → Ziel: <15)

**Zeilen**: 7077-7289 (~212 Zeilen)
**Verantwortung**: Aktualisiert Device-Location-Cache mit neuen Daten

### 13.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_cache_update.py

class TestUpdateDeviceCacheRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: Daten-Validierung
    def test_validates_coordinates(self):
        """Ungültige Koordinaten werden abgelehnt."""

    def test_handles_missing_required_fields(self):
        """Fehlende Pflichtfelder führen zu Skip."""

    def test_normalizes_coordinate_format(self):
        """Koordinaten werden normalisiert (float)."""

    # RISIKO: Cache-Konsistenz
    def test_preserves_existing_data_on_partial_update(self):
        """Partial Updates überschreiben nicht alles."""

    def test_clears_stale_data_on_full_update(self):
        """Full Updates ersetzen alte Daten."""

    # RISIKO: Timestamp-Handling
    def test_updates_last_seen_timestamp(self):
        """Last-seen Timestamp wird aktualisiert."""

    def test_preserves_older_accuracy_if_better(self):
        """Bessere Accuracy wird beibehalten."""

    # RISIKO: Significance Detection
    def test_detects_significant_location_change(self):
        """Signifikante Änderungen werden erkannt."""

    def test_ignores_insignificant_changes(self):
        """Kleine Änderungen triggern kein Update."""

    # RISIKO: Propagation
    def test_propagates_to_shared_devices(self):
        """Updates werden an Shared-Devices propagiert."""
```

### 13.2 Erweiterung von `coordinator_cache.py`

| Funktion | Komplexität | Beschreibung |
|----------|-------------|--------------|
| `validate_location_data` | B (~7) | Validiert Location-Daten |
| `merge_location_update` | B (~8) | Merged neue Location mit bestehenden Daten |
| `detect_significant_change` | B (~6) | Erkennt signifikante Location-Änderungen |
| `select_best_accuracy` | A (~4) | Wählt beste Accuracy aus mehreren Quellen |
| `normalize_location_fields` | A (~5) | Normalisiert Location-Felder |

```python
# coordinator_cache.py - Neue Funktionen

def validate_location_data(
    data: dict[str, Any],
    required_fields: tuple[str, ...] = ("latitude", "longitude"),
) -> bool:
    """Validiert ob Location-Daten verwendbar sind."""
    for field in required_fields:
        value = data.get(field)
        if value is None:
            return False
        if not isinstance(value, (int, float)):
            return False
    return True

def merge_location_update(
    existing: dict[str, Any] | None,
    update: dict[str, Any],
    preserve_better_accuracy: bool = True,
) -> dict[str, Any]:
    """Merged neues Location-Update mit bestehenden Daten."""
    if existing is None:
        return dict(update)

    result = dict(existing)
    result.update(update)

    if preserve_better_accuracy:
        old_acc = existing.get("accuracy")
        new_acc = update.get("accuracy")
        if old_acc is not None and new_acc is not None:
            if old_acc < new_acc:  # Lower is better
                result["accuracy"] = old_acc

    return result
```

---

## Phase 14: `async_locate_device` (Komplexität 40 → Ziel: <15)

**Zeilen**: 8160-8433 (~273 Zeilen)
**Verantwortung**: Einzelnes Device lokalisieren (API-Call + Caching)

### 14.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_locate.py

class TestAsyncLocateDeviceRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: API-Fehler
    def test_handles_api_timeout(self):
        """Timeout wird graceful behandelt."""

    def test_handles_api_not_found(self):
        """404 gibt None zurück."""

    def test_handles_api_auth_error(self):
        """Auth-Fehler propagiert korrekt."""

    # RISIKO: Response-Parsing
    def test_parses_valid_location_response(self):
        """Gültige Response wird korrekt geparst."""

    def test_handles_malformed_response(self):
        """Fehlerhafte Response crasht nicht."""

    def test_handles_empty_location(self):
        """Leere Location (null island) wird erkannt."""

    # RISIKO: Caching
    def test_updates_cache_on_success(self):
        """Cache wird bei Erfolg aktualisiert."""

    def test_does_not_cache_error_response(self):
        """Fehler werden nicht gecacht."""

    # RISIKO: EID-Resolution
    def test_resolves_eid_for_e2ee_device(self):
        """EID wird für E2EE-Geräte aufgelöst."""

    def test_handles_eid_resolution_failure(self):
        """EID-Fehler führt zu Fallback."""

    # RISIKO: Concurrency
    def test_deduplicates_concurrent_requests(self):
        """Parallele Requests werden dedupliziert."""
```

### 14.2 Neues Modul: `coordinator_locate.py`

| Funktion | Komplexität | Beschreibung |
|----------|-------------|--------------|
| `parse_locate_response` | B (~8) | Parsed API Location-Response |
| `validate_locate_result` | B (~6) | Validiert Location-Ergebnis |
| `detect_null_island` | A (~3) | Erkennt ungültige (0,0) Koordinaten |
| `build_locate_request_params` | A (~5) | Erstellt API-Request Parameter |
| `classify_locate_error` | B (~7) | Klassifiziert Locate-Fehler |

```python
# coordinator_locate.py - Pure Functions

def parse_locate_response(
    response: dict[str, Any],
) -> dict[str, Any] | None:
    """Parsed Location aus API-Response."""
    location = response.get("location")
    if not isinstance(location, dict):
        return None

    lat = location.get("latitude")
    lon = location.get("longitude")

    if lat is None or lon is None:
        return None

    return {
        "latitude": float(lat),
        "longitude": float(lon),
        "accuracy": location.get("accuracy"),
        "timestamp": location.get("timestamp"),
        "source": location.get("source", "api"),
    }

def detect_null_island(
    latitude: float,
    longitude: float,
    threshold: float = 0.0001,
) -> bool:
    """Erkennt ungültige (0,0) oder near-zero Koordinaten."""
    return abs(latitude) < threshold and abs(longitude) < threshold
```

---

## Phase 15: `_merge_with_existing_cache_row` (Komplexität 37 → Ziel: <12)

**Zeilen**: 7543-7660 (~117 Zeilen)
**Verantwortung**: Merged neue Location-Daten mit existierendem Cache-Eintrag

### 15.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_cache_merge.py

class TestMergeWithExistingCacheRowRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: Field-Priorität
    def test_new_data_overwrites_old(self):
        """Neue Daten überschreiben alte."""

    def test_preserves_fields_not_in_update(self):
        """Nicht aktualisierte Felder bleiben erhalten."""

    # RISIKO: Accuracy-Handling
    def test_keeps_better_accuracy(self):
        """Bessere Accuracy wird beibehalten."""

    def test_replaces_none_accuracy(self):
        """None Accuracy wird durch Wert ersetzt."""

    # RISIKO: Timestamp-Logik
    def test_newer_timestamp_wins(self):
        """Neuerer Timestamp überschreibt älteren."""

    def test_handles_missing_timestamps(self):
        """Fehlende Timestamps werden graceful behandelt."""

    # RISIKO: Source-Tracking
    def test_tracks_data_source(self):
        """Datenquelle wird korrekt getrackt."""

    def test_prefers_api_over_fcm(self):
        """API-Daten haben Priorität über FCM."""

    # RISIKO: Edge-Cases
    def test_handles_empty_existing_row(self):
        """Leerer bestehender Eintrag."""

    def test_handles_empty_new_data(self):
        """Leere neue Daten."""
```

### 15.2 Erweiterung von `coordinator_cache.py`

| Funktion | Komplexität | Beschreibung |
|----------|-------------|--------------|
| `compare_location_freshness` | B (~6) | Vergleicht Timestamps zweier Locations |
| `select_best_location_source` | A (~4) | Wählt beste Quelle basierend auf Priorität |
| `merge_cache_fields` | B (~7) | Merged einzelne Felder mit Konflikt-Resolution |

```python
# coordinator_cache.py - Neue Funktionen

def compare_location_freshness(
    existing: dict[str, Any],
    new: dict[str, Any],
) -> int:
    """Vergleicht Timestamps. Returns: -1 (existing newer), 0 (equal), 1 (new newer)."""
    existing_ts = existing.get("timestamp")
    new_ts = new.get("timestamp")

    if existing_ts is None and new_ts is None:
        return 0
    if existing_ts is None:
        return 1
    if new_ts is None:
        return -1

    if new_ts > existing_ts:
        return 1
    if new_ts < existing_ts:
        return -1
    return 0

SOURCE_PRIORITY = {"api": 10, "poll": 8, "fcm": 5, "cache": 2, "unknown": 0}

def select_best_location_source(
    source_a: str | None,
    source_b: str | None,
) -> str:
    """Wählt bessere Datenquelle basierend auf Priorität."""
    prio_a = SOURCE_PRIORITY.get(source_a or "unknown", 0)
    prio_b = SOURCE_PRIORITY.get(source_b or "unknown", 0)

    if prio_a >= prio_b:
        return source_a or "unknown"
    return source_b or "unknown"
```

---

## Komplexitäts-Budget pro Modul (Aktualisiert)

| Modul | Aktuelle Komplexität | Budget (max) | Phase |
|-------|---------------------|--------------|-------|
| `coordinator_geo.py` | A (Ø 2.5) | C (Ø <15) | 1 |
| `coordinator_stats.py` | A (Ø 1.8) | B (Ø <10) | 2 |
| `coordinator_cache.py` | A (Ø 5.0) | C (Ø <15) | 3, 13, 15 |
| `coordinator_registry.py` | B (Ø 5.9) | C (Ø <15) | 4, 8, 9, 12 |
| `coordinator_subentry.py` | A (Ø 4.6) | C (Ø <15) | 5, 6 |
| `coordinator_identity.py` | B (Ø 6.2) | C (Ø <15) | 7 |
| `coordinator_update.py` | A (Ø 5.0) | B (Ø <10) | 10 |
| `coordinator_polling.py` (neu) | - | B (Ø <10) | 11 |
| `coordinator_locate.py` (neu) | - | B (Ø <10) | 14 |

**Regel**: Keine Funktion mit Komplexität > C (20) in Helper-Modulen.

---

## TDD-Checkliste pro Phase

### Vor der Extraktion

- [ ] Funktion verstanden und dokumentiert
- [ ] Alle Eingabe-Varianten identifiziert
- [ ] Alle Risiken und Edge-Cases identifiziert
- [ ] Tests für jeden Risiko-Fall geschrieben
- [ ] 100% Branch-Coverage erreicht
- [ ] Tests bestehen

### Während der Extraktion

- [ ] Pure Helper-Funktionen identifiziert
- [ ] Helper-Funktionen extrahiert
- [ ] Unit-Tests für neue Helper geschrieben
- [ ] Orchestrierung vereinfacht
- [ ] Alle bestehenden Tests bestehen weiterhin

### Nach der Extraktion

- [ ] Radon-Analyse zeigt Komplexitäts-Reduktion
- [ ] Modul-Komplexität unter Budget
- [ ] Full Test-Suite bestanden
- [ ] ruff check + mypy bestanden
- [ ] Code-Review durchgeführt

---

## Metriken-Ziele (Aktualisiert)

| Metrik | Phase 1-5 | Phase 6-10 | Nach Phase 11-15 |
|--------|-----------|------------|------------------|
| coordinator.py Zeilen | 8532 | 8557 | ~6000 |
| Max. Komplexität | 139 | 139 | <30 |
| Funktionen mit F-Rating | 8 | 8 | 3 |
| Funktionen mit E-Rating | 2 | 2 | 0 |
| Extrahierte Tests | 289 | 568 | ~750 |
| Helper-Module | 5 | 7 | 9 |

---

## Nächste Schritte

1. [ ] Phase 11 Tests schreiben (`_async_start_poll_cycle`)
2. [ ] 100% Coverage für Phase 11 erreichen
3. [ ] Pure Helpers nach `coordinator_polling.py` extrahieren
4. [ ] Komplexität validieren
5. [ ] Weiter mit Phase 12-15

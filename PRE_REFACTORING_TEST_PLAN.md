# Pre-Refactoring Test Plan

**Ziel**: Sicherstellen, dass alle kritischen Funktionen vor dem Refactoring **100% Test-Coverage** haben, mit besonderem Fokus auf Risiken und Edge-Cases.

---

## ⚠️ KRITISCHE ANFORDERUNG: 100% TEST-COVERAGE

> **KEINE Extraktion ohne vorherige Tests!**
>
> Jede zu extrahierende Funktion MUSS vor dem Refactoring:
> 1. **100% Branch-Coverage** haben
> 2. **Risiko-fokussierte Tests** für alle Edge-Cases
> 3. **Alle Tests müssen bestehen**

---

## 1. Aktuelle Test-Abdeckung

### 1.1 Abgeschlossene Phasen (1-5)

| Modul | Tests | Coverage | Status |
|-------|-------|----------|--------|
| `coordinator_geo.py` | 52 | 100% | ✅ Fertig |
| `coordinator_stats.py` | 63 | 100% | ✅ Fertig |
| `coordinator_cache.py` | 48 | 100% | ✅ Fertig |
| `coordinator_registry.py` | 56 | 100% | ✅ Fertig |
| `coordinator_subentry.py` | 70 | 100% | ✅ Fertig |

### 1.2 Ausstehende Phasen (6-10) - Coverage-Status

| Funktion | Komplexität | Aktuelle Coverage | Ziel | Lücken |
|----------|-------------|-------------------|------|--------|
| `_refresh_subentry_index` | 139 | ⚠️ ~60% | 100% | Edge-Cases, Race-Conditions |
| `get_active_device_identities` | 128 | ⚠️ ~50% | 100% | Multi-Account, Key-Priority |
| `_ensure_registry_for_devices` | 126 | ⚠️ ~70% | 100% | Link-Healing, Legacy-Compat |
| `_ensure_service_device_exists` | 108 | ⚠️ ~65% | 100% | Multi-Account, Updates |
| `_async_update_data` | 100 | ⚠️ ~55% | 100% | Error-Paths, State-Updates |

---

## 2. Risiko-Kategorien und Test-Anforderungen

### 2.1 Eingabe-Validierung (RISIKO: Crash)

```python
# MUSS getestet werden:
- Leere Listen/Dicts
- None-Werte
- Fehlende Keys in Dicts
- Ungültige Typen
- Sehr große Datenmengen (Performance)
```

### 2.2 State-Konsistenz (RISIKO: Daten-Korruption)

```python
# MUSS getestet werden:
- State vor/nach Operation
- Parallele Aufrufe (Race-Conditions)
- Partial Failure (Rollback?)
- Idempotenz (mehrfacher Aufruf = gleicher State)
```

### 2.3 Fehlerbehandlung (RISIKO: Silent Failures)

```python
# MUSS getestet werden:
- Exception-Handling
- Timeout-Verhalten
- Rate-Limiting
- Auth-Fehler
- Netzwerk-Fehler
```

### 2.4 Kompatibilität (RISIKO: Regression)

```python
# MUSS getestet werden:
- Legacy-HA-Versionen
- Multi-Account-Szenarien
- Migration von alten Daten
- Backwards-Compatibility
```

---

## 3. Test-Anforderungen pro Phase

### Phase 6: `_refresh_subentry_index` (Komplexität 139)

**Test-Datei**: `tests/test_coordinator_subentry_index.py`

**Mindest-Tests (15)**:

```python
class TestRefreshSubentryIndexRisks:
    """100% Coverage für _refresh_subentry_index."""

    # === EINGABE-VALIDIERUNG ===
    def test_empty_device_list_returns_empty_index(self): ...
    def test_none_in_device_list_filtered(self): ...
    def test_malformed_device_dict_skipped(self): ...
    def test_missing_required_keys_handled(self): ...

    # === SUBENTRY-ID-LOGIK ===
    def test_extracts_subentry_from_device(self): ...
    def test_duplicate_subentry_ids_deduplicated(self): ...
    def test_provisional_ids_not_persisted(self): ...
    def test_stable_subentry_identifier_consistency(self): ...

    # === STATE-KONSISTENZ ===
    def test_preserves_existing_metadata_for_known_keys(self): ...
    def test_removes_stale_entries_not_in_devices(self): ...
    def test_concurrent_refresh_calls_safe(self): ...
    def test_refresh_idempotent(self): ...

    # === PERFORMANCE ===
    def test_handles_100_plus_devices(self): ...

    # === LISTENER-BENACHRICHTIGUNG ===
    def test_notifies_listeners_on_change(self): ...
    def test_no_notification_when_unchanged(self): ...
```

**Coverage-Ziel**: 100% Branch-Coverage

---

### Phase 7: `get_active_device_identities` (Komplexität 128)

**Test-Datei**: `tests/test_coordinator_identity.py`

**Mindest-Tests (12)**:

```python
class TestGetActiveDeviceIdentitiesRisks:
    """100% Coverage für get_active_device_identities."""

    # === KEY-AUSWAHL ===
    def test_prefers_primary_key(self): ...
    def test_fallback_to_secondary_key(self): ...
    def test_no_valid_key_skips_device(self): ...
    def test_key_priority_order_respected(self): ...

    # === MULTI-ACCOUNT ===
    def test_multiple_accounts_handled(self): ...
    def test_account_isolation_enforced(self): ...
    def test_cross_account_device_id_collision(self): ...

    # === DEVICE-STATUS ===
    def test_skips_disabled_devices(self): ...
    def test_skips_ignored_devices(self): ...
    def test_includes_enabled_devices(self): ...

    # === EID-INTEGRATION ===
    def test_includes_eid_when_available(self): ...
    def test_handles_missing_eid(self): ...
```

**Coverage-Ziel**: 100% Branch-Coverage

---

### Phase 8: `_ensure_registry_for_devices` (Komplexität 126)

**Test-Datei**: `tests/test_coordinator_registry_ensure.py`

**Mindest-Tests (15)**:

```python
class TestEnsureRegistryForDevicesRisks:
    """100% Coverage für _ensure_registry_for_devices."""

    # === DEVICE-ERSTELLUNG ===
    def test_creates_new_tracker_device(self): ...
    def test_updates_existing_device(self): ...
    def test_skips_ignored_device(self): ...
    def test_returns_created_count(self): ...

    # === SUBENTRY-LINKS ===
    def test_creates_subentry_link(self): ...
    def test_heals_broken_subentry_link(self): ...
    def test_removes_orphaned_hub_link(self): ...
    def test_preserves_valid_links(self): ...

    # === LEGACY-KOMPATIBILITÄT ===
    def test_legacy_kwargs_for_old_ha(self): ...
    def test_modern_kwargs_for_new_ha(self): ...
    def test_retry_with_legacy_on_typeerror(self): ...

    # === NAME-RESOLUTION ===
    def test_resolves_hub_name_from_device(self): ...
    def test_resolves_hub_name_from_cache(self): ...
    def test_resolves_hub_name_fallback(self): ...

    # === ERROR-HANDLING ===
    def test_handles_registry_api_error(self): ...
```

**Coverage-Ziel**: 100% Branch-Coverage

---

### Phase 9: `_ensure_service_device_exists` (Komplexität 108)

**Test-Datei**: `tests/test_coordinator_registry_service_device.py`

**Mindest-Tests (10)**:

```python
class TestEnsureServiceDeviceExistsRisks:
    """100% Coverage für _ensure_service_device_exists."""

    # === ERSTELLUNG ===
    def test_creates_service_device_if_missing(self): ...
    def test_does_not_duplicate_service_device(self): ...

    # === UPDATES ===
    def test_updates_service_device_name(self): ...
    def test_no_update_when_unchanged(self): ...

    # === MULTI-ACCOUNT ===
    def test_creates_per_account_service_device(self): ...
    def test_service_device_identifiers_unique(self): ...

    # === SUBENTRY-LINKS ===
    def test_service_device_subentry_link(self): ...
    def test_service_device_config_entry_link(self): ...

    # === ERROR-HANDLING ===
    def test_handles_registry_error(self): ...
    def test_handles_missing_entry_data(self): ...
```

**Coverage-Ziel**: 100% Branch-Coverage

---

### Phase 10: `_async_update_data` (Komplexität 100)

**Test-Datei**: `tests/test_coordinator_update_data.py`

**Mindest-Tests (12)**:

```python
class TestAsyncUpdateDataRisks:
    """100% Coverage für _async_update_data."""

    # === API-FEHLER ===
    def test_handles_api_timeout(self): ...
    def test_handles_api_auth_error(self): ...
    def test_handles_api_rate_limit(self): ...
    def test_handles_api_network_error(self): ...

    # === DATEN-VERARBEITUNG ===
    def test_processes_new_devices(self): ...
    def test_updates_existing_devices(self): ...
    def test_handles_empty_response(self): ...
    def test_handles_malformed_response(self): ...

    # === STATE-UPDATES ===
    def test_updates_last_successful_poll(self): ...
    def test_triggers_entity_updates(self): ...
    def test_increments_poll_counter(self): ...

    # === FCM-INTEGRATION ===
    def test_respects_fcm_ready_state(self): ...
```

**Coverage-Ziel**: 100% Branch-Coverage

---

## 4. Test-Ausführungs-Strategie

### 4.1 Coverage-Messung

```bash
# Coverage für spezifische Funktion messen
python3.13 -m pytest tests/test_coordinator*.py \
    --cov=custom_components.googlefindmy.coordinator \
    --cov-report=term-missing \
    --cov-report=html

# Nur bestimmte Zeilen prüfen (z.B. Zeile 1214-1400 für _refresh_subentry_index)
python3.13 -m pytest tests/test_coordinator_subentry_index.py -v \
    --cov=custom_components.googlefindmy.coordinator \
    --cov-report=term-missing
```

### 4.2 Branch-Coverage prüfen

```bash
# Branch-Coverage aktivieren
python3.13 -m pytest tests/ \
    --cov=custom_components.googlefindmy.coordinator \
    --cov-branch \
    --cov-report=term-missing
```

### 4.3 Vor jedem Refactoring-Schritt

```bash
# 1. Alle Tests bestehen
python3.13 -m pytest tests/test_coordinator*.py -v

# 2. Coverage für zu extrahierende Funktion = 100%
python3.13 -m pytest tests/test_coordinator_subentry_index.py \
    --cov=custom_components.googlefindmy.coordinator \
    --cov-branch \
    --cov-fail-under=100

# 3. Komplexitäts-Check
python3.13 -m radon cc custom_components/googlefindmy/coordinator.py -s --min D
```

---

## 5. Definition of Done (Test-Phase)

### Checkliste pro zu extrahierende Funktion

- [ ] Test-Datei erstellt
- [ ] Alle Risiko-Kategorien abgedeckt:
  - [ ] Eingabe-Validierung
  - [ ] State-Konsistenz
  - [ ] Fehlerbehandlung
  - [ ] Kompatibilität
- [ ] 100% Branch-Coverage erreicht
- [ ] Alle Tests bestehen
- [ ] Property-Based Tests (Hypothesis) für mathematische Logik
- [ ] Performance-Tests für große Datenmengen

### Checkliste nach Extraktion

- [ ] Alle vorherigen Tests bestehen weiterhin
- [ ] Neue Unit-Tests für extrahierte Helper
- [ ] Coverage für neue Helper = 100%
- [ ] Radon zeigt Komplexitäts-Reduktion

---

## 6. Risiko-Matrix

| Phase | Funktion | Risiko | Min. Tests | Kritische Pfade |
|-------|----------|--------|-----------|-----------------|
| 6 | `_refresh_subentry_index` | SEHR HOCH | 15 | Subentry-Logik, State |
| 7 | `get_active_device_identities` | HOCH | 12 | Key-Priority, Multi-Account |
| 8 | `_ensure_registry_for_devices` | HOCH | 15 | Registry-API, Links |
| 9 | `_ensure_service_device_exists` | MITTEL | 10 | Service-Device |
| 10 | `_async_update_data` | HOCH | 12 | API, Error-Handling |

---

## 7. Nächste Schritte

1. [ ] `tests/test_coordinator_subentry_index.py` erstellen
2. [ ] 100% Coverage für `_refresh_subentry_index` erreichen
3. [ ] Alle 15 Mindest-Tests bestehen
4. [ ] Erst dann Phase 6 Extraktion starten
5. [ ] Prozess für Phase 7-10 wiederholen

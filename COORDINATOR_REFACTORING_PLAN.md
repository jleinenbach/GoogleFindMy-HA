# Coordinator.py Refactoring Plan

**Datei**: `custom_components/googlefindmy/coordinator.py`
**Aktuelle Größe**: 8532 Zeilen (Stand: 2026-01-09)
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

### ✅ Abgeschlossene Phasen (1-5)

| Phase | Modul | Status | Tests | Komplexität |
|-------|-------|--------|-------|-------------|
| 1 | `coordinator_geo.py` | ✅ Fertig | 52 Tests | A (Ø 2.5) |
| 2 | `coordinator_stats.py` | ✅ Fertig | 63 Tests | A (Ø 1.8) |
| 3 | `coordinator_cache.py` | ✅ Fertig | 48 Tests | A (Ø 5.0) |
| 4 | `coordinator_registry.py` | ✅ Fertig | 56 Tests | A (Ø 6.75) |
| 5 | `coordinator_subentry.py` | ✅ Fertig | 70 Tests | A (Ø 4.6) |

**Gesamt: 289 Tests, 956 extrahierte Zeilen, Durchschnittskomplexität A (3.59)**

### 🚧 Ausstehende Phasen (6-10): Komplexe Funktionen

| Phase | Funktion | Komplexität | Zielmodul | Status |
|-------|----------|-------------|-----------|--------|
| 6 | `_refresh_subentry_index` | F (139) | Neue Helpers → `coordinator_subentry.py` | ⏳ Pending |
| 7 | `get_active_device_identities` | F (128) | Neue Helpers → `coordinator_identity.py` | ⏳ Pending |
| 8 | `_ensure_registry_for_devices` | F (126) | Neue Helpers → `coordinator_registry.py` | ⏳ Pending |
| 9 | `_ensure_service_device_exists` | F (108) | Neue Helpers → `coordinator_registry.py` | ⏳ Pending |
| 10 | `_async_update_data` | F (100) | Phasen-Extraktion | ⏳ Pending |

---

## Best Practice: Modul-Organisation

### ❌ ANTI-PATTERN: Komplexe Methoden in Helper-Module verschieben

```python
# FALSCH: Gesamte komplexe Methode verschieben
# coordinator_subentry.py
def refresh_subentry_index(coordinator, visible_devices):  # Komplexität 139!
    # Monolithischer Code mit Side-Effects
    ...
```

**Problem:**
- Zerstört die niedrige Komplexität der bestehenden Module
- Bricht das "Pure Functions"-Pattern
- Side-Effects und State-Abhängigkeiten bleiben

### ✅ BEST PRACTICE: Pure Helpers extrahieren, Orchestrierung behalten

```python
# coordinator_subentry.py - NUR pure Helper-Funktionen (Komplexität A-B)
def extract_subentry_candidates(devices: list[dict]) -> list[SubentryCandidate]:
    """Pure function: extrahiert Kandidaten ohne Side-Effects."""
    ...

def validate_subentry_candidates(candidates: list, existing_keys: set) -> list:
    """Pure function: validiert Kandidaten."""
    ...

def build_subentry_index(validated: list) -> dict[str, SubentryMetadata]:
    """Pure function: baut Index-Dictionary."""
    ...

# coordinator.py - Orchestrierung bleibt hier
def _refresh_subentry_index(self, visible_devices):
    """Orchestriert die Phasen, delegiert an pure Helpers."""
    # Phase 1: Collect (delegiert an pure function)
    candidates = extract_subentry_candidates(visible_devices)

    # Phase 2: Validate (delegiert an pure function)
    validated = validate_subentry_candidates(candidates, self._existing_keys)

    # Phase 3: Build index (delegiert an pure function)
    new_index = build_subentry_index(validated)

    # Phase 4: State update (bleibt hier - Side-Effect)
    self._subentry_index = new_index
    self._notify_listeners()
```

**Vorteile:**
- Helper-Module bleiben low-complexity (A-B Rating)
- Pure functions sind isoliert testbar
- Orchestrierung ist klar und übersichtlich
- Side-Effects sind explizit in coordinator.py

---

## Phase 6: `_refresh_subentry_index` (Komplexität 139 → Ziel: <20)

### 6.1 Vorbedingung: 100% Test-Coverage

**Erforderliche Tests VOR der Extraktion:**

```python
# tests/test_coordinator_subentry_index.py

class TestRefreshSubentryIndexRisks:
    """Tests fokussiert auf Risiken und Edge-Cases."""

    # RISIKO: Leere/ungültige Eingaben
    def test_refresh_empty_device_list(self):
        """Leere Device-Liste führt zu leerem Index."""

    def test_refresh_none_devices(self):
        """None-Werte in Device-Liste werden gefiltert."""

    def test_refresh_malformed_device_dict(self):
        """Fehlende Keys führen nicht zu Crash."""

    # RISIKO: Subentry-ID-Konflikte
    def test_refresh_duplicate_subentry_ids(self):
        """Doppelte Subentry-IDs werden dedupliziert."""

    def test_refresh_provisional_ids_not_persisted(self):
        """Provisorische IDs werden nicht persistiert."""

    # RISIKO: State-Inkonsistenz
    def test_refresh_preserves_existing_metadata(self):
        """Bestehende Metadata bleibt erhalten."""

    def test_refresh_removes_stale_entries(self):
        """Veraltete Einträge werden entfernt."""

    def test_refresh_concurrent_calls(self):
        """Parallele Aufrufe führen nicht zu Race-Conditions."""

    # RISIKO: Performance bei großen Datenmengen
    def test_refresh_large_device_list(self):
        """100+ Geräte werden performant verarbeitet."""

    # RISIKO: Listener-Benachrichtigung
    def test_refresh_notifies_listeners(self):
        """Listener werden bei Änderungen benachrichtigt."""

    def test_refresh_no_notification_without_changes(self):
        """Keine Benachrichtigung wenn keine Änderungen."""
```

### 6.2 Zu extrahierende Pure Functions

| Funktion | Komplexität | Beschreibung |
|----------|-------------|--------------|
| `extract_subentry_candidates` | A (~5) | Extrahiert Kandidaten aus Device-Liste |
| `validate_subentry_candidates` | B (~8) | Validiert und filtert Kandidaten |
| `build_subentry_index` | A (~4) | Baut Index-Dictionary |
| `merge_subentry_metadata` | A (~3) | Merged neue mit bestehenden Metadata |
| `detect_subentry_changes` | A (~4) | Erkennt Änderungen für Notifications |

### 6.3 Extraktion-Strategie

1. **Tests schreiben** für `_refresh_subentry_index` (100% Coverage)
2. **Pure Helpers identifizieren** (keine Side-Effects)
3. **Helpers nach `coordinator_subentry.py` verschieben**
4. **Orchestrierung in coordinator.py** vereinfachen
5. **Tests für neue Helpers** schreiben
6. **Alle Tests ausführen** (Regression-Check)

---

## Phase 7: `get_active_device_identities` (Komplexität 128 → Ziel: <25)

### 7.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_identity.py

class TestGetActiveDeviceIdentitiesRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: Key-Auswahl und Priorisierung
    def test_identity_prefers_primary_key(self):
        """Primary Key wird bevorzugt."""

    def test_identity_fallback_to_secondary_key(self):
        """Fallback auf Secondary Key funktioniert."""

    def test_identity_no_valid_key(self):
        """Device ohne gültige Keys wird übersprungen."""

    # RISIKO: Multi-Account-Handling
    def test_identity_multiple_accounts(self):
        """Geräte aus verschiedenen Accounts werden korrekt zugeordnet."""

    def test_identity_account_isolation(self):
        """Account-Grenzen werden respektiert."""

    # RISIKO: Device-Status
    def test_identity_skips_disabled_devices(self):
        """Deaktivierte Geräte werden übersprungen."""

    def test_identity_skips_ignored_devices(self):
        """Ignorierte Geräte werden übersprungen."""

    # RISIKO: EID-Verfügbarkeit
    def test_identity_with_eid(self):
        """EID wird korrekt integriert."""

    def test_identity_without_eid(self):
        """Fehlende EID führt nicht zu Crash."""
```

### 7.2 Neues Modul: `coordinator_identity.py`

```python
# coordinator_identity.py - Pure Functions für Identity-Logik

def select_device_key(device: dict, key_priority: list[str]) -> str | None:
    """Wählt den besten Key basierend auf Priorität."""

def build_device_identity(
    device: dict,
    key: str,
    account_id: str,
    eid_data: dict | None,
) -> DeviceIdentity | None:
    """Baut DeviceIdentity aus Device-Daten."""

def filter_active_devices(
    devices: list[dict],
    disabled_ids: set[str],
    ignored_ids: set[str],
) -> list[dict]:
    """Filtert auf aktive, nicht-ignorierte Geräte."""

def group_identities_by_account(
    identities: list[DeviceIdentity],
) -> dict[str, list[DeviceIdentity]]:
    """Gruppiert Identities nach Account."""
```

---

## Phase 8: `_ensure_registry_for_devices` (Komplexität 126 → Ziel: <20)

### 8.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_registry_ensure.py

class TestEnsureRegistryForDevicesRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: Device-Erstellung
    def test_creates_new_tracker_device(self):
        """Neues Tracker-Device wird erstellt."""

    def test_updates_existing_device(self):
        """Bestehendes Device wird aktualisiert."""

    def test_skips_ignored_device(self):
        """Ignorierte Geräte werden übersprungen."""

    # RISIKO: Subentry-Links
    def test_creates_subentry_link(self):
        """Subentry-Link wird erstellt."""

    def test_heals_broken_subentry_link(self):
        """Defekte Links werden repariert."""

    def test_removes_orphaned_hub_link(self):
        """Verwaiste Hub-Links werden entfernt."""

    # RISIKO: Legacy-Kompatibilität
    def test_legacy_kwargs_for_old_ha(self):
        """Legacy-HA-Versionen bekommen korrekte Kwargs."""

    def test_modern_kwargs_for_new_ha(self):
        """Moderne HA-Versionen bekommen neue Kwargs."""

    # RISIKO: Name-Resolution
    def test_resolves_hub_name_from_device(self):
        """Hub-Name aus Device-Registry."""

    def test_resolves_hub_name_fallback(self):
        """Fallback wenn Device nicht gefunden."""
```

### 8.2 Erweiterung von `coordinator_registry.py`

Die bestehende `coordinator_registry.py` enthält bereits Helper-Funktionen.
Diese Phase fügt **weitere pure Helper-Funktionen** hinzu:

```python
# Neue Helper in coordinator_registry.py

def normalize_tracker_subentry_id(
    device_id: str,
    entry_id: str,
    subentry_map: dict[str, str],
) -> str | None:
    """Normalisiert Subentry-ID für Tracker."""

def resolve_tracker_subentry(
    device: dict,
    subentry_index: dict,
    default_key: str,
) -> str:
    """Bestimmt die korrekte Subentry für ein Device."""

def build_device_registry_kwargs(
    device: dict,
    subentry_id: str,
    hub_name: str | None,
    ha_version: tuple[int, ...],
) -> dict[str, Any]:
    """Baut kwargs für device_registry.async_get_or_create()."""

def detect_subentry_link_issues(
    device_entry: DeviceEntry,
    expected_subentry: str,
) -> list[str]:
    """Erkennt Probleme mit Subentry-Links."""
```

**Komplexitäts-Budget**: Die neuen Funktionen sollten jeweils A-B Rating haben.
Gesamtkomplexität von `coordinator_registry.py` sollte unter C (Ø <15) bleiben.

---

## Phase 9: `_ensure_service_device_exists` (Komplexität 108 → Ziel: <20)

### 9.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_registry_service_device.py

class TestEnsureServiceDeviceExistsRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: Service-Device-Erstellung
    def test_creates_service_device(self):
        """Service-Device wird erstellt."""

    def test_updates_service_device_name(self):
        """Name wird bei Änderung aktualisiert."""

    # RISIKO: Multi-Account-Service-Devices
    def test_creates_per_account_service_device(self):
        """Jeder Account bekommt eigenes Service-Device."""

    def test_service_device_identifiers(self):
        """Identifiers sind korrekt und eindeutig."""

    # RISIKO: Subentry-Zuordnung
    def test_service_device_subentry_link(self):
        """Subentry-Link ist korrekt."""

    def test_service_device_config_entry_link(self):
        """Config-Entry-Link ist korrekt."""
```

### 9.2 Erweiterung von `coordinator_registry.py`

```python
# Neue Helper in coordinator_registry.py

def build_service_device_kwargs(
    entry_id: str,
    account_email: str,
    subentry_id: str | None,
    ha_version: tuple[int, ...],
) -> dict[str, Any]:
    """Baut kwargs für Service-Device-Erstellung."""

def detect_service_device_updates_needed(
    existing: DeviceEntry | None,
    new_kwargs: dict[str, Any],
) -> bool:
    """Prüft ob Update nötig ist."""
```

---

## Phase 10: `_async_update_data` (Komplexität 100 → Ziel: <20)

### 10.1 Vorbedingung: 100% Test-Coverage

```python
# tests/test_coordinator_update_data.py

class TestAsyncUpdateDataRisks:
    """Tests fokussiert auf Risiken."""

    # RISIKO: API-Fehler
    def test_handles_api_timeout(self):
        """Timeout wird graceful behandelt."""

    def test_handles_api_auth_error(self):
        """Auth-Fehler triggert Reauth."""

    def test_handles_api_rate_limit(self):
        """Rate-Limit führt zu Backoff."""

    # RISIKO: Daten-Verarbeitung
    def test_processes_new_devices(self):
        """Neue Geräte werden verarbeitet."""

    def test_updates_existing_devices(self):
        """Bestehende Geräte werden aktualisiert."""

    def test_handles_empty_response(self):
        """Leere API-Antwort führt nicht zu Crash."""

    # RISIKO: State-Updates
    def test_updates_last_successful_poll(self):
        """Timestamp wird aktualisiert."""

    def test_triggers_entity_updates(self):
        """Entities werden benachrichtigt."""
```

### 10.2 Phasen-Extraktion

Die Methode wird in klar definierte Phasen aufgeteilt:

```python
# coordinator.py

async def _async_update_data(self):
    """Update data - orchestriert Phasen."""
    # Phase 1: Pre-flight checks
    if not self._preflight_check():
        return self.data

    # Phase 2: Fetch from API
    raw_data = await self._fetch_api_data()
    if raw_data is None:
        return self.data

    # Phase 3: Process and merge
    processed = self._process_api_response(raw_data)

    # Phase 4: Update state
    self._apply_data_update(processed)

    # Phase 5: Post-processing
    await self._post_update_tasks()

    return self.data
```

---

## Komplexitäts-Budget pro Modul

| Modul | Aktuelle Komplexität | Budget (max) |
|-------|---------------------|--------------|
| `coordinator_geo.py` | A (Ø 2.5) | C (Ø <15) |
| `coordinator_stats.py` | A (Ø 1.8) | B (Ø <10) |
| `coordinator_cache.py` | A (Ø 5.0) | C (Ø <15) |
| `coordinator_registry.py` | A (Ø 6.75) | C (Ø <15) |
| `coordinator_subentry.py` | A (Ø 4.6) | C (Ø <15) |
| `coordinator_identity.py` (neu) | - | C (Ø <15) |

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

## Abhängigkeiten und Import-Struktur

```
coordinator.py (Orchestrierung)
├── coordinator_geo.py      (Pure: Geo-Berechnungen)
├── coordinator_stats.py    (Pure: Metriken, Status-Klassen)
├── coordinator_cache.py    (Pure: Cache-Logik)
├── coordinator_registry.py (Pure: Registry-Helper)
├── coordinator_subentry.py (Pure: Subentry-Helper)
└── coordinator_identity.py (Pure: Identity-Helper) [NEU]
```

**Keine zirkulären Imports**: Helper-Module importieren NICHT coordinator.py.

---

## Metriken-Ziele (Aktualisiert)

| Metrik | Vorher | Phase 1-5 | Nach Phase 6-10 |
|--------|--------|-----------|-----------------|
| coordinator.py Zeilen | 8761 | 8532 | ~2500 |
| Max. Komplexität | 139 | 139 | <30 |
| Funktionen mit F-Rating | 8 | 8 | 0 |
| Extrahierte Tests | 0 | 289 | ~450 |
| Helper-Module | 0 | 5 | 6 |

---

## Nächste Schritte

1. [ ] Phase 6 Tests schreiben (`_refresh_subentry_index`)
2. [ ] 100% Coverage für Phase 6 erreichen
3. [ ] Pure Helpers extrahieren
4. [ ] Komplexität validieren
5. [ ] Weiter mit Phase 7-10

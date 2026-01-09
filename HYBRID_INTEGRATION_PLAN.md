# Hybrid-Plan: Helper-Integration Abschluss

## Übersicht

| Modul | Definiert | Verwendet | Zu integrieren | Zu löschen |
|-------|-----------|-----------|----------------|------------|
| coordinator_cache.py | 19 | 9 | 4 | 6 |
| coordinator_geo.py | 4 | 4 | 0 | 0 |
| coordinator_identity.py | 10 | 10 | 0 | 0 |
| coordinator_locate.py | 7 | 0 | 0 | **7** |
| coordinator_polling.py | 6 | 3 | 0 | 3 |
| coordinator_registry.py | 21 | 10 | 6 | 5 |
| coordinator_stats.py | 3 | 3 | 0 | 0 |
| coordinator_subentry.py | 12 | 5 | 3 | 4 |
| coordinator_update.py | 6 | 6 | 0 | 0 |
| **GESAMT** | **88** | **50** | **13** | **25** |

---

## coordinator_cache.py (10 ungenutzt)

### ZU INTEGRIEREN (4)
| Helper | Inline-Entsprechung | Ort |
|--------|---------------------|-----|
| `compare_location_freshness` | Timestamp-Vergleich in `_merge_with_existing_cache_row` | ~7367 |
| `select_best_location_source` | Source-Selection in `_merge_with_existing_cache_row` | ~7367 |
| `merge_cache_row` | Gesamte Merge-Logik kombiniert | ~7367 |
| `normalize_location_fields` | In `_normalize_coords` | ~4256 |

### ZU LÖSCHEN (6)
| Helper | Begründung |
|--------|------------|
| `validate_location_data` | Keine direkte Entsprechung, Validierung ist verteilt |
| `merge_location_update` | Zu generisch, nicht direkt nutzbar |
| `detect_significant_change` | Feature existiert nicht im aktuellen Code |
| `select_best_accuracy` | Keine inline-Logik dafür |
| `_haversine_distance` (intern) | Duplikat von coordinator_geo |
| `_normalize_epoch_seconds` (intern) | Duplikat von coordinator_subentry |
| `_coerce_float` (intern) | Duplikat von coordinator_geo |

---

## coordinator_locate.py (7 ungenutzt) - **KOMPLETT LÖSCHEN**

| Helper | Begründung |
|--------|------------|
| `parse_locate_response` | API-Response wird direkt verarbeitet, andere Struktur |
| `validate_locate_result` | Validierung ist inline anders implementiert |
| `detect_null_island` | Feature nie implementiert |
| `extract_location_from_response` | Nicht benötigt, direkte Verarbeitung |
| `normalize_locate_coordinates` | `_normalize_coords` existiert bereits |
| `build_locate_cache_entry` | `update_device_cache` wird direkt verwendet |
| `classify_locate_error` | Keine Fehlerklassifizierung implementiert |

**Aktion:** Datei `coordinator_locate.py` und `tests/test_coordinator_locate.py` löschen.

---

## coordinator_polling.py (3 ungenutzt)

### ZU LÖSCHEN (3)
| Helper | Begründung |
|--------|------------|
| `normalize_semantic_name` | Keine inline-Entsprechung |
| `classify_poll_error_type` | Keine Fehlerklassifizierung im Polling |
| `build_poll_device_label` | Labels werden anders generiert |

---

## coordinator_registry.py (11 ungenutzt)

### ZU INTEGRIEREN (6)
| Helper | Inline-Entsprechung | Ort |
|--------|---------------------|-----|
| `normalize_device_name` | `_normalized_name()` | `_ensure_registry_for_devices:3332` |
| `extract_subentry_links` | `_subentry_links()` | `_ensure_registry_for_devices:3436` |
| `has_subentry_link` | `_has_tracker_link()` | `_ensure_registry_for_devices:3469` |
| `is_hub_device_check` | `_is_hub_device()` | `_ensure_registry_for_devices:3377` |
| `resolve_tracker_subentry_candidate` | `_resolve_tracker_subentry()` | `_ensure_registry_for_devices:3277` |
| `extract_service_subentry_ids` | Inline-Loop | `_ensure_service_device_exists:2248-2267` |

### ZU LÖSCHEN (5)
| Helper | Begründung |
|--------|------------|
| `sanitize_entry_title` | Keine inline-Verwendung gefunden |
| `has_user_defined_name` | Keine inline-Verwendung gefunden |
| `should_defer_service_subentry` | Logik ist zu spezifisch eingebettet |
| `detect_extraneous_service_identifiers` | Feature nicht implementiert |
| `determine_removal_subentry_id` | Feature nicht implementiert |
| `is_legacy_unique_id` | Keine inline-Verwendung |

---

## coordinator_subentry.py (7 ungenutzt)

### ZU INTEGRIEREN (3)
| Helper | Inline-Entsprechung | Ort |
|--------|---------------------|-----|
| `filter_provisional_identifier` | Provisional-Check | `_refresh_subentry_index:1339-1351` |
| `extract_subentry_group_key` | group_key Extraktion | `_refresh_subentry_index:1329-1333` |
| `detect_missing_core_subentry_keys` | missing_core_keys | `_refresh_subentry_index:1362-1367` |

### ZU LÖSCHEN (4)
| Helper | Begründung |
|--------|------------|
| `build_device_index_from_list` | Zu generisch, inline-Logik ist spezifischer |
| `normalize_features_list` | Keine inline-Verwendung |
| `normalize_visible_device_id_list` | Keine inline-Verwendung |
| `compute_stable_subentry_id` | Inline-Berechnung ist kontextabhängig |

---

## Implementierungsreihenfolge

### Phase 1: Löschen ungenutzter Helper (einfach)
1. `coordinator_locate.py` komplett löschen
2. `tests/test_coordinator_locate.py` löschen
3. Ungenutzte Funktionen aus anderen Modulen entfernen
4. Tests für gelöschte Funktionen entfernen

### Phase 2: Integration in `_ensure_registry_for_devices` (6 Helper)
1. `normalize_device_name` → `_normalized_name`
2. `extract_subentry_links` → `_subentry_links`
3. `has_subentry_link` → `_has_tracker_link`
4. `is_hub_device_check` → `_is_hub_device`
5. `resolve_tracker_subentry_candidate` → `_resolve_tracker_subentry`
6. `extract_service_subentry_ids` → inline Loop

### Phase 3: Integration in `_refresh_subentry_index` (3 Helper)
1. `filter_provisional_identifier`
2. `extract_subentry_group_key`
3. `detect_missing_core_subentry_keys`

### Phase 4: Integration in Cache-Methoden (4 Helper)
1. `compare_location_freshness`
2. `select_best_location_source`
3. `merge_cache_row`
4. `normalize_location_fields`

---

## Erwartetes Ergebnis

- **Gelöscht:** 25 ungenutzte Funktionen + Tests
- **Integriert:** 13 Helper ersetzen inline-Code
- **Endstand:** 63 Helper, alle verwendet (100%)
- **Komplexitätsreduktion:** Weitere ~50-80 Punkte erwartet

---

## Risikobewertung

| Risiko | Wahrscheinlichkeit | Auswirkung | Mitigation |
|--------|-------------------|------------|------------|
| Regressions durch Löschungen | Niedrig | Niedrig | Nur ungenutzte Helper löschen |
| Integration bricht Tests | Mittel | Mittel | Schrittweise vorgehen, nach jedem Schritt testen |
| Closure-Variablen-Probleme | Mittel | Hoch | Helper benötigen zusätzliche Parameter |

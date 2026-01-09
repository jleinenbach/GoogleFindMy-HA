# Refactoring Lessons Learned

Dokumentation der Code-Qualitäts-Analyse und Refactoring-Methodik aus Branch `claude/review-bug-detection-KVUvG`.

**Hinweis**: Diese Methodik kann auf den aktuellen `1.7.0-3` Branch angewendet werden.

---

## 1. Verwendete Analyse-Tools

### 1.1 Zyklomatische Komplexität (Radon)

```bash
# Installation
pip install radon>=6.0

# Analyse aller Funktionen mit Komplexität >= D (> 20)
radon cc custom_components/googlefindmy -a -s --min D

# Detaillierte Analyse einer einzelnen Datei
radon cc custom_components/googlefindmy/services.py -s
```

**Bewertungsskala:**
| Rang | Komplexität | Bedeutung |
|------|-------------|-----------|
| A | 1-5 | Einfach, geringes Risiko |
| B | 6-10 | Niedrige Komplexität |
| C | 11-20 | Moderate Komplexität |
| D | 21-30 | Hohe Komplexität |
| E | 31-40 | Sehr hohe Komplexität |
| F | > 40 | Nicht wartbar |

### 1.2 Code-Duplikation (jscpd)

```bash
# Installation
npm install -g jscpd

# Analyse mit Konfiguration
jscpd . --config .jscpd.json

# Nur Python-Dateien
jscpd custom_components/googlefindmy --format python --min-lines 10 --min-tokens 50
```

**Empfohlene .jscpd.json:**
```json
{
  "$schema": "https://json.schemastore.org/jscpd.json",
  "threshold": 5,
  "reporters": ["json", "console"],
  "output": "jscpd-report",
  "ignore": [
    "**/node_modules/**",
    "**/.git/**",
    "**/ProtoDecoders/**",
    "**/*_pb2.py",
    "**/*_pb2_grpc.py",
    "**/translations/**",
    "**/__pycache__/**",
    "**/.mypy_cache/**"
  ],
  "format": ["python"],
  "minLines": 10,
  "minTokens": 50,
  "gitignore": true
}
```

### 1.3 Dead Code Detection (Vulture)

```bash
# Installation
pip install vulture>=2.10

# Analyse mit 90% Konfidenz (weniger False Positives)
vulture custom_components/googlefindmy --min-confidence 90

# Whitelist für absichtlich unbenutzten Code
vulture custom_components/googlefindmy vulture_whitelist.py
```

### 1.4 Code Smells (Pylint)

```bash
# Installation
pip install pylint>=3.0

# Fokussierte Analyse (nur bestimmte Checks)
pylint custom_components/googlefindmy --disable=all --enable=C0301,W0611,W0612,R0912,R0915

# Wichtige Checks:
# C0301: Line too long
# W0611: Unused import
# W0612: Unused variable
# R0912: Too many branches
# R0915: Too many statements
```

---

## 2. Refactoring-Patterns

### 2.1 Komplexitäts-Reduktion durch Extraktion

**Vorher** (Komplexität 106):
```python
async def async_rebuild_device_registry(hass, entry):
    # 500+ Zeilen mit verschachtelten Schleifen und Bedingungen
    ...
```

**Nachher** (Komplexität ~15):
```python
async def async_rebuild_device_registry(hass, entry):
    """Rebuild device registry - orchestrates three phases."""
    ctx = RebuildContext(hass=hass, entry=entry, ...)

    # Phase 1: Ensure devices exist
    await rebuild_phase1_ensure_devices(ctx)

    # Phase 2: Cleanup orphaned links
    await _rebuild_phase2_cleanup_devices(ctx)

    # Phase 3: Cleanup legacy entities
    await rebuild_phase3_cleanup_entities(ctx)
```

**Methode:**
1. Identifiziere logische Blöcke im Code
2. Extrahiere jeden Block in eine eigene Funktion
3. Verwende Dataclasses für gemeinsamen State (`RebuildContext`)
4. Benenne Funktionen nach ihrer Aktion (verb_noun)

### 2.2 Guard Clauses statt Verschachtelung

**Vorher:**
```python
def process(data):
    if data:
        if data.valid:
            if data.ready:
                return do_work(data)
    return None
```

**Nachher:**
```python
def process(data):
    if not data:
        return None
    if not data.valid:
        return None
    if not data.ready:
        return None
    return do_work(data)
```

### 2.3 Deduplikation durch Helper-Module

**Pattern:** Extrahiere gemeinsame Funktionalität in dedizierte Module:

| Modul | Zweck | Verwendet von |
|-------|-------|---------------|
| `coordinator_geo.py` | Geografische Berechnungen | coordinator.py |
| `entity_helpers.py` | DeviceInfo, Entity-Utilities | binary_sensor.py, sensor.py |
| `services_rebuild.py` | Device Registry Operationen | services.py |

**Beispiel `entity_helpers.py`:**
```python
def get_integration_device_info(coordinator) -> DeviceInfo:
    """Return DeviceInfo for integration diagnostic entities."""
    entry_id = coordinator.config_entry.entry_id
    return DeviceInfo(
        identifiers={(DOMAIN, f"integration_{entry_id}")},
        name=f"Google Find My Integration",
        manufacturer="BSkando",
        model="Find My Device Integration",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
```

### 2.4 Base-Class-Extraktion für Entities

**Vorher:** Duplizierter Code in jedem Button
```python
class LocateButton(ButtonEntity):
    # 50 Zeilen gemeinsamer Code

class PlaySoundButton(ButtonEntity):
    # 50 Zeilen (fast identisch)
```

**Nachher:** Gemeinsame Base Class
```python
class GoogleFindMyButton(ButtonEntity):
    """Base class for all GoogleFindMy buttons."""
    # Gemeinsamer Code

class LocateButton(GoogleFindMyButton):
    # Nur spezifische Logik
```

---

## 3. Durchgeführte Refactorings

### Komplexitäts-Reduktionen

| Funktion | Vorher | Nachher | Methode |
|----------|--------|---------|---------|
| `async_rebuild_device_registry` | 106 (F) | ~15 (C) | Phase-Extraktion |
| `_async_update_data` | 42 (F) | 9 (A) | Helper-Extraktion |
| `GoogleFindMyMapView.get` | 41 (F) | 4 (A) | Guard Clauses |
| `_generate_aas_token` | 38 (E) | 4 (A) | Token-Builder |
| `_async_start_poll_cycle` | 37 (E) | 7 (B) | State-Machine |

### Neue Module

| Modul | Zeilen | Zweck |
|-------|--------|-------|
| `coordinator_geo.py` | ~150 | Haversine, Signifikanz-Checks |
| `entity_helpers.py` | ~80 | DeviceInfo Factory |
| `services_rebuild.py` | ~650 | Device Registry Operations |

### Duplikations-Status

- **Vor Refactoring**: ~3% Duplikation
- **Nach Refactoring**: 0.64% Duplikation
- **Ziel**: < 5%

---

## 4. CI/CD Erweiterungen

### GitHub Actions Job für Code-Qualität

```yaml
code-quality:
  name: Code Quality Analysis
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: "3.13"

    - name: Install analysis tools
      run: pip install radon vulture pylint

    - name: Cyclomatic Complexity
      run: |
        echo "## Complexity Analysis" >> $GITHUB_STEP_SUMMARY
        radon cc custom_components/googlefindmy -a -s --min C >> $GITHUB_STEP_SUMMARY

    - name: Dead Code Detection
      run: |
        echo "## Dead Code" >> $GITHUB_STEP_SUMMARY
        vulture custom_components/googlefindmy --min-confidence 90 >> $GITHUB_STEP_SUMMARY || true

    - name: Code Duplication
      run: |
        npm install -g jscpd
        jscpd . --config .jscpd.json
```

### Hypothesis Property-Based Tests

```yaml
hypothesis-tests:
  name: Property-Based Tests
  runs-on: ubuntu-latest
  env:
    HYPOTHESIS_CI_JOB: "true"
  steps:
    - uses: actions/checkout@v4
    - name: Run Hypothesis tests
      run: pytest tests/test_hypothesis_properties.py -v
```

---

## 5. Anwendung auf 1.7.0-3

### Empfohlene Analyse-Schritte

1. **Komplexitäts-Scan:**
   ```bash
   git checkout 1.7.0-3
   radon cc custom_components/googlefindmy -a -s --min D
   ```

2. **Duplikations-Check:**
   ```bash
   jscpd custom_components/googlefindmy --format python
   ```

3. **Dead Code:**
   ```bash
   vulture custom_components/googlefindmy --min-confidence 90
   ```

### Potentielle Kandidaten (basierend auf Analyse des alten Codes)

Diese Bereiche könnten auch auf `1.7.0-3` von Refactoring profitieren:
- `coordinator.py` - Falls > 2000 Zeilen
- `services.py` - Falls komplexe Funktionen
- `config_flow.py` - Falls > 800 Zeilen
- `fcm_receiver_ha.py` - Falls > 900 Zeilen

### CI-Integration

Die `.github/workflows/ci.yml` Erweiterung aus diesem Branch könnte auf `1.7.0-3` übertragen werden, um kontinuierliche Code-Qualitäts-Überwachung zu ermöglichen.

---

## 6. Best Practices Zusammenfassung

1. **Analyse vor Refactoring**: Immer zuerst messen (Radon, jscpd)
2. **Kleine Schritte**: Ein Refactoring pro Commit
3. **Tests beibehalten**: Nie ohne Tests refactoren
4. **Komplexitätsziel**: Alle Funktionen unter C (< 20)
5. **Duplikationsziel**: < 5% Gesamtduplikation
6. **Dokumentation**: Neue Module dokumentieren
7. **CI-Integration**: Automatische Qualitäts-Checks

---

## 7. Referenzen

- [Radon Documentation](https://radon.readthedocs.io/)
- [jscpd GitHub](https://github.com/kucherenko/jscpd)
- [Vulture Documentation](https://github.com/jendrikseipp/vulture)
- [Cyclomatic Complexity (Wikipedia)](https://en.wikipedia.org/wiki/Cyclomatic_complexity)

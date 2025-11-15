.PHONY: bootstrap-base-deps clean clean-wheelhouse install-ha-stubs lint test-ha wheelhouse

VENV ?= .venv
PYTHON ?= python3
PYTEST_ARGS ?=
PYTEST_COV_FLAGS ?= --cov-report=term-missing
SKIP_WHEELHOUSE_REFRESH ?= 0
WHEELHOUSE ?= .wheelhouse
WHEELHOUSE_SENTINEL := $(WHEELHOUSE)/.requirements-dev.stamp
BOOTSTRAP_SENTINEL := .bootstrap/homeassistant-preinstall.stamp

clean:
        @python script/clean_pycache.py
        @if [ -f "$(BOOTSTRAP_SENTINEL)" ]; then \
                echo "[make clean] Removing Home Assistant bootstrap sentinel"; \
                rm -f "$(BOOTSTRAP_SENTINEL)"; \
        fi

lint:
	@ruff check . --fix

wheelhouse: $(WHEELHOUSE_SENTINEL)
	@echo "[make wheelhouse] Wheel cache is ready at $(WHEELHOUSE)"

clean-wheelhouse:
	@if [ -d "$(WHEELHOUSE)" ]; then \
		echo "[make clean-wheelhouse] Removing cached wheels in $(WHEELHOUSE)"; \
		rm -rf "$(WHEELHOUSE)"; \
	else \
		echo "[make clean-wheelhouse] No wheel cache present"; \
	fi

install-ha-stubs:
        @echo "[make install-ha-stubs] Installing Home Assistant pytest dependencies"
        @$(PYTHON) -m pip install --upgrade -r requirements-ha-stubs.txt

bootstrap-base-deps: $(BOOTSTRAP_SENTINEL)
        @echo "[make bootstrap-base-deps] Home Assistant base dependencies are ready"

$(BOOTSTRAP_SENTINEL):
        @mkdir -p $(dir $(BOOTSTRAP_SENTINEL))
        @echo "[make bootstrap-base-deps] Pre-installing Home Assistant base dependencies"
        @$(PYTHON) -m pip install --upgrade homeassistant pytest-homeassistant-custom-component
        @touch $(BOOTSTRAP_SENTINEL)

$(WHEELHOUSE_SENTINEL): requirements-dev.txt
	@mkdir -p $(WHEELHOUSE)
	@if [ "$(SKIP_WHEELHOUSE_REFRESH)" = "1" ] && find "$(WHEELHOUSE)" -mindepth 1 -maxdepth 1 -type f >/dev/null 2>&1; then \
		echo "[make wheelhouse] Reusing existing wheel cache in $(WHEELHOUSE)"; \
	else \
		echo "[make wheelhouse] Downloading development wheels into $(WHEELHOUSE)"; \
		echo "[make wheelhouse] Hint: set SKIP_WHEELHOUSE_REFRESH=1 to reuse the cache on future make test-ha runs"; \
		$(PYTHON) -m pip download --requirement requirements-dev.txt --dest $(WHEELHOUSE) --exists-action=i; \
	fi
	@touch $(WHEELHOUSE_SENTINEL)

$(VENV)/bin/activate: requirements-dev.txt $(WHEELHOUSE_SENTINEL) $(BOOTSTRAP_SENTINEL)
        @$(PYTHON) -m venv $(VENV)
        @$(VENV)/bin/pip install --find-links=$(WHEELHOUSE) -r requirements-dev.txt
        @touch $(VENV)/bin/activate

test-ha: $(VENV)/bin/activate
	@echo "[make test-ha] Running targeted Home Assistant regression smoke tests"
	@. $(VENV)/bin/activate && pytest $(PYTEST_ARGS) \
		tests/test_entity_recovery_manager.py \
		tests/test_homeassistant_callback_stub_helper.py
	@echo "[make test-ha] Executing full-suite coverage run (see pytest_output.log for details)"
	@bash -o pipefail -c ". $(VENV)/bin/activate && pytest -q --cov $(PYTEST_COV_FLAGS) $${PYTEST_ARGS:+$${PYTEST_ARGS} } 2>&1 | tee pytest_output.log"

test-unload: $(VENV)/bin/activate
	@echo "[make test-unload] Running parent unload rollback regression suite"
	@. $(VENV)/bin/activate && pytest -q $(PYTEST_ARGS) tests/test_unload_subentry_cleanup.py

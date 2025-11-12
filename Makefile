.PHONY: clean lint test-ha wheelhouse

VENV ?= .venv
PYTHON ?= python3
PYTEST_ARGS ?=
WHEELHOUSE ?= .wheelhouse
WHEELHOUSE_SENTINEL := $(WHEELHOUSE)/.requirements-dev.stamp

clean:
	@python script/clean_pycache.py

lint:
	@ruff check . --fix

wheelhouse: $(WHEELHOUSE_SENTINEL)
	@echo "[make wheelhouse] Wheel cache is ready at $(WHEELHOUSE)"

$(WHEELHOUSE_SENTINEL): requirements-dev.txt
	@mkdir -p $(WHEELHOUSE)
	@echo "[make wheelhouse] Downloading development wheels into $(WHEELHOUSE)"
	@$(PYTHON) -m pip download --requirement requirements-dev.txt --dest $(WHEELHOUSE) --exists-action=i
	@touch $(WHEELHOUSE_SENTINEL)

$(VENV)/bin/activate: requirements-dev.txt $(WHEELHOUSE_SENTINEL)
	@$(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/pip install --find-links=$(WHEELHOUSE) -r requirements-dev.txt
	@touch $(VENV)/bin/activate

test-ha: $(VENV)/bin/activate
	@echo "[make test-ha] Running targeted Home Assistant regression smoke tests"
	@. $(VENV)/bin/activate && pytest $(PYTEST_ARGS) \
		tests/test_entity_recovery_manager.py \
		tests/test_homeassistant_callback_stub_helper.py
	@echo "[make test-ha] Executing full-suite coverage run (see pytest_output.log for details)"
	@bash -o pipefail -c ". $(VENV)/bin/activate && pytest -q --cov $${PYTEST_ARGS:+$${PYTEST_ARGS} }2>&1 | tee pytest_output.log"

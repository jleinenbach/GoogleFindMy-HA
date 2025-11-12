.PHONY: clean lint test-ha

VENV ?= .venv
PYTHON ?= python3
PYTEST_ARGS ?=

clean:
	@python script/clean_pycache.py

lint:
	@ruff check . --fix

$(VENV)/bin/activate: requirements-dev.txt
	@$(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/pip install -r requirements-dev.txt
	@touch $(VENV)/bin/activate

test-ha: $(VENV)/bin/activate
	@echo "[make test-ha] Running targeted Home Assistant regression smoke tests"
	@. $(VENV)/bin/activate && pytest $(PYTEST_ARGS) \
		tests/test_entity_recovery_manager.py \
		tests/test_homeassistant_callback_stub_helper.py
	@echo "[make test-ha] Executing full-suite coverage run (see pytest_output.log for details)"
	@bash -o pipefail -c ". $(VENV)/bin/activate && pytest -q --cov $${PYTEST_ARGS:+$${PYTEST_ARGS} }2>&1 | tee pytest_output.log"

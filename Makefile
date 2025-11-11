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
	@. $(VENV)/bin/activate && pytest $(PYTEST_ARGS) \
		tests/test_entity_recovery_manager.py \
		tests/test_homeassistant_callback_stub_helper.py

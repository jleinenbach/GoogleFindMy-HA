.PHONY: clean lint

clean:
	@python script/clean_pycache.py

lint:
	@ruff check . --fix

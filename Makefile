.PHONY: clean

clean:
	@echo "Removing Python cache artifacts..."
	@python -c "from __future__ import annotations; import shutil; from pathlib import Path; base = Path.cwd(); [shutil.rmtree(p, ignore_errors=True) for p in base.rglob('__pycache__')]; [p.unlink(missing_ok=True) for p in base.rglob('*.pyc')]"
	@echo "Cache artifacts removed."

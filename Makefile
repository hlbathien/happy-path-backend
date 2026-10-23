.PHONY: setup seed migrate test openapi run clean

PYTHON ?= .venv/bin/python
UV ?= uv
PYTHONPATH := src

setup:
	$(UV) sync --frozen --python 3.10

# Explicit destructive reset; serving, export and tests never depend on this target.
seed:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/seed_db.py

migrate:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/seed_db.py --if-missing
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/migrate_db.py

test:
	@db=$$(mktemp /tmp/happy_path_tests_XXXXXX.db); \
	trap 'rm -f "$$db" "$$db-journal"' EXIT; \
	HAPPY_PATH_DB_PATH=$$db PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/seed_db.py && \
	HAPPY_PATH_DB_PATH=$$db PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v

openapi:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/export_openapi.py

run:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/run_backend.py

clean:
	rm -rf __pycache__ .pytest_cache src/happy_path/__pycache__

.PHONY: setup seed test openapi run clean

PYTHON ?= python3
PYTHONPATH := src

setup:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install fastapi "uvicorn[standard]" httpx pydantic pytest pyyaml

seed:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/seed_db.py

test: seed
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m unittest discover -s tests -v

openapi: seed
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/export_openapi.py

run: seed
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m uvicorn happy_path.main:app --host 0.0.0.0 --port 8000

clean:
	rm -rf __pycache__ .pytest_cache src/happy_path/__pycache__

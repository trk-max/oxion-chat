.PHONY: install run test lint clean

install:
	python -m pip install -r requirements.txt

run:
	python -m uvicorn app:app --host 127.0.0.1 --port 8000

test:
	python -m pytest

lint:
	python -m ruff check app.py backend scripts tests
	python -m black --check backend scripts tests
	python -m isort --check-only app.py backend scripts tests
	python -m mypy app.py backend scripts --follow-imports=skip

clean:
	python -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for p in (pathlib.Path('__pycache__'), pathlib.Path('.pytest_cache'), pathlib.Path('.mypy_cache'), pathlib.Path('.ruff_cache'))]"

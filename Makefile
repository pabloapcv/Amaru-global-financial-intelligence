.PHONY: install ingest api web test
install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && npm --prefix apps/web install
ingest:
	.venv/bin/atlas ingest
api:
	.venv/bin/atlas serve
web:
	npm --prefix apps/web run dev
test:
	.venv/bin/python -m pytest

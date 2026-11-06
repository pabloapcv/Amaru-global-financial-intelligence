.PHONY: install ingest api web test demo

# Setup
install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && npm --prefix apps/web install

# Data
ingest:
	.venv/bin/atlas ingest

# Services
api:
	.venv/bin/atlas serve

web:
	npm --prefix apps/web run dev

# Testing
test:
	.venv/bin/python -m pytest

# Demo: Run verified research artifact with synthetic data (no API keys needed)
# Output: docs/research/corn_supply_demand_report.md
#
# Prerequisites: numpy, pandas, scipy, statsmodels
# The demo will use .venv if present, or attempt to install deps to user site.
demo:
	@echo "=== Amaru Demo ==="
	@echo "Running verified research artifact with synthetic fixture data..."
	@echo "(No Anthropic API key required)"
	@echo ""
	@if [ -d .venv ] && [ -f .venv/bin/python ]; then \
		echo "Using virtual environment..."; \
		.venv/bin/python demos/research/corn_supply_demand.py; \
	else \
		echo "No venv found. Checking system Python dependencies..."; \
		python3 -c "import numpy, pandas, scipy, statsmodels" 2>/dev/null || { \
			echo "Installing required packages to user site (numpy, pandas, scipy, statsmodels)..."; \
			pip3 install --user -q numpy pandas scipy statsmodels || { \
				echo ""; \
				echo "Error: Could not install dependencies."; \
				echo "Please run 'make install' first, or install numpy/pandas/scipy/statsmodels manually."; \
				exit 1; \
			}; \
		}; \
		python3 demos/research/corn_supply_demand.py; \
	fi
	@echo ""
	@echo "=== Demo Complete ==="
	@echo "Output: docs/research/corn_supply_demand_report.md"
	@echo ""
	@echo "Next steps:"
	@echo "  - View the report: cat docs/research/corn_supply_demand_report.md"
	@echo "  - Run with live data: make install && .venv/bin/atlas ingest && .venv/bin/atlas research 'your question'"

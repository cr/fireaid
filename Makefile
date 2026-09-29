PYTHON ?= python3
VENV := .venv

.PHONY: test clean

# Tests run against the latest stable Fire, whatever was installed before.
test: $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade fire
	$(VENV)/bin/pytest

$(VENV): pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet --editable ".[test]"
	touch $(VENV)

clean:
	rm -rf $(VENV) .pytest_cache build dist src/fireaid.egg-info
	find . -name __pycache__ -not -path "./.git/*" -prune -exec rm -rf {} +

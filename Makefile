PYTHON ?= python3
# The oldest Fire imports a module that Python 3.13 removed.
PYTHON_MIN ?= python3.12
VENV := .venv
VENV_MIN := .venv-min
FIRE_MIN := $(shell sed -n 's/^ *"fire>=\([^"]*\)".*/\1/p' pyproject.toml)

.PHONY: test test-min clean

# Tests run against the latest stable Fire, whatever was installed before.
test: $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade fire
	$(VENV)/bin/pytest

# And against the oldest Fire that pyproject.toml allows.
test-min: $(VENV_MIN)
	$(VENV_MIN)/bin/pip install --quiet "fire==$(FIRE_MIN)"
	$(VENV_MIN)/bin/pytest

define venv
	$(1) -m venv $(2)
	$(2)/bin/pip install --quiet --upgrade pip
	$(2)/bin/pip install --quiet --editable ".[test]"
	touch $(2)
endef

$(VENV): pyproject.toml
	$(call venv,$(PYTHON),$@)

$(VENV_MIN): pyproject.toml
	$(call venv,$(PYTHON_MIN),$@)

clean:
	rm -rf $(VENV) $(VENV_MIN) .pytest_cache build dist src/fireaid.egg-info
	find . -name __pycache__ -not -path "./.git/*" -prune -exec rm -rf {} +

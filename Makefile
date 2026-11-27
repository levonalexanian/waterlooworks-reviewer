.PHONY: setup scrape seed serve

PYTHON ?= .local/venv/bin/python
PLAYWRIGHT_VERSION := 1.62.1
PLAYWRIGHT := .local/node/node_modules/.bin/playwright
PORT ?= 8767
export BATCH
export PORT
export PLAYWRIGHT_BROWSERS_PATH := $(CURDIR)/.local/browsers

setup: .env $(PYTHON) $(PLAYWRIGHT)
	"$(PYTHON)" -m pip install 'openpyxl>=3.1,<4' 'pypdf>=6,<7'
	$(PLAYWRIGHT) install $(PW_INSTALL_FLAGS) chromium

.env: .env.example
	@umask 077; if [ ! -e .env ]; then cp .env.example .env; fi

.local/venv/bin/python:
	python3 -m venv .local/venv

$(PLAYWRIGHT):
	mkdir -p .local/node
	npm install --prefix .local/node --no-save --no-package-lock --no-audit --no-fund playwright@$(PLAYWRIGHT_VERSION)

scrape:
	WW_PYTHON="$(PYTHON)" node scripts/scrape.mjs $(if $(filter 1,$(HEADED)),--headed)

seed:
	@test -n "$$BATCH" || { echo 'Pass BATCH=path-to-download-directory'; exit 2; }
	"$(PYTHON)" scripts/seed.py import "$$BATCH"

serve:
	"$(PYTHON)" scripts/webpage.py --port "$$PORT"

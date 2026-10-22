PY ?= .venv/bin/python
RECEIPTS ?= .venv/bin/receipts
DEMO_ENV = FIXTURE_MODE=1 RECEIPTS_DB=out/demo/state.db RECEIPTS_OUT=out/demo

.PHONY: setup demo run refresh test audit viewer ui demo-ui lint bench seed-test

setup:
	test -d .venv || python3.11 -m venv .venv || uv venv --python 3.11 .venv
	$(PY) -m pip install -e '.[dev]' 2>/dev/null || uv pip install --python $(PY) -e '.[dev]'
	$(RECEIPTS) init

# no keys, no network: replays recorded fixtures (or the synthetic test world) into out/demo/
demo:
	rm -rf out/demo
	$(DEMO_ENV) $(RECEIPTS) run --input tests/fixtures/sample_input.csv --fixtures --target 50
	$(DEMO_ENV) $(RECEIPTS) export --demo
	$(DEMO_ENV) $(RECEIPTS) trace L0001
	$(DEMO_ENV) $(RECEIPTS) audit --n 20 --seed 7 --channels 20 --fixtures
	@echo "open out/demo/viewer/index.html"

run:
	$(RECEIPTS) run --input config/seeds.yaml --target 1200

refresh:
	$(RECEIPTS) refresh --input config/seeds.yaml
	$(RECEIPTS) export --workbook

test:
	$(PY) -m pytest -q

audit:
	$(RECEIPTS) audit --n 20 --seed 7 --channels 20

viewer:
	$(RECEIPTS) viewer

ui:
	$(RECEIPTS) ui

demo-ui:
	RECEIPTS_DB=out/demo/state.db RECEIPTS_OUT=out/demo $(RECEIPTS) ui

lint:
	$(PY) -m ruff check .

bench:
	$(RECEIPTS) bench --rows 5000 --fixtures

seed-test:
	$(RECEIPTS) seed-test --all

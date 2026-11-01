PORT ?= 8790
.PHONY: dev test fixtures calibrate
dev:
	RELOAD=1 ./dev.sh $(PORT)
test:
	.venv/bin/pytest -q
fixtures:
	.venv/bin/python scripts/make_fixtures.py
calibrate:
	.venv/bin/python scripts/calibrate.py $(DIR)

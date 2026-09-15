# Every target assumes the conda env `curbcheck` is active (it is, in every shell).
.PHONY: setup lint typecheck test audit check sync serve compile

setup:
	pip install --require-hashes -r requirements-dev.txt -r requirements.txt
	pip install -e . --no-deps

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy curbcheck

test:
	pytest -q

audit:
	pip-audit -r requirements.txt

check: lint typecheck test audit

# Re-pin after editing requirements*.in. See docs/DEPENDENCIES.md.
compile:
	uv pip compile --generate-hashes --python-version 3.12 -o requirements.txt requirements.in
	uv pip compile --generate-hashes --python-version 3.12 -o requirements-dev.txt requirements-dev.in

sync:
	curbcheck sync

serve:
	curbcheck serve

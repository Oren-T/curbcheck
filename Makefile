# Every target assumes the conda env `curbcheck` is active (it is, in every shell).
.PHONY: setup lint typecheck test js audit check sync serve compile \
	pack site docker-build docker-sync docker-serve

# --no-build-isolation on the second line: without it pip fetches hatchling from
# PyPI unhashed to build the wheel, which was the one install in the project
# that --require-hashes did not cover. hatchling and editables are pinned and
# hashed in requirements-dev.txt, and the first line installs them.
setup:
	pip install --require-hashes -r requirements-dev.txt -r requirements.txt
	pip install -e . --no-deps --no-build-isolation

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy curbcheck

test:
	pytest -q

# The browser engine's unit tests and, when build/pack and the fixture exist,
# the differential harness (docs/STATIC_SITE.md). Node 20+ is a development
# tool here, not a dependency: nothing is installed from npm.
js:
	node --test site/tests/*.test.js site/tests/harness/*.test.js

audit:
	pip-audit -r requirements.txt

check: lint typecheck test js audit

# Re-pin after editing requirements*.in. See docs/DEPENDENCIES.md.
compile:
	uv pip compile --generate-hashes --python-version 3.12 -o requirements.txt requirements.in
	uv pip compile --generate-hashes --python-version 3.12 -o requirements-dev.txt requirements-dev.in

sync:
	curbcheck sync

serve:
	curbcheck serve

# The static site, end to end, from the current database: pack, fixtures,
# harness, tiles, dist/. What .github/workflows/pages.yml runs.
pack:
	curbcheck pack --out build/pack

site: pack
	python site/tests/harness/make_fixtures.py
	node --test site/tests/*.test.js site/tests/harness/*.test.js
	python site/slice_basemap.py data/basemap/manhattan.pmtiles build/tiles
	python site/build.py --out build/dist --pack build/pack --tiles build/tiles

# Container targets. `serve` uses host networking because the server binds
# 127.0.0.1 and nothing may widen that; see docker-compose.yml for why.
docker-build:
	docker compose build

docker-sync:
	docker compose run --rm sync

docker-serve:
	docker compose up serve

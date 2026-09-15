# CurbCheck in a container: build the wheel and its hash-pinned dependency tree
# in one stage, copy only the installed bytes into a slim runtime.
#
# Read docker-compose.yml before running this. The server binds 127.0.0.1 and
# `curbcheck/config.py` offers no way to widen that (tests/test_boundaries.py
# fails the build if anyone adds one), so `-p 8765:8765` would publish a port
# that nothing is listening on. Host networking is the honest answer; the
# compose file explains the tradeoff.

FROM python:3.12-slim AS build

WORKDIR /src

# Dependencies first, on their own layer: they change far less often than the
# source, and --require-hashes means a tampered wheel fails here (threat T1).
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt

# The build backend, hashed, into a prefix this stage keeps to itself: only
# /install is copied into the runtime image, so hatchling and the rest of the
# dev tree never reach it. `--require-hashes` refuses a requirement named on the
# command line, so the whole file is installed here rather than two names out of
# it; it costs build time and nothing else.
RUN pip install --no-cache-dir --require-hashes --prefix=/buildenv -r requirements-dev.txt
ENV PYTHONPATH=/buildenv/lib/python3.12/site-packages

# pyproject reads CLAUDE.md as its long description, so hatchling needs it here.
COPY pyproject.toml CLAUDE.md ./
COPY curbcheck ./curbcheck
# --no-deps: everything the package needs is already installed above, at the
# hashed versions. --no-build-isolation: build with the hashed hatchling
# installed above rather than letting pip fetch one from PyPI unhashed, which
# used to be the only install in the image that --require-hashes did not cover.
RUN pip install --no-cache-dir --no-deps --no-build-isolation --prefix=/install .


FROM python:3.12-slim

# A fixed uid so the ./data bind mount can be chowned to something stable.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin curbcheck

COPY --from=build /install /usr/local

# `curbcheck.cli` resolves the frontend as `Path(config.__file__).parent.parent
# / "web"`, which inside the image is the site-packages directory. Putting web/
# there is what makes the static mount find it without patching any code.
COPY web /usr/local/lib/python3.12/site-packages/web

# The database, data/raw/ and the basemap archive all live here. config.py
# reads CURBCHECK_DATA_DIR, so this is also where `curbcheck sync` writes.
ENV CURBCHECK_DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown curbcheck:curbcheck /data
VOLUME ["/data"]

USER curbcheck
WORKDIR /data

# Loopback inside the container's network namespace; see the compose file.
EXPOSE 8765

ENTRYPOINT ["curbcheck"]
CMD ["serve", "--db", "/data/curbcheck.sqlite"]

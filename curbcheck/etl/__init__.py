"""Offline-capable ETL: download NYC Open Data, resolve curb geometry, write SQLite.

Step order is fetch -> stage -> streets -> snap -> segments -> parse -> meters ->
calendar -> load (docs/ARCHITECTURE.md). Only `fetch` touches the network, and
only `build` touches the database; everything between them is pure functions
over dataclasses so it can be tested without fixtures.
"""

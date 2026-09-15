"""Offline-capable ETL: download NYC Open Data, resolve curb geometry, write SQLite.

Step order is fetch -> stage -> streets -> snap -> parse -> segments -> meters ->
calendar -> load (docs/ARCHITECTURE.md): `segments` reads the grammar's family
key and arrow arity, so the parse has to exist before the spans do. Only `fetch`
touches the network, and only `build` touches the database; everything between
them is pure functions over dataclasses so it can be tested without fixtures.
"""

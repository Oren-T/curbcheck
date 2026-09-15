"""The import boundaries `docs/ARCHITECTURE.md` promises, enforced by a scan.

Three of the rules in STYLE_GUIDE §5 are about *where* a capability may live
rather than about how it is used, so no unit test of behaviour can catch a
violation. They are checked here against the AST of every module in the package:

- only `curbcheck/net.py` may reach the network;
- `etl/` and `api/` never import each other, which is what keeps the parsers
  that read downloaded files out of the process that serves the browser;
- no module holds a wildcard bind address, and the bind host is a constant
  rather than something the environment or a flag can widen.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from curbcheck.config import REPO_ROOT

PACKAGE_ROOT = REPO_ROOT / "curbcheck"

# Anything that can open a socket. `urllib.parse` is pure string handling and is
# deliberately not on the list; `urllib.request` is.
NETWORK_MODULES = frozenset(
    {
        "httpx",
        "httpcore",
        "requests",
        "urllib.request",
        "urllib3",
        "socket",
        "ssl",
        "ftplib",
        "http.client",
        "asyncio.streams",
        "aiohttp",
        "telnetlib",
        "smtplib",
    }
)

# The two modules allowed to name a network library: net.py because it *is* the
# client, and cli.py because it starts uvicorn, which serves rather than fetches.
NETWORK_ALLOWED = frozenset({"curbcheck/net.py"})

# A wildcard bind address as a whole token, so the browser User-Agent's
# `Chrome/140.0.0.0` does not match. The IPv6 wildcards are compared whole,
# because a bare `::` also appears inside ordinary time regexes.
WILDCARD_BIND = re.compile(r"(?<![\d.])0\.0\.0\.0(?![\d.])")
IPV6_WILDCARDS = frozenset({"::", "[::]", "0:0:0:0:0:0:0:0"})


def python_sources() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def module_id(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def imported_modules(tree: ast.Module) -> set[str]:
    """Every dotted module name the file imports, including partial prefixes."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_scan_has_modules_to_look_at() -> None:
    assert len(python_sources()) > 20


@pytest.mark.parametrize("path", python_sources(), ids=module_id)
def test_only_net_py_can_reach_the_network(path: Path) -> None:
    """STYLE_GUIDE §5: every outbound request goes through `curbcheck.net`."""
    found = imported_modules(ast.parse(path.read_text(encoding="utf-8"))) & NETWORK_MODULES
    if module_id(path) in NETWORK_ALLOWED:
        return
    assert not found, f"{module_id(path)} imports {sorted(found)}; route it through curbcheck.net"


def test_net_py_is_the_module_that_imports_httpx() -> None:
    """The allowlist is only a control if there is no second client beside it."""
    users = [
        module_id(path)
        for path in python_sources()
        if "httpx" in imported_modules(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert users == ["curbcheck/net.py"]


@pytest.mark.parametrize("path", python_sources(), ids=module_id)
def test_the_etl_and_the_api_never_import_each_other(path: Path) -> None:
    imports = imported_modules(ast.parse(path.read_text(encoding="utf-8")))
    relative = module_id(path)
    if relative.startswith("curbcheck/etl/"):
        forbidden = {name for name in imports if name.startswith("curbcheck.api")}
    elif relative.startswith("curbcheck/api/"):
        forbidden = {name for name in imports if name.startswith("curbcheck.etl")}
    else:
        return
    assert not forbidden, f"{relative} imports {sorted(forbidden)}"


@pytest.mark.parametrize("path", python_sources(), ids=module_id)
def test_no_module_binds_the_wildcard_address(path: Path) -> None:
    """SPEC §3.4 (threat T4): a wildcard bind address as a string is a blocker.

    Matched against string literals rather than raw text, so the comment in
    `config.py` that says never to use one is not a false positive.
    """
    literals = {
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    wildcards = {
        value for value in literals if WILDCARD_BIND.search(value) or value in IPV6_WILDCARDS
    }

    assert not wildcards, f"{module_id(path)} holds a wildcard bind address: {sorted(wildcards)}"


def test_the_bind_host_is_a_constant_not_an_environment_variable() -> None:
    """A `CURBCHECK_HOST` would make the loopback bind a default instead of a rule."""
    config_source = (PACKAGE_ROOT / "config.py").read_text(encoding="utf-8")
    cli_source = (PACKAGE_ROOT / "cli.py").read_text(encoding="utf-8")

    assert 'BIND_HOST = "127.0.0.1"' in config_source
    assert "host=BIND_HOST" in cli_source
    assert "--host" not in cli_source.replace("# No --host.", "")


def test_the_scan_would_catch_a_second_http_client() -> None:
    tree = ast.parse("import requests\nfrom urllib import request\nimport urllib.request\n")

    assert imported_modules(tree) & NETWORK_MODULES == {"requests", "urllib.request"}

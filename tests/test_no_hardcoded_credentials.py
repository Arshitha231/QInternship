"""A Grafana Cloud token was committed hardcoded into frontend/src/main.tsx
(commit 2c50915) and stayed there for 87 commits.

The damage was not that it sat in git history -- it was that main.tsx is a
file Vite BUNDLES. Every deploy inlined the token into
/assets/index-*.js and served it to every visitor of a public site, with no
login. By the time it was noticed, rewriting history would have achieved
nothing: the credential had already been published by the application
itself. Only revoking it helps, and that needs whoever owns the account.

That is the specific failure mode worth catching automatically, because it
is invisible in review: `Authorization: 'Basic ...'` in a .tsx file looks
exactly like the same line in a server file, and only one of them is a
broadcast. Anything a bundler can reach must get its credentials from
import.meta.env at build time, or -- better, for anything that actually
needs protecting -- go through our own API, since a browser cannot hold a
secret at all.

GitHub push protection is now enabled on the repo and covers vendor tokens
with recognisable prefixes. This covers what it does not: our own shapes,
and Basic-auth blobs that match no vendor pattern.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Everything a bundler inlines and ships to the browser. Backend source is
# deliberately out of scope here -- a literal in a server file is a different
# (lesser) problem, and .env / app settings already cover that path.
BUNDLED_ROOTS = [REPO_ROOT / "frontend" / "src"]
BUNDLED_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte"}

# Shapes that are credentials wherever they appear. Kept narrow on purpose:
# a matcher that cries wolf gets suppressed, and then it is worth nothing.
CREDENTIAL_PATTERNS = [
    # HTTP Basic/Bearer with a literal value rather than a variable. The
    # {16,} floor is what separates a real token from `Bearer ${t}` or the
    # word appearing in a comment.
    (r"""["']\s*(?:Basic|Bearer)\s+[A-Za-z0-9+/=_.\-]{16,}\s*["']""",
     "hardcoded Authorization header value"),
    (r"\bglc_[A-Za-z0-9+/=_-]{20,}", "Grafana Cloud token"),
    (r"\bsk-[A-Za-z0-9]{32,}", "OpenAI-style API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}", "GitHub token"),
    # Azure connection strings -- the one secret shape most likely to be
    # pasted into a frontend "just to test something".
    (r"AccountKey=[A-Za-z0-9+/=]{40,}", "Azure storage account key"),
]


def _bundled_sources() -> list[Path]:
    return sorted(
        path
        for root in BUNDLED_ROOTS
        if root.is_dir()
        for path in root.rglob("*")
        if path.is_file() and path.suffix in BUNDLED_SUFFIXES
    )


def test_there_is_bundled_source_to_scan() -> None:
    """Guards the guard. If the frontend moves, the scan below starts
    passing vacuously over zero files and nobody finds out."""
    sources = _bundled_sources()
    assert len(sources) > 10, (
        f"expected frontend/src to hold bundled source, found {len(sources)} "
        "files -- did the frontend move? Update BUNDLED_ROOTS."
    )


@pytest.mark.parametrize("pattern,label", CREDENTIAL_PATTERNS,
                         ids=[label for _, label in CREDENTIAL_PATTERNS])
def test_no_credentials_in_browser_bundled_source(pattern: str, label: str) -> None:
    compiled = re.compile(pattern)
    hits: list[str] = []

    for path in _bundled_sources():
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if compiled.search(line):
                rel = path.relative_to(REPO_ROOT)
                hits.append(f"  {rel}:{lineno}")

    assert not hits, (
        f"{label} found in source that Vite bundles and ships to the browser:\n"
        + "\n".join(hits)
        + "\n\nAnything here is served publicly in /assets/index-*.js -- it is "
        "not a secret and cannot be made one by moving it. Read it from "
        "import.meta.env at build time if it is merely configuration, or "
        "proxy the call through our own API if it genuinely needs protecting."
    )

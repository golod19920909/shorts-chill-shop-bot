"""Shared low-level text primitives that must not import feature packages."""

from __future__ import annotations

import re

# Unicode letter runs preserve Cyrillic, Ukrainian and English tokens.
# Keep this primitive outside services.convsearch: importing a submodule of
# that package executes its eager __init__, which loads the search engine.
TOKEN_RE = re.compile(r"[^\W\d_][^\W_]*|\d+", re.UNICODE)

"""Sphinx configuration for the htan documentation."""

from __future__ import annotations

import importlib.metadata

project = "htan"
author = "HTAN DCC"
copyright = "2026, HTAN DCC"

try:
    release = importlib.metadata.version("htan")
except importlib.metadata.PackageNotFoundError:
    release = "0.0.0"
version = ".".join(release.split(".")[:2])

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_click",
    "myst_parser",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# Mock heavy / optional imports so autodoc works on Read the Docs without them.
autodoc_mock_imports = [
    "synapseclient",
    "gen3",
    "google",
    "pandas",
    "db_dtypes",
    "certifi",
]

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
autosummary_generate = True
napoleon_google_docstring = True
napoleon_numpy_docstring = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "click": ("https://click.palletsprojects.com/en/stable/", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
}

html_theme = "furo"
html_title = f"htan {release}"
html_static_path = ["_static"]

# Don't fail the build on the missing _static dir on a fresh checkout.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

myst_enable_extensions = ["colon_fence", "deflist"]

# Sphinx-click introspects Click groups by import path.
sphinx_click_attrs = ["cli"]

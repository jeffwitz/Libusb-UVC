"""Sphinx configuration for the libusb-uvc project."""

from __future__ import annotations

import datetime
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

project = "libusb-uvc"
author = "Libusb-UVC Contributors"
current_year = datetime.datetime.now().year
copyright = f"{current_year}, {author}"

try:
    from libusb_uvc import __version__ as release
except Exception:  # pragma: no cover - fallback when package unavailable
    release = "0.0.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

autosummary_generate = True
autodoc_member_order = "bysource"
autodoc_default_options = {
    "show-inheritance": True,
    "members": True,
    "undoc-members": False,
}

napoleon_google_docstring = True
napoleon_numpy_docstring = False

autodoc_mock_imports = [
    "usb",
    "usb1",
    "cv2",
    "numpy",
    "PIL",
    "gi",
]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

templates_path = ["_templates"]
exclude_patterns = ["_build"]

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_logo = '_static/logo.svg'

"""
ISA-101 color palettes for the desktop GUI: neutral gray shell with color
reserved for state/alarm only. Kept separate from isa101_views.py so the
active palette can be swapped without touching widget-construction code.

Widget stylesheets and pyqtgraph pens bake these constants in as literal
strings at construction time (f"...{BORDER}..."), so switching the palette
only takes effect for widgets built AFTER set_theme() runs. In practice this
means set_theme() must be called once, at startup, before isa101_views is
imported — there is no live in-app toggle, it's a restart-to-apply setting
(config.system.ui_theme).
"""

LIGHT = dict(
    BG="#b9bdc1", PANEL="#c9cdd1", PANEL2="#d7dadd", FIELD="#eceef0",
    BORDER="#8c9296", TEXT="#1d2123", MUTED="#54595d",
    OK="#2e7d32", WARN="#c98a00", CRIT="#c62828", INFO="#1565c0", NEUTRAL="#6b7075",
)

DARK = dict(
    BG="#1c1e21", PANEL="#26292d", PANEL2="#323639", FIELD="#3a3e42",
    BORDER="#4a4f54", TEXT="#e8eaec", MUTED="#a3a9ae",
    OK="#4caf50", WARN="#ffb300", CRIT="#ef5350", INFO="#42a5f5", NEUTRAL="#8a9096",
)

BG = LIGHT["BG"]
PANEL = LIGHT["PANEL"]
PANEL2 = LIGHT["PANEL2"]
FIELD = LIGHT["FIELD"]
BORDER = LIGHT["BORDER"]
TEXT = LIGHT["TEXT"]
MUTED = LIGHT["MUTED"]
OK = LIGHT["OK"]
WARN = LIGHT["WARN"]
CRIT = LIGHT["CRIT"]
INFO = LIGHT["INFO"]
NEUTRAL = LIGHT["NEUTRAL"]


def set_theme(name: str) -> None:
    """Select the active palette ("light" or "dark"). Call before importing
    isa101_views — see module docstring for why this can't be a live toggle."""
    globals().update(DARK if name == "dark" else LIGHT)

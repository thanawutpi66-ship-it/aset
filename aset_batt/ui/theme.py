"""
ISA-101 color palettes for the desktop GUI, layered on top of qt-material's
Material Design theme. qt-material owns the "look" of standard widgets
(buttons, fields, tabs, menus, dialogs — applied once via apply_stylesheet());
this module derives the handful of base surface/text colors used by
custom-drawn panels and pyqtgraph plots (BG/PANEL/PANEL2/FIELD/BORDER/TEXT/
MUTED) from the active qt-material theme, so they match. OK/WARN/CRIT/INFO/
NEUTRAL stay fixed safety-status colors (green/amber/red/blue/gray) independent
of qt-material's decorative accent — ISA-101 reserves color for alarms/state,
so the accent used for ordinary shell chrome (tabs, checkboxes, scrollbars) is
a separate, deliberately desaturated blue-grey (see MATERIAL_THEMES's custom
XMLs) — operators rely on the OK/WARN/CRIT/INFO convention regardless of theme
or accent.

Live retheme: colors are no longer baked into widgets at construction time.
Instead of `widget.setStyleSheet(f"...{PANEL2}...")`, call
`theme.style(widget, lambda: f"...{theme.PANEL2}...")` — it applies the
stylesheet now AND re-applies it on every future retheme(). For anything that
isn't a plain stylesheet (pyqtgraph pens, state-dependent label colors),
register a callback with `theme.on_retheme(fn)`. `retheme(name)` swaps the
palette and drives both mechanisms; the caller is still responsible for
re-running qt-material's `apply_stylesheet()` against the live QApplication
instance (that part needs the app object, which this module doesn't hold).
"""

import logging
import os
import weakref

logger = logging.getLogger(__name__)

# Custom qt-material XMLs (not a stock hue) — ISA-101 calls for a desaturated,
# mostly-grayscale shell with color reserved for alarms/state, so qt-material's
# own decorative accent (tabs, checkboxes, scrollbars, focus rings) uses a
# muted steel blue-grey here instead of a stock Material hue like vivid cyan.
# The neutral secondaryColor/secondaryLightColor/secondaryDarkColor and
# primaryTextColor/secondaryTextColor are otherwise identical to qt-material's
# stock dark_cyan.xml/light_cyan_500.xml — only primaryColor/primaryLightColor
# (the accent) changed. theme.INFO does NOT read this accent (see
# _material_overrides()) — it's a real alarm/state color and stays the
# original vivid blue regardless of the shell's accent choice.
_THEMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "material_themes")
MATERIAL_THEMES = {
    "light": os.path.join(_THEMES_DIR, "isa101_light.xml"),
    "dark": os.path.join(_THEMES_DIR, "isa101_dark.xml"),
}

# Light palette (ก.ค. 2026, operator feedback: "ปรับสีให้ขาวขึ้น เหมือน Thonny"):
# canvas/cards lightened back toward near-white — Thonny reads as flat white
# with subtle grey distinctions, defined mostly by borders rather than filled
# grey blocks. PANEL stays a visible mid-grey (darker than BG/PANEL2) so
# buttons keep the definition from the earlier "ปุ่มขาวเกินไป" fix — only the
# canvas/card surfaces got lighter, not the buttons sitting on them.
# Fallback only — with qt-material installed these are overridden from
# material_themes/isa101_light.xml (kept in sync, same values).
LIGHT = dict(
    BG="#eef0f1", PANEL="#dde0e2", PANEL2="#f5f6f7", FIELD="#ebecee",
    BORDER="#afb0b1", TEXT="#1d2123", MUTED="#43484c",
    OK="#2e7d32", WARN="#c98a00", CRIT="#c62828", INFO="#1565c0", NEUTRAL="#6b7075",
    # GRAPH_BG: plot canvas only, kept at its own near-white value (operators
    # screenshot trend/ICA plots into project reports). Dark mode keeps the
    # PANEL2 surface.
    GRAPH_BG="#fdfdfe",
)

DARK = dict(
    BG="#1c1e21", PANEL="#26292d", PANEL2="#323639", FIELD="#3a3e42",
    BORDER="#4a4f54", TEXT="#e8eaec", MUTED="#a3a9ae",
    OK="#4caf50", WARN="#ffb300", CRIT="#ef5350", INFO="#42a5f5", NEUTRAL="#8a9096",
    GRAPH_BG="#323639",   # = PANEL2: dark plots stay on the panel surface
)


def _to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, v)):02x}" for v in rgb)


def _adjust(hex_color, delta):
    r, g, b = _to_rgb(hex_color)
    return _to_hex((r + delta, g + delta, b + delta))


def _luminance(hex_color):
    r, g, b = _to_rgb(hex_color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_text(hex_color: str) -> str:
    """Pick a readable text color (near-black or white) for an arbitrary
    background hex via relative luminance. Used as _btn()'s auto foreground so
    a neutral/surface background — which can be light in one theme and dark in
    the other (PANEL2/FIELD), or just a fixed light literal (e.g. a pale
    accent tint) — always gets legible text without every caller having to
    remember which foreground pairs with which background in which theme."""
    return "#1a1a1a" if _luminance(hex_color) > 140 else "#ffffff"


def _material_overrides(material_theme: dict) -> dict:
    """Map a qt-material theme dict (qt_material.get_theme() result) onto our
    base surface/text roles. Elevation order in qt-material's own theme XMLs
    is secondaryColor (canvas) -> secondaryDarkColor (mid) -> secondaryLightColor
    (most elevated) — true in both light and dark variants, so this mapping
    doesn't need to branch on which one is active except for which direction
    to nudge FIELD/BORDER for contrast."""
    bg = material_theme["secondaryColor"]
    panel = material_theme["secondaryDarkColor"]
    panel2 = material_theme["secondaryLightColor"]
    dark_mode = _luminance(bg) < 128
    field = _adjust(panel2, 15 if dark_mode else -10)
    border = _adjust(panel2, 40 if dark_mode else -70)
    # INFO is deliberately NOT sourced from material_theme["primaryColor"] here
    # — that's qt-material's decorative shell accent (tabs/checkboxes/scroll-
    # bars), tuned to be muted/desaturated per ISA-101. INFO is a real
    # alarm/state color (charging, running, active) and stays the original
    # vivid blue from LIGHT/DARK regardless of the shell's accent choice.
    return dict(
        BG=bg, PANEL=panel, PANEL2=panel2, FIELD=field, BORDER=border,
        TEXT=material_theme["primaryTextColor"],
        MUTED=material_theme["secondaryTextColor"],
    )


_style_registry = []  # [(weakref.ref(widget), fn), ...]
_retheme_hooks = []   # [callback, ...]
_current_name = "light"


def current_theme() -> str:
    return _current_name


_material_css_cache = {}


def get_material_stylesheet(mode: str) -> str:
    """Build (or return a cached) qt-material app-wide stylesheet string for
    "light"/"dark". qt_material.build_stylesheet() re-does Jinja2 templating
    and re-embeds every icon as a fresh data URI on EVERY call — measured at
    ~0.5-0.8s, vs. ~0.25ms for actually applying an already-built stylesheet
    string via QApplication.setStyleSheet(). Caching means only the first
    switch to a given theme pays that cost; toggling back is then instant."""
    invert_secondary = (mode == "light")
    key = (MATERIAL_THEMES[mode], invert_secondary)
    if key not in _material_css_cache:
        import qt_material
        _material_css_cache[key] = qt_material.build_stylesheet(
            theme=key[0], invert_secondary=key[1])
    return _material_css_cache[key]


def set_theme(name: str) -> None:
    """Select the active palette ("light" or "dark"), preferring colors
    derived from the matching qt-material theme file and falling back to the
    bundled LIGHT/DARK dict if qt-material isn't installed. Only updates this
    module's globals — does not touch any widget (see retheme() for that)."""
    global _current_name
    _current_name = "dark" if name == "dark" else "light"
    base = dict(DARK if _current_name == "dark" else LIGHT)
    try:
        import qt_material
        material = qt_material.get_theme(MATERIAL_THEMES[_current_name])
        base.update(_material_overrides(material))
    except Exception as exc:
        logger.debug("qt-material theme unavailable, using fallback palette: %s", exc)
    globals().update(base)


def style(widget, fn) -> None:
    """Apply fn() (a zero-arg callable returning a stylesheet string built
    from the theme.* constants, e.g. `lambda: f"color:{theme.MUTED};"`) to
    widget now, and register it to be re-applied on every future retheme()."""
    widget.setStyleSheet(fn())
    _style_registry.append((weakref.ref(widget), fn))


def on_retheme(callback) -> None:
    """Register a zero-arg callback to run after every retheme() — for
    refreshing pyqtgraph pens, state-dependent label colors, etc."""
    _retheme_hooks.append(callback)


def retheme(name: str) -> None:
    """Switch the active theme immediately: updates the palette constants,
    re-applies every stylesheet registered via style(), and runs every
    callback registered via on_retheme(). The caller is still responsible for
    re-running qt-material's apply_stylesheet() against the QApplication
    instance — this module has no handle on the app object."""
    set_theme(name)
    alive = []
    for ref, fn in _style_registry:
        widget = ref()
        if widget is None:
            continue
        try:
            widget.setStyleSheet(fn())
        except RuntimeError:
            continue  # underlying C++ object already destroyed
        alive.append((ref, fn))
    _style_registry[:] = alive
    for callback in list(_retheme_hooks):
        try:
            callback()
        except Exception as exc:
            logger.error("retheme hook failed: %s", exc)


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
GRAPH_BG = LIGHT["GRAPH_BG"]

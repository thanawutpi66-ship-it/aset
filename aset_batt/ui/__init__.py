"""UI package for ASET Battery app — PySide6 GUI.

Deliberately does not eagerly import isa101_views here: aset_batt.app.run
must call theme.set_theme() before isa101_views is imported (widget
stylesheets bake the active palette in at construction time), and an
eager re-export in this __init__ would import isa101_views the moment
any sibling submodule (e.g. aset_batt.ui.theme) is imported, defeating
that ordering. Import BatteryQtWindow/QtRootShim from
aset_batt.ui.isa101_views directly instead.
"""

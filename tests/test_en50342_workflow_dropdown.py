"""Regression test: the EN 50342-1 lead-acid standard test is selectable from
the AUTO-test Workflow dropdown, shares the IEC sequence's settings page, and
visibly presets the standard's own conditions.

Design: the standard's Cn test IS the same PREPARE->CHARGE->REST->DISCHARGE->
ANALYZE machinery as the IEC workflow — only the conditions differ — so item 4
maps to page 0 rather than duplicating a whole page (see _WF_PAGE_MAP). The
presets are applied to the VISIBLE widgets (reference-rate combo, skip
checkboxes) instead of silently overriding at run time: the operator sees
exactly what will run, and if they change anything the run is re-labelled
non-standard by en50342_capacity_conditions() at the end rather than lying.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


class TestEn50342WorkflowItem(unittest.TestCase):
    def setUp(self):
        cfg = ConfigManager()
        cfg.battery.battery_type = "LeadAcid"
        self.win = BatteryQtWindow(cfg)

    def tearDown(self):
        self.win.close()

    def test_dropdown_contains_the_standard_item(self):
        items = [self.win.cb_workflow_type.itemText(i)
                 for i in range(self.win.cb_workflow_type.count())]
        self.assertIn("EN 50342-1 Lead-Acid C10", items)

    def test_selecting_it_shows_the_iec_page_with_standard_presets(self):
        # dirty the settings first so the presets are observable
        self.win.cb_test_crate.setCurrentText("0.5C")
        self.win.chk_skip_charge.setChecked(True)
        self.win.chk_skip_rest.setChecked(True)

        self.win.cb_workflow_type.setCurrentIndex(self.win._WF_EN50342_INDEX)

        self.assertEqual(self.win._wf_stack.currentIndex(), 0)   # shared IEC page
        self.assertEqual(self.win.cb_test_crate.currentText(), "0.1C")  # I10 (C10 rating)
        self.assertFalse(self.win.chk_skip_charge.isChecked())
        self.assertFalse(self.win.chk_skip_rest.isChecked())

    def test_other_items_still_map_to_their_own_pages(self):
        for idx, page in ((1, 1), (2, 2), (3, 3), (0, 0)):
            self.win.cb_workflow_type.setCurrentIndex(idx)
            self.assertEqual(self.win._wf_stack.currentIndex(), page)


if __name__ == "__main__":
    unittest.main()

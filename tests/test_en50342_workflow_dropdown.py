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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    def test_rest_workflow_description_tracks_configured_duration(self):
        for minutes in (30, 60, 45):
            self.win.spn_rest_min.setValue(minutes)
            self.assertEqual(self.win._wf_desc_lbls[2].text(), f"{minutes} min rest")

        self.win.cb_test_crate.setCurrentText("0.5C")
        self.assertIn("Discharge 0.5C =", self.win._wf_desc_lbls[3].text())

    def test_rest_label_refresh_does_not_change_runtime_widget_value(self):
        self.win.spn_rest_min.setValue(45)
        self.win._refresh_wf_rest_description()
        self.assertEqual(self.win.spn_rest_min.value(), 45)
        self.assertEqual(self.win._wf_desc_lbls[2].text(), "45 min rest")

    def test_validation_preset_shows_effective_60_min_rest(self):
        self.win.spn_rest_min.setValue(30)
        self.win.chk_skip_charge.setChecked(True)
        self.win.chk_skip_rest.setChecked(True)
        self.win.config.system.validation_campaign = {
            "enabled": True, "campaign_id": "campaign-test", "specimen_id": "specimen-test"
        }
        self.win.hw = SimpleNamespace(is_connected=True, current_temp=25.0,
                                      read_vi=lambda: (12.6, 0.0, 0.0))
        self.win.controller = MagicMock()
        self.win.controller.config = self.win.config
        self.win.controller.estimator.soc = 90.0
        self.win._busy_reason = lambda: None
        shown_plan = []
        self.win._show_pretest_dialog = lambda _title, plan, **_kwargs: (shown_plan.extend(plan) or True)
        self.win._seq_common_start = lambda *_args: True
        spawned = {}
        self.win._spawn_sequence_worker = lambda *args, **_kwargs: spawned.update(args=args, kwargs=_kwargs)

        self.win._on_auto_sequence()

        self.assertTrue(any("REST 60 min" in line for line in shown_plan))
        self.assertEqual(self.win._wf_desc_lbls[2].text(), "60 min rest")
        opts = spawned["kwargs"]["args"][0]
        self.assertEqual(opts["rest_min"], 60)
        self.assertEqual(opts["test_crate"], "0.1C")
        self.assertFalse(opts["skip_charge"])
        self.assertFalse(opts["skip_rest"])

    def test_pretest_invokes_shared_temp_aware_condition_checker(self):
        from aset_batt.ui.sequences import base

        self.win.hw = SimpleNamespace(is_connected=True, current_temp=25.0,
                                      read_vi=lambda: (12.6, 0.0, 0.0))
        self.win.controller = MagicMock()
        self.win.controller.config = self.win.config
        self.win.controller.estimator.soc = 90.0
        self.win._busy_reason = lambda: None
        self.win._show_pretest_dialog = lambda *args, **kwargs: True
        self.win._seq_common_start = lambda *args, **kwargs: True
        self.win._spawn_sequence_worker = MagicMock()

        with patch("aset_batt.ui.sequences.iec_capacity.en50342_capacity_conditions",
                   wraps=base.en50342_capacity_conditions) as checker:
            self.win._on_auto_sequence()

        checker.assert_called_once()
        self.assertEqual(checker.call_args.kwargs["temp_c"], 25.0)


if __name__ == "__main__":
    unittest.main()

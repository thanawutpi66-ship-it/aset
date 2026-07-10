import pytest
from unittest.mock import patch, MagicMock
from aset_batt.app.run import run

def test_run_main():
    with patch('aset_batt.app.app_bootstrapper.ApplicationBootstrapper') as mock_boot, \
         patch('PySide6.QtWidgets.QApplication') as mock_app, \
         patch('aset_batt.ui.isa101_views.BatteryQtWindow') as mock_win, \
         patch('aset_batt.ui.isa101_views.QtRootShim') as mock_root, \
         patch('aset_batt.ui.theme.get_material_stylesheet') as mock_theme:
         
        mock_theme.return_value = ""
         
        mock_boot.return_value.initialize.return_value = True
        mock_boot.return_value.config_manager.system.ui_theme = "light"
        mock_boot.return_value.config_manager.load_error = None
        
        # Test success
        run()
        mock_boot.return_value.initialize.assert_called()
        
        # Test fail init
        mock_boot.return_value.initialize.return_value = False
        run()
        mock_boot.return_value.cleanup.assert_called()

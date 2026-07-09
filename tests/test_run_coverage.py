import pytest
from unittest.mock import patch, MagicMock
from aset_batt.app.run import run

def test_run_main():
    with patch('aset_batt.app.app_bootstrapper.ApplicationBootstrapper') as mock_boot, \
         patch('PySide6.QtWidgets.QApplication') as mock_app, \
         patch('aset_batt.ui.isa101_views.BatteryQtWindow') as mock_win, \
         patch('aset_batt.ui.isa101_views.QtRootShim') as mock_root:
         
        mock_boot.return_value.initialize.return_value = True
        
        # Test success
        run()
        mock_boot.return_value.initialize.assert_called()
        
        # Test fail init
        mock_boot.return_value.initialize.return_value = False
        run()
        mock_boot.return_value.cleanup.assert_called()

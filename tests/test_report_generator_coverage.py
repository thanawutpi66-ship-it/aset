import pytest
import os
import tempfile
from unittest.mock import patch, MagicMock
from aset_batt.storage.report_generator import generate_pdf_report, _render_csv_plot, _info_table

def test_generate_pdf_report():
    with patch('aset_batt.storage.report_generator.SimpleDocTemplate') as mock_doc, \
         patch('aset_batt.storage.report_generator._render_csv_plot', return_value="test.png"), \
         patch('aset_batt.storage.report_generator.os.remove'):
         
        config = MagicMock()
        config.battery.battery_type = "LFP"
        config.battery.cells_series = 4
        config.battery.cells_parallel = 1
        config.battery.pack_nominal_voltage = 12.8
        config.battery.rated_capacity = 100.0
        config.battery.mass_grams = 10000
        
        estimator = MagicMock()
        estimator.get_state.return_value = {"soc": 50.0, "soh": 100.0, "rin": 0.01, "ah_accumulated": 1.0}
        
        analysis = {
            "grade": "A",
            "confidence": 0.9,
            "soh": 100.0,
            "capacity_ah": 100.0,
            "dcir_mohm": 10.0,
            "r0_mohm": 5.0,
            "r1_mohm": 5.0,
            "tau_s": 10.0,
            "ecm_identified": True,
            "ecm_r2": 0.99,
            "quality_warnings": []
        }
        
        generate_pdf_report("out.pdf", config, estimator, analysis, "dummy.csv")
        mock_doc.return_value.build.assert_called()

def test_render_csv_plot():
    # Create a real temporary CSV file
    with tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8-sig") as f:
        f.write("Elapsed_s,Voltage_V,Current_A\n")
        f.write("0,12.0,0.0\n")
        f.write("1,12.1,1.0\n")
        csv_path = f.name
        
    try:
        plot_path = _render_csv_plot(csv_path)
        assert plot_path is not None
        assert os.path.exists(plot_path)
        os.remove(plot_path)
    finally:
        os.remove(csv_path)

def test_info_table():
    _info_table([["Name", "Test"]])

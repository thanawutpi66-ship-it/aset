import csv
import os
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class DataHandler:
    def __init__(self):
        self.is_recording = False
        self.csv_file = None
        self.csv_writer = None

    def start_logging(self, filepath: str):
        """เริ่มบันทึก CSV — คืน (True, "") หรือ (False, error_message)"""
        try:
            self.csv_file = open(filepath, 'a', newline='', encoding='utf-8-sig')
            self.csv_writer = csv.writer(self.csv_file)
            # เขียน header เฉพาะเมื่อไฟล์ใหม่ (ขนาด 0)
            if os.path.getsize(filepath) == 0:
                self.csv_writer.writerow([
                    "Timestamp", "Elapsed_s",
                    "Voltage_V", "Current_A",
                    "SoC_pct", "Resistance_mOhm", "Temperature_C"
                ])
            self.is_recording = True
            return True, "Success"
        except Exception as e:
            return False, str(e)

    def stop_logging(self):
        self.is_recording = False
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception:
                pass
            self.csv_file = None

    def log_row(self, elapsed_s: float, v: float, i_net: float,
                soc: float, resistance_mohm: float, temp_c: float):
        """
        บันทึก 1 แถวข้อมูล

        Args:
            elapsed_s      : วินาทีที่ผ่านไปนับจากเริ่ม test (ไม่ใช่ unix timestamp)
            v              : Voltage (V)
            i_net          : Net current (A)
            soc            : State of Charge (%)
            resistance_mohm: Internal resistance (mΩ)
            temp_c         : Temperature (°C)
        """
        if self.is_recording and self.csv_writer:
            try:
                self.csv_writer.writerow([
                    datetime.now().strftime("%H:%M:%S"),
                    f"{elapsed_s:.1f}",
                    f"{v:.4f}",
                    f"{i_net:.4f}",
                    f"{soc:.2f}",
                    f"{resistance_mohm:.2f}",
                    f"{temp_c:.2f}"
                ])
                self.csv_file.flush()
            except Exception as e:
                logger.error(f"CSV write error: {e}")

    @staticmethod
    def load_profile_csv(filepath: str, default_dt: float):
        """
        โหลด current profile จาก CSV

        รูปแบบที่รองรับ:
          - 2 คอลัมน์: current (A), duration (s)
          - 1 คอลัมน์: current (A) — ใช้ default_dt เป็น duration

        คืน: (data_list, None) หรือ (None, error_message)
        """
        data = []
        try:
            with open(filepath, 'r') as f:
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    try:
                        if len(row) >= 2:
                            data.append((float(row[0]), float(row[1])))
                        elif len(row) == 1:
                            data.append((float(row[0]), float(default_dt)))
                    except ValueError:
                        continue
            return data, None
        except Exception as e:
            return None, str(e)
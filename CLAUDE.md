# ASET Battery Tester — โครงสร้างโปรเจกต์

รีโปนี้มี 2 โปรแกรมแยกกัน:
- `aset_batt/` + `main.py` — โปรแกรมแล็บ (PySide6 GUI) คุมฮาร์ดแวร์ทดสอบแบตจริง
- `cloud_dashboard/` — เว็บแดชบอร์ด (Python stdlib) deploy อัตโนมัติขึ้น Azure ผ่าน GitHub Actions เมื่อ push เข้า main

## ⚠️ โครงสร้าง GUI ถูกรื้อใหม่ (ก.ค. 2026) — อ่านก่อนแก้โค้ด UI

`aset_batt/ui/isa101_views.py` เคยเป็นไฟล์เดียว 5,700 บรรทัด ตอนนี้ถูกแยกเป็น
mixin หลายไฟล์แล้ว **อย่าหาเมธอดใน isa101_views.py อย่างเดียว — ให้ grep ทั้งแพ็กเกจ `aset_batt/ui/`**

| ไฟล์ | มีอะไร |
|---|---|
| `isa101_views.py` | คลาสหลัก `BatteryQtWindow` (signals, `__init__`, slots หลัก, connect/disconnect, cloud push, sessions/PDF/tools handlers, closeEvent) |
| `zones.py` | `ZonesMixin` — โค้ดสร้าง UI: โซน SETUP, workflow guide, หน้า RUN/charge/discharge/HPPC, TEST MODE, TOOLS, แท็บฝั่งขวา |
| `sequences.py` | `SequencesMixin` — เธรดเทสต์อัตโนมัติ 4 ตัว (IEC, Quick Scan, HPPC, Cycle Life) + workflow slots + pre-test dialog + safety helpers |
| `characterize.py` | `CharacterizeMixin` — แท็บ CHARACTERIZE (UI + เธรด Peukert/ETA/GITT) |
| `widgets.py` | widget อิสระ: DigitalReadout, TemperatureGauge, กราฟ trend, QtRootShim, `_btn`/`_hline`, PDF task |
| `report_html.py` | ฟังก์ชัน dict→HTML: `format_seq_result`, `build_results_html` (เทสต์ได้โดยไม่ต้องโหลด GUI) |
| `theme.py` | พาเลตสี LIGHT/DARK + `set_theme()` |

กติกาสำคัญ:
- **Mixin ไม่มี state/signal ของตัวเอง** — signal ทุกตัวประกาศใน `BatteryQtWindow` เท่านั้น (ข้อจำกัด PySide6) เมธอดใน mixin อ้าง `self.` ได้ตามปกติเพราะสุดท้ายเป็น object เดียวกัน
- **ลำดับ import ห้ามสลับ**: `theme.set_theme()` ต้องถูกเรียกก่อน import `isa101_views` (ทำแล้วใน `aset_batt/app/run.py`) เพราะสีถูกฝังใน stylesheet ตอนสร้าง widget — ห้ามเพิ่ม import isa101_views/zones/widgets ระดับ module ในไฟล์ที่ถูกโหลดก่อน run() และห้ามเพิ่ม eager import ใน `aset_batt/ui/__init__.py`
- ธีมมืดตั้งผ่าน checkbox ใน Tools → APPEARANCE (`config.system.ui_theme`) มีผลหลังรีสตาร์ทเท่านั้น

## เช็คก่อน push

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

สร้างหน้าต่างทดสอบแบบไม่ต้องมีจอ (จับ error ในโค้ดสร้าง UI ได้ทุกโซน):

```bash
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -c "
from aset_batt.ui import theme; theme.set_theme('light')
from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow
app = QApplication([]); w = BatteryQtWindow(ConfigManager()); w.close(); print('OK')"
```

## Cloud dashboard

- push เข้า `main` → GitHub Actions build เฉพาะโฟลเดอร์ `cloud_dashboard/` แล้ว deploy ขึ้น Azure App Service อัตโนมัติ (~5 นาที) — ห้ามแก้ workflow ให้ build จาก root เพราะ requirements.txt ที่ root เป็นของ GUI
- เครื่องแล็บ push ข้อมูลขึ้นเว็บผ่าน `CloudPusher` (`aset_batt/storage/cloud_push.py`) — token อยู่ใน `cloud_token.txt` / env `INGEST_TOKEN` (gitignored, ต้องตรงกับที่ตั้งบน Azure)
- `aset_batt/services/cloud_push.py` (`CloudPushService`) เป็นของเก่าที่เลิกใช้แล้ว — อย่าเอากลับมาใช้

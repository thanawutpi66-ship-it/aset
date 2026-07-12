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
| `widgets.py` | กราฟ trend (`MultiAxisTrend`/`SplitTrend`/`TripleTrend`/`TrendContainer`) + shared crosshair (`TrendCrosshair`), `QtRootShim`, `_btn`/`_hline`, PDF task |
| `report_html.py` | ฟังก์ชัน dict→HTML: `format_seq_result`, `build_results_html` (เทสต์ได้โดยไม่ต้องโหลด GUI) |
| `theme.py` | พาเลตสี LIGHT/DARK (override ด้วยสีจาก qt-material ถ้าติดตั้งไว้) + registry สำหรับ live retheme (`style()`/`on_retheme()`/`retheme()`) |

กติกาสำคัญ:
- **Mixin ไม่มี state/signal ของตัวเอง** — signal ทุกตัวประกาศใน `BatteryQtWindow` เท่านั้น (ข้อจำกัด PySide6) เมธอดใน mixin อ้าง `self.` ได้ตามปกติเพราะสุดท้ายเป็น object เดียวกัน
- **UI ทั้งแอปใช้ธีม Material (qt-material)**: `aset_batt/app/run.py` เรียก `apply_stylesheet(app, theme=...)` หลังสร้าง `QApplication` เพื่อจัดหน้าตา widget มาตรฐานทั้งหมด (ปุ่ม/คอมโบบ็อกซ์/แท็บ/เมนู) — **ห้ามเพิ่ม window-level `setStyleSheet()` ที่ครอบ selector กว้างๆ อย่าง `QWidget`/`QComboBox`/`QTabBar`** เพราะ Qt QSS cascade จะให้ per-widget stylesheet ชนะทับธีม material ทันที (เคยเป็นบั๊กนี้มาก่อน — ดู `_build_ui` ใน `isa101_views.py`) สีเฉพาะทาง ISA-101 (badge/LED/alarm/pen กราฟ) ยังคง set ตรงจุดได้ตามปกติ
- **ธีมสลับสดได้ ไม่ต้อง restart**: `aset_batt/ui/theme.py` เก็บค่าพาเลตเป็น module global ธรรมดา — โค้ดที่ต้องการให้สีอัปเดตสดต้องอ่านผ่าน `from aset_batt.ui import theme` แล้วใช้ `theme.PANEL2`/`theme.INFO`/ฯลฯ (ไม่ใช่ `from aset_batt.ui.theme import PANEL2` ซึ่งจะ freeze ค่าไว้ตอน import) widget ที่ set stylesheet **ครั้งเดียวตอนสร้าง แล้วไม่มีจุดไหนเรียกซ้ำ** ต้องห่อด้วย `theme.style(widget, fn)` (จะ re-apply อัตโนมัติ) ส่วน logic ที่ไม่ใช่ stylesheet (pen กราฟ, สี state-dependent) ให้ลงทะเบียนผ่าน `theme.on_retheme(fn)` หรือรวมไว้ใน `BatteryQtWindow._on_retheme()` — ธีมมืดตั้งผ่าน checkbox ใน Tools → APPEARANCE (`config.system.ui_theme`) เรียก `theme.retheme()` + `apply_stylesheet()` ใหม่ทันที ไม่ต้องรีสตาร์ท
- `theme.set_theme()` ยังต้องถูกเรียกก่อน import `isa101_views` เหมือนเดิม (ทำแล้วใน `run.py`) เพื่อให้ widget ชุดแรกสร้างด้วยพาเลตที่ถูกต้องตั้งแต่ต้น — ห้ามเพิ่ม eager import ของ isa101_views/zones/widgets ใน `aset_batt/ui/__init__.py`

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

เช็คโค้ดตาย (unused class/function ที่ไม่มีที่ไหนเรียกเลย — รวม `tests/`/`scripts/` ด้วย ไม่งั้น
false-positive กับอะไรที่ใช้แค่ใน test):

```bash
.venv/Scripts/python.exe -m vulture aset_batt/ tests/ scripts/ --min-confidence 80
```

ยืนยันด้วยมือก่อนลบเสมอ (`grep -rn "ชื่อนั้น" --include="*.py" .`) — vulture มี false-positive จริง
(Qt framework override เช่น `closeEvent`, dataclass field ที่เข้าถึงผ่าน `getattr`)

**เขียนเทสแบบรัน thread จริง ไม่ใช่แค่ unit function** — บั๊กใหญ่ๆ ที่เจอ (estimator ถูกนับซ้ำ,
กราฟไม่อัปเดตระหว่าง sequence, CHARACTERIZE แครชตั้งแต่ sample แรก) ล้วนอยู่ใน "การเดินสาย" ของ
thread/signal จริง — เทส pure-function ผ่านหมดแต่ตัวแอปยังพังอยู่ ดู
`tests/test_graph_feed_during_sequences.py` เป็นตัวอย่าง pattern: เรียก thread target ตรงๆ
(ไม่ผ่าน `threading.Thread(...).start()`) พร้อม mock `_seq_sleep`/`_char_sleep` ให้ออกจาก loop
เร็วๆ แล้วเช็คว่า buffer/CSV ได้ค่าจริง

**ไฟล์ใหม่ที่ mirror ไฟล์เดิม (เช่น `characterize.py` ตาม pattern ของ `sequences.py`) ต้อง grep
เทียบ safety/state-guard ที่มีอยู่แล้วก่อน commit** — อย่าพึ่งความจำหรือ "โค้ดดูคล้ายกันก็คงพอ":

```bash
grep -n "_ensure_logging\|stop_monitor\|_log_sample\|update_display\|_seq_check_temp_stale" aset_batt/ui/sequences.py aset_batt/ui/characterize.py
```

ถ้าไฟล์ใหม่ไม่มี pattern ที่ไฟล์ต้นแบบมี (เช่น "หยุด monitor loop ก่อน feed estimator เอง") ให้ถามว่า
ทำไมถึงต่างกัน ก่อนจะสมมติว่าโอเค — เคสจริงที่เจอ (ก.ค. 2026): characterize.py ก็อปโครงสร้างจาก
sequences.py มาแต่ไม่ได้เอา guard พวกนี้มาด้วย ทำให้ estimator ถูกนับซ้ำ + กราฟไม่อัปเดต + แครชเงียบ
มาหลายเดือนโดยไม่มีใครรู้ (ดู `tests/test_graph_feed_during_sequences.py`)

## เครื่องแล็บ — ต้องปิด USB selective suspend (ตั้งครั้งเดียว)

**อาการ**: sample rate ของ `AcquisitionWorker`/HPPC ตกเป็นช่วงๆ แบบสม่ำเสมอ (~50% ของ block ช้าลง
เหลือ 4-6Hz จากเป้า 10Hz) ทั้งที่ SCPI/estimator/log/cloud-push ทุกจุดวัดแล้วปกติหมด — เคยไล่จนสงสัย
GC, Windows timer resolution, GIL contention มาก่อน แต่ตัดออกหมดด้วย instrumentation ใน
`worker.py`'s per-25-sample breakdown log

**สาเหตุจริง (ยืนยันแล้ว ก.ค. 2026)**: Windows **USB selective suspend** (เปิดเป็นค่า default) พัก
USB ของ PSU/Load/ESP32-serial adapter เป็นระยะ แล้วต้อง "ปลุก" ทุกครั้งที่ใช้งาน กิน ~1.5-1.7s/ครั้ง —
เกิดที่ระดับ driver/OS จึงมองไม่เห็นจาก Python-level instrumentation ใดๆ เลย (ดู commit `e9aa3e0`)

**วิธีแก้ (ต้องทำที่เครื่องแล็บทุกเครื่องที่ใช้จริง — เป็น Windows power setting ไม่ใช่โค้ด แก้ในรีโปไม่ได้)**:
Control Panel → Power Options → เลือก plan ปัจจุบัน → Change plan settings → Change advanced power
settings → USB settings → USB selective suspend setting → ตั้งเป็น **Disabled** ทั้ง On battery และ
Plugged in (หรือ `powercfg` ทาง command line) — ทำครั้งเดียวต่อเครื่อง ถ้า reimage/เปลี่ยนเครื่อง/
Windows update reset ค่า power plan กลับมาเป็น default ต้องทำซ้ำ

`worker.py`'s per-substep breakdown instrumentation (SCPI/estimator/log/emit/safety/flush/ctrl/gc/
hppc_phase) ยังคงอยู่ในโค้ด (overhead ต่ำ) — เผื่อเครื่องที่ IT policy ไม่ให้ปิด USB selective suspend
ได้ จะได้มี fallback วินิจฉัยปัญหาเดิมซ้ำได้ทันทีโดยไม่ต้องไล่ใหม่ตั้งแต่ต้น

## Cloud dashboard

- push เข้า `main` → GitHub Actions build เฉพาะโฟลเดอร์ `cloud_dashboard/` แล้ว deploy ขึ้น Azure App Service อัตโนมัติ (~5 นาที) — ห้ามแก้ workflow ให้ build จาก root เพราะ requirements.txt ที่ root เป็นของ GUI
- เครื่องแล็บ push ข้อมูลขึ้นเว็บผ่าน `CloudPusher` (`aset_batt/storage/cloud_push.py`) — token อยู่ใน `cloud_token.txt` / env `INGEST_TOKEN` (gitignored, ต้องตรงกับที่ตั้งบน Azure)
- `aset_batt/services/cloud_push.py` (`CloudPushService`, ของเก่าที่เลิกใช้แล้ว ไม่มีที่ไหน import เลย) ถูกลบออกแล้ว (ก.ค. 2026 dead-code cleanup) — ถ้าจะทำ cloud push ให้ใช้ `CloudPusher` ด้านบนเท่านั้น อย่าสร้างคลาสใหม่ซ้ำ

import json
import logging
import threading
import urllib.request

logger = logging.getLogger(__name__)

# TODO: ใส่ URL ของ Web App ที่ได้จาก Google Apps Script ที่นี่
WEBAPP_URL = "https://script.google.com/macros/s/AKfycbzzkBxe6rCiQSq0SHLV5fykAfHgMxmsNh4reZ5LUr3KFbWCbXzIlvlxhVblmcTSJWznKA/exec"

def report_to_gsheet(battery_name: str, grade: str, soh: float, dcir: float):
    if not WEBAPP_URL or WEBAPP_URL == "YOUR_WEBAPP_URL_HERE":
        logger.warning("Google Sheet Webhook URL ยังไม่ได้ตั้งค่า (ข้ามการส่งข้อมูล)")
        return

    def _send():
        try:
            safe_soh = soh if soh == soh else "N/A"
            safe_dcir = dcir if dcir == dcir else "N/A"
            
            data = {
                "battery_name": battery_name,
                "grade": grade,
                "soh": safe_soh,
                "dcir": safe_dcir
            }
            
            req = urllib.request.Request(
                WEBAPP_URL, 
                data=json.dumps(data).encode('utf-8'), 
                headers={'Content-Type': 'application/json'}
            )
            
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read().decode())
                if result.get("status") == "success":
                    logger.info("Saved to Google Sheets successfully!")
                else:
                    logger.error(f"Google Sheets API Error: {result.get('message')}")
        except Exception as e:
            logger.error(f"Failed to send to Google Sheets: {e}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()

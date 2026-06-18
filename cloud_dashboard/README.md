# ASET Cloud Dashboard

บริการแสดงผลเทสต์แบตเตอรี่ออนไลน์ **24 ชม.** — เครื่องแล็บ push ข้อมูลขึ้นมา,
เพื่อนเปิดดูได้ตลอดแม้เครื่องแล็บปิด

```
[เครื่องแล็บ]  main.py → battery_data.csv → cloud_push.py ──POST /api/ingest──▶  [cloud_dashboard/server.py]  ◀── เพื่อนเปิดเว็บดู
```

- **stdlib ล้วน** (ไม่มี numpy/matplotlib) → slug เล็ก deploy เร็ว
- charts ด้วย Chart.js (CDN), เก็บ snapshot ล่าสุดใน memory (+ best-effort `snapshot.json`)
- `POST /api/ingest` ป้องกันด้วย header `X-Ingest-Token` = `INGEST_TOKEN`
- การ "ดู" เปิด public (เหมาะแชร์เพื่อน) — ข้อมูล read-only

## ENV
| ตัวแปร | ความหมาย |
|---|---|
| `PORT` | พอร์ต (Heroku ตั้งให้เอง; local default 8001) |
| `INGEST_TOKEN` | token สำหรับ ingest (ตั้งให้เหมือนกันทั้ง cloud และ cloud_push) |
| `SNAPSHOT_PATH` | ไฟล์ snapshot (default `snapshot.json`) |

## ทดสอบ local
```bash
INGEST_TOKEN=devtoken PORT=8011 python server.py        # terminal 1
# terminal 2 (ที่ root โปรเจกต์):
python cloud_push.py --url http://127.0.0.1:8011 --token devtoken
# เปิด http://127.0.0.1:8011
```

## Deploy: Heroku
```bash
cd cloud_dashboard
git init && git add -A && git commit -m "cloud dashboard"
heroku create aset-batt           # ได้ URL https://aset-batt.herokuapp.com
heroku config:set INGEST_TOKEN=$(openssl rand -hex 16)
git push heroku main
heroku config:get INGEST_TOKEN    # เก็บไว้ใส่ใน cloud_push
```
แล้วฝั่งแล็บ:
```bash
set CLOUD_DASHBOARD_URL=https://aset-batt.herokuapp.com
set INGEST_TOKEN=<ค่าที่ได้>
python cloud_push.py --interval 30     # รันค้างระหว่างเทสต์
```

## Deploy: DigitalOcean
- **App Platform:** ชี้ไปที่โฟลเดอร์ `cloud_dashboard/`, run command `python server.py`,
  ตั้ง env `INGEST_TOKEN`. (เก็บ history ถาวรได้ถ้าต่อ Managed Postgres — เป็น follow-up)
- **Droplet (VM):** `scp` โฟลเดอร์ขึ้น VM แล้วรันด้วย `systemd` / `screen`:
  `INGEST_TOKEN=xxxx PORT=80 python3 server.py`

## โดเมนสวย ๆ (Namecheap .me ฟรีจาก Student Pack)
ชี้ CNAME ของโดเมนไปที่ Heroku/DO เพื่อได้ URL แบบ `aset-batt.me`

## หมายเหตุ
- snapshot เก็บแบบ in-memory → Heroku dyno restart ข้อมูลหาย แต่เครื่องแล็บ push รอบใหม่ก็กลับมา
- ถ้าต้องการ **ประวัติย้อนหลังถาวร** ให้ต่อฐานข้อมูล (DO + Postgres) — เก็บไว้เป็นเฟสถัดไป

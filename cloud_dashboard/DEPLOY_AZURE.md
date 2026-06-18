# Deploy ASET Cloud Dashboard บน Microsoft Azure (ละเอียด)

เป้าหมาย: เอา `cloud_dashboard/` ขึ้น **Azure App Service** ให้ได้ URL `https://<ชื่อ>.azurewebsites.net`
ที่รัน 24 ชม. แล้วเครื่องแล็บใช้ [`cloud_push.py`](../cloud_push.py) ส่งข้อมูลขึ้นไป

> โค้ดเป็น **stdlib ล้วน + ฟังพอร์ตจาก env `PORT`** ซึ่งตรงกับที่ Azure App Service (Linux/Python) ต้องการพอดี — ไม่ต้องแก้โค้ด

---

## 0) เปิดใช้ Azure for Students (ครั้งเดียว)
1. ไป **https://azure.microsoft.com/free/students** → Start free
2. ยืนยันด้วยอีเมลสถาบัน `artit.r@ubu.ac.th` → ได้ **เครดิต $100, ไม่ต้องใช้บัตรเครดิต**
3. ได้ subscription ชื่อ "Azure for Students"

## 1) ติดตั้ง Azure CLI (ครั้งเดียว)
```powershell
winget install --id Microsoft.AzureCLI
# ปิด-เปิด terminal ใหม่ แล้ว:
az login        # เปิดเบราว์เซอร์ให้ล็อกอินบัญชี Azure
```
> ไม่อยากติดตั้ง? ใช้ **Azure Cloud Shell** (ปุ่ม `>_` บน portal.azure.com, เป็น bash ในเบราว์เซอร์)
> แต่ต้องมีไฟล์อยู่ที่นั่น → ง่ายสุดคือ push repo ขึ้น GitHub ก่อนแล้ว `git clone` ใน Cloud Shell

---

## 2) Deploy ด้วย Azure CLI (วิธีหลัก — PowerShell)

รันจากโฟลเดอร์ `cloud_dashboard/`:
```powershell
cd C:\Users\boatl\OneDrive\ASET_BATT\cloud_dashboard

$RG    = "aset-rg"
$APP   = "aset-batt-dashboard"          # ต้องไม่ซ้ำใครทั้งโลก -> URL https://aset-batt-dashboard.azurewebsites.net
$LOC   = "southeastasia"                 # สิงคโปร์ ใกล้ไทยสุด
$TOKEN = python -c "import secrets;print(secrets.token_hex(16))"

az group create -n $RG -l $LOC

# สร้าง + deploy โค้ดในโฟลเดอร์นี้ (Linux, Python 3.11). B1 = always-on (~$13/เดือนจากเครดิต)
az webapp up --name $APP --resource-group $RG --location $LOC --runtime "PYTHON:3.11" --sku B1

# สำคัญ! ตั้ง startup command (server เราเป็น http.server ไม่ใช่ gunicorn/WSGI)
az webapp config set -g $RG -n $APP --startup-file "python server.py"

# ตั้ง token (กลายเป็น env var INGEST_TOKEN ใน app)
az webapp config appsettings set -g $RG -n $APP --settings INGEST_TOKEN=$TOKEN

az webapp restart -g $RG -n $APP
Write-Host "URL  : https://$APP.azurewebsites.net"
Write-Host "TOKEN: $TOKEN"     # << เก็บไว้ใส่ใน cloud_push
```

> **ทำไมต้องตั้ง `--startup-file`?** ค่า default ของ Azure Python จะรัน `gunicorn` หา WSGI app
> ซึ่งเราไม่มี → app จะ error จนกว่าจะสั่งให้รัน `python server.py` (มันฟัง `$PORT` ที่ Azure ให้มาเอง)

> **เครดิตประหยัด:** อยากฟรีล้วนใช้ `--sku F1` (Free tier) แทน B1 — แต่ F1 จะ "หลับ" เมื่อไม่มีคนเข้า
> และมีโควตา CPU/วัน เหมาะลองเล่น; ถ้าอยากให้ออนไลน์จริงตลอดใช้ B1

---

## 3) Deploy ผ่าน Portal (GUI — ถ้าไม่อยากใช้ CLI)
1. **portal.azure.com** → *Create a resource* → **Web App**
2. **Basics:**
   - Subscription: *Azure for Students*
   - Resource Group: *Create new* → `aset-rg`
   - Name: `aset-batt-dashboard`
   - Publish: **Code** | Runtime stack: **Python 3.11** | OS: **Linux**
   - Region: **Southeast Asia** | Plan: **B1** (หรือ F1 Free)
   - → **Review + create** → **Create**
3. เข้าหน้า App → **Settings ▸ Configuration**
   - แท็บ **General settings** → **Startup Command** = `python server.py` → **Save**
   - แท็บ **Application settings** → **+ New** → Name `INGEST_TOKEN`, Value `<สุ่มมา>` → **Save** (app จะ restart)
4. **Deployment Center** → เลือกแหล่งโค้ด:
   - **GitHub** (แนะนำ ถ้า push repo แล้ว ตั้ง path เป็น `cloud_dashboard`) → จะตั้ง GitHub Actions อัตโนมัติ
   - หรือ **Local Git / Zip Deploy**: zip เฉพาะไฟล์ในโฟลเดอร์ `cloud_dashboard/` แล้วอัปโหลด
5. URL: `https://<ชื่อ>.azurewebsites.net`

---

## 4) (ทางเลือก) Container ผ่าน Azure Container Registry — ไม่ต้องมี docker ในเครื่อง
มี [`Dockerfile`](Dockerfile) ให้แล้ว ใช้ ACR build บนคลาวด์:
```powershell
az acr create -g aset-rg -n asetbattacr --sku Basic
az acr build -r asetbattacr -t aset-dash:latest .          # build บนคลาวด์
az webapp create -g aset-rg -p <plan> -n aset-batt-dashboard `
  --deployment-container-image-name asetbattacr.azurecr.io/aset-dash:latest
az webapp config appsettings set -g aset-rg -n aset-batt-dashboard `
  --settings INGEST_TOKEN=<token> WEBSITES_PORT=8000
```

---

## 5) ตรวจสอบ + เชื่อมเครื่องแล็บ
```powershell
# 1) เช็คว่า service ขึ้นแล้ว
curl https://aset-batt-dashboard.azurewebsites.net/api/health

# 2) ฝั่งเครื่องแล็บ (root โปรเจกต์) — ส่งข้อมูลขึ้นทุก 30 วิ ระหว่างเทสต์
$env:CLOUD_DASHBOARD_URL = "https://aset-batt-dashboard.azurewebsites.net"
$env:INGEST_TOKEN        = "<token เดียวกับที่ตั้งบน Azure>"
python cloud_push.py --interval 30
```
แล้วเปิด `https://aset-batt-dashboard.azurewebsites.net` ในเบราว์เซอร์ → เห็น dashboard + กราฟ

---

## 6) โดเมนสวย ๆ (.me ฟรีจาก Namecheap Student Pack)
App Service → **Custom domains** → **Add** → ทำตามให้ตั้ง **CNAME** ที่ Namecheap ชี้มาที่
`<ชื่อ>.azurewebsites.net` → ได้ URL แบบ `aset-batt.me` (Azure แถม managed TLS ฟรี)

---

## 7) แก้ปัญหาที่พบบ่อย
| อาการ | สาเหตุ/วิธีแก้ |
|---|---|
| เปิดเว็บแล้ว 502 / "Application Error" | ยังไม่ได้ตั้ง startup `python server.py` → ตั้งแล้ว `az webapp restart` |
| ดู log สด | `az webapp log tail -g aset-rg -n aset-batt-dashboard` |
| push แล้ว 401 | `INGEST_TOKEN` บน Azure กับใน `cloud_push` ไม่ตรงกัน |
| push แล้ว timeout | URL ผิด หรือ app กำลัง cold-start (F1) — ลองซ้ำ |
| ข้อมูลหายหลังผ่านไปนาน | snapshot เป็น in-memory; B1 รีสตาร์ทบ้าง → push รอบใหม่ก็กลับมา (ประวัติถาวรต้องต่อ DB) |

## หมายเหตุค่าใช้จ่าย
- **B1** ~$13/เดือน → เครดิต $100 อยู่ได้ ~7 เดือน | **F1** ฟรีตลอด (มีลิมิต)
- HTTPS ของ `*.azurewebsites.net` ฟรีอัตโนมัติ

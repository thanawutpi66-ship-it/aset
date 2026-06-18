# ASET Cloud Dashboard — stdlib only, image เล็กมาก
# ใช้สำหรับ path container (Azure Container Apps / Web App for Containers)
# build บนคลาวด์ได้โดยไม่ต้องมี docker ในเครื่อง: `az acr build`
FROM python:3.11-slim
WORKDIR /app
COPY server.py .

# Azure (container) ส่ง traffic ไปยังพอร์ตใน WEBSITES_PORT — ให้ตรงกับ PORT ที่ app ฟัง
ENV PORT=8000
EXPOSE 8000

# INGEST_TOKEN ตั้งตอน runtime ผ่าน Azure app settings (ห้าม bake ลง image)
CMD ["python", "server.py"]

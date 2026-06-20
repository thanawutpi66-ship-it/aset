# Starts:
# 1) the ASET app (main.py) in background
# 2) cloudflared Quick Tunnel (http://localhost:8000)
# 3) prints the first trycloudflare URL it sees so you can open it on your phone
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\start_cloudflared_quick_tunnel.ps1
#
# Notes:
# - URL changes every run; open the printed URL on your phone each time.
# - Requires cloudflared installed and available in PATH.

$ErrorActionPreference = "Stop"

Write-Host "Starting ASET app (main.py) in background..."
$python = "python"
$procApp = Start-Process -FilePath $python -ArgumentList "main.py" -NoNewWindow -PassThru

Start-Sleep -Seconds 4

Write-Host "cloudflared Quick Tunnel is deprecated for this project — local dashboard removed. See README for cloud dashboard usage."
# cloudflared output is streamed; we parse the first URL line.
$procCloud = Start-Process -FilePath "cloudflared" -ArgumentList "http", "8000" -NoNewWindow -PassThru -RedirectStandardOutput "cloudflared_quick_tunnel.out" -RedirectStandardError "cloudflared_quick_tunnel.err"

# Poll output file until we find a trycloudflare URL
$regex = 'https://[a-z0-9\-]+\.trycloudflare\.com'
$found = $false

for ($i=0; $i -lt 60; $i++) { # up to ~60 seconds
    Start-Sleep -Seconds 1
    if (Test-Path "cloudflared_quick_tunnel.out") {
        $text = Get-Content "cloudflared_quick_tunnel.out" -Raw -ErrorAction SilentlyContinue
        if ($text -match $regex) {
            $url = $Matches[0]
            Write-Host ""
            Write-Host "====================================="
            Write-Host "📱 Open this URL on your phone: $url"
            Write-Host "====================================="

            # Send the URL to Gmail automatically (optional)
            # Set these env vars before running (DO NOT hardcode real secrets here):
            #   $env:SMTP_USER="your_account@gmail.com"
            #   $env:SMTP_PASS="YOUR_GMAIL_APP_PASSWORD"
            try {
                if ($env:SMTP_USER -and $env:SMTP_PASS) {
                    $scriptEmail = ".\send_tunnel_url_email.ps1"
                    if (Test-Path $scriptEmail) {
                        powershell -ExecutionPolicy Bypass -File $scriptEmail -Url $url
                    } else {
                        Write-Host "Email script not found: $scriptEmail" -ForegroundColor Yellow
                    }
                } else {
                    Write-Host "SMTP_USER/SMTP_PASS not set -> skipping email." -ForegroundColor Yellow
                }
            } catch {
                Write-Host "Failed to send email: $($_.Exception.Message)" -ForegroundColor Red
            }

            $found = $true
            break
        }
    }
}

if (-not $found) {
    Write-Host ""
    Write-Host "❌ Could not find a trycloudflare URL within timeout."
    Write-Host "Check cloudflared logs in:"
    Write-Host " - cloudflared_quick_tunnel.out"
    Write-Host " - cloudflared_quick_tunnel.err"
    Write-Host "You can also open those files and search for 'trycloudflare.com'."
}

Write-Host ""
Write-Host "Done. Leave this PowerShell window open so cloudflared keeps running."

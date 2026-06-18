# Sends a tunnel URL to a Gmail recipient via SMTP (App Password)
# Usage:
#   ./send_tunnel_url_email.ps1 -Url "https://xxxx.trycloudflare.com"
#
# Requirements:
# - Gmail App Password enabled for the account
# - SMTP host: smtp.gmail.com
# - Port: 587
#
# IMPORTANT:
# - Do NOT hardcode credentials in public repos.
# - This script reads secrets from environment variables:
#   SMTP_USER, SMTP_PASS
#
# Example (PowerShell):
#   $env:SMTP_USER="boatlnwza2547@gmail.com"
#   $env:SMTP_PASS="xxxx_app_password_xxxx"
#   ./send_tunnel_url_email.ps1 -Url "https://xxxx.trycloudflare.com"

param(
  [Parameter(Mandatory=$true)]
  [string]$Url,

  [string]$To = "boatlnwza2547@gmail.com",
  [string]$From = "boatlnwza2547@gmail.com"
)

$ErrorActionPreference = "Stop"

$smtpUser = $env:SMTP_USER
$smtpPass = $env:SMTP_PASS

if ([string]::IsNullOrWhiteSpace($smtpUser) -or [string]::IsNullOrWhiteSpace($smtpPass)) {
  Write-Host "Missing env vars SMTP_USER / SMTP_PASS" -ForegroundColor Yellow
  Write-Host "Set them before running, e.g.:" -ForegroundColor Yellow
  Write-Host '$env:SMTP_USER="boatlnwza2547@gmail.com"' -ForegroundColor Yellow
  Write-Host '$env:SMTP_PASS="YOUR_GMAIL_APP_PASSWORD"' -ForegroundColor Yellow
  exit 1
}

$smtpHost = "smtp.gmail.com"
$smtpPort = 587

$subject = "ASET Lab Tunnel URL (Live Results)"
$body = @"
Open this URL on your phone to view live test results:

$Url

Time: $(Get-Date)
"@

try {
  $securePass = ConvertTo-SecureString -String $smtpPass -AsPlainText -Force
  $credential = New-Object System.Management.Automation.PSCredential($smtpUser, $securePass)

  $mailMessage = New-Object System.Net.Mail.MailMessage
  $mailMessage.From = $From
  $mailMessage.To.Add($To) | Out-Null
  $mailMessage.Subject = $subject
  $mailMessage.Body = $body

  $smtpClient = New-Object System.Net.Mail.SmtpClient($smtpHost, $smtpPort)
  $smtpClient.EnableSsl = $true
  $smtpClient.Credentials = $credential.GetNetworkCredential()

  $smtpClient.Send($mailMessage)
  Write-Host "Email sent successfully to $To" -ForegroundColor Green
} catch {
  Write-Host "Failed to send email: $($_.Exception.Message)" -ForegroundColor Red
  throw
}

<#
    Start the app for the whole house and print the addresses to hand out.

    A raw "192.168.0.151:8000" is not something anyone wants to retype on a
    phone. This serves on port 80 by default so the port disappears from the
    URL, and prints the names this machine already answers to.

    Usage:
        .\serve.ps1                 # http://<hostname>/
        .\serve.ps1 -Port 8000      # keep the old port
        .\serve.ps1 -Reload         # auto-restart while developing
#>
param(
    [int]$Port = 80,
    [string]$BindAddress = "0.0.0.0",
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# If something already holds the port, say so rather than dying on a stack trace.
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Write-Host "Port $Port is already in use by $owner." -ForegroundColor Red
    Write-Host "Stop it, or run:  .\serve.ps1 -Port 8000"
    exit 1
}

$hostName = [System.Net.Dns]::GetHostName()
$suffix = if ($Port -eq 80) { "" } else { ":$Port" }

$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" -or
                   $_.IPAddress -like "172.1*" -or $_.IPAddress -like "172.2*" -or
                   $_.IPAddress -like "172.3*" } |
    Select-Object -ExpandProperty IPAddress

Write-Host ""
Write-Host "  Give these to anyone on the house wifi:" -ForegroundColor Cyan
Write-Host ""
Write-Host "    http://$hostName$suffix/          " -NoNewline -ForegroundColor Green
Write-Host "(Windows, Android)"
Write-Host "    http://$hostName.local$suffix/    " -NoNewline -ForegroundColor Green
Write-Host "(iPhone, iPad, Mac)"
foreach ($ip in $ips) {
    Write-Host "    http://$ip$suffix/           " -NoNewline -ForegroundColor DarkGray
    Write-Host "(always works)"
}
Write-Host ""
Write-Host "  For a shorter name like http://radio/ run tools\setup_lan_name.ps1" -ForegroundColor DarkGray
Write-Host ""

$uvicornArgs = @("-m", "uvicorn", "app:app", "--host", $BindAddress, "--port", "$Port")
if ($Reload) { $uvicornArgs += "--reload" }

& $python @uvicornArgs

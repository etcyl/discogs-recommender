<#
    Give this machine a short, memorable name on the house network.

    By default the app is reachable at the machine's own hostname, which is
    whatever the PC was called when it was set up. This registers an extra
    name — "radio" by default — so the address becomes http://radio/ instead.

    Run this in an ELEVATED PowerShell (right-click > Run as administrator).

    Usage:
        .\tools\setup_lan_name.ps1
        .\tools\setup_lan_name.ps1 -Name music -Port 8000

    What it does:
      1. Opens the port in Windows Firewall for private networks only.
      2. Registers the alias as an extra NetBIOS name for this machine.
      3. Prints the addresses to hand out.

    Step 2 works for Windows and Android devices. iPhones and Macs resolve
    "<hostname>.local" instead, which already works with no setup. If a device
    resolves neither, the numeric address always works; see the note at the end
    for the per-device hosts-file fallback.
#>
param(
    [ValidatePattern('^[a-zA-Z][a-zA-Z0-9-]{1,14}$')]
    [string]$Name = "radio",
    [int]$Port = 80,
    [switch]$Undo
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "This needs administrator rights." -ForegroundColor Red
    Write-Host "Right-click PowerShell > Run as administrator, then run it again."
    exit 1
}

$ruleName = "Discogs Recommender (port $Port)"
$regPath  = "HKLM:\SYSTEM\CurrentControlSet\Services\LanmanServer\Parameters"

if ($Undo) {
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule
    $existing = @((Get-ItemProperty -Path $regPath -Name OptionalNames `
        -ErrorAction SilentlyContinue).OptionalNames)
    $kept = $existing | Where-Object { $_ -and $_ -ne $Name }
    if ($kept) {
        Set-ItemProperty -Path $regPath -Name OptionalNames -Value $kept
    } else {
        Remove-ItemProperty -Path $regPath -Name OptionalNames -ErrorAction SilentlyContinue
    }
    Restart-Service -Name LanmanServer -Force
    Write-Host "Removed the '$Name' alias and the firewall rule." -ForegroundColor Yellow
    exit 0
}

# 1. Firewall. Private profile only — this should never be reachable from the
#    internet, and a public-profile rule on a laptop would follow it to a cafe.
if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Write-Host "Firewall rule already present." -ForegroundColor DarkGray
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
    Write-Host "Opened port $Port for private networks." -ForegroundColor Green
}

# 2. The alias itself.
$existing = @((Get-ItemProperty -Path $regPath -Name OptionalNames `
    -ErrorAction SilentlyContinue).OptionalNames) | Where-Object { $_ }

if ($existing -contains $Name) {
    Write-Host "Alias '$Name' already registered." -ForegroundColor DarkGray
} else {
    Set-ItemProperty -Path $regPath -Name OptionalNames `
        -Value ([string[]]($existing + $Name)) -Type MultiString
    Restart-Service -Name LanmanServer -Force
    Write-Host "Registered '$Name' as an extra name for this machine." -ForegroundColor Green
}

# 3. What to actually type.
$hostName = [System.Net.Dns]::GetHostName()
$suffix = if ($Port -eq 80) { "" } else { ":$Port" }
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" } |
    Select-Object -First 1 -ExpandProperty IPAddress)

Write-Host ""
Write-Host "  Addresses for this machine:" -ForegroundColor Cyan
Write-Host "    http://$Name$suffix/"
Write-Host "    http://$hostName.local$suffix/   (iPhone, iPad, Mac)"
Write-Host "    http://$ip$suffix/               (always works)"
Write-Host ""
Write-Host "  If a device resolves none of the names, add one line to its" -ForegroundColor DarkGray
Write-Host "  hosts file (C:\Windows\System32\drivers\etc\hosts on Windows):" -ForegroundColor DarkGray
Write-Host "    $ip  $Name" -ForegroundColor DarkGray
Write-Host ""

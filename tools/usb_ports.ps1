# Lists each COM port with its driver version and the USB hubs it sits behind. Read only.
# Made 2026-11-20 to compare the mini PC (where box + panel work) with the main PC (where the panel hangs).
# Over Remote Desktop with local drives shared, run it from the main PC's disk; the result is saved next to this
# project (install-snapshots\ports-<computer>.txt) so it can be read on the main PC:
#   powershell -ep bypass -f "\\tsclient\C\<path to>\alpaca-hub\tools\usb_ports.ps1"
$lines = @(((Get-CimInstance Win32_OperatingSystem).Caption + ' ' + [Environment]::OSVersion.Version), "Computer: $env:COMPUTERNAME")
$ports = @(Get-PnpDevice -PresentOnly -Class Ports -ErrorAction SilentlyContinue)
if (-not $ports) { $lines += "No COM ports present: plug the devices in and run this again." }
foreach ($d in $ports) {
    if ($d.FriendlyName -notmatch 'COM\d') { continue }
    $drv = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName DEVPKEY_Device_DriverVersion).Data
    $chain = @(); $p = $d.InstanceId
    while ($p -and $p -notmatch 'ROOT_HUB') {
        $chain += ($p -split '\\')[1]
        $p = (Get-PnpDeviceProperty -InstanceId $p -KeyName DEVPKEY_Device_Parent).Data
    }
    $lines += "$($d.FriendlyName) | driver $drv | $($chain -join ' <- ') <- $p"
}
$lines
$out = Join-Path $PSScriptRoot "..\install-snapshots\ports-$env:COMPUTERNAME.txt"
try { New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null; $lines | Set-Content -Encoding utf8 $out; "Saved: $out" }
catch { "Could not save the result: $_" }
Read-Host "Press Enter to close"

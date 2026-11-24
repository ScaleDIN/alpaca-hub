# Open serial ports the way Wanderer Empire does (.NET SerialPort, 19200, DTR and RTS on), one after another,
# keep them all open, and time each open. Never writes a byte.
#
# Made 2026-11-20: tells apart "pyserial's way of opening" from "the WCH driver / USB hub" as the cause of the second
# CH340 open hanging. Run it in Windows PowerShell 5.1, which uses .NET Framework like Empire:
#   powershell -ExecutionPolicy Bypass -File tools\open_test_dotnet.ps1 COM8 COM11
#   powershell -ExecutionPolicy Bypass -File tools\open_test_dotnet.ps1 -LinesOff COM8 COM11   # DTR/RTS off, like the hub
#
# Reading is passive (only what the devices send by themselves), so it is safe for the box. If an open hangs, this
# window stays at "opening COMx ..."; the process may then hold its ports until the device is replugged.
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true, Position = 0, ValueFromRemainingArguments = $true)][string[]]$Ports,
    [int]$Baud = 19200,
    [int]$Hold = 15,
    [switch]$LinesOff
)

function Say($msg) { Write-Host ("{0}  {1}" -f (Get-Date -Format HH:mm:ss), $msg) }

$on = -not $LinesOff
Say (".NET {0}, DTR/RTS {1}, {2} baud" -f [Environment]::Version, $(if ($on) { 'ON' } else { 'OFF' }), $Baud)

$held = @()
foreach ($name in $Ports) {
    $others = if ($held.Count) { ($held | ForEach-Object { $_.PortName }) -join ', ' } else { 'nothing' }
    Say "opening $name (holding $others) ..."
    $p = New-Object System.IO.Ports.SerialPort $name, $Baud
    $p.DtrEnable = $on           # same order as Empire: BaudRate, DtrEnable, RtsEnable, then Open
    $p.RtsEnable = $on
    $p.ReadTimeout = 2000
    $p.WriteTimeout = 2000
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $p.Open()
        Say ("  {0} open in {1:N2} s  (DTR={2} RTS={3})" -f $name, $sw.Elapsed.TotalSeconds, $p.DtrEnable, $p.RtsEnable)
        $held += $p
    } catch {
        Say "  $name FAILED: $($_.Exception.Message)"
    }
}

if ($held.Count -and $Hold -gt 0) {
    Say "holding $($held.Count) port(s) $Hold s, counting bytes the devices send by themselves"
    $counts = @{}; $first = @{}
    foreach ($p in $held) { $counts[$p.PortName] = 0 }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Hold) {
        foreach ($p in $held) {
            $n = $p.BytesToRead
            if ($n -gt 0) {
                [void]$p.ReadExisting()
                $counts[$p.PortName] += $n
                if (-not $first.ContainsKey($p.PortName)) { $first[$p.PortName] = $sw.Elapsed.TotalSeconds }
            }
        }
        Start-Sleep -Milliseconds 50
    }
    foreach ($p in $held) {
        $start = if ($first.ContainsKey($p.PortName)) { ", first after {0:N1} s" -f $first[$p.PortName] } else { "" }
        Say ("  {0}: {1} bytes{2}" -f $p.PortName, $counts[$p.PortName], $start)
    }
}

foreach ($p in $held) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p.Close()
    Say ("closed {0} in {1:N2} s" -f $p.PortName, $sw.Elapsed.TotalSeconds)
}
Say "done"

# Restart the reports service and the WhatsApp inbox through their scheduled tasks.
# Run by restart.cmd, as administrator.
#
# Stopping only the server process is not enough: the task's launcher stays
# alive, the task still counts as "Running", and a task set to ignore a new
# start while running then silently does nothing - the old code keeps serving.
# So: stop the task, kill whatever is left of it, start it, and check that the
# process now answering is a new one.

$services = @(
    @{ Port = 8787; Task = "Iravi Reports service"; Name = "Reports service"; Script = "*reports_web.py*" },
    @{ Port = 8790; Task = "Iravi WhatsApp inbox";  Name = "WhatsApp inbox";  Script = "*serve.py*" }
)

function Get-ServerPid($port) {
    (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
}

$before = @{}
foreach ($s in $services) {
    $before[$s.Port] = Get-ServerPid $s.Port
    Stop-ScheduledTask -TaskName $s.Task -ErrorAction SilentlyContinue
    # The launcher and the real Python, and anything still holding the port.
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like $s.Script } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $left = Get-ServerPid $s.Port
    if ($left) { Stop-Process -Id $left -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Seconds 2

foreach ($s in $services) {
    if (Get-ServerPid $s.Port) {
        Write-Host "$($s.Name): the old copy would not stop (port $($s.Port) still in use)" -ForegroundColor Red
        continue
    }
    Start-ScheduledTask -TaskName $s.Task
}

foreach ($s in $services) {
    $ok = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 1
        $now = Get-ServerPid $s.Port
        if ($now -and $now -ne $before[$s.Port]) {
            try { Invoke-RestMethod "http://127.0.0.1:$($s.Port)/api/health" -TimeoutSec 3 | Out-Null; $ok = $true; break } catch { }
        }
    }
    if ($ok) {
        $started = (Get-Process -Id (Get-ServerPid $s.Port)).StartTime.ToString("HH:mm:ss")
        Write-Host "$($s.Name): restarted at $started" -ForegroundColor Green
    } else {
        Write-Host "$($s.Name): DID NOT RESTART - see its log" -ForegroundColor Red
    }
}

Write-Host ""
Read-Host "Press Enter to close"

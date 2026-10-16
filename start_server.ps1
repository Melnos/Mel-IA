param(
    [string]$Port = "8137",
    [string]$Log = "./logs.txt"
)
$dir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$logPath = [System.IO.Path]::Combine($dir, $Log)
$cmd = "python -m uvicorn app:app --host 127.0.0.1 --port $Port --log-level info > `"$logPath`" 2>&1"
Write-Output "Starting uvicorn in $dir with log $logPath"
Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile -Command `"cd '$dir'; $cmd`""
Start-Sleep -Seconds 4
Get-Content $logPath -Tail 20

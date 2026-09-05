param(
    [Parameter(Mandatory=$true)][string]$Iso,
    [Parameter(Mandatory=$true)][string]$StateFile
)

$ErrorActionPreference = 'Stop'
$snap = Join-Path $env:USERPROFILE 'Documents\PCSX2\snaps'
$exe = 'D:\game\pcsx2-v2.6.3-windows-x64-Qt\pcsx2-qt.exe'
$before = @(Get-ChildItem $snap -Filter '*.png').Count
$isoPath = (Resolve-Path $Iso).Path
$statePath = (Resolve-Path $StateFile).Path

$p = Start-Process -FilePath $exe -ArgumentList @('-batch', '-nofullscreen', '-statefile', ('"' + $statePath + '"'), '--', ('"' + $isoPath + '"')) -PassThru
try {
    Start-Sleep -Seconds 10
    $ws = New-Object -ComObject WScript.Shell
    $null = $ws.AppActivate($p.Id)
    Start-Sleep -Milliseconds 700
    $ws.SendKeys('{F8}')
    Start-Sleep -Seconds 3
}
finally {
    if (-not $p.HasExited) {
        $null = $p.CloseMainWindow()
        Start-Sleep -Seconds 2
        if (-not $p.HasExited) {
            Stop-Process -Id $p.Id -Force
        }
    }
}

$after = @(Get-ChildItem $snap -Filter '*.png' | Sort-Object LastWriteTime -Descending)
Write-Output ('before=' + $before)
Write-Output ('after=' + $after.Count)
$after | Select-Object -First 5 FullName, LastWriteTime, Length | Format-List

# Chrome no longer accepts chrome:// URLs from the command line, so we open a
# plain window and then type the address into the omnibox as keystrokes.
Add-Type -Namespace Win32 -Name Window -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
'@

Start-Sleep -Seconds 2

$proc = Get-Process chrome -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Sort-Object StartTime -Descending |
    Select-Object -First 1

if (-not $proc) {
    Write-Host "ERROR: No Chrome window found."
    exit 1
}

$null = [Win32.Window]::ShowWindow($proc.MainWindowHandle, 9)  # SW_RESTORE
$null = [Win32.Window]::SetForegroundWindow($proc.MainWindowHandle)
Start-Sleep -Milliseconds 600

$shell = New-Object -ComObject WScript.Shell
$shell.SendKeys('^l')
Start-Sleep -Milliseconds 300
$shell.SendKeys('chrome://extensions/')
Start-Sleep -Milliseconds 300
$shell.SendKeys('{ENTER}')

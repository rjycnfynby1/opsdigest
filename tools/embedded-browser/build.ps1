param([string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$sdk = Join-Path $env:LOCALAPPDATA 'OpsDigest\webview2-sdk\1.0.4314.35'
$out = Join-Path $env:LOCALAPPDATA 'OpsDigest\embedded-prototype'
if ($OutputDirectory) { $out = $OutputDirectory }
if (!(Test-Path "$sdk\lib\net462\Microsoft.Web.WebView2.Core.dll")) {
    throw 'Нужен Microsoft.Web.WebView2 SDK 1.0.4314.35 из NuGet. См. docs/EMBEDDED_BROWSER.md.'
}
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item "$sdk\lib\net462\Microsoft.Web.WebView2.Core.dll" $out
Copy-Item "$sdk\lib\net462\Microsoft.Web.WebView2.WinForms.dll" $out
Copy-Item "$sdk\runtimes\win-x64\native\WebView2Loader.dll" $out
Copy-Item "$PSScriptRoot\picker.js" $out
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:winexe /platform:x64 "/out:$out\OpsDigest.Embedded.exe" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll "/reference:$out\Microsoft.Web.WebView2.Core.dll" "/reference:$out\Microsoft.Web.WebView2.WinForms.dll" "$PSScriptRoot\Prototype.cs" "$PSScriptRoot\Scenario.cs"
if ($LASTEXITCODE -ne 0) { throw 'Ошибка компиляции оболочки' }
Write-Output "$out\OpsDigest.Embedded.exe"

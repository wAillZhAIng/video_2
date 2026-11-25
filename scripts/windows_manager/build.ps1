$ErrorActionPreference = 'Stop'
$project = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'The .NET Framework C# compiler is required.'
}
$output = Join-Path $project ('Windows' + [char]0x7BA1 + [char]0x7406 + '.exe')
$manifest = Join-Path $PSScriptRoot 'manager.manifest'
$source = Join-Path $PSScriptRoot 'Program.cs'
$references = '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll'
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ /codepage:65001 @references "/win32manifest:$manifest" "/out:$output" $source
if ($LASTEXITCODE -ne 0) {
    throw "Manager compilation failed with exit code $LASTEXITCODE."
}
Get-Item -LiteralPath $output

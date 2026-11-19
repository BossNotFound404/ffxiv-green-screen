#requires -Version 5.1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$export = Join-Path (Split-Path $PSScriptRoot -Parent) 'export-alpha.ps1'
$base = [IO.Path]::GetTempPath()
$root = Join-Path $base ('greenscreen-test-' + [guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)

function Check($Pass, $Label) {
    if (-not $Pass) { throw "FAILED: $Label" }
}
function Byte-Value($x) { [int][Math]::Round(255 * $x) }
function From-Srgb($x) {
    $v = $x / 255.0
    if ($v -le .04045) { return $v / 12.92 }
    [Math]::Pow(($v + .055) / 1.055, 2.4)
}
function To-Srgb($v) {
    if ($v -le .0031308) { return Byte-Value (12.92 * $v) }
    Byte-Value (1.055 * [Math]::Pow($v, 1/2.4) - .055)
}
function Composite($fg, $bg, $alpha, $linear) {
    $opacity = $alpha / 255.0
    if ($linear) { return To-Srgb ((From-Srgb $fg) * $opacity + (From-Srgb $bg) * (1 - $opacity)) }
    [int][Math]::Round($fg * $opacity + $bg * (1 - $opacity))
}
function Save-Row($path, $colors) {
    $img = [Drawing.Bitmap]::new($colors.Count, 1)
    try {
        for ($x = 0; $x -lt $colors.Count; $x++) { $img.SetPixel($x, 0, $colors[$x]) }
        $img.Save($path, [Drawing.Imaging.ImageFormat]::Png)
    } finally { $img.Dispose() }
}
try {
    $black = Join-Path $root 'black.png'; $white = Join-Path $root 'white.png'
    $alphas = @(0, 64, 128, 192, 255)
    foreach ($space in 'Encoded','LinearSRGB') {
        $linear = $space -eq 'LinearSRGB'
        foreach ($bg in 0, 255) {
            $colors = foreach ($a in $alphas) {
                [Drawing.Color]::FromArgb(255, (Composite 200 $bg $a $linear), (Composite 110 $bg $a $linear), (Composite 60 $bg $a $linear))
            }
            Save-Row $(if ($bg -eq 0) { $black } else { $white }) $colors
        }
        $dest = Join-Path $root $space
        $stats = & $export $black $white $dest -BlendSpace $space
        Check ($stats.MismatchedPixels -eq 0 -and $stats.Frames -eq 1) "$space matte fits inputs"
        $result = [Drawing.Bitmap]::new((Join-Path $dest 'effect.png'))
        try {
            for ($x = 0; $x -lt $alphas.Count; $x++) {
                $pixel = $result.GetPixel($x, 0)
                Check ([Math]::Abs($pixel.A - $alphas[$x]) -le 1) "$space alpha at $x"
                if ($x -eq 0) { Check ($pixel.ToArgb() -eq 0) "$space transparent black" }
                $actual = @($pixel.R,$pixel.G,$pixel.B)
                $foreground = @(200,110,60); $green = @(0,255,0)
                for ($c = 0; $c -lt 3; $c++) {
                    $expected = Composite $foreground[$c] $green[$c] $alphas[$x] $linear
                    $onGreen = Composite $actual[$c] $green[$c] $pixel.A $linear
                    Check ([Math]::Abs($expected-$onGreen) -le 2) "$space independent green plate at $x, $c"
                }
            }
        } finally { $result.Dispose() }
    }
    $darkDir = [IO.Directory]::CreateDirectory((Join-Path $root 'dark')).FullName
    $lightDir = [IO.Directory]::CreateDirectory((Join-Path $root 'light')).FullName
    foreach ($n in '0001.png','0002.png') {
        [IO.File]::Copy($black,(Join-Path $darkDir $n))
        [IO.File]::Copy($white,(Join-Path $lightDir $n))
    }
    $frames = Join-Path $root 'frames'
    Check ((& $export $darkDir $lightDir $frames).Frames -eq 2) 'sequence'
    Check ([IO.File]::Exists((Join-Path $frames '0002.png'))) 'sequence filename'
    $badOutput = Join-Path $root 'bad'
    Save-Row (Join-Path $lightDir '0002.png') @([Drawing.Color]::White)
    $failed = $false
    try { & $export $darkDir $lightDir $badOutput | Out-Null } catch { $failed = $true }
    Check ($failed -and !(Test-Path -LiteralPath $badOutput)) 'dimension mismatch publishes no partial sequence'
    [IO.File]::Move((Join-Path $lightDir '0002.png'),(Join-Path $lightDir '0003.png'))
    $failed = $false
    try { & $export $darkDir $lightDir $badOutput | Out-Null } catch { $failed = $true }
    Check ($failed -and !(Test-Path -LiteralPath $badOutput)) 'mismatched names rejected'
    Save-Row $black @([Drawing.Color]::Red); Save-Row $white @([Drawing.Color]::Blue)
    $bad = & $export $black $white (Join-Path $root 'residual') -WarningAction SilentlyContinue
    Check ($bad.MismatchedPixels -eq 1) 'incompatible pair flagged'
    $failed = $false
    try { & $export $black $white $frames | Out-Null } catch { $failed = $true }
    Check ($failed -and [IO.File]::Exists((Join-Path $frames '0002.png'))) 'existing output preserved'
    'PASS: known alpha and third-background composite in both blend spaces, sequence pairing, atomic failures and mismatch diagnostics.'
} finally {
    $fullRoot = [IO.Path]::GetFullPath($root)
    if ([IO.Path]::GetDirectoryName($fullRoot) -eq $base.TrimEnd('\') -and [IO.Path]::GetFileName($fullRoot) -match '^greenscreen-test-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $fullRoot -Recurse -Force
    }
}

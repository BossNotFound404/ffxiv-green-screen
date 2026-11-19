#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)][string]$Black,
    [Parameter(Mandatory, Position=1)][string]$White,
    [Parameter(Mandatory, Position=2)][string]$Output,
    [ValidateSet('LinearSRGB','Encoded')][string]$BlendSpace = 'LinearSRGB'
)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop') { throw 'Run this tool with Windows PowerShell (powershell.exe).' }

if (-not ('GreenScreen.AlphaFrames' -as [type])) {
    Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;

namespace GreenScreen {
    public sealed class AlphaFrames {
        readonly double[] decode = new double[256];
        readonly byte[] encode = new byte[65536];
        public long PixelCount { get; private set; }
        public long MismatchCount { get; private set; }

        public AlphaFrames(bool linear) {
            for (int i = 0; i < decode.Length; ++i) {
                double v = i / 255.0;
                decode[i] = !linear ? v : v <= 0.04045 ? v / 12.92 : Math.Pow((v + 0.055) / 1.055, 2.4);
            }
            for (int i = 0; i < encode.Length; ++i) {
                double v = i / 65535.0;
                double signal = !linear ? v : v <= 0.0031308 ? 12.92 * v : 1.055 * Math.Pow(v, 1 / 2.4) - 0.055;
                encode[i] = (byte)Math.Round(255 * signal);
            }
        }
        static double Clamp(double x) { return Math.Max(0, Math.Min(1, x)); }
        byte Encode(double x) { return encode[(int)Math.Round(65535 * Clamp(x))]; }

        static Bitmap Open(string path) {
            using (var reader = new BinaryReader(File.OpenRead(path))) {
                byte[] h = reader.ReadBytes(26);
                if (h.Length != 26 || BitConverter.ToString(h, 0, 8) != "89-50-4E-47-0D-0A-1A-0A"
                    || h[24] != 8 || (h[25] != 2 && h[25] != 6))
                    throw new InvalidDataException("Expected an 8-bit RGB or RGBA PNG: " + path);
            }
            return new Bitmap(path, false);
        }

        public void Export(string blackPath, string whitePath, string outputPath) {
            using (var black = Open(blackPath))
            using (var white = Open(whitePath)) {
                if (black.Size != white.Size)
                    throw new InvalidDataException("Frame dimensions differ: " + Path.GetFileName(blackPath));
                var format = PixelFormat.Format32bppArgb;
                var rect = new Rectangle(Point.Empty, black.Size);
                using (var result = new Bitmap(black.Width, black.Height, format)) {
                    var dark = black.LockBits(rect, ImageLockMode.ReadOnly, format);
                    var light = white.LockBits(rect, ImageLockMode.ReadOnly, format);
                    var rgba = result.LockBits(rect, ImageLockMode.WriteOnly, format);
                    try {
                        byte[] b = new byte[checked(black.Width * 4)], w = new byte[b.Length], output = new byte[b.Length];
                        for (int y = 0; y < black.Height; ++y) {
                            Marshal.Copy(IntPtr.Add(dark.Scan0, y * dark.Stride), b, 0, b.Length);
                            Marshal.Copy(IntPtr.Add(light.Scan0, y * light.Stride), w, 0, w.Length);
                            for (int i = 0; i < b.Length; i += 4) {
                                if (b[i+3] != 255 || w[i+3] != 255)
                                    throw new InvalidDataException("Capture inputs must be opaque: " + Path.GetFileName(blackPath));
                                if ((b[i] | b[i+1] | b[i+2]) == 0 && (w[i] & w[i+1] & w[i+2]) == 255) {
                                    output[i] = output[i+1] = output[i+2] = output[i+3] = 0;
                                    continue;
                                }
                                if (b[i] == w[i] && b[i+1] == w[i+1] && b[i+2] == w[i+2]) {
                                    output[i] = b[i]; output[i+1] = b[i+1]; output[i+2] = b[i+2]; output[i+3] = 255;
                                    continue;
                                }
                                double transmittance = Clamp(((decode[w[i]] - decode[b[i]])
                                    + (decode[w[i+1]] - decode[b[i+1]]) + (decode[w[i+2]] - decode[b[i+2]])) / 3);
                                double alpha = 1 - transmittance;
                                byte alpha8 = (byte)Math.Round(255 * alpha);
                                output[i+3] = alpha8;
                                bool mismatch = false;
                                for (int c = i; c < i + 3; ++c) {
                                    double premultiplied = (decode[b[c]] + decode[w[c]] - transmittance) / 2;
                                    output[c] = alpha8 == 0 ? (byte)0 : Encode(premultiplied / alpha);
                                    double p = decode[output[c]] * (alpha8 / 255.0);
                                    if (Math.Abs(Encode(p) - b[c]) > 8 ||
                                        Math.Abs(Encode(p + 1 - alpha8 / 255.0) - w[c]) > 8) mismatch = true;
                                }
                                if (mismatch) MismatchCount++;
                            }
                            Marshal.Copy(output, 0, IntPtr.Add(rgba.Scan0, y * rgba.Stride), output.Length);
                        }
                    } finally {
                        result.UnlockBits(rgba); white.UnlockBits(light); black.UnlockBits(dark);
                    }
                    using (var file = new FileStream(outputPath, FileMode.CreateNew))
                        result.Save(file, ImageFormat.Png);
                    PixelCount += (long)black.Width * black.Height;
                }
            }
        }
    }
}
'@
}

$dark = Get-Item -LiteralPath $Black
$light = Get-Item -LiteralPath $White
if ($dark.FullName -eq $light.FullName) { throw 'Choose two distinct captures.' }
if ($dark.PSIsContainer -ne $light.PSIsContainer) { throw 'Supply two PNG files or two folders of same-named frames.' }
if ($dark.PSIsContainer) {
    $blackFiles = @(Get-ChildItem -LiteralPath $dark.FullName -File -Filter '*.png' | Sort-Object Name)
    $whiteFiles = @(Get-ChildItem -LiteralPath $light.FullName -File -Filter '*.png' | Sort-Object Name)
    if ($blackFiles.Count -eq 0 -or $blackFiles.Count -ne $whiteFiles.Count) { throw 'PNG frame counts differ or are zero.' }
    for ($i = 0; $i -lt $blackFiles.Count; $i++) {
        if ($blackFiles[$i].Name -ne $whiteFiles[$i].Name) { throw 'Frame filenames differ. Match frame numbers in both folders first.' }
    }
} else {
    $blackFiles = @($dark)
    $whiteFiles = @($light)
}

$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
if (Test-Path -LiteralPath $destination) { throw 'Choose a new output folder. Existing files are preserved.' }
$parent = [IO.Path]::GetDirectoryName($destination)
if (-not [IO.Directory]::Exists($parent)) { throw 'The parent of the output folder must exist.' }
$staging = Join-Path $parent ('.greenscreen-' + [Guid]::NewGuid().ToString('N'))
$ownedFiles = [Collections.Generic.List[string]]::new()
$solver = [GreenScreen.AlphaFrames]::new($BlendSpace -eq 'LinearSRGB')
[void][IO.Directory]::CreateDirectory($staging)
try {
    for ($i = 0; $i -lt $blackFiles.Count; $i++) {
        $name = if ($dark.PSIsContainer) { $blackFiles[$i].Name } else { 'effect.png' }
        $path = Join-Path $staging $name
        $ownedFiles.Add($path)
        $solver.Export($blackFiles[$i].FullName, $whiteFiles[$i].FullName, $path)
    }
    [IO.Directory]::Move($staging, $destination)
} finally {
    if ([IO.Directory]::Exists($staging)) {
        foreach ($path in $ownedFiles) { [IO.File]::Delete($path) }
        [IO.Directory]::Delete($staging)
    }
}
if ($solver.MismatchCount) {
    Write-Warning ("{0} of {1} pixels differ by over 8/255 on recomposition. Check frame alignment, light, clipping and blend space." -f $solver.MismatchCount, $solver.PixelCount)
}
[pscustomobject]@{ Frames=$blackFiles.Count; MismatchedPixels=$solver.MismatchCount; TotalPixels=$solver.PixelCount; Output=$destination }

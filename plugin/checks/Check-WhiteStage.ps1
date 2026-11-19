param(
    [Parameter(Mandatory)] [string]$Package,
    [Parameter(Mandatory)] [string]$Baseline,
    [Parameter(Mandatory)] [string]$DalamudLibPath
)

$ErrorActionPreference = 'Stop'
Add-Type -Path (Join-Path $DalamudLibPath 'Lumina.dll')

function Read-WhiteModel([string]$Path) {
    $archive = [IO.Compression.ZipFile]::OpenRead((Resolve-Path $Path).Path)
    try {
        $entry = $archive.GetEntry('46/floor.mdl')
        if (!$entry) { throw "Missing white floor in $Path" }
        $buffer = [IO.MemoryStream]::new()
        $stream = $entry.Open()
        try { $stream.CopyTo($buffer); $bytes = $buffer.ToArray() }
        finally { $stream.Dispose(); $buffer.Dispose() }
    } finally { $archive.Dispose() }

    $model = [Lumina.Data.Files.MdlFile]::new()
    $flags = [Reflection.BindingFlags]'Instance,Public,NonPublic'
    $reader = [Lumina.Data.LuminaBinaryReader]::new($bytes, [Lumina.Data.Structs.PlatformId]::Win32)
    try {
        $model.GetType().GetProperty('Data', $flags).SetValue($model, $bytes)
        $model.GetType().GetProperty('Reader', $flags).SetValue($model, $reader)
        $model.LoadFile()
    } finally { $reader.Dispose() }
    return $model
}

$old = Read-WhiteModel $Baseline
$new = Read-WhiteModel $Package
$mesh = $new.Meshes[0]
$lod = $new.Lods[0]
if ($mesh.VertexCount -ne $old.Meshes[0].VertexCount + 8 -or
    $mesh.IndexCount -ne $old.Meshes[0].IndexCount + 30 -or
    $new.ModelHeader.MeshCount -ne 1 -or $mesh.VertexStreamCount -ne 2 -or
    $lod.IndexDataOffset + $lod.IndexBufferSize -ne $new.Data.Length) { throw 'Unexpected white mesh layout' }
foreach ($i in 0, 1) {
    $oldOffset = $old.Lods[0].VertexDataOffset + $old.Meshes[0].VertexBufferOffset[$i]
    $newOffset = $lod.VertexDataOffset + $mesh.VertexBufferOffset[$i]
    $count = $old.Meshes[0].VertexCount * $old.Meshes[0].VertexBufferStride[$i]
    if ($mesh.VertexBufferStride[$i] -ne $old.Meshes[0].VertexBufferStride[$i] -or
        [Convert]::ToBase64String($old.Data, $oldOffset, $count) -cne
        [Convert]::ToBase64String($new.Data, $newOffset, $count)) { throw "Floor vertex stream $i changed" }
}
$indices = $old.Lods[0].IndexBufferSize
if ([Convert]::ToBase64String($old.Data, $old.Lods[0].IndexDataOffset, $indices) -cne
    [Convert]::ToBase64String($new.Data, $lod.IndexDataOffset, $indices)) { throw 'Floor indices changed' }
if (!$new.ModelHeader.ShadowDisabled -or !$new.ModelHeader.LightShadowDisabled) { throw 'Backdrop casts a shadow' }
foreach ($box in @($new.BoundingBoxes, $new.ModelBoundingBoxes)) {
    if ($box.Min[1] -gt -12 -or $box.Max[1] -lt 256) { throw 'Backdrop exceeds culling bounds' }
}
'PASS Lumina loaded the white backdrop, original floor geometry, culling bounds and shadow settings'

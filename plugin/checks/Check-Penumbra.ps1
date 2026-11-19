param(
    [Parameter(Mandatory)] [string]$PluginDll,
    [Parameter(Mandatory)] [string]$PenumbraApiDll,
    [Parameter(Mandatory)] [string]$DalamudLibPath
)
$ErrorActionPreference = 'Stop'
Add-Type -Path (Join-Path $DalamudLibPath 'Mono.Cecil.dll')
$api = [Mono.Cecil.AssemblyDefinition]::ReadAssembly((Resolve-Path $PenumbraApiDll).Path)
$plugin = [Mono.Cecil.AssemblyDefinition]::ReadAssembly((Resolve-Path $PluginDll).Path)
try {
    $types = @{}
    foreach ($type in $api.MainModule.Types) {
        if ($type.Namespace -ne 'Penumbra.Api.IpcSubscribers') { continue }
        $label = $type.Fields | Where-Object Name -eq Label | Select-Object -First 1
        if ($label -and $type.BaseType.IsGenericInstance) {
            $types[$label.Constant] = $type.BaseType.GenericArguments.FullName -join '|'
        }
    }
    $calls = 0
    foreach ($method in ($plugin.MainModule.Types | Where-Object Name -eq Shelf).Methods) {
        if (-not $method.HasBody) { continue }
        foreach ($ins in $method.Body.Instructions) {
            if ($ins.Operand -isnot [Mono.Cecil.GenericInstanceMethod] -or $ins.Operand.Name -ne 'GetIpcSubscriber') { continue }
            $label = $ins.Previous.Operand
            $signature = $ins.Operand.GenericArguments.FullName -join '|'
            if (-not $types.ContainsKey($label) -or $types[$label] -cne $signature) { throw "IPC mismatch: $label in $($method.Name)" }
            Write-Output "PASS $label"
            $calls++
        }
    }
    if ($calls -lt 8) { throw "Only $calls calls were detected." }
} finally {
    $api.Dispose()
    $plugin.Dispose()
}

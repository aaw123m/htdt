param(
    [string]$RewBaseUrl = 'http://127.0.0.1:4735',
    [string]$OutputPath = ''
)

$ErrorActionPreference = 'Stop'

function Normalize-RewArray {
    param($Value)
    if ($null -eq $Value) { return @() }
    if ($Value -is [System.Array]) { return @($Value) }
    $valueProperty = $Value.PSObject.Properties['value']
    if ($null -ne $valueProperty) { return @($Value.value) }
    return @($Value)
}

function Get-RewJson {
    param([string]$Path)
    try {
        $request = [System.Net.HttpWebRequest]::Create($RewBaseUrl.TrimEnd('/') + $Path)
        $request.Method = 'GET'
        $request.Timeout = 3000
        $request.AutomaticDecompression = [System.Net.DecompressionMethods]::GZip -bor [System.Net.DecompressionMethods]::Deflate
        $response = $request.GetResponse()
        try {
            $stream = $response.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.UTF8Encoding]::new($false), $true)
            $payload = $reader.ReadToEnd()
        }
        finally { $response.Dispose() }
        return $payload | ConvertFrom-Json
    }
    catch { return [pscustomobject]@{ error = $_.Exception.Message } }
}

$computer = Get-CimInstance Win32_ComputerSystem
$video = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        driverVersion = $_.DriverVersion
    }
})

$audioEndpoints = @(Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{
        status = [string]$_.Status
        name = [string]$_.FriendlyName
    }
})
$rewOutputDevices = Normalize-RewArray (Get-RewJson '/audio/java/output-devices')
$rewInputDevices = Normalize-RewArray (Get-RewJson '/audio/java/input-devices')
$rewOutputChannels = Normalize-RewArray (Get-RewJson '/audio/java/output-channels')
$rewMeasurements = Get-RewJson '/measurements'

$result = [ordered]@{
    collectedAt = (Get-Date).ToUniversalTime().ToString('o')
    computer = [ordered]@{
        manufacturer = $computer.Manufacturer
        model = $computer.Model
    }
    video = $video
    windowsAudioEndpoints = $audioEndpoints
    yamahaEndpointPresent = [bool]($audioEndpoints | Where-Object { $_.name -match 'Yamaha|RX-A4A' })
    umik1EndpointPresent = [bool]($audioEndpoints | Where-Object { $_.name -match 'UMIK[- ]?1' })
    rew = [ordered]@{
        baseUrl = $RewBaseUrl
        audioStatus = Get-RewJson '/audio/status'
        driver = Get-RewJson '/audio/driver'
        sampleRate = Get-RewJson '/audio/samplerate'
        outputDevice = Get-RewJson '/audio/java/output-device'
        outputDevices = $rewOutputDevices
        inputDevice = Get-RewJson '/audio/java/input-device'
        inputDevices = $rewInputDevices
        inputDeviceChannels = Get-RewJson '/audio/java/num-input-device-channels'
        input = Get-RewJson '/audio/java/input'
        inputs = Normalize-RewArray (Get-RewJson '/audio/java/inputs')
        inputChannel = Get-RewJson '/audio/java/input-channel'
        inputChannels = Get-RewJson '/audio/java/num-input-channels'
        inputCal = Get-RewJson '/audio/input-cal'
        stereoOnly = Get-RewJson '/audio/java/stereo-only'
        outputDeviceChannels = Get-RewJson '/audio/java/num-output-device-channels'
        outputChannels = $rewOutputChannels
        outputChannelMapping = Get-RewJson '/audio/java/output-channel-mapping'
        measurements = $rewMeasurements
    }
}
$exclusiveCandidates = @()
if ($rewOutputDevices -is [System.Array]) {
    $exclusiveCandidates = @($rewOutputDevices | Where-Object { [string]$_ -like 'EXCL:*' })
}
$result.rew['exclusiveMultichannelCandidates'] = $exclusiveCandidates
$result.rew['umik1InputVisible'] = [bool]($rewInputDevices | Where-Object { [string]$_ -match 'UMIK[- ]?1' })
$currentOutputName = [string]$result.rew.outputDevice.device
$currentOutputIsExclusive = $currentOutputName -like 'EXCL:*'
$currentHardwareChannels = if ($result.rew.outputDeviceChannels -is [int] -or $result.rew.outputDeviceChannels -is [long]) { [int]$result.rew.outputDeviceChannels } else { 0 }
$result.rew['currentOutputIsExclusive'] = [bool]$currentOutputIsExclusive
$stereoOnly = [bool]$result.rew.stereoOnly.enable
$result.rew['javaMultichannelReady'] = [bool]($result.rew.audioStatus.ready -and $currentOutputIsExclusive -and $currentHardwareChannels -gt 2 -and -not $stereoOnly)

$json = $result | ConvertTo-Json -Depth 12
if ($OutputPath) {
    $resolved = [System.IO.Path]::GetFullPath($OutputPath)
    $parent = Split-Path -Parent $resolved
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [System.IO.File]::WriteAllText($resolved, $json, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Saved: $resolved"
}

Write-Host "Yamaha/RX-A4A endpoint present: $($result.yamahaEndpointPresent)"
Write-Host "UMIK-1 endpoint present: $($result.umik1EndpointPresent)"
Write-Host "REW UMIK-1 input visible: $($result.rew.umik1InputVisible)"
Write-Host "REW Java output hardware channels: $($result.rew.outputDeviceChannels)"
Write-Host "REW current output is WASAPI Exclusive: $($result.rew.currentOutputIsExclusive)"
Write-Host "REW Java multichannel ready: $($result.rew.javaMultichannelReady)"
if ($exclusiveCandidates.Count -gt 0) {
    Write-Host 'REW WASAPI-exclusive candidates:'
    $exclusiveCandidates | ForEach-Object { Write-Host "  $_" }
}
else {
    Write-Host 'No REW WASAPI-exclusive output candidate is currently visible.'
}

$json
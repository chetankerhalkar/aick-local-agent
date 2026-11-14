# Read-only System.Speech probe. This never synthesizes or plays audio.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled })
    if ($voices.Count -eq 0) { throw 'No enabled installed voice.' }
    $requestedVoice = $env:AICK_SPEECH_VOICE
    if ([string]::IsNullOrWhiteSpace($requestedVoice)) {
        $requestedVoice = $synth.Voice.Name
    }
    $matching = @($voices | Where-Object { $_.VoiceInfo.Name -ceq $requestedVoice })
    if ($matching.Count -ne 1) { throw 'Requested voice is unavailable.' }
    $synth.SelectVoice($requestedVoice)
    [pscustomobject]@{
        name = $synth.Voice.Name
        culture = $synth.Voice.Culture.Name
    } | ConvertTo-Json -Compress
} finally {
    $synth.Dispose()
}

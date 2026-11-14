param(
    [Parameter(Mandatory=$true)][string]$TextPath,
    [Parameter(Mandatory=$true)][string]$WavPath
)
# Fixed program: untrusted reply text is read as data, never parsed as PowerShell/SSML.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
if ($text.Length -lt 1 -or $text.Length -gt 1000) { throw 'Speech text length rejected.' }
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voice = $env:AICK_SPEECH_VOICE
    if ([string]::IsNullOrWhiteSpace($voice)) { $voice = $synth.Voice.Name }
    $matches = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Name -ceq $voice })
    if ($matches.Count -ne 1) { throw 'Requested installed voice is unavailable.' }
    $synth.SelectVoice($voice)
    $synth.Rate = 0
    $synth.Volume = 100
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($WavPath, $format)
    $synth.Speak($text)
    $synth.SetOutputToNull()
    [pscustomobject]@{ name = $synth.Voice.Name; culture = $synth.Voice.Culture.Name } | ConvertTo-Json -Compress
} finally {
    $synth.Dispose()
}

<#
.SYNOPSIS
    Compatibilite des plugins entre deux instances SonarQube.

.DESCRIPTION
    Regle de Project Move : l'instance CIBLE doit posseder tous les plugins de
    la SOURCE, dans les memes versions. La cible peut en avoir davantage.

    Aucune dependance : PowerShell 5.1 ou superieur, present par defaut sur
    Windows. Pas de jq, pas de Python.

.EXAMPLE
    # A partir de deux fichiers recuperes par curl
    .\Compare-Plugins.ps1 -SourceFile entite.json -TargetFile centrale.json

.EXAMPLE
    # Directement depuis les deux instances
    .\Compare-Plugins.ps1 `
        -SourceUrl https://sonar.entite.corp -SourceToken squ_xxx `
        -TargetUrl https://sonar.groupe.corp -TargetToken squ_yyy

.NOTES
    Codes de sortie : 0 compatible, 1 ecart bloquant, 2 erreur.
    Le token doit avoir le droit "Administer System" sur chaque instance.
#>

[CmdletBinding()]
param(
    [string]$SourceFile,
    [string]$TargetFile,
    [string]$SourceUrl,
    [string]$SourceToken,
    [string]$TargetUrl,
    [string]$TargetToken,
    [switch]$AsJson
)

function Get-Plugins {
    param([string]$Url, [string]$Token)
    $pair    = "${Token}:"
    $b64     = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
    $headers = @{ Authorization = "Basic $b64" }
    $endpoint = ($Url.TrimEnd('/')) + '/api/plugins/installed'
    Invoke-RestMethod -Uri $endpoint -Headers $headers -TimeoutSec 30
}

function ConvertTo-Map {
    param($Data)
    $map = @{}
    foreach ($p in $Data.plugins) {
        $map[$p.key] = [pscustomobject]@{
            Version = $(if ($p.version) { $p.version } else { '' })
            Bundled = [bool]$p.editionBundled
        }
    }
    $map
}

# --- Lecture des inventaires ---------------------------------------------- #

try {
    if ($SourceFile -and $TargetFile) {
        $srcRaw  = Get-Content -Raw -LiteralPath $SourceFile | ConvertFrom-Json
        $tgtRaw  = Get-Content -Raw -LiteralPath $TargetFile | ConvertFrom-Json
        $srcName = $SourceFile ; $tgtName = $TargetFile
    }
    elseif ($SourceUrl -and $TargetUrl) {
        $srcRaw  = Get-Plugins -Url $SourceUrl -Token $SourceToken
        $tgtRaw  = Get-Plugins -Url $TargetUrl -Token $TargetToken
        $srcName = $SourceUrl ; $tgtName = $TargetUrl
    }
    else {
        Write-Host "Usage : voir Get-Help .\Compare-Plugins.ps1 -Full"
        exit 2
    }
}
catch {
    Write-Error "Erreur de lecture : $_"
    exit 2
}

$src = ConvertTo-Map $srcRaw
$tgt = ConvertTo-Map $tgtRaw

# --- Comparaison ----------------------------------------------------------- #

$absents = @()
$versions = @()

foreach ($k in ($src.Keys | Sort-Object)) {
    if (-not $tgt.ContainsKey($k)) {
        $absents += [pscustomobject]@{
            key = $k ; version = $src[$k].Version ; bundled = $src[$k].Bundled
        }
    }
    elseif ($tgt[$k].Version -ne $src[$k].Version) {
        $versions += [pscustomobject]@{
            key = $k ; bundled = $src[$k].Bundled
            version_source = $src[$k].Version
            version_cible  = $tgt[$k].Version
        }
    }
}

$extras = @()
foreach ($k in ($tgt.Keys | Sort-Object)) {
    if (-not $src.ContainsKey($k)) {
        $extras += [pscustomobject]@{ key = $k ; version = $tgt[$k].Version }
    }
}

$compatible = ($absents.Count -eq 0 -and $versions.Count -eq 0)

# --- Restitution ------------------------------------------------------------ #

if ($AsJson) {
    [pscustomobject]@{
        absents_cible        = $absents
        versions_differentes = $versions
        cible_seulement      = $extras
        compatible           = $compatible
    } | ConvertTo-Json -Depth 5
    if ($compatible) { exit 0 } else { exit 1 }
}

Write-Host "Comparaison des plugins"
Write-Host ("  source : {0}  ({1} plugins)" -f $srcName, $src.Count)
Write-Host ("  cible  : {0}  ({1} plugins)" -f $tgtName, $tgt.Count)

if ($absents.Count -gt 0) {
    Write-Host ""
    Write-Host ("BLOQUANT - {0} plugin(s) de la source absent(s) de la cible" -f $absents.Count) -ForegroundColor Red
    Write-Host "  La CIBLE doit les installer, ou la source doit les retirer."
    Write-Host ""
    foreach ($p in $absents) {
        $suf = if ($p.bundled) { "   [fourni avec l edition]" } else { "" }
        Write-Host ("    {0} {1}{2}" -f $p.key, $p.version, $suf)
    }
}

if ($versions.Count -gt 0) {
    Write-Host ""
    Write-Host ("BLOQUANT - {0} plugin(s) en version differente" -f $versions.Count) -ForegroundColor Red
    Write-Host "  La SOURCE doit s aligner sur la version de la cible."
    Write-Host ""
    foreach ($p in $versions) {
        $suf = if ($p.bundled) { "   [fourni avec l edition]" } else { "" }
        Write-Host ("    {0}   source {1}  ->  cible {2}{3}" -f `
                    $p.key, $p.version_source, $p.version_cible, $suf)
    }
}

if ($extras.Count -gt 0) {
    Write-Host ""
    Write-Host ("SANS EFFET - {0} plugin(s) present(s) uniquement sur la cible" -f $extras.Count) -ForegroundColor Yellow
    Write-Host "  Autorise par Project Move. Aucune action cote source."
    Write-Host ""
    foreach ($p in ($extras | Select-Object -First 20)) {
        Write-Host ("    {0} {1}" -f $p.key, $p.version)
    }
    if ($extras.Count -gt 20) {
        Write-Host ("    ... et {0} autre(s)" -f ($extras.Count - 20))
    }
}

Write-Host ""
if ($compatible) {
    Write-Host "COMPATIBLE - l import peut avoir lieu." -ForegroundColor Green
    exit 0
} else {
    Write-Host ("INCOMPATIBLE - {0} ecart(s) bloquant(s) a traiter." -f `
                ($absents.Count + $versions.Count)) -ForegroundColor Red
    exit 1
}

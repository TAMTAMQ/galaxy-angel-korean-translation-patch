param([string]$FrozenImages, [string]$BuildDirectory)
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = 'C:\Users\Timon\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$GameRoot = Join-Path $ProjectRoot 'work\galaxy_angel'
$Assets = Join-Path $GameRoot 'assets\translation'
$Source = Join-Path $GameRoot 'source\scenario'
$Build = Join-Path $GameRoot 'build'
if ($BuildDirectory) { $Build = $BuildDirectory }
$OriginalIso = 'D:\game\pcsx2-v2.6.3-windows-x64-Qt\roms\Galaxy Angel (Japan).iso'
$ImageExtraction = Join-Path $GameRoot 'assets\image_extraction\GADAT032'
$ImageOriginal = Join-Path $ImageExtraction 'png'
$ImageSource = Join-Path $GameRoot 'assets\image_extraction\japanese_images\png'
$ImageWork = Join-Path $GameRoot 'assets\image_extraction\japanese_images\translated_png'
$ImageManifest = Join-Path $GameRoot 'assets\image_extraction\japanese_images\manifest.json'
$ImageReport = Join-Path $Build 'image_patch_report.json'
$ImageRuntimeAudit = Join-Path $Build 'image_runtime_audit.json'
$ImagePatchCache = Join-Path $Build 'image_patch_cache'
$MiniGameCache = Join-Path $Build 'minigame_elf_translations.json'
$MiniImageExtraction = Join-Path $GameRoot 'assets\image_extraction\MINI'
$MiniImageWork = Join-Path $MiniImageExtraction 'translated_png'
$MiniImageLocalization = Join-Path $Build 'mini_image_localization.json'
$MiniDirectLocalization = Join-Path $Build 'mini_image_direct_additions.json'
$MiniImagePatchReport = Join-Path $Build 'mini_image_patch_report.json'
$MiniRuntimeReport = Join-Path $Build 'mini_runtime_image_patch_report.json'
$MiniSeedIso = Join-Path $Build 'Galaxy Angel (Korean) MINI Updated.iso'
$SeedArgs = @('--seed-iso', $MiniSeedIso)
$RuntimeSeedArgs = @('--seed-runtime-iso', $MiniSeedIso)
$RecipeArgs = @()
if ($FrozenImages) {
  $MiniImageExtraction = Join-Path $FrozenImages 'MINI'
  $MiniImageLocalization = Join-Path $FrozenImages 'mini_image_localization.json'
  $MiniDirectLocalization = Join-Path $FrozenImages 'mini_image_direct_additions.json'
  $ImageWork = Join-Path $FrozenImages 'UI'
  $MiniGameCache = Join-Path $GameRoot 'build/minigame_elf_translations.json'
  $SeedArgs = @()
  $RuntimeSeedArgs = @('--preserve-pixels')
  $RecipeArgs = @('--recipe-png', (Join-Path $MiniImageExtraction 'translated_png/mini/mini00/resipi.png'))
}

# The seed ISO exists only to skip recompressing artwork that has not changed.  Once any
# translated PNG is newer than it, its named blocks no longer describe the current artwork and
# the image patcher refuses them outright, so the seed is dropped for this build.  The runtime
# seed is kept either way: that one reuses a stream only when the image's translated bytes are
# unchanged, so a stale seed can only help — and it is what keeps an untouched runtime copy
# from having to be refitted with a merged palette.
if ($SeedArgs.Count -gt 0) {
  if (-not (Test-Path $MiniSeedIso)) {
    Write-Host 'MINI seed ISO is absent; every image block will be rebuilt.'
    $SeedArgs = @()
    $RuntimeSeedArgs = @()
  } else {
    $SeedStamp = (Get-Item $MiniSeedIso).LastWriteTimeUtc
    $NewestPng = Get-ChildItem -Path (Join-Path $MiniImageExtraction 'translated_png') -Recurse -Filter *.png |
      Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($NewestPng -and $NewestPng.LastWriteTimeUtc -gt $SeedStamp) {
      Write-Host "MINI seed ISO is older than $($NewestPng.Name); its named blocks will be rebuilt."
      $SeedArgs = @()
    }
  }
}

New-Item -ItemType Directory -Force -Path $Build | Out-Null
Start-Transcript -Path (Join-Path $Build 'build_patch.log') -Force | Out-Null

function Assert-NativeSuccess([string]$Step) {
  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with exit code $LASTEXITCODE"
  }
}

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_minigame_elf.py') translate `
  --input-elf (Join-Path $GameRoot 'original\SLPM_652.54') `
  --cache $MiniGameCache
Assert-NativeSuccess 'Mini-game Gemma translation'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_font.py') build `
  --assets $Assets `
  --input-elf (Join-Path $GameRoot 'original\SLPM_652.54') `
  --output-elf (Join-Path $Build 'SLPM_652.54') `
  --map-output (Join-Path $Build 'font_map.json') `
  --extra-translations $MiniGameCache `
  --font (Join-Path $ProjectRoot 'vendor\pretendard\packages\pretendard\dist\public\static\alternative\Pretendard-Bold.ttf')
Assert-NativeSuccess 'Font build'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_minigame_elf.py') patch `
  --input-elf (Join-Path $Build 'SLPM_652.54') `
  --output-elf (Join-Path $Build 'SLPM_652.54') `
  --cache $MiniGameCache `
  --font-map (Join-Path $Build 'font_map.json')
Assert-NativeSuccess 'Mini-game ELF patch'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_speakers.py') `
  --input (Join-Path $GameRoot 'original\GADAT000.DAT') `
  --output (Join-Path $Build 'GADAT000.DAT') `
  --names (Join-Path $Assets 'speaker_names.json') `
  --encoding-map (Join-Path $Build 'font_map.json')
Assert-NativeSuccess 'Speaker-name build'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_translation.py') apply `
  --source $Source `
  --assets $Assets `
  --encoding-map (Join-Path $Build 'font_map.json') `
  --output (Join-Path $Build 'scenario')
Assert-NativeSuccess 'Scenario build'

Write-Host 'Building ISO: scenario recompression can take about 3-4 minutes.'
& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_build.py') `
  --original-iso $OriginalIso `
  --output-iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --original-scenario $Source `
  --built-scenario (Join-Path $Build 'scenario') `
  --patched-elf (Join-Path $Build 'SLPM_652.54') `
  --patched-speakers (Join-Path $Build 'GADAT000.DAT') `
  --lz-script (Join-Path $ProjectRoot 'tools\ikusa_lz.py')
Assert-NativeSuccess 'ISO build'

# Current translated PNGs are authoritative.  Do not regenerate them during a
# release build: verify their source/translated hashes and every duplicate
# occurrence path against the two approved localization manifests instead.
& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_minigame_translation_inputs.py') `
  --extraction $MiniImageExtraction `
  --translations $MiniImageLocalization `
  --translations $MiniDirectLocalization `
  --allow-extra 'mini/mini00/resipi.png' `
  --report (Join-Path $Build 'mini_translation_input_verification.json')
Assert-NativeSuccess 'MINI translation input verification'

# Patch all approved MINI image sets together so resources shared by the main
# pass and the later additions are rebuilt only once.  The seed ISO is itself
# verified against the current PNG hashes; its already-compressed named blocks
# are reused byte-for-byte to avoid regenerating approved artwork or spending
# minutes recompressing large TAG resources during every release build.
& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_patch_minigame_images.py') `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --extraction $MiniImageExtraction `
  --translations $MiniImageLocalization `
  --translations $MiniDirectLocalization `
  @SeedArgs `
  --report $MiniImagePatchReport
Assert-NativeSuccess 'MINI image build and verification'

# Apply the shared recipe-card label after every generic MINI.DAT image rewrite.
# This ordering guarantees that mini/mini00/resipi.agi is the final payload in
# the ISO even when the MINI image localization set is expanded later.
& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_patch_minigame_recipe.py') `
  @RecipeArgs `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --font (Join-Path $ProjectRoot 'vendor\pretendard\packages\pretendard\dist\public\static\alternative\Pretendard-Bold.ttf')
Assert-NativeSuccess 'Milfeulle recipe-card image patch'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_minigame_iso.py') `
  @RecipeArgs `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --extraction $MiniImageExtraction `
  --translations $MiniImageLocalization `
  --font (Join-Path $ProjectRoot 'vendor\pretendard\packages\pretendard\dist\public\static\alternative\Pretendard-Bold.ttf') `
  --report (Join-Path $Build 'mini_image_iso_verification.json')
Assert-NativeSuccess 'MINI final ISO image verification'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_minigame_iso.py') `
  @RecipeArgs `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --extraction $MiniImageExtraction `
  --translations $MiniDirectLocalization `
  --font (Join-Path $ProjectRoot 'vendor\pretendard\packages\pretendard\dist\public\static\alternative\Pretendard-Bold.ttf') `
  --report (Join-Path $Build 'mini_image_direct_iso_verification.json')
Assert-NativeSuccess 'MINI direct-addition ISO image verification'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_build_battle.py') `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --assets $Assets `
  --encoding-map (Join-Path $Build 'font_map.json') `
  --cache-dir (Join-Path $Build 'battle_compressed_cache')
Assert-NativeSuccess 'Battle and runtime-copy build'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_localized_ui.py') `
  --source $ImageSource `
  --output $ImageWork `
  --baseline $ImageOriginal `
  --report (Join-Path $Build 'localized_ui_verification.json')
Assert-NativeSuccess 'Localized UI PNG verification'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_gadat032_codec.py') `
  --extraction $ImageExtraction
Assert-NativeSuccess 'GADAT032 TEX codec verification'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_audit_gadat032_runtime.py') `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --manifest $ImageManifest `
  --runtime-container SLGRES `
  --runtime-container MISC `
  --runtime-container SLGINIT `
  --runtime-container ADV `
  --runtime-container ALBUM `
  --strict-name-data-container SLGRES `
  --strict-name-data-container MISC `
  --report $ImageRuntimeAudit
Assert-NativeSuccess 'GADAT032 runtime-copy audit'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_patch_gadat032_images.py') `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --images-dir $ImageWork `
  --original-png-dir $ImageOriginal `
  --runtime-container SLGRES `
  --runtime-container MISC `
  --runtime-container SLGINIT `
  --runtime-container ADV `
  --runtime-container ALBUM `
  --strict-name-data-container SLGRES `
  --strict-name-data-container MISC `
  --report $ImageReport `
  --cache-dir $ImagePatchCache
Assert-NativeSuccess 'GADAT032 and runtime image build'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_gadat032_images.py') `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --report $ImageReport
Assert-NativeSuccess 'GADAT032 and runtime image verification'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_stable_iso.py') `
  --original-iso $OriginalIso `
  --patched-iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --built-scenario (Join-Path $Build 'scenario')
Assert-NativeSuccess 'All external scenario-call verification'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_audit_runtime_indexes.py') `
  --original-iso $OriginalIso `
  --patched-iso (Join-Path $Build 'Galaxy Angel (Korean).iso')
Assert-NativeSuccess 'Full-ISO runtime scenario-index audit'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_battle_iso.py') `
  --original-iso $OriginalIso `
  --patched-iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --assets $Assets `
  --encoding-map (Join-Path $Build 'font_map.json')
Assert-NativeSuccess 'Battle dialogue verification'

# MINI.DAT has a second unindexed image-block pool used by the running game.
# Patch it last so no later ISO pass can restore an original runtime block.
& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_patch_minigame_runtime_images.py') `
  --original-iso $OriginalIso `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --mini-patch-report $MiniImagePatchReport `
  @RuntimeSeedArgs `
  --report $MiniRuntimeReport
Assert-NativeSuccess 'MINI runtime-copy image patch'

# Re-open the finished ISO after the FSTS runtime table rewrite.  This catches
# stale compressed-size entries that cannot be detected by the named PIDX pass.
& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_minigame_iso.py') `
  @RecipeArgs `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --extraction $MiniImageExtraction `
  --translations $MiniImageLocalization `
  --font (Join-Path $ProjectRoot 'vendor\pretendard\packages\pretendard\dist\public\static\alternative\Pretendard-Bold.ttf') `
  --runtime-report $MiniRuntimeReport `
  --report (Join-Path $Build 'mini_image_iso_verification.json')
Assert-NativeSuccess 'MINI final runtime/FSTS verification'

& $Python (Join-Path $ProjectRoot 'tools\galaxy_angel_verify_minigame_iso.py') `
  @RecipeArgs `
  --iso (Join-Path $Build 'Galaxy Angel (Korean).iso') `
  --extraction $MiniImageExtraction `
  --translations $MiniDirectLocalization `
  --font (Join-Path $ProjectRoot 'vendor\pretendard\packages\pretendard\dist\public\static\alternative\Pretendard-Bold.ttf') `
  --runtime-report $MiniRuntimeReport `
  --report (Join-Path $Build 'mini_image_direct_iso_verification.json')
Assert-NativeSuccess 'MINI direct-addition final runtime/FSTS verification'

Stop-Transcript | Out-Null

param([string]$Path, [string]$Out)
$ErrorActionPreference = "Stop"
$pp = New-Object -ComObject PowerPoint.Application
try {
  # Untitled=-1 opens a template as a new, unsaved presentation; ReadOnly + no window = no changes, no UI
  $pres = $pp.Presentations.Open($Path, -1, -1, 0)
  $result = [ordered]@{
    width_pt = $pres.PageSetup.SlideWidth; height_pt = $pres.PageSetup.SlideHeight
    layouts  = @($pres.SlideMaster.CustomLayouts | ForEach-Object { $_.Name })
    slides   = @()
  }
  foreach ($s in $pres.Slides) {
    $shapes = @()
    foreach ($sh in $s.Shapes) {
      $shapes += [ordered]@{ name = $sh.Name; type = [int]$sh.Type; left = [math]::Round($sh.Left,2); top = [math]::Round($sh.Top,2); width = [math]::Round($sh.Width,2); height = [math]::Round($sh.Height,2) }
    }
    $result.slides += [ordered]@{ index = $s.SlideIndex; layout = $s.CustomLayout.Name; shapes = $shapes }
  }
  $pres.Close()
  $result | ConvertTo-Json -Depth 6 | Set-Content -Path $Out -Encoding UTF8
  "ok"
} finally { $pp.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($pp) }

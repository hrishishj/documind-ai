param([string]$Docx, [string]$Xls, [string]$Out)
$ErrorActionPreference = "Stop"
$r = [ordered]@{}
$w = New-Object -ComObject Word.Application
$w.Visible = $false; $w.DisplayAlerts = 0
try {
  $d = $w.Documents.Open($Docx, $false, $true)   # ConfirmConversions=false, ReadOnly=true
  $ps = $d.PageSetup
  $r.word = [ordered]@{ paragraphs = $d.Paragraphs.Count; tables = $d.Tables.Count; sections = $d.Sections.Count
    page_w = $ps.PageWidth; page_h = $ps.PageHeight; top = $ps.TopMargin; bottom = $ps.BottomMargin; left = $ps.LeftMargin; right = $ps.RightMargin
    header_dist = $ps.HeaderDistance; footer_dist = $ps.FooterDistance }
  $d.Close($false)
} finally { $w.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($w) }

$x = New-Object -ComObject Excel.Application
$x.Visible = $false; $x.DisplayAlerts = $false
try {
  $wb = $x.Workbooks.Open($Xls, 0, $true)       # UpdateLinks=0, ReadOnly=true
  $sheets = @()
  foreach ($n in @("ome statements", "sheets", "uncertain tax positions")) {
    $ws = $wb.Worksheets.Item($n)
    $cells = @()
    $addrs = foreach ($row in 1..14) { foreach ($col in "A","B","C","D","E","F") { "$col$row" } }
    foreach ($addr in $addrs) {
      $c = $ws.Range($addr)
      $cells += [ordered]@{ addr = $addr; font = $c.Font.Name; size = $c.Font.Size; bold = [bool]$c.Font.Bold; fmt = $c.NumberFormat; halign = [int]$c.HorizontalAlignment
        merged = [bool]$c.MergeCells; fill = [int]$c.Interior.ColorIndex }
    }
    $sheets += [ordered]@{ name = $n; colA_width = $ws.Columns.Item(1).ColumnWidth; colC_width = $ws.Columns.Item(3).ColumnWidth
      used = $ws.UsedRange.Address($false,$false); cells = $cells }
  }
  $r.excel = [ordered]@{ sheet_count = $wb.Worksheets.Count; sheets = $sheets }
  $wb.Close($false)
} finally { $x.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($x) }
$r | ConvertTo-Json -Depth 8 | Set-Content -Path $Out -Encoding UTF8
"ok"

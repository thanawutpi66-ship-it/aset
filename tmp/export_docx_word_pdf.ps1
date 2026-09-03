param(
    [Parameter(Mandatory = $true)][string]$InputDocx,
    [Parameter(Mandatory = $true)][string]$OutputPdf
)

$word = $null
$document = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $document = $word.Documents.Open($InputDocx, $false, $false)
    $document.Fields.Update() | Out-Null
    foreach ($toc in $document.TablesOfContents) { $toc.Update() }
    $wdFormatPDF = 17
    $document.SaveAs([ref]$OutputPdf, [ref]$wdFormatPDF)
}
finally {
    if ($document -ne $null) { $document.Close([ref]$false) }
    if ($word -ne $null) { $word.Quit() }
}

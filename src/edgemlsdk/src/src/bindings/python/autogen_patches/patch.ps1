Param(
    [string]$PatchFiles,
    [string]$Destination)

function Test-FileLock {
  param (
    [parameter(Mandatory=$true)][string]$Path
  )

  $oFile = New-Object System.IO.FileInfo $Path

  if ((Test-Path -Path $Path) -eq $false) {
    return $false
  }

  try {
    $oStream = $oFile.Open([System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)

    if ($oStream) {
      $oStream.Close()
    }
    return $false
  } catch {
    # file is locked by a process.
    return $true
  }
}

# Give previous scripts in build a chance to release handle
# Simply making a cmake dependency doesn't appear to gurantee this
while((Test-FileLock -Path $Destination) -eq $true)
{
    Start-Sleep -Milliseconds 250 
}

# Apply patches to the generated code
$patch_files = $PatchFiles -split ','
$patch_files | ForEach-Object {
              $PatchFile = $_
              $firstLine = Get-Content $PatchFile | Select-Object -First 1
              $firstLine = $firstLine -replace '\*', '\*'
              $firstLine = $firstLine -replace '\(', '\('
              $firstLine = $firstLine -replace '\)', '\)'
              $pattern = $firstLine+'((.|\n)+?(?=return))'

              (Get-Content $Destination -Raw) -replace $pattern, (Get-Content $PatchFile -Raw) | Out-File $Destination
          }

# Wrap director interfaces with allow/end thread
# SWIG removes these when making an interface a director but it is needed 
# otherwise background operations can cause the Python interpreter to be blocked
$regexPattern = 'if \(upcall\) \{\s+Swig::DirectorPureVirtualException::raise\(".*?"\);\s+\} else \{\s+(.*?)\s+\}'
$replacementPattern = 'if (upcall) {      Swig::DirectorPureVirtualException::raise("$1");' + "`n" +
                      '    } else {' + "`n" +
                      '      SWIG_PYTHON_THREAD_BEGIN_ALLOW;' + "`n" +
                      '      $1' + "`n" +
                      '      SWIG_PYTHON_THREAD_END_ALLOW;' + "`n" +
                      '    }'


(Get-Content $Destination -Raw) -replace $regexPattern, $replacementPattern | Out-File $Destination
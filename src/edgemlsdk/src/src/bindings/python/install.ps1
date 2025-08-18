Param(
[string]$PythonPackageDirectory,
[string]$InstallDirectory,
[string]$Version,
[string]$PythonVersion
)

# Copy package to install directory
Write-Host "Copy-Item -Recurse -Force $PythonPackageDirectory -Destination $InstallDirectory"
Copy-Item -Recurse -Force $PythonPackageDirectory -Destination $InstallDirectory/python_package
"__version__ = '$Version'" | Out-File -FilePath $InstallDirectory/python_package/src/panoramasdkv2/panorama_version.py

# Set the version
$projFile="$InstallDirectory/python_package/pyproject.toml"

(Get-Content $projFile -Raw) -replace "0.0.1", "$Version+${PythonVersion}-$GitCommitHash" | Out-File $projFile

pushd $InstallDirectory/python_package
if($PythonVersion -eq "3.6")
{
    Write-Host "Building with Python3.6"
    python3.6 -m build
}
elseif($PythonVersion -eq "3.9")
{
    Write-Host "Building with Python3.9"
    python3.9 -m build
}
elseif($PythonVersion -eq "3.10")
{
    Write-Host "Building with Python3.10"
    python3.10 -m build
}

popd
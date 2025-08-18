Param(
[string]$InstallDirectory,
[string]$Version
)

# Copy package to install directory
#Write-Host "Copy-Item -Recurse -Force $PythonPackageDirectory -Destination $InstallDirectory"
#Copy-Item -Recurse -Force $PythonPackageDirectory -Destination $InstallDirectory/python_package
"__version__ = '$Version'" | Out-File -FilePath $InstallDirectory/python_package/src/panorama/panorama_version.py

# Set the version
$projFile="$InstallDirectory/python_package/pyproject.toml"

(Get-Content $projFile -Raw) -replace "0.0.1", "$Version" | Out-File $projFile

pushd $InstallDirectory/python_package
python3 -m build
popd
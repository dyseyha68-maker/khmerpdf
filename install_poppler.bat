@echo off
echo Downloading Poppler for Windows...
powershell -Command "Invoke-WebRequest 'https://github.com/oschwartz10612/poppler-windows/releases/download/v26.02.0-0/Release-26.02.0-0.zip' -OutFile 'D:\poppler_dl.zip'"
echo Extracting to D:\poppler...
powershell -Command "Expand-Archive -Path 'D:\poppler_dl.zip' -DestinationPath 'D:\poppler_tmp' -Force; $inner = Get-ChildItem 'D:\poppler_tmp' -Directory | Select-Object -First 1; Move-Item $inner.FullName 'D:\poppler' -Force; Remove-Item 'D:\poppler_tmp' -Recurse -Force; Remove-Item 'D:\poppler_dl.zip' -Force"
echo Done! Installed at D:\poppler
timeout /t 4

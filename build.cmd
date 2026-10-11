@echo off
setlocal
cd /d "%~dp0"

rem ======================================================================
rem  Builds release\vcam\vcam.exe (a FOLDER build) with ALL engines:
rem    - RobustVideoMatting on CUDA or CPU
rem    - MediaPipe selfie segmentation
rem  using the ACTIVE virtual environment.
rem  Activate your dev venv first, then run this file.
rem
rem  The CUDA 12 / cuDNN 9 DLLs from the venv's site-packages\nvidia\*\bin
rem  are copied into the app folder. Ship the whole release\vcam folder.
rem  PyInstaller work files and the .spec go to %TEMP% and are deleted.
rem ======================================================================

set "OUT=%~dp0release"
set "WORK=%TEMP%\vcam_pyinstaller"
set "PYTHONDONTWRITEBYTECODE=1"

if not defined VIRTUAL_ENV (
  echo ERROR: no virtual environment is active. Activate your venv first.
  goto :fail
)
if not exist "%~dp0vcam_ui.py" (
  echo ERROR: vcam_ui.py not found next to build.cmd
  goto :fail
)
if not exist "%~dp0models\rvm_mobilenetv3_fp32.onnx" (
  echo ERROR: models\rvm_mobilenetv3_fp32.onnx not found.
  echo Run vcam once from source with the RVM engine so it downloads the model.
  goto :fail
)
if not exist "%~dp0models\selfie_segmenter.tflite" (
  echo ERROR: models\selfie_segmenter.tflite not found.
  echo Run: python vcam.py --engine mediapipe --no-virtualcam
  echo once so it downloads the model, then build again.
  goto :fail
)
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: PyInstaller is not installed in the active venv.
  goto :fail
)
python -c "import mediapipe" >nul 2>&1
if errorlevel 1 (
  echo ERROR: mediapipe is not installed in the active venv.
  goto :fail
)

rem ---- locate the venv site-packages ----
set "SP="
for /f "usebackq delims=" %%i in (`python -c "import sysconfig;print(sysconfig.get_path('purelib'))"`) do set "SP=%%i"
if not defined SP (
  echo ERROR: could not locate site-packages.
  goto :fail
)
if not exist "%SP%\nvidia\cudnn\bin" (
  echo ERROR: the nvidia CUDA packages are not installed in this venv.
  echo Expected %SP%\nvidia\cudnn\bin
  goto :fail
)

rem ---- bundle the CUDA runtime DLLs next to the app ----
set "NV="
if exist "%SP%\nvidia\cuda_runtime\bin" set NV=%NV% --add-binary "%SP%\nvidia\cuda_runtime\bin\*.dll;nvidia\cuda_runtime\bin"
if exist "%SP%\nvidia\cuda_nvrtc\bin" set NV=%NV% --add-binary "%SP%\nvidia\cuda_nvrtc\bin\*.dll;nvidia\cuda_nvrtc\bin"
if exist "%SP%\nvidia\cublas\bin" set NV=%NV% --add-binary "%SP%\nvidia\cublas\bin\*.dll;nvidia\cublas\bin"
if exist "%SP%\nvidia\cufft\bin" set NV=%NV% --add-binary "%SP%\nvidia\cufft\bin\*.dll;nvidia\cufft\bin"
if exist "%SP%\nvidia\curand\bin" set NV=%NV% --add-binary "%SP%\nvidia\curand\bin\*.dll;nvidia\curand\bin"
if exist "%SP%\nvidia\cudnn\bin" set NV=%NV% --add-binary "%SP%\nvidia\cudnn\bin\*.dll;nvidia\cudnn\bin"

rem ---- bundle the MediaPipe models (no download needed at run time) ----
set MODELS=--add-data "%~dp0models\selfie_segmenter.tflite;models"
if exist "%~dp0models\selfie_segmenter_landscape.tflite" set MODELS=%MODELS% --add-data "%~dp0models\selfie_segmenter_landscape.tflite;models"

rem ---- optional pieces: only added when present ----
set "ICON="
if exist "%~dp0assets\icon.ico" set ICON=--icon "%~dp0assets\icon.ico"
set "ASSETS="
if exist "%~dp0assets" set ASSETS=--add-data "%~dp0assets;assets"

echo Building vcam using %VIRTUAL_ENV% ...
python -m PyInstaller --noconfirm --clean --onedir --windowed --name vcam %ICON% --add-data "%~dp0models\rvm_mobilenetv3_fp32.onnx;models" %MODELS% %ASSETS% %NV% --collect-all onnxruntime --collect-all pyvirtualcam --collect-all mediapipe --distpath "%OUT%" --workpath "%WORK%" --specpath "%WORK%" "%~dp0vcam_ui.py"
set "RC=%ERRORLEVEL%"

rem ---- remove the work files ----
if exist "%WORK%" rmdir /s /q "%WORK%"

if not "%RC%"=="0" goto :fail

echo.
echo Done: %OUT%\vcam\vcam.exe
echo Ship the whole "%OUT%\vcam" folder.
goto :end

:fail
echo.
echo Build failed.
set "RC=1"

:end
echo %cmdcmdline% | find /i "%~0" >nul && pause
exit /b %RC%
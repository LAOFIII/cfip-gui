@echo off
chcp 65001 >nul
REM 一键打包 Windows exe（需先：py -m pip install pyinstaller）
REM 产物：dist\cfip-gui\cfip-gui.exe —— 分发时整个 dist\cfip-gui 文件夹一起打包

cd /d "%~dp0"

echo [1/2] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/2] 开始打包...
py -m PyInstaller --noconfirm --clean --onedir --windowed ^
    --name cfip-gui ^
    --distpath dist --workpath build --specpath build ^
    cfip_gui.py

if errorlevel 1 (
    echo.
    echo 打包失败。请确认已安装 PyInstaller：py -m pip install pyinstaller
    pause
    exit /b 1
)

echo.
echo 打包完成：dist\cfip-gui\cfip-gui.exe
echo 分发时请把 dist\cfip-gui 整个文件夹压缩成 zip。
pause

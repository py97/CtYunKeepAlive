# -*- coding: utf-8 -*-
"""
在线更新模块 - 支持增量包更新
检查 version.json，下载增量 zip 包，自动解压覆盖
"""

import os
import sys
import json
import zipfile
import tempfile
import requests
from PySide6.QtCore import QThread, Signal, QObject, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPushButton,
    QProgressBar, QMessageBox, QTextEdit, QHBoxLayout
)
from PySide6.QtCore import Qt


UPDATE_URL = "http://aisa.cloud/version.json"


class UpdateChecker(QThread):
    """检查远程版本"""
    update_available = Signal(str, str, str, bool)  # version, changelog, url, mandatory
    error_occurred = Signal(str)
    up_to_date = Signal(str)  # 已是最新版本

    def __init__(self, current_version: str):
        super().__init__()
        self.current_version = current_version

    def _version_compare(self, v1: str, v2: str) -> int:
        """比较版本号，v1 > v2 返回 1"""
        try:
            n1 = [int(x) for x in v1.split(".")]
            n2 = [int(x) for x in v2.split(".")]
            n1 += [0] * (max(len(n1), len(n2)) - len(n1))
            n2 += [0] * (max(len(n1), len(n2)) - len(n2))
            for a, b in zip(n1, n2):
                if a > b:
                    return 1
                if a < b:
                    return -1
            return 0
        except Exception:
            return 0

    def run(self):
        try:
            resp = requests.get(UPDATE_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            latest = data.get("version", "")
            if not latest:
                self.up_to_date.emit(self.current_version)
                return
            cur = self.current_version.lstrip("vV")
            lat = latest.lstrip("vV")
            if self._version_compare(lat, cur) > 0:
                changelog = data.get("changelog", "无更新日志")
                url = data.get("url", "")
                mandatory = data.get("mandatory", False)
                self.update_available.emit(latest, changelog, url, mandatory)
            else:
                self.up_to_date.emit(self.current_version)
        except Exception as e:
            self.error_occurred.emit(f"检查更新失败: {e}")


class UpdateDialog(QDialog):
    """更新提示对话框"""

    def __init__(self, version: str, changelog: str, url: str,
                 mandatory: bool = False, parent=None):
        super().__init__(parent)
        self.url = url
        self.zip_path = None  # 下载后的本地路径
        self.setWindowTitle("发现新版本")
        self.setMinimumWidth(450)
        self.setModal(True)
        self._setup_ui(version, changelog, mandatory)

    def _setup_ui(self, version, changelog, mandatory):
        layout = QVBoxLayout(self)

        lbl_title = QLabel(f"发现新版本：<b>{version}</b>")
        lbl_title.setWordWrap(True)
        layout.addWidget(lbl_title)

        te = QTextEdit()
        te.setReadOnly(True)
        te.setMaximumHeight(150)
        te.setMarkdown(changelog)
        layout.addWidget(te)

        self.pbar = QProgressBar()
        self.pbar.setRange(0, 0)  # 先显示忙等待
        self.pbar.setVisible(False)
        layout.addWidget(self.pbar)

        self.lbl_status = QLabel("")
        self.lbl_status.setVisible(False)
        layout.addWidget(self.lbl_status)

        btn_layout = QHBoxLayout()
        self.btn_download = QPushButton("下载更新")
        self.btn_cancel = QPushButton("取消")
        btn_layout.addWidget(self.btn_download)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        if mandatory:
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setVisible(False)

        self.btn_download.clicked.connect(self._on_download)
        self.btn_cancel.clicked.connect(self.reject)

    def _on_download(self):
        self.btn_download.setEnabled(False)
        self.btn_download.setText("下载中...")
        self.pbar.setVisible(True)
        self.lbl_status.setVisible(True)
        self.lbl_status.setText("正在下载...")

        # 在新线程中下载
        self._downloader = _DownloadWorker(self.url)
        self._downloader.progress.connect(self.pbar.setValue)
        self._downloader.max_progress.connect(self.pbar.setMaximum)
        self._downloader.finished.connect(self._on_download_finished)
        self._downloader.error.connect(self._on_download_error)
        self._downloader.start()

    def _on_download_finished(self, save_path: str):
        self.zip_path = save_path

        # 验证下载的文件是否为有效 zip
        try:
            file_size = os.path.getsize(save_path)
            if file_size == 0:
                self.pbar.setVisible(False)
                self.lbl_status.setText("下载失败：文件大小为 0！")
                self.lbl_status.setStyleSheet("color:red;")
                self._reset_download_button()
                return
            with open(save_path, "rb") as f:
                if f.read(2) != b"PK":
                    f.seek(0)
                    preview = f.read(200)
                    self.pbar.setVisible(False)
                    self.lbl_status.setText(
                        f"下载的不是有效的 zip 文件（{file_size} 字节）。\n"
                        f"文件头预览: {preview[:80]}"
                    )
                    self.lbl_status.setStyleSheet("color:red;")
                    self.lbl_status.setWordWrap(True)
                    self._reset_download_button()
                    return
        except Exception as e:
            self.pbar.setVisible(False)
            self.lbl_status.setText(f"下载后验证失败: {e}")
            self.lbl_status.setStyleSheet("color:red;")
            self._reset_download_button()
            return

        self.pbar.setVisible(False)
        self.lbl_status.setText("下载完成！点击「安装更新」应用更新。")
        self.lbl_status.setVisible(True)

        # 把"下载更新"按钮改成"安装更新"
        self.btn_download.setText("安装更新")
        self.btn_download.clicked.disconnect()
        self.btn_download.clicked.connect(self._on_install)
        self.btn_download.setEnabled(True)

    def _reset_download_button(self):
        """重置下载按钮为初始状态"""
        self.btn_download.setText("重试")
        self.btn_download.clicked.disconnect()
        self.btn_download.clicked.connect(self._on_download)
        self.btn_download.setEnabled(True)

    def _on_download_error(self, msg: str):
        self.pbar.setVisible(False)
        self.lbl_status.setText(msg)
        self.lbl_status.setVisible(True)
        self.btn_download.setText("重试")
        self.btn_download.clicked.disconnect()
        self.btn_download.clicked.connect(self._on_download)
        self.btn_download.setEnabled(True)

    def _on_install(self):
        """安装更新：解压验证后调用独立 updater.exe 完成覆盖+重启"""
        if not self.zip_path or not os.path.exists(self.zip_path):
            QMessageBox.warning(self, "错误", "更新包文件不存在！")
            return

        # —— 步骤 1：验证文件完整性 ——
        import tempfile, shutil, subprocess
        file_size = os.path.getsize(self.zip_path)
        if file_size == 0:
            QMessageBox.critical(self, "错误",
                f"更新包文件大小为 0，下载可能未完成。\n路径: {self.zip_path}")
            return

        with open(self.zip_path, "rb") as f:
            magic = f.read(2)
        if magic != b"PK":
            with open(self.zip_path, "rb") as f:
                preview = f.read(200)
            QMessageBox.critical(self, "错误",
                f"更新包不是有效的 zip 文件！\n"
                f"文件大小: {file_size} 字节\n"
                f"文件头: {preview[:200]}")
            return

        # —— 步骤 2：在 Python 中解压到临时目录（修正中文文件名编码）——
        stage_dir = tempfile.mkdtemp(prefix="ctyun_update_")
        try:
            with zipfile.ZipFile(self.zip_path, "r") as zf:
                for item in zf.infolist():
                    # 如果没有 UTF-8 flag（bit 11），按 GBK 重新解码文件名
                    if not (item.flag_bits & 0x800):
                        try:
                            item.filename = item.filename.encode("cp437").decode("gbk")
                        except Exception:
                            pass
                    zf.extract(item, stage_dir)
        except zipfile.BadZipFile as e:
            QMessageBox.critical(self, "错误",
                f"解压更新包失败 (BadZipFile):\n{e}\n"
                f"文件大小: {file_size} 字节\n路径: {self.zip_path}")
            shutil.rmtree(stage_dir, ignore_errors=True)
            return
        except Exception as e:
            QMessageBox.critical(self, "错误",
                f"解压更新包失败 ({type(e).__name__}):\n{e}\n"
                f"文件大小: {file_size} 字节\n路径: {self.zip_path}")
            shutil.rmtree(stage_dir, ignore_errors=True)
            return

        # —— 步骤 3：定位 updater.exe ——
        if getattr(sys, "frozen", False):
            app_dir = os.path.dirname(sys.executable)
            app_exe = os.path.join(app_dir, "CtYunKeepAlive.exe")
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))
            app_exe = os.path.join(app_dir, "CtYunKeepAlive.exe")

        updater_exe = os.path.join(app_dir, "updater.exe")
        if not os.path.exists(updater_exe):
            QMessageBox.critical(self, "错误",
                f"找不到升级器 updater.exe！\n路径: {updater_exe}\n"
                f"请重新下载完整安装包。")
            shutil.rmtree(stage_dir, ignore_errors=True)
            return

        # —— 步骤 4：设置主窗口强制退出标记 ——
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            for w in app.topLevelWidgets():
                if hasattr(w, '_force_quit_for_update'):
                    w._force_quit_for_update = True
                    break

        QMessageBox.information(
            self, "准备更新",
            "程序即将关闭以应用更新。\n\n"
            "更新完成后程序会自动重启。\n"
            "如果未自动重启，请手动启动 CtYunKeepAlive.exe"
        )
        self.accept()

        # —— 步骤 5：启动 updater.exe（独立进程，无黑框）——
        subprocess.Popen(
            [updater_exe, stage_dir, app_dir, app_exe, self.zip_path],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        QTimer.singleShot(1500, app.quit)


class _DownloadWorker(QThread):
    """下载工作线程"""
    progress = Signal(int)
    max_progress = Signal(int)
    finished = Signal(str)  # 保存路径
    error = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            resp = requests.get(self.url, stream=True, timeout=30)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            self.max_progress.emit(total if total else 0)

            # 保存到临时目录
            tmp_dir = tempfile.gettempdir()
            save_path = os.path.join(tmp_dir, "CtYunKeepAlive_update.zip")

            downloaded = 0
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            self.progress.emit(downloaded)

            if not total:
                self.progress.emit(100)
            self.finished.emit(save_path)
        except Exception as e:
            self.error.emit(f"下载失败: {e}")


class UpdateInstaller:
    """更新安装器 - 生成批处理脚本"""

    @staticmethod
    def create_update_bat(stage_dir: str, zip_path: str) -> str:
        """
        生成更新批处理脚本：
        1. 等待程序退出
        2. 用 xcopy 覆盖文件（已在 Python 中解压到 stage_dir）
        3. 清理临时文件
        4. 重启程序
        """
        try:
            bat_dir = tempfile.gettempdir()
            bat_path = os.path.join(bat_dir, "ctyun_update.bat")

            if getattr(sys, "frozen", False):
                app_dir = os.path.dirname(sys.executable)
                app_exe = sys.executable
            else:
                app_dir = os.path.dirname(os.path.abspath(__file__))
                app_exe = os.path.join(app_dir, "ctyun_gui.py")

            # 路径中的反斜杠在 .bat 中需要转义，用短路径或正斜杠
            app_dir_safe = app_dir.replace("\\", "/")
            stage_dir_safe = stage_dir.replace("\\", "/")
            zip_path_safe = zip_path.replace("\\", "/")

            bat_content = f"""@echo off
chcp 65001 > nul
title CtYunKeepAlive 更新

echo 等待程序退出...
:WAIT
timeout /t 1 /nobreak > nul
tasklist /FI "IMAGENAME eq CtYunKeepAlive.exe" 2>nul | find /I "CtYunKeepAlive.exe" > nul 2>&1
if not errorlevel 1 goto WAIT

echo 正在安装更新...
xcopy "{stage_dir_safe}\\*" "{app_dir_safe}\\" /E /Y /Q /H /R

if errorlevel 1 (
    echo 安装失败！请手动解压更新包。
    echo 更新包位置: {zip_path_safe}
    timeout /t 5 /nobreak > nul
    exit /b 1
)

echo 清理临时文件...
rmdir /S /Q "{stage_dir_safe}" 2>nul
del "{zip_path_safe}" 2>nul

echo 更新完成，正在重启...
start "" "{app_exe}"

del "%~f0" 2>nul
"""
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(bat_content)

            return bat_path
        except Exception as e:
            print(f"创建更新批处理失败: {e}")
            return ""

    @staticmethod
    def apply_update(zip_path: str) -> bool:
        """直接解压增量包（供批处理调用）"""
        import zipfile
        try:
            base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                else os.path.dirname(os.path.abspath(__file__))
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(base)
            return True
        except Exception as e:
            print(f"应用更新失败: {e}")
            return False

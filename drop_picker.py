import argparse
import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}


def normalize_paths(urls):
    paths = []
    seen = set()
    for url in urls:
        local_path = url.toLocalFile()
        if not local_path:
            continue
        path = str(Path(local_path))
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


class DropWindow(QWidget):
    def __init__(self, mode, title, allow_multiple):
        super().__init__()
        self.mode = mode
        self.allow_multiple = allow_multiple
        self.result = {"ok": False, "cancelled": True}
        self.setWindowTitle(title)
        self.setAcceptDrops(True)
        self.setMinimumSize(460, 280)
        self.setStyleSheet(
            """
            QWidget { background: #f7f8fb; color: #111827; font-size: 14px; }
            QLabel#dropArea {
                background: white;
                border: 2px dashed #93c5fd;
                border-radius: 14px;
                padding: 28px;
            }
            QPushButton {
                background: white;
                border: 1px solid #d1d5db;
                border-radius: 8px;
                padding: 8px 16px;
            }
            QPushButton:hover { background: #f3f4f6; }
            """
        )
        self.build_ui(title)

    def build_ui(self, title):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title_label)

        self.hint_label = QLabel(self.build_hint_text())
        self.hint_label.setObjectName("dropArea")
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.handle_cancel)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row)

    def build_hint_text(self):
        if self.mode == "directory":
            return "把 1 个文件夹拖到这里"
        if self.mode == "video_files":
            return "把一个或多个视频文件拖到这里" if self.allow_multiple else "把 1 个视频文件拖到这里"
        if self.mode == "audio_file":
            return "把 1 个音频文件拖到这里"
        if self.mode == "png_file":
            return "把 1 个 PNG 图片拖到这里"
        return "把文件拖到这里"

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        paths = normalize_paths(event.mimeData().urls())
        ok, payload_or_message = self.validate_paths(paths)
        if not ok:
            QMessageBox.warning(self, "提示", payload_or_message)
            return
        self.result = payload_or_message
        self.close()

    def validate_paths(self, paths):
        if not paths:
            return False, "未识别到可用路径。"
        if self.mode == "directory":
            if len(paths) != 1 or not Path(paths[0]).is_dir():
                return False, "这里需要拖入 1 个文件夹。"
            return True, {"ok": True, "path": paths[0]}
        if self.mode == "video_files":
            if not self.allow_multiple and len(paths) != 1:
                return False, "这里需要拖入 1 个视频文件。"
            invalid = [path for path in paths if not Path(path).is_file() or Path(path).suffix.lower() not in VIDEO_EXTENSIONS]
            if invalid:
                return False, "这里只接受视频文件。"
            return True, {"ok": True, "paths": paths}
        if self.mode == "audio_file":
            if len(paths) != 1:
                return False, "这里需要拖入 1 个音频文件。"
            path_obj = Path(paths[0])
            if not path_obj.is_file() or path_obj.suffix.lower() not in AUDIO_EXTENSIONS:
                return False, "这里只接受音频文件。"
            return True, {"ok": True, "path": paths[0]}
        if self.mode == "png_file":
            if len(paths) != 1:
                return False, "这里需要拖入 1 个 PNG 文件。"
            path_obj = Path(paths[0])
            if not path_obj.is_file() or path_obj.suffix.lower() != ".png":
                return False, "这里只接受 PNG 图片。"
            return True, {"ok": True, "path": paths[0]}
        return False, "未知拖拽模式。"

    def handle_cancel(self):
        self.result = {"ok": False, "cancelled": True}
        self.close()

    def closeEvent(self, event):
        print(json.dumps(self.result, ensure_ascii=False), flush=True)
        super().closeEvent(event)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["directory", "video_files", "audio_file", "png_file"])
    parser.add_argument("--title", required=True)
    parser.add_argument("--multiple", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    app = QApplication(sys.argv)
    window = DropWindow(args.mode, args.title, args.multiple)
    window.show()
    app.exec()


if __name__ == "__main__":
    main()

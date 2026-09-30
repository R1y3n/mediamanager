import sys
import os
import sqlite3
import subprocess
from pathlib import Path
from io import BytesIO

from PySide6.QtWidgets import (QApplication, QMainWindow, QLabel,
                               QStackedWidget, QFileDialog)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QPixmap, QImage, QKeyEvent
from PIL import Image

# --- Configuration ---
DB_NAME = "media_culler.db"
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tga', '.jxl', '.bmp', '.gif', '.webp'}
VIDEO_EXTS = {'.mp4', '.avi', '.mkv', '.mov', '.webm', '.flv', '.wmv'}
ALL_EXTS = IMAGE_EXTS.union(VIDEO_EXTS)
PLAYBACK_RATES = [0.5, 1.0, 2.0, 5.0, 10.0, 20.0]

# Try loading JXL plugin for Pillow (optional, we have djxl fallback)
try:
    import pillow_jxl  # noqa: F401
except ImportError:
    pass


class MediaDB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_NAME)
        self.c = self.conn.cursor()
        self.c.execute("""CREATE TABLE IF NOT EXISTS media
                          (path TEXT PRIMARY KEY, viewed INTEGER DEFAULT 0,
                           favorite INTEGER DEFAULT 0)""")
        self.conn.commit()

    def get_viewed_paths(self):
        self.c.execute("SELECT path FROM media WHERE viewed = 1")
        return set(row[0] for row in self.c.fetchall())

    def mark_viewed(self, path):
        self.c.execute(
            "INSERT INTO media (path, viewed) VALUES (?, 1) "
            "ON CONFLICT(path) DO UPDATE SET viewed = 1", (path,))
        self.conn.commit()

    def mark_favorite(self, path):
        self.c.execute(
            "INSERT INTO media (path, viewed, favorite) VALUES (?, 1, 1) "
            "ON CONFLICT(path) DO UPDATE SET viewed = 1, favorite = 1", (path,))
        self.conn.commit()

    def close(self):
        self.conn.close()


class MediaCuller(QMainWindow):
    def __init__(self, root_path):
        super().__init__()
        self.setWindowTitle("Media Culler")
        self.resize(1280, 720)

        self.db = MediaDB()
        self.root_path = root_path
        self.media_list = self.scan_media()
        self.current_index = 0
        self.rate_index = 1
        self.load_failed = False  # Track if current media failed to load

        if not self.media_list:
            print("No new unviewed media found. Exiting.")
            sys.exit(0)

        # --- GUI ---
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # Page 0: Image
        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setStyleSheet("background-color: black;")
        self.stack.addWidget(self.img_label)

        # Page 1: Video
        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background-color: black;")
        self.stack.addWidget(self.video_widget)

        self.player = QMediaPlayer()
        self.audio_out = QAudioOutput()
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video_widget)
        self.player.errorOccurred.connect(self.on_media_error)

        # --- Counter overlay (top-left) ---
        self.counter = QLabel(self)
        self.counter.setStyleSheet(
            "color: #0f0; background: rgba(0,0,0,200); "
            "padding: 6px 14px; font-size: 20px; font-weight: bold; "
            "font-family: monospace; border-radius: 6px;"
        )
        self.counter.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.counter.move(16, 16)

        # --- Speed overlay (top-right, shown only for videos) ---
        self.speed_label = QLabel(self)
        self.speed_label.setStyleSheet(
            "color: #ff0; background: rgba(0,0,0,200); "
            "padding: 6px 14px; font-size: 20px; font-weight: bold; "
            "font-family: monospace; border-radius: 6px;"
        )
        self.speed_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.speed_label.hide()

        # --- Favorite indicator ---
        self.fav_label = QLabel("★ FAVORITE", self)
        self.fav_label.setStyleSheet(
            "color: #ffd700; background: rgba(0,0,0,200); "
            "padding: 6px 14px; font-size: 20px; font-weight: bold; "
            "font-family: monospace; border-radius: 6px;"
        )
        self.fav_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.fav_label.hide()

        self.showFullScreen()
        self.load_current_media()

    # ---- DB / Scan ----

    def scan_media(self):
        viewed = self.db.get_viewed_paths()
        files = []
        for root, _dirs, filenames in os.walk(self.root_path):
            for f in filenames:
                if f.lower().endswith(tuple(ALL_EXTS)):
                    full = os.path.join(root, f)
                    if full not in viewed:
                        files.append(full)
        files.sort()
        return files

    # ---- Loading ----

    def load_current_media(self):
        if self.current_index >= len(self.media_list):
            print("Finished reviewing all new media!")
            self.close()
            return
        if self.current_index < 0:
            self.current_index = 0

        self.load_failed = False
        self.fav_label.hide()
        self.speed_label.hide()

        # Update counter
        self.counter.setText(f" {self.current_index + 1} / {len(self.media_list)} ")
        self.counter.raise_()

        path = self.media_list[self.current_index]
        ext = Path(path).suffix.lower()

        self.player.stop()

        if ext in IMAGE_EXTS:
            self.stack.setCurrentIndex(0)
            self.load_image(path)
        else:
            self.stack.setCurrentIndex(1)
            self.load_video(path)

    def load_image(self, path):
        img = None
        try:
            # Strategy 1: Pillow (handles jpg, png, tga, bmp, gif, webp)
            img = Image.open(path)
        except Exception:
            pass

        # Strategy 2: djxl for .jxl files (most reliable on Linux)
        if img is None and path.lower().endswith('.jxl'):
            try:
                result = subprocess.run(
                    ['djxl', path, '-'],
                    capture_output=True, timeout=30
                )
                if result.returncode == 0 and result.stdout:
                    img = Image.open(BytesIO(result.stdout))
            except Exception as e:
                print(f"djxl fallback failed: {e}")

        # Strategy 3: imageio as last resort
        if img is None:
            try:
                import imageio.v3 as iio
                arr = iio.imread(path)
                img = Image.fromarray(arr)
            except Exception:
                pass

        if img is None:
            self.load_failed = True
            self.img_label.setText(f"⚠ Could not load:\n{path}")
            self.img_label.setStyleSheet(
                "background-color: black; color: red; font-size: 22px;")
            print(f"FAILED to load: {path}")
            return

        # Convert and display
        try:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            w, h = self.width(), self.height()
            img.thumbnail((w, h), Image.Resampling.LANCZOS)

            if img.mode == "RGB":
                fmt = QImage.Format_RGB888
                bpl = 3 * img.width
            else:
                fmt = QImage.Format_Grayscale8
                bpl = img.width

            qimg = QImage(img.tobytes(), img.width, img.height, bpl, fmt)
            self.img_label.setPixmap(QPixmap.fromImage(qimg))
            self.img_label.setStyleSheet("background-color: black;")
        except Exception as e:
            self.load_failed = True
            self.img_label.setText(f"⚠ Display error:\n{e}")
            print(f"Display error for {path}: {e}")

    def load_video(self, path):
        print(f"Playing video: {path}")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.setPlaybackRate(PLAYBACK_RATES[self.rate_index])
        self.player.play()
        self.speed_label.setText(f" {PLAYBACK_RATES[self.rate_index]}x ")
        self.speed_label.adjustSize()
        self.speed_label.move(self.width() - self.speed_label.width() - 16, 16)
        self.speed_label.show()
        self.speed_label.raise_()

    def on_media_error(self, error):
        print(f"!!! PLAYER ERROR: {self.player.errorString()} !!!")
        self.load_failed = True

    # ---- Navigation ----

    def mark_and_navigate(self, direction):
        # Only mark as viewed if it loaded successfully
        if not self.load_failed:
            current_path = self.media_list[self.current_index]
            self.db.mark_viewed(current_path)
        else:
            print(f"Skipping mark (load failed): {self.media_list[self.current_index]}")

        self.current_index += direction
        self.current_index = max(0, min(self.current_index, len(self.media_list)))
        self.load_current_media()

    # ---- Key Handling ----

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        mods = event.modifiers()
        is_shift = bool(mods & Qt.ShiftModifier)
        ext = Path(self.media_list[self.current_index]).suffix.lower()
        is_video = ext in VIDEO_EXTS

        if key == Qt.Key_Right:
            if is_shift and is_video:
                self.player.setPosition(self.player.position() + 10000)
            else:
                self.mark_and_navigate(1)

        elif key == Qt.Key_Left:
            if is_shift and is_video:
                self.player.setPosition(max(0, self.player.position() - 10000))
            else:
                self.mark_and_navigate(-1)

        elif key == Qt.Key_Exclam:  # Shift+1 = '!'
            path = self.media_list[self.current_index]
            self.db.mark_favorite(path)
            print(f"★ FAVORITE: {path}")
            # Show brief favorite indicator
            self.fav_label.move(self.width() // 2 - 80, self.height() - 60)
            self.fav_label.show()
            self.fav_label.raise_()
            QTimer.singleShot(1500, self.fav_label.hide)
            # Mark viewed + advance
            self.db.mark_viewed(path)
            self.load_failed = False  # Force mark since user explicitly favorited
            self.current_index += 1
            self.current_index = min(self.current_index, len(self.media_list))
            self.load_current_media()

        elif key == Qt.Key_X:
            if is_video:
                self.rate_index = min(self.rate_index + 1, len(PLAYBACK_RATES) - 1)
                rate = PLAYBACK_RATES[self.rate_index]
                self.player.setPlaybackRate(rate)
                self.speed_label.setText(f" {rate}x ")
                self.speed_label.adjustSize()
                self.speed_label.move(self.width() - self.speed_label.width() - 16, 16)
                print(f"Speed: {rate}x")

        elif key == Qt.Key_Z:
            if is_video:
                self.rate_index = max(self.rate_index - 1, 0)
                rate = PLAYBACK_RATES[self.rate_index]
                self.player.setPlaybackRate(rate)
                self.speed_label.setText(f" {rate}x ")
                self.speed_label.adjustSize()
                self.speed_label.move(self.width() - self.speed_label.width() - 16, 16)
                print(f"Speed: {rate}x")

        elif key == Qt.Key_Escape:
            if not self.load_failed:
                self.db.mark_viewed(self.media_list[self.current_index])
            self.close()

        elif key == Qt.Key_Space:
            if is_video:
                if self.player.playbackState() == QMediaPlayer.PlayingState:
                    self.player.pause()
                else:
                    self.player.play()

        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep overlays positioned correctly on resize
        self.speed_label.move(self.width() - self.speed_label.width() - 16, 16)
        self.fav_label.move(self.width() // 2 - 80, self.height() - 60)

    def closeEvent(self, event):
        self.player.stop()
        self.db.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    if len(sys.argv) > 1:
        root_path = sys.argv[1]
    else:
        root_path = QFileDialog.getExistingDirectory(None, "Select Root Media Folder")

    if not root_path or not os.path.exists(root_path):
        print("Invalid path. Exiting.")
        sys.exit(1)

    viewer = MediaCuller(root_path)
    viewer.show()
    sys.exit(app.exec())

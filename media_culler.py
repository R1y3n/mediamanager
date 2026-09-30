import sys
import os
import sqlite3
from pathlib import Path

from PySide6.QtWidgets import (QApplication, QMainWindow, QLabel, 
                               QVBoxLayout, QWidget, QStackedWidget, QFileDialog)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap, QImage, QKeyEvent
from PIL import Image

# --- Configuration ---
DB_NAME = "media_culler.db"
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tga', '.jxl', '.bmp', '.gif', '.webp'}
VIDEO_EXTS = {'.mp4', '.avi', '.mkv', '.mov', '.webm', '.flv', '.wmv'}
ALL_EXTS = IMAGE_EXTS.union(VIDEO_EXTS)
PLAYBACK_RATES = [0.5, 1.0, 2.0, 5.0, 10.0, 20.0]

class MediaDB:
    def __init__(self):
        self.conn = sqlite3.connect(DB_NAME)
        self.c = self.conn.cursor()
        self.c.execute("""CREATE TABLE IF NOT EXISTS media 
                          (path TEXT PRIMARY KEY, viewed INTEGER DEFAULT 0, favorite INTEGER DEFAULT 0)""")
        self.conn.commit()

    def get_viewed_paths(self):
        self.c.execute("SELECT path FROM media WHERE viewed = 1")
        return set(row[0] for row in self.c.fetchall())

    def mark_viewed(self, path):
        self.c.execute("INSERT INTO media (path, viewed) VALUES (?, 1) "
                       "ON CONFLICT(path) DO UPDATE SET viewed = 1", (path,))
        self.conn.commit()

    def mark_favorite(self, path):
        self.c.execute("INSERT INTO media (path, viewed, favorite) VALUES (?, 1, 1) "
                       "ON CONFLICT(path) DO UPDATE SET viewed = 1, favorite = 1", (path,))
        self.conn.commit()

    def close(self):
        self.conn.close()

class MediaCuller(QMainWindow):
    def __init__(self, root_path):
        super().__init__()
        self.setWindowTitle("Media Culler - Minimal Viewer")
        self.resize(1280, 720)
        
        self.db = MediaDB()
        self.root_path = root_path
        self.media_list = self.scan_media()
        self.current_index = 0
        self.rate_index = 1  
        
        if not self.media_list:
            print("No new unviewed media found. Exiting.")
            sys.exit(0)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        
        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setStyleSheet("background-color: black;")
        self.stack.addWidget(self.img_label)
        
        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background-color: black;")
        self.stack.addWidget(self.video_widget)
        
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        
        # --- ADDED: Error logging for video playback ---
        self.player.errorOccurred.connect(self.on_media_error)
        
        self.showFullScreen()
        self.load_current_media()

    def on_media_error(self, error):
        # This will print the exact GStreamer/Qt error to the terminal if video fails
        print(f"!!! MEDIA PLAYER ERROR: {self.player.errorString()} !!!")

    def scan_media(self):
        viewed = self.db.get_viewed_paths()
        files = []
        for root, dirs, filenames in os.walk(self.root_path):
            for f in filenames:
                if f.lower().endswith(tuple(ALL_EXTS)):
                    full_path = os.path.join(root, f)
                    if full_path not in viewed:
                        files.append(full_path)
        files.sort()
        return files

    def load_current_media(self):
        if self.current_index >= len(self.media_list):
            print("Finished reviewing all new media!")
            self.close()
            return

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
        try:
            img = Image.open(path)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            
            w, h = self.width(), self.height()
            img.thumbnail((w, h), Image.Resampling.LANCZOS)
            
            if img.mode == "RGB":
                fmt = QImage.Format_RGB888
                bytes_per_line = 3 * img.width
            else:
                fmt = QImage.Format_Grayscale8
                bytes_per_line = img.width
                
            q_img = QImage(img.tobytes(), img.width, img.height, bytes_per_line, fmt)
            pixmap = QPixmap.fromImage(q_img)
            self.img_label.setPixmap(pixmap)
        except Exception as e:
            print(f"Error loading image {path}: {e}")
            self.img_label.setText(f"Error loading image")

    def load_video(self, path):
        print(f"Attempting to play video: {path}")
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.setPlaybackRate(PLAYBACK_RATES[self.rate_index])
        self.player.play()

    def mark_and_navigate(self, direction):
        current_path = self.media_list[self.current_index]
        self.db.mark_viewed(current_path)
        
        self.current_index += direction
        self.current_index = max(0, min(self.current_index, len(self.media_list)))
        self.load_current_media()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        modifiers = event.modifiers()
        is_shift = modifiers & Qt.ShiftModifier
        current_path = self.media_list[self.current_index]
        ext = Path(current_path).suffix.lower()
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
                
        elif key == Qt.Key_Exclam: 
            self.db.mark_favorite(current_path)
            print(f"Marked as Favorite: {current_path}")
            self.mark_and_navigate(1)
            
        elif key == Qt.Key_X:
            if is_video:
                self.rate_index = min(self.rate_index + 1, len(PLAYBACK_RATES) - 1)
                self.player.setPlaybackRate(PLAYBACK_RATES[self.rate_index])
                print(f"Speed: {PLAYBACK_RATES[self.rate_index]}x")
                
        elif key == Qt.Key_Z:
            if is_video:
                self.rate_index = max(self.rate_index - 1, 0)
                self.player.setPlaybackRate(PLAYBACK_RATES[self.rate_index])
                print(f"Speed: {PLAYBACK_RATES[self.rate_index]}x")
                
        elif key == Qt.Key_Escape:
            self.mark_and_navigate(0) 
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.player.stop()
        self.db.close()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    if len(sys.argv) > 1:
        root_path = sys.argv[1]
    else:
        print("No path provided. Please select a root folder.")
        root_path = QFileDialog.getExistingDirectory(None, "Select Root Media Folder")
        
    if not root_path or not os.path.exists(root_path):
        print("Invalid path. Exiting.")
        sys.exit(1)

    viewer = MediaCuller(root_path)
    viewer.show()
    sys.exit(app.exec())

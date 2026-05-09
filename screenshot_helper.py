import os
import sys
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import ImageGrab
except ImportError:
    print("Missing dependency: pillow. Install with: pip install pillow")
    sys.exit(1)


class ScreenshotHelperApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Win 截屏辅助工具")
        self.root.geometry("420x260")
        self.root.resizable(False, False)

        self.save_dir = Path.home() / "Pictures" / "ShotHelper"
        self.save_dir.mkdir(parents=True, exist_ok=True)

        self.delay_var = tk.IntVar(value=0)
        self.status_var = tk.StringVar(value="就绪")

        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self.root, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="保存目录:").grid(row=0, column=0, sticky="w")
        self.path_entry = ttk.Entry(frm, width=40)
        self.path_entry.insert(0, str(self.save_dir))
        self.path_entry.grid(row=1, column=0, columnspan=3, pady=(4, 12), sticky="we")

        ttk.Label(frm, text="延迟秒数:").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(frm, from_=0, to=30, textvariable=self.delay_var, width=8).grid(row=2, column=1, sticky="w")

        ttk.Button(frm, text="全屏截图", command=self.capture_fullscreen).grid(row=3, column=0, pady=14, sticky="we")
        ttk.Button(frm, text="延迟截图", command=self.capture_with_delay).grid(row=3, column=1, pady=14, sticky="we")
        ttk.Button(frm, text="打开目录", command=self.open_dir).grid(row=3, column=2, pady=14, sticky="we")

        ttk.Separator(frm).grid(row=4, column=0, columnspan=3, sticky="we", pady=(8, 12))
        ttk.Label(frm, textvariable=self.status_var, foreground="#0b5").grid(row=5, column=0, columnspan=3, sticky="w")

        frm.columnconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(2, weight=1)

    def _target_dir(self) -> Path:
        entered = self.path_entry.get().strip()
        target = Path(entered) if entered else self.save_dir
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _save_shot(self, image):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._target_dir() / f"shot_{ts}.png"
        image.save(path, "PNG")
        self.status_var.set(f"已保存: {path}")

    def capture_fullscreen(self):
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)
        image = ImageGrab.grab(all_screens=True)
        self._save_shot(image)
        self.root.deiconify()

    def capture_with_delay(self):
        delay = max(self.delay_var.get(), 0)
        self.status_var.set(f"将在 {delay} 秒后截图...")
        self.root.update()
        self.root.withdraw()
        for i in range(delay, 0, -1):
            self.status_var.set(f"倒计时: {i} 秒")
            self.root.update()
            time.sleep(1)
        image = ImageGrab.grab(all_screens=True)
        self._save_shot(image)
        self.root.deiconify()

    def open_dir(self):
        target = self._target_dir()
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("提示", f"当前不是 Windows 平台，目录路径:\n{target}")


def main():
    root = tk.Tk()
    app = ScreenshotHelperApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

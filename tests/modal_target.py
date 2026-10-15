"""
modal_target.py - tiny Win32 app used by the smoke test: a window that opens a MODAL popup.

Run with pythonw.exe (no console). After --popup-after milliseconds it calls the native
MessageBox with its own window as owner - the same thing most desktop applications do for
their dialogs - and blocks until the popup is closed.
"""
import argparse
import tkinter as tk
from tkinter import messagebox

parser = argparse.ArgumentParser()
parser.add_argument("--title", default="Modal Target")
parser.add_argument("--popup-after", type=int, default=3000)
args = parser.parse_args()

root = tk.Tk()
root.title(args.title)
root.geometry("640x420")
tk.Label(root, text="Application under test (modal popup demo)", font=("Segoe UI", 14)).pack(expand=True)
root.after(args.popup_after, lambda: messagebox.showinfo("Popup", "This is a modal dialog.", parent=root))
root.mainloop()

"""
femos-client/main.py — macOS entrypoint
"""
import tkinter as tk
from core.client_core import ClientCore
from ui.macos_app import App


def main():
    root = tk.Tk()
    core = ClientCore()
    App(root, core)
    core.start()
    root.mainloop()


if __name__ == "__main__":
    main()

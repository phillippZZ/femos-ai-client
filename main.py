"""
femos-client/main.py — macOS entrypoint
"""
import logging
import tkinter as tk
from core.client_core import ClientCore
from ui.macos_app import App


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    root = tk.Tk()
    core = ClientCore()
    App(root, core)
    core.start()
    root.mainloop()


if __name__ == "__main__":
    main()

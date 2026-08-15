"""Settings dialog (tkinter). Edits settings.json and the credential store."""
from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import asdict
from tkinter import messagebox, ttk

from src.version import APP_NAME

log = logging.getLogger(__name__)


def show_settings_dialog(settings, on_save=None) -> bool:
    """Show the settings window. Returns True if the user saved.

    `on_save(updated_settings)` is called after a successful save (used by
    the main app to re-register hotkeys etc.).
    """
    from src.config import save_settings, set_api_key, get_api_key

    root = tk.Tk()
    root.title(f"{APP_NAME} — Settings")
    root.resizable(False, False)

    frm = ttk.Frame(root, padding=16)
    frm.grid(row=0, column=0, sticky="nsew")
    frm.columnconfigure(1, weight=1)

    hotkey_var = tk.StringVar(value=settings.hotkey)
    mode_var = tk.StringVar(value=settings.mode)
    lang_var = tk.StringVar(value=settings.language or "")
    glossary_var = tk.StringVar(value=", ".join(settings.glossary))
    cleanup_var = tk.BooleanVar(value=bool(settings.cleanup_model))
    autostart_var = tk.BooleanVar(value=settings.autostart)
    key_var = tk.StringVar(value=get_api_key())
    key_shown = tk.BooleanVar(value=False)

    def field(row, label, widget):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
        widget.grid(row=row, column=1, sticky="ew", pady=3)

    row = 0
    field(row, "Hotkey (e.g. ctrl+space)",
          ttk.Entry(frm, textvariable=hotkey_var, width=30))
    row += 1

    mode_box = ttk.Combobox(frm, textvariable=mode_var, state="readonly",
                            values=("hold", "tap"), width=28)
    field(row, "Mode", mode_box)
    ttk.Label(frm, text="hold = press-hold-release · tap = toggle on/off").grid(
        row=row, column=0, columnspan=2, sticky="w", padx=(8, 0))
    row += 1

    field(row, "Language (blank = auto-detect)",
          ttk.Entry(frm, textvariable=lang_var, width=30))
    row += 1

    field(row, "Custom words (comma-separated)",
          ttk.Entry(frm, textvariable=glossary_var, width=30))
    ttk.Label(frm,
              text="Names/terms to keep verbatim (e.g. Razorpay, Lorem Ipsum)").grid(
        row=row, column=0, columnspan=2, sticky="w", padx=(8, 0))
    row += 1

    cleanup_chk = ttk.Checkbutton(frm, variable=cleanup_var)
    field(row, "AI cleanup (removes um's, fixes punctuation)", cleanup_chk)
    row += 1

    key_entry = ttk.Entry(frm, textvariable=key_var, width=30, show="*")
    field(row, "Groq API key (Windows Credential Locker)", key_entry)

    def _toggle_key():
        key_shown.set(not key_shown.get())
        key_entry.config(show="" if key_shown.get() else "*")

    show_chk = ttk.Checkbutton(frm, text="Show key", variable=key_shown,
                               command=_toggle_key)
    show_chk.grid(row=row, column=1, sticky="w", pady=(0, 4))
    row += 1

    autostart_chk = ttk.Checkbutton(frm, variable=autostart_var)
    field(row, "Start with Windows", autostart_chk)
    row += 1

    def _save():
        hotkey = hotkey_var.get().strip().lower()
        if not hotkey:
            messagebox.showerror("Settings", "Hotkey cannot be empty.")
            return
        key = key_var.get().strip()
        if key:
            try:
                set_api_key(key)
            except Exception as e:
                messagebox.showerror("Settings", f"Could not save API key: {e}")
                return
        updated = type(settings)(**{
            **asdict(settings),
            "hotkey": hotkey,
            "mode": mode_var.get(),
            "language": lang_var.get().strip() or None,
            "cleanup_model": "llama-3.3-70b-versatile" if cleanup_var.get() else None,
            "autostart": autostart_var.get(),
            "glossary": [t.strip() for t in glossary_var.get().split(",") if t.strip()],
        })
        save_settings(updated)
        if on_save:
            try:
                on_save(updated)
            except Exception as e:
                log.error("post-save hook failed: %s", e)
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(row=row, column=0, columnspan=2, pady=12)
    ttk.Button(btns, text="Cancel", command=root.destroy).pack(side="left", padx=6)
    ttk.Button(btns, text="Save", command=_save).pack(side="left", padx=6)

    root.mainloop()
    return True
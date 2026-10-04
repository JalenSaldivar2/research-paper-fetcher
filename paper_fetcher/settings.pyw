"""
Paper Search Settings
=====================

A small window for choosing what the daily paper fetcher searches for and how many
papers it saves each day. Everything is stored in config.json, which the scheduled
daily run (fetch_papers.py) reads.

Open it with the "Paper Search Settings" shortcut, or:  pythonw settings.pyw
"""

import json
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
WINDOW_TITLE = "Paper Search Settings"


def load_config():
    # utf-8-sig tolerates the invisible "BOM" marker some Windows editors add.
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


class SettingsWindow:
    """The settings window. Changes are kept in memory until Save is clicked."""

    def __init__(self, root):
        self.root = root
        self.config = load_config()
        root.title(WINDOW_TITLE)
        root.geometry("760x680")

        self.pad = {"padx": 10, "pady": 4}
        self._build_daily_settings()
        self._build_add_search()
        self._build_search_list()
        self._build_buttons()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_daily_settings(self):
        """Top section: papers per day, library links per day, library proxy."""
        counts = ttk.Frame(self.root)
        counts.pack(fill="x", **self.pad)

        ttk.Label(counts, text="Papers per day:", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.per_day = tk.IntVar(value=self.config.get("papers_per_day_max", 5))
        ttk.Spinbox(counts, from_=1, to=25, width=5, textvariable=self.per_day).pack(side="left", padx=6)

        ttk.Label(counts, text="School-login links per day:").pack(side="left", padx=(20, 0))
        self.library_per_day = tk.IntVar(value=self.config.get("school_links_per_day", 3))
        ttk.Spinbox(counts, from_=0, to=20, width=5, textvariable=self.library_per_day).pack(side="left", padx=6)

        proxy_row = ttk.Frame(self.root)
        proxy_row.pack(fill="x", **self.pad)
        ttk.Label(proxy_row, text="Library proxy link (optional, for paywalled papers):").pack(side="left")
        self.proxy = ttk.Entry(proxy_row, width=50)
        self.proxy.insert(0, self.config.get("school_proxy_prefix", ""))
        self.proxy.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Label(self.root, foreground="#666",
                  text="e.g. https://proxy.library.yourschool.edu/login?url=   (leave blank to skip paywalled papers)"
                  ).pack(anchor="w", padx=10)

    def _build_add_search(self):
        """Middle section: name + search terms for a new search."""
        box = ttk.LabelFrame(self.root, text="Add a new search")
        box.pack(fill="x", **self.pad)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Name (becomes the folder name):").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.name = ttk.Entry(box, width=40)
        self.name.grid(row=0, column=1, sticky="we", padx=6, pady=3)

        ttk.Label(box, text="What to search for\n(one search per line):").grid(row=1, column=0, sticky="nw", padx=6)
        self.queries = tk.Text(box, height=4, width=50, font=("Segoe UI", 10))
        self.queries.grid(row=1, column=1, sticky="we", padx=6, pady=3)

        self.only_new = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Only search this (turn off all other searches)",
                        variable=self.only_new).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Button(box, text="Add search", command=self.add_search).grid(row=3, column=1, sticky="e", padx=6, pady=4)

    def _build_search_list(self):
        """Scrollable list of existing searches, each with an on/off tick and Remove button."""
        box = ttk.LabelFrame(self.root, text="Your searches (ticked ones run each day)")
        box.pack(fill="both", expand=True, **self.pad)

        canvas = tk.Canvas(box, highlightthickness=0)
        scrollbar = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        self.rows = ttk.Frame(canvas)
        self.rows.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.rows, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.draw_search_list()

    def _build_buttons(self):
        """Bottom row: status message and Save buttons."""
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", **self.pad)
        ttk.Button(bar, text="Save", command=self.save).pack(side="right", padx=4)
        ttk.Button(bar, text="Save and find papers now", command=self.save_and_run).pack(side="right", padx=4)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left")

    def draw_search_list(self):
        """(Re)draw the list of searches from self.config."""
        for widget in self.rows.winfo_children():
            widget.destroy()
        self.enabled_vars = []
        for i, topic in enumerate(self.config["topics"]):
            enabled = tk.BooleanVar(value=topic.get("enabled", True))
            self.enabled_vars.append(enabled)
            ttk.Checkbutton(self.rows, text=topic["name"], variable=enabled).grid(row=i, column=0, sticky="w", padx=4)

            preview = "; ".join(topic.get("queries", []))
            if len(preview) > 70:
                preview = preview[:70] + "..."
            ttk.Label(self.rows, text=preview, foreground="#666").grid(row=i, column=1, sticky="w", padx=8)

            ttk.Button(self.rows, text="Remove", width=8,
                       command=lambda i=i: self.remove_search(i)).grid(row=i, column=2, padx=4, pady=1)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _read_ticks(self):
        """Copy the on/off ticks from the window into self.config."""
        for topic, enabled in zip(self.config["topics"], self.enabled_vars):
            topic["enabled"] = enabled.get()

    def add_search(self):
        """Add the typed search to the list (or extend an existing one with the same name)."""
        queries = [q.strip() for q in self.queries.get("1.0", "end").splitlines() if q.strip()]
        if not queries:
            messagebox.showwarning(WINDOW_TITLE, "Type at least one search.")
            return
        # The name becomes a folder, so drop characters Windows doesn't allow in folder names.
        name = re.sub(r'[<>:"/\\|?*]+', "", self.name.get()).strip() or queries[0][:40]

        self._read_ticks()
        if self.only_new.get():
            for topic in self.config["topics"]:
                topic["enabled"] = False

        existing = next((t for t in self.config["topics"] if t["name"].lower() == name.lower()), None)
        if existing:
            existing["queries"] = list(dict.fromkeys(existing["queries"] + queries))  # merge, no repeats
            existing["enabled"] = True
        else:
            # An empty relevance_terms list tells the fetcher to judge relevance by the
            # search's own words instead of the preset keyword list.
            self.config["topics"].insert(0, {"name": name, "weight": 3, "enabled": True,
                                             "relevance_terms": [], "queries": queries})

        self.name.delete(0, "end")
        self.queries.delete("1.0", "end")
        self.draw_search_list()
        self.status.config(text=f'Added "{name}". Click Save to keep it.')

    def remove_search(self, index):
        self._read_ticks()
        name = self.config["topics"][index]["name"]
        if messagebox.askyesno(WINDOW_TITLE, f'Remove the search "{name}"?\n(Papers already saved stay put.)'):
            del self.config["topics"][index]
            self.draw_search_list()

    def save(self):
        """Validate and write config.json. Returns True if saved."""
        self._read_ticks()
        try:
            per_day = int(self.per_day.get())
            library_per_day = int(self.library_per_day.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning(WINDOW_TITLE, "Papers per day must be a number.")
            return False
        if not any(t.get("enabled", True) for t in self.config["topics"]):
            messagebox.showwarning(WINDOW_TITLE, "Tick at least one search.")
            return False

        self.config["papers_per_day_min"] = self.config["papers_per_day_max"] = max(1, per_day)
        self.config["school_links_per_day"] = max(0, library_per_day)
        self.config["school_proxy_prefix"] = self.proxy.get().strip()
        save_config(self.config)
        self.status.config(text="Saved. The next scheduled run will use these settings.")
        return True

    def save_and_run(self):
        """Save, then run the fetcher right away in a console window."""
        if not self.save():
            return
        python = Path(sys.executable).with_name("python.exe")  # console Python, so output is visible
        # "cmd /k" keeps the window open after the run so the results can be read.
        subprocess.Popen(["cmd", "/k", str(python), str(HERE / "fetch_papers.py")], cwd=HERE,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.status.config(text="Searching now in a separate window. New PDFs land in the topic folders.")


if __name__ == "__main__":
    root = tk.Tk()
    SettingsWindow(root)
    root.mainloop()

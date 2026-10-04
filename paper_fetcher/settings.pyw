"""Paper Search Settings: a small window for choosing what the daily fetcher looks for
and how many papers it saves each day. Writes config.json; the 9 AM task reads it."""

import json
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"


def load():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def save(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load()
        root.title("Paper Search Settings")
        root.geometry("760x680")
        pad = {"padx": 10, "pady": 4}

        # --- papers per day ---
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Papers per day:", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.per_day = tk.IntVar(value=self.cfg.get("papers_per_day_max", 5))
        ttk.Spinbox(top, from_=1, to=25, width=5, textvariable=self.per_day).pack(side="left", padx=6)
        ttk.Label(top, text="School-login links per day:").pack(side="left", padx=(20, 0))
        self.school = tk.IntVar(value=self.cfg.get("school_links_per_day", 3))
        ttk.Spinbox(top, from_=0, to=20, width=5, textvariable=self.school).pack(side="left", padx=6)

        lib = ttk.Frame(root)
        lib.pack(fill="x", **pad)
        ttk.Label(lib, text="Library proxy link (optional, for paywalled papers):").pack(side="left")
        self.proxy = ttk.Entry(lib, width=50)
        self.proxy.insert(0, self.cfg.get("school_proxy_prefix", ""))
        self.proxy.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Label(root, text="e.g. https://proxy.library.yourschool.edu/login?url=  (leave blank to skip paywalled papers)",
                  foreground="#666").pack(anchor="w", padx=10)

        # --- add a search ---
        add = ttk.LabelFrame(root, text="Add a new search")
        add.pack(fill="x", **pad)
        ttk.Label(add, text="Name (becomes the folder name):").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.name = ttk.Entry(add, width=40)
        self.name.grid(row=0, column=1, sticky="we", padx=6, pady=3)
        ttk.Label(add, text="What to search for\n(one search per line):").grid(row=1, column=0, sticky="nw", padx=6)
        self.queries = tk.Text(add, height=4, width=50, font=("Segoe UI", 10))
        self.queries.grid(row=1, column=1, sticky="we", padx=6, pady=3)
        self.only_new = tk.BooleanVar(value=False)
        ttk.Checkbutton(add, text="Only search this (turn off all other searches)",
                        variable=self.only_new).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Button(add, text="Add search", command=self.add_topic).grid(row=3, column=1, sticky="e", padx=6, pady=4)
        add.columnconfigure(1, weight=1)

        # --- existing searches ---
        lst = ttk.LabelFrame(root, text="Your searches (ticked ones run each day)")
        lst.pack(fill="both", expand=True, **pad)
        canvas = tk.Canvas(lst, highlightthickness=0)
        sb = ttk.Scrollbar(lst, orient="vertical", command=canvas.yview)
        self.rows = ttk.Frame(canvas)
        self.rows.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.rows, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.draw_topics()

        # --- buttons ---
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="Save", command=self.save).pack(side="right", padx=4)
        ttk.Button(bottom, text="Save and find papers now", command=self.save_and_run).pack(side="right", padx=4)
        self.status = ttk.Label(bottom, text="")
        self.status.pack(side="left")

    def draw_topics(self):
        for w in self.rows.winfo_children():
            w.destroy()
        self.enabled_vars = []
        for i, t in enumerate(self.cfg["topics"]):
            var = tk.BooleanVar(value=t.get("enabled", True))
            self.enabled_vars.append(var)
            ttk.Checkbutton(self.rows, text=t["name"], variable=var).grid(row=i, column=0, sticky="w", padx=4)
            preview = "; ".join(t.get("queries", []))
            ttk.Label(self.rows, text=preview[:70] + ("..." if len(preview) > 70 else ""),
                      foreground="#666").grid(row=i, column=1, sticky="w", padx=8)
            ttk.Button(self.rows, text="Remove", width=8,
                       command=lambda i=i: self.remove_topic(i)).grid(row=i, column=2, padx=4, pady=1)

    def sync(self):
        for t, var in zip(self.cfg["topics"], self.enabled_vars):
            t["enabled"] = var.get()

    def add_topic(self):
        name = re.sub(r'[<>:"/\\|?*]+', "", self.name.get()).strip()
        queries = [q.strip() for q in self.queries.get("1.0", "end").splitlines() if q.strip()]
        if not queries:
            messagebox.showwarning("Paper Search Settings", "Type at least one search.")
            return
        name = name or queries[0][:40]
        self.sync()
        if self.only_new.get():
            for t in self.cfg["topics"]:
                t["enabled"] = False
        existing = next((t for t in self.cfg["topics"] if t["name"].lower() == name.lower()), None)
        if existing:
            existing["queries"] = list(dict.fromkeys(existing["queries"] + queries))
            existing["enabled"] = True
        else:
            # Custom searches use their own words to judge relevance, not the SiC keyword list.
            self.cfg["topics"].insert(0, {"name": name, "weight": 3, "enabled": True,
                                          "relevance_terms": [], "queries": queries})
        self.name.delete(0, "end")
        self.queries.delete("1.0", "end")
        self.draw_topics()
        self.status.config(text=f"Added \"{name}\". Click Save to keep it.")

    def remove_topic(self, i):
        self.sync()
        name = self.cfg["topics"][i]["name"]
        if messagebox.askyesno("Paper Search Settings", f"Remove the search \"{name}\"?\n(Papers already saved stay put.)"):
            del self.cfg["topics"][i]
            self.draw_topics()

    def save(self):
        self.sync()
        try:
            n, s = int(self.per_day.get()), int(self.school.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("Paper Search Settings", "Papers per day must be a number.")
            return False
        if not any(t.get("enabled", True) for t in self.cfg["topics"]):
            messagebox.showwarning("Paper Search Settings", "Tick at least one search.")
            return False
        self.cfg["papers_per_day_min"] = self.cfg["papers_per_day_max"] = max(1, n)
        self.cfg["school_links_per_day"] = max(0, s)
        self.cfg["school_proxy_prefix"] = self.proxy.get().strip()
        save(self.cfg)
        self.status.config(text="Saved. Tomorrow's 9 AM run will use these settings.")
        return True

    def save_and_run(self):
        if not self.save():
            return
        py = Path(sys.executable).with_name("python.exe")
        # cmd /k keeps the window open so the results stay readable after the run.
        subprocess.Popen(["cmd", "/k", str(py), str(HERE / "fetch_papers.py")], cwd=HERE,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.status.config(text="Searching now in a separate window. New PDFs land in the topic folders.")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

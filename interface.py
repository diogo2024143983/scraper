"""Pequena interface gráfica para experimentar pesquisas reais."""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from scraper import PriceScraper


class ScraperApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Pesquisa de preços")
        self.geometry("980x560")
        self.minsize(760, 420)
        self.scraper = PriceScraper(timeout=8, retries=1, use_browser=True)
        self._build_ui()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(frame)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Produto:").pack(side=tk.LEFT)
        self.query = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.query)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        entry.bind("<Return>", lambda _event: self.start_search())
        self.search_button = ttk.Button(controls, text="Pesquisar", command=self.start_search)
        self.search_button.pack(side=tk.RIGHT)

        options = ttk.Frame(frame)
        options.pack(fill=tk.X, pady=(8, 8))
        ttk.Label(options, text="Resultados com confiança mínima:").pack(side=tk.LEFT)
        self.minimum_score = tk.DoubleVar(value=0.25)
        ttk.Spinbox(
            options,
            from_=0.0,
            to=1.0,
            increment=0.05,
            textvariable=self.minimum_score,
            width=6,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.status = tk.StringVar(value="Pronto. A pesquisa fará pedidos reais aos sites.")
        ttk.Label(options, textvariable=self.status).pack(side=tk.RIGHT)

        columns = ("retailer", "name", "price", "score", "kind", "url")
        self.results = ttk.Treeview(frame, columns=columns, show="headings")
        headings = {
            "retailer": "Loja",
            "name": "Produto encontrado",
            "price": "Preço",
            "score": "Confiança",
            "kind": "Correspondência",
            "url": "URL",
        }
        widths = {"retailer": 110, "name": 270, "price": 75, "score": 85, "kind": 110, "url": 300}
        for column in columns:
            self.results.heading(column, text=headings[column])
            self.results.column(column, width=widths[column], anchor=tk.W)
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.results.yview)
        self.results.configure(yscrollcommand=scrollbar.set)
        self.results.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def start_search(self) -> None:
        query = self.query.get().strip()
        if not query:
            messagebox.showwarning("Pesquisa", "Escreve o nome de um produto.")
            return
        try:
            minimum_score = float(self.minimum_score.get())
            if not 0 <= minimum_score <= 1:
                raise ValueError
        except (TypeError, ValueError):
            messagebox.showwarning("Confiança", "Indica um valor entre 0 e 1.")
            return

        self.search_button.configure(state=tk.DISABLED)
        self.status.set("A pesquisar nos sites; isto pode demorar...")
        for item in self.results.get_children():
            self.results.delete(item)
        threading.Thread(
            target=self._search_worker,
            args=(query, minimum_score),
            daemon=True,
        ).start()

    def _search_worker(self, query: str, minimum_score: float) -> None:
        try:
            matches = self.scraper.search_product(query, minimum_score=minimum_score)
            self.after(0, self._show_results, matches)
        except Exception as exc:
            logging.exception("Pesquisa falhou")
            self.after(0, self._show_error, str(exc))

    def _show_results(self, matches) -> None:
        for match in matches:
            result = match.result
            self.results.insert(
                "",
                tk.END,
                values=(
                    result.retailer,
                    result.name,
                    f"{result.price:.2f} {result.currency}",
                    f"{match.score:.0%}",
                    match.match_type,
                    result.url,
                ),
            )
        self.status.set(f"{len(matches)} resultado(s) encontrado(s).")
        self.search_button.configure(state=tk.NORMAL)

    def _show_error(self, error: str) -> None:
        self.status.set("A pesquisa falhou.")
        self.search_button.configure(state=tk.NORMAL)
        messagebox.showerror("Erro na pesquisa", error)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ScraperApp().mainloop()

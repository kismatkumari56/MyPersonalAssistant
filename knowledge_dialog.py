"""PySide6 Knowledge Base Manager Dialog and Interactive Semantic Query Tester."""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from rag_engine import KnowledgeBase, get_knowledge_base
from strings import get_string


class KnowledgeBaseDialog(QDialog):
    """Visual dialog to manage local RAG documents, inspect chunks, and test hybrid semantic search."""

    index_updated = Signal()

    def __init__(self, parent: Optional[QWidget] = None, kb: Optional[KnowledgeBase] = None):
        super().__init__(parent)
        self.kb = kb or get_knowledge_base()
        self.setWindowTitle(self.t("kb_dialog_title", "Knowledge Base Studio (RAG)"))
        self.setMinimumSize(700, 500)
        self.resize(800, 560)
        self.build_ui()

    def t(self, key: str, fallback: str = "") -> str:
        parent = self.parent()
        lang = getattr(parent, "current_language", "en") if parent else "en"
        val = get_string(lang, key)
        return val if val != key else fallback

    def build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        header = QLabel(self.t("kb_header", "📚 Local Knowledge Base & RAG Engine"))
        font = header.font()
        font.setPointSize(14)
        font.setBold(True)
        header.setFont(font)
        layout.addWidget(header)

        subhead = QLabel(
            self.t(
                "kb_subhead",
                "Index local files and directories for zero-latency, private, hybrid semantic retrieval.",
            )
        )
        subhead.setStyleSheet("color: #718096; font-size: 12px;")
        layout.addWidget(subhead)

        splitter = QSplitter(Qt.Horizontal)

        # Left: Indexed documents list
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)

        files_label = QLabel(self.t("kb_indexed_files", "<b>Indexed Documents:</b>"))
        left_layout.addWidget(files_label)

        self.file_list = QListWidget()
        left_layout.addWidget(self.file_list, 1)

        btn_row = QHBoxLayout()
        add_file_btn = QPushButton(self.t("kb_add_file", "+ Add File"))
        add_file_btn.clicked.connect(self.on_add_file)
        btn_row.addWidget(add_file_btn)

        add_dir_btn = QPushButton(self.t("kb_add_folder", "+ Add Folder"))
        add_dir_btn.clicked.connect(self.on_add_folder)
        btn_row.addWidget(add_dir_btn)

        remove_btn = QPushButton(self.t("kb_remove_btn", "Remove"))
        remove_btn.clicked.connect(self.on_remove_file)
        btn_row.addWidget(remove_btn)

        left_layout.addLayout(btn_row)
        splitter.addWidget(left_widget)

        # Right: Interactive Semantic Search Tester
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        tester_label = QLabel(self.t("kb_query_tester", "<b>Hybrid Search Tester:</b>"))
        right_layout.addWidget(tester_label)

        test_row = QHBoxLayout()
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText(self.t("kb_test_placeholder", "Enter a query to test retrieval..."))
        self.query_input.returnPressed.connect(self.on_test_query)
        test_row.addWidget(self.query_input, 1)

        search_btn = QPushButton(self.t("kb_search_btn", "Search"))
        search_btn.clicked.connect(self.on_test_query)
        test_row.addWidget(search_btn)
        right_layout.addLayout(test_row)

        self.results_view = QPlainTextEdit()
        self.results_view.setReadOnly(True)
        self.results_view.setPlaceholderText(
            self.t("kb_results_placeholder", "Retrieved chunks with BM25, Cosine, and RRF scores will appear here.")
        )
        right_layout.addWidget(self.results_view, 1)

        splitter.addWidget(right_widget)
        splitter.setSizes([320, 480])
        layout.addWidget(splitter, 1)

        # Bottom Bar
        bottom = QHBoxLayout()
        self.stats_label = QLabel()
        self.update_stats()
        bottom.addWidget(self.stats_label)
        bottom.addStretch()

        clear_btn = QPushButton(self.t("kb_clear_all", "Clear Index"))
        clear_btn.setStyleSheet("color: #E53E3E;")
        clear_btn.clicked.connect(self.on_clear_all)
        bottom.addWidget(clear_btn)

        close_btn = QPushButton(self.t("close_btn", "Close"))
        close_btn.setFixedWidth(90)
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)

        layout.addLayout(bottom)
        self.refresh_list()

    def refresh_list(self) -> None:
        self.file_list.clear()
        for fpath in sorted(self.kb.indexed_files):
            chunks_count = sum(1 for c in self.kb.chunks.values() if c.doc_path == fpath)
            item = QListWidgetItem(f"📄 {os.path.basename(fpath)}  ({chunks_count} chunks)")
            item.setData(Qt.UserRole, fpath)
            self.file_list.addItem(item)
        self.update_stats()

    def update_stats(self) -> None:
        total_docs = len(self.kb.indexed_files)
        total_chunks = len(self.kb.chunks)
        self.stats_label.setText(
            f"Indexed: <b>{total_docs}</b> documents | <b>{total_chunks}</b> total chunks"
        )

    def on_add_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.t("kb_select_file", "Select Document to Index"),
            "",
            "Documents (*.md *.txt *.py *.json *.csv *.rst *.html);;All Files (*)",
        )
        if not path:
            return
        try:
            count = self.kb.add_file(path)
            self.refresh_list()
            self.index_updated.emit()
            QMessageBox.information(
                self,
                "Indexed",
                f"Successfully indexed '{os.path.basename(path)}' into {count} chunks.",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not index file: {e}")

    def on_add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, self.t("kb_select_folder", "Select Folder to Index"))
        if not folder:
            return
        try:
            count = self.kb.add_directory(folder, recursive=True)
            self.refresh_list()
            self.index_updated.emit()
            QMessageBox.information(
                self,
                "Folder Indexed",
                f"Successfully indexed folder '{os.path.basename(folder)}' into {count} chunks.",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not index folder: {e}")

    def on_remove_file(self) -> None:
        cur = self.file_list.currentItem()
        if not cur:
            return
        fpath = cur.data(Qt.UserRole)
        self.kb.remove_file(fpath)
        self.refresh_list()
        self.index_updated.emit()

    def on_clear_all(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear Index",
            "Are you sure you want to clear the entire knowledge base index?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.kb.clear()
            self.refresh_list()
            self.results_view.clear()
            self.index_updated.emit()

    def on_test_query(self) -> None:
        query = self.query_input.text().strip()
        if not query:
            return
        results = self.kb.query(query, top_k=5)
        if not results:
            self.results_view.setPlainText(f"No relevant chunks found for query: '{query}'")
            return

        lines = [f"=== Top {len(results)} Hybrid Search Matches for: '{query}' ===\n"]
        for idx, r in enumerate(results, 1):
            c = r["chunk"]
            lines.append(
                f"[{idx}] {c.doc_name} > {c.heading} (Chunk #{c.chunk_index})\n"
                f"    RRF Score: {r['rrf_score']:.5f} | BM25: {r['bm25_score']:.2f} | Cosine: {r['vector_score']:.3f}\n"
                f"    {'-'*50}\n"
                f"    {c.text}\n"
            )
        self.results_view.setPlainText("\n".join(lines))

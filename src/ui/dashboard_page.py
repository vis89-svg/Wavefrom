"""Dashboard page: headline stats, words per day, top apps, Groq usage.

Stats come from :mod:`src.stats` over the dictation history; usage comes
from :mod:`src.usage` (counted on this computer, per day).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from src import stats as S
from src.ui import theme as T

log = logging.getLogger(__name__)

_STYLE = f"""
QWidget#dashboardPage, QWidget#dashBody {{ background-color: {T.BG}; }}
QFrame#tile, QFrame#panel {{ background: {T.SURFACE}; border-radius: 12px; }}
{T.BUTTONS}
{T.SCROLLBARS}
{T.MENUS}
"""

_WARN = "#d9a441"          # reserved for "close to the limit" (always with text)
_MODEL_ROLES = {
    "whisper-large-v3-turbo": "Live preview",
    "whisper-large-v3": "Final transcription",
    "openai/gpt-oss-120b": "Cleanup & Polish",
    "openai/gpt-oss-20b": "Cleanup",
}


def greeting(name: str, now: datetime | None = None) -> str:
    h = (now or datetime.now()).hour
    part = "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17 else "evening"
    return f"Good {part}, {name}" if name else f"Good {part}"


# ── Pieces ───────────────────────────────────────────────────────────────────
def _panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("panel")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(20, 16, 20, 16)
    lay.setSpacing(12)
    head = QLabel(title)
    head.setStyleSheet(f"color: {T.TEXT}; font: 16px '{T.SERIF}'; background: transparent;")
    lay.addWidget(head)
    return frame, lay


class _Tile(QFrame):
    """One headline number: label, value, caption."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)
        self._label = QLabel(label)
        self._label.setStyleSheet(T.label_style(T.TEXT_MUTED, 12))
        self._value = QLabel("—")
        self._value.setStyleSheet(
            f"color: {T.TEXT}; font: 30px '{T.SERIF}'; background: transparent;")
        self._caption = QLabel("")
        self._caption.setStyleSheet(T.label_style(T.TEXT_FAINT, 12))
        self._caption.setWordWrap(True)
        for w in (self._label, self._value, self._caption):
            lay.addWidget(w)

    def set(self, value: str, caption: str = "") -> None:
        self._value.setText(value)
        self._caption.setText(caption)


class BarChart(QWidget):
    """Words per day: one series, thin rounded bars, hover tooltip."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: list[tuple[date, int]] = []
        self._hover = -1
        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, data: list[tuple[date, int]]) -> None:
        self._data = data
        self.update()

    # layout: plot area leaves room for x labels below and a max label left
    def _plot(self) -> QRectF:
        return QRectF(40, 8, self.width() - 48, self.height() - 34)

    def _slot(self, i: int) -> QRectF:
        plot = self._plot()
        n = max(1, len(self._data))
        w = plot.width() / n
        return QRectF(plot.left() + i * w, plot.top(), w, plot.height())

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self._plot()
        peak = max((v for _, v in self._data), default=0)
        top = max(peak, 1)
        # Recessive axis: baseline + one max gridline with its value.
        grid = QColor(T.BORDER)
        p.setPen(QPen(grid, 1))
        p.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
        if peak:
            p.setPen(QPen(grid, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.right(), plot.top()))
            p.setPen(QColor(T.TEXT_FAINT))
            p.drawText(QRectF(0, plot.top() - 8, 34, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       S.format_count(peak))
        font = p.font()
        font.setPixelSize(11)
        p.setFont(font)
        n = len(self._data)
        for i, (d, v) in enumerate(self._data):
            slot = self._slot(i)
            bw = min(22.0, slot.width() * 0.62)  # 2px+ surface gap between bars
            x = slot.center().x() - bw / 2
            if v > 0:
                h = max(3.0, plot.height() * v / top)
                bar = QRectF(x, plot.bottom() - h, bw, h)
                color = QColor(T.ACCENT_HOVER if i == self._hover else T.ACCENT)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(color))
                # Rounded data end, square on the baseline.
                r = min(4.0, bw / 2, h / 2)
                p.drawRoundedRect(bar, r, r)
                p.drawRect(QRectF(x, bar.bottom() - r, bw, r))
            # Label every other day (and always the last = today).
            if i % 2 == (n - 1) % 2:
                p.setPen(QColor(T.TEXT_FAINT if i != n - 1 else T.TEXT_MUTED))
                label = "Today" if i == n - 1 else f"{d.day}"
                p.drawText(QRectF(slot.left() - 10, plot.bottom() + 6, slot.width() + 20, 16),
                           Qt.AlignmentFlag.AlignHCenter, label)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        x = event.position().x()
        idx = -1
        for i in range(len(self._data)):
            if self._slot(i).left() <= x < self._slot(i).right():
                idx = i
                break
        if idx != self._hover:
            self._hover = idx
            self.update()
        if idx >= 0:
            d, v = self._data[idx]
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"{d:%a} {d.day} {d:%b} · {v:,} words", self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        QToolTip.hideText()
        self.update()


class Meter(QWidget):
    """Thin horizontal usage bar (fraction 0..1)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frac = 0.0
        self._color = T.ACCENT
        self.setFixedHeight(6)

    def set(self, frac: float, color: str) -> None:
        self._frac = max(0.0, min(1.0, frac))
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(T.BORDER)))
        p.drawRoundedRect(QRectF(0, 0, self.width(), 6), 3, 3)
        if self._frac > 0:
            p.setBrush(QBrush(QColor(self._color)))
            p.drawRoundedRect(QRectF(0, 0, max(6.0, self.width() * self._frac), 6), 3, 3)


# ── Page ─────────────────────────────────────────────────────────────────────
class DashboardPage(QWidget):
    def __init__(self, display_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardPage")
        self.setStyleSheet(_STYLE)
        self._name = display_name
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("dashBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(40, 30, 32, 24)
        lay.setSpacing(16)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        title = QLabel("Dashboard")
        title.setStyleSheet(T.page_title_style(28))
        lay.addWidget(title)
        self._greet = QLabel("")
        self._greet.setStyleSheet(T.label_style(T.TEXT_MUTED, 14))
        lay.addWidget(self._greet)

        tiles = QGridLayout()
        tiles.setSpacing(12)
        self._t_words = _Tile("Words dictated")
        self._t_saved = _Tile("Time saved")
        self._t_streak = _Tile("Day streak")
        self._t_wpm = _Tile("Speaking speed")
        for i, t in enumerate((self._t_words, self._t_saved, self._t_streak, self._t_wpm)):
            tiles.addWidget(t, 0, i)
        lay.addLayout(tiles)

        chart_panel, chart_lay = _panel("Words per day · last 14 days")
        self._chart = BarChart()
        chart_lay.addWidget(self._chart)
        lay.addWidget(chart_panel)

        row = QHBoxLayout()
        row.setSpacing(12)
        apps_panel, self._apps_lay = _panel("Top apps")
        usage_panel, self._usage_lay = _panel("Groq usage today")
        row.addWidget(apps_panel, stretch=1)
        row.addWidget(usage_panel, stretch=1)
        lay.addLayout(row)

        recent_panel, self._recent_lay = _panel("Recent dictations")
        lay.addWidget(recent_panel)
        lay.addStretch()

    # ── data ────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        from src import history, usage
        try:
            entries = history.all_entries()
        except Exception as e:
            log.warning("Dashboard: history read failed: %s", e)
            entries = []
        st = S.compute(entries)
        self._greet.setText(greeting(self._name))

        self._t_words.set(S.format_count(st.total_words),
                          f"+{st.words_today:,} today · {st.total_dictations:,} dictations")
        self._t_saved.set(S.format_duration(st.time_saved_secs),
                          f"vs typing at {int(S.TYPING_WPM)} wpm")
        days = "day" if st.streak_days == 1 else "days"
        self._t_streak.set(f"{st.streak_days} {days}",
                           "Keep it going" if st.streak_days else "Dictate today to start one")
        if st.wpm:
            faster = st.wpm / S.TYPING_WPM
            self._t_wpm.set(f"{st.wpm:.0f} wpm", f"≈ {faster:.1f}× faster than typing")
        else:
            self._t_wpm.set("—", "Starts with your next dictation")
        self._chart.set_data(st.words_per_day)
        self._fill_apps(st)
        self._fill_usage(usage.today())
        self._fill_recent(entries[:3])

    @staticmethod
    def _clear(lay: QVBoxLayout) -> None:
        # Keep the panel title (index 0); drop everything after it.
        while lay.count() > 1:
            item = lay.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                DashboardPage._clear_layout(item.layout())

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _note(self, lay: QVBoxLayout, text: str) -> None:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(T.label_style(T.TEXT_FAINT, 13))
        lay.addWidget(lbl)

    def _fill_apps(self, st: S.Stats) -> None:
        self._clear(self._apps_lay)
        if not st.top_apps:
            self._note(self._apps_lay,
                       "Appears after your next dictation — Waveform now notes "
                       "which app you dictate into.")
            self._apps_lay.addStretch()
            return
        top = st.top_apps[0][1] or 1
        total = sum(w for _, w in st.top_apps) or 1
        for app, words in st.top_apps:
            line = QHBoxLayout()
            name = QLabel(app)
            name.setStyleSheet(T.label_style(T.TEXT, 13))
            val = QLabel(f"{words:,} words · {words / total:.0%}")
            val.setStyleSheet(T.label_style(T.TEXT_MUTED, 12))
            line.addWidget(name)
            line.addStretch()
            line.addWidget(val)
            self._apps_lay.addLayout(line)
            m = Meter()
            m.set(words / top, T.ACCENT)
            self._apps_lay.addWidget(m)
        self._apps_lay.addStretch()

    def _fill_usage(self, rows: list[dict]) -> None:
        self._clear(self._usage_lay)
        if not rows:
            self._note(self._usage_lay, "No requests yet today.")
        for r in rows:
            role = _MODEL_ROLES.get(r["model"], r["model"])
            req_limit = r["limits"].get("requests")
            used = r["requests"]
            frac = used / req_limit if req_limit else 0.0
            near = frac >= 0.8
            head = QHBoxLayout()
            name = QLabel(role)
            name.setStyleSheet(T.label_style(T.TEXT, 13))
            head.addWidget(name)
            head.addStretch()
            count = QLabel(f"{used:,} / {req_limit:,} requests" if req_limit
                           else f"{used:,} requests")
            count.setStyleSheet(T.label_style(T.TEXT_MUTED, 12))
            head.addWidget(count)
            self._usage_lay.addLayout(head)
            m = Meter()
            m.set(frac, _WARN if near else T.ACCENT)
            self._usage_lay.addWidget(m)
            bits = [r["model"]]
            if r["audio_secs"]:
                a_lim = r["limits"].get("audio_secs")
                bits.append(f"{r['audio_secs'] / 60:.0f} min audio"
                            + (f" of {a_lim // 60:,}" if a_lim else ""))
            if r["tokens"]:
                t_lim = r["limits"].get("tokens")
                bits.append(f"{S.format_count(r['tokens'])} tokens"
                            + (f" of {S.format_count(t_lim)}" if t_lim else ""))
            if r["remaining"] is not None:
                bits.append(f"Groq reports {r['remaining']:,} left")
            if near:
                bits.append("close to today's limit")
            sub = QLabel(" · ".join(bits))
            sub.setWordWrap(True)
            sub.setStyleSheet(T.label_style(_WARN if near else T.TEXT_FAINT, 11))
            self._usage_lay.addWidget(sub)
        self._note(self._usage_lay,
                   "Counted on this computer. Free-tier limits are approximate.")
        self._usage_lay.addStretch()

    def _fill_recent(self, entries: list[dict]) -> None:
        from src.ui.history_page import _Card
        self._clear(self._recent_lay)
        if not entries:
            self._note(self._recent_lay, "Hold Ctrl + Win and speak — your words show up here.")
            return
        for e in entries:
            card = _Card(e, self._on_delete)
            card.setStyleSheet(f"QFrame#card {{ background: {T.BG}; border: 1px solid {T.BORDER}; "
                               "border-radius: 10px; }")
            self._recent_lay.addWidget(card)

    def _on_delete(self, entry_id: int) -> None:
        from PySide6.QtCore import QTimer
        from src.history import delete
        delete(entry_id)
        QTimer.singleShot(0, self.refresh)

# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""AI Memory terminal client. Run with uv run mem.py."""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Input, Static

from mem_backend import Backend, BackendError, Citation
from mem_flow import FlowCanvas

VERSION = "1.0.0"

# ───────────────── OpenCode "opencode" theme (dark) ─────────────────
BG, PANEL, ELEM = (10, 10, 10), (20, 20, 20), (30, 30, 30)
PROMPT_BG, BORDER, BORDER_DIM = (28, 28, 28), (72, 72, 72), (50, 50, 50)
TEXT, MUTED = (238, 238, 238), (128, 128, 128)
PEACH, BLUE, PURPLE = (250, 178, 131), (92, 156, 245), (157, 124, 216)
RED, ORANGE, GREEN = (224, 108, 117), (245, 167, 66), (127, 216, 143)
CYAN, YELLOW = (86, 182, 194), (229, 192, 123)
PROV = {"ChatGPT": GREEN, "Claude": ORANGE, "Gemini": CYAN, "Codex": BLUE, "OpenCode": PURPLE}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WEB_PROVIDERS = ("ChatGPT", "Claude", "Gemini")
SYNC_PHASES = {
    "prepare": "Илгээх багцыг бэлтгэж байна",
    "reconcile": "Одоо байгаа эхийг тулгаж байна",
    "upload": "Шинэ хувилбарыг илгээж байна",
    "readiness": "Эх бэлэн болохыг хүлээж байна",
    "cleanup": "Баталгаажуулж, хуучин хувилбарыг цэвэрлэж байна",
}
AUTO_SYNC_RETRY_DELAY = 60


def sync_phase_text(activity: dict) -> str:
    label = SYNC_PHASES.get(activity.get("phase"), "NotebookLM sync явагдаж байна")
    segment = activity.get("segment_id")
    return f"{segment} · {label}" if segment else label


def rgb(k: tuple[int, int, int]) -> str:
    return f"rgb({k[0]},{k[1]},{k[2]})"


def hx(k: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*k)


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def tx(s: str, k=TEXT, bold: bool = False) -> Text:
    return Text(s, style=("bold " if bold else "") + rgb(k))


def message_markdown(text: str) -> Markdown:
    return Markdown(text, style=rgb(TEXT), hyperlinks=False)


def lr(left: Text, right: Text, w: int) -> Text:
    return Text.assemble(left, " " * max(1, w - left.cell_len - right.cell_len), right)


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def when_label(ts: float) -> tuple[str, str]:
    """(категори, цаг) — OpenCode-ийн sessions жагсаалт шиг өдрөөр бүлэглэнэ."""
    now = time.time()
    if not ts:
        return "Огноогүй", ""
    d, n = time.localtime(ts), time.localtime(now)
    day = (d.tm_year, d.tm_yday)
    hm = time.strftime("%H:%M", d)
    if day == (n.tm_year, n.tm_yday):
        return "Өнөөдөр", hm
    y = time.localtime(now - 86400)
    if day == (y.tm_year, y.tm_yday):
        return "Өчигдөр", hm
    return time.strftime("%Y-%m-%d", d), hm


# ───────────────── logo: OpenCode-ийн pixel фонтын кодчилол ─────────────────
GLYPHS = {
    "m": ["       ", "█▀▀▄▀▀▄", "█__█__█", "▀~~▀~~▀"],
    "e": ["    ", "█▀▀█", "█^^^", "▀▀▀▀"],
    "o": ["    ", "█▀▀█", "█__█", "▀▀▀▀"],
    "r": ["    ", "█▀▀▄", "█___", "▀   "],
    "y": ["    ", "█__█", "▀▄▄█", "▄▄▄▀"],
}


def _word(w: str) -> list[str]:
    return [" ".join(GLYPHS[c][r] for c in w) for r in range(4)]


def _logo_line(line: str, fg, bold: bool) -> Text:
    shadow = mix(BG, fg, 0.25)
    t = Text(no_wrap=True)
    st = "bold " if bold else ""
    for ch in line:
        if ch == "_":
            t.append(" ", style=f"{st}{rgb(fg)} on {rgb(shadow)}")
        elif ch == "^":
            t.append("▀", style=f"{st}{rgb(fg)} on {rgb(shadow)}")
        elif ch == "~":
            t.append("▀", style=f"{st}{rgb(shadow)}")
        elif ch == ",":
            t.append("▄", style=f"{st}{rgb(shadow)}")
        else:
            t.append(ch, style=f"{st}{rgb(fg)}")
    return t


def logo_text(width: int) -> Text:
    if width < 40:
        return Text.assemble(("mem", rgb(MUTED)), ("ory", "bold " + rgb(TEXT)), justify="center")
    left, right = _word("mem"), _word("ory")
    rows = [
        Text.assemble(_logo_line(left_row, MUTED, False), " ", _logo_line(right_row, TEXT, True))
        for left_row, right_row in zip(left, right, strict=True)
    ]
    out = Text("\n").join(rows)
    out.justify = "center"
    return out


# ───────────────── өгөгдөл ─────────────────
@dataclass
class Chat:
    id: str
    ai: str
    title: str
    ts: float
    message_count: int
    account: str = ""
    platform: str = ""
    revision: int = 0
    project: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    local: bool = True
    approved: bool = False
    nb: str = "none"
    parts: tuple[int, int] = (0, 0)
    error: str = ""
    prog: float = 0.0
    messages: list[tuple[str, str]] | None = None

    @property
    def fully_synced(self) -> bool:
        return self.nb == "done" or (self.nb == "running" and self.parts[0] == self.parts[1] > 0)


@dataclass
class Sess:
    id: str
    kind: str  # ask | chat
    title: str
    ts: float
    chat_id: str | None = None
    msgs: list[dict] = field(default_factory=list)
    cite: int = 0


# ───────────────── командууд ─────────────────
@dataclass
class Cmd:
    name: str
    title: str
    desc: str
    category: str
    aliases: tuple[str, ...] = ()
    key: str = ""


COMMANDS = [
    Cmd("dashboard", "Хяналтын самбар", "Receiver, NotebookLM, pipeline, үйл явдал", "Санах ой"),
    Cmd("chats", "Чатуудын архив", "Архивын чат хайж унших", "Санах ой", ("archive",)),
    Cmd(
        "resume",
        "Яриаг үргэлжлүүлэх",
        "Энэ ажиллуулалтын асуулт, уншсан чатууд",
        "Санах ой",
        ("sessions",),
    ),
    Cmd("new", "Шинэ асуулт", "Нүүр хуудас руу очно", "Санах ой", (), "ctrl+n"),
    Cmd("sources", "Ишлэлүүд", "Хариултын эх мессежийг нээх", "Санах ой", (), "ctrl+o"),
    Cmd("chatgpt", "ChatGPT чатууд", "ChatGPT-ийн чатуудын tab", "Provider"),
    Cmd("claude", "Claude чатууд", "Claude-ийн чатуудын tab", "Provider"),
    Cmd("gemini", "Gemini чатууд", "Gemini-ийн чатуудын tab", "Provider"),
    Cmd("codex", "Codex чатууд", "Codex-ийн чатуудын tab", "Provider"),
    Cmd("opencode", "OpenCode чатууд", "OpenCode-ийн чатуудын tab", "Provider"),
    Cmd("provider", "Provider сонгох", "Архивын шүүлтүүр", "Provider", ("providers",)),
    Cmd("connect", "Холболт", "Extension, Google Docs, NotebookLM", "Холболт", ("status",)),
    Cmd("setup", "NotebookLM тохируулах", "Notebook үүсгэх эсвэл бэлэн notebook холбох", "Холболт"),
    Cmd("login", "NotebookLM-д нэвтрэх", "Brave-д нэвтэрсэн Google аккаунтаар холбох", "Холболт"),
    Cmd("docs-connect", "Google Docs холбох", "Brave-ийн Docs нэвтрэлтийг ашиглах", "Холболт"),
    Cmd("check", "Холболт шалгах", "Алхам алхмаар шалгана", "Холболт"),
    Cmd("flow", "Ажлын урсгал", "Архиваас NotebookLM хүрэх урсгал", "Санах ой"),
    Cmd(
        "sync-all",
        "Архивын өөрчлөлтийг sync хийх",
        "Локал архивын бүх чатын өөрчлөлтийг NotebookLM руу илгээнэ",
        "Sync",
        ("syns-all",),
    ),
    Cmd(
        "auto",
        "Автомат sync",
        "Архивын өөрчлөлтийг илгээнэ · /auto on|off",
        "Sync",
    ),
    Cmd("stop", "Ажил зогсоох", "Идэвхтэй sync, шалгалт эсвэл асуултыг зогсоох", "Sync"),
    Cmd("help", "Тусламж", "Товчлуур, командууд", "Систем"),
    Cmd("exit", "Гарах", "mem-ээс гарна", "Систем", ("quit",)),
]


def find_cmd(name: str) -> Cmd | None:
    name = name.lower().lstrip("/")
    return next((c for c in COMMANDS if c.name == name or name in c.aliases), None)


# ───────────────── жижиг clickable widget-үүд ─────────────────
class Click(Static):
    def __init__(self, content: Text, cb: Callable[[], None] | None = None, **kw: Any) -> None:
        super().__init__(content, **kw)
        self.cb = cb

    def on_click(self, event: events.Click) -> None:
        if self.cb:
            event.stop()
            self.cb()
            self.app.call_after_refresh(self.app._refocus)


class Hint(Click):
    """`key  label` — OpenCode-ийн footer hint. Товшиж болно."""

    def __init__(
        self, key: str, label: str, cb: Callable[[], None] | None = None, **kw: Any
    ) -> None:
        super().__init__(
            Text.assemble(
                (key, rgb(TEXT)), (" " + label if label else "", rgb(MUTED)), no_wrap=True
            ),
            cb,
            **kw,
        )


def fit_dialog(screen: ModalScreen, cols: int, top_divisor: int) -> int:
    width, height = screen.app.size
    top = max(1, height // top_divisor) if height >= 16 else 0
    dlg = screen.query_one("#dlg")
    dlg.styles.width = min(cols, max(1, width - 4))
    dlg.styles.margin = (top, 0, 0, 0)
    return height - top


# ───────────────── жагсаалтын мөр ─────────────────
@dataclass
class Opt:
    value: Any
    title: str
    desc: str = ""
    desc_color: tuple = MUTED
    category: str = ""
    footer: str = ""
    footer_color: tuple = MUTED
    glyph: tuple[str, tuple] | None = None
    disabled: bool = False


class Row(Static):
    """Dialog болон slash-popup-ийн мөр. Идэвхтэй мөр = peach дэвсгэр, бараан текст."""

    def __init__(self, owner: Any, idx: int, opt: Opt) -> None:
        super().__init__("", classes="row")
        self.owner, self.idx, self.opt = owner, idx, opt
        self.active = False
        self.marked = False

    def on_mount(self) -> None:
        self.paint()

    def on_resize(self, event: events.Resize) -> None:
        self.paint()

    def paint(self) -> None:
        o, a = self.opt, self.active
        fg = BG if a else TEXT
        mu = BG if a else MUTED
        w = max(20, (self.size.width or 58) - 2)
        left = Text(no_wrap=True, overflow="ellipsis")
        if getattr(self.owner, "markable", False):
            left.append(
                "◼ " if self.marked else "◻ ",
                style=rgb(BG if a else (PEACH if self.marked else BORDER)),
            )
        if o.glyph:
            left.append(o.glyph[0] + " ", style=rgb(BG if a else o.glyph[1]))
        left.append(o.title, style=("bold " if a else "") + rgb(fg if not o.disabled else mu))
        if o.desc:
            left.append("  " + o.desc, style=rgb(BG if a else o.desc_color))
        right = Text(o.footer, style=rgb(BG if a else o.footer_color), no_wrap=True)
        room = max(4, w - right.cell_len - 1)
        if left.cell_len > room:
            left.truncate(room, overflow="ellipsis")
        self.update(lr(left, right, w))

    def set_active(self, on: bool) -> None:
        if self.active != on:
            self.active = on
            self.set_class(on, "active")
            self.paint()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self.opt.disabled:
            self.owner.hover(self.idx)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if not self.opt.disabled:
            self.owner.pick(self.idx)


# ───────────────── dialog-ууд ─────────────────
class DialogSelect(ModalScreen):
    """OpenCode-ийн DialogSelect: гарчиг, хайлт, ангилал, мөр, footer hint. Mouse + keyboard."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "move(-1)", show=False),
        Binding("down", "move(1)", show=False),
        Binding("pageup", "move(-8)", show=False),
        Binding("pagedown", "move(8)", show=False),
        Binding("escape", "close", show=False),
        Binding("tab", "mark", show=False),
        Binding("enter", "enter", show=False),
    ]

    def __init__(
        self,
        title: str,
        options: list[Opt] | Callable[[], list[Opt]],
        *,
        size: int = 60,
        multi: bool = False,
        marks: set | None = None,
        placeholder: str = "Хайх",
        searchable: bool = True,
        hints: list[tuple[str, str, Callable]] | None = None,
        current: Any = None,
        query: str = "",
        empty: str = "Үр дүн олдсонгүй",
        fallback: bool = True,
    ) -> None:
        super().__init__()
        self.dtitle, self.size_cols = title, size
        self.options = options if callable(options) else None
        self.opts_all = options() if callable(options) else options
        self.markable = multi
        self.multi = multi
        self.marks: set = set(marks or ())
        self.placeholder, self.searchable = placeholder, searchable
        self.hint_spec = hints or []
        self.current, self.qtext, self.empty, self.fallback = current, query, empty, fallback
        self.shown: list[Opt] = []
        self.rows: list[Row] = []
        self.active = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            with Horizontal(id="dhead"):
                yield Static(Text(self.dtitle, style="bold " + rgb(TEXT)), id="dtitle")
                yield Click(tx("esc", MUTED), self.action_close, id="desc")
            if self.searchable:
                yield SearchInput(
                    value=self.qtext, placeholder=self.placeholder, id="dsearch", compact=True
                )
            yield VerticalScroll(id="dlist")
            yield Static(id="dnote")
            with Horizontal(id="dhints"):
                for key, label, fn in self.hint_spec:
                    yield Hint(key, label, (lambda f=fn: f(self)))

    def on_mount(self) -> None:
        self.fit()
        if not self.hint_spec:
            self.query_one("#dhints").display = False
        self.rebuild()
        if self.searchable:
            self.query_one("#dsearch", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.fit()

    def fit(self) -> None:
        height = fit_dialog(self, self.size_cols, 8)
        chrome = 5 + (2 if self.searchable else 0) + (2 if self.hint_spec else 0)
        self.query_one("#dlist").styles.max_height = max(2, height - chrome)

    def refresh_live(self) -> None:
        if self.options is None or not self.query("#dlist"):
            return
        options = self.options()
        if options != self.opts_all:
            selected = self.current_opt()
            self.current = selected.value if selected else self.current
            self.opts_all = options
            self.marks.intersection_update(o.value for o in options)
            self.rebuild()

    # — жагсаалт —
    def rebuild(self) -> None:
        q = self.qtext.strip().casefold()
        self.shown = [
            o
            for o in self.opts_all
            if not q or q in f"{o.title} {o.desc} {o.category} {o.footer}".casefold()
        ]
        box = self.query_one("#dlist", VerticalScroll)
        box.remove_children()
        widgets: list[Any] = []
        self.rows, last = [], None
        for i, o in enumerate(self.shown):
            if o.category and o.category != last and not q:
                widgets.append(Static(Text(o.category, style="bold " + rgb(PURPLE)), classes="cat"))
                last = o.category
            r = Row(self, i, o)
            r.marked = o.value in self.marks
            self.rows.append(r)
            widgets.append(r)
        if widgets:
            box.mount(*widgets)
        self.query_one("#dnote", Static).update(tx(self.empty, MUTED) if not self.shown else "")
        self.active = -1
        start = next(
            (i for i, o in enumerate(self.shown) if o.value == self.current and not o.disabled),
            None,
        )
        if start is None:
            start = next((i for i, o in enumerate(self.shown) if not o.disabled), -1)
        self.call_after_refresh(lambda: self.set_active(start, scroll=True))

    def set_active(self, i: int, scroll: bool = False) -> None:
        if not (0 <= i < len(self.rows)):
            return
        if 0 <= self.active < len(self.rows):
            self.rows[self.active].set_active(False)
        self.active = i
        self.rows[i].set_active(True)
        if scroll:
            self.rows[i].scroll_visible(animate=False)

    def hover(self, idx: int) -> None:
        if idx != self.active:
            self.set_active(idx)

    def action_move(self, d: int) -> None:
        n = len(self.rows)
        if not n:
            return
        if abs(d) == 1:
            i = self.active
            for _ in range(n):
                i = (i + d) % n
                if not self.shown[i].disabled:
                    break
        else:
            i = max(0, min(n - 1, self.active + d))
        self.set_active(i, scroll=True)

    def current_opt(self) -> Opt | None:
        return self.shown[self.active] if 0 <= self.active < len(self.shown) else None

    # — үйлдэл —
    def pick(self, idx: int) -> None:
        if not (0 <= idx < len(self.shown)):
            return
        if self.multi:
            self.set_active(idx)
            self.toggle(idx)
        else:
            self.dismiss(("pick", self.shown[idx].value))

    def toggle(self, idx: int) -> None:
        v = self.shown[idx].value
        self.marks.symmetric_difference_update({v})
        self.rows[idx].marked = v in self.marks
        self.rows[idx].paint()

    def action_mark(self) -> None:
        if self.markable and 0 <= self.active < len(self.rows):
            i = self.active
            self.toggle(i)
            if i + 1 < len(self.rows):
                self.set_active(i + 1, scroll=True)

    def mark_all(self) -> None:
        vals = {o.value for o in self.shown if not o.disabled}
        if vals <= self.marks:
            self.marks -= vals
        else:
            self.marks |= vals
        for r in self.rows:
            r.marked = r.opt.value in self.marks
            r.paint()

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Input.Changed, "#dsearch")
    def _changed(self, e: Input.Changed) -> None:
        self.qtext = e.value
        self.rebuild()

    def confirm(self) -> None:
        if self.multi:
            picked = [o.value for o in self.opts_all if o.value in self.marks]
            if not picked and self.fallback and (o := self.current_opt()):
                picked = [o.value]
            self.dismiss(("ok", picked))
        elif o := self.current_opt():
            self.dismiss(("pick", o.value))

    def action_enter(self) -> None:
        self.confirm()

    @on(Input.Submitted, "#dsearch")
    def _submitted(self, e: Input.Submitted) -> None:
        self.confirm()


class JobDialog(ModalScreen):
    """Sync / шалгалтын явц. Esc дарвал арын горимд үргэлжилнэ."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape,enter,q", "close", show=False)]

    def __init__(self, get_job: Callable[[], Any]) -> None:
        super().__init__()
        self.get_job = get_job

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            with Horizontal(id="dhead"):
                yield Static(id="dtitle")
                yield Click(tx("esc", MUTED), self.action_close, id="desc")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(id="jbody")
            yield Static(id="jbar")
            with Horizontal(classes="nb-actions"):
                yield Button("Зогсоох", id="job-stop", compact=True)
                yield Button("Нэвтрэх", id="job-login", compact=True)
                yield Button("Нуух", id="job-close", compact=True)

    def on_mount(self) -> None:
        self.fit()
        self.query_one(".dialog-scroll").focus()
        self.set_interval(1 / 12, self.refresh_job)
        self.refresh_job()

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.fit()

    def fit(self) -> None:
        height = fit_dialog(self, 78, 6)
        self.query_one(".dialog-scroll").styles.max_height = max(2, height - 8)

    def refresh_job(self) -> None:
        job = self.get_job()
        if job is None:
            return
        w = max(30, (self.query_one("#dlg").size.width or 70) - 4)
        frame = SPIN[int(time.monotonic() * 12) % len(SPIN)]
        self.query_one("#dtitle", Static).update(Text(job.title, style="bold " + rgb(TEXT)))
        rows: list[Text] = [Text("")]
        for s in job.steps:
            g = {
                "pending": ("·", MUTED),
                "running": (frame, BLUE),
                "done": ("✔", GREEN),
                "fail": ("✕", RED),
                "skip": ("–", MUTED),
            }[s.state]
            left = Text.assemble(
                (g[0] + " ", rgb(g[1])),
                (s.label, rgb(TEXT if s.state in ("running", "done", "fail") else MUTED)),
            )
            right = tx(s.detail, RED if s.state == "fail" else MUTED)
            if right and left.cell_len + right.cell_len + 2 > w:
                rows.extend((left, Text.assemble("  ", right)))
            else:
                rows.append(lr(left, right, w))
        if job.notes:
            rows.append(Text(""))
            rows.extend(job.notes)
        self.query_one("#jbody", Static).update(Text("\n").join(rows))
        completed = sum(s.state == "done" for s in job.steps)
        label = {
            "running": "Ажиллаж байна",
            "done": "Дууссан",
            "partial": "Дахин илгээх ажил үлдсэн",
            "failed": "Алдаа гарлаа",
            "cancelled": "Зогсоосон",
            "stopping": "Зогсоож байна…",
        }[job.status]
        if job.status in ("running", "stopping"):
            label += f" · {int(time.monotonic() - job.started)}с"
        if job.status == "running":
            label += f" · {completed}/{len(job.steps)} алхам"
        bar = tx(label, GREEN if job.status == "done" else TEXT)
        self.query_one("#jbar", Static).update(bar)
        self.query_one("#job-stop", Button).display = job.status in {"running", "stopping"}
        self.query_one("#job-stop", Button).disabled = job.status == "stopping"
        self.query_one("#job-login", Button).display = (
            job.status == "failed" and job.error_code == "auth_required"
        )
        close = self.query_one("#job-close", Button)
        label = "Нуух" if job.status in {"running", "stopping"} else "Хаах"
        if str(close.label) != label:
            close.label = label

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "job-stop":
            self.app.cmd_stop()
        elif event.button.id == "job-login":
            self.dismiss(None)
            self.app.call_after_refresh(lambda: self.app.open_notebook_form("login"))
        else:
            self.action_close()

    def action_close(self) -> None:
        self.dismiss(None)


class DialogInfo(ModalScreen):
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape,enter,q", "close", show=False)]

    def __init__(
        self,
        title: str,
        body_fn: Callable[[int], Text],
        hints: list[tuple[str, str, Callable]] | None = None,
        size: int = 72,
    ) -> None:
        super().__init__()
        self.dtitle, self.body_fn, self.hint_spec, self.size_cols = (
            title,
            body_fn,
            hints or [],
            size,
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            with Horizontal(id="dhead"):
                yield Static(Text(self.dtitle, style="bold " + rgb(TEXT)), id="dtitle")
                yield Click(tx("esc", MUTED), self.action_close, id="desc")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(id="ibody")
            with Horizontal(id="dhints"):
                for key, label, fn in self.hint_spec:
                    yield Hint(key, label, (lambda f=fn: f(self)))

    def on_mount(self) -> None:
        self.fit()
        self.query_one(".dialog-scroll").focus()
        self.set_interval(1.0, self.refresh_body)
        self.refresh_body()

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.fit()
            self.call_after_refresh(self.refresh_body)

    def fit(self) -> None:
        height = fit_dialog(self, self.size_cols, 6)
        self.query_one("#dhints").display = bool(self.hint_spec)
        footer = 2 if self.hint_spec else 0
        self.query_one(".dialog-scroll").styles.max_height = max(2, height - 5 - footer)

    def refresh_body(self) -> None:
        w = max(30, (self.query_one("#dlg").size.width or 70) - 4)
        self.query_one("#ibody", Static).update(self.body_fn(w))

    def action_close(self) -> None:
        self.dismiss(None)


class NotebookDialog(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, details: Callable[[], tuple[Text, bool]]) -> None:
        super().__init__()
        self.details = details
        self.configured = None

    ACTIONS = (
        ("login", "Нэвтрэх", True),
        ("docs-connect", "Docs холбох", True),
        ("check", "Шалгах", True),
        ("flow", "Урсгал", True),
        ("create", "Notebook үүсгэх", False),
        ("attach", "Бэлэн notebook", False),
    )

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Static("Холболт", classes="nb-heading")
            with VerticalScroll(id="nb-scroll"):
                yield Static(classes="nb-text")
            with Horizontal(classes="nb-actions"):
                for action, label, _ in self.ACTIONS:
                    yield Button(label, id=action, compact=True)

    def on_mount(self) -> None:
        self.fit()
        self.refresh_live()

    def refresh_live(self) -> None:
        content = self.query(".nb-text")
        if not content:
            return
        body, configured = self.details()
        content.first(Static).update(body)
        if configured != self.configured:
            self.configured = configured
            for action, _, requires_configured in self.ACTIONS:
                self.query_one(f"#{action}", Button).display = configured == requires_configured
            first_action = "login" if configured else "create"
            self.query_one(f"#{first_action}", Button).focus(scroll_visible=False)

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.fit()

    def fit(self) -> None:
        height = fit_dialog(self, 68, 6)
        self.query_one("#nb-scroll").styles.max_height = max(3, height - 6)

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NotebookForm(ModalScreen[dict[str, str]]):
    BINDINGS = [Binding("escape", "cancel", show=False)]
    TITLES = {
        "create": "Notebook үүсгэх",
        "attach": "Бэлэн notebook холбох",
        "login": "Brave-ээр нэвтрэх",
    }

    def __init__(self, mode: str, account: str = "") -> None:
        super().__init__()
        self.mode, self.account = mode, account

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Static(self.TITLES[self.mode], classes="nb-heading")
            with VerticalScroll(id="nb-scroll"):
                yield Static(
                    "NotebookLM нээдэг Google аккаунтаараа Brave-д нэвтэрсэн байна.",
                    classes="nb-text",
                )
                if self.mode != "login":
                    heading = (
                        "Notebook-ийн нэр"
                        if self.mode == "create"
                        else "Notebook-ийн линк эсвэл ID"
                    )
                    yield Static(heading, classes="nb-field")
                    yield Input(
                        "AI Memory" if self.mode == "create" else "",
                        placeholder=(
                            "Notebook-ийн нэр"
                            if self.mode == "create"
                            else "https://notebooklm.google.com/notebook/…"
                        ),
                        id="nb-value",
                        max_length=150 if self.mode == "create" else 512,
                    )
                yield Static("Brave профайл · сонголтоор", classes="nb-field")
                yield Input(placeholder="Жишээ нь Profile 1", id="nb-profile", max_length=256)
                yield Static("Google имэйл · сонголтоор", classes="nb-field")
                yield Input(
                    self.account,
                    placeholder="Нэг профайлд олон аккаунттай бол",
                    id="nb-account",
                    max_length=256,
                )
            yield Static("", id="nb-validation", classes="nb-text")
            with Horizontal(classes="nb-actions"):
                label = {"login": "Нэвтрэх", "create": "Үүсгэх", "attach": "Холбох"}[self.mode]
                yield Button(label, id="nb-submit", compact=True)
                yield Button("Буцах", id="nb-cancel", compact=True)

    def on_mount(self) -> None:
        self.fit()
        self.query_one(Input).focus(scroll_visible=False)

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.fit()

    def fit(self) -> None:
        height = fit_dialog(self, 68, 6)
        self.query_one("#nb-scroll").styles.max_height = max(3, height - 7)

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        if event.button.id == "nb-cancel":
            self.dismiss(None)
        else:
            self.submit()

    @on(Input.Submitted)
    def submit(self) -> None:
        fields = {
            "profile": self.query_one("#nb-profile", Input).value.strip(),
            "account": self.query_one("#nb-account", Input).value.strip(),
        }
        if self.mode != "login":
            field = self.query_one("#nb-value", Input)
            if not field.value.strip():
                self.query_one("#nb-validation", Static).update(tx("Талбарыг бөглөнө үү.", RED))
                field.focus()
                return
            fields.update(mode=self.mode, value=field.value.strip())
        self.dismiss(fields)

    @on(Input.Changed, "#nb-value")
    def clear_validation(self) -> None:
        self.query_one("#nb-validation", Static).update("")

    def action_cancel(self) -> None:
        self.dismiss(None)


# ───────────────── slash autocomplete popup ─────────────────
class Popup(VerticalScroll, can_focus=False):
    markable = False
    multi = False

    def __init__(self) -> None:
        super().__init__(id="popup")
        self.display = False
        self.rows: list[Row] = []
        self.items: list[Opt] = []
        self.active = 0
        self.on_pick: Callable[[Opt], None] | None = None

    def show(self, items: list[Opt], region: Any, on_pick: Callable[[Opt], None]) -> None:
        self.on_pick = on_pick
        self.items = items
        self.remove_children()
        self.rows = [Row(self, i, o) for i, o in enumerate(items)]
        self.mount(*self.rows)
        h = min(len(items), 8)
        self.styles.height = h
        self.styles.width = region.width
        self.styles.offset = (region.x, max(0, region.y - h))
        self.display = True
        self.active = 0
        self.call_after_refresh(lambda: self.set_active(0))

    def hide(self) -> None:
        self.display = False
        self.items, self.rows = [], []

    @property
    def is_open(self) -> bool:
        return bool(self.display and self.items)

    def set_active(self, i: int) -> None:
        if not self.rows:
            return
        i %= len(self.rows)
        self.rows[self.active % len(self.rows)].set_active(False)
        self.active = i
        self.rows[i].set_active(True)
        self.rows[i].scroll_visible(animate=False)

    def move(self, d: int) -> None:
        if self.rows:
            self.set_active(self.active + d)

    def hover(self, idx: int) -> None:
        if idx != self.active:
            self.set_active(idx)

    def pick(self, idx: int) -> None:
        if self.on_pick and 0 <= idx < len(self.items):
            self.on_pick(self.items[idx])

    def chosen(self) -> Opt | None:
        return self.items[self.active] if 0 <= self.active < len(self.items) else None


# ───────────────── prompt ─────────────────
class PromptInput(Input):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("up", "p_up", show=False),
        Binding("down", "p_down", show=False),
        Binding("pageup", "p_page(-1)", show=False),
        Binding("pagedown", "p_page(1)", show=False),
        Binding("tab", "p_tab", show=False),
        Binding("escape", "p_esc", show=False),
        Binding("enter", "p_enter", show=False),
    ]

    def __init__(self, prompt: Prompt, **kw: Any) -> None:
        super().__init__(**kw)
        self.prompt = prompt

    def action_p_up(self) -> None:
        self.prompt.kb_up()

    def action_p_down(self) -> None:
        self.prompt.kb_down()

    def action_p_page(self, direction: int) -> None:
        app = self.app
        if app.popup.is_open:
            app.popup.move(direction * 8)
        else:
            app.scroll_content(direction)

    def action_p_tab(self) -> None:
        self.prompt.kb_tab()

    def action_p_esc(self) -> None:
        self.prompt.kb_esc()

    def action_p_enter(self) -> None:
        self.prompt.kb_enter()


class Edge(Static):
    """Prompt-ийн доод ирмэг: `╹▀▀▀▀`."""

    def __init__(self) -> None:
        super().__init__("", classes="pedge")
        self.color = BLUE

    def on_resize(self, event: events.Resize) -> None:
        self.paint()

    def paint(self) -> None:
        w = self.size.width or 40
        self.update(
            Text.assemble(
                ("╹", rgb(self.color)), ("▀" * max(0, w - 1), rgb(PROMPT_BG)), no_wrap=True
            )
        )


class Prompt(Vertical):
    """Команд ба асуулт хоёрыг нэг дор авдаг prompt (OpenCode-ийн prompt-той адил)."""

    def __init__(self, pid: str) -> None:
        super().__init__(id=pid, classes="prompt")
        self.history: list[str] = []
        self.hist_i = -1

    def compose(self) -> ComposeResult:
        with Vertical(classes="pbox"):
            yield PromptInput(
                self,
                placeholder="NotebookLM-ээс асуух…",
                compact=True,
                classes="pinput",
            )
            with Horizontal(classes="pmeta"):
                yield Static(tx("NotebookLM", BLUE, True))
        yield Edge()
        with Horizontal(classes="pfoot"):
            yield Static(Text(""), classes="pstatus")
            with Horizontal(classes="phints"):
                yield Hint("Ctrl+O", "ишлэл", self.app.cmd_sources, classes="source-hint")
                yield Hint("Ctrl+P", "команд", self.app.cmd_palette)

    def on_mount(self) -> None:
        self.refresh_meta()

    # — харагдац —
    def refresh_meta(self) -> None:
        app = self.app
        self.query_one(".source-hint", Hint).display = self.id == "p-sess" and bool(
            app.session_citations()
        )

    def refresh_status(self) -> None:
        app = self.app
        st = self.query_one(".pstatus", Static)
        job = app.job
        active_job = job is not None and job.status in ("running", "stopping")
        active = active_job or bool(app.sync_activity)
        crowded = app.size.width < 54 and self.query_one(".source-hint").display
        st.display = not crowded or active
        self.query_one(".pfoot").set_class(crowded and active, "stacked")
        if active_job:
            frame = SPIN[int(time.monotonic() * 12) % len(SPIN)]
            done = sum(1 for s in job.steps if s.state in ("done", "skip", "fail"))
            elapsed = int(time.monotonic() - job.started)
            detail = next((s.detail for s in job.steps if s.state == "running" and s.detail), "")
            label = detail or f"{job.title} · {done}/{len(job.steps)}"
            if job.status == "stopping":
                label = "Зогсоож байна…"
            st.update(
                Text.assemble(
                    (frame + " ", rgb(BLUE)),
                    (f"{label} · {elapsed}с", rgb(TEXT)),
                )
            )
        elif app.sync_activity:
            st.update(tx(sync_phase_text(app.sync_activity), BLUE))
        else:
            st.update(tx(app.archive_state, MUTED))

    @property
    def inp(self) -> PromptInput:
        return self.query_one(PromptInput)

    def focus_input(self) -> None:
        self.screen.set_focus(self.inp, scroll_visible=False)

    # — popup —
    def update_popup(self, value: str) -> None:
        popup = self.app.popup
        if not value.startswith("/") or " " in value:
            popup.hide()
            return
        q = value[1:].lower()
        items = [
            Opt(c.name, ("/" + c.name).ljust(15), c.desc, MUTED)
            for c in COMMANDS
            if not q or q in c.name or any(q in a for a in c.aliases) or q in c.title.lower()
        ]
        if not items:
            popup.hide()
            return
        popup.show(items, self.region, self._popup_pick)

    def _popup_pick(self, opt: Opt) -> None:
        self.app.popup.hide()
        self.inp.value = ""
        self.app.run_cmd(opt.value, "")

    @on(Input.Changed)
    def _changed(self, e: Input.Changed) -> None:
        e.stop()
        self.update_popup(e.value)

    # — товчлуурууд —
    def kb_up(self) -> None:
        if self.app.popup.is_open:
            self.app.popup.move(-1)
        elif self.app.route == "prov" and not self.inp.value:
            self.app.prov_move(-1)
        elif self.history:
            self.hist_i = min(len(self.history) - 1, self.hist_i + 1)
            self.inp.value = self.history[-1 - self.hist_i]
            self.inp.cursor_position = len(self.inp.value)

    def kb_down(self) -> None:
        if self.app.popup.is_open:
            self.app.popup.move(1)
        elif self.app.route == "prov" and not self.inp.value:
            self.app.prov_move(1)
        elif self.hist_i >= 0:
            self.hist_i -= 1
            self.inp.value = self.history[-1 - self.hist_i] if self.hist_i >= 0 else ""
            self.inp.cursor_position = len(self.inp.value)

    def kb_tab(self) -> None:
        popup = self.app.popup
        if popup.is_open and (o := popup.chosen()):
            self.inp.value = f"/{o.value} "
            self.inp.cursor_position = len(self.inp.value)
            popup.hide()
        else:
            self.app.screen.focus_next()

    def kb_esc(self) -> None:
        if self.app.popup.is_open:
            self.app.popup.hide()
        elif self.inp.value:
            self.inp.value = ""
        else:
            self.app.on_escape()

    def kb_enter(self) -> None:
        app, popup = self.app, self.app.popup
        text = self.inp.value.strip()
        if popup.is_open and (o := popup.chosen()):
            popup.hide()
            self.inp.value = ""
            app.run_cmd(o.value, "")
            return
        if not text:
            if app.route == "prov" and app.size.width < 100:
                app.show_chat(app.sel.get(app.cur_prov), dialog=True)
            return
        self.history.append(text)
        self.hist_i = -1
        self.inp.value = ""
        if text.startswith("/"):
            name, _, args = text[1:].partition(" ")
            app.run_cmd(name, args.strip())
        else:
            app.ask(text)


# ───────────────── tab bar ─────────────────
class Tab(Static):
    def __init__(self, sid: str | None, title: str, active: bool, icon: str = "") -> None:
        label = Text(no_wrap=True, overflow="ellipsis")
        if icon:
            label.append(icon + " ", style=rgb(TEXT if active else MUTED))
        label.append(title, style=("bold " if active else "") + rgb(TEXT if active else MUTED))
        super().__init__(label, classes="tab" + (" active" if active else ""))
        self.sid = sid

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if event.button == 1:
            self.app.nav_tab(self.sid)
            self.app.call_after_refresh(self.app._refocus)


# ───────────────── session-ийн блокууд ─────────────────
class UserBlock(Static):
    def __init__(
        self, body: RenderableType, color=BLUE, cited: bool = False, label: Text | None = None
    ) -> None:
        if isinstance(body, str):
            body = tx(body)
        super().__init__(
            Group(label, body) if label else body, classes="ublock" + (" cited" if cited else "")
        )
        if not cited:
            self.styles.border_left = ("heavy", hx(color))


class AiBlock(Static):
    def __init__(self, body: RenderableType, cited: bool = False) -> None:
        super().__init__(body, classes="ablock" + (" cited" if cited else ""))


class SourceRow(Static):
    def __init__(self, citation: Citation) -> None:
        ok = bool(citation.message_id or citation.passage)
        prefix = f"↳ #{citation.number:04d} · " if citation.message_id else "↳ Ишлэл · "
        t = Text.assemble(
            (prefix, rgb(GREEN if ok else MUTED)),
            (citation.label, rgb(TEXT if ok else MUTED)),
            (" · хуучин хувилбар", rgb(YELLOW)) if citation.stale else "",
            no_wrap=True,
            overflow="ellipsis",
        )
        super().__init__(t, classes="srow" + ("" if ok else " dim"))
        self.citation = citation

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app.open_citation(self.citation)


class Panel(Static):
    def __init__(self, color=BLUE, **kw: Any) -> None:
        super().__init__("", classes="panel", **kw)
        self.styles.border_left = ("heavy", hx(color))


class ChatList(VerticalScroll):
    """Provider tab-ын чатуудын жагсаалт (OpenCode-ийн sessions мөртэй адил). ↑↓ + mouse."""

    markable = False
    multi = False

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.rows: list[Row] = []
        self.items: list[Opt] = []
        self.active = -1
        self.on_select: Callable[[Any, bool], None] | None = None

    def set_items(self, opts: list[Opt], active_value: Any) -> None:
        if (
            opts == self.items
            and 0 <= self.active < len(opts)
            and opts[self.active].value == active_value
        ):
            return
        self.items = opts
        self.remove_children()
        widgets: list[Any] = []
        self.rows, last = [], None
        for i, o in enumerate(opts):
            if o.category and o.category != last:
                widgets.append(Static(Text(o.category, style="bold " + rgb(PURPLE)), classes="cat"))
                last = o.category
            r = Row(self, i, o)
            self.rows.append(r)
            widgets.append(r)
        if widgets:
            self.mount(*widgets)
        self.active = -1
        idx = next((i for i, o in enumerate(opts) if o.value == active_value), 0 if opts else -1)
        self.call_after_refresh(lambda: self._activate(idx))

    def _activate(self, i: int) -> None:
        if not (0 <= i < len(self.rows)):
            return
        if 0 <= self.active < len(self.rows):
            self.rows[self.active].set_active(False)
        self.active = i
        self.rows[i].set_active(True)
        self.rows[i].scroll_visible(animate=False)

    def hover(self, idx: int) -> None:
        pass

    def pick(self, idx: int) -> None:
        self.choose(idx, True)
        self.app.call_after_refresh(self.app._refocus)

    def choose(self, idx: int, click: bool) -> None:
        if 0 <= idx < len(self.items):
            self._activate(idx)
            if self.on_select:
                self.on_select(self.items[idx].value, click)

    def move(self, d: int) -> None:
        if self.rows:
            self.choose(max(0, min(len(self.rows) - 1, max(self.active, 0) + d)), False)


class ChatDialog(ModalScreen):
    """Нарийн terminal-д чатыг бүтэн дэлгэцээр уншина."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape,q", "close", show=False)]

    def __init__(self, chat: Chat, cite: int = 0) -> None:
        super().__init__()
        self.chat, self.cite = chat, cite

    def compose(self) -> ComposeResult:
        with Vertical(id="cdlg"):
            with Horizontal(id="dhead"):
                yield Static(
                    Text(clip(self.chat.title, 40), style="bold " + rgb(TEXT)), id="dtitle"
                )
                yield Click(tx("esc", MUTED), self.action_close, id="desc")
            yield VerticalScroll(id="cmsgs")

    def on_mount(self) -> None:
        ws, cited = self.app.chat_widgets(self.chat, self.cite)
        content = self.query_one("#cmsgs", VerticalScroll)
        content.mount(*ws)
        content.focus()
        if cited is not None:
            self.set_timer(0.25, lambda: cited.scroll_visible(animate=True))

    def refresh_live(self) -> None:
        current = self.app.chat(self.chat.id)
        if current != replace(self.chat, messages=None) and self.query("#cmsgs"):
            self.run_worker(self.reload(current), group="chat-dialog", exclusive=True)

    async def reload(self, current: Chat | None) -> None:
        try:
            messages = (
                await asyncio.to_thread(self.app.backend.messages, current.id) if current else []
            )
        except BackendError:
            return
        if not self.is_mounted or self.app.chat(self.chat.id) != current:
            return
        content = self.query_one("#cmsgs", VerticalScroll)
        scroll_y = content.scroll_y
        await content.remove_children()
        if current is None:
            await content.mount(Static(tx("Чат архиваас олдсонгүй.", MUTED)))
            return
        self.chat = replace(current, messages=messages)
        self.query_one("#dtitle", Static).update(tx(clip(current.title, 40), TEXT, True))
        widgets, _ = self.app.chat_widgets(self.chat, self.cite)
        await content.mount(*widgets)
        self.call_after_refresh(lambda: content.scroll_to(y=scroll_y, animate=False))

    def action_close(self) -> None:
        self.dismiss(None)


# ───────────────── ажил (sync, шалгалт) ─────────────────
@dataclass
class Step:
    label: str
    state: str = "pending"  # pending | running | done | fail | skip
    detail: str = ""


@dataclass
class Job:
    title: str
    steps: list[Step] = field(default_factory=list)
    status: str = "running"
    error_code: str = ""
    notes: list[Text] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    automatic: bool = False


class SearchInput(Input):
    """Dialog-ийн хайлт: ctrl+a = бүгдийг сонгох (Input-ийн home-ыг дарна)."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("ctrl+a", "all", show=False)]

    def action_all(self) -> None:
        fn = getattr(self.screen, "mark_all", None)
        if fn:
            fn()


# ───────────────── апп ─────────────────
class Mem(App):
    TITLE = "mem"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #0a0a0a; layers: base popup; color: #eeeeee; }
    * { scrollbar-background: #0a0a0a; scrollbar-background-hover: #0a0a0a; scrollbar-background-active: #0a0a0a;
        scrollbar-color: #2a2a2a; scrollbar-color-hover: #484848; scrollbar-color-active: #808080;
        scrollbar-size-vertical: 1; scrollbar-size-horizontal: 0; }
    #tabs, #views, #statusbar { layer: base; }
    #tabs { height: 1; overflow-y: hidden; }
    .tab { width: auto; max-width: 28; height: 1; padding: 0 1; color: #808080; }
    .tab:hover { background: #141414; }
    .tab.active { background: #1e1e1e; color: #eeeeee; }
    #views { height: 1fr; }
    #statusbar { height: 1; padding: 0 2; }
    #statusbar > Click, #statusbar > Static { width: auto; height: 1; margin-right: 3; }
    #statusbar > #sb-fill { width: 1fr; margin-right: 0; }
    #statusbar.compact > #sb-prov, #statusbar.compact > #sb-ver { display: none; }

    #home { align: center middle; }
    #logo { width: 100%; max-width: 75; height: auto; margin-bottom: 1; text-align: center; }
    #notice { width: 100%; max-width: 75; height: auto; max-height: 2; margin-bottom: 1; text-align: center; }
    #p-home { width: 100%; max-width: 75; height: auto; }

    .prompt { height: auto; }
    .pbox { height: auto; background: #1c1c1c; border-left: heavy #5c9cf5; padding: 1 2 0 2; }
    PromptInput.pinput { height: 1; border: none; padding: 0; background: transparent; color: #eeeeee; }
    PromptInput.pinput:focus { background: transparent; border: none; }
    PromptInput.pinput > .input--placeholder { color: #808080; }
    PromptInput.pinput > .input--cursor { background: #eeeeee; color: #0a0a0a; text-style: none; }
    PromptInput.pinput > .input--selection { background: #484848; }
    .pmeta { height: 2; padding-top: 1; }
    .pmeta > * { width: auto; height: 1; }
    #views.tiny .pbox { padding-left: 1; padding-right: 1; }
    #views.tiny .phints > Hint { margin-left: 1; }
    .pedge { height: 1; }
    .pfoot { height: 1; }
    .pfoot.stacked { layout: vertical; height: 2; }
    .pstatus { width: 1fr; height: 1; }
    .phints { width: auto; height: 1; }
    .phints > Hint { width: auto; margin-left: 2; }

    #session { height: 1fr; }
    #sess-main { width: 1fr; }
    #msgs { height: 1fr; padding: 0 2; }
    #p-sess { padding: 0 2; }
    #sidebar { width: 40; height: 100%; background: #141414; padding: 1 2; }
    #views.narrow #sidebar { display: none; }
    .ublock { background: #141414; padding: 1 2; margin-top: 1; height: auto; }
    .ublock.cited { background: #1e1e1e; border-left: heavy #fab283; }
    .ablock { padding: 0 0 0 3; margin-top: 1; height: auto; }
    .ablock.cited { background: #1e1e1e; border-left: heavy #fab283; padding-left: 2; }
    .srow { height: 1; padding: 0 0 0 0; }
    .srow:hover { background: #141414; }
    .chead { height: auto; margin-top: 1; }
    .chactions { height: 1; margin-top: 1; }
    .chactions > Hint { width: auto; margin-right: 3; }

    #dash { height: 1fr; }
    #d-body { height: 1fr; padding: 1 2 0 2; }
    #d-head { height: 1; margin-bottom: 1; }
    .row2 { height: auto; }
    .row2 > .panel { width: 1fr; margin-right: 1; }
    .row2 > .panel:last-child { margin-right: 0; }
    .panel { background: #141414; padding: 1 2; height: auto; margin-bottom: 1; }
    #views.narrow .row2 { layout: vertical; }
    #views.narrow .row2 > .panel { width: 100%; margin-right: 0; }
    #p-dash { padding: 0 2; }

    DialogSelect, JobDialog, DialogInfo, ChatDialog, NotebookDialog, NotebookForm {
        align: center top; background: #000000 60%;
    }
    #cdlg { width: 96%; height: 90%; margin-top: 1; background: #141414; padding: 1 2; }
    #cmsgs { height: 1fr; }
    #prov { height: 1fr; }
    #prov-body { height: 1fr; }
    #plist-card { width: 46; background: #141414; padding: 1 1 0 2; }
    #plist-head { height: 1; }
    #pfilters { height: 1; margin: 1 0 0 0; }
    #pfilters > Click { width: auto; margin-right: 2; }
    #plist { height: 1fr; margin-top: 0; }
    #pread-card { width: 1fr; padding: 0 2; }
    #pmsgs { height: 1fr; }
    #p-prov { padding: 0 2; }
    #views.narrow #pread-card { display: none; }
    #views.narrow #plist-card { width: 100%; }
    .row:hover { background: #1e1e1e; }
    .row.active, .row.active:hover { background: #fab283; }
    #dlg { background: #141414; height: auto; max-height: 92%; padding: 1 2 1 2; }
    DialogInfo #dlg { max-height: 100%; }
    #dhead { height: 1; }
    #dtitle { width: 1fr; }
    #desc { width: auto; }
    #dsearch, SearchInput { height: 1; border: none; padding: 0; background: transparent; margin-top: 1; color: #eeeeee; }
    #dsearch:focus { background: transparent; border: none; }
    #dsearch > .input--placeholder { color: #808080; }
    #dsearch > .input--cursor { background: #eeeeee; color: #0a0a0a; text-style: none; }
    #dlist, .dialog-scroll { height: auto; margin-top: 1; }
    .cat { height: 1; margin-top: 1; }
    .row { height: 1; padding: 0 1; }
    .row.active { background: #fab283; }
    #dnote { height: auto; }
    #dhints { height: auto; margin-top: 1; }
    #dhints > Hint { width: auto; margin-right: 3; }
    NotebookDialog #dlg, NotebookForm #dlg { max-height: 100%; }
    .nb-heading { height: 1; text-style: bold; }
    #nb-scroll { height: auto; margin-top: 1; }
    .nb-text, .nb-field { height: auto; }
    #nb-validation { color: #e06c75; }
    #nb-scroll Input {
        height: 1; border: none; border-left: solid #484848;
        background: #1c1c1c; padding: 0 1;
    }
    #nb-scroll Input:focus { border-left: solid #fab283; }
    .nb-actions { height: 2; margin-top: 1; }
    .nb-actions Button {
        height: 1; width: auto; min-width: 0; padding: 0 1; margin-right: 1;
        border: none; background: #303030; color: #eeeeee;
    }
    .nb-actions Button:focus { background: #fab283; color: #0a0a0a; }
    #ibody, #jbody, #jbar { height: auto; }
    #popup { layer: popup; background: #1e1e1e; height: auto; }

    Toast { background: #141414; color: #eeeeee; border: none; border-left: heavy #56b6c2; border-right: heavy #56b6c2;
            padding: 0 1; width: auto; max-width: 60; }
    Toast.-warning { border-left: heavy #f5a742; border-right: heavy #f5a742; }
    Toast.-error { border-left: heavy #e06c75; border-right: heavy #e06c75; }
    Toast .toast--title { display: none; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+p", "palette", show=False, priority=True),
        Binding("ctrl+n", "new", show=False, priority=True),
        Binding("ctrl+o", "sources", show=False, priority=True),
        Binding("ctrl+w", "close_current", show=False, priority=True),
        Binding("ctrl+x", "stop", show=False, priority=True),
        Binding("ctrl+c", "ctrl_c", show=False, priority=True),
    ]

    def __init__(self, backend: Backend | None = None) -> None:
        super().__init__()
        self.console.push_theme(
            Theme(
                {
                    "markdown.h2": f"bold underline {hx(TEXT)}",
                    "markdown.h3": f"bold {hx(TEXT)}",
                    "markdown.h4": f"italic {hx(TEXT)}",
                    "markdown.h6": hx(TEXT),
                }
            )
        )
        self.backend = backend if backend is not None else Backend()
        self.chats: list[Chat] = []
        self.chat_index: dict[str, Chat] = {}
        self.available_providers = list(WEB_PROVIDERS)
        self.providers = set(WEB_PROVIDERS)
        self.scope_selected = False
        self.account_counts: dict[str, int] = {}
        self.archive_status: dict[str, Any] = {}
        self.sync_activity: dict[str, Any] = {}
        self.connection_status: dict[str, Any] = {}
        self.notebook_status: dict[str, Any] = {}
        self.last_notebook_check: dict[str, Any] = {}
        self.archive_error = ""
        self.archive_error_code = ""
        self.archive_loading = True
        self.archive_stamp = None
        self.archive_lock = asyncio.Lock()
        self.auto_sync = False
        self.background_sync = self.backend.background_sync_enabled()
        self.auto_sync_due: float | None = None
        self.auto_retry_after = 0.0
        self.reader_key: tuple[str, int, Citation | None] | None = None
        self.ask_workers: dict[str, Any] = {}
        self.job_worker = None
        self.sessions: dict[str, Sess] = {}
        self.cur_prov = "ChatGPT"
        self.sel: dict[str, str] = {}
        self.pfilter = "all"
        self.cite_id: str | None = None
        self.citation: Citation | None = None
        self.viewed: dict[str, float] = {}
        self.route, self.prev_route = "home", "home"
        self.cur_sid: str | None = None
        self.job: Job | None = None
        self.spin = 0
        self.quit_armed = 0.0
        self.events: list[tuple[str, float, str]] = []
        self.popup = Popup()

    @property
    def archive_state(self) -> str:
        if self.archive_loading:
            return "Архив уншиж байна…"
        if self.archive_error:
            return "Архив · алдаа"
        return f"SQLite · {len(self.chats)} чат"

    def archive_signature(self) -> tuple:
        stamps = []
        db = self.backend.db
        for path in (
            db,
            db.with_name(db.name + "-wal"),
            db.with_name(db.name + ".sync-progress.json"),
        ):
            try:
                st = path.stat()
                stamps.append((st.st_ino, st.st_size, st.st_mtime_ns))
            except OSError:
                stamps.append(None)
        return tuple(stamps)

    async def refresh_archive(self, force: bool = False) -> bool:
        async with self.archive_lock:
            stamp = self.archive_signature()
            if not force and stamp == self.archive_stamp:
                return not self.archive_error
            old_ids = set(self.chat_index)
            had_snapshot = not self.archive_loading
            try:
                snapshot = await asyncio.to_thread(self.backend.snapshot)
            except BackendError as error:
                if self.archive_error != str(error):
                    self.note("ERR", "Архив · " + error.code)
                self.archive_error = str(error)
                self.archive_error_code = error.code
                if error.code == "archive_busy":
                    stamp = None
            else:
                visible_id = self.sel.get(self.cur_prov)
                previous = self.chat_index.get(visible_id) if visible_id else None
                self.chats = [Chat(**row) for row in snapshot["chats"]]
                self.chat_index = {chat.id: chat for chat in self.chats}
                if visible_id and self.chat_index.get(visible_id) != previous:
                    self.reader_key = None
                accounts: dict[str, set[str]] = {}
                for chat in self.chats:
                    accounts.setdefault(chat.ai, set()).add(chat.account)
                self.account_counts = {name: len(ids) for name, ids in accounts.items()}
                names = set(accounts)
                self.available_providers = list(dict.fromkeys((*WEB_PROVIDERS, *sorted(names))))
                if not self.scope_selected:
                    self.providers = set(self.available_providers)
                self.archive_status = snapshot["status"]
                self.sync_activity = snapshot.get("sync_activity") or {}
                self.auto_sync = snapshot.get("auto_sync", False)
                self.auto_sync_due = snapshot.get("auto_sync_due")
                self.last_notebook_check = snapshot.get("notebook_identity", {})
                if (
                    self.notebook_status.get("id")
                    and "notebook_id" in snapshot
                    and self.notebook_status["id"] != snapshot["notebook_id"]
                ):
                    self.notebook_status = {}
                connection = snapshot["connection"]
                if (
                    connection.get("state") == "configured"
                    and self.connection_status.get("transport") == "http"
                    and connection.get("port") == self.connection_status.get("port")
                    and connection.get("connection_file")
                    == self.connection_status.get("connection_file")
                ):
                    connection["state"] = self.connection_status.get("state", "configured")
                self.connection_status = connection
                self.archive_error = ""
                self.archive_error_code = ""
                added = len(self.chat_index.keys() - old_ids)
                if had_snapshot and added:
                    self.note("NEW", f"Архивт {added} шинэ чат нэмэгдлээ")
            self.archive_stamp = stamp
            self.archive_loading = False
            self.refresh_all()
            self.poll_auto_sync()
            return not self.archive_error

    def poll_archive(self) -> None:
        if self.archive_lock.locked():
            return
        active = bool(self.sync_activity)
        if active or self.archive_signature() != self.archive_stamp:
            self.run_worker(self.refresh_archive(force=active), group="archive")
        else:
            self.poll_auto_sync()

    def poll_auto_sync(self) -> None:
        if (
            not self.auto_sync
            or self.background_sync
            or self.auto_sync_due is None
            or self.auto_sync_due > time.time()
            or time.monotonic() < self.auto_retry_after
            or self.archive_error
            or self.sync_activity
            or self._exit
            or not self.is_running
            or (self.job and self.job.status in {"running", "stopping"})
            or isinstance(self.screen, JobDialog)
            or any(worker.group == "auto-setting" for worker in self.workers)
        ):
            return
        if (self.notebook_status.get("error") or {}).get(
            "code"
        ) == "auth_required" or self.notebook_status.get("can_write") is False:
            return
        self.start_job("auto")

    def chat(self, cid: str) -> Chat | None:
        return self.chat_index.get(cid)

    def scoped(self) -> list[Chat]:
        return [c for c in self.chats if c.ai in self.providers]

    def stats(self, *, all_chats: bool = False) -> dict[str, int]:
        cs = self.chats if all_chats else self.scoped()
        return {
            "total": len(cs),
            "inbox": sum(not c.local for c in cs),
            "local": sum(c.local for c in cs),
            "syncable": sum(c.local and c.message_count > 0 for c in cs),
            "failed": sum(c.nb == "failed" for c in cs),
            "done": sum(c.fully_synced for c in cs),
        }

    def note(self, kind: str, msg: str) -> None:
        self.events.insert(0, (kind, time.time(), msg))
        del self.events[12:]

    def toast(self, msg: str, kind: str = "information") -> None:
        self.notify(msg, severity=kind, timeout=4)

    def q(self, selector: str, widget_type: Any = None) -> Any:
        base = self.screen_stack[0]
        return base.query_one(selector, widget_type) if widget_type else base.query_one(selector)

    @property
    def prompts(self) -> list[Prompt]:
        return list(self.screen_stack[0].query(Prompt))

    # ───── layout ─────
    def compose(self) -> ComposeResult:
        yield HorizontalScroll(id="tabs")
        with ContentSwitcher(initial="home", id="views"):
            with Vertical(id="home"):
                yield Static(Text(""), id="logo")
                yield Click(Text(""), self.run_notice, id="notice")
                yield Prompt("p-home")
            with Horizontal(id="session"):
                with Vertical(id="sess-main"):
                    yield VerticalScroll(id="msgs")
                    yield Prompt("p-sess")
                yield Static(Text(""), id="sidebar")
            with Vertical(id="prov"):
                with Horizontal(id="prov-body"):
                    with Vertical(id="plist-card"):
                        yield Static(Text(""), id="plist-head")
                        with Horizontal(id="pfilters"):
                            yield Click(Text(""), lambda: self.set_filter("all"), id="pf-all")
                            yield Click(Text(""), lambda: self.set_filter("wait"), id="pf-wait")
                            yield Click(Text(""), lambda: self.set_filter("done"), id="pf-done")
                        yield ChatList(id="plist")
                    with Vertical(id="pread-card"):
                        yield VerticalScroll(id="pmsgs")
                yield Prompt("p-prov")
            with Vertical(id="dash"):
                with VerticalScroll(id="d-body"):
                    yield Static(Text(""), id="d-head")
                    with Horizontal(classes="row2"):
                        yield Panel(CYAN, id="d-recv")
                        yield Panel(PEACH, id="d-nb")
                    with Horizontal(classes="row2"):
                        yield Panel(BLUE, id="d-flow")
                        yield Panel(PURPLE, id="d-feed")
                yield Prompt("p-dash")
        with Horizontal(id="statusbar"):
            yield Click(Text(""), self.cmd_connect, id="sb-conn")
            yield Click(Text(""), self.cmd_provider, id="sb-prov")
            yield Static(Text(""), id="sb-fill")
            yield Static(Text(""), id="sb-auto")
            yield Static(tx(f"mem {VERSION}", MUTED), id="sb-ver")
        yield self.popup

    def on_mount(self) -> None:
        self.refresh_all()
        self._apply_size(self.size.width, self.size.height)
        self.set_interval(1 / 12, self._tick)
        self.set_interval(3, self.poll_archive)
        self.set_interval(10, self.poll_connection)
        self.set_interval(60, self.poll_notebook)
        self.run_worker(self.load_initial_status(), group="archive")
        self.call_after_refresh(self._refocus)
        self.q("#plist", ChatList).on_select = self.on_list_pick

    async def load_initial_status(self) -> None:
        if await self.refresh_archive(force=True):
            self.poll_notebook()

    def on_resize(self, event: events.Resize) -> None:
        self._apply_size(event.size.width, event.size.height)
        self.call_after_refresh(self.refresh_all)

    def _apply_size(self, w: int, h: int) -> None:
        try:
            views = self.q("#views", ContentSwitcher)
            views.set_class(w < 100, "narrow")
            views.set_class(w < 48, "tiny")
            self.q("#statusbar").set_class(w < 78, "compact")
        except NoMatches:
            pass

    def _tick(self) -> None:
        self.spin += 1
        active_job = self.job and self.job.status in ("running", "stopping")
        if not isinstance(self.screen, ModalScreen) and (active_job or self.spin % 12 == 0):
            for p in self.prompts:
                p.refresh_status()
        if self.route == "session" and self.spin % 3 == 0:
            sess = self.cur_sess()
            if sess and sess.id in self.ask_workers:
                self.refresh_tail(sess)

    def poll_notebook(self) -> None:
        if (
            not self.archive_status.get("notebook_configured")
            or self.archive_error
            or self.sync_activity
            or (self.job and self.job.status in {"running", "stopping"})
            or any(worker.group == "notebook-status" for worker in self.workers)
        ):
            return
        self.run_worker(self.refresh_notebook_status(), group="notebook-status")

    def poll_connection(self) -> None:
        if (
            self.archive_loading
            or self.archive_error
            or any(worker.group == "connection-status" for worker in self.workers)
        ):
            return
        self.run_worker(self.refresh_connection_status(), group="connection-status")

    async def refresh_connection_status(self) -> None:
        try:
            connection = await asyncio.to_thread(self.backend.connection, probe=True)
        except BackendError:
            return
        if connection != self.connection_status:
            self.connection_status = connection
            self.refresh_all()

    async def refresh_notebook_status(self) -> None:
        try:
            result = await self.backend.call("check")
        except BackendError as error:
            result = {"error": {"code": error.code, "message": str(error)}}
        self.update_notebook_status(result)
        await self.refresh_archive(force=True)

    def update_notebook_status(self, result: dict) -> None:
        notebook = result.get("notebook") or {"error": result.get("error")}
        if notebook.get("connected"):
            self.notebook_status = notebook
        else:
            self.notebook_status.update(notebook, connected=False)

    def dialog(self, dlg: ModalScreen, cb: Callable[[Any], None] | None = None) -> None:
        """Dialog нээж, хаагдсаны дараа prompt руу focus буцаана."""

        def done(res: Any) -> None:
            if cb:
                cb(res)
            self.call_after_refresh(self._refocus)

        self.push_screen(dlg, done)

    def _refocus(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self._focus_prompt()

    def _focus_prompt(self) -> None:
        pid = {"home": "#p-home", "session": "#p-sess", "dash": "#p-dash", "prov": "#p-prov"}[
            self.route
        ]
        try:
            self.q(pid, Prompt).focus_input()
        except NoMatches:
            pass

    # ───── routes / tabs ─────
    def go(self, route: str) -> None:
        if route != self.route:
            self.prev_route = self.route
            if self.route == "prov":
                self.reader_key = None
                self.workers.cancel_group(self, "reader")
        self.route = route
        self.q("#views", ContentSwitcher).current = route
        self.popup.hide()
        self._refocus()
        self.refresh_all()

    def nav_tab(self, sid: str | None) -> None:
        if sid is None:
            self.go("home")
        elif sid == "dash":
            self.go("dash")
        elif sid == "archive":
            self.cmd_chats()
        elif sid == "flow":
            self.cmd_flow()
        elif sid == "browse":
            self.browse_providers()
        elif sid.startswith("prov:"):
            self.open_provider(sid[5:])

    def open_session(self, sid: str) -> None:
        if sid not in self.sessions:
            return
        self.cur_sid = sid
        self.go("session")
        self.render_session()

    def action_close_current(self) -> None:
        if self.route == "dash":
            self.on_escape()

    def action_new(self) -> None:
        self.go("home")

    def on_escape(self) -> None:
        if self.route == "dash":
            if self.prev_route == "session" and self.cur_sid:
                self.open_session(self.cur_sid)
            else:
                self.go(self.prev_route if self.prev_route != "dash" else "home")
        elif self.route == "prov" and self.prev_route == "session" and self.cur_sid:
            self.open_session(self.cur_sid)
        elif self.route != "home":
            self.go("home")

    def scroll_content(self, direction: int) -> None:
        if self.route == "prov" and self.size.width < 100:
            self.prov_move(direction * 8)
            return
        selector = {"session": "#msgs", "dash": "#d-body", "prov": "#pmsgs"}.get(self.route)
        if selector:
            scroll = self.q(selector, VerticalScroll)
            if direction < 0:
                scroll.scroll_page_up(animate=False)
            else:
                scroll.scroll_page_down(animate=False)

    def action_ctrl_c(self) -> None:
        now = time.monotonic()
        if now - self.quit_armed < 2.0:
            self.exit()
        else:
            self.quit_armed = now
            self.toast("Гарахын тулд ctrl+c-г дахин дарна уу", "warning")

    def cur_sess(self) -> Sess | None:
        return self.sessions.get(self.cur_sid or "")

    def session_citations(self) -> list[Citation]:
        session = self.cur_sess() if self.route == "session" else None
        if not session:
            return []
        return list(
            dict.fromkeys(
                citation
                for message in reversed(session.msgs)
                for citation in message.get("sources", ())
                if citation.message_id or citation.passage
            )
        )

    # ───── refresh ─────
    def refresh_all(self) -> None:
        if self._exit or not self.is_running:
            return
        self.refresh_tabs()
        self.refresh_home()
        self.refresh_statusbar()
        if self.route == "session":
            self.refresh_sidebar()
        if self.route == "dash":
            self.refresh_dash()
        if self.route == "prov":
            self.render_prov()
        for p in self.prompts:
            p.refresh_meta()
            p.refresh_status()
        for screen in self.screen_stack[1:]:
            if isinstance(screen, FlowCanvas):
                screen.update_status(self.flow_counts(), self.flow_notebook_label())
            elif isinstance(screen, (DialogSelect, NotebookDialog, ChatDialog)):
                screen.refresh_live()

    def refresh_tabs(self) -> None:
        bar = self.q("#tabs")
        bar.remove_children()
        ws = [
            Tab(None, "Шинэ", self.route == "home", "+"),
            Tab("archive", "Архив", False),
            Tab("dash", "Төлөв", self.route == "dash"),
            Tab("flow", "Урсгал", False),
        ]
        if self.size.width < 80:
            ws.append(Tab("browse", "Үйлчилгээ", self.route == "prov"))
        else:
            ws.extend(
                Tab(f"prov:{p}", p, self.route == "prov" and self.cur_prov == p)
                for p in self.available_providers
                if p in self.providers
            )
        bar.mount(*ws)

    def notice(self) -> tuple[str, str]:
        if self.archive_loading or self.archive_error_code == "archive_busy":
            return "", ""
        if self.archive_error_code == "archive_missing":
            return "setup", "NotebookLM болон архив тохируулах"
        if self.archive_error:
            return "dashboard", self.archive_error
        if not self.archive_status.get("notebook_configured"):
            return "setup", "NotebookLM холбох"
        if self.sync_activity or (self.job and self.job.status in {"running", "stopping"}):
            return "", ""
        error = self.notebook_status.get("error") or {}
        if error.get("code") == "auth_required":
            return "login", "NotebookLM-д дахин нэвтрэх"
        if error:
            return "check", self.notebook_label()[0]
        if self.notebook_status.get("connected") and self.notebook_status.get("can_write") is False:
            return "login", "NotebookLM-д засах эрхтэй аккаунтаар нэвтрэх"
        s = self.stats(all_chats=True)
        if (
            s["failed"]
            or self.archive_status.get("sync_errors")
            or self.archive_status.get("sync_preflight_errors")
        ):
            return "check", "NotebookLM sync-ийн алдаа байна"
        if remaining := s["syncable"] - s["done"]:
            return "dashboard", f"NotebookLM рүү илгээх {remaining} чат байна"
        work = []
        if segments := self.archive_status.get("pending_segments"):
            work.append(f"{segments} багц шинэчлэх")
        if messages := self.archive_status.get("unassigned_messages"):
            work.append(f"{messages} мессеж нэмэх")
        if work:
            return "dashboard", " · ".join(work)
        if jobs := self.archive_status.get("pending_jobs"):
            return "dashboard", f"{jobs} sync ажил хүлээгдэж байна"
        return "", ""

    def run_notice(self) -> None:
        if command := self.notice()[0]:
            self.run_cmd(command, "")

    def refresh_home(self) -> None:
        w = self.size.width if self.size.height >= 16 else 0
        self.q("#logo", Static).update(logo_text(w))
        _, text = self.notice()
        notice = self.q("#notice", Click)
        notice.display = bool(text)
        if text:
            notice.update(Text(text, style=rgb(PEACH), justify="center"))

    def connection_label(self) -> tuple[str, tuple]:
        conn = self.connection_status
        if conn.get("native_registered"):
            return "Native Messaging бүртгэлтэй", GREEN
        if conn.get("transport") == "http":
            state, port = conn.get("state"), conn.get("port")
            label, color = {
                "online": ("Receiver ажиллаж байна", GREEN),
                "offline": ("Receiver зогссон", MUTED),
                "invalid": ("Receiver шалгалт амжилтгүй", RED),
                "configured": ("Receiver тохируулсан", CYAN),
            }.get(state, ("Receiver-ийн төлөв тодорхойгүй", MUTED))
            return f"{label} · :{port}" if port else label, color
        if self.archive_loading:
            return "Холболтын төлөв уншиж байна", MUTED
        return "Extension бүртгэл олдсонгүй", MUTED

    def notebook_label(self) -> tuple[str, tuple]:
        if self.notebook_status.get("connected"):
            return "NotebookLM шалгалт амжилттай", GREEN
        if error := self.notebook_status.get("error"):
            if error.get("code") == "auth_required":
                return "NotebookLM нэвтрэлт шаардлагатай · /login", YELLOW
            label = {
                "network_error": "Сүлжээний холболт тасарсан",
                "timeout": "Холболтын хугацаа хэтэрсэн",
                "notebook_not_found": "Notebook олдсонгүй",
                "account_unverified": "Google аккаунтыг баталгаажуулж чадсангүй",
            }.get(error.get("code"), "NotebookLM холболт амжилтгүй")
            return label, RED
        if self.archive_status.get("notebook_configured"):
            return "NotebookLM тохируулсан · /check", CYAN
        if self.archive_loading:
            return "NotebookLM тохиргоо уншиж байна", MUTED
        return "NotebookLM тохируулаагүй", MUTED

    def refresh_statusbar(self) -> None:
        label, color = self.connection_label()
        self.q("#sb-conn", Click).update(
            Text.assemble(
                ("⊙ ", rgb(color)), (clip(label, max(8, min(33, self.size.width - 21))), rgb(TEXT))
            )
        )
        self.q("#sb-prov", Click).update(tx(f"{len(self.providers)} provider", MUTED))
        active = self.job and self.job.automatic and self.job.status in {"running", "stopping"}
        label = "Auto …" if active else "Auto ON" if self.auto_sync else "Auto OFF"
        self.q("#sb-auto", Static).update(
            tx(label, BLUE if active else GREEN if self.auto_sync else MUTED)
        )

    def refresh_sidebar(self) -> None:
        s, st = self.cur_sess(), self.stats()
        t = Text()
        if s:
            t.append(clip(s.title, 34) + "\n\n", style="bold " + rgb(TEXT))
            t.append("▾ ", style=rgb(MUTED))
            t.append("Эх сурвалж\n" if s.kind == "ask" else "Чат\n", style="bold " + rgb(TEXT))
            if s.kind == "ask":
                srcs = [x for m in s.msgs if m["role"] == "ai" for x in m.get("sources", [])]
                for citation in srcs[-6:]:
                    ok = bool(citation.message_id)
                    t.append("  " + ("✔ " if ok else "○ "), style=rgb(GREEN if ok else MUTED))
                    t.append(clip(citation.label, 30) + "\n", style=rgb(TEXT if ok else MUTED))
                if not srcs:
                    t.append("  одоохондоо алга\n", style=rgb(MUTED))
            else:
                c = self.chat(s.chat_id or "")
                if c:
                    t.append("  " + c.ai + "\n", style=rgb(PROV.get(c.ai, TEXT)))
                    t.append(f"  {c.message_count} мессеж\n", style=rgb(MUTED))
        t.append("\n▾ ", style=rgb(MUTED))
        t.append("Sync\n", style="bold " + rgb(TEXT))
        t.append(f"  Локал       {st['local']}/{st['total']}\n", style=rgb(TEXT))
        t.append(f"  Бүрэн шинэчлэгдсэн  {st['done']}/{st['syncable']}\n", style=rgb(TEXT))
        t.append("\n▾ ", style=rgb(MUTED))
        t.append("Provider\n", style="bold " + rgb(TEXT))
        for p in self.available_providers:
            on = p in self.providers
            t.append("  ● " if on else "  ○ ", style=rgb(PROV.get(p, TEXT) if on else MUTED))
            t.append(p + "\n", style=rgb(TEXT if on else MUTED))
        self.q("#sidebar", Static).update(t)

    def refresh_dash(self) -> None:
        conn, conn_color = self.connection_label()
        notebook, nb_color = self.notebook_label()
        self.q("#d-head", Static).update(tx("Архивын төлөв", TEXT, True))
        recv = Text("\n").join(
            [
                tx("Локал архив", TEXT, True),
                tx(self.archive_state, TEXT),
                tx(self.archive_error or str(self.backend.db), MUTED),
                tx(conn, conn_color),
                tx("Brave · AI Memory → Sync now", MUTED),
            ]
        )
        remote_count = self.notebook_status.get("sources")
        if remote_count is not None:
            remote_label = f"Сүүлд шалгахад Google-д нийт {remote_count} эх"
        else:
            remote_label = "Google-ийн нийт эхийг /check-ээр шалгана"
        eligible = self.archive_status.get("last_verified_remote", {}).get("eligible_sources")
        if eligible is None:
            source_label = "Эх сурвалжийн төлөв тодорхойгүй"
        elif eligible:
            source_label = f"Ашиглах эх сурвалж · {eligible}"
        else:
            source_label = "Ашиглах эх сурвалж алга"
        nb = Text("\n").join(
            [
                tx("NotebookLM", TEXT, True),
                tx(notebook, nb_color),
                tx(source_label, YELLOW if eligible == 0 else TEXT),
                tx(remote_label, MUTED),
                *([tx(sync_phase_text(self.sync_activity), BLUE)] if self.sync_activity else []),
                tx(
                    f"Шинэчлэх багц · {self.archive_status.get('pending_segments', 0)}",
                    MUTED,
                ),
                tx(
                    f"Нэмэх мессеж · {self.archive_status.get('unassigned_messages', 0)}",
                    MUTED,
                ),
            ]
        )
        auto = "асаалттай" if self.auto_sync else "унтраалттай"
        when = "mem хаалттай үед ч ажиллана" if self.background_sync else "mem нээлттэй үед"
        saved = self.archive_status.get("conversations", 0)
        ready = self.archive_status.get("ready_conversations", 0)
        pending = self.archive_status.get("pending_conversations", 0)
        empty = saved - ready - pending
        flow = Text("\n").join(
            [
                tx("Синкийн төлөв", TEXT, True),
                tx(f"Хадгалсан чат · {saved}", CYAN),
                tx(f"NotebookLM-д бэлэн · {ready}", GREEN),
                tx(f"Хүлээгдэж буй чат · {pending}", YELLOW),
                *([tx(f"Мессежгүй чат · {empty}", MUTED)] if empty > 0 else []),
                tx(f"Автомат синк {auto} · {when}", MUTED),
            ]
        )
        col = {"OK": GREEN, "INFO": CYAN, "WARN": ORANGE, "ERR": RED, "NEW": PURPLE}
        events = [
            Text.assemble(
                (f"{k:<4}", "bold " + rgb(col[k])),
                "  ",
                (time.strftime("%H:%M", time.localtime(ts)), rgb(MUTED)),
                "  ",
                message,
            )
            for k, ts, message in self.events[:5]
        ]
        feed = Text("\n").join(
            [
                tx("Энэ ажиллуулалтын үйл явдал", TEXT, True),
                Text(""),
                *(events or [tx("Үйл явдал алга", MUTED)]),
            ]
        )
        for sel, content in (
            ("#d-recv", recv),
            ("#d-nb", nb),
            ("#d-flow", flow),
            ("#d-feed", feed),
        ):
            self.q(sel, Static).update(content)

    # ───── session render ─────
    def ai_text(self, msg: dict) -> RenderableType:
        if msg.get("pending"):
            frame = SPIN[self.spin % len(SPIN)]
            elapsed = int(time.monotonic() - msg["started"])
            return Text.assemble(
                (frame + " ", rgb(BLUE)),
                (f"NotebookLM · {elapsed}с · Ctrl+X", rgb(MUTED)),
            )
        footer = tx(f"NotebookLM · {msg.get('took', 0):.1f}с", MUTED)
        if code := msg.get("error_code"):
            footer.append(f" · {code}", style=rgb(RED))
            return Text.assemble(tx(msg["text"]), "\n\n", footer)
        warnings = []
        if msg.get("uncited"):
            warnings.append(tx("Эх сурвалжгүй хариулт", YELLOW, True))
        elif any(source.stale for source in msg.get("sources", ())):
            warnings.append(tx("Хуучин хувилбарын эх сурвалж ашигласан", YELLOW, True))
        return Group(*warnings, message_markdown(msg["text"]), Text(), footer)

    def render_session(self) -> None:
        s = self.cur_sess()
        box = self.q("#msgs", VerticalScroll)
        box.remove_children()
        if not s:
            return
        ws: list[Any] = []
        for m in s.msgs:
            if m["role"] == "user":
                ws.append(UserBlock(m["text"], BLUE))
            else:
                ws.append(AiBlock(self.ai_text(m)))
                if not m.get("pending"):
                    for citation in m.get("sources", []):
                        ws.append(SourceRow(citation))
        box.mount(*ws)
        prompt = self.q("#p-sess", Prompt)
        prompt.refresh_meta()
        prompt.refresh_status()
        self.call_after_refresh(lambda: box.scroll_end(animate=False))
        self.refresh_sidebar()

    # ───── provider tab: чатуудын жагсаалт + уншигч ─────
    def chat_widgets(self, c: Chat, cite: int = 0) -> tuple[list[Any], Any]:
        ws: list[Any] = list(self._chat_head(c))
        cited_w = None
        if not c.messages:
            ws.append(Static(tx("Энэ чатад хадгалсан мессеж алга.", MUTED)))
        for i, (who, text) in enumerate(c.messages or (), 1):
            is_cited = cite == i
            body = message_markdown(text)
            tag = Text.assemble(
                (f"#{i:04d}", rgb(MUTED)),
                ("  ИШЛЭЛ", "bold " + rgb(PEACH)) if is_cited else ("", ""),
            )
            if who == "Та":
                w: Any = UserBlock(body, BLUE, is_cited, label=tag)
            else:
                footer = Text.assemble(
                    ("▣  ", rgb(PROV.get(c.ai, BLUE))), (f"{c.ai} · ", rgb(MUTED)), tag
                )
                w = AiBlock(Group(body, Text(), footer), is_cited)
            if is_cited:
                cited_w = w
            ws.append(w)
        return ws, cited_w

    def prov_chats(self, name: str) -> list[Chat]:
        chats = sorted((c for c in self.chats if c.ai == name), key=lambda c: -c.ts)
        f = self.pfilter
        if f == "wait":
            return [c for c in chats if not c.fully_synced]
        if f == "done":
            return [c for c in chats if c.fully_synced]
        return chats

    def set_filter(self, f: str) -> None:
        self.pfilter = f
        self.render_prov()

    def render_prov(self) -> None:
        name = self.cur_prov
        allc = [c for c in self.chats if c.ai == name]
        chats = self.prov_chats(name)
        wait = sum(not c.fully_synced for c in allc)
        head = Text.assemble(
            (name, "bold " + rgb(PROV.get(name, TEXT))),
            (f"  {len(allc)} чат", rgb(MUTED)),
            (f" · {wait} хүлээгдэж байна" if wait else "", rgb(YELLOW)),
        )
        self.q("#plist-head", Static).update(head)
        for fid, key, label in (
            ("#pf-all", "all", "Бүгд"),
            ("#pf-wait", "wait", "Хүлээгдэж буй"),
            ("#pf-done", "done", "Орсон"),
        ):
            on = self.pfilter == key
            self.q(fid, Click).update(
                Text(label, style=("bold underline " + rgb(PEACH)) if on else rgb(MUTED))
            )
        opts = []
        for c in chats:
            cat, hm = when_label(c.ts)
            opts.append(
                Opt(c.id, c.title, self.account_label(c), MUTED, cat, hm, MUTED, self.chat_glyph(c))
            )
        ids = [c.id for c in chats]
        sel = self.cite_id if self.cite_id in ids else self.sel.get(name)
        if sel not in ids:
            sel = ids[0] if ids else None
        if sel:
            self.sel[name] = sel
        else:
            self.sel.pop(name, None)
        self.q("#plist", ChatList).set_items(opts, sel)
        self.show_chat(sel)

    def account_label(self, c: Chat) -> str:
        if self.account_counts.get(c.ai, 0) > 1:
            return clip(c.account, 36)
        return ""

    def show_chat(self, cid: str | None, *, dialog: bool = False) -> None:
        box = self.q("#pmsgs", VerticalScroll)
        c = self.chat(cid) if cid else None
        if not c:
            self.reader_key = None
            box.remove_children()
            choose = (
                "Архив уншиж байна…"
                if self.archive_loading
                else self.archive_error or "Энэ шүүлтүүрт чат алга."
            )
            box.mount(Static(tx(choose, MUTED)))
            return
        cite = self.citation if self.cite_id == c.id else None
        key = (c.id, c.revision, cite)
        if not dialog and (self.size.width < 100 or self.reader_key == key):
            return
        self.reader_key = key
        if not dialog:
            box.remove_children()
            box.mount(Static(tx("Чат уншиж байна…", MUTED)))
        self.run_worker(self.read_chat(c, key, dialog), group="reader", exclusive=True)

    async def read_chat(self, c: Chat, key: tuple[str, int, Citation | None], dialog: bool) -> None:
        try:
            if citation := key[2]:
                messages, number = await asyncio.to_thread(self.backend.cited_messages, citation)
            else:
                messages, number = await asyncio.to_thread(self.backend.messages, c.id), 0
        except BackendError as error:
            if self.reader_key == key and self.route == "prov":
                self.reader_key = None
                box = self.q("#pmsgs", VerticalScroll)
                await box.remove_children()
                box.mount(Static(tx(str(error), RED)))
                self.toast(str(error), "error")
            return
        if self.reader_key != key or self.route != "prov" or self.sel.get(self.cur_prov) != c.id:
            return
        loaded = replace(c, messages=messages)
        if dialog:
            self.reader_key = None
            if not isinstance(self.screen, ModalScreen):
                self.dialog(ChatDialog(loaded, number))
            return
        box = self.q("#pmsgs", VerticalScroll)
        ws, cited = self.chat_widgets(loaded, number)
        await box.remove_children()
        if self.reader_key == key and self.route == "prov":
            await box.mount(*ws)
            if cited is not None:
                self.call_after_refresh(lambda: cited.scroll_visible(animate=False))
            else:
                self.call_after_refresh(lambda: box.scroll_home(animate=False))

    def on_list_pick(self, cid: str, click: bool) -> None:
        self.sel[self.cur_prov] = cid
        self.viewed[cid] = time.time()
        if self.cite_id != cid:
            self.cite_id = None
        self.show_chat(cid, dialog=click and self.size.width < 100)

    def prov_move(self, d: int) -> None:
        self.q("#plist", ChatList).move(d)

    def open_provider(self, name: str) -> None:
        self.providers.add(name)
        self.cur_prov = name
        self.cite_id = None
        self.go("prov")

    def _chat_head(self, c: Chat) -> list[Any]:
        status = Text()
        status.append("● " if c.local else "◌ ", style=rgb(GREEN if c.local else MUTED))
        status.append(
            "компьютерт хадгалсан" if c.local else "inbox · хадгалагдаагүй",
            style=rgb(TEXT if c.local else MUTED),
        )
        status.append("   ")
        glyph, color = self.chat_glyph(c)
        status.append(glyph + " ", style=rgb(color))
        status.append(self.status_word(c), style=rgb(MUTED))
        cat, hm = when_label(c.ts)
        details = " · ".join(
            part
            for part in (c.account, c.project, f"{cat} {hm}".strip(), f"{c.message_count} мессеж")
            if part
        )
        head = Static(
            Text.assemble(
                (c.title + "\n", "bold " + rgb(TEXT)),
                (c.ai, rgb(PROV.get(c.ai, TEXT))),
                (f" · {details}\n", rgb(MUTED)),
                status,
            ),
            classes="chead",
        )
        hints: list[Hint] = []
        if c.local and c.message_count > 0 and not c.fully_synced:
            hints.append(Hint("/sync-all", "бүгдийг sync хийх", self.cmd_sync_all))
        hints.append(Hint("/chats", "архив", self.cmd_chats))
        return [head, Horizontal(*hints, classes="chactions")]

    def refresh_tail(self, sess: Sess) -> None:
        if self.route == "session" and self.cur_sid == sess.id:
            blocks = list(self.q("#msgs").query(AiBlock))
            if blocks:
                blocks[-1].update(self.ai_text(sess.msgs[-1]))

    # ───── асуулт ─────
    def ask(self, text: str) -> None:
        s = self.cur_sess() if self.route == "session" else None
        if s and s.id in self.ask_workers:
            self.toast("Энэ асуулт боловсруулагдаж байна · ctrl+x", "warning")
            return
        if not s or s.kind != "ask":
            sid = f"s{len(self.sessions) + 1}"
            s = Sess(sid, "ask", clip(text, 40), time.time())
            self.sessions[sid] = s
        s.ts = time.time()
        s.msgs.append({"role": "user", "text": text})
        msg = {
            "role": "ai",
            "text": "",
            "pending": True,
            "sources": [],
            "took": 0.0,
            "started": time.monotonic(),
        }
        s.msgs.append(msg)
        self.open_session(s.id)
        self.ask_workers[s.id] = self.run_worker(self._answer(s, msg, text), group="answers")

    async def _answer(self, s: Sess, msg: dict, text: str) -> None:
        t0 = time.monotonic()
        citations = []
        kind = "INFO"
        try:
            result = await self.backend.call("ask", question=text)
            if error := result.get("error"):
                body = error.get("message") or "NotebookLM хариулж чадсангүй."
                if error.get("code") == "auth_required":
                    body = "NotebookLM-ийн нэвтрэлт дууссан. /login ажиллуул."
                    self.notebook_status = {"configured": True, "connected": False, "error": error}
                elif error.get("code") == "no_synced_sources":
                    body = "NotebookLM-д ашиглах баталгаажсан эх сурвалж алга. Синкийн төлөвийг шалгана уу."
                elif error.get("code") == "unverified_sources":
                    body = "Эх сурвалжийг баталгаажуулж чадсангүй."
                msg["error_code"] = error.get("code")
                kind = "ERR"
            else:
                body = result.get("answer") or "NotebookLM хоосон хариу өглөө."
                citations = result.get("citations", [])
                msg["uncited"] = not result.get("sources")
        except BackendError as error:
            body = str(error)
            msg["error_code"] = error.code
            kind = "ERR"
        except asyncio.CancelledError:
            body = "Асуултыг зогсоосон."
            kind = "WARN"
        finally:
            self.ask_workers.pop(s.id, None)
        msg.update(text=body, pending=False, sources=citations, took=time.monotonic() - t0)
        if self._exit or not self.is_running:
            return
        self.note(kind, "NotebookLM асуулт")
        if self.route == "session" and self.cur_sid == s.id:
            self.render_session()
        self.refresh_all()

    # ───── командууд ─────
    def run_cmd(self, name: str, args: str = "") -> None:
        cmd = find_cmd(name)
        if not cmd:
            self.toast(f"Тодорхойгүй команд: /{name}", "error")
            return
        getattr(self, "cmd_" + cmd.name.replace("-", "_"))(args)

    def action_palette(self) -> None:
        self.cmd_palette()

    def cmd_palette(self, args: str = "") -> None:
        opts = [Opt(c.name, c.title, "", MUTED, c.category, "/" + c.name) for c in COMMANDS]

        def done(res: Any) -> None:
            if res and res[0] == "pick":
                self.call_after_refresh(lambda: self.run_cmd(res[1], ""))

        self.dialog(DialogSelect("Командууд", opts, size=64, placeholder="Команд хайх"), done)

    def cmd_chatgpt(self, args: str = "") -> None:
        self.open_provider("ChatGPT")

    def cmd_claude(self, args: str = "") -> None:
        self.open_provider("Claude")

    def cmd_gemini(self, args: str = "") -> None:
        self.open_provider("Gemini")

    def cmd_codex(self, args: str = "") -> None:
        self.open_provider("Codex")

    def cmd_opencode(self, args: str = "") -> None:
        self.open_provider("OpenCode")

    def cmd_dashboard(self, args: str = "") -> None:
        self.go("dash")

    def action_sources(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.cmd_sources()

    def source_options(self) -> list[Opt]:
        return [
            Opt(
                citation,
                citation.label,
                footer=(
                    f"#{citation.number:04d}"
                    if citation.message_id
                    else "хуучин ишлэл"
                    if citation.stale
                    else "ишилсэн хэсэг"
                ),
            )
            for citation in self.session_citations()
        ]

    def open_citation(self, citation: Citation) -> None:
        if citation.message_id:
            self.open_chat(citation.chat_id, citation)
        elif citation.passage:
            title = "Хуучин эхээс ишилсэн хэсэг" if citation.stale else "Эхээс ишилсэн хэсэг"
            self.dialog(DialogInfo(title, lambda _: tx(citation.passage)))
        else:
            self.toast("Энэ ишлэл тодорхой нэг мессежтэй холбогдоогүй.", "warning")

    def cmd_sources(self, args: str = "") -> None:
        if not self.source_options():
            self.toast("Нээх боломжтой мессежийн ишлэл алга")
            return

        def done(result: Any) -> None:
            if result and result[0] == "pick":
                citation = result[1]
                self.call_after_refresh(lambda: self.open_citation(citation))

        self.dialog(DialogSelect("Мессежийн ишлэлүүд", self.source_options), done)

    def cmd_new(self, args: str = "") -> None:
        self.go("home")

    def cmd_exit(self, args: str = "") -> None:
        self.exit()

    # — чатууд —
    def chat_glyph(self, c: Chat) -> tuple[str, tuple]:
        if not c.local:
            return "◌", MUTED
        return {
            "pending": ("…", MUTED),
            "running": ("▸", BLUE),
            "partial": ("◐", ORANGE),
            "failed": ("✕", RED),
            "done": ("●", GREEN),
        }.get(c.nb, ("…", MUTED))

    def status_word(self, c: Chat) -> str:
        if not c.local:
            return "inbox · хадгалагдаагүй"
        if not c.message_count:
            return "Мессежгүй"
        return {
            "none": "Sync хүлээж байна",
            "pending": "NotebookLM шинэчлэлт хүлээж байна",
            "running": "тухайн багцыг sync хийж байна…",
            "partial": f"хэсэгчлэн {c.parts[0]}/{c.parts[1]}",
            "failed": f"алдаатай · {c.error or 'operation_failed'}",
            "done": "NotebookLM-д орсон",
        }.get(c.nb, "")

    def chat_options(self) -> list[Opt]:
        opts = []
        for c in sorted(self.scoped(), key=lambda c: -c.ts):
            cat, hm = when_label(c.ts)
            glyph = self.chat_glyph(c)
            opts.append(
                Opt(
                    c.id,
                    c.title,
                    " · ".join(
                        filter(None, (c.ai, self.account_label(c), c.project, self.status_word(c)))
                    ),
                    MUTED,
                    cat,
                    hm,
                    MUTED,
                    glyph,
                )
            )

        return opts

    def cmd_chats(self, args: str = "") -> None:
        def done(res: Any) -> None:
            if res and res[0] == "pick":
                self.call_after_refresh(lambda: self.open_chat(res[1]))

        self.dialog(
            DialogSelect(
                "Чатууд",
                self.chat_options,
                size=100,
                hints=[("↵", "нээх", lambda dlg: dlg.confirm())],
                query=args,
                placeholder="Чат хайх",
            ),
            done,
        )

    def open_chat(self, cid: str, cite: Citation | None = None) -> None:
        c = self.chat(cid)
        if not c:
            self.toast("Чат энэ архивт олдсонгүй", "warning")
            return
        self.providers.add(c.ai)
        self.cur_prov = c.ai
        self.sel[c.ai] = cid
        self.pfilter = "all"
        self.cite_id, self.citation = (cid, cite) if cite else (None, None)
        self.viewed[cid] = time.time()
        self.go("prov")
        if self.size.width < 100:
            self.show_chat(cid, dialog=True)

    # — resume —
    def resume_options(self) -> list[Opt]:
        items: list[tuple[float, Opt]] = []
        for s in self.sessions.values():
            cat, hm = when_label(s.ts)
            items.append(
                (s.ts, Opt(("ask", s.id), s.title, "асуулт", MUTED, cat, hm, MUTED, ("▸", BLUE)))
            )
        for cid, ts in self.viewed.items():
            c = self.chat(cid)
            if c:
                cat, hm = when_label(ts)
                items.append(
                    (
                        ts,
                        Opt(
                            ("chat", cid),
                            c.title,
                            f"чат · {c.ai}",
                            MUTED,
                            cat,
                            hm,
                            MUTED,
                            ("●", PROV.get(c.ai, TEXT)),
                        ),
                    )
                )
        return [o for _, o in sorted(items, key=lambda x: -x[0])]

    def cmd_resume(self, args: str = "") -> None:
        def done(res: Any) -> None:
            if res and res[0] == "pick":
                kind, ident = res[1]
                self.call_after_refresh(
                    lambda: self.open_session(ident) if kind == "ask" else self.open_chat(ident)
                )

        self.dialog(
            DialogSelect(
                "Үргэлжлүүлэх",
                self.resume_options,
                size=70,
                placeholder="Яриа хайх",
                hints=[
                    (
                        "↵",
                        "нээх",
                        lambda d: d.shown and d.dismiss(("pick", d.shown[d.active].value)),
                    )
                ],
            ),
            done,
        )

    # — provider —
    def provider_options(self, *, selected_only: bool = False) -> list[Opt]:
        return [
            Opt(p, p, footer=f"{sum(c.ai == p for c in self.chats)} чат")
            for p in self.available_providers
            if not selected_only or p in self.providers
        ]

    def browse_providers(self) -> None:
        def done(result: Any) -> None:
            if result and result[0] == "pick":
                self.call_after_refresh(lambda: self.open_provider(result[1]))

        self.dialog(
            DialogSelect("Үйлчилгээний чатууд", lambda: self.provider_options(selected_only=True)),
            done,
        )

    def cmd_provider(self, args: str = "") -> None:
        def done(res: Any) -> None:
            if not res:
                return
            picked = set(res[1])
            if not picked:
                self.toast("Дор хаяж нэг provider сонгоно уу", "warning")
                return
            self.scope_selected = picked != set(self.available_providers)
            self.providers = picked
            if self.route == "prov" and self.cur_prov not in picked:
                self.open_provider(next(p for p in self.available_providers if p in picked))
            else:
                self.refresh_all()
            self.toast(f"{len(picked)} provider сонголоо")

        self.dialog(
            DialogSelect(
                "Provider шүүлтүүр",
                self.provider_options,
                size=60,
                multi=True,
                marks=self.providers,
                searchable=False,
                fallback=False,
                hints=[
                    ("tab", "сонгох", lambda d: d.action_mark()),
                    ("↵", "хэрэгжүүлэх", lambda d: d.confirm()),
                ],
            ),
            done,
        )

    def connection_details(self) -> tuple[Text, bool]:
        connected = bool(self.archive_status.get("notebook_configured"))
        conn, conn_color = self.connection_label()
        notebook, nb_color = self.notebook_label()
        status = self.notebook_status
        identity = status if status.get("checked_at") else self.last_notebook_check
        rows = [tx("NotebookLM", TEXT, True), tx(notebook, nb_color)]
        if connected:
            if title := identity.get("title"):
                rows.append(tx(title, TEXT, True))
            account = identity.get("account") or "Шалгалтаар тодорхойгүй"
            rows.append(tx(f"Google аккаунт · {account}", TEXT))
            if checked_at := identity.get("checked_at"):
                try:
                    checked = (
                        datetime.fromisoformat(checked_at).astimezone().strftime("%Y-%m-%d %H:%M")
                    )
                except (TypeError, ValueError):
                    pass
                else:
                    rows.append(tx(f"Сүүлд амжилттай шалгасан · {checked}", MUTED))
            if status.get("connected") and status.get("can_write") is False:
                rows.append(tx("Унших эрхтэй · илгээхэд засах эрх хэрэгтэй", YELLOW))
            elif status.get("connected") and status.get("can_write") is True:
                rows.append(tx("Эх сурвалж нэмэх эрхтэй", GREEN))
        else:
            rows.append(tx("Шинэ notebook үүсгэх эсвэл бэлэн notebook-ийн линкийг оруулна.", TEXT))
        if error := status.get("error"):
            rows.append(tx(error.get("message", ""), YELLOW))
        if connected:
            docs = self.archive_status.get("google_docs")
            rows.append(Text(""))
            rows.append(tx("Google Docs", TEXT, True))
            if docs is not None:
                rows.append(
                    tx(f"Docs sync идэвхтэй · {docs['indexed_parts']} Docs индексжсэн", GREEN)
                )
            rows.append(tx("Brave-д Google Docs-д нэвтэрч, Docs холбох товчийг дарна.", MUTED))
        if self.sync_activity:
            rows.append(tx(sync_phase_text(self.sync_activity), BLUE))
        rows += [
            Text(""),
            tx("Вебээс локал архивт", TEXT, True),
            tx(conn, conn_color),
            tx("Brave → AI Memory → Sync now", MUTED),
            tx(str(self.backend.db), MUTED),
        ]

        return Text("\n").join(rows), connected

    def cmd_connect(self, args: str = "") -> None:
        def chosen(action: str | None) -> None:
            if action in {"login", "create", "attach"}:
                self.call_after_refresh(lambda: self.open_notebook_form(action))
            elif action:
                self.call_after_refresh(lambda: self.run_cmd(action))

        self.dialog(NotebookDialog(self.connection_details), chosen)

    def cmd_setup(self, args: str = "") -> None:
        self.cmd_connect()

    def open_notebook_form(self, mode: str) -> None:
        operation = "login" if mode == "login" else "setup"

        def submitted(options: dict[str, str] | None) -> None:
            if options is not None:
                self.call_after_refresh(lambda: self.start_job(operation, options))

        account = (
            self.notebook_status.get("account") or self.last_notebook_check.get("account") or ""
        )
        self.dialog(NotebookForm(mode, account), submitted)

    def flow_counts(self) -> dict[str, int]:
        counts = self.stats(all_chats=True)
        if (docs := self.archive_status.get("google_docs")) is not None:
            counts.update(docs_total=docs["documents"], docs_indexed=docs["indexed_parts"])
        return counts

    def flow_notebook_label(self) -> str:
        return (
            sync_phase_text(self.sync_activity) if self.sync_activity else self.notebook_label()[0]
        )

    def cmd_flow(self, args: str = "") -> None:
        def chosen(command: str | None) -> None:
            if command:
                self.call_after_refresh(lambda: self.run_cmd(command))

        self.dialog(FlowCanvas(self.flow_counts(), self.flow_notebook_label()), chosen)

    # — sync —
    def cmd_check(self, args: str = "") -> None:
        self.start_job("check")

    def cmd_docs_connect(self, args: str = "") -> None:
        if not self.archive_status.get("notebook_configured"):
            self.cmd_setup()
            return
        self.start_job("docs-connect")

    def cmd_login(self, args: str = "") -> None:
        if not self.archive_status.get("notebook_configured"):
            self.cmd_setup()
        else:
            self.start_job("login", args.strip())

    def cmd_sync_all(self, args: str = "") -> None:
        if self.job and self.job.status in ("running", "stopping"):
            self.toast("Ажил явагдаж байна", "warning")
            return
        if self.sync_activity:
            self.go("dash")
            self.toast(sync_phase_text(self.sync_activity))
            return
        if self.archive_error_code == "archive_missing":
            self.cmd_setup()
            return
        if self.archive_loading:
            self.toast("Архив уншиж байна…", "warning")
            return
        if not self.archive_error and not self.archive_status.get("notebook_configured"):
            self.cmd_setup()
            return
        if self.notebook_status.get("connected") and self.notebook_status.get("can_write") is False:
            self.toast(
                "Энэ Google аккаунт унших эрхтэй. Засах эрхтэй аккаунтаар нэвтэрнэ үү.", "warning"
            )
            self.open_notebook_form("login")
            return
        self.start_job("all")

    def cmd_auto(self, args: str = "") -> None:
        mode = args.strip().lower()
        if mode not in {"", "on", "off"}:
            self.toast("/auto, /auto on, /auto off", "warning")
            return
        if any(worker.group == "auto-setting" for worker in self.workers):
            return
        if self.archive_loading:
            self.toast("Архив уншиж байна…", "warning")
            return
        enabled = mode == "on" if mode else not self.auto_sync
        self.run_worker(self.set_auto_sync(enabled), group="auto-setting")

    async def set_auto_sync(self, enabled: bool) -> None:
        try:
            await asyncio.to_thread(self.backend.set_auto_sync, enabled)
        except BackendError as error:
            self.toast(str(error), "error")
            return
        self.auto_sync = enabled
        self.background_sync = await asyncio.to_thread(self.backend.background_sync_enabled)
        self.auto_retry_after = 0.0
        if (
            not enabled
            and self.job
            and self.job.automatic
            and self.job.status == "running"
            and self.job_worker
        ):
            self.job.status = "stopping" if self.job.steps else "cancelled"
            self.job_worker.cancel()
        if enabled:
            when = "mem хаалттай үед ч" if self.background_sync else "mem нээлттэй үед"
            self.toast(f"Auto sync асаалттай · {when} ажиллана")
        else:
            self.toast("Auto sync унтраалаа")
        await self.refresh_archive(force=True)

    def start_job(self, kind: str, args: str | dict[str, str] = "") -> None:
        if self.job and self.job.status in ("running", "stopping"):
            self.toast("Ажил явагдаж байна", "warning")
            if not isinstance(self.screen, JobDialog):
                self.dialog(JobDialog(lambda: self.job))
            return
        self.workers.cancel_group(self, "notebook-status")
        titles = {
            "all": "Архивын өөрчлөлтийг sync хийх",
            "auto": "NotebookLM руу автомат sync хийх",
            "check": "Холболт шалгах",
            "login": "NotebookLM-д нэвтрэх",
            "docs-connect": "Google Docs холбох",
            "setup": "NotebookLM анх тохируулах",
        }
        self.job = Job(titles[kind], automatic=kind == "auto")
        if not self.job.automatic:
            self.dialog(JobDialog(lambda: self.job))
        else:
            self.refresh_home()
            self.refresh_statusbar()
        self.job_worker = self.run_worker(self._run_job(kind, self.job, args), group="jobs")

    def cmd_stop(self, args: str = "") -> None:
        self.action_stop()

    def action_stop(self) -> None:
        if self.job and self.job.status == "stopping":
            return
        if self.job and self.job.status == "running" and self.job_worker:
            if self.job.automatic:
                self.cmd_auto("off")
                return
            if not self.job.steps:
                self.job.status = "cancelled"
            else:
                self.job.status = "stopping"
            self.job_worker.cancel()
            return
        if self.sync_activity.get("automatic"):
            self.cmd_auto("off")
            return
        if self.ask_workers:
            sid = (
                self.cur_sid
                if self.cur_sid in self.ask_workers
                else next(reversed(self.ask_workers))
            )
            worker = self.ask_workers[sid]
            if worker.is_running:
                worker.cancel()
            else:
                self.ask_workers.pop(sid)
                self.sessions[sid].msgs[-1].update(text="Асуултыг зогсоосон.", pending=False)
                worker.cancel()
                if self.route == "session" and self.cur_sid == sid:
                    self.render_session()
            return
        self.toast("Зогсоох ажил алга")

    async def _run_job(self, kind: str, job: Job, args: str | dict[str, str] = "") -> None:
        local_step = Step("Локал архив унших")
        remote_step = Step(
            {
                "check": "NotebookLM холболт",
                "login": "Brave-ийн нэвтрэлтийг холбох",
                "docs-connect": "Brave-ийн Google Docs нэвтрэлтийг шалгах",
                "setup": "NotebookLM тохируулж байна",
            }.get(
                kind,
                "Google Docs → NotebookLM"
                if "google_docs" in self.archive_status
                else "NotebookLM руу илгээх",
            )
        )
        job.steps = [local_step, remote_step]
        current_step = local_step
        deferred = 0
        work_left = False
        completed = 0

        def show_phase(event: dict) -> None:
            remote_step.detail = sync_phase_text(event)

        try:
            local_step.state = "running"
            if not await self.refresh_archive(force=True):
                if kind != "setup" or self.archive_error_code != "archive_missing":
                    raise BackendError(self.archive_error, code="archive_error")
                local_step.state, local_step.detail = "skip", "Тохиргоогоор үүсгэнэ"
            else:
                local_step.state, local_step.detail = "done", f"{len(self.chats)} чат"
            if job.automatic and (
                not self.auto_sync
                or self.sync_activity
                or self.auto_sync_due is None
                or self.auto_sync_due > time.time()
            ):
                job.status = "cancelled"
                self.refresh_all()
                return
            current_step = remote_step
            remote_step.state = "running"
            if kind == "check":
                self.connection_status = await asyncio.to_thread(
                    self.backend.connection, probe=True
                )
                result = await self.backend.call("check")
            elif kind in {"login", "setup"}:
                result = await self.backend.call(kind, args)
            elif kind == "docs-connect":
                result = await self.backend.call(kind)
            else:
                operation = "auto-sync" if job.automatic else "sync-all"
                result = await self.backend.call(operation, on_progress=show_phase)
                if result.get("automatic") in {"idle", "busy", "stopped"}:
                    remote_step.state = "skip"
                    job.status = "cancelled"
                    await self.refresh_archive(force=True)
                    return
            if kind in {"check", "login", "setup"}:
                self.update_notebook_status(result)
                if kind in {"login", "setup"} and self.notebook_status.get("connected"):
                    self.auto_retry_after = 0.0
            archive_ready = await self.refresh_archive(force=True)
            if kind == "setup" and archive_ready:
                local_step.state, local_step.detail = "done", f"{len(self.chats)} чат"
            if error := result.get("error"):
                if error.get("code") == "docs_web_auth_required":
                    error = {**error, "message": "Brave-д Google Docs-д нэвтэрч дахин холбоорой."}
                raise BackendError(
                    error.get("message", "Ажил амжилтгүй."),
                    code=error.get("code", "operation_failed"),
                )
            if kind in {"check", "login", "setup"}:
                if not self.notebook_status.get("connected"):
                    error = self.notebook_status.get("error", {})
                    raise BackendError(
                        error.get("message", "NotebookLM тохируулаагүй."),
                        code=error.get("code", "not_configured"),
                    )
                ready = self.notebook_status.get("ready_sources", 0)
                title = self.notebook_status.get("title")
                remote_step.detail = f"{title} · {ready} бэлэн эх" if title else f"{ready} бэлэн эх"
            elif kind == "docs-connect":
                remote_step.detail = "Google Docs sync идэвхтэй"
            else:
                completed, deferred = result.get("completed", 0), result.get("deferred", 0)
                remote_step.detail = f"{completed} дууссан · {deferred} хойшилсон"
                if type(payload_bytes := result.get("docs_web_write_payload_bytes")) is int:
                    remote_step.detail += f" · Docs хадгалалт {payload_bytes:,} байт"
                status = result.get("status") or {}
                work_left = (
                    status.get("sync_status") == "pending"
                    or self.archive_status.get("sync_status") == "pending"
                    or any(c.local and c.message_count and c.nb == "none" for c in self.chats)
                )
                if blocked := result.get("blocked"):
                    reasons = list(
                        dict.fromkeys(
                            str(item.get("reason", "operation_failed")) for item in blocked
                        )
                    )
                    if "auth_required" in reasons:
                        message = "NotebookLM-ийн нэвтрэлт дууссан. Brave-ээр дахин нэвтэрнэ үү."
                        self.notebook_status.update(
                            connected=False, error={"code": "auth_required", "message": message}
                        )
                        raise BackendError(message, code="auth_required")
                    if "docs_web_auth_required" in reasons:
                        raise BackendError(
                            "Brave-д Google Docs-д нэвтэрч /docs-connect ажиллуулна уу.",
                            code="docs_web_auth_required",
                        )
                    if "docs_web_conflict" in reasons:
                        raise BackendError(
                            "Google Docs өөрчлөгдсөн байна. Sync зогссон. "
                            "Архив болон Docs-ийн хувилбарыг тулгаж шалгана уу.",
                            code="docs_web_conflict",
                        )
                    raise BackendError(
                        f"{len(blocked)} саатсан · {', '.join(reasons)}", code="sync_blocked"
                    )
                if deferred:
                    job.notes.append(tx("Хойшилсон ажлууд архивын дараалалд үлдлээ.", ORANGE))
                elif work_left:
                    job.notes.append(tx("Илгээх ажил архивын дараалалд үлдлээ.", ORANGE))
            remote_step.state = "done"
            job.status = "partial" if deferred or work_left else "done"
            if job.status == "partial":
                self.note("WARN", job.title + " · илгээх ажил үлдсэн")
                if not job.automatic:
                    self.toast("Илгээх ажил архивын дараалалд үлдлээ.", "warning")
            else:
                self.note("OK", job.title)
                if not job.automatic:
                    self.toast(job.title + " дууслаа")
        except BackendError as error:
            if kind == "check" and current_step is remote_step:
                self.update_notebook_status({"error": {"code": error.code, "message": str(error)}})
            current_step.state = "fail"
            for step in job.steps:
                if step.state == "pending":
                    step.state = "skip"
            job.status = "failed"
            job.error_code = error.code
            job.notes.append(tx(str(error), RED))
            self.note("ERR", job.title + " · " + error.code)
            if not job.automatic or error.code == "auth_required":
                self.toast(str(error), "error")
        except asyncio.CancelledError:
            for step in job.steps:
                if step.state in ("running", "pending"):
                    step.state = "skip"
                    step.detail = ""
            job.status = "cancelled"
            self.note("WARN", job.title + " зогслоо")
            if self._exit or not self.is_running:
                return
            self.toast("Ажлыг зогсоолоо")
            await self.refresh_archive(force=True)
        if (
            job.automatic
            and not completed
            and job.status in {"failed", "partial"}
            and (self.auto_sync_due is None or self.auto_sync_due <= time.time())
        ):
            self.auto_retry_after = time.monotonic() + AUTO_SYNC_RETRY_DELAY
        self.refresh_all()

    # — тусламж —
    def cmd_help(self, args: str = "") -> None:
        def body(w: int) -> Text:
            rows: list[Text] = [Text("")]
            last = ""
            for c in COMMANDS:
                if c.category != last:
                    rows.append(tx(c.category, PURPLE, True))
                    last = c.category
                rows.append(lr(tx("/" + c.name, PEACH), tx(c.desc, MUTED), w))
            rows += [Text(""), tx("Товчлуур", PURPLE, True)]
            for k, d in [
                ("ctrl+p", "командын цонх"),
                ("ctrl+o", "хариултын ишлэл нээх"),
                ("PgUp / PgDn", "унших хэсгийг гүйлгэх"),
                ("ctrl+n", "шинэ асуулт"),
                ("ctrl+x", "идэвхтэй ажил зогсоох"),
                ("↑ ↓", "provider tab дээр чат сонгох"),
                ("ctrl+w", "Төлөвөөс буцах"),
                ("ctrl+c ×2", "гарах"),
                ("esc", "цонх хаах / буцах"),
            ]:
                rows.append(lr(tx(k, TEXT), tx(d, MUTED), w))
            rows += [
                Text(""),
                tx("Mouse: tab, мөр, товч, hint, статус мөр бүрийг товшиж болно.", MUTED),
            ]
            return Text("\n").join(rows)

        self.dialog(DialogInfo("Тусламж", body, size=76))


def main() -> None:
    parser = argparse.ArgumentParser(prog="mem", description="AI Memory terminal client")
    parser.add_argument("--db", help="Path to the AI Memory SQLite archive")
    parser.add_argument("--repo", help="Path to the local AI Memory repository")
    args = parser.parse_args()
    try:
        backend = Backend(db=args.db, repo=args.repo)
    except BackendError as error:
        parser.exit(1, f"mem: {error}\n")
    Mem(backend).run()


if __name__ == "__main__":
    main()

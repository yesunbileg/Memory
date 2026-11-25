"""Interactive map of the capture, publication and question flow."""

from __future__ import annotations

from typing import ClassVar

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class FlowCard(Vertical):
    def __init__(
        self,
        title: str,
        description: str,
        *,
        kind: str,
        status: str = "",
        action: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(classes=f"flow-card {kind}")
        self.title_text = title
        self.description = description
        self.status = status
        self.link = action

    def compose(self) -> ComposeResult:
        yield Label(self.title_text, classes="flow-card-title", markup=False)
        yield Static(self.description, classes="flow-card-copy", markup=False)
        if self.status:
            yield Static(self.status, classes="flow-card-status", markup=False)
        if self.link:
            command, label = self.link
            yield Button(label, id=command, classes="flow-action", compact=True)


class FlowCanvas(ModalScreen[str]):
    """Connected cards describe what each operation sends and where it goes."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", show=False),
        Binding("up", "scroll_flow(-1)", show=False),
        Binding("down", "scroll_flow(1)", show=False),
    ]
    DEFAULT_CSS = """
    FlowCanvas { align: center middle; background: #000000 72%; }
    FlowCanvas #flow-panel {
        width: 96%; max-width: 150; height: 94%; max-height: 40;
        background: #141414; padding: 1 2;
    }
    FlowCanvas #flow-header { height: 1; margin-bottom: 1; }
    FlowCanvas #flow-title { width: 1fr; text-style: bold; color: #eeeeee; }
    FlowCanvas #flow-close { width: auto; min-width: 5; }
    FlowCanvas #flow-overview { display: none; height: auto; color: #fab283; margin-bottom: 1; }
    FlowCanvas #flow-scroll { height: 1fr; }
    FlowCanvas #flow-map { height: auto; align: center middle; }
    FlowCanvas #flow-inputs { width: 1.4fr; height: auto; }
    FlowCanvas #flow-inputs > .flow-card { width: 100%; }
    FlowCanvas #flow-inputs > .cli-ingress { margin-top: 1; }
    FlowCanvas .flow-map-edge {
        width: 3; height: auto; margin-top: 1; text-align: center; color: #a8a8a8;
    }
    FlowCanvas .flow-card {
        background: #1c1c1c; border: round #484848; height: auto; width: 1fr;
        padding: 0 1;
    }
    FlowCanvas .flow-card.ingress { border: round #5c9cf5; }
    FlowCanvas .flow-card.archive { border: round #56b6c2; }
    FlowCanvas .flow-card.sync { border: round #e5c07b; }
    FlowCanvas .flow-card.ask { border: round #7fd88f; }
    FlowCanvas .flow-card-title { color: #eeeeee; text-style: bold; height: auto; }
    FlowCanvas .flow-card-copy { color: #cccccc; height: auto; }
    FlowCanvas .flow-card-status { color: #a8a8a8; height: auto; margin-top: 1; }
    FlowCanvas Button {
        width: auto; min-width: 0; height: 1; border: none;
        padding: 0 1; background: #303030; color: #eeeeee;
    }
    FlowCanvas Button.flow-action { margin-top: 1; }
    FlowCanvas Button:focus { background: #fab283; color: #141414; text-style: bold; }
    FlowCanvas Button:hover { background: #484848; }
    FlowCanvas Button:focus:hover { background: #fab283; }
    FlowCanvas #flow-footer { height: auto; margin-top: 1; color: #a8a8a8; }
    FlowCanvas.-vertical #flow-panel { padding: 1; }
    FlowCanvas.-vertical #flow-overview { display: block; }
    FlowCanvas.-vertical #flow-map { layout: vertical; }
    FlowCanvas.-vertical #flow-inputs { layout: horizontal; width: 100%; }
    FlowCanvas.-vertical #flow-inputs > .flow-card { width: 1fr; }
    FlowCanvas.-vertical #flow-inputs > .cli-ingress { margin: 0 0 0 1; }
    FlowCanvas.-vertical #flow-map > .flow-card { width: 100%; }
    FlowCanvas.-vertical .flow-map-edge { width: 100%; margin-top: 0; }
    FlowCanvas.-compact #flow-panel { padding: 0 1; }
    FlowCanvas.-compact #flow-header, FlowCanvas.-compact #flow-overview { margin-bottom: 0; }
    FlowCanvas.-compact #flow-footer { margin-top: 0; }
    """

    def __init__(self, counts: dict, notebook_label: str) -> None:
        super().__init__()
        self.counts = counts
        self.notebook_label = notebook_label

    def status_labels(self) -> dict[str, str]:
        total = self.counts.get("total", 0)
        done = self.counts.get("done", 0)
        syncable = self.counts.get("syncable", 0)
        published = f"{done:,}/{syncable:,} чат нийтлэгдсэн"
        if "docs_total" in self.counts:
            indexed = self.counts.get("docs_indexed", 0)
            published += f"\n{indexed:,}/{self.counts['docs_total']:,} Docs индексжсэн"
        return {
            "archive": f"{total:,} чат",
            "sync": f"{self.notebook_label}\n{published}",
        }

    def sync_copy(self) -> tuple[str, str, str]:
        if "docs_total" in self.counts:
            return (
                "Веб + локал → SQLite → Google Docs → NotebookLM → Асуух",
                "Docs → NotebookLM",
                "/sync-all Docs-д анх бүтнээр нь бичнэ. Дараа нь нэмсэн, зассан, устгасан "
                "хэсгийг илгээнэ. NotebookLM холбосон Docs-ийг шинэчилнэ.",
            )
        return (
            "Веб + локал → SQLite → NotebookLM → Асуух",
            "NotebookLM sync",
            "/sync-all өөрчлөгдсөн Markdown хэсгүүдийг NotebookLM-д бүтнээр нь байршуулна.",
        )

    def update_status(self, counts: dict, notebook_label: str) -> None:
        if (counts, notebook_label) == (self.counts, self.notebook_label):
            return
        changed_mode = ("docs_total" in counts) != ("docs_total" in self.counts)
        self.counts, self.notebook_label = counts, notebook_label
        if changed_mode:
            overview, title, description = self.sync_copy()
            self.query_one("#flow-overview", Static).update(overview)
            self.query_one(".sync .flow-card-title", Label).update(title)
            self.query_one(".sync .flow-card-copy", Static).update(description)
        for kind, status in self.status_labels().items():
            for widget in self.query(f".{kind} .flow-card-status").results(Static):
                widget.update(status)

    def compose(self) -> ComposeResult:
        labels = self.status_labels()
        overview, sync_title, sync_description = self.sync_copy()
        with Vertical(id="flow-panel"):
            with Horizontal(id="flow-header"):
                yield Label("Ажлын урсгал", id="flow-title")
                yield Button("Esc", id="flow-close", compact=True, tooltip="Урсгалаас буцах")
            yield Static(overview, id="flow-overview")
            with VerticalScroll(id="flow-scroll"):
                with Horizontal(id="flow-map"):
                    with Vertical(id="flow-inputs"):
                        yield FlowCard(
                            "Веб чатууд",
                            "ChatGPT · Claude · Gemini\nBrave дээр чатаа сонгоод Sync now дар.",
                            kind="ingress",
                            action=("connect", "Холболт"),
                        )
                        yield FlowCard(
                            "Codex · OpenCode",
                            "Stop / idle hook тухайн session-ийг хадгална.",
                            kind="ingress cli-ingress",
                        )
                    yield Static("┬→", id="flow-join", classes="flow-map-edge")
                    yield FlowCard(
                        "Локал архив",
                        "SQLite-д чатын эх агуулга хадгалагдана.",
                        kind="archive",
                        status=labels["archive"],
                        action=("chats", "Чатууд"),
                    )
                    yield Static("→", classes="flow-map-edge flow-forward")
                    yield FlowCard(
                        sync_title,
                        sync_description,
                        kind="sync",
                        status=labels["sync"],
                        action=("sync-all", "/sync-all"),
                    )
                    yield Static("→", classes="flow-map-edge flow-forward")
                    yield FlowCard(
                        "Асуух",
                        "NotebookLM-ийн бэлэн эхээс хариулна.\nmem · асуултаа бичээд Enter\nMCP · memory_ask",
                        kind="ask",
                    )
            yield Static("Tab сонгох · Enter нээх · ↑↓ гүйлгэх", id="flow-footer")

    def on_mount(self) -> None:
        self.fit(self.size.width)
        self.query_one("#connect", Button).focus(scroll_visible=False)

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.fit(event.size.width)

    def fit(self, width: int) -> None:
        vertical = width < 116
        self.set_class(vertical, "-vertical")
        self.set_class(width < 60, "-compact")
        for edge in self.query(".flow-forward").results(Static):
            edge.update("↓" if vertical else "→")
        self.call_after_refresh(self.draw_branches)

    def draw_branches(self) -> None:
        join = self.query_one("#flow-join", Static)
        web, cli = self.query(".ingress").results(FlowCard)
        if self.has_class("-vertical"):
            distance = round(cli.region.center[0] - web.region.center[0])
            span = "─" * (max(0, distance - 2) // 2)
            join.update(f"╰{span}┬{span}╯\n▼")
        else:
            height = max(1, cli.region.y - web.region.y)
            join.update("\n".join(["┬→", *(["│ "] * (height - 1)), "┘ "]))

    def action_scroll_flow(self, direction: int) -> None:
        self.query_one("#flow-scroll", VerticalScroll).scroll_relative(
            y=3 * direction, animate=False
        )

    @on(Button.Pressed)
    def handle_action(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None if event.button.id == "flow-close" else event.button.id)

    def action_close(self) -> None:
        self.dismiss(None)

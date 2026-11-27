"""The alert rule: when a failing check alerts, repeats, and resolves.

Each check's state is one line in its own file: when it began failing, when it
last alerted (0 for never), and its newest text. A state changes only once its
message is posted, so no alert is lost.
"""

from pathlib import Path

import lib


class Alerts:
    def __init__(self, folder: Path, now: int):
        self.folder, self.now = folder, now
        self.messages: list[str] = []
        self.marks: list[tuple[str, str]] = []
        folder.mkdir(parents=True, exist_ok=True)

    def check(self, name: str, failing: bool, hold: int, repeat: int, text: str):
        """Record a check that fails or passes, and queue what it should post.

        A failing check alerts after hold seconds, then every repeat seconds.
        """
        state = self.folder / name
        since, sent = self.now, 0
        if lib.has_data(state):
            first, second, *_ = state.read_text().split(" ", 2)
            since, sent = int(first), int(second)
        if not failing:
            if sent == 0:
                state.unlink(missing_ok=True)
            else:
                self.queue(f"resolved: {text} ({name})", "resolved", name)
            return
        lib.write(state, f"{since} {sent} {text}")
        due = since + hold if sent == 0 else sent + repeat
        if self.now >= due:
            self.queue(f"{text} ({name})", "sent", name)

    def queue(self, message: str, action: str, name: str) -> None:
        self.messages.append(message)
        self.marks.append((action, name))

    def send(self) -> None:
        """Post the queued messages as one, then mark them, after a good post."""
        if not self.messages:
            return
        messages = "\n".join(self.messages)
        lib.log(f"alert: {messages}")
        # from a file, as curl reads a message that starts with @ as a file name
        outbox = self.folder / ".outbox"
        lib.write(outbox, messages)
        if lib.request_url("alert_url", "--data-binary", f"@{outbox}"):
            for action, name in self.marks:
                self.mark_posted(action, name)
        else:
            lib.log("alert not sent")
        outbox.unlink()
        self.messages, self.marks = [], []

    def mark_posted(self, action: str, name: str) -> None:
        """Record when a check alerted, or forget a check that resolved."""
        state = self.folder / name
        if not state.exists():
            return
        if action == "resolved":
            state.unlink()
            return
        since, _, text = state.read_text().removesuffix("\n").split(" ", 2)
        lib.write(state, f"{since} {self.now} {text}")

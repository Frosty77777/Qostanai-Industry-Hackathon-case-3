"""Read-only repository work off the GUI thread; no camera or native hooks."""

from PySide6.QtCore import QThread, Signal


class ReviewLoadWorker(QThread):
    listed = Signal(object, object)
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, repository, parent=None, *, session_id=None):
        super().__init__(parent)
        self.repository, self.session_id = repository, session_id

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            if self.session_id is None:
                rows = self.repository.list_completed()
                if not self.isInterruptionRequested():
                    self.listed.emit(rows, self.repository.errors)
            else:
                result = self.repository.load(self.session_id)
                if not self.isInterruptionRequested():
                    self.loaded.emit(result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))

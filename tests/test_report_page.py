"""Final report and local image review tests; no camera or OS input."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel

from test_desktop import APP
from monitoring import EventType, ProctoringEvent, Severity
from ui.evidence_viewer import PAGE_SIZE, frame_source_note, resolve_evidence_path
from ui.report_page import EVENT_LABELS, SUMMARY_TYPES, ReportPage, display_time
from ui.session import ROOT, SessionResult

START = datetime(2026, 10, 8, 9, 32, 0, tzinfo=timezone.utc)


def event(kind=EventType.PHONE_DETECTED, severity=Severity.CRITICAL, delta=30, path=None, **kwargs):
    return ProctoringEvent(kind, START + timedelta(seconds=11), severity, 0.82,
                          "A suspicious event for human review", evidence_path=path,
                          risk_delta=delta, **kwargs)


def result(events=(), **kwargs):
    defaults = dict(student="Demo Student", exam="Practice Exam", duration_seconds=125,
                    risk_score=53, risk_level="HIGH", events=tuple(events), event_counts={},
                    started_at=START, ended_at=START + timedelta(seconds=125))
    defaults.update(kwargs)
    return SessionResult(**defaults)


class ReportPageChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.page = ReportPage()

    def tearDown(self):
        self.page.reset()
        self.page.close()
        self.page.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def image(self, filename="image.jpg"):
        path = self.directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(640, 480, QImage.Format.Format_RGB888)
        image.fill(0x486078)
        self.assertTrue(image.save(str(path)))
        return path

    def test_report_shows_metadata_in_client_timezone(self):
        self.page.set_result(result())
        self.assertEqual(self.page.values["Student"].text(), "Demo Student")
        self.assertEqual(self.page.values["Exam"].text(), "Practice Exam")
        self.assertEqual(self.page.values["Start time"].text(), "2026-10-08 14:32:00")
        self.assertEqual(self.page.values["End time"].text(), "2026-10-08 14:34:05")
        self.assertEqual(self.page.values["Duration"].text(), "00:02:05")

    def test_report_older_metadata_without_dates_is_readable(self):
        self.page.set_result(result(started_at=None, ended_at=None))
        self.assertEqual(self.page.values["Start time"].text(), "—")
        self.assertEqual(display_time(datetime(2026, 10, 8, 14, 32)), "2026-10-08 14:32:00")

    def test_report_totals_include_zero_risk_break_entries(self):
        events = (
            event(), event(EventType.FACE_MISSING, Severity.HIGH, 15),
            event(EventType.COPY_ATTEMPT, Severity.MEDIUM, 5),
            event(EventType.AUTHORIZED_BREAK_STARTED, Severity.INFO, 0),
        )
        self.page.set_result(result(events))
        self.assertEqual(self.page.values["Total Events"].text(), "4")
        for severity in ("critical", "high", "medium", "info"):
            self.assertEqual(self.page.severity_values[severity].text(), "1")

    def test_summary_covers_all_fifteen_violation_types(self):
        self.assertEqual(len(SUMMARY_TYPES), 15)
        self.page.set_result(result([event(kind) for kind in SUMMARY_TYPES]))
        self.assertEqual(set(self.page.summary_values), set(SUMMARY_TYPES))
        self.assertTrue(all(value.text() == "1" for value in self.page.summary_values.values()))

    def test_summary_uses_actual_history_and_counts_repeated_events(self):
        self.page.set_result(result([event(), event()], event_counts={EventType.PHONE_DETECTED: 99}))
        self.assertEqual(self.page.summary_values[EventType.PHONE_DETECTED].text(), "2")
        self.assertEqual(self.page.summary_values[EventType.FACE_MISSING].text(), "0")

    def test_risk_score_level_and_progress_are_prominent(self):
        for score, level in ((0, "LOW"), (30, "MODERATE"), (53, "HIGH"), (100, "CRITICAL")):
            with self.subTest(level=level):
                self.page.set_result(result(risk_score=score, risk_level=level))
                self.assertEqual(self.page.risk_score.text(), f"{score} / 100")
                self.assertEqual(self.page.risk_level.text(), level)
                self.assertEqual(self.page.risk_bar.value(), score)

    def test_full_timeline_displays_time_type_severity_delta_message(self):
        selected = event()
        self.page.set_result(result([selected]))
        model = self.page.timeline_model
        expected = ("2026-10-08 14:32:11", "Phone detected", "CRITICAL", "+30", selected.message)
        for column, text in enumerate(expected):
            self.assertEqual(model.data(model.index(0, column)), text)
        self.assertIn("PHONE_DETECTED", model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole))

    def test_full_timeline_is_not_limited_to_exam_recent_events(self):
        events = tuple(replace(event(), timestamp=START + timedelta(seconds=i)) for i in range(300))
        self.page.set_result(result(events))
        self.assertEqual(self.page.timeline_model.rowCount(), 300)
        self.assertEqual(self.page.timeline_model.data(self.page.timeline_model.index(299, 0)), "2026-10-08 14:36:59")

    def test_break_timeline_entry_has_info_and_zero_delta(self):
        self.page.set_result(result([event(EventType.AUTHORIZED_BREAK_EXPIRED, Severity.INFO, 0)]))
        model = self.page.timeline_model
        self.assertEqual(model.data(model.index(0, 1)), "Authorized break expired")
        self.assertEqual(model.data(model.index(0, 2)), "INFO")
        self.assertEqual(model.data(model.index(0, 3)), "+0")

    def test_report_displays_component_and_persistence_failures(self):
        self.page.set_result(result(error="Camera disconnected", persistence_error="Disk unavailable"))
        self.assertIn("Camera disconnected", self.page.message.text())
        self.assertIn("Disk unavailable", self.page.message.text())
        self.assertIn("in-memory report", self.page.message.text())

    def test_report_displays_completed_session_directory(self):
        self.page.set_result(result(session_directory=self.directory))
        self.assertIn(str(self.directory), self.page.message.text())

    def test_view_evidence_button_navigates_to_gallery(self):
        self.assertTrue(self.page.view_button.isEnabled())
        self.page.view_button.click()
        self.assertEqual(self.page.tabs.currentIndex(), 2)

    def test_new_session_button_emits_request(self):
        calls = []
        self.page.new_session_requested.connect(lambda: calls.append(True))
        self.page.new_button.click()
        self.assertEqual(calls, [True])

    def test_valid_evidence_has_thumbnail_metadata_and_larger_image(self):
        selected = event(path=str(self.image()), confidence=0.92)
        self.page.set_result(result([selected], session_directory=self.directory))
        card_event, panel, button, error = self.page.evidence.cards[0]
        self.assertEqual(card_event, selected)
        self.assertFalse(button.icon().isNull())
        self.assertEqual(error, "")
        texts = [widget.text() for widget in panel.findChildren(QLabel)]
        self.assertIn("Confidence: 92%", texts)
        self.assertIn("CRITICAL · +30 risk", texts)
        dialog = self.page.evidence.open_event(selected)
        APP.processEvents()
        self.assertEqual(dialog.error, "")
        self.assertIsNotNone(dialog._source)
        self.assertTrue(dialog.isVisible())

    def test_missing_evidence_is_retained_as_non_crashing_error_card(self):
        selected = event(path=str(self.directory / "missing.jpg"))
        self.page.set_result(result([selected], session_directory=self.directory))
        self.assertIn("missing", self.page.evidence.cards[0][3])
        self.assertIn("missing", self.page.evidence.open_event(selected).error)

    def test_corrupt_evidence_is_retained_as_non_crashing_error_card(self):
        path = self.directory / "corrupt.jpg"
        path.write_bytes(b"this is not an image")
        selected = event(path=str(path))
        self.page.set_result(result([selected], session_directory=self.directory))
        self.assertIn("cannot be read", self.page.evidence.cards[0][3])
        self.assertIn("cannot be read", self.page.evidence.open_event(selected).error)

    def test_security_event_has_no_webcam_evidence(self):
        self.page.set_result(result([event(EventType.ALT_TAB_ATTEMPT, Severity.HIGH, 15)]))
        self.assertEqual(self.page.evidence.events, ())
        self.assertIn("1 event(s) without an image", self.page.evidence.description.text())
        model = self.page.timeline_model
        self.assertIn("No webcam evidence", model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole))

    def test_evidence_gallery_decodes_only_one_page(self):
        selected = event(path=str(self.image()))
        events = tuple(replace(selected, timestamp=START + timedelta(seconds=i)) for i in range(PAGE_SIZE + 2))
        from ui.evidence_viewer import read_scaled_image
        with patch("ui.evidence_viewer.read_scaled_image", wraps=read_scaled_image) as reader:
            self.page.set_result(result(events, session_directory=self.directory))
            self.assertEqual(reader.call_count, PAGE_SIZE)
            self.assertEqual(len(self.page.evidence.cards), PAGE_SIZE)
            self.page.evidence.next_button.click()
            self.assertEqual(len(self.page.evidence.cards), 2)
            self.assertEqual(reader.call_count, PAGE_SIZE + 2)
            self.assertFalse(self.page.evidence.next_button.isEnabled())
            self.assertTrue(self.page.evidence.previous_button.isEnabled())

    def test_session_relative_image_path_resolves(self):
        path = self.image("evidence/suspicious.jpg")
        selected = event(path="evidence/suspicious.jpg")
        self.page.set_result(result([selected], session_directory=self.directory))
        self.assertEqual(resolve_evidence_path(selected.evidence_path, self.directory), path)
        self.assertEqual(self.page.evidence.cards[0][3], "")

    def test_project_relative_image_path_resolves(self):
        path = ROOT / "sessions" / "current" / "evidence" / "example.jpg"
        self.assertEqual(resolve_evidence_path("sessions/current/evidence/example.jpg"), path.resolve())

    def test_evidence_rejects_traversal_network_and_non_image_paths(self):
        for value in ("../other.jpg", "evidence/../../other.jpg", "https://example.test/image.jpg",
                      "\\\\server\\share\\image.jpg", "C:relative.jpg", "image.exe", "bad\x00.jpg"):
            with self.subTest(value=value):
                self.assertIsNone(resolve_evidence_path(value, self.directory))
        self.assertIsNone(resolve_evidence_path(str(self.directory.parent / "outside.jpg"), self.directory))

    def test_obstruction_cached_frame_age_is_visible(self):
        selected = event(EventType.CAMERA_OBSTRUCTED, Severity.HIGH, 20,
                         path=str(self.image()), metadata={"evidence_frame_source": "last_usable", "last_usable_age_seconds": 2.3})
        self.page.set_result(result([selected], session_directory=self.directory))
        self.assertIn("2.3s before event", frame_source_note(selected))
        panel = self.page.evidence.cards[0][1]
        self.assertTrue(any("2.3s before event" in widget.text() for widget in panel.findChildren(QLabel)))

    def test_reset_clears_report_and_closes_large_image_dialog(self):
        selected = event(path=str(self.image()))
        self.page.set_result(result([selected], session_directory=self.directory))
        dialog = self.page.evidence.open_event(selected)
        APP.processEvents()
        self.page.reset()
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(self.page.evidence.dialog)
        self.assertEqual(self.page.evidence.events, ())
        self.assertEqual(self.page.evidence.cards, [])
        self.assertEqual(self.page.timeline_model.rowCount(), 0)
        self.assertEqual(self.page.risk_score.text(), "0 / 100")
        self.assertEqual(self.page.values["Total Events"].text(), "0")


if __name__ == "__main__":
    unittest.main()

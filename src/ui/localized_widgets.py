"""Qt presentation wrappers which retain canonical English sources.

Language switches update existing widgets without replaying events or model
updates. Text-entry contents and raw student/answer/file data are never passed
through the translation catalog.
"""

from PySide6.QtGui import QAction as _QAction
from PySide6.QtWidgets import (QCheckBox as _QCheckBox, QComboBox as _QComboBox,
                              QLabel as _QLabel, QLineEdit as _QLineEdit,
                              QPlainTextEdit as _QPlainTextEdit,
                              QPushButton as _QPushButton, QRadioButton as _QRadioButton,
                              QToolButton as _QToolButton)

from i18n import language_manager, tr


class _LocalizedMetadata:
    def _init_localization(self):
        self._localized_metadata = {}
        self._localization_ready = True
        unsubscribe = language_manager.subscribe(self._retranslate)
        self.destroyed.connect(lambda: unsubscribe())

    def _set_metadata(self, method, value):
        if getattr(self, "_localization_ready", False):
            self._localized_metadata[method] = str(value)
        getattr(super(), method)(tr(str(value)))

    def setToolTip(self, value):
        self._set_metadata("setToolTip", value)

    def setStatusTip(self, value):
        self._set_metadata("setStatusTip", value)

    def setWhatsThis(self, value):
        self._set_metadata("setWhatsThis", value)

    def setAccessibleName(self, value):
        self._set_metadata("setAccessibleName", value)

    def setAccessibleDescription(self, value):
        self._set_metadata("setAccessibleDescription", value)

    def setRawToolTip(self, value):
        self._localized_metadata.pop("setToolTip", None)
        super().setToolTip(str(value))

    def setRawAccessibleName(self, value):
        self._localized_metadata.pop("setAccessibleName", None)
        super().setAccessibleName(str(value))

    def setRawAccessibleDescription(self, value):
        self._localized_metadata.pop("setAccessibleDescription", None)
        super().setAccessibleDescription(str(value))

    def _retranslate(self, language=None):
        for method, value in self._localized_metadata.items():
            getattr(super(), method)(tr(value))


class _LocalizedText(_LocalizedMetadata):
    @property
    def source_text(self):
        return self._source_text

    @property
    def canonical_text(self):
        return (self._source_text.format(**self._source_values)
                if self._source_values else self._source_text)

    def _init_text_localization(self):
        self._source_text = super().text()
        self._source_values = {}
        self._raw_text = False
        self._init_localization()
        self._retranslate()

    def setText(self, text):
        self._source_text = str(text)
        self._source_values = {}
        self._raw_text = False
        super().setText(tr(self._source_text))

    def setMessage(self, source, **values):
        self._source_text = str(source)
        self._source_values = dict(values)
        self._raw_text = False
        super().setText(tr(self._source_text, **self._source_values))

    def setRawText(self, text):
        self._source_text = str(text)
        self._source_values = {}
        self._raw_text = True
        super().setText(self._source_text)

    def _retranslate(self, language=None):
        if not self._raw_text:
            super().setText(tr(self._source_text, **self._source_values))
        super()._retranslate(language)


class QLabel(_LocalizedText, _QLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_text_localization()

    def clear(self):
        self.setText("")

    def setPixmap(self, pixmap):
        # A preview/image label must not replace pixels with its stale loading
        # sentence when the presentation language changes.
        self._source_text = ""
        self._source_values = {}
        self._raw_text = True
        super().setPixmap(pixmap)


class QPushButton(_LocalizedText, _QPushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_text_localization()


class QCheckBox(_LocalizedText, _QCheckBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_text_localization()


class QRadioButton(_LocalizedText, _QRadioButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_text_localization()


class QToolButton(_LocalizedText, _QToolButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_text_localization()


class QAction(_LocalizedText, _QAction):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_text_localization()


class QLineEdit(_LocalizedMetadata, _QLineEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_localization()

    def setPlaceholderText(self, value):
        self._set_metadata("setPlaceholderText", value)

    def setRawText(self, value):
        super().setText(str(value))


class QPlainTextEdit(_LocalizedMetadata, _QPlainTextEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._init_localization()

    def setPlaceholderText(self, value):
        self._set_metadata("setPlaceholderText", value)

    def setRawText(self, value):
        super().setPlainText(str(value))


class QComboBox(_LocalizedMetadata, _QComboBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._source_items = []
        self._init_localization()

    @property
    def source_items(self):
        return tuple(source for source, raw in self._source_items)

    def addItem(self, *args):
        self._add_item(False, *args)

    def addRawItem(self, *args):
        self._add_item(True, *args)

    def _add_item(self, raw, *args):
        translated = list(args)
        text_index = 0 if isinstance(args[0], str) else 1
        source = str(args[text_index])
        self._source_items.append((source, raw))
        translated[text_index] = source if raw else tr(source)
        super().addItem(*translated)

    def addItems(self, texts):
        for text in texts:
            self.addItem(text)

    def insertItem(self, index, *args):
        translated = list(args)
        text_index = 0 if isinstance(args[0], str) else 1
        source = str(args[text_index])
        index = max(0, min(index, len(self._source_items)))
        self._source_items.insert(index, (source, False))
        translated[text_index] = tr(source)
        super().insertItem(index, *translated)

    def removeItem(self, index):
        if 0 <= index < len(self._source_items):
            self._source_items.pop(index)
        super().removeItem(index)

    def clear(self):
        self._source_items.clear()
        super().clear()

    def setItemText(self, index, text):
        if 0 <= index < len(self._source_items):
            self._source_items[index] = (str(text), False)
        super().setItemText(index, tr(str(text)))

    def setRawItemText(self, index, text):
        if 0 <= index < len(self._source_items):
            self._source_items[index] = (str(text), True)
        super().setItemText(index, str(text))

    def setPlaceholderText(self, value):
        self._set_metadata("setPlaceholderText", value)

    def _retranslate(self, language=None):
        blocked = self.blockSignals(True)
        try:
            for index, (source, raw) in enumerate(self._source_items):
                super().setItemText(index, source if raw else tr(source))
            super()._retranslate(language)
        finally:
            self.blockSignals(blocked)


LocalizedQLabel = QLabel
LocalizedLabel = QLabel
LocalizedPushButton = QPushButton
LocalizedCheckBox = QCheckBox
LocalizedToolButton = QToolButton
LocalizedLineEdit = QLineEdit
LocalizedComboBox = QComboBox

"""Central English-source catalog and a small Qt-independent language service.

The source strings are presentation keys, never event/storage enum mutations.
Arbitrary student data is preserved by explicit template values or raw widget
APIs. Automatic formatting recognizes only registered complete templates.
"""

from collections.abc import Callable, Mapping
from functools import lru_cache
import importlib
import logging
import re
from string import Formatter
import weakref


SUPPORTED_LANGUAGES = ("en", "ru", "kk")
LANGUAGE_NAMES = {"en": "English", "ru": "Русский", "kk": "Қазақша"}
LOGGER = logging.getLogger(__name__)
_FORMATTER = Formatter()
_SEMANTIC_FIELDS = frozenset({"state", "level", "direction", "severity", "event",
                              "message", "component", "hint"})


class LanguageManager:
    """One presentation language, with weak observers and no filesystem I/O."""

    def __init__(self, language: str = "en") -> None:
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError("Unsupported UI language")
        self._language = language
        # Deleting a page can unsubscribe hundreds of child widgets at once.
        # Tokens keep both explicit cleanup and weak-reference cleanup O(1).
        self._subscribers: dict[object, weakref.ReferenceType] = {}

    @property
    def language(self) -> str:
        return self._language

    def subscribe(self, callback: Callable[[str], None]) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("Language observer must be callable")
        token = object()
        manager_reference = weakref.ref(self)

        def discarded(reference) -> None:
            manager = manager_reference()
            if manager is not None:
                manager._subscribers.pop(token, None)

        reference = (weakref.WeakMethod(callback, discarded)
                     if getattr(callback, "__self__", None) is not None
                     else weakref.ref(callback, discarded))
        self._subscribers[token] = reference

        def unsubscribe() -> None:
            self._subscribers.pop(token, None)

        return unsubscribe

    def set_language(self, language: str) -> bool:
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError("Unsupported UI language")
        if language == self.language:
            return False
        self._language = language
        for token, reference in tuple(self._subscribers.items()):
            if self._subscribers.get(token) is not reference:
                continue
            callback = reference()
            if callback is None:
                self._subscribers.pop(token, None)
                continue
            try:
                callback(language)
            except Exception:
                # A deleted view must not prevent all other pages switching.
                LOGGER.exception("UI language observer failed")
        return True


language_manager = LanguageManager()


@lru_cache(maxsize=1)
def catalogue() -> dict[str, dict[str, str]]:
    """Load the common, student, and instructor portions of the central catalog."""
    result: dict[str, dict[str, str]] = {}
    for fragment in ("common_translations", "student_translations", "report_translations"):
        name = f"{__package__}.{fragment}"
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name == name:
                continue
            raise
        for source, translations in module.TRANSLATIONS.items():
            selected = {"en": source, **translations}
            if source in result and result[source] != selected:
                raise ValueError(f"Conflicting UI translation: {source}")
            result[source] = selected
    return result


def _field_names(template: str) -> set[str]:
    return {field for _, field, _, _ in _FORMATTER.parse(template) if field is not None}


def missing_translations() -> dict[str, tuple[str, ...]]:
    """Validate all languages and preserve every template's placeholder names."""
    missing = {}
    for source, translations in catalogue().items():
        invalid = []
        for language in SUPPORTED_LANGUAGES:
            value = translations.get(language)
            if not isinstance(value, str) or not value.strip():
                invalid.append(language)
            else:
                try:
                    if _field_names(value) != _field_names(source):
                        invalid.append(language)
                except ValueError:
                    invalid.append(language)
        if invalid:
            missing[source] = tuple(invalid)
    return missing


@lru_cache(maxsize=1)
def _template_patterns() -> tuple[tuple[str, re.Pattern], ...]:
    patterns = []
    for source in catalogue():
        parts = tuple(_FORMATTER.parse(source))
        if not any(field is not None for _, field, _, _ in parts):
            continue
        # Complete, registered sentences only; never a broad word replacement.
        if sum(len(literal) for literal, _, _, _ in parts) < 3:
            continue
        expression = []
        fields = set()
        valid = True
        for literal, field, _, _ in parts:
            expression.append(re.escape(literal))
            if field is not None:
                if not field.isidentifier():
                    valid = False
                    break
                expression.append(f"(?P={field})" if field in fields else
                                  f"(?P<{field}>[^\\n]*?)")
                fields.add(field)
        if valid:
            patterns.append((source, re.compile("".join(expression), re.DOTALL)))
    # Specific long templates win over short prefixes with unconstrained data.
    return tuple(sorted(patterns, key=lambda item: len(item[0]), reverse=True))


def _semantic_values(values: Mapping[str, object], language: str) -> dict[str, object]:
    result = dict(values)
    for field in _SEMANTIC_FIELDS & result.keys():
        raw = result[field]
        if isinstance(raw, str) and raw in catalogue():
            result[field] = catalogue()[raw].get(language, raw)
    return result


def _captured_format(template: str, values: Mapping[str, str]) -> str:
    # Captured values were already formatted in English (e.g. 12.3 FPS).
    # Keep that numeric formatting while translating the surrounding sentence.
    return "".join(literal + (str(values[field]) if field is not None else "")
                   for literal, field, _, _ in _FORMATTER.parse(template))


def tr(source: str, *, language: str | None = None, **values: object) -> str:
    """Translate UI source or a registered rendered template without altering data."""
    if not isinstance(source, str):
        raise TypeError("UI translation source must be a string")
    selected = language_manager.language if language is None else language
    if selected not in SUPPORTED_LANGUAGES:
        raise ValueError("Unsupported UI language")
    translations = catalogue()
    if values:
        template = translations.get(source, {}).get(selected, source)
        return template.format(**_semantic_values(values, selected))
    if source in translations:
        return translations[source].get(selected, source)
    if selected == "en":
        return source
    for template, pattern in _template_patterns():
        match = pattern.fullmatch(source)
        if match is not None:
            captured = _semantic_values(match.groupdict(), selected)
            return _captured_format(translations[template].get(selected, template), captured)
    # Technical detail lines remain verbatim beside localized guidance.
    if "\n" in source:
        return "\n".join(tr(line, language=selected) for line in source.split("\n"))
    return source

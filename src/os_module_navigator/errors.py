"""Errors raised by providers. Messages are safe to show to the MCP client."""

from __future__ import annotations

from os_module_navigator.models import ModuleInfo


class NavigatorError(Exception):
    """Base class. ``code`` is a stable machine-readable identifier."""

    code = "navigator_error"


class ServiceStudioNotRunningError(NavigatorError):
    code = "service_studio_not_running"


class ServiceStudioWindowNotFoundError(NavigatorError):
    code = "window_not_found"


class NotConnectedError(NavigatorError):
    code = "not_connected"


class UnexpectedDialogError(NavigatorError):
    """Service Studio is showing a dialog we did not expect. The user must handle it."""

    code = "unexpected_dialog"


class SearchDialogUnavailableError(NavigatorError):
    code = "search_dialog_unavailable"


class ModuleNotFoundInEnvironmentError(NavigatorError):
    code = "module_not_found"

    def __init__(self, message: str, suggestions: list[ModuleInfo] | None = None) -> None:
        super().__init__(message)
        self.suggestions = suggestions or []


class AmbiguousModuleError(NavigatorError):
    code = "ambiguous_module"

    def __init__(self, message: str, candidates: list[ModuleInfo]) -> None:
        super().__init__(message)
        self.candidates = candidates


class AutomationTimeoutError(NavigatorError):
    code = "automation_timeout"


class FocusError(NavigatorError):
    """We could not make sure keyboard input would reach Service Studio."""

    code = "focus_error"


class WrongModuleOpenedError(NavigatorError):
    code = "wrong_module_opened"

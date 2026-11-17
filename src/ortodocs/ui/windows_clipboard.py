"""Publică RichDocument în clipboard-ul Windows ca RTF și Unicode.

Formatele de excludere cer Windows să nu colecteze elementul în Clipboard
History sau Cloud Clipboard. Aceste marcaje nu împiedică alte aplicații să
citească clipboard-ul.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import struct
import sys
import time
from collections.abc import Callable

from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.services.rtf_codec import encode_rtf


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_EXCLUDE_MONITOR = "ExcludeClipboardContentFromMonitorProcessing"
_CLIPBOARD_HISTORY = "CanIncludeInClipboardHistory"
_CLOUD_CLIPBOARD = "CanUploadToCloudClipboard"
_RTF_FORMAT = "Rich Text Format"
_PROTECTION_FORMATS = (_EXCLUDE_MONITOR, _CLIPBOARD_HISTORY, _CLOUD_CLIPBOARD)
_OPEN_RETRY_DELAYS = (0.01, 0.02, 0.04)


class WindowsClipboardError(RuntimeError):
    """Eroare de validare sau de operare cu clipboard-ul Windows."""

    def __init__(
        self,
        message: str,
        *,
        cleanup_errors: tuple[str, ...] = (),
        clipboard_state_uncertain: bool = False,
    ) -> None:
        self.cleanup_errors = cleanup_errors
        self.clipboard_state_uncertain = clipboard_state_uncertain
        details = message
        if cleanup_errors:
            details += " Erori la eliberare/cleanup: " + "; ".join(cleanup_errors) + "."
        if clipboard_state_uncertain:
            details += " Starea clipboard-ului nu a putut fi garantată."
        super().__init__(details)


@dataclass(slots=True)
class _PreparedHandle:
    format_name: str
    format_id: int
    handle: int
    transferred: bool = False


class _NativeWin32Api:
    """Adaptor Win32 încărcat doar când aplicația rulează pe Windows."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsClipboardError("Clipboard-ul Rich Text este disponibil numai pe Windows.")
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise WindowsClipboardError(f"Nu s-au putut încărca bibliotecile Win32: {exc}") from exc

        self._register_clipboard_format = self._user32.RegisterClipboardFormatW
        self._register_clipboard_format.argtypes = [ctypes.c_wchar_p]
        self._register_clipboard_format.restype = ctypes.c_uint

        self._is_window = self._user32.IsWindow
        self._is_window.argtypes = [ctypes.c_void_p]
        self._is_window.restype = ctypes.c_int32

        self._open_clipboard = self._user32.OpenClipboard
        self._open_clipboard.argtypes = [ctypes.c_void_p]
        self._open_clipboard.restype = ctypes.c_int32

        self._close_clipboard = self._user32.CloseClipboard
        self._close_clipboard.argtypes = []
        self._close_clipboard.restype = ctypes.c_int32

        self._empty_clipboard = self._user32.EmptyClipboard
        self._empty_clipboard.argtypes = []
        self._empty_clipboard.restype = ctypes.c_int32

        self._set_clipboard_data = self._user32.SetClipboardData
        self._set_clipboard_data.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        self._set_clipboard_data.restype = ctypes.c_void_p

        self._global_alloc = self._kernel32.GlobalAlloc
        self._global_alloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        self._global_alloc.restype = ctypes.c_void_p

        self._global_lock = self._kernel32.GlobalLock
        self._global_lock.argtypes = [ctypes.c_void_p]
        self._global_lock.restype = ctypes.c_void_p

        self._global_unlock = self._kernel32.GlobalUnlock
        self._global_unlock.argtypes = [ctypes.c_void_p]
        self._global_unlock.restype = ctypes.c_int32

        self._global_free = self._kernel32.GlobalFree
        self._global_free.argtypes = [ctypes.c_void_p]
        self._global_free.restype = ctypes.c_void_p

    def RegisterClipboardFormatW(self, name: str) -> int:
        return int(self._register_clipboard_format(name))

    def IsWindow(self, hwnd: int) -> bool:
        return bool(self._is_window(ctypes.c_void_p(hwnd)))

    def OpenClipboard(self, hwnd: int) -> bool:
        return bool(self._open_clipboard(ctypes.c_void_p(hwnd)))

    def CloseClipboard(self) -> bool:
        return bool(self._close_clipboard())

    def EmptyClipboard(self) -> bool:
        return bool(self._empty_clipboard())

    def SetClipboardData(self, format_id: int, handle: int) -> int | None:
        value = self._set_clipboard_data(format_id, ctypes.c_void_p(handle))
        return int(value) if value else None

    def GlobalAlloc(self, flags: int, size: int) -> int | None:
        value = self._global_alloc(flags, size)
        return int(value) if value else None

    def GlobalLock(self, handle: int) -> int | None:
        value = self._global_lock(ctypes.c_void_p(handle))
        return int(value) if value else None

    def GlobalUnlock(self, handle: int) -> bool:
        return bool(self._global_unlock(ctypes.c_void_p(handle)))

    def GlobalFree(self, handle: int) -> int | None:
        value = self._global_free(ctypes.c_void_p(handle))
        return int(value) if value else None

    @staticmethod
    def get_last_error() -> int:
        return ctypes.get_last_error()

    @staticmethod
    def set_last_error(error_code: int) -> None:
        ctypes.set_last_error(error_code)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _create_win32_api() -> _NativeWin32Api:
    return _NativeWin32Api()


def _prepare_document_payloads(document: RichDocument) -> tuple[bytes, bytes]:
    if not isinstance(document, RichDocument):
        raise WindowsClipboardError("document trebuie să fie un RichDocument.")
    try:
        snapshot = RichDocument.from_json(document.to_json())
        rtf_text = encode_rtf(snapshot)
        rtf_data = rtf_text.encode("ascii") + b"\x00"
        plain_text_parts: list[str] = []
        for paragraph_index, paragraph in enumerate(snapshot.paragraphs):
            if paragraph_index:
                plain_text_parts.append("\r\n")
            for inline in paragraph.inlines:
                if isinstance(inline, TextInline):
                    plain_text_parts.append(inline.text)
                elif isinstance(inline, LineBreakInline):
                    plain_text_parts.append("\r\n")
                else:  # Protecție pentru model modificat după construire.
                    raise TypeError("Tip de fragment Rich Text necunoscut.")
        unicode_data = "".join(plain_text_parts).encode("utf-16le") + b"\x00\x00"
    except (AttributeError, RecursionError, TypeError, UnicodeEncodeError, ValueError) as exc:
        raise WindowsClipboardError(f"Documentul nu poate fi copiat în clipboard: {exc}") from exc
    return rtf_data, unicode_data


def _win32_error(api: object, function_name: str) -> str:
    try:
        code = api.get_last_error()  # type: ignore[attr-defined]
    except Exception:
        code = "necunoscut"
    return f"{function_name} a eșuat (GetLastError={code})"


def _format_error(api: object, name: str) -> WindowsClipboardError:
    return WindowsClipboardError(_win32_error(api, f"RegisterClipboardFormatW({name})"))


def _unlock_error(api: object, handle: int) -> str | None:
    try:
        api.set_last_error(0)  # type: ignore[attr-defined]
    except Exception as exc:
        return f"LastError nu a putut fi resetat înainte de GlobalUnlock: {exc}"
    try:
        result = api.GlobalUnlock(handle)  # type: ignore[attr-defined]
    except Exception as exc:
        return f"GlobalUnlock a generat o eroare: {exc}"
    if result:
        return "GlobalUnlock a lăsat memoria blocată (rezultatul a fost nenul)."
    try:
        error_code = api.get_last_error()  # type: ignore[attr-defined]
    except Exception as exc:
        return f"GlobalUnlock nu a putut fi verificat: {exc}"
    # GlobalUnlock întoarce zero și când ultimul lock a fost eliberat cu succes.
    return None if error_code == 0 else f"GlobalUnlock a eșuat (GetLastError={error_code})"


def _free_handle(api: object, handle: int) -> str | None:
    try:
        result = api.GlobalFree(handle)  # type: ignore[attr-defined]
    except Exception as exc:
        return f"GlobalFree a generat o eroare: {exc}"
    if result:
        return _win32_error(api, "GlobalFree")
    return None


def _allocate_payload(api: object, format_name: str, format_id: int, data: bytes) -> _PreparedHandle:
    try:
        handle = api.GlobalAlloc(GMEM_MOVEABLE, len(data))  # type: ignore[attr-defined]
    except Exception as exc:
        raise WindowsClipboardError(f"GlobalAlloc a generat o eroare pentru {format_name}: {exc}") from exc
    if not handle:
        raise WindowsClipboardError(_win32_error(api, f"GlobalAlloc({format_name})"))

    try:
        pointer = api.GlobalLock(handle)  # type: ignore[attr-defined]
    except Exception as exc:
        pointer = None
        lock_message = f"GlobalLock a generat o eroare pentru {format_name}: {exc}"
    else:
        lock_message = _win32_error(api, f"GlobalLock({format_name})")
    if not pointer:
        free_error = _free_handle(api, handle)
        cleanup = (free_error,) if free_error else ()
        raise WindowsClipboardError(lock_message, cleanup_errors=cleanup) from None

    copy_error: BaseException | None = None
    try:
        ctypes.memmove(pointer, data, len(data))
    except BaseException as exc:  # cleanup-ul trebuie făcut și la întreruperi.
        copy_error = exc
    unlock_message = _unlock_error(api, handle)
    cleanup_errors: list[str] = []
    free_error: str | None = None
    if copy_error is not None or unlock_message is not None:
        free_error = _free_handle(api, handle)
        if free_error:
            cleanup_errors.append(free_error)
        message = (
            f"Copierea payload-ului {format_name} a eșuat: {copy_error}"
            if copy_error is not None
            else unlock_message or "GlobalUnlock a eșuat."
        )
        if isinstance(copy_error, BaseException) and not isinstance(copy_error, Exception):
            raise copy_error
        raise WindowsClipboardError(message, cleanup_errors=tuple(cleanup_errors)) from copy_error
    return _PreparedHandle(format_name, format_id, int(handle))


def _free_untransferred(api: object, handles: list[_PreparedHandle]) -> list[str]:
    errors: list[str] = []
    for record in reversed(handles):
        if record.transferred:
            continue
        error = _free_handle(api, record.handle)
        if error:
            errors.append(f"{record.format_name}: {error}")
    return errors


def _empty_clipboard(api: object) -> str | None:
    try:
        if api.EmptyClipboard():  # type: ignore[attr-defined]
            return None
    except Exception as exc:
        return f"EmptyClipboard a generat o eroare: {exc}"
    return _win32_error(api, "EmptyClipboard")


def _close_clipboard(api: object) -> str | None:
    try:
        if api.CloseClipboard():  # type: ignore[attr-defined]
            return None
    except Exception as exc:
        return f"CloseClipboard a generat o eroare: {exc}"
    return _win32_error(api, "CloseClipboard")


def _open_clipboard(
    api: object,
    owner_hwnd: int,
    sleep: Callable[[float], None],
) -> None:
    for attempt in range(len(_OPEN_RETRY_DELAYS) + 1):
        try:
            if api.OpenClipboard(owner_hwnd):  # type: ignore[attr-defined]
                return
        except Exception as exc:
            raise WindowsClipboardError(f"OpenClipboard a generat o eroare: {exc}") from exc
        if attempt < len(_OPEN_RETRY_DELAYS):
            try:
                sleep(_OPEN_RETRY_DELAYS[attempt])
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                raise WindowsClipboardError(f"Așteptarea pentru retry OpenClipboard a eșuat: {exc}") from exc
    raise WindowsClipboardError(_win32_error(api, "OpenClipboard după retry-uri limitate"))


def _copy_rich_document_with_api(
    document: RichDocument,
    owner_hwnd: int,
    api: object,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Implementare internă injectabilă; nu accesează clipboard-ul până la validare."""

    if type(owner_hwnd) is not int or owner_hwnd <= 0 or owner_hwnd > (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1:
        raise WindowsClipboardError("owner_hwnd trebuie să fie un HWND valid, nenul și încadrabil.")
    rtf_data, unicode_data = _prepare_document_payloads(document)
    protection_payloads = {
        _EXCLUDE_MONITOR: b"\x01",
        _CLIPBOARD_HISTORY: struct.pack("<I", 0),
        _CLOUD_CLIPBOARD: struct.pack("<I", 0),
    }
    payloads: list[tuple[str, bytes]] = [
        *((name, protection_payloads[name]) for name in _PROTECTION_FORMATS),
        (_RTF_FORMAT, rtf_data),
        ("CF_UNICODETEXT", unicode_data),
    ]

    try:
        if not api.IsWindow(owner_hwnd):  # type: ignore[attr-defined]
            raise WindowsClipboardError("owner_hwnd nu identifică o fereastră Windows validă.")
    except WindowsClipboardError:
        raise
    except Exception as exc:
        raise WindowsClipboardError(f"Verificarea owner_hwnd a eșuat: {exc}") from exc

    format_ids: dict[str, int] = {}
    for format_name, _data in payloads:
        if format_name == "CF_UNICODETEXT":
            format_ids[format_name] = CF_UNICODETEXT
            continue
        try:
            format_id = api.RegisterClipboardFormatW(format_name)  # type: ignore[attr-defined]
        except Exception as exc:
            raise WindowsClipboardError(
                f"RegisterClipboardFormatW({format_name}) a generat o eroare: {exc}"
            ) from exc
        if not format_id:
            raise _format_error(api, format_name)
        format_ids[format_name] = int(format_id)

    prepared: list[_PreparedHandle] = []
    try:
        for format_name, data in payloads:
            prepared.append(_allocate_payload(api, format_name, format_ids[format_name], data))
    except BaseException as exc:
        cleanup_errors = _free_untransferred(api, prepared)
        if isinstance(exc, WindowsClipboardError):
            if cleanup_errors:
                raise WindowsClipboardError(
                    str(exc), cleanup_errors=tuple((*exc.cleanup_errors, *cleanup_errors))
                ) from exc
            raise
        if not isinstance(exc, Exception):
            raise
        raise WindowsClipboardError(
            f"Pregătirea memoriei clipboard a eșuat: {exc}", cleanup_errors=tuple(cleanup_errors)
        ) from exc

    try:
        _open_clipboard(api, owner_hwnd, sleep)
    except BaseException as exc:
        cleanup_errors = _free_untransferred(api, prepared)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            if cleanup_errors and hasattr(exc, "add_note"):
                exc.add_note("Erori la eliberarea memoriei clipboard: " + "; ".join(cleanup_errors))
            raise
        if not isinstance(exc, Exception):
            if cleanup_errors and hasattr(exc, "add_note"):
                exc.add_note("Erori la eliberarea memoriei clipboard: " + "; ".join(cleanup_errors))
            raise
        if cleanup_errors:
            if isinstance(exc, WindowsClipboardError):
                raise WindowsClipboardError(str(exc), cleanup_errors=tuple(cleanup_errors)) from exc
        if isinstance(exc, WindowsClipboardError):
            raise
        raise WindowsClipboardError(
            f"Deschiderea clipboard-ului a eșuat înainte de acces: {exc}",
            cleanup_errors=tuple(cleanup_errors),
        ) from exc

    primary_error: WindowsClipboardError | None = None
    fatal_exception: BaseException | None = None
    cleanup_errors: list[str] = []
    clipboard_cleared = False
    clipboard_changed = False
    all_published = False
    close_error: str | None = None
    closed = False
    try:
        try:
            empty_error = _empty_clipboard(api)
            if empty_error:
                raise WindowsClipboardError(empty_error)
            clipboard_cleared = True
            clipboard_changed = True

            for record in prepared:
                try:
                    result = api.SetClipboardData(record.format_id, record.handle)  # type: ignore[attr-defined]
                except Exception as exc:
                    raise WindowsClipboardError(
                        f"SetClipboardData({record.format_name}) a generat o eroare: {exc}"
                    ) from exc
                if not result:
                    raise WindowsClipboardError(
                        _win32_error(api, f"SetClipboardData({record.format_name})")
                    )
                # Windows now owns this handle; never pass it to GlobalFree.
                record.transferred = True
                clipboard_cleared = False
            all_published = True
        except BaseException as exc:
            if isinstance(exc, WindowsClipboardError):
                primary_error = exc
            elif isinstance(exc, Exception):
                primary_error = WindowsClipboardError(f"Operatia clipboard a esuat: {exc}")
            else:
                fatal_exception = exc
                primary_error = WindowsClipboardError("Operatia clipboard a fost intrerupta.")

            if clipboard_changed and not clipboard_cleared:
                cleanup_error = _empty_clipboard(api)
                if cleanup_error:
                    cleanup_errors.append(cleanup_error)
                else:
                    clipboard_cleared = True
    finally:
        # Every successful OpenClipboard is paired with a CloseClipboard attempt.
        close_error = _close_clipboard(api)
        closed = close_error is None
        if close_error:
            cleanup_errors.append(close_error)
            if clipboard_changed and not clipboard_cleared:
                cleanup_error = _empty_clipboard(api)
                if cleanup_error:
                    cleanup_errors.append(cleanup_error)
                else:
                    clipboard_cleared = True
            second_close_error = _close_clipboard(api)
            closed = second_close_error is None
            if second_close_error:
                cleanup_errors.append(second_close_error)

        cleanup_errors.extend(_free_untransferred(api, prepared))

    if fatal_exception is not None:
        if cleanup_errors and hasattr(fatal_exception, "add_note"):
            fatal_exception.add_note("Clipboard cleanup errors: " + "; ".join(cleanup_errors))
        raise fatal_exception
    if primary_error is not None or close_error is not None:
        message = str(primary_error) if primary_error else "CloseClipboard a esuat."
        state_uncertain = not closed or (clipboard_changed and not clipboard_cleared)
        raise WindowsClipboardError(
            message,
            cleanup_errors=tuple(cleanup_errors),
            clipboard_state_uncertain=state_uncertain,
        ) from primary_error
    if not all_published:
        raise WindowsClipboardError("Clipboard-ul nu a primit toate formatele obligatorii.")


def copy_rich_document_to_clipboard(document: RichDocument, owner_hwnd: int) -> None:
    """Copiază un document în format Rich Text, cu fallback-ul Unicode.

    Solicită Windows să excludă elementul din istoricul și sincronizarea
    clipboard-ului. Alte aplicații pot citi în continuare conținutul clipboard-ului.
    """

    if not _is_windows():
        raise WindowsClipboardError("Clipboard-ul Rich Text este disponibil numai pe Windows.")
    api = _create_win32_api()
    _copy_rich_document_with_api(document, owner_hwnd, api)


__all__ = ["WindowsClipboardError", "copy_rich_document_to_clipboard"]

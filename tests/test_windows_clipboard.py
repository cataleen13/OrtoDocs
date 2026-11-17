"""Teste pentru clipboard-ul Windows printr-un adaptor Win32 fals."""

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.models.rich_document import LineBreakInline, Paragraph, RichDocument, TextInline
from ortodocs.services.rtf_codec import encode_rtf
from ortodocs.ui import windows_clipboard as clipboard


def sample_document() -> RichDocument:
    return RichDocument(
        paragraphs=[
            Paragraph(
                [
                    TextInline("Șold și șurub 😀", bold=True),
                    LineBreakInline(),
                    TextInline("Acolade {și} backslash \\", italic=True),
                ]
            ),
            Paragraph([]),
            Paragraph([TextInline("Ultimul paragraf", underline=True)]),
        ]
    )


class FakeWin32Api:
    """API fals care urmărește alocările și transferurile de ownership."""

    def __init__(
        self,
        *,
        invalid_window: bool = False,
        register_failure: str | None = None,
        alloc_failure_at: int | None = None,
        lock_failure_at: int | None = None,
        unlock_failure_at: int | None = None,
        unlock_stays_locked_at: int | None = None,
        unlock_preserve_last_error: bool = False,
        open_results: tuple[bool, ...] = (True,),
        empty_results: dict[int, bool] | None = None,
        set_failure: str | None = None,
        set_interrupt_at: int | None = None,
        close_results: tuple[bool, ...] = (True,),
        free_failure_at: int | None = None,
    ) -> None:
        self.invalid_window = invalid_window
        self.register_failure = register_failure
        self.alloc_failure_at = alloc_failure_at
        self.lock_failure_at = lock_failure_at
        self.unlock_failure_at = unlock_failure_at
        self.unlock_stays_locked_at = unlock_stays_locked_at
        self.unlock_preserve_last_error = unlock_preserve_last_error
        self.open_results = open_results
        self.empty_results = empty_results or {}
        self.set_failure = set_failure
        self.set_interrupt_at = set_interrupt_at
        self.close_results = close_results
        self.free_failure_at = free_failure_at
        self.calls: list[tuple[object, ...]] = []
        self.registered: dict[str, int] = {}
        self.format_names: dict[int, str] = {clipboard.CF_UNICODETEXT: "CF_UNICODETEXT"}
        self.allocations: dict[int, tuple[int, ctypes.Array[ctypes.c_char]]] = {}
        self.transferred: list[int] = []
        self.freed: list[int] = []
        self.system_freed: list[int] = []
        self.clipboard_data: dict[str, bytes] = {}
        self.last_error = 0
        self._next_format = 0xC000
        self._next_handle = 100
        self._register_count = 0
        self._alloc_count = 0
        self._lock_count = 0
        self._unlock_count = 0
        self.unlock_last_errors: list[int] = []
        self._free_count = 0
        self._open_count = 0
        self._empty_count = 0
        self._set_count = 0
        self._close_count = 0

    def get_last_error(self) -> int:
        self.calls.append(("get_last_error",))
        return self.last_error

    def set_last_error(self, error_code: int) -> None:
        self.calls.append(("set_last_error", error_code))
        self.last_error = error_code

    def RegisterClipboardFormatW(self, name: str) -> int:
        self.calls.append(("RegisterClipboardFormatW", name))
        self._register_count += 1
        if name == self.register_failure:
            self.last_error = 87
            return 0
        self._next_format += 1
        self.registered[name] = self._next_format
        self.format_names[self._next_format] = name
        return self._next_format

    def IsWindow(self, hwnd: int) -> bool:
        self.calls.append(("IsWindow", hwnd))
        return not self.invalid_window

    def GlobalAlloc(self, flags: int, size: int) -> int:
        self.calls.append(("GlobalAlloc", flags, size))
        self._alloc_count += 1
        if self._alloc_count == self.alloc_failure_at:
            self.last_error = 8
            return 0
        handle = self._next_handle
        self._next_handle += 1
        self.allocations[handle] = (size, ctypes.create_string_buffer(size))
        return handle

    def GlobalLock(self, handle: int) -> int:
        self.calls.append(("GlobalLock", handle))
        self._lock_count += 1
        if self._lock_count == self.lock_failure_at:
            self.last_error = 6
            return 0
        _size, buffer = self.allocations[handle]
        return ctypes.addressof(buffer)

    def GlobalUnlock(self, handle: int) -> bool:
        self.calls.append(("GlobalUnlock", handle))
        self._unlock_count += 1
        self.unlock_last_errors.append(self.last_error)
        if self._unlock_count == self.unlock_stays_locked_at:
            return True
        if self._unlock_count == self.unlock_failure_at:
            self.last_error = 87
            return False
        # Win32 returns zero when the last lock is successfully released.
        if not self.unlock_preserve_last_error:
            self.last_error = 0
        return False

    def GlobalFree(self, handle: int) -> int:
        self.calls.append(("GlobalFree", handle))
        self._free_count += 1
        if self._free_count == self.free_failure_at:
            self.last_error = 8
            return handle
        self.freed.append(handle)
        self.allocations.pop(handle, None)
        return 0

    def OpenClipboard(self, hwnd: int) -> bool:
        self.calls.append(("OpenClipboard", hwnd))
        self._open_count += 1
        result = self.open_results[self._open_count - 1] if self._open_count <= len(self.open_results) else False
        if not result:
            self.last_error = 5
        return result

    def EmptyClipboard(self) -> bool:
        self.calls.append(("EmptyClipboard",))
        self._empty_count += 1
        result = self.empty_results.get(self._empty_count, True)
        if not result:
            self.last_error = 5
            return False
        self.system_freed.extend(self.transferred)
        self.transferred.clear()
        self.clipboard_data.clear()
        return True

    def SetClipboardData(self, format_id: int, handle: int) -> int:
        name = self.format_names[format_id]
        self.calls.append(("SetClipboardData", name, handle))
        self._set_count += 1
        if self._set_count == self.set_interrupt_at:
            raise KeyboardInterrupt()
        if name == self.set_failure:
            self.last_error = 5
            return 0
        size, buffer = self.allocations[handle]
        self.clipboard_data[name] = buffer.raw[:size]
        self.transferred.append(handle)
        return handle

    def CloseClipboard(self) -> bool:
        self.calls.append(("CloseClipboard",))
        self._close_count += 1
        result = self.close_results[self._close_count - 1] if self._close_count <= len(self.close_results) else True
        if not result:
            self.last_error = 5
        return result

    def call_names(self, name: str) -> list[tuple[object, ...]]:
        return [call for call in self.calls if call[0] == name]


class WindowsClipboardTests(unittest.TestCase):
    def copy_with_fake(
        self,
        api: FakeWin32Api,
        document: RichDocument | object | None = None,
        *,
        sleeps: list[float] | None = None,
    ) -> None:
        clipboard._copy_rich_document_with_api(
            sample_document() if document is None else document, 1234, api,
            sleep=(sleeps if sleeps is not None else []).append,
        )  # type: ignore[arg-type]

    def test_publishes_protections_before_rtf_and_unicode_with_exact_payloads(self) -> None:
        document = sample_document()
        api = FakeWin32Api()

        self.copy_with_fake(api, document)

        expected_text = "Șold și șurub 😀\r\nAcolade {și} backslash \\\r\n\r\nUltimul paragraf"
        expected_payloads = {
            clipboard._EXCLUDE_MONITOR: b"\x01",
            clipboard._CLIPBOARD_HISTORY: b"\x00\x00\x00\x00",
            clipboard._CLOUD_CLIPBOARD: b"\x00\x00\x00\x00",
            clipboard._RTF_FORMAT: encode_rtf(document).encode("ascii") + b"\x00",
            "CF_UNICODETEXT": expected_text.encode("utf-16le") + b"\x00\x00",
        }
        self.assertEqual(api.clipboard_data, expected_payloads)
        set_names = [call[1] for call in api.call_names("SetClipboardData")]
        self.assertEqual(
            set_names,
            [*clipboard._PROTECTION_FORMATS, clipboard._RTF_FORMAT, "CF_UNICODETEXT"],
        )
        self.assertEqual(
            [call[1] for call in api.call_names("RegisterClipboardFormatW")],
            [*clipboard._PROTECTION_FORMATS, clipboard._RTF_FORMAT],
        )
        self.assertEqual([call[1] for call in api.call_names("GlobalAlloc")], [clipboard.GMEM_MOVEABLE] * 5)
        registration_end = max(
            index for index, call in enumerate(api.calls) if call[0] == "RegisterClipboardFormatW"
        )
        allocation_end = max(index for index, call in enumerate(api.calls) if call[0] == "GlobalAlloc")
        open_index = next(index for index, call in enumerate(api.calls) if call[0] == "OpenClipboard")
        empty_index = next(index for index, call in enumerate(api.calls) if call[0] == "EmptyClipboard")
        self.assertLess(registration_end, allocation_end)
        self.assertLess(allocation_end, open_index)
        self.assertLess(open_index, empty_index)
        self.assertEqual(len(api.transferred), 5)
        self.assertEqual(api.freed, [])
        self.assertEqual(api.call_names("CloseClipboard"), [("CloseClipboard",)])

    def test_non_windows_public_api_fails_without_loading_win32(self) -> None:
        with patch.object(clipboard, "_is_windows", return_value=False), patch.object(
            clipboard, "_create_win32_api"
        ) as create_api:
            with self.assertRaisesRegex(clipboard.WindowsClipboardError, "numai pe Windows"):
                clipboard.copy_rich_document_to_clipboard(sample_document(), 1234)
        create_api.assert_not_called()

    def test_invalid_document_is_rejected_before_any_win32_call(self) -> None:
        api = FakeWin32Api()

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "RichDocument"):
            self.copy_with_fake(api, object())

        self.assertEqual(api.calls, [])

    def test_invalid_mutated_document_is_rejected_before_any_win32_call(self) -> None:
        document = RichDocument([Paragraph([TextInline("valid")])])
        document.paragraphs[0].inlines[0].text = "line\nbreak"
        api = FakeWin32Api()

        with self.assertRaises(clipboard.WindowsClipboardError):
            self.copy_with_fake(api, document)

        self.assertEqual(api.calls, [])

    def test_invalid_hwnd_values_are_rejected_before_any_win32_call(self) -> None:
        for hwnd in (0, -1, True, "1234", 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)):
            with self.subTest(hwnd=hwnd):
                api = FakeWin32Api()
                with self.assertRaises(clipboard.WindowsClipboardError):
                    clipboard._copy_rich_document_with_api(sample_document(), hwnd, api)  # type: ignore[arg-type]
                self.assertEqual(api.calls, [])

    def test_hwnd_not_owned_by_a_window_is_rejected_before_registration(self) -> None:
        api = FakeWin32Api(invalid_window=True)

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "owner_hwnd"):
            self.copy_with_fake(api)

        self.assertEqual(api.call_names("IsWindow"), [("IsWindow", 1234)])
        self.assertEqual(api.call_names("RegisterClipboardFormatW"), [])
        self.assertEqual(api.call_names("OpenClipboard"), [])

    def test_protection_format_registration_failure_prevents_text_publication(self) -> None:
        for format_name in (*clipboard._PROTECTION_FORMATS, clipboard._RTF_FORMAT):
            with self.subTest(format_name=format_name):
                api = FakeWin32Api(register_failure=format_name)
                with self.assertRaises(clipboard.WindowsClipboardError):
                    self.copy_with_fake(api)
                self.assertEqual(api.call_names("GlobalAlloc"), [])
                self.assertEqual(api.call_names("OpenClipboard"), [])
                self.assertEqual(api.call_names("SetClipboardData"), [])

    def test_allocation_failure_frees_previously_allocated_handles(self) -> None:
        api = FakeWin32Api(alloc_failure_at=4)

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "GlobalAlloc"):
            self.copy_with_fake(api)

        self.assertEqual(len(api.freed), 3)
        self.assertEqual(len(set(api.freed)), 3)
        self.assertEqual(api.call_names("OpenClipboard"), [])
        self.assertEqual(api.call_names("SetClipboardData"), [])

    def test_lock_failure_frees_current_and_prior_allocations(self) -> None:
        api = FakeWin32Api(lock_failure_at=3)

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "GlobalLock"):
            self.copy_with_fake(api)

        self.assertEqual(len(api.freed), 3)
        self.assertEqual(len(set(api.freed)), 3)
        self.assertEqual(api.call_names("OpenClipboard"), [])

    def test_unlock_failure_frees_memory_and_prevents_clipboard_open(self) -> None:
        api = FakeWin32Api(unlock_failure_at=2)

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "GlobalUnlock"):
            self.copy_with_fake(api)

        self.assertEqual(len(api.freed), 2)
        self.assertEqual(api.unlock_last_errors, [0, 0])
        self.assertEqual(api.call_names("OpenClipboard"), [])

    def test_global_unlock_true_means_memory_remains_locked(self) -> None:
        api = FakeWin32Api(unlock_stays_locked_at=1)

        with self.assertRaises(clipboard.WindowsClipboardError) as raised:
            self.copy_with_fake(api)

        self.assertIn("memoria blocată", str(raised.exception))
        self.assertEqual(api.unlock_last_errors, [0])
        self.assertEqual(api.call_names("OpenClipboard"), [])
        self.assertEqual(len(api.freed), 1)

    def test_stale_last_error_is_cleared_before_zero_result_unlock(self) -> None:
        api = FakeWin32Api(unlock_preserve_last_error=True)
        api.last_error = 123

        self.copy_with_fake(api)

        self.assertEqual(api.unlock_last_errors, [0] * 5)
        self.assertEqual(api.call_names("set_last_error"), [("set_last_error", 0)] * 5)
        self.assertEqual(len(api.call_names("SetClipboardData")), 5)

    def test_cleanup_reports_global_free_failure_without_double_free(self) -> None:
        api = FakeWin32Api(alloc_failure_at=3, free_failure_at=1)

        with self.assertRaises(clipboard.WindowsClipboardError) as raised:
            self.copy_with_fake(api)

        self.assertTrue(any("GlobalFree" in item for item in raised.exception.cleanup_errors))
        self.assertEqual(len(api.call_names("GlobalFree")), 2)
        self.assertEqual(len({call[1] for call in api.call_names("GlobalFree")}), 2)

    def test_open_clipboard_retries_and_uses_valid_window_owner(self) -> None:
        api = FakeWin32Api(open_results=(False, True))
        sleeps: list[float] = []

        self.copy_with_fake(api, sleeps=sleeps)

        self.assertEqual(sleeps, [clipboard._OPEN_RETRY_DELAYS[0]])
        self.assertEqual(api.call_names("OpenClipboard"), [("OpenClipboard", 1234)] * 2)
        self.assertEqual(len(api.call_names("SetClipboardData")), 5)

    def test_open_clipboard_exhaustion_frees_all_handles_without_close(self) -> None:
        api = FakeWin32Api(open_results=(False, False, False, False))
        sleeps: list[float] = []

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "OpenClipboard"):
            self.copy_with_fake(api, sleeps=sleeps)

        self.assertEqual(sleeps, list(clipboard._OPEN_RETRY_DELAYS))
        self.assertEqual(len(api.call_names("OpenClipboard")), 4)
        self.assertEqual(len(api.freed), 5)
        self.assertEqual(api.call_names("EmptyClipboard"), [])
        self.assertEqual(api.call_names("CloseClipboard"), [])

    def test_open_retry_interruptions_propagate_and_free_all_prepared_handles(self) -> None:
        for interrupt_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt_type=interrupt_type.__name__):
                api = FakeWin32Api(open_results=(False,))

                def interrupt_sleep(_delay: float) -> None:
                    raise interrupt_type()

                with self.assertRaises(interrupt_type):
                    clipboard._copy_rich_document_with_api(
                        sample_document(), 1234, api, sleep=interrupt_sleep
                    )

                self.assertEqual(len(api.freed), 5)
                self.assertEqual(len(set(api.freed)), 5)
                self.assertEqual(len(api.call_names("OpenClipboard")), 1)
                self.assertEqual(api.call_names("EmptyClipboard"), [])
                self.assertEqual(api.call_names("CloseClipboard"), [])

    def test_open_retry_interrupt_preserves_cleanup_failure_note(self) -> None:
        api = FakeWin32Api(open_results=(False,), free_failure_at=1)

        def interrupt_sleep(_delay: float) -> None:
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt) as raised:
            clipboard._copy_rich_document_with_api(
                sample_document(), 1234, api, sleep=interrupt_sleep
            )

        notes = getattr(raised.exception, "__notes__", [])
        self.assertTrue(any("GlobalFree" in note for note in notes))
        self.assertEqual(len(api.freed), 4)
        self.assertEqual(api.call_names("EmptyClipboard"), [])
        self.assertEqual(api.call_names("CloseClipboard"), [])

    def test_open_retry_ordinary_sleep_error_remains_clipboard_error(self) -> None:
        api = FakeWin32Api(open_results=(False,))

        def failing_sleep(_delay: float) -> None:
            raise RuntimeError("retry wait unavailable")

        with self.assertRaises(clipboard.WindowsClipboardError) as raised:
            clipboard._copy_rich_document_with_api(
                sample_document(), 1234, api, sleep=failing_sleep
            )

        self.assertIn("retry wait unavailable", str(raised.exception))
        self.assertEqual(len(api.freed), 5)
        self.assertEqual(api.call_names("EmptyClipboard"), [])
        self.assertEqual(api.call_names("CloseClipboard"), [])

    def test_initial_empty_failure_closes_clipboard_and_frees_all_handles(self) -> None:
        api = FakeWin32Api(empty_results={1: False})

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "EmptyClipboard"):
            self.copy_with_fake(api)

        self.assertEqual(api.call_names("SetClipboardData"), [])
        self.assertEqual(len(api.freed), 5)
        self.assertEqual(api.call_names("CloseClipboard"), [("CloseClipboard",)])

    def test_each_set_failure_stops_later_publications_and_frees_only_untransferred(self) -> None:
        expected_names = [*clipboard._PROTECTION_FORMATS, clipboard._RTF_FORMAT, "CF_UNICODETEXT"]
        for failed_index, failed_name in enumerate(expected_names):
            with self.subTest(failed_name=failed_name):
                api = FakeWin32Api(set_failure=failed_name)
                with self.assertRaisesRegex(clipboard.WindowsClipboardError, "SetClipboardData"):
                    self.copy_with_fake(api)
                published_calls = api.call_names("SetClipboardData")
                self.assertEqual([call[1] for call in published_calls], expected_names[: failed_index + 1])
                self.assertEqual(len(api.transferred), 0)
                self.assertEqual(len(api.system_freed), failed_index)
                # Handles transferred before the failure are released by EmptyClipboard,
                # while remaining handles are released explicitly with GlobalFree.
                self.assertEqual(len(api.freed), 5 - failed_index)
                self.assertEqual(len(api.call_names("CloseClipboard")), 1)

    def test_failed_cleanup_marks_clipboard_state_uncertain(self) -> None:
        api = FakeWin32Api(set_failure=clipboard._RTF_FORMAT, empty_results={2: False})

        with self.assertRaises(clipboard.WindowsClipboardError) as raised:
            self.copy_with_fake(api)

        self.assertTrue(raised.exception.clipboard_state_uncertain)
        self.assertTrue(any("EmptyClipboard" in item for item in raised.exception.cleanup_errors))
        self.assertIn("Starea clipboard", str(raised.exception))
        self.assertEqual(len(api.call_names("SetClipboardData")), 4)
        self.assertEqual(len(api.call_names("CloseClipboard")), 1)

    def test_close_failure_clears_published_data_retries_close_and_reports_failure(self) -> None:
        api = FakeWin32Api(close_results=(False, True))

        with self.assertRaisesRegex(clipboard.WindowsClipboardError, "CloseClipboard") as raised:
            self.copy_with_fake(api)

        self.assertEqual(len(api.call_names("SetClipboardData")), 5)
        self.assertEqual(len(api.call_names("EmptyClipboard")), 2)
        self.assertEqual(len(api.call_names("CloseClipboard")), 2)
        self.assertEqual(api.clipboard_data, {})
        self.assertFalse(raised.exception.clipboard_state_uncertain)
        self.assertEqual(api.freed, [])

    def test_cleanup_and_repeated_close_failures_are_reported_as_uncertain(self) -> None:
        api = FakeWin32Api(
            set_failure=clipboard._RTF_FORMAT,
            empty_results={2: False, 3: False},
            close_results=(False, False),
        )

        with self.assertRaises(clipboard.WindowsClipboardError) as raised:
            self.copy_with_fake(api)

        self.assertTrue(raised.exception.clipboard_state_uncertain)
        self.assertGreaterEqual(len(raised.exception.cleanup_errors), 3)
        self.assertEqual(len(api.call_names("CloseClipboard")), 2)
        self.assertEqual(len(api.call_names("SetClipboardData")), 4)

    def test_keyboard_interrupt_after_open_still_cleans_up_and_closes_clipboard(self) -> None:
        api = FakeWin32Api(set_interrupt_at=2)

        with self.assertRaises(KeyboardInterrupt):
            self.copy_with_fake(api)

        self.assertEqual(len(api.call_names("CloseClipboard")), 1)
        self.assertEqual(len(api.call_names("EmptyClipboard")), 2)
        self.assertEqual(api.clipboard_data, {})
        transferred = set(api.system_freed)
        freed = {call[1] for call in api.call_names("GlobalFree")}
        self.assertTrue(transferred.isdisjoint(freed))
        self.assertEqual(len(api.freed), 4)

    def test_successful_set_handles_are_never_released_with_global_free(self) -> None:
        api = FakeWin32Api()

        self.copy_with_fake(api)

        transferred_handles = {call[2] for call in api.call_names("SetClipboardData")}
        freed_handles = {call[1] for call in api.call_names("GlobalFree")}
        self.assertTrue(transferred_handles.isdisjoint(freed_handles))
        self.assertEqual(len(transferred_handles), 5)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SOURCE_DIRECTORY = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from ortodocs.main import main
from ortodocs.ui.dpi import (
    DPI_AWARENESS_CONTEXT_SYSTEM_AWARE,
    DPI_AWARENESS_PER_MONITOR_AWARE,
    DPI_AWARENESS_SYSTEM_AWARE,
    DPI_AWARENESS_UNAWARE,
    DpiConfigurationResult,
    collect_tk_dpi_diagnostics,
    configure_windows_dpi_awareness,
    format_dpi_diagnostics,
    get_current_windows_dpi_awareness,
)


class _FakeUser32:
    def __init__(
        self,
        *,
        initial_awareness: int = DPI_AWARENESS_UNAWARE,
        set_context_result: int | None = 1,
        raise_on_set: bool = False,
        has_set_context: bool = True,
        window_dpi: int = 144,
    ) -> None:
        self.awareness = initial_awareness
        self.set_context_result = set_context_result
        self.raise_on_set = raise_on_set
        self.window_dpi = window_dpi
        self.calls: list[int | None] = []
        if has_set_context:
            self.SetProcessDpiAwarenessContext = self._set_context

    def GetThreadDpiAwarenessContext(self) -> int:
        return self.awareness

    def GetAwarenessFromDpiAwarenessContext(self, ctx: int) -> int:
        return int(ctx)

    def _set_context(self, value: ctypes.c_void_p) -> int:
        raw = value.value if isinstance(value, ctypes.c_void_p) else int(value)
        self.calls.append(raw)
        if self.raise_on_set:
            raise OSError("SetProcessDpiAwarenessContext unsupported")
        if self.set_context_result:
            self.awareness = DPI_AWARENESS_SYSTEM_AWARE
            return int(self.set_context_result)
        return 0

    def GetDpiForWindow(self, _hwnd: int) -> int:
        return self.window_dpi


class _FakeShcore:
    def __init__(
        self,
        *,
        user32: _FakeUser32,
        hr: int = 0,
        raise_on_set: bool = False,
        has_set_process: bool = True,
    ) -> None:
        self._user32 = user32
        self.hr = hr
        self.raise_on_set = raise_on_set
        self.calls: list[int] = []
        if has_set_process:
            self.SetProcessDpiAwareness = self._set_process

    def GetProcessDpiAwareness(self, _handle: int, out_ptr: object) -> int:
        # ctypes.byref(c_int) exposes ._obj
        out_ptr._obj.value = self._user32.awareness  # type: ignore[attr-defined]
        return 0

    def _set_process(self, level: int) -> int:
        self.calls.append(level)
        if self.raise_on_set:
            raise OSError("E_ACCESSDENIED")
        if self.hr == 0:
            self._user32.awareness = level
        return self.hr


class WindowsDpiConfigurationTests(unittest.TestCase):
    def test_skips_cleanly_on_non_windows_platforms(self) -> None:
        for platform in ("linux", "darwin"):
            result = configure_windows_dpi_awareness(platform=platform)
            self.assertEqual(result.status, "skipped_non_windows")
            self.assertIsNone(result.api_used)
            self.assertIsNone(get_current_windows_dpi_awareness(platform=platform))

    def test_does_not_reconfigure_when_already_system_or_per_monitor_aware(self) -> None:
        for level in (DPI_AWARENESS_SYSTEM_AWARE, DPI_AWARENESS_PER_MONITOR_AWARE):
            user32 = _FakeUser32(initial_awareness=level)
            shcore = _FakeShcore(user32=user32)
            windll = SimpleNamespace(user32=user32, shcore=shcore)

            result = configure_windows_dpi_awareness(platform="win32", windll_obj=windll)
            self.assertEqual(result.status, "already_aware")
            self.assertIsNone(result.api_used)
            self.assertEqual(result.awareness_before, level)
            self.assertEqual(result.awareness_after, level)
            self.assertEqual(user32.calls, [])
            self.assertEqual(shcore.calls, [])

    def test_uses_set_process_dpi_awareness_context_system_aware_first(self) -> None:
        user32 = _FakeUser32(initial_awareness=DPI_AWARENESS_UNAWARE)
        shcore = _FakeShcore(user32=user32)
        windll = SimpleNamespace(user32=user32, shcore=shcore)

        result = configure_windows_dpi_awareness(platform="win32", windll_obj=windll)
        self.assertEqual(result.status, "configured_context")
        self.assertEqual(result.api_used, "SetProcessDpiAwarenessContext")
        self.assertEqual(
            user32.calls,
            [ctypes.c_void_p(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE).value],
        )
        self.assertEqual(shcore.calls, [])
        self.assertEqual(result.awareness_after, DPI_AWARENESS_SYSTEM_AWARE)

    def test_falls_back_to_shcore_set_process_dpi_awareness_when_context_api_unavailable_or_fails(self) -> None:
        user32 = _FakeUser32(
            initial_awareness=DPI_AWARENESS_UNAWARE,
            has_set_context=False,
        )
        shcore = _FakeShcore(user32=user32, hr=0)
        windll = SimpleNamespace(user32=user32, shcore=shcore)

        result = configure_windows_dpi_awareness(platform="win32", windll_obj=windll)
        self.assertEqual(result.status, "configured_shcore")
        self.assertEqual(result.api_used, "SetProcessDpiAwareness")
        self.assertEqual(shcore.calls, [DPI_AWARENESS_SYSTEM_AWARE])
        self.assertEqual(result.awareness_after, DPI_AWARENESS_SYSTEM_AWARE)

        # Fallback când SetProcessDpiAwarenessContext returnează False/0
        user32_false = _FakeUser32(
            initial_awareness=DPI_AWARENESS_UNAWARE,
            set_context_result=0,
        )
        shcore_false = _FakeShcore(user32=user32_false, hr=0)
        windll_false = SimpleNamespace(user32=user32_false, shcore=shcore_false)
        result_false = configure_windows_dpi_awareness(
            platform="win32", windll_obj=windll_false
        )
        self.assertEqual(result_false.status, "configured_shcore")
        self.assertEqual(shcore_false.calls, [DPI_AWARENESS_SYSTEM_AWARE])

    def test_failed_call_when_process_already_aware_is_reported_as_already_aware(self) -> None:
        user32 = _FakeUser32(
            initial_awareness=DPI_AWARENESS_UNAWARE,
            set_context_result=0,
        )
        shcore = _FakeShcore(user32=user32, hr=-2147024891)

        # Simulăm situația în care verificarea post-eșec vede că procesul este deja System Aware (ex. manifest)
        original_get_aw = user32.GetAwarenessFromDpiAwarenessContext
        calls = {"n": 0}

        def _post_aware(_ctx: int) -> int:
            calls["n"] += 1
            if calls["n"] >= 2:
                return DPI_AWARENESS_SYSTEM_AWARE
            return original_get_aw(DPI_AWARENESS_UNAWARE)

        user32.GetAwarenessFromDpiAwarenessContext = _post_aware  # type: ignore[method-assign]
        windll = SimpleNamespace(user32=user32, shcore=shcore)

        result = configure_windows_dpi_awareness(platform="win32", windll_obj=windll)
        self.assertEqual(result.status, "already_aware")
        self.assertEqual(result.awareness_after, DPI_AWARENESS_SYSTEM_AWARE)

    def test_failure_or_missing_apis_never_raise_or_block_startup(self) -> None:
        user32 = _FakeUser32(initial_awareness=DPI_AWARENESS_UNAWARE, raise_on_set=True)
        shcore = _FakeShcore(user32=user32, raise_on_set=True)
        windll_fail = SimpleNamespace(user32=user32, shcore=shcore)

        failed = configure_windows_dpi_awareness(platform="win32", windll_obj=windll_fail)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.awareness_after, DPI_AWARENESS_UNAWARE)

        windll_empty = SimpleNamespace(user32=SimpleNamespace(), shcore=SimpleNamespace())
        unavailable = configure_windows_dpi_awareness(
            platform="win32", windll_obj=windll_empty
        )
        self.assertEqual(unavailable.status, "unavailable")

    def test_main_configures_dpi_awareness_strictly_before_creating_tk_root(self) -> None:
        order: list[str] = []

        def _fake_dpi() -> DpiConfigurationResult:
            order.append("dpi")
            return DpiConfigurationResult("configured_context", "SetProcessDpiAwarenessContext", 0, 1)

        class _FakeTk:
            def withdraw(self) -> None:
                order.append("withdraw")

            def deiconify(self) -> None:
                order.append("deiconify")

            def destroy(self) -> None:
                order.append("destroy")

        def _make_tk() -> _FakeTk:
            order.append("tk")
            return _FakeTk()

        with (
            patch("ortodocs.main.configure_windows_dpi_awareness", side_effect=_fake_dpi),
            patch("ortodocs.main.tk.Tk", side_effect=_make_tk),
            patch("ortodocs.main.initialize_database", side_effect=RuntimeError("stop-early")),
            patch("ortodocs.main.messagebox.showerror"),
        ):
            main()

        self.assertEqual(order[:3], ["dpi", "tk", "withdraw"])

    def test_collect_and_format_tk_dpi_diagnostics(self) -> None:
        class _FakeTkInterp:
            def call(self, *args: object) -> object:
                if args == ("info", "patchlevel"):
                    return "9.0.4"
                if args == ("tk", "scaling"):
                    return 2.0
                raise AssertionError(args)

        class _FakeRoot:
            tk = _FakeTkInterp()

            def update_idletasks(self) -> None:
                pass

            def winfo_id(self) -> int:
                return 1234

            def winfo_fpixels(self, _spec: str) -> float:
                return 144.0

            def winfo_screenwidth(self) -> int:
                return 2560

            def winfo_screenheight(self) -> int:
                return 1600

        user32 = _FakeUser32(initial_awareness=DPI_AWARENESS_SYSTEM_AWARE, window_dpi=144)
        windll = SimpleNamespace(user32=user32, shcore=_FakeShcore(user32=user32))
        info = collect_tk_dpi_diagnostics(_FakeRoot(), platform="win32", windll_obj=windll)  # type: ignore[arg-type]
        self.assertEqual(info["process_dpi_awareness"], 1)
        self.assertEqual(info["window_dpi"], 144)
        self.assertEqual(info["tk_scaling"], 2.0)
        self.assertEqual(info["screen_width"], 2560)
        self.assertEqual(info["screen_height"], 1600)
        formatted = format_dpi_diagnostics(
            info,
            config_result=DpiConfigurationResult("configured_context", "SetProcessDpiAwarenessContext", 0, 1),
        )
        self.assertIn("PROCESS_SYSTEM_DPI_AWARE (1)", formatted)
        self.assertIn("DPI fereastră: 144", formatted)
        self.assertIn("2560x1600", formatted)


if __name__ == "__main__":
    unittest.main()

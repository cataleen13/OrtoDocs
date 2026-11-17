"""Configurare System DPI Awareness (Windows) și diagnostic DPI Tkinter."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import tkinter as tk
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2

_DPI_AWARENESS_NAMES = {
    DPI_AWARENESS_UNAWARE: "PROCESS_DPI_UNAWARE (0)",
    DPI_AWARENESS_SYSTEM_AWARE: "PROCESS_SYSTEM_DPI_AWARE (1)",
    DPI_AWARENESS_PER_MONITOR_AWARE: "PROCESS_PER_MONITOR_DPI_AWARE (2)",
}


@dataclass(frozen=True, slots=True)
class DpiConfigurationResult:
    """Rezultatul configurării DPI awareness pentru procesul curent."""

    status: str
    api_used: str | None
    awareness_before: int | None
    awareness_after: int | None


def _get_windll(windll_obj: object | None) -> Any:
    if windll_obj is not None:
        return windll_obj
    return getattr(ctypes, "windll", None)


def get_current_windows_dpi_awareness(
    *,
    platform: str | None = None,
    windll_obj: object | None = None,
) -> int | None:
    """Citește nivelul curent de DPI awareness fără a modifica starea procesului."""

    effective_platform = sys.platform if platform is None else platform
    if effective_platform != "win32":
        return None
    windll = _get_windll(windll_obj)
    if windll is None:
        return None

    try:
        user32 = getattr(windll, "user32", None)
        if user32 is not None:
            get_ctx = getattr(user32, "GetThreadDpiAwarenessContext", None)
            get_aw = getattr(user32, "GetAwarenessFromDpiAwarenessContext", None)
            if callable(get_ctx) and callable(get_aw):
                if windll_obj is None:
                    get_ctx.restype = ctypes.c_void_p
                    get_aw.argtypes = [ctypes.c_void_p]
                    get_aw.restype = ctypes.c_int
                ctx = get_ctx()
                awareness = int(get_aw(ctx))
                if awareness >= 0:
                    return awareness
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    try:
        shcore = getattr(windll, "shcore", None)
        if shcore is not None:
            get_proc = getattr(shcore, "GetProcessDpiAwareness", None)
            if callable(get_proc):
                value = ctypes.c_int(-1)
                hr = int(get_proc(0, ctypes.byref(value)))
                if hr == 0 and value.value >= 0:
                    return int(value.value)
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    return None


def _extract_awareness_arg(value: ctypes.c_void_p) -> int | None:
    return value.value if isinstance(value, ctypes.c_void_p) else None


def configure_windows_dpi_awareness(
    *,
    platform: str | None = None,
    windll_obj: object | None = None,
) -> DpiConfigurationResult:
    """Activează System DPI Awareness pe Windows înainte de crearea primei ferestre Tk."""

    effective_platform = sys.platform if platform is None else platform
    if effective_platform != "win32":
        return DpiConfigurationResult(
            status="skipped_non_windows",
            api_used=None,
            awareness_before=None,
            awareness_after=None,
        )

    windll = _get_windll(windll_obj)
    if windll is None:
        return DpiConfigurationResult(
            status="unavailable",
            api_used=None,
            awareness_before=None,
            awareness_after=None,
        )

    before = get_current_windows_dpi_awareness(
        platform=effective_platform,
        windll_obj=windll,
    )
    if before in (DPI_AWARENESS_SYSTEM_AWARE, DPI_AWARENESS_PER_MONITOR_AWARE):
        return DpiConfigurationResult(
            status="already_aware",
            api_used=None,
            awareness_before=before,
            awareness_after=before,
        )

    api_seen = False

    # 1. Preferat pe Windows 10/11: SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)
    user32 = getattr(windll, "user32", None)
    set_context = (
        getattr(user32, "SetProcessDpiAwarenessContext", None)
        if user32 is not None
        else None
    )
    if callable(set_context):
        api_seen = True
        try:
            if windll_obj is None:
                set_context.argtypes = [ctypes.c_void_p]
                set_context.restype = ctypes.wintypes.BOOL
            arg = ctypes.c_void_p(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)
            ok = bool(set_context(arg))
        except (AttributeError, OSError, TypeError, ValueError):
            ok = False

        after = get_current_windows_dpi_awareness(
            platform=effective_platform,
            windll_obj=windll,
        )
        if ok:
            return DpiConfigurationResult(
                status="configured_context",
                api_used="SetProcessDpiAwarenessContext",
                awareness_before=before,
                awareness_after=(
                    after if after is not None else DPI_AWARENESS_SYSTEM_AWARE
                ),
            )
        if after in (DPI_AWARENESS_SYSTEM_AWARE, DPI_AWARENESS_PER_MONITOR_AWARE):
            return DpiConfigurationResult(
                status="already_aware",
                api_used="SetProcessDpiAwarenessContext",
                awareness_before=before,
                awareness_after=after,
            )

    # 2. Fallback compatibil: shcore.SetProcessDpiAwareness(PROCESS_SYSTEM_DPI_AWARE)
    shcore = getattr(windll, "shcore", None)
    set_process = (
        getattr(shcore, "SetProcessDpiAwareness", None)
        if shcore is not None
        else None
    )
    if callable(set_process):
        api_seen = True
        try:
            if windll_obj is None:
                set_process.argtypes = [ctypes.c_int]
                set_process.restype = ctypes.c_long
            hr = int(set_process(DPI_AWARENESS_SYSTEM_AWARE))
        except (AttributeError, OSError, TypeError, ValueError):
            hr = -1

        after = get_current_windows_dpi_awareness(
            platform=effective_platform,
            windll_obj=windll,
        )
        if hr == 0:
            return DpiConfigurationResult(
                status="configured_shcore",
                api_used="SetProcessDpiAwareness",
                awareness_before=before,
                awareness_after=(
                    after if after is not None else DPI_AWARENESS_SYSTEM_AWARE
                ),
            )
        if after in (DPI_AWARENESS_SYSTEM_AWARE, DPI_AWARENESS_PER_MONITOR_AWARE):
            return DpiConfigurationResult(
                status="already_aware",
                api_used="SetProcessDpiAwareness",
                awareness_before=before,
                awareness_after=after,
            )

    final_awareness = get_current_windows_dpi_awareness(
        platform=effective_platform,
        windll_obj=windll,
    )
    return DpiConfigurationResult(
        status="failed" if api_seen else "unavailable",
        api_used=None,
        awareness_before=before,
        awareness_after=final_awareness,
    )


def collect_tk_dpi_diagnostics(
    root: tk.Misc,
    *,
    platform: str | None = None,
    windll_obj: object | None = None,
) -> dict[str, object]:
    """Colectează valorile DPI și de scalare raportate de Windows și Tkinter."""

    effective_platform = sys.platform if platform is None else platform
    awareness = get_current_windows_dpi_awareness(
        platform=effective_platform,
        windll_obj=windll_obj,
    )
    window_dpi: int | None = None
    if effective_platform == "win32":
        windll = _get_windll(windll_obj)
        user32 = getattr(windll, "user32", None) if windll is not None else None
        get_dpi_for_window = (
            getattr(user32, "GetDpiForWindow", None) if user32 is not None else None
        )
        if callable(get_dpi_for_window):
            try:
                root.update_idletasks()
                hwnd = int(root.winfo_id())
                if windll_obj is None:
                    get_dpi_for_window.argtypes = [ctypes.wintypes.HWND]
                    get_dpi_for_window.restype = ctypes.wintypes.UINT
                value = int(get_dpi_for_window(hwnd))
                if value > 0:
                    window_dpi = value
            except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
                window_dpi = None

    tcl_tk_version = str(root.tk.call("info", "patchlevel"))
    tk_scaling = float(root.tk.call("tk", "scaling"))
    fpixels_1i = float(root.winfo_fpixels("1i"))
    screen_width = int(root.winfo_screenwidth())
    screen_height = int(root.winfo_screenheight())

    return {
        "tcl_tk_version": tcl_tk_version,
        "process_dpi_awareness": awareness,
        "process_dpi_awareness_label": (
            _DPI_AWARENESS_NAMES.get(awareness, str(awareness))
            if awareness is not None
            else "indisponibil"
        ),
        "window_dpi": window_dpi,
        "tk_scaling": tk_scaling,
        "fpixels_1i": fpixels_1i,
        "screen_width": screen_width,
        "screen_height": screen_height,
    }


def format_dpi_diagnostics(
    info: Mapping[str, object],
    *,
    config_result: DpiConfigurationResult | None = None,
) -> str:
    """Formatează sumarul DPI pentru diagnostic în terminal."""

    parts = [
        f"DPI awareness: {info.get('process_dpi_awareness_label')}",
        f"DPI fereastră: {info.get('window_dpi') if info.get('window_dpi') is not None else 'indisponibil'}",
        f"tk scaling: {float(info['tk_scaling']):.4f}" if "tk_scaling" in info else "tk scaling: ?",
        f"winfo_fpixels('1i'): {float(info['fpixels_1i']):.2f}" if "fpixels_1i" in info else "winfo_fpixels: ?",
        f"Ecran Tk: {info.get('screen_width')}x{info.get('screen_height')}",
        f"Tcl/Tk: {info.get('tcl_tk_version')}",
    ]
    if config_result is not None:
        parts.append(f"Configurare DPI: {config_result.status}")
    return "[OrtoDocs DPI] " + " | ".join(parts)

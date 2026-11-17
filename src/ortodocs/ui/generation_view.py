"""Ecranul de selectare a contextului și completare a formularului de generare."""

from __future__ import annotations

import json
import locale
import os
import sqlite3
import sys
import tempfile
import tkinter as tk
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import ttk

from tk_date_entry import DateEntry

from ortodocs.models.generation import (
    FieldValidationIssue,
    GeneratedSession,
    GenerationField,
    GenerationForm,
)
from ortodocs.services import document_generation, medical_library
from ortodocs.services.document_generation import (
    GenerationConfigurationError,
    GenerationConflictError,
    GenerationContextError,
    GenerationTemplateError,
    GenerationValidationError,
)
from ortodocs.storage.generation_repositories import GenerationPersistenceError
from ortodocs.ui.generated_documents_window import GeneratedDocumentsWindow
from ortodocs.ui.theme import (
    BG_SURFACE,
    BORDER,
    CardFrame,
    SPACE_MD,
    apply_soft_medical_theme,
)

STANDARD_FIELD_KEYS = frozenset(
    {
        "varsta",
        "sex",
        "lateralitate",
        "regim_prezentare",
        "data_operatiei",
        "medic_anestezist",
    }
)


DOCUMENT_LABELS = {
    "epicrisis_recommendations": "Epicriză + Recomandări",
    "operative_protocol": "Protocol operator",
}

SCROLL_DIAG_ENV_VAR = "ORTODOCS_SCROLL_DIAG"
_SCROLL_DIAG_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SCROLL_DIAG_FALSY = frozenset({"", "0", "false", "no", "off"})
_WIN32_SCROLL_MESSAGES = {
    0x020A: "WM_MOUSEWHEEL",
    0x020E: "WM_MOUSEHWHEEL",
    0x0119: "WM_GESTURE",
    0x024E: "WM_POINTERWHEEL",
    0x024F: "WM_POINTERHWHEEL",
}


def unpack_touchpad_scroll_deltas(raw_delta: int | float) -> tuple[int, int]:
    """Extrage (delta_x, delta_y) ca întregi cu semn pe 16 biți din %D al <TouchpadScroll>."""

    packed = int(raw_delta)
    dx_unsigned = (packed >> 16) & 0xFFFF
    dy_unsigned = packed & 0xFFFF
    delta_x = dx_unsigned - 0x10000 if dx_unsigned >= 0x8000 else dx_unsigned
    delta_y = dy_unsigned - 0x10000 if dy_unsigned >= 0x8000 else dy_unsigned
    return delta_x, delta_y


def _resolve_scroll_diag_path() -> Path | None:
    """Returnează calea fișierului de diagnostic doar când ORTODOCS_SCROLL_DIAG este activat."""

    raw_value = os.environ.get(SCROLL_DIAG_ENV_VAR, "").strip()
    lowered = raw_value.lower()
    if lowered in _SCROLL_DIAG_FALSY:
        return None
    if lowered in _SCROLL_DIAG_TRUTHY:
        return Path(tempfile.gettempdir()) / "ortodocs_step3_scroll_diag.log"
    return Path(raw_value)


def _event_pointer_coords(event: tk.Event[tk.Misc]) -> tuple[int, int] | None:
    """Returnează (x_root, y_root) dacă evenimentul are coordonate globale populate."""

    x_root = int(getattr(event, "x_root", -1))
    y_root = int(getattr(event, "y_root", -1))
    if x_root == -1 and y_root == -1:
        return None
    return x_root, y_root


def _describe_widget_meta(widget: tk.Misc | None) -> str | None:
    """Descrie exclusiv clasa/tag-ul tehnic al widgetului, fără date din formular."""

    if widget is None:
        return None
    try:
        if not widget.winfo_exists():
            return "<destroyed>"
        return f"{widget.winfo_class()}:{widget!s}"
    except tk.TclError:
        return "<unavailable>"


class _Step3ScrollDiagnosticLogger:
    """Logger temporar de diagnostic activat exclusiv prin ORTODOCS_SCROLL_DIAG."""

    def __init__(self, view: "GenerationView", log_path: Path) -> None:
        self.view = view
        self.log_path = log_path
        self._win32_hooks: list[tuple[int, int, object]] = []
        self._installed = False
        self._active = True
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.record(stage="diag_init", event_type="init")

    def install(self) -> None:
        if self._installed:
            return
        self._active = True
        for sequence in ("<MouseWheel>", "<TouchpadScroll>", "<Shift-MouseWheel>"):
            try:
                self.view.bind_all(
                    sequence,
                    lambda event, seq=sequence: self._on_bind_all_event(seq, event),
                    add="+",
                )
            except tk.TclError:
                continue
        if sys.platform == "win32":
            self._install_win32_observers()
        self._installed = True

    def uninstall(self) -> None:
        self._active = False
        if sys.platform == "win32" and self._win32_hooks:
            self._uninstall_win32_hooks()
        self._installed = False

    def _on_bind_all_event(self, sequence: str, event: tk.Event[tk.Misc]) -> None:
        if not self._active:
            return
        self.record_tk_event(
            stage="bind_all_observer",
            event_name=sequence,
            event=event,
            handler_called=False,
            ignored_reason="observer_only",
            yview_before=self._canvas_yview(),
            yview_after=None,
        )

    def _canvas_yview(self) -> list[float] | None:
        canvas = self.view.form_canvas
        if canvas is None:
            return None
        try:
            if not canvas.winfo_exists():
                return None
            first, last = canvas.yview()
            return [round(float(first), 6), round(float(last), 6)]
        except tk.TclError:
            return None

    def _pointer_widget_summary(self, event: tk.Event[tk.Misc]) -> dict[str, object]:
        coords = _event_pointer_coords(event)
        pointer_widget: tk.Misc | None = None
        if coords is not None:
            try:
                pointer_widget = self.view.winfo_containing(coords[0], coords[1])
            except (tk.TclError, KeyError):
                pointer_widget = None
        target_widget = getattr(event, "widget", None)
        return {
            "coords": coords,
            "event_widget": _describe_widget_meta(target_widget),
            "pointer_widget": _describe_widget_meta(pointer_widget),
            "pointer_in_step3": (
                self.view._scroll_bindtag in pointer_widget.bindtags()
                if pointer_widget is not None and pointer_widget.winfo_exists()
                else False
            ),
        }

    def record_tk_event(
        self,
        *,
        stage: str,
        event_name: str,
        event: tk.Event[tk.Misc],
        handler_called: bool,
        ignored_reason: str | None,
        yview_before: list[float] | None,
        yview_after: list[float] | None,
        delta_xy: tuple[int, int] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "stage": stage,
            "event_name": event_name,
            "event_type": str(getattr(event, "type", "")),
            "delta": getattr(event, "delta", None),
            "num": getattr(event, "num", None),
            "handler_called": handler_called,
            "ignored_reason": ignored_reason,
            "yview_before": yview_before,
            "yview_after": yview_after,
            **self._pointer_widget_summary(event),
        }
        if delta_xy is not None:
            payload["delta_x"] = delta_xy[0]
            payload["delta_y"] = delta_xy[1]
        self.record(**payload)

    def record(self, **fields: object) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **fields,
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _install_win32_observers(self) -> None:
        import ctypes
        from ctypes import wintypes

        try:
            toplevel = self.view.winfo_toplevel()
            if not toplevel.winfo_exists():
                return
            toplevel.update_idletasks()
            inner_hwnd = int(toplevel.winfo_id())
            outer_hwnd = int(ctypes.windll.user32.GetParent(inner_hwnd))
        except (OSError, AttributeError, tk.TclError, ValueError):
            return

        user32 = ctypes.windll.user32
        GWLP_WNDPROC = -4
        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user32.CallWindowProcW.restype = LRESULT
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p

        hooked: set[int] = set()
        for hwnd in (outer_hwnd, inner_hwnd):
            if not hwnd or hwnd in hooked:
                continue
            hooked.add(hwnd)
            orig_ptr_holder: list[int] = []

            def _make_proc(holder: list[int]) -> object:
                def _wndproc(h: int, msg: int, wp: int, lp: int) -> int:
                    msg_name = _WIN32_SCROLL_MESSAGES.get(msg)
                    if msg_name is not None:
                        hi_word = (wp >> 16) & 0xFFFF
                        signed_delta = hi_word - 0x10000 if hi_word >= 0x8000 else hi_word
                        self.record(
                            stage="win32_wndproc",
                            event_type=msg_name,
                            hwnd=hex(h),
                            delta=signed_delta,
                            wparam_low=wp & 0xFFFF,
                            x_root=lp & 0xFFFF if (lp & 0xFFFF) < 0x8000 else (lp & 0xFFFF) - 0x10000,
                            y_root=((lp >> 16) & 0xFFFF)
                            if ((lp >> 16) & 0xFFFF) < 0x8000
                            else ((lp >> 16) & 0xFFFF) - 0x10000,
                        )
                    return int(user32.CallWindowProcW(holder[0], h, msg, wp, lp))

                return WNDPROC(_wndproc)

            callback = _make_proc(orig_ptr_holder)
            callback_ptr = ctypes.cast(callback, ctypes.c_void_p).value
            prev_ptr = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, callback_ptr)
            if prev_ptr:
                orig_ptr_holder.append(int(prev_ptr))
                self._win32_hooks.append((hwnd, int(prev_ptr), callback))

    def _uninstall_win32_hooks(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        GWLP_WNDPROC = -4
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        for hwnd, orig_ptr, _callback in self._win32_hooks:
            try:
                if user32.IsWindow(hwnd):
                    user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, orig_ptr)
            except (OSError, ctypes.ArgumentError):
                pass
        self._win32_hooks.clear()


def select_display_values(field: GenerationField) -> tuple[str, ...]:
    """Returnează alegerea goală urmată de etichetele configurate."""

    if field.data_type != "select":
        raise ValueError("Opțiunile de afișare sunt disponibile numai pentru câmpuri select.")
    return ("", *(option.option_label for option in field.options))


def select_value_for_index(field: GenerationField, index: int) -> str:
    """Leagă poziția aleasă de valoare, fără ambiguități între etichete duplicate."""

    if field.data_type != "select":
        raise ValueError("Maparea opțiunii este disponibilă numai pentru câmpuri select.")
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index trebuie să fie un întreg.")
    if index in (-1, 0):
        return ""
    if index < -1 or index > len(field.options):
        raise ValueError("index nu corespunde unei opțiuni afișate.")
    return field.options[index - 1].option_value


def collect_generation_values(
    form: GenerationForm,
    values_by_key: Mapping[str, str],
) -> dict[str, str]:
    """Construiește maparea completă, incluzând șiruri goale pentru câmpuri opționale."""

    if not isinstance(form, GenerationForm):
        raise TypeError("form trebuie să fie GenerationForm.")
    if not isinstance(values_by_key, Mapping):
        raise TypeError("values_by_key trebuie să fie o mapare.")
    values: dict[str, str] = {}
    for field in form.fields:
        value = values_by_key.get(field.variable_key, "")
        if not isinstance(value, str):
            raise TypeError(f"Valoarea pentru {field.variable_key!r} trebuie să fie text.")
        values[field.variable_key] = value
    return values


def field_usage_text(field: GenerationField) -> str:
    """Descrie documentele în care apare câmpul și unde este obligatoriu."""

    if not field.used_in:
        return "Câmp standard V3 (opțional)."
    used = ", ".join(DOCUMENT_LABELS[item] for item in field.used_in)
    required = ", ".join(DOCUMENT_LABELS[item] for item in field.required_in)
    if required:
        return f"Utilizat în: {used}. Obligatoriu în: {required}."
    return f"Utilizat în: {used}. Opțional."


def _choice_label(item: Mapping[str, object]) -> str:
    name = str(item["name"])
    code = item.get("code")
    cleaned_code = code.strip() if isinstance(code, str) else ""
    return f"{name} · {cleaned_code}" if cleaned_code else name


def _matches_record_query(item: Mapping[str, object], query: str) -> bool:
    """Potrivește case-insensitive și parțial după denumire sau cod."""

    cleaned = query.strip().casefold()
    if not cleaned:
        return True
    name = str(item.get("name") or "").casefold()
    raw_code = item.get("code")
    code = raw_code.strip().casefold() if isinstance(raw_code, str) else ""
    return cleaned in name or (bool(code) and cleaned in code)


def _update_code_hint(
    label: ttk.Label,
    variable: tk.StringVar,
    item: Mapping[str, object] | None,
) -> None:
    raw_code = item.get("code") if item is not None else None
    cleaned_code = raw_code.strip() if isinstance(raw_code, str) else ""
    if cleaned_code:
        variable.set(f"Cod: {cleaned_code}")
        label.grid()
    else:
        variable.set("")
        label.grid_remove()


@dataclass(frozen=True, slots=True)
class _WindowReplacementApproval:
    """Permisiune internă, legată de fereastra care a fost verificată."""

    owner: object
    previous_window: GeneratedDocumentsWindow | None


_ROMANIAN_LOCALE_CANDIDATES = (
    "ro_RO.UTF-8",
    "ro_RO",
    "ro-RO",
    "Romanian_Romania.1250",
    "Romanian",
)


def _resolve_romanian_locale() -> str | None:
    """Identifică un locale românesc suportat fără a altera locale-ul global."""

    saved = None
    try:
        saved = locale.setlocale(locale.LC_TIME)
    except (locale.Error, ValueError):
        saved = None

    try:
        for candidate in _ROMANIAN_LOCALE_CANDIDATES:
            try:
                locale.setlocale(locale.LC_TIME, candidate)
                return candidate
            except (locale.Error, ValueError):
                continue
    finally:
        if saved:
            try:
                locale.setlocale(locale.LC_TIME, saved)
            except (locale.Error, ValueError):
                pass
    return None


class _OrtoDocsDateEntry(DateEntry):
    """Wrapper subțire peste tk_date_entry.DateEntry adaptat ciclului de viață Tkinter."""

    def __init__(self, master: tk.Misc, **kwargs: object) -> None:
        self._opening_popup = False
        super().__init__(master, **kwargs)
        if getattr(self.variable, "_tk", None) is not self.tk:
            self.variable = tk.StringVar(master=self, value=self.entry.get())
            self.entry.configure(textvariable=self.variable)

    def drop_down(self) -> None:
        self._opening_popup = True
        try:
            super().drop_down()
        finally:
            self._opening_popup = False
        if self._calendar_window is not None:
            after_id = getattr(self, "_poll_after_id", None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
            self._poll_after_id = self.after(300, self._poll_app_focus)

    def _poll_app_focus(self) -> None:
        if getattr(self, "_opening_popup", False):
            return
        if self._calendar_window is not None and sys.platform == "win32":
            try:
                import ctypes

                user32 = ctypes.windll.user32
                fg = user32.GetForegroundWindow()
                popup_hwnd = user32.GetAncestor(self._calendar_window.winfo_id(), 2)
                if fg == popup_hwnd:
                    self._poll_after_id = self.after(300, self._poll_app_focus)
                    return
            except (AttributeError, tk.TclError):
                pass
        super()._poll_app_focus()

    def _on_app_move(self, event: tk.Event[tk.Misc]) -> None:
        if self._calendar_window is None:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        super()._on_app_move(event)

    def _on_app_destroy(self, event: tk.Event[tk.Misc]) -> None:
        if self._calendar_window is None:
            return
        super()._on_app_destroy(event)

    def destroy(self) -> None:
        self._close_popup()
        super().destroy()


def _read_field_value(
    field: GenerationField,
    details: Mapping[str, object],
) -> str:
    widget = details["widget"]
    if field.data_type == "select":
        assert isinstance(widget, ttk.Combobox)
        return select_value_for_index(field, widget.current())
    if isinstance(widget, DateEntry):
        parsed_date = widget.get_date()
        if parsed_date is not None:
            return parsed_date.strftime("%d.%m.%Y")
        return widget.variable.get().strip()
    variable = details["variable"]
    assert isinstance(variable, tk.StringVar)
    return variable.get()


def _apply_widget_validation_style(widget: object, *, invalid: bool) -> None:
    if isinstance(widget, ttk.Combobox):
        widget.configure(style="Invalid.TCombobox" if invalid else "TCombobox")
        return
    if isinstance(widget, DateEntry):
        widget.entry.configure(style="Invalid.TEntry" if invalid else "TEntry")
        return
    assert isinstance(widget, ttk.Entry)
    widget.configure(style="Invalid.TEntry" if invalid else "TEntry")


class GenerationView(ttk.Frame):
    """Formular Tkinter pentru generarea atomică a ambelor documente."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        connection: sqlite3.Connection,
    ) -> None:
        super().__init__(parent, style="App.TFrame")
        self.connection = connection
        self.diagnoses: list[dict[str, object]] = []
        self.interventions: list[dict[str, object]] = []
        self._visible_diagnoses: list[dict[str, object]] = []
        self._visible_interventions: list[dict[str, object]] = []
        self.selected_diagnosis_id: int | None = None
        self.selected_intervention_id: int | None = None
        self.generation_form: GenerationForm | None = None
        self.generated_session: GeneratedSession | None = None
        self.field_values: dict[str, str] = {}
        self.field_controls: dict[str, dict[str, object]] = {}
        self.validation_issues: dict[str, tuple[FieldValidationIssue, ...]] = {}
        self._generating = False
        self.documents_window: GeneratedDocumentsWindow | None = None
        self._canvas_host: tk.Frame | None = None
        self.form_canvas: tk.Canvas | None = None
        self.form_scrollbar: ttk.Scrollbar | None = None
        self.fields_container: ttk.Frame | None = None
        self._fields_window: int | None = None
        self._scroll_bindtag = f"OrtoDocsStep3Scroll_{id(self)}"
        self._wheel_delta_remainder = 0.0
        self._mousewheel_bindings_installed = False
        diag_path = _resolve_scroll_diag_path()
        self._scroll_diag: _Step3ScrollDiagnosticLogger | None = (
            _Step3ScrollDiagnosticLogger(self, diag_path) if diag_path is not None else None
        )

        self._configure_styles()
        self._build_content()
        if self._scroll_diag is not None:
            self._scroll_diag.install()
        self.bind("<Destroy>", self._on_view_destroy, add="+")
        self._load_diagnoses()

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        if not style.lookup("CardCode.TLabel", "font"):
            apply_soft_medical_theme(self.winfo_toplevel())

    def _build_content(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=0)
        self.rowconfigure(1, weight=0)

        # Etapa 1 și Etapa 2: selecția diagnosticului și a intervenției pe toată lățimea
        self.context_card = CardFrame(self, padding=(18, 14))
        self.context_card.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_MD))
        context_body = self.context_card.body
        context_body.columnconfigure(0, weight=1, uniform="generation-steps")
        context_body.columnconfigure(1, weight=1, uniform="generation-steps")

        diag_header = ttk.Frame(context_body, style="Card.TFrame")
        diag_header.grid(row=0, column=0, sticky="ew", padx=(0, 12), pady=(0, 6))
        ttk.Label(diag_header, text="Pas 1", style="StepTag.TLabel").pack(side="left", padx=(0, 8))
        ttk.Label(diag_header, text="Selectarea diagnosticului", style="CardTitle.TLabel").pack(side="left")

        int_header = ttk.Frame(context_body, style="Card.TFrame")
        int_header.grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        ttk.Label(int_header, text="Pas 2", style="StepTag.TLabel").pack(side="left", padx=(0, 8))
        ttk.Label(int_header, text="Selectarea intervenției", style="CardTitle.TLabel").pack(side="left")

        self.diagnosis_search_var = tk.StringVar(master=self, value="")
        self.diagnosis_search_entry = ttk.Entry(
            context_body,
            textvariable=self.diagnosis_search_var,
        )
        self.diagnosis_search_entry.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(0, 6))
        self.diagnosis_search_var.trace_add("write", self._on_diagnosis_search_changed)
        self.diagnosis_search_entry.bind("<KeyRelease>", self._on_diagnosis_search_changed)
        self.diagnosis_search_entry.bind("<Return>", self._on_diagnosis_search_submit)
        self.diagnosis_search_entry.bind("<KP_Enter>", self._on_diagnosis_search_submit)

        self.intervention_search_var = tk.StringVar(master=self, value="")
        self.intervention_search_entry = ttk.Entry(
            context_body,
            textvariable=self.intervention_search_var,
        )
        self.intervention_search_entry.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        self.intervention_search_var.trace_add("write", self._on_intervention_search_changed)
        self.intervention_search_entry.bind("<KeyRelease>", self._on_intervention_search_changed)
        self.intervention_search_entry.bind("<Return>", self._on_intervention_search_submit)
        self.intervention_search_entry.bind("<KP_Enter>", self._on_intervention_search_submit)

        ttk.Label(
            context_body,
            text="Filtrează după denumire sau cod și alege din listă:",
            style="CardMuted.TLabel",
        ).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=(0, 4))
        ttk.Label(
            context_body,
            text="Filtrează după denumire sau cod și alege intervenția:",
            style="CardMuted.TLabel",
        ).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(0, 4))

        self.diagnosis_var = tk.StringVar(master=self, value="")
        self.diagnosis_combo = ttk.Combobox(
            context_body,
            textvariable=self.diagnosis_var,
            state="readonly",
        )
        self.diagnosis_combo.grid(row=3, column=0, sticky="ew", padx=(0, 12))
        self.diagnosis_combo.bind("<<ComboboxSelected>>", self._on_diagnosis_selected)

        self.intervention_var = tk.StringVar(master=self, value="")
        self.intervention_combo = ttk.Combobox(
            context_body,
            textvariable=self.intervention_var,
            state="readonly",
        )
        self.intervention_combo.grid(row=3, column=1, sticky="ew", padx=(12, 0))
        self.intervention_combo.bind("<<ComboboxSelected>>", self._on_intervention_selected)

        self.diagnosis_code_var = tk.StringVar(master=self, value="")
        self.diagnosis_code_label = ttk.Label(
            context_body,
            textvariable=self.diagnosis_code_var,
            style="CardCode.TLabel",
        )
        self.diagnosis_code_label.grid(row=4, column=0, sticky="w", padx=(0, 12), pady=(4, 0))
        self.diagnosis_code_label.grid_remove()

        self.intervention_code_var = tk.StringVar(master=self, value="")
        self.intervention_code_label = ttk.Label(
            context_body,
            textvariable=self.intervention_code_var,
            style="CardCode.TLabel",
        )
        self.intervention_code_label.grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(4, 0))
        self.intervention_code_label.grid_remove()

        # Etapa 3: completarea datelor pe toată lățimea (compactă când lipsește selecția)
        self.form_card = CardFrame(self, padding=(18, 16))
        self.form_card.grid(row=1, column=0, sticky="ew")
        self.form_body = self.form_card.body
        self.form_body.columnconfigure(0, weight=1)
        self.form_body.rowconfigure(1, weight=0)

        form_header = ttk.Frame(self.form_body, style="Card.TFrame")
        form_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        form_header.columnconfigure(1, weight=1)
        ttk.Label(form_header, text="Pas 3", style="StepTag.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Label(
            form_header,
            text="Completarea datelor",
            style="CardTitle.TLabel",
        ).grid(row=0, column=1, sticky="w")
        self.form_hint_label = ttk.Label(
            form_header,
            text="Câmpurile opționale pot rămâne necompletate",
            style="CardMuted.TLabel",
        )
        self.form_hint_label.grid(row=0, column=2, sticky="e")
        self.form_hint_label.grid_remove()

        self.prompt_frame = ttk.Frame(
            self.form_body,
            style="Subtle.TFrame",
            padding=(20, 22),
        )
        self.prompt_frame.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        self.prompt_frame.columnconfigure(0, weight=1)
        self.prompt_label = ttk.Label(
            self.prompt_frame,
            text="Selectați un diagnostic și o intervenție.",
            style="Subtle.TLabel",
            anchor="center",
            justify="center",
        )
        self.prompt_label.grid(row=0, column=0, sticky="ew")

        footer_actions = ttk.Frame(self.form_body, style="Card.TFrame")
        footer_actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        footer_actions.columnconfigure(0, weight=1)

        footer_left = ttk.Frame(footer_actions, style="Card.TFrame")
        footer_left.grid(row=0, column=0, sticky="ew", padx=(0, 14))
        footer_left.columnconfigure(0, weight=1)

        self.required_hint_label = ttk.Label(
            footer_left,
            text="* Câmp obligatoriu conform șabloanelor active",
            style="CardMuted.TLabel",
        )
        self.required_hint_label.grid(row=0, column=0, sticky="w")
        self.required_hint_label.grid_remove()

        self.status_var = tk.StringVar(master=self, value="")
        self.status_label = ttk.Label(
            footer_left,
            textvariable=self.status_var,
            style="GenerationMuted.TLabel",
            justify="left",
            wraplength=760,
        )
        self.status_label.grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.generate_button = ttk.Button(
            footer_actions,
            text="Generează documentele",
            style="Primary.TButton",
            command=self._on_generate,
            state="disabled",
        )
        self.generate_button.grid(row=0, column=1, sticky="e")

        self._show_form_prompt("Selectați un diagnostic și o intervenție.")

    def _ensure_form_area(self) -> ttk.Frame:
        if (
            self._canvas_host is not None
            and self.form_canvas is not None
            and self.fields_container is not None
        ):
            return self.fields_container

        canvas_host = tk.Frame(
            self.form_body,
            background=BG_SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
            borderwidth=0,
        )
        canvas_host.grid(row=1, column=0, sticky="nsew")
        canvas_host.columnconfigure(0, weight=1)
        canvas_host.rowconfigure(0, weight=1)

        form_canvas = tk.Canvas(
            canvas_host,
            background=BG_SURFACE,
            borderwidth=0,
            highlightthickness=0,
        )
        form_canvas.grid(row=0, column=0, sticky="nsew")
        form_scrollbar = ttk.Scrollbar(
            canvas_host,
            orient="vertical",
            command=form_canvas.yview,
        )
        form_scrollbar.grid(row=0, column=1, sticky="ns")
        form_canvas.configure(yscrollcommand=form_scrollbar.set)
        fields_container = ttk.Frame(
            form_canvas,
            style="Card.TFrame",
            padding=(16, 14),
        )
        fields_container.columnconfigure(0, weight=1)
        fields_window = form_canvas.create_window(
            (0, 0), window=fields_container, anchor="nw"
        )
        fields_container.bind("<Configure>", self._update_form_scroll_region)
        form_canvas.bind("<Configure>", self._resize_fields_container)

        self._canvas_host = canvas_host
        self.form_canvas = form_canvas
        self.form_scrollbar = form_scrollbar
        self.fields_container = fields_container
        self._fields_window = fields_window
        self._install_step3_mousewheel_bindings()
        self._attach_scroll_bindtags(canvas_host)
        canvas_host.bind("<Leave>", self._on_form_area_leave, add="+")
        return fields_container

    def _destroy_form_area(self) -> None:
        self._unbind_mousewheel()
        if self._canvas_host is not None:
            try:
                self._canvas_host.destroy()
            except tk.TclError:
                pass
        self._canvas_host = None
        self.form_canvas = None
        self.form_scrollbar = None
        self.fields_container = None
        self._fields_window = None
        self._wheel_delta_remainder = 0.0

    def _on_view_destroy(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is self:
            self._unbind_mousewheel()
            if self._scroll_diag is not None:
                self._scroll_diag.uninstall()

    def _install_step3_mousewheel_bindings(self) -> None:
        if self._mousewheel_bindings_installed:
            return
        self.bind_class(self._scroll_bindtag, "<MouseWheel>", self._on_form_mousewheel)
        try:
            self.bind_class(
                self._scroll_bindtag,
                "<TouchpadScroll>",
                self._on_form_touchpad_scroll,
            )
        except tk.TclError:
            pass
        self.bind_class(self._scroll_bindtag, "<Button-4>", self._on_form_mousewheel_linux_up)
        self.bind_class(self._scroll_bindtag, "<Button-5>", self._on_form_mousewheel_linux_down)
        self._mousewheel_bindings_installed = True

    def _attach_scroll_bindtags(self, root_widget: tk.Misc) -> None:
        stack: list[tk.Misc] = [root_widget]
        while stack:
            widget = stack.pop()
            if widget is self.form_scrollbar:
                continue
            try:
                tags = widget.bindtags()
                if self._scroll_bindtag not in tags:
                    insert_at = 1 if tags else 0
                    new_tags = tags[:insert_at] + (self._scroll_bindtag,) + tags[insert_at:]
                    widget.bindtags(new_tags)
                stack.extend(widget.winfo_children())
            except tk.TclError:
                continue

    def _on_form_area_leave(self, event: tk.Event[tk.Misc]) -> None:
        coords = _event_pointer_coords(event)
        if coords is not None and not self._is_point_in_canvas_host(coords[0], coords[1]):
            self._wheel_delta_remainder = 0.0

    def _is_point_in_canvas_host(self, x_root: int, y_root: int) -> bool:
        if self._canvas_host is None or not self._canvas_host.winfo_exists():
            return False
        if not self._canvas_host.winfo_ismapped():
            return False
        left = self._canvas_host.winfo_rootx()
        top = self._canvas_host.winfo_rooty()
        width = self._canvas_host.winfo_width()
        height = self._canvas_host.winfo_height()
        return left <= x_root < left + width and top <= y_root < top + height

    @staticmethod
    def _is_combobox_popdown_mapped(combo: ttk.Combobox) -> bool:
        try:
            if not combo.winfo_exists():
                return False
            popdown = str(combo.tk.call("ttk::combobox::PopdownWindow", combo))
            return bool(int(combo.tk.call("winfo", "ismapped", popdown)))
        except tk.TclError:
            return False

    def _has_open_combobox_popdown(self) -> bool:
        candidates: list[ttk.Combobox] = [self.diagnosis_combo, self.intervention_combo]
        for details in self.field_controls.values():
            widget = details.get("widget")
            if isinstance(widget, ttk.Combobox):
                candidates.append(widget)
        return any(self._is_combobox_popdown_mapped(combo) for combo in candidates)

    def _is_calendar_popup_blocking_scroll(self, event: tk.Event[tk.Misc]) -> bool:
        coords = _event_pointer_coords(event)
        for details in self.field_controls.values():
            widget = details.get("widget")
            if not isinstance(widget, DateEntry):
                continue
            calendar_window = getattr(widget, "_calendar_window", None)
            if calendar_window is None:
                continue
            try:
                if not calendar_window.winfo_exists() or not calendar_window.winfo_ismapped():
                    continue
                if coords is not None:
                    left = calendar_window.winfo_rootx()
                    top = calendar_window.winfo_rooty()
                    right = left + calendar_window.winfo_width()
                    bottom = top + calendar_window.winfo_height()
                    if left <= coords[0] < right and top <= coords[1] < bottom:
                        return True
            except tk.TclError:
                continue
        return False

    def _is_form_scrollable(self) -> bool:
        if self.form_canvas is None or not self.form_canvas.winfo_exists():
            return False
        bbox = self.form_canvas.bbox("all")
        if bbox is None:
            return False
        content_height = max(0, int(bbox[3] - bbox[1]))
        canvas_height = int(self.form_canvas.winfo_height())
        if canvas_height > 1 and content_height <= canvas_height:
            return False
        first, last = self.form_canvas.yview()
        return not (first <= 0.0 and last >= 1.0)

    def _scroll_form_by_delta(self, delta: float) -> str | None:
        if self.form_canvas is None or not self.form_canvas.winfo_exists():
            return None
        if not self._is_form_scrollable():
            self._wheel_delta_remainder = 0.0
            return None

        bbox = self.form_canvas.bbox("all")
        if bbox is None:
            return None
        content_height = max(1, int(bbox[3] - bbox[1]))
        first, last = self.form_canvas.yview()
        span = last - first
        max_first = max(0.0, 1.0 - span)
        if max_first <= 0.0:
            return None

        # 48 px per notch (120 delta) menține aceeași sensibilitate pentru mouse wheel și touchpad.
        delta_px = (-delta / 120.0) * 48.0
        if delta_px == 0.0:
            return None

        if self._wheel_delta_remainder != 0.0 and (
            (delta_px > 0.0 and self._wheel_delta_remainder < 0.0)
            or (delta_px < 0.0 and self._wheel_delta_remainder > 0.0)
        ):
            self._wheel_delta_remainder = 0.0

        step_px = delta_px + self._wheel_delta_remainder
        if abs(step_px) < 1.0:
            self._wheel_delta_remainder = step_px
            return "break"

        whole_px = int(step_px)
        self._wheel_delta_remainder = step_px - whole_px
        if abs(self._wheel_delta_remainder) < 1e-9:
            self._wheel_delta_remainder = 0.0

        new_first = min(max(0.0, first + (whole_px / content_height)), max_first)
        if (new_first <= 0.0 and whole_px < 0) or (new_first >= max_first and whole_px > 0):
            self._wheel_delta_remainder = 0.0
        self.form_canvas.yview_moveto(new_first)
        return "break"

    def _step3_scroll_ignore_reason(self, event: tk.Event[tk.Misc]) -> str | None:
        if self.form_canvas is None or not self.form_canvas.winfo_exists():
            return "no_form_canvas"
        coords = _event_pointer_coords(event)
        if (
            coords is not None
            and self._canvas_host is not None
            and self._canvas_host.winfo_ismapped()
            and not self._is_point_in_canvas_host(coords[0], coords[1])
        ):
            return "outside_canvas_host"
        if self._has_open_combobox_popdown():
            return "combobox_popdown_open"
        if self._is_calendar_popup_blocking_scroll(event):
            return "calendar_popup_blocking"
        return None

    def _dispatch_step3_scroll_event(
        self,
        event: tk.Event[tk.Misc],
        *,
        event_name: str,
        delta: float | None,
        zero_reason: str,
        delta_xy: tuple[int, int] | None = None,
    ) -> str | None:
        yview_before = self._scroll_diag._canvas_yview() if self._scroll_diag is not None else None
        ignored_reason = self._step3_scroll_ignore_reason(event)
        result: str | None = None
        if ignored_reason is None:
            if delta is None or delta == 0.0:
                ignored_reason = zero_reason
            else:
                result = self._scroll_form_by_delta(delta)
                if result is None:
                    ignored_reason = "form_not_scrollable"
        if self._scroll_diag is not None:
            self._scroll_diag.record_tk_event(
                stage="step3_handler",
                event_name=event_name,
                event=event,
                handler_called=True,
                ignored_reason=ignored_reason,
                yview_before=yview_before,
                yview_after=self._scroll_diag._canvas_yview(),
                delta_xy=delta_xy,
            )
        return result

    def _on_form_mousewheel(self, event: tk.Event[tk.Misc]) -> str | None:
        delta = float(getattr(event, "delta", 0) or 0)
        return self._dispatch_step3_scroll_event(
            event,
            event_name="<MouseWheel>",
            delta=delta,
            zero_reason="zero_delta",
        )

    def _on_form_touchpad_scroll(self, event: tk.Event[tk.Misc]) -> str | None:
        raw_delta = getattr(event, "delta", 0) or 0
        delta_x, delta_y = unpack_touchpad_scroll_deltas(raw_delta)
        zero_reason = "horizontal_only_delta_y_zero" if delta_x != 0 else "zero_delta"
        return self._dispatch_step3_scroll_event(
            event,
            event_name="<TouchpadScroll>",
            delta=float(delta_y),
            zero_reason=zero_reason,
            delta_xy=(delta_x, delta_y),
        )

    def _on_form_mousewheel_linux_up(self, event: tk.Event[tk.Misc]) -> str | None:
        return self._dispatch_step3_scroll_event(
            event,
            event_name="<Button-4>",
            delta=120.0,
            zero_reason="zero_delta",
        )

    def _on_form_mousewheel_linux_down(self, event: tk.Event[tk.Misc]) -> str | None:
        return self._dispatch_step3_scroll_event(
            event,
            event_name="<Button-5>",
            delta=-120.0,
            zero_reason="zero_delta",
        )

    def _unbind_mousewheel(self) -> None:
        if not self._mousewheel_bindings_installed:
            return
        for sequence in ("<MouseWheel>", "<TouchpadScroll>", "<Button-4>", "<Button-5>"):
            try:
                self.unbind_class(self._scroll_bindtag, sequence)
            except tk.TclError:
                pass
        self._mousewheel_bindings_installed = False

    def _load_diagnoses(self) -> None:
        try:
            self.diagnoses = medical_library.list_diagnoses(
                self.connection,
                active_only=True,
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._set_status(f"Diagnosticele active nu au putut fi încărcate: {exc}", error=True)
            return
        self._apply_diagnosis_filter()
        self.diagnosis_combo.set("")
        if not self.diagnoses:
            self._set_status("Nu există diagnostice active disponibile.", error=False)

    def _apply_diagnosis_filter(self) -> None:
        query = (
            self.diagnosis_search_var.get()
            if hasattr(self, "diagnosis_search_var")
            else ""
        )
        self._visible_diagnoses = [
            item
            for item in self.diagnoses
            if _matches_record_query(item, query)
        ]
        self.diagnosis_combo.configure(
            values=tuple(_choice_label(item) for item in self._visible_diagnoses)
        )
        if self.selected_diagnosis_id is not None:
            match_index = next(
                (
                    idx
                    for idx, item in enumerate(self._visible_diagnoses)
                    if int(item["id"]) == self.selected_diagnosis_id
                ),
                None,
            )
            if match_index is not None:
                self.diagnosis_combo.current(match_index)
            elif self.diagnosis_var.get():
                self.diagnosis_combo.set("")

    def _apply_intervention_filter(self) -> None:
        query = (
            self.intervention_search_var.get()
            if hasattr(self, "intervention_search_var")
            else ""
        )
        self._visible_interventions = [
            item
            for item in self.interventions
            if _matches_record_query(item, query)
        ]
        self.intervention_combo.configure(
            values=tuple(_choice_label(item) for item in self._visible_interventions)
        )
        if self.selected_intervention_id is not None:
            match_index = next(
                (
                    idx
                    for idx, item in enumerate(self._visible_interventions)
                    if int(item["id"]) == self.selected_intervention_id
                ),
                None,
            )
            if match_index is not None:
                self.intervention_combo.current(match_index)
            elif self.intervention_var.get():
                self.intervention_combo.set("")

    @staticmethod
    def _post_combobox(combo: ttk.Combobox) -> None:
        combo.focus_set()
        try:
            combo.tk.call("ttk::combobox::Post", combo)
        except tk.TclError:
            pass

    def _on_diagnosis_search_changed(self, *_args: object) -> None:
        if hasattr(self, "diagnoses"):
            self._apply_diagnosis_filter()

    def _on_intervention_search_changed(self, *_args: object) -> None:
        if hasattr(self, "interventions"):
            self._apply_intervention_filter()

    def _on_diagnosis_search_submit(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        self._apply_diagnosis_filter()
        if len(self._visible_diagnoses) == 1:
            self.diagnosis_combo.current(0)
            self._on_diagnosis_selected()
        elif len(self._visible_diagnoses) > 1:
            self._post_combobox(self.diagnosis_combo)
        else:
            self._set_status("Niciun diagnostic activ nu corespunde căutării.")
        return "break"

    def _on_intervention_search_submit(self, _event: tk.Event[tk.Misc] | None = None) -> str:
        self._apply_intervention_filter()
        if len(self._visible_interventions) == 1:
            self.intervention_combo.current(0)
            self._on_intervention_selected()
        elif len(self._visible_interventions) > 1:
            self._post_combobox(self.intervention_combo)
        elif self.selected_diagnosis_id is not None:
            self._set_status("Nicio intervenție activă nu corespunde căutării.")
        return "break"

    def on_show(self) -> None:
        """Reîncarcă selecțiile și elimină formularul contextual devenit vechi."""

        self.refresh()

    def refresh(self) -> None:
        """Resetează contextul de generare fără să închidă fereastra documentelor."""

        self._reset_for_context_change(clear_diagnosis=True)
        self.diagnoses = []
        self.interventions = []
        self._visible_diagnoses = []
        self._visible_interventions = []
        self.diagnosis_search_var.set("")
        self.intervention_search_var.set("")
        self.diagnosis_combo.configure(values=())
        self.diagnosis_combo.set("")
        self.intervention_combo.configure(values=())
        self.intervention_combo.set("")
        _update_code_hint(self.diagnosis_code_label, self.diagnosis_code_var, None)
        _update_code_hint(self.intervention_code_label, self.intervention_code_var, None)
        self._show_form_prompt("Selectați un diagnostic și o intervenție.")
        self._load_diagnoses()

    def _on_diagnosis_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        index = self.diagnosis_combo.current()
        self._reset_for_context_change(clear_diagnosis=False)
        self.selected_diagnosis_id = None
        self.interventions = []
        self._visible_interventions = []
        self.intervention_search_var.set("")
        self.intervention_combo.configure(values=())
        self.intervention_var.set("")
        _update_code_hint(self.diagnosis_code_label, self.diagnosis_code_var, None)
        _update_code_hint(self.intervention_code_label, self.intervention_code_var, None)
        if index < 0 or index >= len(self._visible_diagnoses):
            self._show_form_prompt("Selectați un diagnostic și o intervenție.")
            return

        selected_diagnosis = self._visible_diagnoses[index]
        self.selected_diagnosis_id = int(selected_diagnosis["id"])
        _update_code_hint(
            self.diagnosis_code_label, self.diagnosis_code_var, selected_diagnosis
        )
        try:
            self.interventions = medical_library.list_interventions(
                self.connection,
                diagnosis_id=self.selected_diagnosis_id,
                active_only=True,
            )
        except (medical_library.MedicalLibraryError, sqlite3.Error) as exc:
            self._show_form_prompt("Selectați un diagnostic și o intervenție.")
            self._set_status(f"Intervențiile active nu au putut fi încărcate: {exc}", error=True)
            return
        self._apply_intervention_filter()
        self.intervention_combo.set("")
        if self.interventions:
            self._show_form_prompt("Selectați un diagnostic și o intervenție.")
        else:
            self._show_form_prompt("Nu există intervenții active pentru diagnosticul selectat.")

    def _on_intervention_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        index = self.intervention_combo.current()
        self._reset_for_context_change(clear_diagnosis=False)
        _update_code_hint(self.intervention_code_label, self.intervention_code_var, None)
        if index < 0 or index >= len(self._visible_interventions):
            self._show_form_prompt("Selectați un diagnostic și o intervenție.")
            return
        selected_intervention = self._visible_interventions[index]
        self.selected_intervention_id = int(selected_intervention["id"])
        _update_code_hint(
            self.intervention_code_label,
            self.intervention_code_var,
            selected_intervention,
        )
        try:
            form = document_generation.get_generation_form(
                self.connection,
                self.selected_intervention_id,
            )
        except GenerationContextError as exc:
            self._show_form_prompt("Contextul selectat nu permite generarea documentelor.")
            self._set_status(str(exc), error=True)
            return
        except GenerationTemplateError as exc:
            self._show_form_prompt("Perechea de șabloane nu poate fi utilizată.")
            self._set_status(str(exc), error=True)
            return
        except GenerationConflictError as exc:
            self._show_form_prompt("Contextul s-a schimbat. Reîncărcați selecția.")
            self._set_status(str(exc), error=True)
            return
        except GenerationConfigurationError as exc:
            self._show_form_prompt("Configurația bazei de date nu permite generarea.")
            self._set_status(str(exc), error=True)
            return
        except GenerationPersistenceError as exc:
            self._show_form_prompt("Formularul nu a putut fi citit din stocarea locală.")
            self._set_status(str(exc), error=True)
            return
        except sqlite3.Error as exc:
            self._show_form_prompt("Formularul nu a putut fi încărcat din baza locală.")
            self._set_status(str(exc), error=True)
            return

        self.generation_form = form
        self._render_form(form)
        self._set_status("Completați câmpurile, apoi generați ambele documente.")

    def _render_form(self, form: GenerationForm) -> None:
        self._clear_form_widgets()
        self.generation_form = form
        self.prompt_frame.grid_remove()
        fields_container = self._ensure_form_area()
        self.rowconfigure(1, weight=1)
        self.form_card.grid_configure(sticky="nsew")
        self.form_body.rowconfigure(1, weight=1)
        self.form_hint_label.grid()
        self.required_hint_label.grid()

        if not form.fields:
            ttk.Label(
                fields_container,
                text="Formularul nu conține câmpuri de completat.",
                style="GenerationMuted.TLabel",
                wraplength=520,
            ).grid(row=0, column=0, sticky="w")
        else:
            standard_fields = [
                field for field in form.fields if field.variable_key in STANDARD_FIELD_KEYS
            ]
            dynamic_fields = [
                field for field in form.fields if field.variable_key not in STANDARD_FIELD_KEYS
            ]
            row_index = 0
            if standard_fields:
                ttk.Label(
                    fields_container,
                    text="Date generale",
                    style="CardTitle.TLabel",
                ).grid(row=row_index, column=0, sticky="w", pady=(0, 8))
                row_index += 1
                for field in standard_fields:
                    self._add_field_control(row_index, field)
                    row_index += 1
            if dynamic_fields:
                if standard_fields:
                    ttk.Frame(
                        fields_container,
                        height=1,
                        style="Hairline.TFrame",
                    ).grid(row=row_index, column=0, sticky="ew", pady=(6, 12))
                    row_index += 1
                ttk.Label(
                    fields_container,
                    text="Câmpuri suplimentare din șabloane",
                    style="CardTitle.TLabel",
                ).grid(row=row_index, column=0, sticky="w", pady=(0, 8))
                row_index += 1
                for field in dynamic_fields:
                    self._add_field_control(row_index, field)
                    row_index += 1

        self.field_values = {field.variable_key: "" for field in form.fields}
        self._clear_result()
        self._attach_scroll_bindtags(fields_container)
        fields_container.update_idletasks()
        if self.form_canvas is not None and self.form_canvas.winfo_exists():
            self.form_canvas.yview_moveto(0.0)
        self._update_form_scroll_region()
        self._update_generate_state()

    def _add_field_control(self, row_index: int, field: GenerationField) -> None:
        row = ttk.Frame(self.fields_container, style="Card.TFrame", padding=(0, 0, 0, 14))
        row.grid(row=row_index, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)

        title_row = ttk.Frame(row, style="Card.TFrame")
        title_row.grid(row=0, column=0, sticky="ew")
        title_row.columnconfigure(0, weight=1)
        required_marker = " *" if field.required_in else ""
        ttk.Label(
            title_row,
            text=f"{field.label}{required_marker}",
            style="GenerationRequired.TLabel" if field.required_in else "CardFieldLabel.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            title_row,
            text="Obligatoriu" if field.required_in else "Opțional",
            style="GenerationMuted.TLabel",
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        value_var = tk.StringVar(master=self, value="")
        control: ttk.Entry | ttk.Combobox | DateEntry
        calendar_button: ttk.Button | None = None
        if field.data_type == "select":
            control = ttk.Combobox(
                row,
                textvariable=value_var,
                values=select_display_values(field),
                state="readonly",
            )
            control.set("")
            control.grid(row=1, column=0, sticky="ew", pady=(5, 3))
        elif field.data_type == "date":
            date_entry = _OrtoDocsDateEntry(
                row,
                date_pattern="%d.%m.%Y",
                first_weekday=0,
                show_week_numbers=False,
                locale=_resolve_romanian_locale(),
                validate=False,
            )
            date_entry.configure(style="Card.TFrame")
            date_entry.button.configure(style="Secondary.TButton")
            date_entry.clear()
            date_entry.grid(row=1, column=0, sticky="ew", pady=(5, 3))
            date_entry.bind(
                "<<DateEntrySelected>>",
                lambda _event, key=field.variable_key: self._on_field_changed(key),
            )
            value_var = date_entry.variable
            control = date_entry
            calendar_button = date_entry.button
        else:
            control = ttk.Entry(row, textvariable=value_var)
            control.grid(row=1, column=0, sticky="ew", pady=(5, 3))
        if field.data_type == "date":
            ttk.Label(
                row,
                text="Format: DD.MM.YYYY",
                style="GenerationMuted.TLabel",
            ).grid(row=2, column=0, sticky="w", pady=(0, 3))
            usage_row = 3
        else:
            usage_row = 2
        usage_label = ttk.Label(
            row,
            text=field_usage_text(field),
            style="GenerationMuted.TLabel",
            wraplength=560,
            justify="left",
        )
        usage_label.grid(row=usage_row, column=0, sticky="w")
        error_var = tk.StringVar(master=self, value="")
        error_label = ttk.Label(
            row,
            textvariable=error_var,
            style="GenerationError.TLabel",
            wraplength=560,
            justify="left",
        )
        error_label.grid(row=usage_row + 1, column=0, sticky="w")
        error_label.grid_remove()

        details: dict[str, object] = {
            "field": field,
            "variable": value_var,
            "widget": control,
            "usage_label": usage_label,
            "error_variable": error_var,
            "error_label": error_label,
            "row": row,
        }
        if isinstance(control, DateEntry):
            details["date_entry"] = control
        if calendar_button is not None:
            details["calendar_button"] = calendar_button
        self.field_controls[field.variable_key] = details
        value_var.trace_add(
            "write",
            lambda *_args, key=field.variable_key: self._on_field_changed(key),
        )

    def _on_field_changed(self, variable_key: str) -> None:
        current_values: dict[str, str] = {}
        for key, details in self.field_controls.items():
            field = details["field"]
            assert isinstance(field, GenerationField)
            current_values[key] = _read_field_value(field, details)
        self.field_values = current_values
        self._clear_field_issue(variable_key)
        self._clear_result()
        if not self.validation_issues:
            self._set_status("Modificările formularului sunt nesalvate; generați documentele când sunt gata.")

    def _on_generate(self) -> None:
        if self._generating:
            return
        if self.generation_form is None or self.selected_intervention_id is None:
            self._set_status("Selectați un diagnostic și o intervenție înainte de generare.", error=True)
            return

        raw_values: dict[str, str] = {}
        for key, details in self.field_controls.items():
            field = details["field"]
            if not isinstance(field, GenerationField):
                continue
            raw_values[key] = _read_field_value(field, details)
        values = collect_generation_values(self.generation_form, raw_values)
        self.field_values = dict(values)

        self._generating = True
        self.generate_button.configure(state="disabled")
        self._clear_validation_issues()
        self._set_status("Se pregătesc documentele...")
        self.update_idletasks()
        try:
            prepared = document_generation.prepare_documents(
                self.connection,
                self.selected_intervention_id,
                values,
            )
            replacement_approval = self._request_window_replacement(
                replacement_description="Generarea nouă"
            )
            if replacement_approval is None:
                return

            self._set_status("Se salvează sesiunea pregătită...")
            result = document_generation.persist_prepared_documents(
                self.connection,
                prepared,
            )
        except GenerationValidationError as exc:
            self._display_validation_issues(exc.issues)
        except GenerationContextError as exc:
            self._set_status(f"Diagnosticul sau intervenția nu mai permite generarea: {exc}", error=True)
        except GenerationTemplateError as exc:
            self._set_status(f"Perechea de șabloane nu mai poate fi utilizată: {exc}", error=True)
        except GenerationConflictError as exc:
            self._set_status(f"Contextul s-a schimbat înainte de salvare: {exc}", error=True)
        except GenerationConfigurationError as exc:
            self._set_status(f"Configurația SQLite nu permite salvarea: {exc}", error=True)
        except GenerationPersistenceError as exc:
            self._set_status(f"Documentele nu au putut fi salvate în istoric: {exc}", error=True)
        except sqlite3.Error as exc:
            self._set_status(f"Baza de date locală nu a putut salva sesiunea: {exc}", error=True)
        else:
            try:
                opened = self._open_documents_session_after_approval(
                    result,
                    replacement_approval,
                )
            except Exception as exc:
                self.generated_session = result
                self._set_status(
                    f"Sesiunea {result.session_id} a fost salvată, dar fereastra documentelor nu a putut fi deschisă: {exc}",
                    error=True,
                )
                return

            if not opened:
                return
            self.generated_session = result
            self._set_status(
                f"Generarea s-a încheiat cu succes. Documentele (sesiunea {result.session_id}) au fost salvate local în istoric.",
                success=True,
            )
        finally:
            self._generating = False
            self._update_generate_state()

    def open_documents_session(
        self,
        session: GeneratedSession,
    ) -> bool:
        """Deschide sesiunea în fereastra comună, înlocuind-o numai după succes."""

        if not isinstance(session, GeneratedSession):
            raise TypeError("session trebuie să fie GeneratedSession.")

        old_window = self.documents_window
        if (
            old_window is not None
            and getattr(old_window, "_closed", False) is not True
            and old_window.session_id == session.session_id
        ):
            old_window.show()
            return True

        replacement_approval = self._request_window_replacement(
            replacement_description="Deschiderea sesiunii solicitate"
        )
        if replacement_approval is None:
            return False

        return self._open_documents_session_after_approval(session, replacement_approval)

    def _open_documents_session_after_approval(
        self,
        session: GeneratedSession,
        approval: _WindowReplacementApproval,
    ) -> bool:
        """Înlocuiește fereastra numai cu aprobarea internă pentru starea curentă."""

        if not isinstance(approval, _WindowReplacementApproval) or approval.owner is not self:
            raise TypeError("approval trebuie să provină din verificarea internă a ferestrei.")
        if self.documents_window is not approval.previous_window:
            raise RuntimeError("Fereastra documentelor s-a schimbat după confirmare.")

        new_window = GeneratedDocumentsWindow(
            self.winfo_toplevel(),
            self.connection,
            session,
            on_closed=self._on_documents_window_closed,
            initially_hidden=True,
        )
        if approval.previous_window is not None:
            approval.previous_window.close_after_replacement()
        self.documents_window = new_window
        new_window.show()
        return True

    def _request_window_replacement(
        self,
        *,
        replacement_description: str,
    ) -> _WindowReplacementApproval | None:
        """Cere rezolvarea modificărilor și emite o aprobare internă de unică utilizare."""

        old_window = self.documents_window
        if old_window is None or not old_window.has_dirty_documents:
            return _WindowReplacementApproval(self, old_window)

        replacement_action = old_window.ask_replacement_action()
        if replacement_action == "cancel":
            self._set_status(f"{replacement_description} a fost anulată.")
            return None
        if replacement_action == "save" and not old_window.save_dirty_documents():
            self._set_status(
                f"{replacement_description} a fost oprită deoarece documentele vechi nu au putut fi salvate.",
                error=True,
            )
            return None
        if replacement_action not in {"save", "discard"}:
            self._set_status(
                f"{replacement_description} a fost oprită deoarece alegerea de înlocuire nu este validă.",
                error=True,
            )
            return None
        return _WindowReplacementApproval(self, old_window)

    def resolve_application_close(self) -> bool:
        """Rezolvă documentele nesalvate înainte ca aplicația să închidă SQLite."""

        if self.documents_window is None:
            return True
        return self.documents_window.close()

    def _on_documents_window_closed(
        self,
        closed_window: GeneratedDocumentsWindow,
    ) -> None:
        if self.documents_window is closed_window:
            self.documents_window = None

    def _display_validation_issues(
        self,
        issues: tuple[FieldValidationIssue, ...],
    ) -> None:
        grouped: dict[str, list[FieldValidationIssue]] = {}
        unassigned: list[str] = []
        for issue in issues:
            grouped.setdefault(issue.variable_key, []).append(issue)
        for variable_key, field_issues in grouped.items():
            details = self.field_controls.get(variable_key)
            if details is None:
                for issue in field_issues:
                    documents = ", ".join(
                        DOCUMENT_LABELS.get(item, item) for item in issue.document_types
                    )
                    unassigned.append(f"{issue.message} ({documents})")
                continue
            error_variable = details["error_variable"]
            error_label = details["error_label"]
            widget = details["widget"]
            assert isinstance(error_variable, tk.StringVar)
            assert isinstance(error_label, ttk.Label)
            messages: list[str] = []
            for issue in field_issues:
                documents = ", ".join(
                    DOCUMENT_LABELS.get(item, item) for item in issue.document_types
                )
                messages.append(f"{issue.message} Document: {documents}.")
            error_variable.set("\n".join(messages))
            error_label.grid()
            _apply_widget_validation_style(widget, invalid=True)
            self.validation_issues[variable_key] = tuple(field_issues)

        summary = "Corectați câmpurile evidențiate. Niciun document nu a fost generat."
        if unassigned:
            summary += "\n" + "\n".join(unassigned)
        self._set_status(summary, error=True)

    def _clear_field_issue(self, variable_key: str) -> None:
        details = self.field_controls.get(variable_key)
        if details is None:
            return
        error_variable = details["error_variable"]
        error_label = details["error_label"]
        widget = details["widget"]
        assert isinstance(error_variable, tk.StringVar)
        assert isinstance(error_label, ttk.Label)
        error_variable.set("")
        error_label.grid_remove()
        _apply_widget_validation_style(widget, invalid=False)
        self.validation_issues.pop(variable_key, None)

    def _clear_validation_issues(self) -> None:
        for key in tuple(self.validation_issues):
            self._clear_field_issue(key)
        self.validation_issues.clear()

    def _reset_for_context_change(self, *, clear_diagnosis: bool) -> None:
        self.selected_intervention_id = None
        self.generation_form = None
        self.field_values = {}
        self._clear_validation_issues()
        self._clear_result()
        self._clear_form_widgets()
        self._set_status("")
        self._update_generate_state()
        if clear_diagnosis:
            self.selected_diagnosis_id = None

    def _clear_form_widgets(self) -> None:
        self.field_controls.clear()
        self._wheel_delta_remainder = 0.0
        if self.fields_container is not None:
            for child in self.fields_container.winfo_children():
                child.destroy()

    def _show_form_prompt(self, message: str) -> None:
        self._clear_form_widgets()
        self._destroy_form_area()
        self.rowconfigure(1, weight=0)
        self.form_card.grid_configure(sticky="ew")
        self.form_body.rowconfigure(1, weight=0)
        self.form_hint_label.grid_remove()
        self.required_hint_label.grid_remove()
        self.prompt_label.configure(text=message)
        self.prompt_frame.grid()
        self._update_generate_state()

    def _clear_result(self) -> None:
        self.generated_session = None

    def _update_generate_state(self) -> None:
        if not hasattr(self, "generate_button"):
            return
        state = "normal" if self.generation_form is not None and not self._generating else "disabled"
        self.generate_button.configure(state=state)

    def _set_status(
        self,
        message: str,
        *,
        error: bool = False,
        success: bool = False,
    ) -> None:
        self.status_var.set(message)
        if error:
            style = "GenerationError.TLabel"
        elif success:
            style = "GenerationSuccess.TLabel"
        else:
            style = "GenerationMuted.TLabel"
        self.status_label.configure(style=style)

    def _update_form_scroll_region(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self.form_canvas is None or not self.form_canvas.winfo_exists():
            return
        bbox = self.form_canvas.bbox("all")
        if bbox is None:
            self.form_canvas.configure(scrollregion=(0, 0, 0, 0))
            content_height = 0
        else:
            self.form_canvas.configure(scrollregion=bbox)
            content_height = max(0, int(bbox[3] - bbox[1]))

        canvas_height = int(self.form_canvas.winfo_height())
        if canvas_height <= 1:
            return

        if content_height <= canvas_height:
            self.form_canvas.yview_moveto(0.0)
            if (
                self.form_scrollbar is not None
                and self.form_scrollbar.winfo_exists()
                and self.form_scrollbar.winfo_manager()
            ):
                self.form_scrollbar.grid_remove()
            return

        if (
            self.form_scrollbar is not None
            and self.form_scrollbar.winfo_exists()
            and not self.form_scrollbar.winfo_manager()
        ):
            self.form_scrollbar.grid()
        first, last = self.form_canvas.yview()
        span = last - first
        if first > 0.0 and first + span > 1.0:
            self.form_canvas.yview_moveto(max(0.0, 1.0 - span))

    def _resize_fields_container(self, event: tk.Event[tk.Misc]) -> None:
        if (
            self.form_canvas is not None
            and self._fields_window is not None
            and self.form_canvas.winfo_exists()
        ):
            self.form_canvas.itemconfigure(self._fields_window, width=event.width)
            self._update_form_scroll_region()

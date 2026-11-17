"""Sistem vizual unitar «Soft Medical» pentru interfața Tkinter / ttk OrtoDocs."""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Literal


# Paletă Soft Medical
BG_APP = "#F1F4F2"
BG_SURFACE = "#FFFFFF"
BG_SUBTLE = "#F6F8F7"
BG_HOVER = "#E8EFEC"

PRIMARY = "#355F5B"
PRIMARY_HOVER = "#2A4D49"
PRIMARY_PRESSED = "#213E3B"
ACCENT_SECONDARY = "#DCE9E5"

TEXT_PRIMARY = "#24332F"
TEXT_SECONDARY = "#5F706A"
TEXT_DISCRETE = "#667770"
TEXT_ON_PRIMARY = "#FFFFFF"

BORDER = "#DCE3E0"
DISABLED_BG = "#E6EBE8"
DISABLED_FG = "#798681"

# Stări semantice discrete
SUCCESS_FG = "#236B4B"
SUCCESS_BG = "#E3F1EA"
SUCCESS_BORDER = "#B7DAC7"

WARNING_FG = "#8A6116"
WARNING_BG = "#FAF2DC"
WARNING_BORDER = "#E5CFA0"

ERROR_FG = "#A63A3A"
ERROR_BG = "#FBEAEA"
ERROR_BORDER = "#E7B8B8"

NEUTRAL_FG = "#4B5B56"
NEUTRAL_BG = "#ECEFEF"
NEUTRAL_BORDER = "#D2D9D6"

# Spațieri standard
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 14
SPACE_LG = 20
SPACE_XL = 26

BadgeKind = Literal["success", "warning", "error", "neutral"]


@dataclass(frozen=True, slots=True)
class ThemeFonts:
    family: str
    heading_family: str
    emoji_family: str
    mono_family: str
    heading_weight: str


def _heading_font(fonts: ThemeFonts, size: int) -> tuple[str, ...]:
    if fonts.heading_weight:
        return (fonts.heading_family, size, fonts.heading_weight)
    return (fonts.heading_family, size)


def resolve_theme_fonts(root: tk.Misc) -> ThemeFonts:
    """Selectează fonturile compatibile Windows/DPI cu fallback sigur."""

    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        available = set()

    if "Segoe UI" in available:
        family = "Segoe UI"
    elif "Arial" in available:
        family = "Arial"
    else:
        family = "TkDefaultFont"

    if "Segoe UI Semibold" in available:
        heading_family = "Segoe UI Semibold"
        heading_weight = ""
    else:
        heading_family = family
        heading_weight = "bold"

    emoji_family = "Segoe UI Emoji" if "Segoe UI Emoji" in available else family
    mono_family = "Consolas" if "Consolas" in available else "Courier New"
    return ThemeFonts(
        family=family,
        heading_family=heading_family,
        emoji_family=emoji_family,
        mono_family=mono_family,
        heading_weight=heading_weight,
    )


def apply_soft_medical_theme(root: tk.Misc) -> ThemeFonts:
    """Configurează tema globală Soft Medical pe fereastra rădăcină."""

    fonts = resolve_theme_fonts(root)
    style = ttk.Style(root)
    available_themes = style.theme_names()
    if "clam" in available_themes:
        style.theme_use("clam")
    elif "vista" in available_themes:
        style.theme_use("vista")

    default_font = tkfont.nametofont("TkDefaultFont")
    default_font.configure(family=fonts.family, size=10)
    try:
        tkfont.nametofont("TkTextFont").configure(family=fonts.family, size=10)
        tkfont.nametofont("TkHeadingFont").configure(
            family=fonts.heading_family,
            size=10,
            weight="normal" if not fonts.heading_weight else "bold",
        )
    except tk.TclError:
        pass

    linespace = max(16, int(default_font.metrics("linespace")))
    row_height = max(30, int(linespace * 1.7))

    window_classes = tuple(
        cls for cls in (tk.Tk, tk.Toplevel, tk.Wm) if isinstance(cls, type)
    )
    if window_classes and isinstance(root, window_classes):
        root.configure(background=BG_APP)

    # Containere de bază
    style.configure("TFrame", background=BG_APP)
    style.configure("App.TFrame", background=BG_APP)
    style.configure("Sidebar.TFrame", background=BG_SURFACE)
    style.configure("Card.TFrame", background=BG_SURFACE)
    style.configure("Subtle.TFrame", background=BG_SUBTLE)
    style.configure("Hairline.TFrame", background=BORDER)
    style.configure("ActiveMarker.TFrame", background=PRIMARY)

    # Tipografie
    field_label_fg = "#3F504B"
    style.configure("TLabel", background=BG_APP, foreground=TEXT_PRIMARY, font=(fonts.family, 10))
    style.configure("Card.TLabel", background=BG_SURFACE, foreground=TEXT_PRIMARY, font=(fonts.family, 10))
    style.configure("Subtle.TLabel", background=BG_SUBTLE, foreground=TEXT_PRIMARY, font=(fonts.family, 10))
    style.configure("PageTitle.TLabel", background=BG_APP, foreground=TEXT_PRIMARY, font=(fonts.family, 20, "bold"))
    style.configure("SidebarTitle.TLabel", background=BG_SURFACE, foreground=PRIMARY, font=(fonts.family, 16, "bold"))
    style.configure("SidebarMuted.TLabel", background=BG_SURFACE, foreground=TEXT_SECONDARY, font=(fonts.family, 9))
    style.configure("CardTitle.TLabel", background=BG_SURFACE, foreground=TEXT_PRIMARY, font=_heading_font(fonts, 11))
    style.configure("StepTag.TLabel", background=ACCENT_SECONDARY, foreground=PRIMARY, font=_heading_font(fonts, 9), padding=(8, 3))
    style.configure("Muted.TLabel", background=BG_APP, foreground=TEXT_SECONDARY, font=(fonts.family, 10))
    style.configure("CardMuted.TLabel", background=BG_SURFACE, foreground=TEXT_SECONDARY, font=(fonts.family, 9))
    style.configure("CardCode.TLabel", background=BG_SURFACE, foreground=TEXT_DISCRETE, font=(fonts.family, 9))
    style.configure("CardFieldLabel.TLabel", background=BG_SURFACE, foreground=field_label_fg, font=_heading_font(fonts, 10))
    style.configure("SubtleFieldLabel.TLabel", background=BG_SUBTLE, foreground=field_label_fg, font=_heading_font(fonts, 9))
    style.configure("Mono.TLabel", background=BG_SUBTLE, foreground=TEXT_PRIMARY, font=(fonts.mono_family, 9))

    # Stări și compatibilitate stiluri existente GenerationView
    style.configure("Success.TLabel", background=BG_APP, foreground=SUCCESS_FG, font=(fonts.family, 10))
    style.configure("Warning.TLabel", background=BG_APP, foreground=WARNING_FG, font=(fonts.family, 10))
    style.configure("Error.TLabel", background=BG_APP, foreground=ERROR_FG, font=(fonts.family, 10))
    style.configure("CardSuccess.TLabel", background=BG_SURFACE, foreground=SUCCESS_FG, font=(fonts.family, 10))
    style.configure("CardError.TLabel", background=BG_SURFACE, foreground=ERROR_FG, font=(fonts.family, 10))

    style.configure("GenerationCard.TLabelframe", background=BG_SURFACE, bordercolor=BORDER, borderwidth=1, relief="solid", padding=(16, 14))
    style.configure("GenerationCard.TLabelframe.Label", background=BG_SURFACE, foreground=PRIMARY, font=_heading_font(fonts, 11))
    style.configure("GenerationHeading.TLabel", background=BG_SURFACE, foreground=TEXT_PRIMARY, font=_heading_font(fonts, 11))
    style.configure("GenerationMuted.TLabel", background=BG_SURFACE, foreground=TEXT_SECONDARY, font=(fonts.family, 9))
    style.configure("GenerationError.TLabel", background=BG_SURFACE, foreground=ERROR_FG, font=(fonts.family, 9))
    style.configure("GenerationSuccess.TLabel", background=BG_SURFACE, foreground=SUCCESS_FG, font=_heading_font(fonts, 10))
    style.configure("GenerationRequired.TLabel", background=BG_SURFACE, foreground=PRIMARY, font=_heading_font(fonts, 10))

    # Badge-uri de stare
    for prefix, fg, bg in (
        ("SuccessBadge", SUCCESS_FG, SUCCESS_BG),
        ("WarningBadge", WARNING_FG, WARNING_BG),
        ("ErrorBadge", ERROR_FG, ERROR_BG),
        ("NeutralBadge", NEUTRAL_FG, NEUTRAL_BG),
    ):
        style.configure(
            f"{prefix}.TLabel",
            background=bg,
            foreground=fg,
            font=_heading_font(fonts, 9),
            padding=(9, 4),
        )

    # Butoane
    style.configure(
        "TButton",
        background=BG_SURFACE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        lightcolor=BG_SURFACE,
        darkcolor=BG_SURFACE,
        focuscolor=PRIMARY,
        focusthickness=1,
        borderwidth=1,
        relief="flat",
        padding=(12, 7),
        font=(fonts.family, 10),
    )
    style.map(
        "TButton",
        background=[("disabled", DISABLED_BG), ("pressed", ACCENT_SECONDARY), ("active", BG_HOVER)],
        foreground=[("disabled", DISABLED_FG)],
        bordercolor=[("focus", PRIMARY), ("active", BORDER)],
    )

    style.configure(
        "Primary.TButton",
        background=PRIMARY,
        foreground=TEXT_ON_PRIMARY,
        bordercolor=PRIMARY,
        lightcolor=PRIMARY,
        darkcolor=PRIMARY,
        focuscolor=ACCENT_SECONDARY,
        focusthickness=1,
        borderwidth=1,
        relief="flat",
        padding=(16, 8),
        font=(fonts.family, 10, "bold"),
    )
    style.map(
        "Primary.TButton",
        background=[("disabled", DISABLED_BG), ("pressed", PRIMARY_PRESSED), ("active", PRIMARY_HOVER)],
        foreground=[("disabled", DISABLED_FG), ("active", TEXT_ON_PRIMARY)],
        bordercolor=[("disabled", DISABLED_BG), ("active", PRIMARY_HOVER)],
    )

    style.configure(
        "Secondary.TButton",
        background=BG_SURFACE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        lightcolor=BG_SURFACE,
        darkcolor=BG_SURFACE,
        focuscolor=PRIMARY,
        focusthickness=1,
        borderwidth=1,
        relief="flat",
        padding=(12, 7),
        font=(fonts.family, 10),
    )
    style.map(
        "Secondary.TButton",
        background=[("disabled", DISABLED_BG), ("pressed", ACCENT_SECONDARY), ("active", BG_HOVER)],
        foreground=[("disabled", DISABLED_FG)],
        bordercolor=[("focus", PRIMARY), ("active", PRIMARY)],
    )

    style.configure(
        "Subtle.TButton",
        background=BG_SUBTLE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        lightcolor=BG_SUBTLE,
        darkcolor=BG_SUBTLE,
        borderwidth=1,
        relief="flat",
        padding=(10, 6),
        font=(fonts.family, 9),
    )
    style.map(
        "Subtle.TButton",
        background=[("disabled", DISABLED_BG), ("pressed", ACCENT_SECONDARY), ("active", BG_HOVER)],
        foreground=[("disabled", DISABLED_FG)],
    )

    style.configure(
        "Navigation.TButton",
        anchor="w",
        background=BG_SURFACE,
        foreground=TEXT_PRIMARY,
        bordercolor=BG_SURFACE,
        lightcolor=BG_SURFACE,
        darkcolor=BG_SURFACE,
        focuscolor=PRIMARY,
        borderwidth=0,
        relief="flat",
        padding=(12, 10),
        font=(fonts.family, 10),
    )
    style.map(
        "Navigation.TButton",
        background=[("pressed", ACCENT_SECONDARY), ("active", BG_HOVER)],
        foreground=[("active", TEXT_PRIMARY)],
    )

    style.configure(
        "NavigationActive.TButton",
        anchor="w",
        background=ACCENT_SECONDARY,
        foreground=PRIMARY,
        bordercolor=ACCENT_SECONDARY,
        lightcolor=ACCENT_SECONDARY,
        darkcolor=ACCENT_SECONDARY,
        focuscolor=PRIMARY,
        borderwidth=0,
        relief="flat",
        padding=(12, 10),
        font=(fonts.family, 10, "bold"),
    )
    style.map(
        "NavigationActive.TButton",
        background=[("pressed", ACCENT_SECONDARY), ("active", ACCENT_SECONDARY)],
        foreground=[("active", PRIMARY)],
    )

    # Câmpuri input
    style.configure(
        "TEntry",
        fieldbackground=BG_SURFACE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        lightcolor=BG_SURFACE,
        darkcolor=BG_SURFACE,
        insertcolor=TEXT_PRIMARY,
        borderwidth=1,
        padding=(8, 6),
        font=(fonts.family, 10),
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", PRIMARY), ("readonly", BORDER)],
        fieldbackground=[("readonly", BG_SUBTLE), ("disabled", DISABLED_BG)],
        foreground=[("disabled", DISABLED_FG)],
    )
    style.configure(
        "Invalid.TEntry",
        fieldbackground=ERROR_BG,
        foreground=TEXT_PRIMARY,
        bordercolor=ERROR_FG,
        padding=(8, 6),
        font=(fonts.family, 10),
    )

    style.configure(
        "TCombobox",
        fieldbackground=BG_SURFACE,
        background=BG_SUBTLE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        lightcolor=BG_SURFACE,
        darkcolor=BG_SURFACE,
        arrowcolor=PRIMARY,
        borderwidth=1,
        padding=(8, 5),
        font=(fonts.family, 10),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", BG_SURFACE), ("disabled", DISABLED_BG)],
        bordercolor=[("focus", PRIMARY)],
        background=[("active", BG_HOVER)],
    )
    style.configure(
        "Invalid.TCombobox",
        fieldbackground=ERROR_BG,
        foreground=TEXT_PRIMARY,
        bordercolor=ERROR_FG,
        arrowcolor=ERROR_FG,
        padding=(8, 5),
    )
    style.map(
        "Invalid.TCombobox",
        fieldbackground=[("readonly", ERROR_BG)],
        bordercolor=[("focus", ERROR_FG)],
    )

    # Checkbutton și LabelFrame
    style.configure("TCheckbutton", background=BG_SURFACE, foreground=TEXT_PRIMARY, font=(fonts.family, 10))
    style.configure("TLabelframe", background=BG_SURFACE, bordercolor=BORDER, borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", background=BG_SURFACE, foreground=PRIMARY, font=(fonts.family, 10, "bold"))

    # Liste / Treeview
    style.configure(
        "Treeview",
        background=BG_SURFACE,
        fieldbackground=BG_SURFACE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        borderwidth=1,
        relief="flat",
        rowheight=row_height,
        font=(fonts.family, 10),
    )
    style.map(
        "Treeview",
        background=[("selected", ACCENT_SECONDARY)],
        foreground=[("selected", TEXT_PRIMARY)],
    )
    style.configure(
        "Treeview.Heading",
        background=BG_SUBTLE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        relief="flat",
        padding=(10, 8),
        font=_heading_font(fonts, 10),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", BG_HOVER)],
    )

    # Notebook
    style.configure("TNotebook", background=BG_APP, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab",
        background=BG_HOVER,
        foreground=TEXT_SECONDARY,
        bordercolor=BORDER,
        lightcolor=BG_HOVER,
        darkcolor=BG_HOVER,
        padding=(16, 9),
        font=(fonts.family, 10),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", BG_SURFACE), ("active", ACCENT_SECONDARY)],
        foreground=[("selected", PRIMARY), ("active", TEXT_PRIMARY)],
        font=[("selected", (fonts.family, 10, "bold"))],
    )

    # Scrollbar și Progressbar
    style.configure(
        "Vertical.TScrollbar",
        background="#C5D0CC",
        troughcolor=BG_APP,
        bordercolor=BG_APP,
        arrowcolor=TEXT_SECONDARY,
        relief="flat",
    )
    style.map(
        "Vertical.TScrollbar",
        background=[("active", TEXT_SECONDARY)],
    )
    style.configure(
        "TProgressbar",
        background=PRIMARY,
        troughcolor=ACCENT_SECONDARY,
        bordercolor=BORDER,
        thickness=8,
    )
    return fonts


def resolve_app_icon_path() -> Path | None:
    """Returnează calea către iconița aplicației (în dezvoltare sau build frozen)."""

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if isinstance(meipass, str) and meipass:
        meipass_dir = Path(meipass)
        candidates.append(meipass_dir / "ortodocs.ico")
        candidates.append(meipass_dir / "packaging" / "ortodocs.ico")
    candidates.append(Path(__file__).resolve().parents[3] / "packaging" / "ortodocs.ico")

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def apply_window_icon(window: tk.Misc) -> bool:
    """Aplică iconița OrtoDocs pe Windows fără a afecta DPI sau testele headless."""

    if sys.platform != "win32":
        return False
    icon_path = resolve_app_icon_path()
    if icon_path is None:
        return False
    iconbitmap = getattr(window, "iconbitmap", None)
    if not callable(iconbitmap):
        return False
    try:
        is_root = isinstance(tk.Tk, type) and isinstance(window, tk.Tk)
        if not is_root:
            is_root = str(window) == "."
        if is_root:
            iconbitmap(default=str(icon_path))
        else:
            iconbitmap(str(icon_path))
        return True
    except tk.TclError:
        return False


def style_toplevel(window: tk.Toplevel) -> None:
    """Aplică fundalul Soft Medical și iconița unei ferestre secundare."""

    window.configure(background=BG_APP)
    apply_window_icon(window)


def style_text_widget(widget: tk.Text, *, subtle: bool = False) -> None:
    """Uniformizează aspectul widgeturilor tk.Text."""

    widget.configure(
        background=BG_SUBTLE if subtle else BG_SURFACE,
        foreground=TEXT_PRIMARY,
        insertbackground=TEXT_PRIMARY,
        selectbackground=ACCENT_SECONDARY,
        selectforeground=TEXT_PRIMARY,
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=PRIMARY,
        relief="flat",
        borderwidth=0,
        padx=10,
        pady=8,
    )


def apply_badge_style(label: ttk.Label, kind: BadgeKind) -> None:
    mapping = {
        "success": "SuccessBadge.TLabel",
        "warning": "WarningBadge.TLabel",
        "error": "ErrorBadge.TLabel",
        "neutral": "NeutralBadge.TLabel",
    }
    label.configure(style=mapping[kind])


class CardFrame(tk.Frame):
    """Card alb Soft Medical cu bordură discretă și padding interior uniform."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        padding: tuple[int, int] | tuple[int, int, int, int] = (18, 16),
    ) -> None:
        super().__init__(
            parent,
            background=BG_SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            borderwidth=0,
        )
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.body = ttk.Frame(self, style="Card.TFrame", padding=padding)
        self.body.grid(row=0, column=0, sticky="nsew")


class CollapsibleSection(ttk.Frame):
    """Secțiune expandabilă la cerere pentru detalii secundare/tehnice."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        expanded: bool = False,
        style: str = "Card.TFrame",
    ) -> None:
        super().__init__(parent, style=style)
        self.columnconfigure(0, weight=1)
        self._title = title
        self._expanded = expanded
        self._toggle_var = tk.StringVar(master=self, value=self._button_label())

        self.toggle_button = ttk.Button(
            self,
            textvariable=self._toggle_var,
            style="Subtle.TButton",
            command=self.toggle,
        )
        self.toggle_button.grid(row=0, column=0, sticky="w")

        inner_style = "Subtle.TFrame"
        self.content = ttk.Frame(self, style=inner_style, padding=(12, 10))
        self.content.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.content.columnconfigure(0, weight=1)
        if not self._expanded:
            self.content.grid_remove()

    def _button_label(self) -> str:
        prefix = "▾" if self._expanded else "▸"
        return f"{prefix} {self._title}"

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self._toggle_var.set(self._button_label())
        if self._expanded:
            self.content.grid()
        else:
            self.content.grid_remove()

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

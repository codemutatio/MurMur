"""Murmur main window — PyObjC/AppKit view layer (main thread only).

Thin view code: all logic lives in tested modules (``output.reader``,
``app.settings_io``, ``control``). The window shares the single
``NSApplication`` run loop that ``rumps`` already drives, so it must only be
built and shown on the main thread (ADR-1).

Three tabs:
  * Today    — live status header, pause controls, auto-refreshing transcript.
  * History  — pick a past day, view its transcript read-only.
  * Settings — edit the user-facing config fields, Save writes config.toml.

Helper/builder methods are marked ``@objc.python_method`` so PyObjC does not
bridge them into Objective-C selectors; only the target/action, timer, delegate
and ``init`` methods are real selectors. This module is excluded from coverage
(see pyproject ``[tool.coverage.run]``): it is GUI glue that needs a running
window server to exercise.
"""

from __future__ import annotations

import subprocess
from datetime import date
from typing import Any

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSButton,
    NSPopUpButton,
    NSScrollView,
    NSTabView,
    NSTabViewItem,
    NSTextField,
    NSTextView,
    NSView,
    NSViewHeightSizable,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject, NSTimer

from murmur.app.settings_io import with_edits, write_config
from murmur.config import Config, load_config
from murmur.control import Controller, IconState, StatusBoard
from murmur.output.reader import TranscriptReader

_WIN_W = 640.0
_WIN_H = 560.0
_PAD = 16.0


def _label(frame: Any, text: str = "") -> Any:
    """A non-editable, borderless, transparent text label."""
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    return field


def _scrolling_textview(frame: Any) -> tuple[Any, Any]:
    """Return (scrollview, textview): a read-only, vertically scrolling view."""
    scroll = NSScrollView.alloc().initWithFrame_(frame)
    scroll.setHasVerticalScroller_(True)
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    text = NSTextView.alloc().initWithFrame_(frame)
    text.setEditable_(False)
    text.setRichText_(False)
    text.setAutoresizingMask_(NSViewWidthSizable)
    scroll.setDocumentView_(text)
    return scroll, text


class MurmurWindowController(NSObject):  # type: ignore[misc]
    """Owns the main window and refreshes it on a 1 s timer while visible."""

    # ------------------------------------------------------------------
    # Construction (PyObjC alloc().init... pattern)

    def initWithDeps_(self, deps: dict[str, Any]) -> Any:
        self = objc.super(MurmurWindowController, self).init()
        if self is None:
            return None
        self._controller: Controller = deps["controller"]
        self._status: StatusBoard = deps["status"]
        self._reader: TranscriptReader = deps["reader"]
        self._config_file = deps["config_file"]
        self._timer: Any = None
        self._history_dates: list[date] = []
        self._pause_button: Any = None
        self._build_window()
        return self

    # ------------------------------------------------------------------
    # Window construction

    @objc.python_method
    def _build_window(self) -> None:
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, _WIN_W, _WIN_H), style, NSBackingStoreBuffered, False
        )
        win.setTitle_("Murmur")
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self)
        win.center()
        self._window = win

        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(0, 0, _WIN_W, _WIN_H))
        tabs.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        tabs.addTabViewItem_(self._build_today_tab())
        tabs.addTabViewItem_(self._build_history_tab())
        tabs.addTabViewItem_(self._build_settings_tab())
        win.contentView().addSubview_(tabs)

    @objc.python_method
    def _tab(self, label: str) -> tuple[Any, Any]:
        item = NSTabViewItem.alloc().initWithIdentifier_(label)
        item.setLabel_(label)
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _WIN_W, _WIN_H - 40))
        item.setView_(view)
        return item, view

    @objc.python_method
    def _build_today_tab(self) -> Any:
        item, view = self._tab("Today")
        w, h = _WIN_W, _WIN_H - 40

        self._status_label = _label(NSMakeRect(_PAD, h - 36, w - 2 * _PAD, 20), "Starting…")
        self._status_label.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
        view.addSubview_(self._status_label)

        # Pause controls row.
        specs = [
            ("Pause", "onPauseResume:"),
            ("30 min", "onPause30:"),
            ("1 hour", "onPause1h:"),
            ("Rest of day", "onPauseDay:"),
        ]
        x = _PAD
        y = h - 72
        for title, action in specs:
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, 110, 28))
            btn.setTitle_(title)
            btn.setBezelStyle_(1)  # NSBezelStyleRounded
            btn.setTarget_(self)
            btn.setAction_(action)
            btn.setAutoresizingMask_(NSViewMinYMargin)
            view.addSubview_(btn)
            if action == "onPauseResume:":
                self._pause_button = btn
            x += 118

        scroll, text = _scrolling_textview(NSMakeRect(_PAD, _PAD, w - 2 * _PAD, h - 96))
        self._today_text = text
        view.addSubview_(scroll)
        return item

    @objc.python_method
    def _build_history_tab(self) -> Any:
        item, view = self._tab("History")
        w, h = _WIN_W, _WIN_H - 40

        self._history_popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(_PAD, h - 40, 220, 26)
        )
        self._history_popup.setTarget_(self)
        self._history_popup.setAction_("onHistoryPick:")
        self._history_popup.setAutoresizingMask_(NSViewMinYMargin)
        view.addSubview_(self._history_popup)

        scroll, text = _scrolling_textview(NSMakeRect(_PAD, _PAD, w - 2 * _PAD, h - 64))
        self._history_text = text
        view.addSubview_(scroll)
        return item

    @objc.python_method
    def _build_settings_tab(self) -> Any:
        item, view = self._tab("Settings")
        w, h = _WIN_W, _WIN_H - 40
        y = h - 44

        view.addSubview_(
            _label(NSMakeRect(_PAD, y, 300, 18), "Vocabulary hints (comma-separated):")
        )
        y -= 26
        self._vocab_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(_PAD, y, w - 2 * _PAD, 24)
        )
        self._vocab_field.setAutoresizingMask_(NSViewWidthSizable)
        view.addSubview_(self._vocab_field)
        y -= 40

        view.addSubview_(_label(NSMakeRect(_PAD, y, 230, 18), "Retention days (0 = keep forever):"))
        self._retention_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(_PAD + 240, y - 2, 80, 24)
        )
        view.addSubview_(self._retention_field)
        y -= 40

        self._keep_audio_check = NSButton.alloc().initWithFrame_(NSMakeRect(_PAD, y, 360, 22))
        self._keep_audio_check.setButtonType_(3)  # NSButtonTypeSwitch
        self._keep_audio_check.setTitle_("Keep audio after transcription")
        view.addSubview_(self._keep_audio_check)
        y -= 30

        self._api_fallback_check = NSButton.alloc().initWithFrame_(NSMakeRect(_PAD, y, 360, 22))
        self._api_fallback_check.setButtonType_(3)
        self._api_fallback_check.setTitle_("Enable API fallback (needs OPENAI_API_KEY)")
        view.addSubview_(self._api_fallback_check)
        y -= 40

        view.addSubview_(_label(NSMakeRect(_PAD, y, 120, 18), "Toggle hotkey:"))
        self._hotkey_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(_PAD + 130, y - 2, 200, 24)
        )
        view.addSubview_(self._hotkey_field)
        y -= 48

        save = NSButton.alloc().initWithFrame_(NSMakeRect(_PAD, y, 180, 30))
        save.setTitle_("Save (restart to apply)")
        save.setBezelStyle_(1)
        save.setTarget_(self)
        save.setAction_("onSaveSettings:")
        view.addSubview_(save)

        reveal = NSButton.alloc().initWithFrame_(NSMakeRect(_PAD + 196, y, 170, 30))
        reveal.setTitle_("Reveal config.toml")
        reveal.setBezelStyle_(1)
        reveal.setTarget_(self)
        reveal.setAction_("onRevealConfig:")
        view.addSubview_(reveal)

        self._settings_status = _label(NSMakeRect(_PAD, y - 28, w - 2 * _PAD, 18), "")
        self._settings_status.setAutoresizingMask_(NSViewWidthSizable)
        view.addSubview_(self._settings_status)
        return item

    # ------------------------------------------------------------------
    # Public API (called from the menubar, main thread)

    @objc.python_method
    def show(self) -> None:
        """Bring the window to front, refresh, and start the 1 s timer."""
        self._reload_settings_fields()
        self._refresh_history_list()
        self._refresh_today()
        self._window.makeKeyAndOrderFront_(None)
        if self._timer is None:
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self, "onTick:", None, True
            )

    # ------------------------------------------------------------------
    # Refresh helpers

    @objc.python_method
    def _refresh_today(self) -> None:
        snap = self._status.snapshot()
        state = {
            IconState.RECORDING: "● Recording",
            IconState.PAUSED: "⏸ Paused",
            IconState.ERROR: "⚠ Error",
        }.get(snap.icon, str(snap.icon.value))
        line = f"{state}    Queue: {snap.queue_depth}    Errors: {snap.error_count}"
        if snap.notice:
            line += f"    {snap.notice}"
        self._status_label.setStringValue_(line)
        if self._pause_button is not None:
            self._pause_button.setTitle_("Resume" if snap.icon == IconState.PAUSED else "Pause")

        entries = self._reader.entries_for(date.today())
        body = (
            "\n".join(f"[{e.time}]  {e.text}" for e in entries) or "No speech captured yet today."
        )
        if self._today_text.string() != body:
            self._today_text.setString_(body)
            self._today_text.scrollRangeToVisible_((len(body), 0))

    @objc.python_method
    def _refresh_history_list(self) -> None:
        self._history_dates = self._reader.history_dates()
        self._history_popup.removeAllItems()
        self._history_popup.addItemsWithTitles_([f"{d:%Y-%m-%d}" for d in self._history_dates])
        if self._history_dates:
            self._show_history_for(self._history_dates[0])

    @objc.python_method
    def _show_history_for(self, day: date) -> None:
        entries = self._reader.entries_for(day)
        body = "\n".join(f"[{e.time}]  {e.text}" for e in entries) or "(empty)"
        self._history_text.setString_(body)

    @objc.python_method
    def _reload_settings_fields(self) -> None:
        config: Config = load_config(self._config_file)[0]
        self._vocab_field.setStringValue_(", ".join(config.stt.vocab_hints))
        self._retention_field.setStringValue_(str(config.storage.retention_days))
        self._keep_audio_check.setState_(1 if config.storage.keep_audio else 0)
        self._api_fallback_check.setState_(1 if config.stt.api_fallback else 0)
        self._hotkey_field.setStringValue_(config.hotkey.toggle)
        self._settings_status.setStringValue_("")

    # ------------------------------------------------------------------
    # Selector methods (target/action, timer, delegate)

    def onTick_(self, _timer: object) -> None:
        self._refresh_today()

    def onPauseResume_(self, _sender: object) -> None:
        self._controller.toggle_pause()
        self._refresh_today()

    def onPause30_(self, _sender: object) -> None:
        self._controller.pause_for(30 * 60)

    def onPause1h_(self, _sender: object) -> None:
        self._controller.pause_for(60 * 60)

    def onPauseDay_(self, _sender: object) -> None:
        self._controller.pause_rest_of_day()

    def onHistoryPick_(self, _sender: object) -> None:
        idx = self._history_popup.indexOfSelectedItem()
        if 0 <= idx < len(self._history_dates):
            self._show_history_for(self._history_dates[idx])

    def onRevealConfig_(self, _sender: object) -> None:
        if self._config_file is not None:
            subprocess.run(["open", "-R", str(self._config_file)], check=False)

    def onSaveSettings_(self, _sender: object) -> None:
        if self._config_file is None:
            self._settings_status.setStringValue_("No config path available.")
            return
        config, _ = load_config(self._config_file)
        hints = tuple(h.strip() for h in self._vocab_field.stringValue().split(",") if h.strip())
        try:
            retention = int(self._retention_field.stringValue().strip() or "0")
        except ValueError:
            self._settings_status.setStringValue_("Retention days must be a whole number.")
            return
        edited = with_edits(
            config,
            vocab_hints=hints,
            keep_audio=self._keep_audio_check.state() == 1,
            api_fallback=self._api_fallback_check.state() == 1,
            retention_days=retention,
            hotkey_toggle=self._hotkey_field.stringValue().strip() or None,
        )
        write_config(self._config_file, edited)
        self._settings_status.setStringValue_("Saved. Restart Murmur to apply.")

    def windowWillClose_(self, _notification: object) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None


def make_window_controller(
    *,
    controller: Controller,
    status: StatusBoard,
    reader: TranscriptReader,
    config_file: Any,
) -> Any:
    """Build the window controller (module-level factory; see PyObjC note above)."""
    return MurmurWindowController.alloc().initWithDeps_(
        {
            "controller": controller,
            "status": status,
            "reader": reader,
            "config_file": config_file,
        }
    )

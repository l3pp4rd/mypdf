#!/usr/bin/env python3
"""mypdf - a lightweight PDF reader (GTK 3 + Poppler).

Usage: mypdf [file.pdf]
"""
import bisect
import os
import signal
import sys
import time
from collections import OrderedDict
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("Poppler", "0.18")
except ValueError:
    sys.exit("Poppler bindings are missing. Install them with:\n"
             "  sudo apt install gir1.2-poppler-0.18")
import cairo
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Poppler

MARGIN = 16                  # px around and between pages
ZOOM_MIN, ZOOM_MAX = 0.2, 6.0
ZOOM_STEP = 1.2
AUTO_ZOOM_MAX = 1.25        # cap for the automatic zoom used when opening
CACHE_PIXELS = 16_000_000    # rendered-page cache budget (~64 MB)
BACKGROUND = (0.40, 0.40, 0.42)
SEL_GLYPH = (0xFFFF, 0xFFFF, 0xFFFF)
SEL_BG = (0x3584, 0x8484, 0xE4E4)
MATCH_COLOR = (1.0, 0.85, 0.0, 0.40)
CURRENT_MATCH_COLOR = (1.0, 0.45, 0.0, 0.55)
SAFE_URI_SCHEMES = {"http", "https", "mailto", "ftp"}

CTRL = Gdk.ModifierType.CONTROL_MASK
SHIFT = Gdk.ModifierType.SHIFT_MASK
ALT = Gdk.ModifierType.MOD1_MASK


def poppler_color(rgb):
    c = Poppler.Color()
    c.red, c.green, c.blue = rgb
    return c


class Reader(Gtk.Window):
    def __init__(self, path=None):
        super().__init__(title="mypdf")
        self.set_default_size(1000, 850)
        self.set_icon_name("x-office-document")

        self.doc = None
        self.path = None
        self.pages = []          # Poppler.Page objects
        self.sizes = []          # page sizes in PDF points
        self.rects = []          # page rectangles on the canvas (x, y, w, h) in px
        self.ys = []             # top y of each page, for bisect
        self.zoom = 1.0
        self.fit_mode = "auto"     # "auto", "width" or None (fixed zoom)
        self.current = 0
        self.cache = OrderedDict()   # page index -> (key, surface)
        self.print_settings = None

        # Selection: anchor/end are (page, x, y) in PDF points.
        self.sel_anchor = None
        self.sel_end = None
        self.sel_style = Poppler.SelectionStyle.GLYPH
        self.dragging = False
        self.autoscroll_id = 0
        self.glyph_color = poppler_color(SEL_GLYPH)
        self.bg_color = poppler_color(SEL_BG)

        # Search: matches are (page, (x1, y1, x2, y2)) in top-left PDF points.
        self.matches = []
        self.match_pages = {}    # page -> list of rects, for drawing
        self.match_index = -1
        self.search_id = 0
        self.search_text = ""
        self.search_page = 0
        self.search_start = 0

        # Links
        self.link_cache = {}     # page -> list of (x1, y1, x2, y2, Poppler.LinkMapping)
        self.hover_link = None
        self.pressed_link = None
        self.history = []        # previous scroll positions, for Alt+Left

        self._build_ui()
        if path:
            GLib.idle_add(self.load, path)

    # ------------------------------------------------------------------ UI
    def _button(self, icon, tooltip, callback):
        b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        b.set_tooltip_text(tooltip)
        b.connect("clicked", lambda *_: callback())
        return b

    def _build_ui(self):
        hb = Gtk.HeaderBar(show_close_button=True, title="mypdf")
        self.headerbar = hb
        self.set_titlebar(hb)

        hb.pack_start(self._button("document-open-symbolic", "Open (Ctrl+O)", self.open_dialog))
        self.print_button = self._button("document-print-symbolic", "Print (Ctrl+P)", self.print_doc)
        hb.pack_start(self.print_button)

        nav = Gtk.Box(spacing=6)
        self.page_entry = Gtk.Entry(width_chars=4, xalign=1.0)
        self.page_entry.set_tooltip_text("Go to page")
        self.page_entry.connect("activate", self.on_page_entry)
        self.page_entry.connect("focus-out-event", lambda *_: self.update_page_entry())
        self.page_label = Gtk.Label(label="/ 0")
        nav.pack_start(self.page_entry, False, False, 0)
        nav.pack_start(self.page_label, False, False, 0)
        hb.pack_start(nav)

        zoom = Gtk.Box()
        zoom.get_style_context().add_class("linked")
        zoom.pack_start(self._button("zoom-out-symbolic", "Zoom out (Ctrl+-)", self.zoom_out), False, False, 0)
        self.zoom_button = Gtk.Button(label="100%")
        self.zoom_button.set_tooltip_text("Fit width (Ctrl+0)")
        self.zoom_button.connect("clicked", lambda *_: self.set_zoom(fit="width"))
        zoom.pack_start(self.zoom_button, False, False, 0)
        zoom.pack_start(self._button("zoom-in-symbolic", "Zoom in (Ctrl++)", self.zoom_in), False, False, 0)
        hb.pack_end(zoom)

        self.search_toggle = Gtk.ToggleButton(image=Gtk.Image.new_from_icon_name(
            "edit-find-symbolic", Gtk.IconSize.BUTTON), tooltip_text="Find (Ctrl+F)")
        hb.pack_end(self.search_toggle)

        self.search_entry = Gtk.SearchEntry(width_chars=30)
        self.search_entry.connect("search-changed", lambda *_: self.start_search())
        self.search_entry.connect("activate", lambda *_: self.next_match(1))
        self.search_entry.connect("next-match", lambda *_: self.next_match(1))
        self.search_entry.connect("previous-match", lambda *_: self.next_match(-1))
        self.search_entry.connect("key-press-event", self.on_search_key)
        self.search_label = Gtk.Label()
        self.search_label.get_style_context().add_class("dim-label")
        search_box = Gtk.Box(spacing=6)
        search_box.pack_start(self.search_entry, False, False, 0)
        nav_box = Gtk.Box()
        nav_box.get_style_context().add_class("linked")
        nav_box.pack_start(self._button("go-up-symbolic", "Previous match (Shift+Enter)",
                                        lambda: self.next_match(-1)), False, False, 0)
        nav_box.pack_start(self._button("go-down-symbolic", "Next match (Enter)",
                                        lambda: self.next_match(1)), False, False, 0)
        search_box.pack_start(nav_box, False, False, 0)
        search_box.pack_start(self.search_label, False, False, 6)
        self.search_bar = Gtk.SearchBar(show_close_button=True)
        self.search_bar.add(search_box)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.bind_property("search-mode-enabled", self.search_toggle, "active",
                                      GObject.BindingFlags.BIDIRECTIONAL)
        self.search_bar.connect("notify::search-mode-enabled", self.on_search_mode)

        self.canvas = Gtk.Layout()
        self.canvas.set_can_focus(True)
        self.canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                               | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                               | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.canvas.connect("draw", self.on_draw)
        self.canvas.connect("button-press-event", self.on_press)
        self.canvas.connect("motion-notify-event", self.on_motion)
        self.canvas.connect("button-release-event", self.on_release)
        self.canvas.connect("scroll-event", self.on_scroll)
        self.canvas.connect("size-allocate", self.on_size_allocate)
        self.canvas.connect("realize", lambda w: w.get_bin_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "text")))

        sw = Gtk.ScrolledWindow()
        sw.add(self.canvas)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(self.search_bar, False, False, 0)
        vbox.pack_start(sw, True, True, 0)
        self.add(vbox)
        self.hadj = sw.get_hadjustment()
        self.vadj = sw.get_vadjustment()
        self.vadj.connect("value-changed", lambda *_: self.update_current_page())
        self._last_width = 0

        self.connect("key-press-event", self.on_key)
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self.on_drop)
        self.update_sensitivity()

    def update_sensitivity(self):
        for w in (self.print_button, self.page_entry, self.search_toggle):
            w.set_sensitive(self.doc is not None)

    def error(self, text):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                              buttons=Gtk.ButtonsType.CLOSE, text=text)
        d.run()
        d.destroy()

    def ask_password(self, name, retry):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.OK_CANCEL, text="Password required")
        d.format_secondary_text(("Wrong password. " if retry else "")
                                + f"“{name}” is encrypted. Enter its password:")
        entry = Gtk.Entry(visibility=False, activates_default=True)
        d.get_message_area().pack_end(entry, False, False, 0)
        d.set_default_response(Gtk.ResponseType.OK)
        entry.show()
        ok = d.run() == Gtk.ResponseType.OK
        password = entry.get_text()
        d.destroy()
        return password if ok else None

    # ------------------------------------------------------------ document
    def load(self, path):
        gfile = Gio.File.new_for_commandline_arg(path)
        name = gfile.get_basename()
        password = None
        while True:
            try:
                doc = Poppler.Document.new_from_file(gfile.get_uri(), password)
                break
            except GLib.Error as e:
                if e.code == Poppler.Error.ENCRYPTED and "poppler" in str(e.domain):
                    password = self.ask_password(name, password is not None)
                    if password is None:
                        return False
                else:
                    self.error(f"Cannot open “{name}”:\n{e.message}")
                    return False

        self.doc = doc
        self.path = gfile.get_path()
        self.pages = [doc.get_page(i) for i in range(doc.get_n_pages())]
        self.sizes = [p.get_size() for p in self.pages]
        self.cache.clear()
        self.link_cache.clear()
        self.history.clear()
        self.hover_link = self.pressed_link = None
        self.sel_anchor = self.sel_end = None
        self.fit_mode = "auto"
        self.current = 0
        self.headerbar.set_title(doc.get_title() or name)
        self.headerbar.set_subtitle(name if doc.get_title() else None)
        self.page_label.set_text(f"/ {len(self.pages)}")
        self.update_page_entry()
        self.update_sensitivity()
        self.relayout()
        self.vadj.set_value(0)
        self.hadj.set_value(0)
        self.canvas.grab_focus()
        self.start_search()  # re-run (or clear) the search for the new document
        return False

    def open_dialog(self):
        d = Gtk.FileChooserNative(title="Open PDF", transient_for=self,
                                  action=Gtk.FileChooserAction.OPEN)
        f = Gtk.FileFilter()
        f.set_name("PDF documents")
        f.add_mime_type("application/pdf")
        f.add_pattern("*.pdf")
        f.add_pattern("*.PDF")
        d.add_filter(f)
        if self.path:
            d.set_current_folder(os.path.dirname(self.path))
        if d.run() == Gtk.ResponseType.ACCEPT:
            self.load(d.get_filename())
        d.destroy()

    def on_drop(self, widget, ctx, x, y, data, info, time):
        uris = data.get_uris()
        if uris:
            self.load(uris[0])

    # -------------------------------------------------------------- layout
    def relayout(self):
        if not self.pages:
            self.canvas.set_size(1, 1)
            return
        avail = self.canvas.get_allocated_width()
        max_w = max(w for w, _ in self.sizes)
        if self.fit_mode and avail > 2 * MARGIN:
            z = (avail - 2 * MARGIN) / max_w
            if self.fit_mode == "auto":
                z = min(z, AUTO_ZOOM_MAX)
            self.zoom = min(max(z, ZOOM_MIN), ZOOM_MAX)
        z = self.zoom
        canvas_w = max(avail, round(max_w * z) + 2 * MARGIN)
        y = MARGIN
        self.rects = []
        for w, h in self.sizes:
            pw, ph = round(w * z), round(h * z)
            self.rects.append(((canvas_w - pw) // 2, y, pw, ph))
            y += ph + MARGIN
        self.ys = [r[1] for r in self.rects]
        self.canvas.set_size(canvas_w, y)
        self.zoom_button.set_label(f"{round(z * 100)}%")
        self.canvas.queue_draw()

    def on_size_allocate(self, widget, alloc):
        if alloc.width != self._last_width:
            self._last_width = alloc.width
            GLib.idle_add(self._relayout_keep_position)

    def _relayout_keep_position(self):
        self.set_zoom(self.zoom, fit=self.fit_mode)
        return False

    def set_zoom(self, zoom=None, fit=None):
        if not self.pages:
            return
        vfrac = (self.vadj.get_value() + self.vadj.get_page_size() / 2) / max(1, self.vadj.get_upper())
        hfrac = (self.hadj.get_value() + self.hadj.get_page_size() / 2) / max(1, self.hadj.get_upper())
        self.fit_mode = fit
        if zoom is not None:
            self.zoom = min(max(zoom, ZOOM_MIN), ZOOM_MAX)
        self.relayout()
        self.vadj.set_value(vfrac * self.vadj.get_upper() - self.vadj.get_page_size() / 2)
        self.hadj.set_value(hfrac * self.hadj.get_upper() - self.hadj.get_page_size() / 2)

    def zoom_in(self):
        self.set_zoom(self.zoom * ZOOM_STEP)

    def zoom_out(self):
        self.set_zoom(self.zoom / ZOOM_STEP)

    def page_at(self, y):
        return max(0, bisect.bisect_right(self.ys, y) - 1)

    def update_current_page(self):
        if not self.rects:
            return
        i = self.page_at(self.vadj.get_value() + self.vadj.get_page_size() * 0.3)
        if i != self.current:
            self.current = i
            self.update_page_entry()

    def update_page_entry(self):
        if not self.page_entry.has_focus():
            self.page_entry.set_text(str(self.current + 1) if self.pages else "")

    def go_to_page(self, i, y=0.0):
        """Scroll so that point y (PDF points from the page top) of page i is at the top."""
        i = min(max(i, 0), len(self.pages) - 1)
        self.push_history()
        self.vadj.set_value(self.rects[i][1] + y * self.zoom - MARGIN // 2)

    def push_history(self):
        self.history.append(self.vadj.get_value())
        del self.history[:-50]

    def go_back(self):
        if self.history:
            self.vadj.set_value(self.history.pop())

    def on_page_entry(self, entry):
        try:
            self.go_to_page(int(entry.get_text()) - 1)
        except ValueError:
            pass
        self.canvas.grab_focus()
        self.update_page_entry()

    # ----------------------------------------------------------- rendering
    def render_page(self, i, scale):
        key = (self.zoom, scale)
        hit = self.cache.get(i)
        if hit and hit[0] == key:
            self.cache.move_to_end(i)
            return hit[1]
        _, _, pw, ph = self.rects[i]
        surf = cairo.ImageSurface(cairo.FORMAT_RGB24, pw * scale, ph * scale)
        surf.set_device_scale(scale, scale)
        cr = cairo.Context(surf)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.scale(self.zoom, self.zoom)
        self.pages[i].render(cr)
        self.cache[i] = (key, surf)
        total = sum(s.get_width() * s.get_height() for _, s in self.cache.values())
        while total > CACHE_PIXELS and len(self.cache) > 1:
            _, (_, old) = self.cache.popitem(last=False)
            total -= old.get_width() * old.get_height()
        return surf

    def on_draw(self, widget, cr):
        bin_window = widget.get_bin_window()
        if not Gtk.cairo_should_draw_window(cr, bin_window):
            return False
        cr.save()
        Gtk.cairo_transform_to_window(cr, widget, bin_window)
        x1, y1, x2, y2 = cr.clip_extents()
        cr.set_source_rgb(*BACKGROUND)
        cr.paint()
        if self.rects:
            scale = widget.get_scale_factor()
            for i in range(self.page_at(y1), len(self.rects)):
                px, py, pw, ph = self.rects[i]
                if py > y2:
                    break
                if py + ph < y1:
                    continue
                cr.set_source_rgba(0, 0, 0, 0.35)
                cr.rectangle(px + 2, py + 2, pw, ph)
                cr.fill()
                cr.set_source_surface(self.render_page(i, scale), px, py)
                cr.rectangle(px, py, pw, ph)
                cr.fill()
                sel = self.page_selection(i)
                if sel:
                    cr.save()
                    cr.translate(px, py)
                    cr.scale(self.zoom, self.zoom)
                    self.pages[i].render_selection(cr, sel, Poppler.Rectangle(), self.sel_style,
                                                   self.glyph_color, self.bg_color)
                    cr.restore()
                self.draw_matches(cr, i)
        cr.restore()
        return False

    def draw_matches(self, cr, i):
        rects = self.match_pages.get(i)
        if not rects:
            return
        px, py, _, _ = self.rects[i]
        z = self.zoom
        current = self.matches[self.match_index] if self.match_index >= 0 else None
        for r in rects:
            cr.set_source_rgba(*(CURRENT_MATCH_COLOR if current == (i, r) else MATCH_COLOR))
            cr.rectangle(px + r[0] * z - 1, py + r[1] * z - 1, (r[2] - r[0]) * z + 2, (r[3] - r[1]) * z + 2)
            cr.fill()

    # -------------------------------------------------------------- search
    def show_search(self):
        if not self.pages:
            return
        text = self.selected_text().strip()
        if text and "\n" not in text and len(text) < 100:
            self.search_entry.set_text(text)
        self.search_bar.set_search_mode(True)
        self.search_entry.grab_focus()

    def on_search_mode(self, bar, _pspec):
        if bar.get_search_mode():
            self.start_search()
            return
        self.cancel_search()
        self.matches, self.match_pages, self.match_index = [], {}, -1
        self.canvas.queue_draw()
        self.canvas.grab_focus()

    def on_search_key(self, entry, ev):
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and ev.state & SHIFT:
            self.next_match(-1)
            return True
        return False

    def cancel_search(self):
        if self.search_id:
            GLib.source_remove(self.search_id)
            self.search_id = 0

    def start_search(self):
        """(Re)start an incremental search; pages are scanned in idle time."""
        self.cancel_search()
        self.matches, self.match_pages, self.match_index = [], {}, -1
        self.search_text = self.search_entry.get_text() if self.search_bar.get_search_mode() else ""
        if self.search_text and self.pages:
            self.search_page = 0
            self.search_start = self.current
            self.search_id = GLib.idle_add(self.search_step)
        self.update_search_label()
        self.canvas.queue_draw()

    def search_step(self):
        deadline = time.monotonic() + 0.03
        n = len(self.pages)
        while self.search_page < n and time.monotonic() < deadline:
            i = self.search_page
            self.search_page += 1
            found = self.pages[i].find_text_with_options(self.search_text, Poppler.FindFlags.DEFAULT)
            if not found:
                continue
            h = self.sizes[i][1]
            rects = [(r.x1, h - r.y2, r.x2, h - r.y1) for r in found]
            self.match_pages[i] = rects
            first = len(self.matches)
            self.matches.extend((i, r) for r in rects)
            if self.match_index < 0 and i >= self.search_start:
                self.match_index = first
                self.show_match()
        done = self.search_page >= n
        if done:
            self.search_id = 0
            if self.match_index < 0 and self.matches:
                self.match_index = 0
                self.show_match()
        self.update_search_label()
        self.canvas.queue_draw()
        return not done

    def update_search_label(self):
        searching = bool(self.search_id)
        if self.matches:
            text = f"{self.match_index + 1} of {len(self.matches)}" if self.match_index >= 0 \
                else f"{len(self.matches)} found"
            text += "…" if searching else ""
        elif searching:
            text = "Searching…"
        else:
            text = "No matches" if self.search_text else ""
        self.search_label.set_text(text)

    def next_match(self, step):
        if not self.matches:
            return
        if self.match_index < 0:
            self.match_index = 0
        else:
            self.match_index = (self.match_index + step) % len(self.matches)
        self.show_match()
        self.update_search_label()

    def show_match(self):
        """Scroll the current match into view and redraw."""
        i, (x1, y1, x2, y2) = self.matches[self.match_index]
        px, py, _, _ = self.rects[i]
        z = self.zoom
        top, bottom = py + y1 * z, py + y2 * z
        v = self.vadj
        if top < v.get_value() or bottom > v.get_value() + v.get_page_size():
            v.set_value(top - v.get_page_size() / 3)
        left, right = px + x1 * z, px + x2 * z
        hz = self.hadj
        if left < hz.get_value() or right > hz.get_value() + hz.get_page_size():
            hz.set_value(left - hz.get_page_size() / 3)
        self.canvas.queue_draw()

    # --------------------------------------------------------------- links
    def page_links(self, i):
        if i not in self.link_cache:
            h = self.sizes[i][1]
            self.link_cache[i] = [(m.area.x1, h - m.area.y2, m.area.x2, h - m.area.y1, m)
                                  for m in self.pages[i].get_link_mapping()]
        return self.link_cache[i]

    def link_at(self, x, y):
        if not self.rects:
            return None
        i = self.page_at(y)
        px, py, pw, ph = self.rects[i]
        if not (px <= x <= px + pw and py <= y <= py + ph):
            return None
        lx, ly = (x - px) / self.zoom, (y - py) / self.zoom
        for x1, y1, x2, y2, mapping in self.page_links(i):
            if x1 <= lx <= x2 and y1 <= ly <= y2:
                return mapping.action
        return None

    def resolve_dest(self, dest):
        if dest and dest.type == Poppler.DestType.NAMED:
            dest = self.doc.find_dest(dest.named_dest)
        return dest

    def describe_link(self, action):
        t = action.type
        if t == Poppler.ActionType.URI:
            return action.uri.uri
        if t == Poppler.ActionType.GOTO_DEST:
            dest = self.resolve_dest(action.goto_dest.dest)
            return f"Go to page {dest.page_num}" if dest else None
        if t == Poppler.ActionType.GOTO_REMOTE:
            return f"Open {action.goto_remote.file_name}"
        if t == Poppler.ActionType.NAMED:
            return action.named.named_dest
        return None

    def set_hover_link(self, action):
        if action is self.hover_link or (action is None and self.hover_link is None):
            return
        self.hover_link = action
        window = self.canvas.get_bin_window()
        window.set_cursor(Gdk.Cursor.new_from_name(window.get_display(), "pointer" if action else "text"))
        self.canvas.set_tooltip_text(self.describe_link(action) if action else None)

    def activate_link(self, action):
        t = action.type
        if t == Poppler.ActionType.URI:
            self.open_uri(action.uri.uri)
        elif t == Poppler.ActionType.GOTO_DEST:
            self.go_to_dest(action.goto_dest.dest)
        elif t == Poppler.ActionType.GOTO_REMOTE:
            name = action.goto_remote.file_name
            path = name if os.path.isabs(name) else os.path.join(os.path.dirname(self.path or ""), name)
            if os.path.isfile(path):
                dest = action.goto_remote.dest
                self.load(path)
                self.go_to_dest(dest)
            else:
                self.error(f"Linked file not found:\n{path}")
        elif t == Poppler.ActionType.NAMED:
            name = action.named.named_dest
            target = {"NextPage": self.current + 1, "PrevPage": self.current - 1,
                      "FirstPage": 0, "LastPage": len(self.pages) - 1}.get(name)
            if target is not None:
                self.go_to_page(target)
        # Other action types (launching programs, JavaScript, media) are deliberately ignored.

    def go_to_dest(self, dest):
        dest = self.resolve_dest(dest)
        if not dest or not 1 <= dest.page_num <= len(self.pages):
            return
        i = dest.page_num - 1
        y = 0.0
        if dest.type in (Poppler.DestType.XYZ, Poppler.DestType.FITH, Poppler.DestType.FITBH) and dest.top > 0:
            y = max(0.0, self.sizes[i][1] - dest.top)
        self.go_to_page(i, y)

    def open_uri(self, uri):
        if urlparse(uri).scheme.lower() not in SAFE_URI_SCHEMES:
            if uri.startswith("www."):
                uri = "https://" + uri
            else:
                self.error(f"Not opening link with unsupported address:\n{uri}")
                return
        try:
            Gtk.show_uri_on_window(self, uri, Gdk.CURRENT_TIME)
        except GLib.Error as e:
            self.error(f"Cannot open link:\n{e.message}")

    # ----------------------------------------------------------- selection
    def hit(self, x, y):
        """Canvas px -> (page, x, y) in PDF points, clamped to the nearest page."""
        i = self.page_at(y)
        px, py, pw, ph = self.rects[i]
        return (i, min(max(x - px, 0), pw) / self.zoom, min(max(y - py, 0), ph) / self.zoom)

    def selection_range(self):
        if self.sel_anchor is None or self.sel_end is None:
            return None
        a, b = self.sel_anchor, self.sel_end
        return (a, b) if a[0] <= b[0] else (b, a)

    def page_selection(self, i):
        rng = self.selection_range()
        if not rng:
            return None
        a, b = rng
        if not a[0] <= i <= b[0]:
            return None
        w, h = self.sizes[i]
        r = Poppler.Rectangle()
        r.x1, r.y1 = (a[1], a[2]) if i == a[0] else (0, 0)
        r.x2, r.y2 = (b[1], b[2]) if i == b[0] else (w, h)
        return r

    def selected_text(self):
        rng = self.selection_range()
        if not rng:
            return ""
        parts = []
        for i in range(rng[0][0], rng[1][0] + 1):
            t = self.pages[i].get_selected_text(self.sel_style, self.page_selection(i))
            if t:
                parts.append(t)
        return "\n".join(parts)

    def select_all(self):
        if self.pages:
            self.sel_style = Poppler.SelectionStyle.GLYPH
            self.sel_anchor = (0, 0, 0)
            self.sel_end = (len(self.pages) - 1, *self.sizes[-1])
            self.canvas.queue_draw()
            self.set_primary()

    def copy(self):
        text = self.selected_text()
        if text:
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            cb.set_text(text, -1)
            cb.store()

    def set_primary(self):
        text = self.selected_text()
        if text:
            Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY).set_text(text, -1)

    # --------------------------------------------------------------- mouse
    def on_press(self, widget, ev):
        self.canvas.grab_focus()
        if not self.pages:
            return False
        if ev.button == 3:
            self.popup_menu(ev)
            return True
        if ev.button != 1:
            return False
        point = self.hit(ev.x, ev.y)
        self.pressed_link = None
        if ev.type == Gdk.EventType.BUTTON_PRESS and not ev.state & SHIFT:
            link = self.link_at(ev.x, ev.y)
            if link:
                self.pressed_link = (link, ev.x, ev.y)
        if ev.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            self.sel_style = Poppler.SelectionStyle.WORD
        elif ev.type == Gdk.EventType.TRIPLE_BUTTON_PRESS:
            self.sel_style = Poppler.SelectionStyle.LINE
        elif ev.state & SHIFT and self.sel_anchor:
            self.sel_end = point
        else:
            self.sel_style = Poppler.SelectionStyle.GLYPH
            self.sel_anchor = self.sel_end = point
        self.dragging = True
        if not self.autoscroll_id:
            self.autoscroll_id = GLib.timeout_add(40, self.autoscroll)
        self.canvas.queue_draw()
        return True

    def on_motion(self, widget, ev):
        if self.dragging:
            self.sel_end = self.hit(ev.x, ev.y)
            self.canvas.queue_draw()
        elif self.pages:
            self.set_hover_link(self.link_at(ev.x, ev.y))
        return False

    def on_release(self, widget, ev):
        if ev.button == 1 and self.dragging:
            self.dragging = False
            if self.autoscroll_id:
                GLib.source_remove(self.autoscroll_id)
                self.autoscroll_id = 0
            pressed, self.pressed_link = self.pressed_link, None
            # A click (not a drag) on a link follows it.
            if pressed and abs(ev.x - pressed[1]) < 4 and abs(ev.y - pressed[2]) < 4:
                self.sel_anchor = self.sel_end = None
                self.canvas.queue_draw()
                self.activate_link(pressed[0])
                return True
            self.set_primary()
        return False

    def autoscroll(self):
        """While drag-selecting, scroll when the pointer leaves the visible area."""
        win = self.canvas.get_window()
        pointer = self.canvas.get_display().get_default_seat().get_pointer()
        _, x, y, _ = win.get_device_position(pointer)
        h, w = self.canvas.get_allocated_height(), self.canvas.get_allocated_width()
        dy = y if y < 0 else (y - h if y > h else 0)
        dx = x if x < 0 else (x - w if x > w else 0)
        if dx or dy:
            self.vadj.set_value(self.vadj.get_value() + dy)
            self.hadj.set_value(self.hadj.get_value() + dx)
            self.sel_end = self.hit(x + self.hadj.get_value(), y + self.vadj.get_value())
            self.canvas.queue_draw()
        return True

    def on_scroll(self, widget, ev):
        if not ev.state & CTRL:
            return False
        if ev.direction == Gdk.ScrollDirection.UP:
            self.zoom_in()
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            self.zoom_out()
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:
            _, _, dy = ev.get_scroll_deltas()
            if dy:
                self.set_zoom(self.zoom * ZOOM_STEP ** (-dy))
        return True

    def popup_menu(self, ev):
        menu = Gtk.Menu()
        has_sel = bool(self.selected_text())
        for label, callback, sensitive in (("Copy", self.copy, has_sel),
                                           ("Select All", self.select_all, True),
                                           ("Print…", self.print_doc, True)):
            item = Gtk.MenuItem(label=label)
            item.set_sensitive(sensitive)
            item.connect("activate", lambda _, cb=callback: cb())
            menu.append(item)
        menu.show_all()
        menu.attach_to_widget(self.canvas)
        menu.popup_at_pointer(ev)
        self._menu = menu  # keep a reference while shown

    # ------------------------------------------------------------ keyboard
    def on_key(self, widget, ev):
        key = Gdk.keyval_to_lower(ev.keyval)
        entry_focused = isinstance(self.get_focus(), Gtk.Entry)
        if key == Gdk.KEY_F3:
            self.next_match(-1 if ev.state & SHIFT else 1)
            return True
        if ev.state & ALT and key == Gdk.KEY_Left:
            self.go_back()
            return True
        if ev.state & CTRL:
            actions = {
                Gdk.KEY_f: self.show_search,
                Gdk.KEY_g: lambda: self.next_match(-1 if ev.state & SHIFT else 1),
                Gdk.KEY_o: self.open_dialog,
                Gdk.KEY_p: self.print_doc,
                Gdk.KEY_w: self.close,
                Gdk.KEY_q: self.close,
                Gdk.KEY_plus: self.zoom_in, Gdk.KEY_equal: self.zoom_in, Gdk.KEY_KP_Add: self.zoom_in,
                Gdk.KEY_minus: self.zoom_out, Gdk.KEY_KP_Subtract: self.zoom_out,
                Gdk.KEY_0: lambda: self.set_zoom(fit="width"),
                Gdk.KEY_1: lambda: self.set_zoom(1.0),
            }
            if not entry_focused:
                actions[Gdk.KEY_c] = self.copy
                actions[Gdk.KEY_Insert] = self.copy
                actions[Gdk.KEY_a] = self.select_all
            if key in actions:
                actions[key]()
                return True
            if key == Gdk.KEY_Home:
                self.vadj.set_value(0)
                return True
            if key == Gdk.KEY_End:
                self.vadj.set_value(self.vadj.get_upper())
                return True
            return False
        if entry_focused or not self.pages:
            return False
        if key == Gdk.KEY_Escape:
            if self.search_bar.get_search_mode():
                self.search_bar.set_search_mode(False)
                return True
            self.sel_anchor = self.sel_end = None
            self.canvas.queue_draw()
            return True
        page = self.vadj.get_page_size() * 0.9
        line = 48
        steps = {
            Gdk.KEY_Up: (0, -line), Gdk.KEY_Down: (0, line),
            Gdk.KEY_Left: (-line, 0), Gdk.KEY_Right: (line, 0),
            Gdk.KEY_Page_Up: (0, -page), Gdk.KEY_Page_Down: (0, page),
            Gdk.KEY_space: (0, -page if ev.state & SHIFT else page),
            Gdk.KEY_BackSpace: (0, -page),
        }
        if key in steps:
            dx, dy = steps[key]
            self.hadj.set_value(self.hadj.get_value() + dx)
            self.vadj.set_value(self.vadj.get_value() + dy)
            return True
        if key == Gdk.KEY_Home:
            self.vadj.set_value(0)
            return True
        if key == Gdk.KEY_End:
            self.vadj.set_value(self.vadj.get_upper())
            return True
        return False

    # ------------------------------------------------------------ printing
    def print_doc(self):
        if not self.pages:
            return
        op = Gtk.PrintOperation(n_pages=len(self.pages), use_full_page=False,
                                job_name=os.path.basename(self.path or "document"))
        if self.print_settings:
            op.set_print_settings(self.print_settings)
        op.connect("request-page-setup", self.on_print_page_setup)
        op.connect("draw-page", self.on_print_draw_page)
        try:
            result = op.run(Gtk.PrintOperationAction.PRINT_DIALOG, self)
        except GLib.Error as e:
            self.error(f"Printing failed:\n{e.message}")
            return
        if result == Gtk.PrintOperationResult.APPLY:
            self.print_settings = op.get_print_settings()

    def on_print_page_setup(self, op, ctx, i, setup):
        w, h = self.sizes[i]
        setup.set_orientation(Gtk.PageOrientation.LANDSCAPE if w > h else Gtk.PageOrientation.PORTRAIT)

    def on_print_draw_page(self, op, ctx, i):
        """Scale each page to fit the printable area, centred."""
        cr = ctx.get_cairo_context()
        w, h = self.sizes[i]
        cw, ch = ctx.get_width(), ctx.get_height()
        s = min(cw / w, ch / h)
        cr.translate((cw - w * s) / 2, (ch - h * s) / 2)
        cr.scale(s, s)
        self.pages[i].render_for_printing(cr)


def main():
    GLib.set_prgname("mypdf")
    win = Reader(sys.argv[1] if len(sys.argv) > 1 else None)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, Gtk.main_quit)
    Gtk.main()


if __name__ == "__main__":
    main()

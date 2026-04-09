#!/usr/bin/env python3
"""
RNSD Menu Bar App for macOS
Automatically starts rnsd when the app launches and stops it on quit.
Monitors and controls the Reticulum Network Stack Daemon from the menu bar.

Place this script in your Reticulum project folder (the one containing
the .venv created by uv). It will find rnsd inside .venv/bin/.

Install dependencies (in the same venv):
    uv pip install rumps pyobjc-framework-Cocoa

Usage:
    python3 rnsd_menubar.py

To install as a login service:
    bash install.sh
"""

import sys

# ── Tool-mode dispatcher ─────────────────────────────────────────
# When the bundled app is invoked with --rns-tool <name>, run that
# RNS utility's main() and exit instead of starting the menu bar.
# This lets a single PyInstaller bundle act as both the GUI and
# the rns* command-line tools, since `sys.executable` inside a
# bundle points to the app itself (not to a Python interpreter).
if "--rns-tool" in sys.argv:
    idx = sys.argv.index("--rns-tool")
    tool_name = sys.argv[idx + 1]
    # Strip --rns-tool and tool name from argv before calling main()
    sys.argv = [tool_name] + sys.argv[idx + 2:]

    # Force unbuffered stdout/stderr so output reaches the parent immediately
    import io
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding=sys.stdout.encoding, line_buffering=True
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding=sys.stderr.encoding, line_buffering=True
    )

    # PyInstaller strips site.py which normally provides exit/quit builtins.
    # RNS code uses bare exit() so we need to inject them.
    import builtins
    builtins.exit = sys.exit
    builtins.quit = sys.exit

    import importlib
    try:
        mod = importlib.import_module(f"RNS.Utilities.{tool_name}")
        if hasattr(mod, "main"):
            mod.main()
    except SystemExit:
        pass
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    sys.exit(0)

# ── MUST come before any other GUI imports ───────────────────────
# Register as an Accessory app so macOS gives us proper GUI status
# (keyboard focus, window ownership) without showing a Dock icon.
from AppKit import (
    NSApplication, NSApp, NSImage, NSAlert, NSTextField,
    NSAlertFirstButtonReturn, NSFont, NSPopUpButton,
    NSScrollView, NSTextView, NSBezelBorder, NSColor,
    NSForegroundColorAttributeName, NSFontAttributeName, NSLinkAttributeName,
    NSUnderlineStyleAttributeName,
    NSWindow, NSView, NSBackingStoreBuffered, NSPasteboard,
    NSPasteboardTypeString, NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
    NSWindowStyleMaskMiniaturizable, NSButton, NSBezelStyleAccessoryBarAction,
    NSSearchField,
)
from Foundation import (
    NSSize, NSMakeRect, NSObject, NSURL, NSURLRequest, NSURLResponse,
    NSAttributedString, NSMutableAttributedString,
)
from WebKit import WKWebView, WKWebViewConfiguration, WKNavigationAction
NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory

# Install a standard Edit menu so Cmd+C/V/X/A work in WKWebView and text fields.
# Without this, macOS can't route keyboard shortcuts through the responder chain.
from AppKit import NSMenu, NSMenuItem
_edit_menu = NSMenu.alloc().initWithTitle_("Edit")
_edit_menu.addItemWithTitle_action_keyEquivalent_("Cut", "cut:", "x")
_edit_menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
_edit_menu.addItemWithTitle_action_keyEquivalent_("Paste", "paste:", "v")
_edit_menu.addItemWithTitle_action_keyEquivalent_("Select All", "selectAll:", "a")
_edit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
_edit_item.setSubmenu_(_edit_menu)
_main_menu = NSApp.mainMenu()
if _main_menu is None:
    _main_menu = NSMenu.alloc().initWithTitle_("MainMenu")
    NSApp.setMainMenu_(_main_menu)
_main_menu.addItem_(_edit_item)

import rumps

# Patch rumps' NSApp delegate to prevent macOS from terminating the app
# when the last window closes. With accessory activation policy, macOS
# calls applicationShouldTerminate: (not just ...AfterLastWindowClosed).
import objc as _objc

_allow_quit = False  # Set to True when user explicitly quits

def _should_terminate(self, sender):
    if _allow_quit:
        return 1  # NSTerminateNow
    return 0  # NSTerminateCancel

_should_terminate_sel = _objc.selector(
    _should_terminate,
    selector=b"applicationShouldTerminate:",
    signature=b"Q@:@",
)
_objc.classAddMethod(
    rumps.rumps.NSApp,
    b"applicationShouldTerminate:",
    _should_terminate_sel,
)
import subprocess
import os
import signal
import shutil
import atexit
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from PyObjCTools import AppHelper

# ── Locate binaries ─────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── App icon ────────────────────────────────────────────────────

ICON_PATH = os.path.join(SCRIPT_DIR, "rns_icon.png")
MENU_ICON_PATH = os.path.join(SCRIPT_DIR, "rns_menu_icon.png")
APP_ICON = None


def _load_icon():
    global APP_ICON
    if os.path.isfile(ICON_PATH):
        try:
            APP_ICON = NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
        except Exception:
            pass


_load_icon()

SEARCH_DIRS = [
    os.path.join(SCRIPT_DIR, ".venv", "bin"),
    os.path.join(SCRIPT_DIR, "venv", "bin"),
    SCRIPT_DIR,
]

if os.environ.get("VIRTUAL_ENV"):
    SEARCH_DIRS.append(os.path.join(os.environ["VIRTUAL_ENV"], "bin"))


def find_binary(name):
    for d in SEARCH_DIRS:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name)
    if found:
        return found
    return None


# Module fallbacks for when RNS is bundled (e.g. inside a PyInstaller .app)
# but the entry-point scripts aren't on disk. We invoke the modules directly.

RNS_MODULES = {
    "rnsd":     "RNS.Utilities.rnsd",
    "rnstatus": "RNS.Utilities.rnstatus",
    "rnid":     "RNS.Utilities.rnid",
    "rnpath":   "RNS.Utilities.rnpath",
    "rnprobe":  "RNS.Utilities.rnprobe",
}


def _find_rns_module(name):
    """Try common RNS.Utilities module name variations."""
    module = RNS_MODULES.get(name)
    if not module:
        return None
    try:
        import importlib
        importlib.import_module(module)
        return module
    except ImportError:
        # Try alternate casings as fallback
        base = name[2:]  # strip "rn"
        for variant in (
            f"RNS.Utilities.{name}",
            f"RNS.Utilities.{base}",
            f"RNS.Utilities.{base.capitalize()}",
            f"RNS.Utilities.{name.capitalize()}",
        ):
            try:
                importlib.import_module(variant)
                return variant
            except ImportError:
                continue
    return None


def get_command(name):
    """Return command list for invoking an RNS utility.
    Tries the standalone binary first, falls back to module invocation.
    When running inside a PyInstaller bundle, uses --rns-tool to re-invoke
    ourselves in tool-mode (since sys.executable is the bundled app)."""
    binary = find_binary(name)
    if binary:
        return [binary]
    module = _find_rns_module(name)
    if not module:
        return None
    if getattr(sys, "frozen", False):
        # Running inside a PyInstaller bundle: re-invoke ourselves
        return [sys.executable, "--rns-tool", name]
    else:
        # Normal Python: use -m module
        return [sys.executable, "-m", module]


BINS = {
    "rnsd":     get_command("rnsd"),
    "rnstatus": get_command("rnstatus"),
    "rnid":     get_command("rnid"),
    "rnpath":   get_command("rnpath"),
    "rnprobe":  get_command("rnprobe"),
}


# ── GUI helpers ──────────────────────────────────────────────────

def _focus():
    """Force this app to the foreground."""
    NSApp.activateIgnoringOtherApps_(True)


def _set_menu_icon(menu_item, sf_symbol_name, size=16):
    """Apply an SF Symbol icon to a rumps MenuItem.
    Uses NSImage's system symbol API (macOS 11+). Silently no-ops on failure."""
    try:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            sf_symbol_name, None
        )
        if img is None:
            return
        img.setSize_(NSSize(size, size))
        # Treat as template so it adapts to light/dark menu themes
        img.setTemplate_(True)
        menu_item._menuitem.setImage_(img)
    except Exception:
        pass


def show_alert(title, message, ok="OK", width=520, max_height=500,
               monospace=False, attributed_message=None, use_textview=False):
    """Show an alert dialog with the Reticulum icon.
    Uses a scrollable text view if the content would be too tall.
    Pass monospace=True to use a fixed-width font for technical output.
    Pass attributed_message=NSAttributedString for styled (colored) text.
    Pass use_textview=True to force a scrollable NSTextView (needed for links)."""
    _focus()
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.addButtonWithTitle_(ok)
    if APP_ICON:
        alert.setIcon_(APP_ICON)

    if monospace:
        font = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)
    else:
        font = NSFont.systemFontOfSize_(12.0)

    # Use the plain message for measuring even if we have an attributed version
    measure_str = message if not attributed_message else attributed_message.string()

    label = NSTextField.alloc().initWithFrame_(((0, 0), (width, 10)))
    label.setStringValue_(measure_str)
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(True)
    label.setFont_(font)
    label.cell().setWraps_(True)
    bounds = ((0, 0), (width, 100000))
    size = label.cell().cellSizeForBounds_(bounds)

    if size.height <= max_height and not use_textview:
        # Short enough — use the simple label
        label.setFrame_(((0, 0), (width, size.height)))
        if attributed_message is not None:
            label.setAttributedStringValue_(attributed_message)
        alert.setAccessoryView_(label)
    else:
        # Too tall or forced — use a scrollable text view
        view_height = min(size.height + 16, max_height) if use_textview else max_height
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width, view_height)
        )
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutohidesScrollers_(False)

        text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width, max_height)
        )
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setFont_(font)
        text_view.setTextContainerInset_(NSSize(8, 8))
        if attributed_message is not None:
            text_view.textStorage().setAttributedString_(attributed_message)
        else:
            text_view.setString_(message)

        scroll.setDocumentView_(text_view)
        alert.setAccessoryView_(scroll)

    alert.runModal()


def show_prompt(title, message, ok="OK", cancel="Cancel",
                default_text="", width=380):
    """Show an input dialog with the Reticulum icon. Returns text or None."""
    _focus()
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_(cancel)
    if APP_ICON:
        alert.setIcon_(APP_ICON)

    text_field = NSTextField.alloc().initWithFrame_(((0, 0), (width, 24)))
    text_field.setStringValue_(default_text)
    alert.setAccessoryView_(text_field)
    alert.window().setInitialFirstResponder_(text_field)

    result = alert.runModal()
    if result == NSAlertFirstButtonReturn:
        return text_field.stringValue().strip()
    return None


def show_dropdown(title, message, options, ok="OK", cancel="Cancel", width=380):
    """Show a dialog with a dropdown selector. Returns selected option or None."""
    _focus()
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_(cancel)
    if APP_ICON:
        alert.setIcon_(APP_ICON)

    popup = NSPopUpButton.alloc().initWithFrame_(((0, 0), (width, 26)))
    for opt in options:
        popup.addItemWithTitle_(opt)
    alert.setAccessoryView_(popup)

    result = alert.runModal()
    if result == NSAlertFirstButtonReturn:
        return popup.titleOfSelectedItem()
    return None


def run_and_show(args, title="Output", timeout=15, post_process=None):
    """Run a command and show its output in a dialog."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if post_process:
            output = post_process(output)
        output = output.strip() or "No output"
        show_alert(title, output, ok="Close", monospace=True)
    except FileNotFoundError:
        show_alert("Error", f"Command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        show_alert("Timeout", f"Command timed out after {timeout}s.")
    except Exception as e:
        show_alert("Error", str(e))


def require_bin(name):
    if not BINS.get(name):
        show_alert("Error", f"{name} not found.")
        return False
    return True


# ── Nodebook ─────────────────────────────────────────────────────

import json
import time as _time

PHONEBOOK_PATH = os.path.expanduser("~/.reticulum/menubar_phonebook.json")


def is_valid_name(s):
    """Heuristic: does this string look like a real name vs binary garbage?"""
    if not s:
        return False
    s = s.strip()
    if len(s) < 3 or len(s) > 64:
        return False
    # All characters must be from a conservative allowed set
    for c in s:
        if not (c.isalnum() or c.isspace() or c in "_-.()[]!?/'#:&,+"):
            return False
    # Must have at least 2 letters (rejects things like "i7(" or "8.5")
    letter_count = sum(1 for c in s if c.isalpha())
    return letter_count >= 2


class Phonebook:
    """JSON-backed contact store keyed by destination hash.
    Auto-populated from RNS announces; persisted across restarts."""

    def __init__(self, path=PHONEBOOK_PATH):
        self.path = path
        self.contacts = {}  # hash -> {"name": str, "type": str}
        self._lock = threading.Lock()
        self.load()

    def load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            raw = data.get("contacts", {})
            self.contacts = {}
            for key, value in raw.items():
                # Migrate from older formats
                if isinstance(value, str):
                    # Legacy v1: {name: hash_string}
                    if is_valid_name(key):
                        self.contacts[value] = {"name": key, "type": "Unknown"}
                elif isinstance(value, dict):
                    if "name" in value and "type" in value:
                        # Current format: {hash: {name, type}}
                        self.contacts[key] = value
                    elif "hash" in value:
                        # Intermediate v2: {name: {hash, type}}
                        if is_valid_name(key):
                            self.contacts[value["hash"]] = {
                                "name": key,
                                "type": value.get("type", "Unknown"),
                            }
        except Exception:
            self.contacts = {}

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            with open(self.path, "w") as f:
                json.dump({"contacts": self.contacts}, f, indent=2)
        except Exception:
            pass

    def add_auto(self, name, hash_, type_="Unknown"):
        """Add or update a contact from an announce. Thread-safe.
        - Rejects names that look like binary garbage
        - One entry per hash; the first valid name we see is kept
        - Type can be upgraded from Unknown to a known type
        - Always updates the announce timestamp"""
        if not is_valid_name(name):
            return False
        now = _time.time()
        with self._lock:
            existing = self.contacts.get(hash_)
            if existing:
                changed = False
                # Upgrade Unknown type if we now have a better one
                if existing.get("type") == "Unknown" and type_ != "Unknown":
                    existing["type"] = type_
                    changed = True
                # Always update the last-seen timestamp
                existing["seen"] = now
                self.save()
                return changed
            self.contacts[hash_] = {"name": name, "type": type_, "seen": now}
            self.save()
            return True

    def clear(self):
        with self._lock:
            self.contacts = {}
            self.save()

    def names(self):
        """Return sorted list of contact names for dropdowns."""
        with self._lock:
            return sorted(
                (entry["name"] for entry in self.contacts.values()),
                key=str.lower,
            )

    def get(self, name):
        """Return the hash for a contact name."""
        with self._lock:
            for hash_, entry in self.contacts.items():
                if entry["name"] == name:
                    return hash_
            return None

    def grouped(self):
        """Return contacts grouped by type: {type: [(name, hash, seen), ...]}.
        Each group is sorted by announce time, latest first."""
        with self._lock:
            groups = {}
            for hash_, entry in self.contacts.items():
                t = entry.get("type", "Unknown")
                seen = entry.get("seen", 0)
                groups.setdefault(t, []).append((entry["name"], hash_, seen))
            for t in groups:
                groups[t].sort(key=lambda x: x[2], reverse=True)
            return groups

    def is_empty(self):
        with self._lock:
            return not self.contacts


PHONEBOOK = Phonebook()


def time_ago(ts):
    """Human-readable time-since string for an announce timestamp."""
    if not ts:
        return ""
    delta = _time.time() - ts
    if delta < 60:
        return "just now"
    elif delta < 3600:
        m = int(delta // 60)
        return f"{m}m ago"
    elif delta < 86400:
        h = int(delta // 3600)
        return f"{h}h ago"
    else:
        d = int(delta // 86400)
        return f"{d}d ago"


_rns_instance = None  # holds the RNS.Reticulum instance for cleanup


def shutdown_rns():
    """Cleanly tear down the RNS client connection so the LocalInterface
    is removed from the shared instance."""
    global _rns_instance
    if _rns_instance is not None:
        try:
            import RNS
            RNS.Reticulum.exit_handler()
        except Exception:
            pass
        _rns_instance = None


def init_announce_listener():
    """Initialize RNS and register announce handlers.
    MUST be called from the main thread because RNS.Reticulum() installs
    signal handlers, which Python only allows from the main thread.
    Once registered, RNS's own threads invoke the handlers when announces arrive."""
    global _rns_instance
    try:
        import RNS
    except ImportError:
        return False

    try:
        _rns_instance = RNS.Reticulum(loglevel=0)
    except Exception:
        return False

    def decode_name(app_data):
        if not app_data:
            return None
        try:
            name = app_data.decode("utf-8", errors="ignore").strip()
            name = "".join(c for c in name if c.isprintable())
            return name[:64] if name else None
        except Exception:
            return None

    def make_handler(aspect, type_label):
        class Handler:
            aspect_filter = aspect
            def received_announce(self, destination_hash, announced_identity, app_data):
                try:
                    name = decode_name(app_data)
                    if name:
                        PHONEBOOK.add_auto(name, destination_hash.hex(), type_label)
                except Exception:
                    pass
        return Handler()

    try:
        # Filtered handlers tag the contact type
        RNS.Transport.register_announce_handler(
            make_handler("lxmf.delivery", "LXMF")
        )
        RNS.Transport.register_announce_handler(
            make_handler("nomadnetwork.node", "NomadNet")
        )
        # Catch-all so we still capture other aspects (tagged Unknown)
        RNS.Transport.register_announce_handler(
            make_handler(None, "Unknown")
        )
        return True
    except Exception:
        return False


# ── Nomadnet page fetch ───────────────────────────────────────

def _fetch_raw(dest_hash_hex, path="/page/index.mu", timeout=15):
    """Fetch raw bytes from a NomadNet node via the shared rnsd instance.
    Works for both /page/ (micron) and /file/ (binary download) paths."""
    import RNS

    dest_hash = bytes.fromhex(dest_hash_hex)

    # Ensure path is known
    if not RNS.Transport.has_path(dest_hash):
        RNS.Transport.request_path(dest_hash)
        # Wait for path resolution
        path_timeout = timeout
        import time
        start = time.time()
        while not RNS.Transport.has_path(dest_hash):
            time.sleep(0.2)
            if time.time() - start > path_timeout:
                raise TimeoutError(f"Could not resolve path to {dest_hash_hex}")

    identity = RNS.Identity.recall(dest_hash)
    if identity is None:
        raise ValueError(f"Could not recall identity for {dest_hash_hex}")

    destination = RNS.Destination(
        identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "nomadnetwork", "node",
    )

    result_event = threading.Event()
    result_data = {"response": None, "error": None}

    def link_established(link):
        link.request(
            path,
            data=None,
            response_callback=response_received,
            failed_callback=request_failed,
        )

    def link_closed(link):
        if result_data["response"] is None and result_data["error"] is None:
            result_data["error"] = "Link closed before response"
            result_event.set()

    def response_received(request_receipt):
        result_data["response"] = request_receipt.response
        result_event.set()

    def request_failed(request_receipt):
        result_data["error"] = "Page request failed"
        result_event.set()

    link = RNS.Link(destination, established_callback=link_established,
                     closed_callback=link_closed)

    if not result_event.wait(timeout=timeout):
        try:
            link.teardown()
        except Exception:
            pass
        raise TimeoutError(f"Timed out fetching {path} from {dest_hash_hex}")

    try:
        link.teardown()
    except Exception:
        pass

    if result_data["error"]:
        raise RuntimeError(result_data["error"])

    return result_data["response"]


def fetch_page(dest_hash_hex, path="/page/index.mu", timeout=15):
    """Fetch a Nomadnet page (micron markup) as a string."""
    response = _fetch_raw(dest_hash_hex, path, timeout)
    if isinstance(response, bytes):
        return response.decode("utf-8", errors="replace")
    elif isinstance(response, str):
        return response
    else:
        return str(response) if response is not None else ""


def fetch_file(dest_hash_hex, path, timeout=30):
    """Fetch a file from a NomadNet node. Returns raw bytes."""
    response = _fetch_raw(dest_hash_hex, path, timeout)
    if isinstance(response, bytes):
        return response
    elif isinstance(response, str):
        return response.encode("utf-8")
    else:
        return b""


# ── Loopback HTTP bridge ─────────────────────────────────────

WEB_DIR = os.path.join(SCRIPT_DIR, "assets", "web")

# URL queue for requests arriving before RNS is ready
_pending_urls = []
_bridge_port = None


class _BridgeHandler(BaseHTTPRequestHandler):
    """Handles requests from the renderer page."""

    def log_message(self, format, *args):
        pass  # silence access logs

    def _check_host(self):
        host = self.headers.get("Host", "")
        allowed = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if host not in allowed:
            self.send_error(403, "Forbidden")
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_file("renderer.html", "text/html; charset=utf-8")
        elif path == "/fetch":
            if not self._check_host():
                return
            self._handle_fetch(query)
        elif path.endswith(".js"):
            fname = os.path.basename(path)
            safe_path = os.path.join(WEB_DIR, fname)
            if os.path.isfile(safe_path):
                self._serve_file(fname, "application/javascript; charset=utf-8")
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def _serve_file(self, filename, content_type):
        filepath = os.path.join(WEB_DIR, filename)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _handle_fetch(self, query):
        dest = query.get("dest", [None])[0]
        path = query.get("path", ["/page/index.mu"])[0]

        if not dest or not all(c in "0123456789abcdefABCDEF" for c in dest):
            self._json_error(400, "Invalid destination hash")
            return

        if not path.startswith("/page/"):
            path = "/page/" + path.lstrip("/")

        try:
            micron = fetch_page(dest, path, timeout=30)
            data = micron.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except TimeoutError as e:
            self._json_error(504, str(e))
        except ValueError as e:
            self._json_error(404, str(e))
        except Exception as e:
            self._json_error(502, str(e))

    def _json_error(self, code, message):
        import json
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_bridge_server():
    """Start the loopback HTTP bridge on an ephemeral port. Returns the port."""
    global _bridge_port
    server = HTTPServer(("127.0.0.1", 0), _BridgeHandler)
    _bridge_port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return _bridge_port


def open_rns_url(url):
    """Route an rns:// URL to the native WKWebView browser window."""
    print(f"[open_rns_url] called with: {url}, bridge_port={_bridge_port}", flush=True)
    if _bridge_port is None:
        _pending_urls.append(url)
        print("[open_rns_url] Bridge not ready, queued", flush=True)
        return
    try:
        _init_nomad_browser()
        print(f"[open_rns_url] Browser initialized: {_nomad_browser}", flush=True)
        _nomad_browser.navigate(url)
        print("[open_rns_url] navigate() returned", flush=True)
    except Exception as e:
        import traceback
        print(f"[open_rns_url] ERROR: {e}", flush=True)
        traceback.print_exc()


def _flush_pending_urls():
    """Open any URLs that arrived before the bridge was ready."""
    while _pending_urls:
        open_rns_url(_pending_urls.pop(0))


# ── Native WKWebView browser ────────────────────────────────

import objc


def _parse_rns_url(url):
    """Parse rns://<hash>[/page/<path>|/file/<path>] into (dest_hash, path) or None."""
    import re
    m = re.match(r'^rns://([0-9a-fA-F]+)(/.*)?$', url)
    if not m:
        return None
    dest = m.group(1)
    path = m.group(2) or "/page/index.mu"
    if path in ("/", ""):
        path = "/page/index.mu"
    # Preserve /file/ paths as-is; default others to /page/
    if not path.startswith("/page/") and not path.startswith("/file/"):
        path = "/page/" + path.lstrip("/")
    return dest, path


def _build_renderer_html(micron_source, rns_url, bridge_port):
    """Build a self-contained HTML page that renders micron client-side.
    Inlines the JS dependencies to avoid cross-origin issues in WKWebView."""
    import html as html_mod
    # Embed micron source as a JS string literal instead of in a text/plain
    # script tag, to avoid any HTML entity issues. Escape for JS embedding.
    import json
    escaped_js = json.dumps(micron_source)

    # Read JS files and inline them to avoid WKWebView security restrictions
    # on loading http:// resources from loadHTMLString pages
    purify_js = ""
    micron_js = ""
    try:
        with open(os.path.join(WEB_DIR, "purify.min.js"), "r") as f:
            purify_js = f.read()
    except Exception:
        pass
    try:
        with open(os.path.join(WEB_DIR, "micron-parser.js"), "r") as f:
            micron_js = f.read()
    except Exception:
        pass

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_mod.escape(rns_url)}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "SF Mono", "Menlo", "Monaco", "Courier New", monospace;
    font-size: 14px;
    background: #1a1a1a;
    color: #ddd;
    padding: 1em;
    min-height: 100vh;
  }}
  #content a {{ color: #6af; }}
  #content a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div id="content"></div>
<script>{purify_js}</script>
<script>{micron_js}</script>
<script>
(function() {{
  "use strict";
  var currentDest = "{html_mod.escape(rns_url.split('//')[1].split('/')[0]) if '//' in rns_url else ''}";
  var micron = {escaped_js};
  var isDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var parser = new MicronParser(isDark !== false, true);
  var html = parser.convertMicronToHtml(micron);
  var el = document.getElementById("content");
  el.innerHTML = DOMPurify.sanitize(html, {{
    USE_PROFILES: {{ html: true }},
    ADD_ATTR: ["style", "class", "data-action", "data-destination", "data-fields"]
  }});
  var colors = parser.parseHeaderTags(micron);
  if (colors.bg && colors.bg !== "default") document.body.style.backgroundColor = "#" + colors.bg;
  if (colors.fg && colors.fg !== "default") document.body.style.color = "#" + colors.fg;

  // Intercept clicks on nomadnetwork links (data-action="openNode")
  document.addEventListener("click", function(e) {{
    var link = e.target.closest("[data-action=openNode]");
    if (!link) return;
    e.preventDefault();
    e.stopPropagation();
    var dest = link.getAttribute("data-destination") || "";
    // dest is like ":/page/path" (same node) or "<hash>/page/path"
    if (dest.startsWith(":")) dest = dest.substring(1);
    if (dest.startsWith("/") || dest === "") dest = currentDest + dest;
    if (dest.indexOf("/") === -1) dest = dest + "/page/index.mu";
    if (!dest.startsWith("/")) {{
      var rnsUrl = "rns://" + dest;
      // Use rnsnav:// scheme to trigger navigation via the delegate
      // (rns:// goes to the scheme handler, bypassing decidePolicyFor)
      window.location.href = "rnsnav://" + dest;
    }}
  }});
}})();
</script>
</body>
</html>'''


def _build_loading_html(rns_url):
    """Build a loading page shown while fetching."""
    import html as html_mod
    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Loading...</title>
<style>
  body {{
    font-family: "SF Mono", monospace; font-size: 14px;
    background: #1a1a1a; color: #888;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    min-height: 100vh; margin: 0;
  }}
  .spinner {{
    width: 28px; height: 28px; border: 3px solid #333;
    border-top-color: #888; border-radius: 50%;
    animation: spin 0.8s linear infinite; margin-bottom: 1em;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .url {{ color: #6af; font-size: 12px; margin-top: 0.5em; }}
</style></head><body>
<div class="spinner"></div>
<div>Fetching page...</div>
<div class="url">{html_mod.escape(rns_url)}</div>
</body></html>'''


def _build_error_html(rns_url, error_msg):
    """Build an error page."""
    import html as html_mod
    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Error</title>
<style>
  body {{
    font-family: "SF Mono", monospace; font-size: 14px;
    background: #1a1a1a; color: #ddd;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    min-height: 100vh; margin: 0;
  }}
  h2 {{ color: #e55; margin-bottom: 0.5em; }}
  .details {{ color: #999; font-size: 13px; margin-top: 0.5em; }}
</style></head><body>
<h2>Page Load Failed</h2>
<div>{html_mod.escape(error_msg)}</div>
<div class="details">{html_mod.escape(rns_url)}</div>
</body></html>'''


class RNSSchemeHandler(NSObject):
    """WKURLSchemeHandler for rns:// URLs in WKWebView.
    Navigation is handled by NomadnetBrowserWindow.navigate() via the
    navigation delegate. This handler is a required fallback that returns
    a loading page if WKWebView resolves an rns:// URL directly."""

    def webView_startURLSchemeTask_(self, webView, task):
        url_str = task.request().URL().absoluteString()
        html = _build_loading_html(url_str)
        data = html.encode("utf-8")
        url = NSURL.URLWithString_(url_str)
        response = NSURLResponse.alloc().initWithURL_MIMEType_expectedContentLength_textEncodingName_(
            url, "text/html", len(data), "utf-8"
        )
        try:
            task.didReceiveResponse_(response)
            from Foundation import NSData
            task.didReceiveData_(NSData.dataWithBytes_length_(data, len(data)))
            task.didFinish()
        except Exception:
            pass

    def webView_stopURLSchemeTask_(self, webView, urlSchemeTask):
        pass


# Map WKWebView id() → NomadnetBrowserWindow for back-references
_webview_to_browser = {}


class _NavigationDelegate(NSObject):
    """WKNavigationDelegate that intercepts rns:// link clicks."""

    def webView_decidePolicyForNavigationAction_decisionHandler_(
        self, webView, navigationAction, decisionHandler
    ):
        url = navigationAction.request().URL()
        url_str = url.absoluteString() if url else None

        if url and url.scheme() in ("rns", "rnsnav"):
            # Cancel the default navigation, route through our handler
            decisionHandler(0)  # WKNavigationActionPolicyCancel
            # Normalize rnsnav:// back to rns://
            if url.scheme() == "rnsnav":
                url_str = "rns://" + url_str[len("rnsnav://"):]
            browser = _webview_to_browser.get(id(webView))
            if browser:
                browser.navigate(url_str)
        else:
            decisionHandler(1)  # WKNavigationActionPolicyAllow


class NomadnetBrowserWindow:
    """Singleton native browser window with WKWebView and rns:// URL bar."""

    _instance = None

    def __init__(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(200, 200, 800, 600),
            style,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("Nomadnet")
        self._window.setReleasedWhenClosed_(False)
        self._window.setMinSize_(NSSize(400, 300))
        _make_window_hide_on_close(self._window)
        if APP_ICON:
            self._window.setRepresentedURL_(NSURL.URLWithString_("rns://"))

        # Navigation history
        self._history = []
        self._forward_history = []

        # Container view
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 600))

        # Back button
        self._back_btn = NSButton.alloc().initWithFrame_(NSMakeRect(8, 600 - 30, 28, 24))
        self._back_btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
        self._back_btn.setTitle_("◀")
        self._back_btn.setFont_(NSFont.systemFontOfSize_(12.0))
        self._back_btn.setEnabled_(False)
        self._back_delegate = _BackButtonDelegate.alloc().initWithBrowser_(self)
        self._back_btn.setTarget_(self._back_delegate)
        self._back_btn.setAction_(b"backAction:")
        content.addSubview_(self._back_btn)

        # Forward button
        self._fwd_btn = NSButton.alloc().initWithFrame_(NSMakeRect(36, 600 - 30, 28, 24))
        self._fwd_btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
        self._fwd_btn.setTitle_("▶")
        self._fwd_btn.setFont_(NSFont.systemFontOfSize_(12.0))
        self._fwd_btn.setEnabled_(False)
        self._fwd_delegate = _ForwardButtonDelegate.alloc().initWithBrowser_(self)
        self._fwd_btn.setTarget_(self._fwd_delegate)
        self._fwd_btn.setAction_(b"forwardAction:")
        content.addSubview_(self._fwd_btn)

        # URL bar (shifted right to make room for nav buttons)
        self._url_bar = NSTextField.alloc().initWithFrame_(NSMakeRect(68, 600 - 30, 724, 24))
        self._url_bar.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
        self._url_bar.setEditable_(True)
        self._url_bar.setSelectable_(True)
        self._url_bar.setPlaceholderString_("rns://destination_hash/page/path")
        self._url_bar_delegate = _URLBarDelegate.alloc().initWithBrowser_(self)
        self._url_bar.setTarget_(self._url_bar_delegate)
        self._url_bar.setAction_(b"urlBarAction:")
        content.addSubview_(self._url_bar)

        # Configure WKWebView with custom scheme handler
        config = WKWebViewConfiguration.alloc().init()
        self._scheme_handler = RNSSchemeHandler.alloc().init()
        config.setURLSchemeHandler_forURLScheme_(self._scheme_handler, "rns")

        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, 800, 600 - 36), config
        )
        # Store back-reference for navigation delegate lookups
        _webview_to_browser[id(self._webview)] = self

        self._nav_delegate = _NavigationDelegate.alloc().init()
        self._webview.setNavigationDelegate_(self._nav_delegate)

        content.addSubview_(self._webview)
        self._window.setContentView_(content)

        # Handle window resize
        from Foundation import NSNotificationCenter
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            _WindowResizeObserver.alloc().initWithBrowser_(self),
            b"windowDidResize:",
            "NSWindowDidResizeNotification",
            self._window,
        )

        self._current_url = None

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def go_back(self):
        """Navigate back in history."""
        if not self._history:
            return
        self._forward_history.append(self._current_url)
        self._fwd_btn.setEnabled_(True)
        url = self._history.pop()
        self._back_btn.setEnabled_(len(self._history) > 0)
        self._navigate_internal(url)

    def go_forward(self):
        """Navigate forward in history."""
        if not self._forward_history:
            return
        self._history.append(self._current_url)
        self._back_btn.setEnabled_(True)
        url = self._forward_history.pop()
        self._fwd_btn.setEnabled_(len(self._forward_history) > 0)
        self._navigate_internal(url)

    def navigate(self, rns_url):
        """Navigate to an rns:// URL, pushing current page to history."""
        if self._current_url:
            self._history.append(self._current_url)
            self._back_btn.setEnabled_(True)
        # Clear forward history on new navigation
        self._forward_history.clear()
        self._fwd_btn.setEnabled_(False)
        self._navigate_internal(rns_url)

    def _navigate_internal(self, rns_url):
        """Navigate without pushing to history."""
        self._current_url = rns_url
        self._url_bar.setStringValue_(rns_url)
        self._window.setTitle_(f"Nomadnet — {rns_url}")

        # Show loading page immediately via loadHTMLString
        loading_html = _build_loading_html(rns_url)
        base_url = NSURL.URLWithString_(f"http://127.0.0.1:{_bridge_port}/")
        self._webview.loadHTMLString_baseURL_(loading_html, base_url)

        # Fetch in background
        def do_fetch():
            parsed = _parse_rns_url(rns_url)
            if not parsed:
                html = _build_error_html(rns_url, "Invalid rns:// URL")
                def deliver():
                    if self._current_url == rns_url:
                        self._load_html_preserving_url(html, rns_url)
                AppHelper.callAfter(deliver)
                return

            dest, path = parsed

            if path.startswith("/file/"):
                # File download
                try:
                    data = fetch_file(dest, path, timeout=60)
                    filename = path.split("/")[-1] or "download"
                    def save():
                        if self._current_url == rns_url:
                            self._save_file(filename, data, rns_url)
                    AppHelper.callAfter(save)
                except Exception as e:
                    html = _build_error_html(rns_url, str(e))
                    def deliver():
                        if self._current_url == rns_url:
                            self._load_html_preserving_url(html, rns_url)
                    AppHelper.callAfter(deliver)
            else:
                # Page fetch
                try:
                    micron = fetch_page(dest, path, timeout=30)
                    html = _build_renderer_html(micron, rns_url, _bridge_port)
                except Exception as e:
                    html = _build_error_html(rns_url, str(e))
                def deliver():
                    if self._current_url == rns_url:
                        self._load_html_preserving_url(html, rns_url)
                AppHelper.callAfter(deliver)

        threading.Thread(target=do_fetch, daemon=True).start()

        # Show and focus the window
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
        self._window.orderFrontRegardless()

    def _load_html_preserving_url(self, html, rns_url):
        """Load HTML content while keeping the rns:// URL in the URL bar."""
        base_url = NSURL.URLWithString_(f"http://127.0.0.1:{_bridge_port}/")
        self._webview.loadHTMLString_baseURL_(html, base_url)
        self._url_bar.setStringValue_(rns_url)

    def _save_file(self, filename, data, rns_url):
        """Present a Save panel and write downloaded file bytes to disk."""
        from AppKit import NSSavePanel
        panel = NSSavePanel.savePanel()
        panel.setNameFieldStringValue_(filename)
        panel.setTitle_("Save Downloaded File")
        result = panel.runModal()
        if result == 1:  # NSModalResponseOK
            save_path = panel.URL().path()
            try:
                with open(save_path, "wb") as f:
                    f.write(data)
                rumps.notification("RNSD", "File Downloaded",
                                   f"Saved to {os.path.basename(save_path)}")
            except Exception as e:
                show_alert("Download Error", str(e))
        # Restore the previous page in the webview
        self._url_bar.setStringValue_(rns_url)

    def _relayout(self):
        """Reposition subviews after window resize."""
        frame = self._window.contentView().frame()
        w, h = frame.size.width, frame.size.height
        self._back_btn.setFrame_(NSMakeRect(8, h - 30, 28, 24))
        self._fwd_btn.setFrame_(NSMakeRect(36, h - 30, 28, 24))
        self._url_bar.setFrame_(NSMakeRect(68, h - 30, w - 76, 24))
        self._webview.setFrame_(NSMakeRect(0, 0, w, h - 36))

    def _url_bar_submitted(self):
        """Called when user presses Enter in the URL bar."""
        url = self._url_bar.stringValue().strip()
        if url and url.startswith("rns://") and len(url) > 6:
            self.navigate(url)


class _BackButtonDelegate(NSObject):
    _browser = objc.ivar()

    def initWithBrowser_(self, browser):
        self = objc.super(_BackButtonDelegate, self).init()
        if self is None:
            return None
        self._browser = browser
        return self

    def backAction_(self, sender):
        if self._browser:
            self._browser.go_back()


class _ForwardButtonDelegate(NSObject):
    _browser = objc.ivar()

    def initWithBrowser_(self, browser):
        self = objc.super(_ForwardButtonDelegate, self).init()
        if self is None:
            return None
        self._browser = browser
        return self

    def forwardAction_(self, sender):
        if self._browser:
            self._browser.go_forward()


class _URLBarDelegate(NSObject):
    _browser = objc.ivar()

    def initWithBrowser_(self, browser):
        self = objc.super(_URLBarDelegate, self).init()
        if self is None:
            return None
        self._browser = browser
        return self

    def urlBarAction_(self, sender):
        if self._browser:
            self._browser._url_bar_submitted()


class _WindowResizeObserver(NSObject):
    _browser = objc.ivar()

    def initWithBrowser_(self, browser):
        self = objc.super(_WindowResizeObserver, self).init()
        if self is None:
            return None
        self._browser = browser
        return self

    def windowDidResize_(self, notification):
        if self._browser:
            self._browser._relayout()


# Singleton instance — created lazily
_nomad_browser = None


def _init_nomad_browser():
    global _nomad_browser
    if _nomad_browser is None:
        _nomad_browser = NomadnetBrowserWindow.shared()


# ── Apple Event URL handler ──────────────────────────────────

_kAEGetURL = 0x4755524c  # fourcc('GURL')
_keyDirectObject = 0x2d2d2d2d  # fourcc('----')


class _URLHandlerTarget(NSObject):
    def handleGetURL_withReplyEvent_(self, event, reply_event):
        print(f"[URL Handler] Apple Event received: {event}", flush=True)
        url_desc = event.paramDescriptorForKeyword_(_keyDirectObject)
        if url_desc is None:
            print("[URL Handler] No URL descriptor found", flush=True)
            return
        url = url_desc.stringValue()
        print(f"[URL Handler] URL: {url}", flush=True)
        if url and url.startswith("rns://"):
            # Call directly — we're already on the main thread
            open_rns_url(url)


def register_url_handler():
    """Register this app as the handler for rns:// URLs via Apple Events.
    Must be called before the NSApplication event loop starts."""
    from Foundation import NSAppleEventManager

    em = NSAppleEventManager.sharedAppleEventManager()
    handler = _URLHandlerTarget.alloc().init()
    # Store a reference so it doesn't get garbage collected
    register_url_handler._handler = handler
    em.setEventHandler_andSelector_forEventClass_andEventID_(
        handler,
        "handleGetURL:withReplyEvent:",
        _kAEGetURL,  # kInternetEventClass
        _kAEGetURL,  # kAEGetURL
    )
    print("[URL Handler] Registered Apple Event handler for rns://", flush=True)


def show_two_field_prompt(title, message, label1, label2,
                          ok="OK", cancel="Cancel", width=380):
    """Show a dialog with two stacked text fields. Returns (val1, val2) or None."""
    _focus()
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_(cancel)
    if APP_ICON:
        alert.setIcon_(APP_ICON)

    from AppKit import NSView
    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))

    lbl1 = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 76, width, 18))
    lbl1.setStringValue_(label1)
    lbl1.setBezeled_(False)
    lbl1.setDrawsBackground_(False)
    lbl1.setEditable_(False)
    lbl1.setSelectable_(False)
    lbl1.setFont_(NSFont.systemFontOfSize_(11.0))
    container.addSubview_(lbl1)

    field1 = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 52, width, 22))
    container.addSubview_(field1)

    lbl2 = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 26, width, 18))
    lbl2.setStringValue_(label2)
    lbl2.setBezeled_(False)
    lbl2.setDrawsBackground_(False)
    lbl2.setEditable_(False)
    lbl2.setSelectable_(False)
    lbl2.setFont_(NSFont.systemFontOfSize_(11.0))
    container.addSubview_(lbl2)

    field2 = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, width, 22))
    container.addSubview_(field2)

    alert.setAccessoryView_(container)
    alert.window().setInitialFirstResponder_(field1)

    result = alert.runModal()
    if result == NSAlertFirstButtonReturn:
        return (field1.stringValue().strip(), field2.stringValue().strip())
    return None


class _PickerSearchDelegate(NSObject):
    _picker = objc.ivar()

    def initWithPicker_(self, picker):
        self = objc.super(_PickerSearchDelegate, self).init()
        if self is None:
            return None
        self._picker = picker
        return self

    def controlTextDidChange_(self, notification):
        if self._picker:
            self._picker._refresh()


class _PickerClickDelegate(NSObject):
    _picker = objc.ivar()

    def initWithPicker_(self, picker):
        self = objc.super(_PickerClickDelegate, self).init()
        if self is None:
            return None
        self._picker = picker
        return self

    def textView_clickedOnLink_atIndex_(self, textView, link, charIndex):
        url_str = str(link) if not isinstance(link, str) else link
        if hasattr(link, 'absoluteString'):
            url_str = link.absoluteString()
        if url_str.startswith("pick://") and self._picker:
            self._picker._selected_hash = url_str[len("pick://"):]
            NSApp.stopModalWithCode_(1)  # OK
            return True
        return False


class _PickerWindowDelegate(NSObject):
    def windowShouldClose_(self, sender):
        NSApp.stopModalWithCode_(0)  # Cancel
        sender.orderOut_(None)  # Hide instead of close
        return False


def pick_contact_or_manual(title, action_label="Use"):
    """Show a searchable contact picker window.
    Returns the destination hash, or None if cancelled."""
    _focus()

    picker = type('Picker', (), {
        '_selected_hash': None,
    })()

    # Build the window
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(300, 300, 600, 420),
        NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
        NSBackingStoreBuffered,
        False,
    )
    win.setTitle_(title)
    win_delegate = _PickerWindowDelegate.alloc().init()
    win.setDelegate_(win_delegate)
    _window_delegates.append(win_delegate)
    if APP_ICON:
        win.setRepresentedURL_(NSURL.URLWithString_("rns://"))

    content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, 420))

    # Search field
    search = NSSearchField.alloc().initWithFrame_(NSMakeRect(12, 420 - 36, 576, 24))
    search.setPlaceholderString_("Filter by name or hash...")
    search.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
    content.addSubview_(search)

    # Scrollable text view for contacts
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 50, 600, 420 - 50 - 36))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(NSBezelBorder)
    text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 600, 300))
    text_view.setEditable_(False)
    text_view.setSelectable_(True)
    text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
    text_view.setTextContainerInset_(NSSize(8, 8))
    text_view.setBackgroundColor_(NSColor.textBackgroundColor())
    click_delegate = _PickerClickDelegate.alloc().initWithPicker_(picker)
    text_view.setDelegate_(click_delegate)
    from AppKit import NSCursor, NSCursorAttributeName
    text_view.setLinkTextAttributes_({
        NSForegroundColorAttributeName: NSColor.linkColor(),
        NSUnderlineStyleAttributeName: 0,
        NSCursorAttributeName: NSCursor.pointingHandCursor(),
    })
    scroll.setDocumentView_(text_view)
    content.addSubview_(scroll)

    # Manual hash entry at the bottom
    hash_label = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 18, 100, 20))
    hash_label.setStringValue_("Manual hash:")
    hash_label.setBezeled_(False)
    hash_label.setDrawsBackground_(False)
    hash_label.setEditable_(False)
    hash_label.setFont_(NSFont.systemFontOfSize_(12.0))
    content.addSubview_(hash_label)

    hash_field = NSTextField.alloc().initWithFrame_(NSMakeRect(112, 16, 380, 24))
    hash_field.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
    hash_field.setPlaceholderString_("Enter destination hash (hex)")
    content.addSubview_(hash_field)

    use_btn = NSButton.alloc().initWithFrame_(NSMakeRect(500, 14, 88, 28))
    use_btn.setBezelStyle_(NSBezelStyleAccessoryBarAction)
    use_btn.setTitle_(action_label)
    use_btn.setTarget_(None)
    use_btn.setAction_(b"stopModalWithCode:")
    use_btn.setTag_(2)  # manual entry code
    content.addSubview_(use_btn)

    win.setContentView_(content)

    # Render function
    font = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)
    normal_attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: NSColor.labelColor(),
    }
    dim_attrs = {
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
    }
    header_attrs = {
        NSFontAttributeName: NSFont.boldSystemFontOfSize_(12.0),
        NSForegroundColorAttributeName: NSColor.labelColor(),
    }
    link_color = NSColor.linkColor() if hasattr(NSColor, 'linkColor') else NSColor.colorWithSRGBRed_green_blue_alpha_(0.4, 0.67, 1.0, 1.0)

    def refresh():
        query = search.stringValue().strip().lower()
        groups = PHONEBOOK.grouped()
        order = ["NomadNet", "LXMF"]
        ordered = [(t, groups[t]) for t in order if t in groups]
        for t in sorted(groups.keys()):
            if t not in order:
                ordered.append((t, groups[t]))
        if query:
            ordered = [
                (t, [c for c in cs if query in c[0].lower() or query in c[1].lower()])
                for t, cs in ordered
            ]
            ordered = [(t, cs) for t, cs in ordered if cs]

        section_titles = {"LXMF": "LXMF", "NomadNet": "NomadNet", "Unknown": "Other"}
        attr = NSMutableAttributedString.alloc().init()

        def append(text, attrs):
            piece = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            attr.appendAttributedString_(piece)

        total = sum(len(cs) for _, cs in ordered)
        if total == 0:
            append("No matches." if query else "No contacts yet.", dim_attrs)
        else:
            first = True
            for type_, contacts in ordered:
                t = section_titles.get(type_, type_)
                header = f"{t}  ({len(contacts)})"
                if not first:
                    append("\n", normal_attrs)
                first = False
                append(header + "\n", header_attrs)
                append("─" * len(header) + "\n", dim_attrs)
                for name, hash_, seen in contacts:
                    ago = time_ago(seen)
                    link_attrs = dict(normal_attrs)
                    link_attrs[NSLinkAttributeName] = NSURL.URLWithString_(f"pick://{hash_}")
                    link_attrs[NSForegroundColorAttributeName] = link_color
                    link_attrs[NSUnderlineStyleAttributeName] = 0
                    append(f"  {name:<24}", link_attrs)
                    append(f"  {hash_}", dim_attrs)
                    ago_str = ago if ago else ""
                    append(f"  {ago_str:>8}", dim_attrs)
                    append("\n", normal_attrs)

        text_view.textStorage().setAttributedString_(attr)

    picker._refresh = refresh
    search_delegate = _PickerSearchDelegate.alloc().initWithPicker_(picker)
    search.setDelegate_(search_delegate)

    refresh()

    # Run as modal
    result = NSApp.runModalForWindow_(win)
    win.orderOut_(None)

    if picker._selected_hash:
        return picker._selected_hash
    if result == 2:
        # Manual hash entry
        h = hash_field.stringValue().strip()
        return h if h else None
    return None


def clean_rns_output(output):
    """Clean up RNS tool output for GUI display:
    - Handle \\r overwrites (progress lines that overwrite themselves)
    - Remove Braille spinner characters used by RNS as progress indicators
    - Remove inline progress dot sequences (`. . . .`)
    - Strip trailing whitespace
    - Drop empty lines
    """
    import re
    cleaned_lines = []
    # Use split('\n') not splitlines(), since splitlines() also splits on \r
    # which would break apart progress-overwrite lines we want to handle.
    for line in output.split("\n"):
        # \r overwrites: keep only the part after the last \r (the final state)
        if "\r" in line:
            line = line.split("\r")[-1]
        # Remove RNS Braille-pattern spinner characters (U+2800–U+28FF)
        # plus surrounding whitespace
        line = re.sub(r"[\u2800-\u28FF][ \t\u2800-\u28FF]*", "", line)
        # Remove sequences of `.` separated by whitespace (legacy dot spinner)
        line = re.sub(r"(?:\.[ \t]*){2,}", "", line)
        # Strip trailing whitespace
        line = line.rstrip()
        if line.strip():
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines) or "No response"


def _format_kv(pairs, indent=2):
    """Format a list of (label, value) pairs as aligned columns."""
    if not pairs:
        return ""
    width = max(len(label) for label, _ in pairs)
    lines = []
    for label, value in pairs:
        lines.append(f"{' ' * indent}{label:<{width}}  {value}")
    return "\n".join(lines)


def format_rnpath_lookup(output):
    """Reformat rnpath lookup output as a clean key-value display."""
    import re
    cleaned = clean_rns_output(output)

    # Match: "Path found, destination <hash> is N hops away via <hash> on <iface>"
    m = re.search(
        r"destination\s+<([0-9a-f]+)>\s+is\s+(\d+)\s+hops?\s+away\s+via\s+<([0-9a-f]+)>\s+on\s+(.+)",
        cleaned,
    )
    if m:
        dest, hops, via, iface = m.groups()
        return "✓ Path found\n\n" + _format_kv([
            ("Destination", dest),
            ("Hops", hops),
            ("Via", via),
            ("Interface", iface.strip()),
        ])

    # Match: "No path to <hash>"
    m = re.search(r"[Nn]o\s+path", cleaned)
    if m:
        dest_match = re.search(r"<([0-9a-f]+)>", cleaned)
        dest = dest_match.group(1) if dest_match else "(unknown)"
        return f"✗ No path found\n\n  Destination: {dest}"

    return cleaned


def format_rnprobe(output):
    """Reformat rnprobe output as a clean key-value display."""
    import re
    cleaned = clean_rns_output(output)

    # Pull out destination hash from any line
    dest_match = re.search(r"<([0-9a-f]+)>", cleaned)
    dest = dest_match.group(1) if dest_match else None

    # Successful probe: "Round-trip time is X.XXX milliseconds over N hops"
    rtt_match = re.search(
        r"Round-trip time is\s+([\d.]+)\s+(\w+)\s+over\s+(\d+)\s+hops?",
        cleaned,
    )
    if rtt_match:
        rtt, unit, hops = rtt_match.groups()
        pairs = []
        if dest:
            pairs.append(("Destination", dest))
        pairs.append(("Status", "Reply received"))
        pairs.append(("Round-trip", f"{rtt} {unit}"))
        pairs.append(("Hops", hops))

        # Optional: RSSI / SNR if reported
        rssi = re.search(r"RSSI\s+([-\d]+)\s*dBm", cleaned)
        snr = re.search(r"SNR\s+([-\d.]+)\s*dB", cleaned)
        if rssi:
            pairs.append(("RSSI", f"{rssi.group(1)} dBm"))
        if snr:
            pairs.append(("SNR", f"{snr.group(1)} dB"))

        return "✓ Probe successful\n\n" + _format_kv(pairs)

    # Timed out: "Probe timed out" + "Sent N, received M, packet loss X%"
    if "timed out" in cleaned.lower():
        loss_match = re.search(
            r"[Ss]ent\s+(\d+),\s+received\s+(\d+),\s+packet loss\s+([\d.]+)%",
            cleaned,
        )
        pairs = []
        if dest:
            pairs.append(("Destination", dest))
        pairs.append(("Status", "Timed out"))
        if loss_match:
            sent, recv, loss = loss_match.groups()
            pairs.append(("Sent", sent))
            pairs.append(("Received", recv))
            pairs.append(("Packet loss", f"{loss}%"))
        return "✗ Destination unreachable\n\n" + _format_kv(pairs)

    # Could not resolve path
    if "could not" in cleaned.lower() or "no path" in cleaned.lower():
        return "✗ No path to destination\n\n" + (f"  Destination: {dest}" if dest else cleaned)

    return cleaned


def run_async_and_show(args, title="Output", timeout=30, post_process=None):
    """Run a command in a background thread and show output when done.
    Doesn't block the menu bar app while waiting.
    `post_process(output)` can be passed to clean up the output before display."""
    rumps.notification("RNSD", title, "Running...")

    def worker():
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout,
            )
            output = (result.stdout or "") + (result.stderr or "")
            if post_process:
                output = post_process(output)
            output = output.strip() or "No output"
            AppHelper.callAfter(
                lambda: show_alert(title, output, "Close", monospace=True)
            )
        except FileNotFoundError:
            AppHelper.callAfter(show_alert, "Error", f"Command not found: {args[0]}")
        except subprocess.TimeoutExpired:
            msg = f"Timed out after {timeout}s — destination likely unreachable."
            AppHelper.callAfter(
                lambda: show_alert(title, msg, "Close", monospace=True)
            )
        except Exception as e:
            err = str(e)
            AppHelper.callAfter(lambda: show_alert("Error", err))

    threading.Thread(target=worker, daemon=True).start()


# ── Shared window delegate (hides window on close, prevents app quit) ──

class _HideOnCloseDelegate(NSObject):
    """Window delegate that hides instead of closing, preventing app termination."""
    def windowShouldClose_(self, sender):
        sender.orderOut_(None)
        return False


# Module-level list to prevent garbage collection of window delegates.
# PyObjC 12+ does not allow setting arbitrary Python attributes on ObjC
# objects (e.g. window._hideDelegate raises AttributeError), so we keep
# strong references here instead.
_window_delegates = []


def _make_window_hide_on_close(window):
    """Configure a window to hide instead of close when the X button is clicked."""
    delegate = _HideOnCloseDelegate.alloc().init()
    window.setDelegate_(delegate)
    _window_delegates.append(delegate)
    return delegate


# ── Path Table Window ─────────────────────────────────────────────

def _parse_path_table(raw_output):
    """Parse rnpath -t output into structured entries.
    Each line: <hash> is N hop(s) away via <hash> on Interface expires YYYY-MM-DD HH:MM:SS
    Returns list of dicts sorted by hops then hash."""
    import re
    from datetime import datetime
    entries = []
    for line in raw_output.strip().splitlines():
        m = re.match(
            r"<([0-9a-fA-F]+)>\s+is\s+(\d+)\s+hops?\s+away\s+"
            r"via\s+<([0-9a-fA-F]+)>\s+on\s+(.+?)\s+expires\s+(.+)$",
            line.strip(),
        )
        if not m:
            continue
        dest, hops, via, iface, expires_str = m.groups()
        # Shorten interface name: "TCPInterface[host/host:port]" → "host:port"
        iface_short = iface
        im = re.search(r'\[.*?/(.+?)\]', iface)
        if im:
            iface_short = im.group(1)
        elif re.search(r'\[(.+?)\]', iface):
            iface_short = re.search(r'\[(.+?)\]', iface).group(1)
        # Parse expiry to relative time
        try:
            exp_dt = datetime.strptime(expires_str.strip(), "%Y-%m-%d %H:%M:%S")
            delta = (exp_dt - datetime.now()).total_seconds()
            if delta < 0:
                expires_rel = "expired"
            elif delta < 3600:
                expires_rel = f"{int(delta // 60)}m"
            elif delta < 86400:
                expires_rel = f"{int(delta // 3600)}h"
            else:
                expires_rel = f"{int(delta // 86400)}d"
        except Exception:
            expires_rel = expires_str.strip()
        # Resolve name from Nodebook
        name = None
        with PHONEBOOK._lock:
            entry = PHONEBOOK.contacts.get(dest)
            if entry:
                name = entry.get("name")
        entries.append({
            "dest": dest,
            "name": name,
            "hops": int(hops),
            "via": via,
            "iface": iface_short,
            "expires": expires_rel,
        })
    entries.sort(key=lambda e: (e["hops"], (e["name"] or "").lower(), e["dest"]))
    return entries


class _PathTableSearchDelegate(NSObject):
    _window = objc.ivar()

    def initWithWindow_(self, window):
        self = objc.super(_PathTableSearchDelegate, self).init()
        if self is None:
            return None
        self._window = window
        return self

    def controlTextDidChange_(self, notification):
        if self._window:
            self._window._refresh_display()


class PathTableWindow:
    """Singleton window showing the path table with live search."""

    _instance = None

    def __init__(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(200, 200, 700, 500),
            style,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("Path Table")
        self._window.setReleasedWhenClosed_(False)
        self._window.setMinSize_(NSSize(400, 300))
        _make_window_hide_on_close(self._window)

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 700, 500))

        # Search field
        self._search = NSSearchField.alloc().initWithFrame_(NSMakeRect(8, 500 - 30, 684, 24))
        self._search.setPlaceholderString_("Filter by name or hash...")
        self._search.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
        self._search_delegate = _PathTableSearchDelegate.alloc().initWithWindow_(self)
        self._search.setDelegate_(self._search_delegate)
        content.addSubview_(self._search)

        # Scrollable text view
        self._scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 700, 500 - 36))
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setHasHorizontalScroller_(False)
        self._scroll.setBorderType_(NSBezelBorder)
        self._scroll.setAutohidesScrollers_(False)

        self._text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 700, 500 - 36))
        self._text_view.setEditable_(False)
        self._text_view.setSelectable_(True)
        self._text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
        self._text_view.setTextContainerInset_(NSSize(8, 8))
        self._text_view.setBackgroundColor_(NSColor.textBackgroundColor())
        self._scroll.setDocumentView_(self._text_view)
        content.addSubview_(self._scroll)

        self._window.setContentView_(content)

        # Handle resize
        from Foundation import NSNotificationCenter
        self._resize_obs = _PathTableResizeObserver.alloc().initWithWindow_(self)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._resize_obs, b"windowDidResize:",
            "NSWindowDidResizeNotification", self._window,
        )

        self._entries = []

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def show(self, raw_output):
        """Parse output and show the window."""
        self._entries = _parse_path_table(raw_output)
        self._refresh_display()
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def _refresh_display(self):
        """Rebuild the attributed string, filtered by search query."""
        query = self._search.stringValue().strip().lower()
        entries = self._entries
        if query:
            entries = [
                e for e in entries
                if query in e["dest"].lower()
                or (e["name"] and query in e["name"].lower())
                or query in e["iface"].lower()
            ]

        font = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)
        normal_attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        dim_attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }
        header_attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(12.0),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }

        attr = NSMutableAttributedString.alloc().init()

        def append(text, attrs):
            piece = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            attr.appendAttributedString_(piece)

        # Group by hops
        from itertools import groupby
        if not entries:
            append("No paths found." if not query else "No matches.", dim_attrs)
        else:
            total = len(entries)
            append(f"{total} path{'s' if total != 1 else ''}\n\n", dim_attrs)
            first = True
            for hops, group in groupby(entries, key=lambda e: e["hops"]):
                group = list(group)
                if not first:
                    append("\n", normal_attrs)
                first = False
                label = f"{hops} hop{'s' if hops != 1 else ''}  ({len(group)})"
                append(label + "\n", header_attrs)
                append("─" * len(label) + "\n", dim_attrs)
                for e in group:
                    display = e["name"] or e["dest"]
                    append(f"  {display:<24}", normal_attrs)
                    append(f"  {e['dest']}", dim_attrs)
                    append(f"  {e['expires']:>8}", dim_attrs)
                    append(f"  {e['iface']}", dim_attrs)
                    append("\n", normal_attrs)

        self._text_view.textStorage().setAttributedString_(attr)

    def _relayout(self):
        frame = self._window.contentView().frame()
        w, h = frame.size.width, frame.size.height
        self._search.setFrame_(NSMakeRect(8, h - 30, w - 16, 24))
        self._scroll.setFrame_(NSMakeRect(0, 0, w, h - 36))


class _PathTableResizeObserver(NSObject):
    _win = objc.ivar()

    def initWithWindow_(self, window):
        self = objc.super(_PathTableResizeObserver, self).init()
        if self is None:
            return None
        self._win = window
        return self

    def windowDidResize_(self, notification):
        if self._win:
            self._win._relayout()


# ── Nodebook Window ───────────────────────────────────────────────

class _NodebookSearchDelegate(NSObject):
    _window = objc.ivar()

    def initWithWindow_(self, window):
        self = objc.super(_NodebookSearchDelegate, self).init()
        if self is None:
            return None
        self._window = window
        return self

    def controlTextDidChange_(self, notification):
        if self._window:
            self._window._refresh_display()


class _NodebookClickDelegate(NSObject):
    """Handles clicks on NomadNet node links in the Nodebook text view."""
    def textView_clickedOnLink_atIndex_(self, textView, link, charIndex):
        url_str = str(link) if not isinstance(link, str) else link
        if hasattr(link, 'absoluteString'):
            url_str = link.absoluteString()
        if url_str.startswith("rns://"):
            open_rns_url(url_str)
            return True
        elif url_str.startswith("copy://"):
            # copy:// is our internal scheme for the copy-link action
            rns_link = "rns://" + url_str[len("copy://"):]
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(rns_link, NSPasteboardTypeString)
            rumps.notification("RNSD", "Link Copied", rns_link)
            return True
        return False


class NodebookWindow:
    """Singleton searchable Nodebook window."""

    _instance = None

    def __init__(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(200, 250, 760, 500),
            style,
            NSBackingStoreBuffered,
            False,
        )
        self._window.setTitle_("Nodebook")
        self._window.setReleasedWhenClosed_(False)
        self._window.setMinSize_(NSSize(400, 300))
        _make_window_hide_on_close(self._window)

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 760, 500))

        # Search field
        self._search = NSSearchField.alloc().initWithFrame_(NSMakeRect(8, 500 - 30, 744, 24))
        self._search.setPlaceholderString_("Filter by name or hash...")
        self._search.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
        self._search_delegate = _NodebookSearchDelegate.alloc().initWithWindow_(self)
        self._search.setDelegate_(self._search_delegate)
        content.addSubview_(self._search)

        # Scrollable text view
        self._scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 760, 500 - 36))
        self._scroll.setHasVerticalScroller_(True)
        self._scroll.setHasHorizontalScroller_(False)
        self._scroll.setBorderType_(NSBezelBorder)
        self._scroll.setAutohidesScrollers_(False)

        self._text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 760, 500 - 36))
        self._text_view.setEditable_(False)
        self._text_view.setSelectable_(True)
        self._text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))
        self._text_view.setTextContainerInset_(NSSize(8, 8))
        self._text_view.setBackgroundColor_(NSColor.textBackgroundColor())
        # Enable link clicking
        self._text_view.setAutomaticLinkDetectionEnabled_(False)
        self._click_delegate = _NodebookClickDelegate.alloc().init()
        self._text_view.setDelegate_(self._click_delegate)
        from AppKit import NSCursor, NSCursorAttributeName
        self._text_view.setLinkTextAttributes_({
            NSForegroundColorAttributeName: NSColor.linkColor(),
            NSUnderlineStyleAttributeName: 0,
            NSCursorAttributeName: NSCursor.pointingHandCursor(),
        })

        self._scroll.setDocumentView_(self._text_view)
        content.addSubview_(self._scroll)

        self._window.setContentView_(content)

        # Handle resize
        from Foundation import NSNotificationCenter
        self._resize_obs = _NodebookResizeObserver.alloc().initWithWindow_(self)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._resize_obs, b"windowDidResize:",
            "NSWindowDidResizeNotification", self._window,
        )

    @classmethod
    def shared(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def show(self):
        """Refresh data and show the window."""
        self._refresh_display()
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)
        # Start auto-refresh while window is visible
        self._start_refresh_timer()

    def _start_refresh_timer(self):
        if hasattr(self, '_refresh_timer') and self._refresh_timer:
            return
        from Foundation import NSTimer
        self._refresh_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            5.0, True, lambda t: self._auto_refresh()
        )

    def _auto_refresh(self):
        if not self._window.isVisible():
            if self._refresh_timer:
                self._refresh_timer.invalidate()
                self._refresh_timer = None
            return
        self._refresh_display()

    def _refresh_display(self):
        """Rebuild the attributed string, filtered by search query."""
        query = self._search.stringValue().strip().lower()

        groups = PHONEBOOK.grouped()
        order = ["NomadNet", "LXMF"]
        ordered = [(t, groups[t]) for t in order if t in groups]
        for t in sorted(groups.keys()):
            if t not in order:
                ordered.append((t, groups[t]))

        # Apply search filter
        if query:
            filtered = []
            for type_, contacts in ordered:
                matches = [
                    c for c in contacts
                    if query in c[0].lower() or query in c[1].lower()
                ]
                if matches:
                    filtered.append((type_, matches))
            ordered = filtered

        section_titles = {
            "LXMF": "LXMF",
            "NomadNet": "NomadNet Nodes",
            "Unknown": "Other",
        }

        font = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)
        normal_attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        dim_attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }
        header_attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(12.0),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        link_color = NSColor.linkColor() if hasattr(NSColor, 'linkColor') else NSColor.colorWithSRGBRed_green_blue_alpha_(0.4, 0.67, 1.0, 1.0)
        copy_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(10.0),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }

        attr = NSMutableAttributedString.alloc().init()

        def append(text, attrs):
            piece = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            attr.appendAttributedString_(piece)

        total = sum(len(contacts) for _, contacts in ordered)

        if total == 0:
            if query:
                append("No matches.", dim_attrs)
            else:
                append(
                    "No nodes discovered yet.\n\n"
                    "Nodes are auto-populated as RNS announces\n"
                    "are received by rnsd.",
                    dim_attrs,
                )
        else:
            append(f"{total} node{'s' if total != 1 else ''}\n\n", dim_attrs)
            first_section = True
            for type_, contacts in ordered:
                title = section_titles.get(type_, type_)
                header = f"{title}  ({len(contacts)})"
                if not first_section:
                    append("\n\n", normal_attrs)
                first_section = False
                append(header + "\n", header_attrs)
                append("─" * len(header) + "\n", dim_attrs)
                for name, hash_, seen in contacts:
                    ago = time_ago(seen)
                    # Name — clickable for NomadNet nodes
                    if type_ == "NomadNet":
                        rns_url = f"rns://{hash_}/page/index.mu"
                        name_link_attrs = dict(normal_attrs)
                        name_link_attrs[NSLinkAttributeName] = NSURL.URLWithString_(rns_url)
                        name_link_attrs[NSForegroundColorAttributeName] = link_color
                        name_link_attrs[NSUnderlineStyleAttributeName] = 0
                        append(f"  {name:<24}", name_link_attrs)
                    else:
                        append(f"  {name:<24}", normal_attrs)
                    append(f"  {hash_}", dim_attrs)
                    ago_str = ago if ago else ""
                    append(f"  {ago_str:>8}", dim_attrs)
                    # Copy link
                    copy_link_attrs = dict(copy_attrs)
                    copy_url = f"copy://{hash_}/page/index.mu"
                    copy_link_attrs[NSLinkAttributeName] = NSURL.URLWithString_(copy_url)
                    copy_link_attrs[NSUnderlineStyleAttributeName] = 0
                    append(f"  [copy]", copy_link_attrs)
                    append("\n", normal_attrs)

        self._text_view.textStorage().setAttributedString_(attr)

    def _relayout(self):
        frame = self._window.contentView().frame()
        w, h = frame.size.width, frame.size.height
        self._search.setFrame_(NSMakeRect(8, h - 30, w - 16, 24))
        self._scroll.setFrame_(NSMakeRect(0, 0, w, h - 36))


class _NodebookResizeObserver(NSObject):
    _win = objc.ivar()

    def initWithWindow_(self, window):
        self = objc.super(_NodebookResizeObserver, self).init()
        if self is None:
            return None
        self._win = window
        return self

    def windowDidResize_(self, notification):
        if self._win:
            self._win._relayout()


# ── App ──────────────────────────────────────────────────────────

class RNSDMenuBar(rumps.App):
    def __init__(self):
        super().__init__("RNSD", title="⬡", quit_button=None)
        self.running = False
        self.rnsd_process = None

        # ── Build menu ───────────────────────────────────────────

        self.status_item = rumps.MenuItem("Status: starting...")

        m_status = rumps.MenuItem("rnstatus", callback=self.cmd_rnstatus)

        m_id = rumps.MenuItem("rnid")
        m_id["Show Identity"] = rumps.MenuItem(
            "Show Identity", callback=self.cmd_rnid_show)
        m_id["Generate New Identity..."] = rumps.MenuItem(
            "Generate New Identity...", callback=self.cmd_rnid_generate)

        m_net = rumps.MenuItem("Network")
        m_net["Lookup Path..."] = rumps.MenuItem(
            "Lookup Path...", callback=self.cmd_rnpath_lookup)
        m_net["Probe..."] = rumps.MenuItem(
            "Probe...", callback=self.cmd_probe)
        m_net["_sep1"] = None
        m_net["Path Table"] = rumps.MenuItem(
            "Path Table", callback=self.cmd_rnpath_table)

        m_nomad = rumps.MenuItem("Nomadnet")
        m_nomad["Nodebook..."] = rumps.MenuItem(
            "Nodebook...", callback=self.cmd_nodebook_open)
        m_nomad["Open Page..."] = rumps.MenuItem(
            "Open Page...", callback=self.cmd_nomad_open)
        m_nomad["_sep"] = None
        m_nomad["Clear Nodebook"] = rumps.MenuItem(
            "Clear Nodebook", callback=self.cmd_nodebook_clear)

        restart_item = rumps.MenuItem("Restart RNSD", callback=self.restart_rnsd)
        config_item = rumps.MenuItem("Open Config Folder", callback=self.open_config)
        quit_item = rumps.MenuItem("Quit (stops RNSD)", callback=self.quit_app)

        # Apply SF Symbol icons
        _set_menu_icon(restart_item, "arrow.clockwise")
        _set_menu_icon(m_status, "info.circle")
        _set_menu_icon(m_id, "key")
        _set_menu_icon(m_net, "antenna.radiowaves.left.and.right")
        _set_menu_icon(m_nomad, "globe")
        _set_menu_icon(config_item, "folder")
        _set_menu_icon(quit_item, "power")

        self.menu = [
            self.status_item,
            None,
            restart_item,
            None,
            m_status,
            m_id,
            m_net,
            m_nomad,
            None,
            config_item,
            None,
            quit_item,
        ]

        # Auto-start rnsd
        self._start_rnsd_process()

        # Start the loopback HTTP bridge for rns:// page rendering
        try:
            port = start_bridge_server()
            _flush_pending_urls()
        except Exception as e:
            print(f"Could not start HTTP bridge: {e}")

        # Note: URL scheme handler is registered at module level before app.run()

        # Initialize the announce listener on the main thread, after a delay
        # to give rnsd time to start its shared instance.
        # RNS.Reticulum() installs signal handlers, which Python only allows
        # from the main thread, so we cannot use a background thread here.
        self._announce_init_done = False
        self._announce_init_attempts = 0
        self._announce_timer = rumps.Timer(self._try_init_announces, 3.0)
        self._announce_timer.start()

        # Clean up RNS client and rnsd no matter how the app exits
        atexit.register(shutdown_rns)
        atexit.register(self._stop_rnsd_process)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Poll status every 10 seconds
        self.timer = rumps.Timer(self.check_status, 10)
        self.timer.start()


        # Set custom icon shortly after the app loop starts
        self._icon_set = False
        self._icon_timer = rumps.Timer(self._try_set_icon, 0.2)
        self._icon_timer.start()

    def _try_set_icon(self, _):
        if self._icon_set:
            return
        if self._set_menubar_icon():
            self._icon_set = True
            self._icon_timer.stop()

    def _try_init_announces(self, _):
        """Main-thread one-shot init for the announce listener.
        Retries up to 5 times with 3-second intervals if rnsd isn't ready."""
        if self._announce_init_done:
            return
        self._announce_init_attempts += 1
        if init_announce_listener():
            self._announce_init_done = True
            self._announce_timer.stop()
            # RNS.Reticulum() overwrites SIGTERM/SIGINT with handlers that
            # call os._exit(0), which kills the app instantly when macOS
            # sends SIGTERM on last window close. Re-register ours.
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGINT, self._signal_handler)
        elif self._announce_init_attempts >= 5:
            self._announce_timer.stop()

    # ── Process management ───────────────────────────────────────

    def _set_menubar_icon(self):
        """Set custom menu bar icon. Returns True if done (success or give up),
        False if NSStatusItem isn't ready yet so we should retry."""
        if not os.path.isfile(MENU_ICON_PATH):
            return True  # nothing to do

        try:
            # Find the NSStatusItem in rumps internals
            status_item = None
            for attr in ('_nsapp', 'nsapp'):
                if hasattr(self, attr):
                    obj = getattr(self, attr)
                    if hasattr(obj, 'nsstatusitem'):
                        status_item = obj.nsstatusitem
                        break

            if status_item is None:
                return False  # not ready yet, retry

            image = NSImage.alloc().initWithContentsOfFile_(MENU_ICON_PATH)
            if not image:
                return True  # bad image, give up

            image.setSize_(NSSize(22, 22))
            image.setTemplate_(False)
            button = status_item.button()
            if button:
                button.setImage_(image)
                button.setTitle_("")
            return True
        except Exception as e:
            print(f"Could not set menu bar icon: {e}")
            return True

    def _signal_handler(self, signum, frame):
        # SIGTERM is sent by macOS when the last window of an accessory app
        # closes. Ignore it so the menu bar app stays alive.
        # Only SIGINT (Ctrl+C) should actually quit.
        if signum == signal.SIGTERM:
            return
        global _allow_quit
        _allow_quit = True
        shutdown_rns()
        self._stop_rnsd_process()
        rumps.quit_application()

    @staticmethod
    def _kill_orphan_rnsd():
        """Kill any leftover rnsd processes from previous app runs."""
        my_pid = os.getpid()
        try:
            result = subprocess.run(
                ["pgrep", "-f", "rns-tool rnsd|bin/rnsd"],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.strip().splitlines():
                pid = int(line.strip())
                if pid == my_pid:
                    continue
                # Check if the process's parent is still a running RNSD app
                try:
                    ppid_result = subprocess.run(
                        ["ps", "-o", "ppid=", "-p", str(pid)],
                        capture_output=True, text=True, timeout=2,
                    )
                    ppid = int(ppid_result.stdout.strip())
                    # ppid 1 means orphaned (adopted by init/launchd)
                    if ppid <= 1:
                        os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
        except Exception:
            pass

    def _start_rnsd_process(self):
        if self.rnsd_process and self.rnsd_process.poll() is None:
            return
        self._kill_orphan_rnsd()
        if not BINS["rnsd"]:
            self.title = "✕" if os.path.isfile(MENU_ICON_PATH) else "⬡✕"
            self.status_item.title = "Status: rnsd not found!"
            searched = "\n".join(f"  • {d}" for d in SEARCH_DIRS)
            show_alert(
                "RNSD not found",
                f"Could not find rnsd.\n\n"
                f"Searched:\n{searched}\n  • system PATH\n\n"
                f"Make sure Reticulum is installed in the uv venv:\n"
                f"  cd {SCRIPT_DIR}\n"
                f"  uv pip install rns",
            )
            return
        try:
            log_out = open("/tmp/rnsd.log", "a")
            log_err = open("/tmp/rnsd.err", "a")
            self.rnsd_process = subprocess.Popen(
                BINS["rnsd"] + ["--service"],
                stdout=log_out,
                stderr=log_err,
            )
            self.status_item.title = "Status: starting..."
            self.title = "" if os.path.isfile(MENU_ICON_PATH) else "⬡"
        except Exception as e:
            self.rnsd_process = None
            self.title = "✕" if os.path.isfile(MENU_ICON_PATH) else "⬡✕"
            self.status_item.title = "Status: start failed"
            show_alert("Error starting RNSD", str(e))

    def _stop_rnsd_process(self):
        if self.rnsd_process and self.rnsd_process.poll() is None:
            try:
                self.rnsd_process.terminate()
                self.rnsd_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.rnsd_process.kill()
            except Exception:
                pass
        self.rnsd_process = None

    # ── Status polling ───────────────────────────────────────────

    def check_status(self, _):
        if self.rnsd_process and self.rnsd_process.poll() is not None:
            self._start_rnsd_process()

        # Only check process liveness — avoid running rnstatus on a timer
        # because each invocation opens a new LocalInterface to the shared
        # instance, and polling every 10s causes connections to pile up.
        if self.rnsd_process and self.rnsd_process.poll() is None:
            self.running = True
            self.title = "" if os.path.isfile(MENU_ICON_PATH) else "⬡"
            self.status_item.title = "Status: running"
        else:
            self._set_stopped()

    def _set_stopped(self):
        self.running = False
        self.title = "✕" if os.path.isfile(MENU_ICON_PATH) else "⬡✕"
        self.status_item.title = "Status: not running"

    # ── rnstatus ─────────────────────────────────────────────────

    def cmd_rnstatus(self, _):
        if require_bin("rnstatus"):
            run_and_show(BINS["rnstatus"], "rnstatus")

    # ── rnid ─────────────────────────────────────────────────────

    def cmd_rnid_show(self, _):
        if not require_bin("rnid"):
            return
        id_dir = os.path.expanduser("~/.reticulum/identities")
        if not os.path.isdir(id_dir):
            show_alert("rnid", "No identities directory found.\nGenerate one first.")
            return
        names = sorted(
            f for f in os.listdir(id_dir)
            if os.path.isfile(os.path.join(id_dir, f))
            and not f.startswith(".")
        )
        if not names:
            show_alert("rnid", "No identities found.\nGenerate one first.")
            return
        name = show_dropdown(
            title="rnid — Show Identity",
            message="Select an identity to inspect:",
            options=names,
            ok="Show",
        )
        if name:
            path = os.path.join(id_dir, name)
            run_and_show(BINS["rnid"] + ["-i", path], f"rnid — {name}")

    def cmd_rnid_generate(self, _):
        if not require_bin("rnid"):
            return
        id_dir = os.path.expanduser("~/.reticulum/identities")
        os.makedirs(id_dir, exist_ok=True)
        name = show_prompt(
            title="rnid — Generate Identity",
            message=f"Enter a name for the new identity:\n\n"
                    f"It will be saved to:\n{id_dir}/<name>",
            ok="Generate",
            default_text="my_identity",
        )
        if name:
            path = os.path.join(id_dir, name)
            if os.path.exists(path):
                show_alert("Error", f"Identity already exists:\n{path}")
                return
            run_and_show(BINS["rnid"] + ["-g", path], "rnid — Generate")

    # ── rnpath ───────────────────────────────────────────────────

    def cmd_rnpath_table(self, _):
        if not require_bin("rnpath"):
            return
        rumps.notification("RNSD", "Path Table", "Loading...")

        def worker():
            try:
                result = subprocess.run(
                    BINS["rnpath"] + ["-t"],
                    capture_output=True, text=True, timeout=10,
                )
                output = (result.stdout or "") + (result.stderr or "")
                AppHelper.callAfter(lambda: PathTableWindow.shared().show(output))
            except Exception as e:
                err = str(e)
                AppHelper.callAfter(lambda: show_alert("Error", err))

        threading.Thread(target=worker, daemon=True).start()

    def cmd_rnpath_rates(self, _):
        if require_bin("rnpath"):
            run_and_show(BINS["rnpath"] + ["-r"], "rnpath — Announce Rates")

    def cmd_rnpath_lookup(self, _):
        if not require_bin("rnpath"):
            return
        dest = pick_contact_or_manual("rnpath — Lookup", action_label="Lookup")
        if dest:
            run_async_and_show(
                BINS["rnpath"] + [dest],
                "rnpath — Lookup Result",
                timeout=15,
                post_process=format_rnpath_lookup,
            )

    def cmd_rnpath_drop(self, _):
        if not require_bin("rnpath"):
            return
        dest = pick_contact_or_manual("rnpath — Drop Path", action_label="Drop")
        if dest:
            run_and_show(
                BINS["rnpath"] + ["-d", dest],
                "rnpath — Drop Path",
            )

    def cmd_rnpath_drop_announces(self, _):
        if require_bin("rnpath"):
            run_and_show(
                BINS["rnpath"] + ["-D"],
                "rnpath — Drop Queued Announces",
            )

    # ── rnprobe ──────────────────────────────────────────────────

    def cmd_probe(self, _):
        if not require_bin("rnprobe"):
            return
        dest = pick_contact_or_manual("Probe", action_label="Probe")
        if not dest:
            return
        # Auto-detect aspect from the Nodebook entry type
        aspect = None
        with PHONEBOOK._lock:
            entry = PHONEBOOK.contacts.get(dest)
            if entry:
                t = entry.get("type")
                if t == "NomadNet":
                    aspect = "nomadnetwork.node"
                elif t == "LXMF":
                    aspect = "lxmf.delivery"
        cmd = list(BINS["rnprobe"])
        if aspect:
            cmd.append(aspect)
        cmd.append(dest)
        run_async_and_show(
            cmd,
            "Probe",
            timeout=30,
            post_process=format_rnprobe,
        )

    # ── Nomadnet ─────────────────────────────────────────────────

    def cmd_nomad_open(self, _):
        """Prompt for an rns:// URL and open it in the browser."""
        url = show_prompt(
            title="Nomadnet — Open Page",
            message="Enter an rns:// URL:\n\n"
                    "Format: rns://<destination_hash>/page/<path>",
            ok="Open",
            default_text="rns://",
        )
        if url and url.startswith("rns://") and len(url) > 6:
            open_rns_url(url)

    # ── Nodebook ─────────────────────────────────────────────────

    def cmd_nodebook_open(self, _):
        """Open the searchable Nodebook window."""
        NodebookWindow.shared().show()

    def cmd_nodebook_clear(self, _):
        if PHONEBOOK.is_empty():
            show_alert("Nodebook", "No nodes to clear.")
            return
        PHONEBOOK.clear()
        show_alert("Nodebook", "All nodes cleared.")

    # ── Other menu actions ───────────────────────────────────────

    def restart_rnsd(self, _):
        self._stop_rnsd_process()
        self._set_stopped()
        import time
        time.sleep(1)
        self._start_rnsd_process()
        rumps.notification("RNSD", "", "Restarting rnsd...")

    def open_config(self, _):
        config_dir = os.path.expanduser("~/.reticulum")
        if os.path.isdir(config_dir):
            subprocess.run(["open", config_dir])
        else:
            show_alert("Config", f"Config directory not found:\n{config_dir}")

    def quit_app(self, _):
        global _allow_quit
        _allow_quit = True
        shutdown_rns()
        self._stop_rnsd_process()
        rumps.quit_application()


if __name__ == "__main__":
    # Register the rns:// URL scheme handler BEFORE the event loop starts.
    # This must happen early so macOS routes URL events to our handler.
    try:
        register_url_handler()
    except Exception as e:
        print(f"Could not register URL handler: {e}")

    RNSDMenuBar().run()

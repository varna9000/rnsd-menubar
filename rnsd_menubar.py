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
    NSForegroundColorAttributeName, NSFontAttributeName,
)
from Foundation import (
    NSSize, NSMakeRect,
    NSAttributedString, NSMutableAttributedString,
)
NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory

import rumps
import subprocess
import os
import signal
import shutil
import atexit
import threading
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
               monospace=False, attributed_message=None):
    """Show an alert dialog with the Reticulum icon.
    Uses a scrollable text view if the content would be too tall.
    Pass monospace=True to use a fixed-width font for technical output.
    Pass attributed_message=NSAttributedString for styled (colored) text."""
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

    if size.height <= max_height:
        # Short enough — use the simple label
        label.setFrame_(((0, 0), (width, size.height)))
        if attributed_message is not None:
            label.setAttributedStringValue_(attributed_message)
        alert.setAccessoryView_(label)
    else:
        # Too tall — use a scrollable text view
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width, max_height)
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
        - Type can be upgraded from Unknown to a known type"""
        if not is_valid_name(name):
            return False
        with self._lock:
            existing = self.contacts.get(hash_)
            if existing:
                changed = False
                # Upgrade Unknown type if we now have a better one
                if existing.get("type") == "Unknown" and type_ != "Unknown":
                    existing["type"] = type_
                    changed = True
                if changed:
                    self.save()
                return changed
            self.contacts[hash_] = {"name": name, "type": type_}
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
        """Return contacts grouped by type: {type: [(name, hash), ...]}."""
        with self._lock:
            groups = {}
            for hash_, entry in self.contacts.items():
                t = entry.get("type", "Unknown")
                groups.setdefault(t, []).append((entry["name"], hash_))
            for t in groups:
                groups[t].sort(key=lambda x: x[0].lower())
            return groups

    def is_empty(self):
        with self._lock:
            return not self.contacts


PHONEBOOK = Phonebook()


def init_announce_listener():
    """Initialize RNS and register announce handlers.
    MUST be called from the main thread because RNS.Reticulum() installs
    signal handlers, which Python only allows from the main thread.
    Once registered, RNS's own threads invoke the handlers when announces arrive."""
    try:
        import RNS
    except ImportError:
        return False

    try:
        RNS.Reticulum(loglevel=0)
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


def pick_contact_or_manual(title, action_label="Use"):
    """Show a dropdown of phonebook entries plus a manual entry option.
    Returns the destination hash, or None if cancelled."""
    options = []
    if not PHONEBOOK.is_empty():
        options.extend(PHONEBOOK.names())
    options.append("— Enter hash manually —")

    choice = show_dropdown(
        title=title,
        message="Select a contact or enter a hash manually:",
        options=options,
        ok=action_label,
    )
    if not choice:
        return None
    if choice == "— Enter hash manually —":
        return show_prompt(
            title=title,
            message="Enter destination hash (hex):",
            ok=action_label,
        )
    return PHONEBOOK.get(choice)


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


# ── App ──────────────────────────────────────────────────────────

class RNSDMenuBar(rumps.App):
    def __init__(self):
        super().__init__("RNSD", title="⬡", quit_button=None)
        self.running = False
        self.rnsd_process = None

        # ── Build menu ───────────────────────────────────────────

        self.status_item = rumps.MenuItem("Status: starting...")
        self.interfaces_item = rumps.MenuItem("Interfaces: —")

        m_status = rumps.MenuItem("rnstatus", callback=self.cmd_rnstatus)

        m_id = rumps.MenuItem("rnid")
        m_id["Show Identity"] = rumps.MenuItem(
            "Show Identity", callback=self.cmd_rnid_show)
        m_id["Generate New Identity..."] = rumps.MenuItem(
            "Generate New Identity...", callback=self.cmd_rnid_generate)

        m_path = rumps.MenuItem("rnpath")
        m_path["Path Table"] = rumps.MenuItem(
            "Path Table", callback=self.cmd_rnpath_table)
        m_path["Announce Rates"] = rumps.MenuItem(
            "Announce Rates", callback=self.cmd_rnpath_rates)
        m_path["Lookup Destination..."] = rumps.MenuItem(
            "Lookup Destination...", callback=self.cmd_rnpath_lookup)
        m_path["Drop Path..."] = rumps.MenuItem(
            "Drop Path...", callback=self.cmd_rnpath_drop)
        m_path["Drop All Queued Announces"] = rumps.MenuItem(
            "Drop All Queued Announces", callback=self.cmd_rnpath_drop_announces)

        m_probe = rumps.MenuItem("rnprobe")
        m_probe["LXMF Delivery"] = rumps.MenuItem(
            "LXMF Delivery", callback=self.cmd_rnprobe_lxmf)
        m_probe["NomadNet Node"] = rumps.MenuItem(
            "NomadNet Node", callback=self.cmd_rnprobe_nomad)
        m_probe["By Hash Only..."] = rumps.MenuItem(
            "By Hash Only...", callback=self.cmd_rnprobe_hash)

        m_node = rumps.MenuItem("Nodebook")
        m_node["List Nodes"] = rumps.MenuItem(
            "List Nodes", callback=self.cmd_nodebook_list)
        m_node["Clear All"] = rumps.MenuItem(
            "Clear All", callback=self.cmd_nodebook_clear)

        restart_item = rumps.MenuItem("Restart RNSD", callback=self.restart_rnsd)
        config_item = rumps.MenuItem("Open Config Folder", callback=self.open_config)
        quit_item = rumps.MenuItem("Quit (stops RNSD)", callback=self.quit_app)

        # Apply SF Symbol icons
        _set_menu_icon(restart_item, "arrow.clockwise")
        _set_menu_icon(m_status, "info.circle")
        _set_menu_icon(m_id, "key")
        _set_menu_icon(m_path, "map")
        _set_menu_icon(m_probe, "antenna.radiowaves.left.and.right")
        _set_menu_icon(m_node, "book")
        _set_menu_icon(config_item, "folder")
        _set_menu_icon(quit_item, "power")

        self.menu = [
            self.status_item,
            self.interfaces_item,
            None,
            restart_item,
            None,
            m_status,
            m_id,
            m_path,
            m_probe,
            m_node,
            None,
            config_item,
            None,
            quit_item,
        ]

        # Auto-start rnsd
        self._start_rnsd_process()

        # Initialize the announce listener on the main thread, after a delay
        # to give rnsd time to start its shared instance.
        # RNS.Reticulum() installs signal handlers, which Python only allows
        # from the main thread, so we cannot use a background thread here.
        self._announce_init_done = False
        self._announce_init_attempts = 0
        self._announce_timer = rumps.Timer(self._try_init_announces, 3.0)
        self._announce_timer.start()

        # Clean up rnsd no matter how the app exits
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
        self._stop_rnsd_process()
        rumps.quit_application()

    def _start_rnsd_process(self):
        if self.rnsd_process and self.rnsd_process.poll() is None:
            return
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

        if not BINS["rnstatus"]:
            if self.rnsd_process and self.rnsd_process.poll() is None:
                self.running = True
                self.title = "" if os.path.isfile(MENU_ICON_PATH) else "⬡"
                self.status_item.title = "Status: running (rnstatus not found)"
            else:
                self._set_stopped()
            return

        try:
            result = subprocess.run(
                BINS["rnstatus"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                self.running = True
                self.title = "" if os.path.isfile(MENU_ICON_PATH) else "⬡"
                self.status_item.title = "Status: running"
                lines = result.stdout.strip().splitlines()
                iface_count = sum(
                    1 for l in lines
                    if l.strip() and not l.startswith(" ")
                    and "─" not in l and "Reticulum" not in l
                )
                if iface_count > 0:
                    self.interfaces_item.title = f"Interfaces: {iface_count} active"
                else:
                    self.interfaces_item.title = "Interfaces: see full status"
            else:
                self._set_stopped()
        except subprocess.TimeoutExpired:
            self._set_stopped()
            self.status_item.title = "Status: timeout"
        except Exception:
            self._set_stopped()

    def _set_stopped(self):
        self.running = False
        self.title = "✕" if os.path.isfile(MENU_ICON_PATH) else "⬡✕"
        self.status_item.title = "Status: not running"
        self.interfaces_item.title = "Interfaces: —"

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
        if require_bin("rnpath"):
            run_and_show(BINS["rnpath"] + ["-t"], "rnpath — Path Table")

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

    def cmd_rnprobe_lxmf(self, _):
        self._do_probe("lxmf.delivery", "LXMF Delivery")

    def cmd_rnprobe_nomad(self, _):
        self._do_probe("nomadnetwork.node", "NomadNet Node")

    def cmd_rnprobe_hash(self, _):
        self._do_probe(None, "Destination")

    def _do_probe(self, full_name, label):
        if not require_bin("rnprobe"):
            return
        dest = pick_contact_or_manual(f"rnprobe — {label}", action_label="Probe")
        if dest:
            cmd = list(BINS["rnprobe"])
            if full_name:
                cmd.append(full_name)
            cmd.append(dest)
            run_async_and_show(
                cmd,
                f"rnprobe — {label}",
                timeout=30,
                post_process=format_rnprobe,
            )

    # ── Nodebook ─────────────────────────────────────────────────

    def cmd_nodebook_clear(self, _):
        if PHONEBOOK.is_empty():
            show_alert("Nodebook", "No nodes to clear.")
            return
        PHONEBOOK.clear()
        show_alert("Nodebook", "All nodes cleared.")

    def cmd_nodebook_list(self, _):
        if PHONEBOOK.is_empty():
            show_alert(
                "Nodebook",
                "No nodes discovered yet.\n\n"
                "Nodes are auto-populated as RNS announces are received "
                "by rnsd. Wait a few minutes for nodes to announce themselves.",
            )
            return

        groups = PHONEBOOK.grouped()
        # Order: LXMF, NomadNet, then anything else (Unknown last)
        order = ["LXMF", "NomadNet"]
        ordered = [(t, groups[t]) for t in order if t in groups]
        for t in sorted(groups.keys()):
            if t not in order:
                ordered.append((t, groups[t]))

        section_titles = {
            "LXMF": "LXMF Nodes",
            "NomadNet": "NomadNet Nodes",
            "Unknown": "Other",
        }

        # Build an attributed string so we can dim the hash text
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

        first_section = True
        for type_, contacts in ordered:
            title = section_titles.get(type_, type_)
            header = f"{title}  ({len(contacts)})"
            if not first_section:
                append("\n\n", normal_attrs)
            first_section = False
            append(header + "\n", header_attrs)
            append("─" * len(header) + "\n", dim_attrs)
            for name, hash_ in contacts:
                append(f"  {name} ", normal_attrs)
                append(f"[{hash_}]", dim_attrs)
                append("\n", normal_attrs)

        show_alert(
            "Nodebook",
            attr.string(),
            monospace=True,
            width=760,
            attributed_message=attr,
        )

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
        self._stop_rnsd_process()
        rumps.quit_application()


if __name__ == "__main__":
    RNSDMenuBar().run()

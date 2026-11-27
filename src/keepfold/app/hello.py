"""The Private switch's check: Windows asks the user to verify with Windows Hello (face,
fingerprint or PIN), through `UserConsentVerifier` (winrt, called from the server process).

There is no Keepfold password. Without Windows Hello the switch says how to set it up: the Windows
password prompt (CredUI + LogonUser) is not used as a fallback, because it would hand the
account password to this process as an unwipeable Python string, fails for passwordless and
PIN-only Microsoft accounts, and its wrong tries count toward the account's lockout.

macOS asks through LocalAuthentication (Touch ID or the login password, shown by the OS) and
Linux through polkit's `pkcheck` on the .deb's own action (`auth_self`: the user's own password
or fingerprint, typed into the desktop's agent). For the same reasons, Keepfold never sees or
asks for a password on any OS.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time

MESSAGE = "Show private items in Keepfold"
SETUP = ("Set up Windows Hello (a PIN, face or fingerprint) in Settings > Accounts > Sign-in "
         "options, then try again.")
SETUP_MAC = "Set up Touch ID or a login password in System Settings, then try again."
CANCELLED = "Cancelled; private items stay hidden."
ACTION = "io.github.alenissacsam2.keepfold.unlock"  # packaging/linux/<ACTION minus .unlock>.policy
HOST = "Credential Dialog Xaml Host"  # the window class of Windows Hello's prompt
_busy = threading.Lock()  # one prompt at a time


def _la():
    """pyobjc's LocalAuthentication, only on a Mac that has it (tests replace this)."""
    import LocalAuthentication
    return LocalAuthentication


def _pkcheck(args: list[str]) -> int:
    """pkcheck's exit code, 127 when it is missing (tests replace this)."""
    try:
        return subprocess.run(["pkcheck", *args], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=120).returncode
    except subprocess.TimeoutExpired:
        return 1
    except OSError:
        return 127


def _mac_verify(message: str) -> tuple[bool, str | None]:
    try:
        la = _la()
    except ImportError:
        return False, ("This copy of Keepfold has no Touch ID support; use the Keepfold app "
                       "from the release page.")
    if not _busy.acquire(blocking=False):
        return False, "macOS is already asking; answer that prompt first."
    done, out = threading.Event(), []

    def reply(ok, err):  # called on LocalAuthentication's own queue
        out.append((bool(ok), None if err is None else err.code()))
        done.set()
    try:
        ctx = la.LAContext.alloc().init()
        ctx.evaluatePolicy_localizedReason_reply_(la.LAPolicyDeviceOwnerAuthentication,
                                                  message, reply)
        if not done.wait(120):
            ctx.invalidate()  # close the prompt nobody answered
            return False, "No answer in time; private items stay hidden."
    except Exception as e:  # noqa: BLE001  (a pyobjc call failed)
        return False, f"macOS did not answer ({type(e).__name__})."
    finally:
        _busy.release()
    ok, code = out[0]
    if ok:
        return True, None
    return False, {-2: CANCELLED,  # LAErrorUserCancel
                   -5: SETUP_MAC,  # LAErrorPasscodeNotSet
                   }.get(code, "macOS did not verify you; private items stay hidden.")


def _linux_verify(message: str) -> tuple[bool, str | None]:
    """`message` is unused: polkit shows the one in the .policy file."""
    if not _busy.acquire(blocking=False):
        return False, "Ubuntu is already asking; answer that prompt first."
    try:
        code = _pkcheck(["--action-id", ACTION, "--process", str(os.getpid()),
                         "--allow-user-interaction"])
    finally:
        _busy.release()
    # man pkcheck: 0 authorized, 1 not, 2 no agent, 3 dismissed, 127 error (incl. unknown action)
    if code == 0:
        return True, None
    return False, {3: CANCELLED,
                   127: "The Private switch needs the Keepfold app for Ubuntu (the .deb).",
                   }.get(code, "Ubuntu did not verify you; private items stay hidden.")


def _front(stop: threading.Event) -> None:
    """Bring Windows Hello's prompt in front of the app window (it can open behind it): this
    process lets any process take the foreground, then raises the prompt once it appears."""
    import ctypes
    user32 = ctypes.windll.user32
    user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    end = time.monotonic() + 5
    while not stop.is_set() and time.monotonic() < end:
        hwnd = user32.FindWindowW(HOST, None)
        if hwnd:
            user32.SetForegroundWindow(hwnd)
            return
        stop.wait(0.1)


def verify(message: str = MESSAGE) -> tuple[bool, str | None]:
    """(True, None) once Windows has verified the user; else (False, why) in words for them."""
    if sys.platform == "darwin":
        return _mac_verify(message)
    if sys.platform.startswith("linux"):
        return _linux_verify(message)
    if sys.platform != "win32":
        return False, "The Private switch needs Windows Hello, which only Windows has."
    try:
        from winrt.windows.security.credentials.ui import (
            UserConsentVerificationResult as Result,
        )
        from winrt.windows.security.credentials.ui import UserConsentVerifier as Verifier
        from winrt.windows.security.credentials.ui import (
            UserConsentVerifierAvailability as Availability,
        )
    except ImportError:
        return False, ("This copy of Keepfold has no Windows Hello support; reinstall Keepfold "
                       "to add it.")
    if not _busy.acquire(blocking=False):
        return False, "Windows Hello is already asking; answer that prompt first."
    stop = threading.Event()
    try:
        async def ask():
            if await Verifier.check_availability_async() != Availability.AVAILABLE:
                return None
            threading.Thread(target=_front, args=(stop,), daemon=True,
                             name="keepfold-hello-front").start()
            return await Verifier.request_verification_async(message)
        r = asyncio.run(ask())
    except Exception as e:  # noqa: BLE001  (a winrt call failed: OSError with its HRESULT)
        return False, f"Windows Hello did not answer ({type(e).__name__})."
    finally:
        stop.set()
        _busy.release()
    if r is None or r in (Result.NOT_CONFIGURED_FOR_USER, Result.DEVICE_NOT_PRESENT):
        return False, SETUP
    if r == Result.VERIFIED:
        return True, None
    return False, {Result.CANCELED: "Cancelled; private items stay hidden.",
                   Result.DISABLED_BY_POLICY: "Windows Hello is turned off on this computer by "
                                              "its administrator.",
                   Result.DEVICE_BUSY: "Windows Hello is busy; try again in a moment.",
                   Result.RETRIES_EXHAUSTED: "Too many tries; try again later.",
                   }.get(r, "Windows did not verify you; private items stay hidden.")

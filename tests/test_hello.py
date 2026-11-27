"""The Private switch's Windows Hello check (keepfold/app/hello.py) against a fake winrt module:
no test shows a real prompt (conftest also blocks `hello.verify` for every other test)."""

import sys
import types

import pytest

from keepfold.app import hello

VERIFY = hello.verify  # the real function; conftest replaces the module's for other tests
LEAF = "winrt.windows.security.credentials.ui"
A = types.SimpleNamespace(AVAILABLE=0, DEVICE_NOT_PRESENT=1, NOT_CONFIGURED_FOR_USER=2,
                          DISABLED_BY_POLICY=3, DEVICE_BUSY=4)
R = types.SimpleNamespace(VERIFIED=0, DEVICE_NOT_PRESENT=1, NOT_CONFIGURED_FOR_USER=2,
                          DISABLED_BY_POLICY=3, DEVICE_BUSY=4, RETRIES_EXHAUSTED=5, CANCELED=6)


@pytest.fixture
def winrt(monkeypatch):
    """A fake UserConsentVerifier on 'Windows'; set .avail and .result; .asked lists prompts."""
    state = types.SimpleNamespace(avail=A.AVAILABLE, result=R.VERIFIED, asked=[])

    class Verifier:
        @staticmethod
        async def check_availability_async():
            return state.avail

        @staticmethod
        async def request_verification_async(message):
            state.asked.append(message)
            return state.result
    mod = types.ModuleType(LEAF)
    mod.UserConsentVerifier = Verifier
    mod.UserConsentVerifierAvailability, mod.UserConsentVerificationResult = A, R
    monkeypatch.setitem(sys.modules, LEAF, mod)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(hello, "_front", lambda stop: None)  # no window to raise
    return state


def test_verified_unlocks_and_everything_else_says_why(winrt):
    assert VERIFY() == (True, None) and winrt.asked == [hello.MESSAGE]
    winrt.result = R.CANCELED
    assert VERIFY() == (False, "Cancelled; private items stay hidden.")
    winrt.result = R.RETRIES_EXHAUSTED
    assert VERIFY()[0] is False
    winrt.asked.clear()
    winrt.avail = A.NOT_CONFIGURED_FOR_USER  # no Hello: no prompt, and how to set it up
    assert VERIFY() == (False, hello.SETUP) and winrt.asked == []


def test_no_winrt_or_not_windows_refuses_with_no_password(winrt, monkeypatch):
    monkeypatch.setitem(sys.modules, LEAF, None)  # the package is missing: import fails
    ok, why = VERIFY()
    assert ok is False and "reinstall Keepfold" in why and winrt.asked == []
    monkeypatch.setattr(sys, "platform", "freebsd14")  # Mac and Linux have their own checks
    assert VERIFY() == (False, "The Private switch needs Windows Hello, which only Windows has.")


@pytest.fixture
def la(monkeypatch):
    """A fake LocalAuthentication on 'macOS' whose reply comes from another thread, as the real
    one does; set .ok and .code (an LAError code or None); .asked lists (policy, reason)."""
    import threading
    state = types.SimpleNamespace(ok=True, code=None, asked=[])

    class Err:
        def __init__(self, code):
            self._code = code

        def code(self):
            return self._code

    class Ctx:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def evaluatePolicy_localizedReason_reply_(self, policy, reason, reply):
            state.asked.append((policy, reason))
            err = None if state.code is None else Err(state.code)
            threading.Thread(target=reply, args=(state.ok, err)).start()

        def invalidate(self):
            pass
    mod = types.SimpleNamespace(LAContext=Ctx, LAPolicyDeviceOwnerAuthentication=2)
    monkeypatch.setattr(hello, "_la", lambda: mod)
    monkeypatch.setattr(sys, "platform", "darwin")
    return state


def test_mac_verified(la):
    assert VERIFY() == (True, None) and la.asked == [(2, hello.MESSAGE)]


def test_mac_cancel(la):
    la.ok, la.code = False, -2  # LAErrorUserCancel
    assert VERIFY() == (False, "Cancelled; private items stay hidden.")
    la.code = -5  # LAErrorPasscodeNotSet
    assert VERIFY() == (False, hello.SETUP_MAC)
    la.code = -1  # LAErrorAuthenticationFailed
    assert VERIFY() == (False, "macOS did not verify you; private items stay hidden.")


def test_mac_missing_tool(la, monkeypatch):
    def no_pyobjc():
        raise ImportError("No module named 'LocalAuthentication'")
    monkeypatch.setattr(hello, "_la", no_pyobjc)
    assert VERIFY() == (False, "This copy of Keepfold has no Touch ID support; use the Keepfold "
                               "app from the release page.")


@pytest.fixture
def pkcheck(monkeypatch):
    """A fake pkcheck on 'Linux'; set .code (its exit code); .args lists the calls."""
    state = types.SimpleNamespace(code=0, args=[])

    def run(args):
        state.args.append(args)
        return state.code
    monkeypatch.setattr(hello, "_pkcheck", run)
    monkeypatch.setattr(sys, "platform", "linux")
    return state


def test_linux_verified(pkcheck):
    import os
    assert VERIFY() == (True, None)
    assert pkcheck.args == [["--action-id", hello.ACTION, "--process", str(os.getpid()),
                             "--allow-user-interaction"]]
    assert hello.ACTION == "io.github.alenissacsam2.keepfold.unlock"


def test_linux_cancel_and_refusal(pkcheck):
    pkcheck.code = 3  # man pkcheck: the dialog was dismissed
    assert VERIFY() == (False, "Cancelled; private items stay hidden.")
    for code in (1, 2):  # not authorized; no authentication agent
        pkcheck.code = code
        assert VERIFY() == (False, "Ubuntu did not verify you; private items stay hidden.")


def test_linux_missing_tool(pkcheck):
    pkcheck.code = 127  # pkcheck missing, or the .deb's action is not registered
    assert VERIFY() == (False, "The Private switch needs the Keepfold app for Ubuntu (the .deb).")


def test_policy_file_is_auth_self():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    path = (Path(__file__).parent.parent / "packaging" / "linux"
            / "io.github.alenissacsam2.keepfold.policy")
    actions = ET.parse(path).getroot().findall("action")
    assert [a.get("id") for a in actions] == [hello.ACTION]
    action = actions[0]
    assert action.findtext("message") == hello.MESSAGE
    assert {d.tag: d.text for d in action.find("defaults")} == {
        "allow_any": "no", "allow_inactive": "no", "allow_active": "auth_self"}

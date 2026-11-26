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
    monkeypatch.setattr(sys, "platform", "linux")
    assert VERIFY() == (False, "The Private switch needs Windows Hello, which only Windows has.")

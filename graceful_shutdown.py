
# graceful_shutdown.py
import os
import sys
import signal
import atexit
import termios
from typing import Callable, Iterable, Optional


class ShutdownManager:
    """
    Centralized, idempotent shutdown for your Twisted app.

    You provide:
      - reactor: the Twisted reactor instance
      - client:  your ctrader_open_api Client (has startService/stopService)
      - get_subscribed_symbols(): -> Iterable[int]
      - unsubscribe_symbol(symbol_id: int) -> None
      - account_logout(): -> None  (best-effort; may no-op if not logged in)
      - stop_live_ui(): -> None    (stop Rich Live, set flags, etc.)

    Optional:
      - restore_tty() is handled if you call set_tty_old_settings(...)
        from your key-listener when entering cbreak mode.
    """

    def __init__(
        self,
        *,
        reactor,
        client,
        get_subscribed_symbols: Callable[[], Iterable[int]],
        unsubscribe_symbol: Callable[[int], None],
        account_logout: Callable[[], None],
        stop_live_ui: Callable[[], None],
    ):
        self.reactor = reactor
        self.client = client
        self.get_subscribed_symbols = get_subscribed_symbols
        self.unsubscribe_symbol = unsubscribe_symbol
        self.account_logout = account_logout
        self.stop_live_ui = stop_live_ui

        self._shutting_down = False
        self._tty_old_settings: Optional[tuple] = None

        # install atexit hook
        atexit.register(lambda: self.cleanup(reason="atexit"))


    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def install_signal_handlers(self) -> None:
        """Handle Ctrl+C, kill, hangup, *and* Ctrl+Z by cleaning up then exiting."""
        for sig_name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGTSTP"):
            _sig = getattr(signal, sig_name, None)
            if _sig is not None:
                signal.signal(_sig, self._on_signal)

    def set_tty_old_settings(self, old_settings: tuple) -> None:
        """Call this after tcgetattr(...) when you enter cbreak mode."""
        self._tty_old_settings = old_settings

    def clear_tty_old_settings(self) -> None:
        self._tty_old_settings = None

    def cleanup(self, *, reason: str = "signal") -> None:
        """Idempotent cleanup: safe to call multiple times."""
        if self._shutting_down:
            return
        self._shutting_down = True

        try:
            print(f"\n🧹 Cleaning up ({reason})…")

            # 1) stop UI / key threads
            try:
                self.stop_live_ui()
            except Exception:
                pass

            # 2) cancel any scheduled callLater jobs
            self._cancel_all_delayed_calls()

            # 3) politely unsubscribe from spots
            try:
                for sid in list(self.get_subscribed_symbols()):
                    try:
                        self.unsubscribe_symbol(int(sid))
                    except Exception:
                        pass
            except Exception:
                pass

            # 4) best-effort account logout
            try:
                self.account_logout()
            except Exception:
                pass

            # 5) stop client and reactor
            try:
                self.client.stopService()
            except Exception:
                pass

            try:
                if getattr(self.reactor, "running", False):
                    self.reactor.stop()
            except Exception:
                pass

            # 6) restore terminal mode if needed
            self._restore_tty()

        except Exception:
            # swallow all exceptions on shutdown
            pass

    def hard_exit(self, code: int = 0) -> None:
        """Cleanup then forcefully exit the interpreter (no lingering threads)."""
        self.cleanup(reason="hard-exit")
        os._exit(code)

    # ---------------- internals ----------------

    def _on_signal(self, signum, frame):
        # print light info, cleanup, then hard-exit
        try:
            sig_name = next(k for k, v in signal.__dict__.items() if v == signum and k.startswith("SIG"))
        except Exception:
            sig_name = f"{signum}"
        print(f"\n⚡ Received {sig_name}")
        self.hard_exit(0)

    def _restore_tty(self) -> None:
        if self._tty_old_settings is None:
            return
        try:
            fd = sys.stdin.fileno()
            termios.tcsetattr(fd, termios.TCSADRAIN, self._tty_old_settings)
        except Exception:
            pass
        finally:
            self._tty_old_settings = None

    def _cancel_all_delayed_calls(self) -> None:
        """Cancel reactor.callLater() timers if the reactor exposes getDelayedCalls()."""
        try:
            get_calls = getattr(self.reactor, "getDelayedCalls", None)
            if callable(get_calls):
                for dc in list(get_calls()):
                    try:
                        if dc.active():
                            dc.cancel()
                    except Exception:
                        pass
        except Exception:
            pass

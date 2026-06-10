"""Browser backend — Camoufox-only support for standalone use."""
from typing import Optional, Callable, Any


class BrowserBackendConfig:
    """Browser backend configuration."""
    def __init__(self):
        self.is_camoufox = True
        self.is_bitbrowser = False
        self.is_headless = False
        self.window_mode = "headed"
        self.bit_profile_id = None

    @staticmethod
    def camoufox(headless: bool = False) -> "BrowserBackendConfig":
        c = BrowserBackendConfig()
        c.is_headless = headless
        c.window_mode = "headless" if headless else "headed"
        return c


def open_browser_backend(
    launch_opts: dict,
    config: BrowserBackendConfig,
    camoufox_class: Any = None,
    log: Optional[Callable] = None,
):
    """Launch Camoufox browser. Returns a context manager."""
    if config.is_bitbrowser:
        raise NotImplementedError("BitBrowser backend not available in standalone mode")

    if camoufox_class is None:
        from camoufox.sync_api import Camoufox as camoufox_class

    # Build Camoufox launch args from launch_opts
    kwargs = {}
    for key in ("headless", "proxy", "geoip", "locale", "os", "screen",
                "humanize", "block_webrtc", "font", "window"):
        if key in launch_opts:
            kwargs[key] = launch_opts[key]

    # Handle addons/exclude_addons
    if "exclude_addons" in launch_opts:
        kwargs["exclude_addons"] = launch_opts["exclude_addons"]
    if "addons" in launch_opts:
        kwargs["addons"] = launch_opts["addons"]

    browser = camoufox_class(**kwargs)
    return browser  # Camoufox instance IS a context manager

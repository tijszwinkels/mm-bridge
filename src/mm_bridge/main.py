"""Entry point for the Mattermost ↔ VibeDeck bridge."""

import asyncio
import logging
import sys

from .bridge import Bridge
from .config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    config = Config.load()

    if not config.mm_bot_token:
        print("Error: MM_BOT_TOKEN environment variable is required")
        print()
        print("Usage:")
        print("  MM_BOT_TOKEN=<token> mm-bridge")
        print()
        print("Config precedence: built-in defaults < ~/.config/mm-bridge/config.toml < env vars")
        print()
        print("Environment variables:")
        print("  MM_BOT_TOKEN          - Mattermost bot token (required, env-only)")
        print("  MM_BRIDGE_CONFIG      - override config.toml path")
        print("  MM_BRIDGE_STATE       - override state.json path")
        print("  MM_URL / MM_PORT / MM_SCHEME / MM_TEAM")
        print("  VD_URL / VD_DEFAULT_CWD / VD_DEFAULT_BACKEND / VD_DEFAULT_MODEL")
        sys.exit(1)

    bridge = Bridge(config)

    async def run() -> None:
        try:
            await bridge.start()
        except KeyboardInterrupt:
            pass
        finally:
            await bridge.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()

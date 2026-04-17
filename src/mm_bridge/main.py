"""Entry point for the Mattermost ↔ VibeDeck bridge."""

import asyncio
import logging
import sys

from .config import Config
from .bridge import Bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    config = Config.from_env()

    if not config.mm_bot_token:
        print("Error: MM_BOT_TOKEN environment variable is required")
        print()
        print("Usage:")
        print("  MM_BOT_TOKEN=<token> mm-bridge")
        print()
        print("Environment variables:")
        print("  MM_BOT_TOKEN     - Mattermost bot token (required)")
        print("  MM_URL           - Mattermost host (default: localhost)")
        print("  MM_PORT          - Mattermost port (default: 8065)")
        print("  MM_TEAM          - Team name (default: workspace)")
        print("  VD_URL           - VibeDeck URL (default: http://localhost:8765)")
        print("  VD_DEFAULT_CWD   - Working dir for new sessions (default: ~)")
        print("  VD_NEW_SESSION_BACKEND     - Optional backend for MM-originated sessions")
        print("  VD_NEW_SESSION_MODEL_INDEX - Optional model index for MM-originated sessions")
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

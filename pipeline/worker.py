"""Temporal worker command entry point."""

import asyncio

from pipeline.temporal.worker import main

if __name__ == "__main__":
    asyncio.run(main())

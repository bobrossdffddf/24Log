# Discord ATC24 Flight Plan Monitor Bot

## Overview
This is a Discord bot that monitors flight plans from ATC24 (a flight simulation platform) and sends real-time notifications to Discord servers when aircraft with specific callsign prefixes file flight plans.

## Project Purpose
- Monitor ATC24 WebSocket feeds for new flight plans
- Filter flight plans by airline callsign prefixes (e.g., SWA, UAL, DAL)
- Send formatted Discord embeds with flight plan details to configured channels
- Support multiple Discord servers with individual configurations

## Current State
- ✅ Python project using uv package manager
- ✅ Dependencies installed (discord.py, aiohttp, websockets, etc.)
- ✅ SQLite database for storing server configurations
- ✅ Slash commands for setup, configuration, and status
- ⚠️  Requires Discord bot token configuration
- ⚠️  Needs workflow setup for continuous operation

## Project Architecture
- **Language**: Python 3.11
- **Package Manager**: uv (with pyproject.toml and uv.lock)
- **Database**: SQLite (bot_config.db)
- **Main Components**:
  - Discord bot with slash commands (`/setup`, `/remove`, `/status`)
  - WebSocket client for ATC24 flight plan monitoring
  - SQLite database for persistent server configurations
  - Async task loop for continuous monitoring

## Key Features
1. **Multi-server support**: Each Discord server can configure its own callsign prefixes and notification channels
2. **Real-time monitoring**: Connects to ATC24 WebSocket for live flight plan updates
3. **Duplicate prevention**: Tracks processed flight plans to avoid spam
4. **Rich notifications**: Sends formatted embeds with flight details (callsign, pilot, aircraft, route, etc.)

## Dependencies
- `discord.py`: Discord bot framework
- `aiohttp`: HTTP client for API calls
- `websockets`: WebSocket client for ATC24 connection
- `asyncio-mqtt`: MQTT client (if needed)
- `websocket-client`: Additional WebSocket support

## Configuration Requirements
- `DISCORD_BOT_TOKEN`: Discord bot token (environment variable)
- Bot requires proper Discord permissions (Send Messages, Embed Links)

## Recent Changes
- 2025-09-19: Project imported from GitHub and configured for Replit environment
- 2025-09-19: Dependencies installed using uv package manager
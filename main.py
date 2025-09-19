import asyncio
import json
import os
import sqlite3
from typing import Dict, Set, Optional
import discord
from discord.ext import commands, tasks
import aiohttp
import websockets
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database setup
def init_database():
    """Initialize SQLite database for storing server configurations"""
    conn = sqlite3.connect('bot_config.db')
    cursor = conn.cursor()
    
    # Create table for storing server configurations
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS server_configs (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            callsign_prefixes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

class FlightPlanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Remove privileged intents requirement since we don't need them
        # intents.message_content = True  # Not needed for slash commands
        super().__init__(command_prefix='!', intents=intents)
        
        # Store active configurations
        self.server_configs: Dict[int, Dict] = {}
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.websocket_connection = None
        self.processed_flight_plans = set()  # Track processed flight plans to avoid duplicates
        
    async def setup_hook(self):
        """Called when the bot is starting up"""
        logger.info("Bot is starting up...")
        
        # Initialize HTTP session
        self.http_session = aiohttp.ClientSession()
        
        # Initialize database
        init_database()
        
        # Load existing configurations
        await self.load_configurations()
        
        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def close(self):
        """Clean up resources when bot shuts down"""
        if self.http_session:
            await self.http_session.close()
        await super().close()
        
    
    async def load_configurations(self):
        """Load server configurations from database"""
        conn = sqlite3.connect('bot_config.db')
        cursor = conn.cursor()
        
        cursor.execute("SELECT guild_id, channel_id, callsign_prefixes FROM server_configs")
        rows = cursor.fetchall()
        
        for guild_id, channel_id, callsign_prefixes in rows:
            prefixes = json.loads(callsign_prefixes) if callsign_prefixes else []
            self.server_configs[guild_id] = {
                'channel_id': channel_id,
                'callsign_prefixes': prefixes
            }
        
        conn.close()
        logger.info(f"Loaded {len(self.server_configs)} server configurations")
    
    async def save_configuration(self, guild_id: int, channel_id: int, prefixes: list):
        """Save server configuration to database"""
        conn = sqlite3.connect('bot_config.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO server_configs 
            (guild_id, channel_id, callsign_prefixes, updated_at) 
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (guild_id, channel_id, json.dumps(prefixes)))
        
        conn.commit()
        conn.close()
        
        # Update in-memory configuration
        self.server_configs[guild_id] = {
            'channel_id': channel_id,
            'callsign_prefixes': prefixes
        }
    
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f'{self.user} has logged in!')
        
        # Start ATC 24 flight plan monitoring via WebSocket
        if not flight_plan_monitor.is_running():
            flight_plan_monitor.start()

# Create bot instance
bot = FlightPlanBot()

@bot.tree.command(name="setup", description="Configure flight plan monitoring for this server")
async def setup_command(interaction: discord.Interaction, callsign_prefix: str, channel: Optional[discord.TextChannel] = None):
    """
    Set up flight plan monitoring for a callsign prefix
    
    Parameters:
    - callsign_prefix: The airline callsign prefix to monitor (e.g., SWA, UAL, DAL)
    - channel: The channel to send notifications to (optional, defaults to current channel)
    """
    # Check if user has manage server permissions
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You need 'Manage Server' permissions to use this command.", ephemeral=True)
        return
    
    # Use current channel if none specified
    if channel is None:
        if isinstance(interaction.channel, discord.TextChannel):
            channel = interaction.channel
        else:
            await interaction.response.send_message("❌ Please specify a text channel or run this command in a text channel.", ephemeral=True)
            return
    
    # Validate callsign prefix
    callsign_prefix = callsign_prefix.upper().strip()
    if not callsign_prefix or len(callsign_prefix) < 2:
        await interaction.response.send_message("❌ Please provide a valid callsign prefix (at least 2 characters).", ephemeral=True)
        return
    
    # Get existing configuration or create new one
    guild_id = interaction.guild.id
    existing_config = bot.server_configs.get(guild_id, {'channel_id': channel.id, 'callsign_prefixes': []})
    
    # Add new prefix if not already present
    if callsign_prefix not in existing_config['callsign_prefixes']:
        existing_config['callsign_prefixes'].append(callsign_prefix)
    
    # Update channel
    existing_config['channel_id'] = channel.id
    
    # Save configuration
    await bot.save_configuration(guild_id, channel.id, existing_config['callsign_prefixes'])
    
    embed = discord.Embed(
        title="✅ Flight Plan Monitoring Configured",
        color=0x00ff00,
        description=f"Now monitoring callsign prefix: **{callsign_prefix}**"
    )
    embed.add_field(name="Channel", value=channel.mention, inline=True)
    embed.add_field(name="All Monitored Prefixes", value=", ".join(existing_config['callsign_prefixes']), inline=True)
    embed.set_footer(text="Flight plan notifications will be posted when aircraft with matching callsigns file flight plans.")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="remove", description="Remove a callsign prefix from monitoring")
async def remove_command(interaction: discord.Interaction, callsign_prefix: str):
    """Remove a callsign prefix from monitoring"""
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You need 'Manage Server' permissions to use this command.", ephemeral=True)
        return
    
    guild_id = interaction.guild.id
    config = bot.server_configs.get(guild_id)
    
    if not config:
        await interaction.response.send_message("❌ No monitoring configuration found for this server.", ephemeral=True)
        return
    
    callsign_prefix = callsign_prefix.upper().strip()
    
    if callsign_prefix not in config['callsign_prefixes']:
        await interaction.response.send_message(f"❌ Callsign prefix **{callsign_prefix}** is not being monitored.", ephemeral=True)
        return
    
    # Remove prefix
    config['callsign_prefixes'].remove(callsign_prefix)
    
    # Save updated configuration
    await bot.save_configuration(guild_id, config['channel_id'], config['callsign_prefixes'])
    
    embed = discord.Embed(
        title="✅ Callsign Prefix Removed",
        color=0xff9900,
        description=f"Removed **{callsign_prefix}** from monitoring"
    )
    if config['callsign_prefixes']:
        embed.add_field(name="Remaining Monitored Prefixes", value=", ".join(config['callsign_prefixes']), inline=True)
    else:
        embed.add_field(name="Status", value="No prefixes are currently being monitored", inline=True)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="status", description="Show current monitoring configuration for this server")
async def status_command(interaction: discord.Interaction):
    """Show current monitoring configuration"""
    if not interaction.guild:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return
    
    guild_id = interaction.guild.id
    config = bot.server_configs.get(guild_id)
    
    if not config or not config['callsign_prefixes']:
        embed = discord.Embed(
            title="📊 Monitoring Status",
            color=0x999999,
            description="No flight plan monitoring is currently configured for this server."
        )
        embed.add_field(name="Setup", value="Use `/setup <callsign_prefix>` to start monitoring", inline=False)
    else:
        channel = bot.get_channel(config['channel_id'])
        channel_mention = channel.mention if channel and isinstance(channel, discord.TextChannel) else f"<#{config['channel_id']}> (Channel not found)"
        
        embed = discord.Embed(
            title="📊 Monitoring Status",
            color=0x00ff00,
            description="Flight plan monitoring is active"
        )
        embed.add_field(name="Monitored Prefixes", value=", ".join(config['callsign_prefixes']), inline=True)
        embed.add_field(name="Notification Channel", value=channel_mention, inline=True)
    
    await interaction.response.send_message(embed=embed)

@tasks.loop(reconnect=True)
async def flight_plan_monitor():
    """Monitor ATC 24 WebSocket for new flight plans"""
    if not bot.server_configs:
        await asyncio.sleep(10)  # Wait if no configurations
        return
    
    # ATC24 WebSocket endpoint - you may need to update this URL
    websocket_urls = [
        "wss://24data.ptfs.app/ws",
        "wss://24data.ptfs.app/websocket", 
        "wss://24data.ptfs.app/api/ws",
        "wss://24data.ptfs.app/live"
    ]
    
    for ws_url in websocket_urls:
        try:
            logger.info(f"Attempting to connect to WebSocket: {ws_url}")
            async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as websocket:
                logger.info(f"Successfully connected to ATC24 WebSocket: {ws_url}")
                bot.websocket_connection = websocket
                
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        await process_flight_plan(data)
                    except json.JSONDecodeError:
                        logger.warning(f"Received invalid JSON from WebSocket: {message[:100]}...")
                    except Exception as e:
                        logger.error(f"Error processing WebSocket message: {e}")
                        
        except websockets.exceptions.ConnectionClosed:
            logger.warning(f"WebSocket connection closed: {ws_url}")
            await asyncio.sleep(5)  # Wait before reconnecting
            break
        except websockets.exceptions.InvalidURI:
            logger.warning(f"Invalid WebSocket URI: {ws_url}")
            continue  # Try next URL
        except Exception as e:
            logger.error(f"WebSocket connection error for {ws_url}: {e}")
            await asyncio.sleep(5)
            continue  # Try next URL
    
    # If all URLs failed, wait before trying again
    logger.warning("All WebSocket URLs failed, waiting 30 seconds before retry")
    await asyncio.sleep(30)

async def process_flight_plan(flight_plan_data):
    """Process flight plan data and send notifications for matching callsigns"""
    # Handle both single flight plan and array of flight plans
    flight_plans = []
    if isinstance(flight_plan_data, list):
        flight_plans = flight_plan_data
    elif isinstance(flight_plan_data, dict):
        # Check if it's a single flight plan
        if 'callsign' in flight_plan_data:
            flight_plans = [flight_plan_data]
        else:
            # Might be a wrapper object, check common wrapper keys
            for key in ['flightPlan', 'data', 'flight_plan']:
                if key in flight_plan_data:
                    data = flight_plan_data[key]
                    if isinstance(data, list):
                        flight_plans = data
                    elif isinstance(data, dict) and 'callsign' in data:
                        flight_plans = [data]
                    break
    
    if not flight_plans:
        logger.debug(f"No flight plans found in data: {flight_plan_data}")
        return
    
    # Process each flight plan
    for flight_plan in flight_plans:
        if not isinstance(flight_plan, dict) or 'callsign' not in flight_plan:
            continue
            
        callsign = flight_plan['callsign']
        
        # Create unique identifier for this flight plan to avoid duplicates
        flight_plan_id = f"{callsign}_{flight_plan.get('robloxName', '')}_{flight_plan.get('departing', '')}_{flight_plan.get('arriving', '')}"
        
        if flight_plan_id in bot.processed_flight_plans:
            continue  # Already processed this flight plan
            
        bot.processed_flight_plans.add(flight_plan_id)
        
        # Find matching servers and prefixes
        matching_configs = []
        for guild_id, config in bot.server_configs.items():
            for prefix in config['callsign_prefixes']:
                if callsign.upper().startswith(prefix.upper()):
                    matching_configs.append((guild_id, config, prefix))
                    break
        
        if matching_configs:
            logger.info(f"New flight plan filed: {callsign} by {flight_plan.get('robloxName', 'Unknown')}")
            await send_flight_plan_notification(flight_plan, matching_configs)

async def send_flight_plan_notification(flight_plan, matching_configs):
    """Send flight plan notification to Discord channels"""
    callsign = flight_plan.get('callsign', 'Unknown')
    
    # Create embed for flight plan notification
    embed = discord.Embed(
        title="✈️ New Flight Plan Filed",
        color=0x00ff00,
        timestamp=discord.utils.utcnow()
    )
    
    embed.description = f"Flight **{callsign}** has filed a flight plan"
    
    embed.add_field(name="Callsign", value=f"**{callsign}**", inline=True)
    embed.add_field(name="Pilot", value=flight_plan.get('robloxName', 'Unknown'), inline=True)
    
    # Add flight plan details
    if 'aircraft' in flight_plan:
        embed.add_field(name="Aircraft", value=flight_plan['aircraft'], inline=True)
    
    if 'departing' in flight_plan:
        embed.add_field(name="Departure", value=flight_plan['departing'], inline=True)
    
    if 'arriving' in flight_plan:
        embed.add_field(name="Arrival", value=flight_plan['arriving'], inline=True)
    
    if 'flightlevel' in flight_plan:
        embed.add_field(name="Flight Level", value=f"FL{flight_plan['flightlevel']}", inline=True)
    
    if 'flightrules' in flight_plan:
        embed.add_field(name="Flight Rules", value=flight_plan['flightrules'], inline=True)
    
    if 'route' in flight_plan and flight_plan['route'] != 'N/A':
        embed.add_field(name="Route", value=flight_plan['route'], inline=False)
    
    if 'realcallsign' in flight_plan and flight_plan['realcallsign'] != callsign:
        embed.add_field(name="Real Callsign", value=flight_plan['realcallsign'], inline=True)
    
    embed.set_footer(text="ATC24 Flight Plan Monitor")
    
    # Send to all matching channels
    for guild_id, config, matched_prefix in matching_configs:
        try:
            channel = bot.get_channel(config['channel_id'])
            if channel and isinstance(channel, discord.TextChannel):
                # Check bot permissions before sending
                permissions = channel.permissions_for(channel.guild.me)
                if permissions.send_messages and permissions.embed_links:
                    await channel.send(embed=embed)
                    logger.info(f"Sent flight plan notification for {callsign} (prefix: {matched_prefix}) to guild {guild_id}")
                else:
                    logger.warning(f"Missing permissions to send embeds in channel {config['channel_id']} for guild {guild_id}")
            else:
                logger.warning(f"Could not find channel {config['channel_id']} for guild {guild_id}")
        except Exception as e:
            logger.error(f"Error sending notification to guild {guild_id}: {e}")

# The monitor task is defined globally and will be started in on_ready

if __name__ == "__main__":
    # Get bot token from environment
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        logger.error("DISCORD_BOT_TOKEN environment variable not set!")
        exit(1)
    
    # Add some debug logging
    logger.info("Starting Discord bot...")
    
    try:
        # Run the bot
        bot.run(token, log_level=logging.INFO)
    except Exception as e:
        logger.error("Failed to start bot: %s", e)
        raise
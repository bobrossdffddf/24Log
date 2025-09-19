import asyncio
import json
import os
import sqlite3
from typing import Dict, Set, Optional
import discord
from discord.ext import commands, tasks
import aiohttp
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
        self.previous_aircraft_states = {}
        
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
        
        # Start ATC 24 monitoring
        if not atc24_monitor.is_running():
            atc24_monitor.start()

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

@tasks.loop(seconds=3)  # Poll every 3 seconds as recommended by ATC24 API
async def atc24_monitor():
    """Monitor ATC 24 API for new aircraft (flight plans)"""
    if not bot.server_configs:
        return
    
    try:
        if not bot.http_session:
            logger.error("HTTP session not initialized")
            return
            
        # Get data from both main server and event server
        main_data = await fetch_aircraft_data(bot.http_session, 'https://24data.ptfs.app/acft-data')
        event_data = await fetch_aircraft_data(bot.http_session, 'https://24data.ptfs.app/acft-data/event')
        
        if main_data:
            await process_flight_data(main_data, 'Main Server')
        if event_data:
            await process_flight_data(event_data, 'Event Server')
                
    except Exception as e:
        logger.error(f"Error monitoring ATC24: {e}")

async def fetch_aircraft_data(session, url):
    """Fetch aircraft data from ATC24 API with proper error handling and rate limiting"""
    max_retries = 3
    base_delay = 1
    
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    # Validate response structure - ATC24 returns dict with callsigns as keys
                    if isinstance(data, dict):
                        # Convert dict to list format for easier processing
                        aircraft_list = []
                        for callsign, aircraft_data in data.items():
                            aircraft_data['callsign'] = callsign
                            aircraft_list.append(aircraft_data)
                        return aircraft_list
                    elif isinstance(data, list):
                        return data
                    else:
                        logger.warning(f"Unexpected data format from {url}: {type(data)}")
                        return None
                elif response.status == 429:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"ATC24 API rate limit hit - backing off for {delay}s (attempt {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(delay)
                        continue
                    return None
                else:
                    logger.warning(f"Failed to fetch ATC24 data from {url}: HTTP {response.status}")
                    return None
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching data from {url} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(base_delay * (attempt + 1))
                continue
            return None
        except Exception as e:
            logger.error(f"Error fetching data from {url}: {e} (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(base_delay * (attempt + 1))
                continue
            return None
    
    return None

async def process_flight_data(aircraft_list, server_name):
    """Process aircraft data and send notifications for new matching callsigns"""
    if not isinstance(aircraft_list, list):
        logger.warning(f"Expected list from ATC24 API, got {type(aircraft_list)}")
        return
    
    current_aircraft = set()
    
    # Extract current aircraft callsigns with validation
    for aircraft in aircraft_list:
        if isinstance(aircraft, dict):
            # Handle different possible field names for callsign
            callsign = aircraft.get('callsign') or aircraft.get('call_sign') or aircraft.get('flight_id')
            if callsign and isinstance(callsign, str) and callsign.strip():
                current_aircraft.add(callsign.strip())
    
    # Check for new aircraft against previous state
    server_key = server_name.lower().replace(" ", "_")
    previous_aircraft = bot.previous_aircraft_states.get(server_key, set())
    new_aircraft = current_aircraft - previous_aircraft
    
    # Store current state for next comparison
    bot.previous_aircraft_states[server_key] = current_aircraft
    
    if not new_aircraft:
        return
    
    logger.info(f"Detected {len(new_aircraft)} new aircraft in {server_name}: {', '.join(new_aircraft)}")
    
    # Check each new aircraft against server configurations
    for aircraft in aircraft_list:
        if not isinstance(aircraft, dict) or 'callsign' not in aircraft:
            continue
            
        callsign = aircraft['callsign']
        if callsign not in new_aircraft:
            continue
            
        # Find matching servers and prefixes
        matching_configs = []
        for guild_id, config in bot.server_configs.items():
            for prefix in config['callsign_prefixes']:
                if callsign.upper().startswith(prefix.upper()):
                    matching_configs.append((guild_id, config, prefix))
                    break
        
        if matching_configs:
            await send_flight_plan_notification(aircraft, server_name, matching_configs)

async def send_flight_plan_notification(aircraft, server_name, matching_configs):
    """Send flight plan notification to Discord channels"""
    callsign = aircraft.get('callsign', 'Unknown')
    
    # Create embed for flight plan notification
    embed = discord.Embed(
        title="✈️ New Aircraft Detected",
        color=0x00ff00,
        timestamp=discord.utils.utcnow()
    )
    
    embed.description = f"Aircraft **{callsign}** has spawned and is now online"
    
    embed.add_field(name="Callsign", value=f"**{callsign}**", inline=True)
    embed.add_field(name="Server", value=server_name, inline=True)
    
    # Add additional aircraft information if available (using ATC24 API field names)
    if 'aircraftType' in aircraft:
        embed.add_field(name="Aircraft", value=aircraft['aircraftType'], inline=True)
    
    if 'playerName' in aircraft:
        embed.add_field(name="Pilot", value=aircraft['playerName'], inline=True)
    
    if 'altitude' in aircraft:
        embed.add_field(name="Altitude", value=f"{aircraft['altitude']} ft", inline=True)
    
    if 'speed' in aircraft:
        embed.add_field(name="Speed", value=f"{aircraft['speed']} kts", inline=True)
    
    if 'groundSpeed' in aircraft:
        embed.add_field(name="Ground Speed", value=f"{aircraft['groundSpeed']:.0f} kts", inline=True)
    
    if 'isOnGround' in aircraft:
        status = "On Ground" if aircraft['isOnGround'] else "In Flight"
        embed.add_field(name="Status", value=status, inline=True)
    
    embed.set_footer(text=f"ATC24 Aircraft Monitor • {server_name}")
    
    # Send to all matching channels
    for guild_id, config, matched_prefix in matching_configs:
        try:
            channel = bot.get_channel(config['channel_id'])
            if channel and isinstance(channel, discord.TextChannel):
                # Check bot permissions before sending
                permissions = channel.permissions_for(channel.guild.me)
                if permissions.send_messages and permissions.embed_links:
                    await channel.send(embed=embed)
                    logger.info(f"Sent aircraft notification for {callsign} (prefix: {matched_prefix}) to guild {guild_id}")
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
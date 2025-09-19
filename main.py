import asyncio
import json
import os
import sqlite3
from typing import Dict, Set, Optional
from collections import deque
import discord
from discord.ext import commands, tasks
import aiohttp
import websockets
import logging

# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv not installed, skip loading .env file
    pass

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
            embed_color INTEGER DEFAULT 65280,
            embed_title TEXT DEFAULT "✈️ New Flight Plan Filed",
            embed_thumbnail TEXT,
            embed_image TEXT,
            show_callsign BOOLEAN DEFAULT 1,
            show_pilot BOOLEAN DEFAULT 1,
            show_aircraft BOOLEAN DEFAULT 1,
            show_departure BOOLEAN DEFAULT 1,
            show_arrival BOOLEAN DEFAULT 1,
            show_flightlevel BOOLEAN DEFAULT 1,
            show_flightrules BOOLEAN DEFAULT 1,
            show_route BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Add columns to existing table if they don't exist (for database migration)
    columns_to_add = [
        ('embed_color', 'INTEGER DEFAULT 65280'),
        ('embed_title', 'TEXT DEFAULT "✈️ New Flight Plan Filed"'),
        ('embed_thumbnail', 'TEXT'),
        ('embed_image', 'TEXT'),
        ('show_callsign', 'BOOLEAN DEFAULT 1'),
        ('show_pilot', 'BOOLEAN DEFAULT 1'),
        ('show_aircraft', 'BOOLEAN DEFAULT 1'),
        ('show_departure', 'BOOLEAN DEFAULT 1'),
        ('show_arrival', 'BOOLEAN DEFAULT 1'),
        ('show_flightlevel', 'BOOLEAN DEFAULT 1'),
        ('show_flightrules', 'BOOLEAN DEFAULT 1'),
        ('show_route', 'BOOLEAN DEFAULT 1')
    ]
    
    for column_name, column_definition in columns_to_add:
        try:
            cursor.execute(f'ALTER TABLE server_configs ADD COLUMN {column_name} {column_definition}')
        except sqlite3.OperationalError:
            # Column already exists, skip
            pass
    
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
        self.processed_flight_plans = deque(maxlen=500)  # Track processed flight plans to avoid duplicates (max 500)
        
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
        
        cursor.execute("""
            SELECT guild_id, channel_id, callsign_prefixes, embed_color, embed_title, 
                   embed_thumbnail, embed_image, show_callsign, show_pilot, show_aircraft,
                   show_departure, show_arrival, show_flightlevel, show_flightrules, show_route
            FROM server_configs
        """)
        rows = cursor.fetchall()
        
        for row in rows:
            (guild_id, channel_id, callsign_prefixes, embed_color, embed_title, 
             embed_thumbnail, embed_image, show_callsign, show_pilot, show_aircraft,
             show_departure, show_arrival, show_flightlevel, show_flightrules, show_route) = row
             
            prefixes = json.loads(callsign_prefixes) if callsign_prefixes else []
            self.server_configs[guild_id] = {
                'channel_id': channel_id,
                'callsign_prefixes': prefixes,
                'embed_color': embed_color or 65280,
                'embed_title': embed_title or "✈️ New Flight Plan Filed",
                'embed_thumbnail': embed_thumbnail,
                'embed_image': embed_image,
                'show_callsign': bool(show_callsign),
                'show_pilot': bool(show_pilot),
                'show_aircraft': bool(show_aircraft),
                'show_departure': bool(show_departure),
                'show_arrival': bool(show_arrival),
                'show_flightlevel': bool(show_flightlevel),
                'show_flightrules': bool(show_flightrules),
                'show_route': bool(show_route)
            }
        
        conn.close()
        logger.info(f"Loaded {len(self.server_configs)} server configurations")
    
    async def save_configuration(self, guild_id: int, channel_id: int, prefixes: list):
        """Save server configuration to database"""
        conn = sqlite3.connect('bot_config.db')
        cursor = conn.cursor()
        
        # Get existing configuration to preserve embed settings
        existing_config = self.server_configs.get(guild_id, {})
        
        cursor.execute('''
            INSERT OR REPLACE INTO server_configs 
            (guild_id, channel_id, callsign_prefixes, embed_color, embed_title,
             embed_thumbnail, embed_image, show_callsign, show_pilot, show_aircraft,
             show_departure, show_arrival, show_flightlevel, show_flightrules, show_route, updated_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            guild_id, channel_id, json.dumps(prefixes),
            existing_config.get('embed_color', 65280),
            existing_config.get('embed_title', '✈️ New Flight Plan Filed'),
            existing_config.get('embed_thumbnail'),
            existing_config.get('embed_image'),
            existing_config.get('show_callsign', True),
            existing_config.get('show_pilot', True),
            existing_config.get('show_aircraft', True),
            existing_config.get('show_departure', True),
            existing_config.get('show_arrival', True),
            existing_config.get('show_flightlevel', True),
            existing_config.get('show_flightrules', True),
            existing_config.get('show_route', True)
        ))
        
        conn.commit()
        conn.close()
        
        # Update in-memory configuration
        if guild_id not in self.server_configs:
            self.server_configs[guild_id] = {}
        self.server_configs[guild_id].update({
            'channel_id': channel_id,
            'callsign_prefixes': prefixes
        })
    
    async def save_embed_configuration(self, guild_id: int, **kwargs):
        """Save embed configuration to database"""
        conn = sqlite3.connect('bot_config.db')
        cursor = conn.cursor()
        
        # Get current configuration
        current_config = self.server_configs.get(guild_id, {})
        
        # Update with new values
        embed_color = kwargs.get('embed_color', current_config.get('embed_color', 65280))
        embed_title = kwargs.get('embed_title', current_config.get('embed_title', '✈️ New Flight Plan Filed'))
        embed_thumbnail = kwargs.get('embed_thumbnail', current_config.get('embed_thumbnail'))
        embed_image = kwargs.get('embed_image', current_config.get('embed_image'))
        show_callsign = kwargs.get('show_callsign', current_config.get('show_callsign', True))
        show_pilot = kwargs.get('show_pilot', current_config.get('show_pilot', True))
        show_aircraft = kwargs.get('show_aircraft', current_config.get('show_aircraft', True))
        show_departure = kwargs.get('show_departure', current_config.get('show_departure', True))
        show_arrival = kwargs.get('show_arrival', current_config.get('show_arrival', True))
        show_flightlevel = kwargs.get('show_flightlevel', current_config.get('show_flightlevel', True))
        show_flightrules = kwargs.get('show_flightrules', current_config.get('show_flightrules', True))
        show_route = kwargs.get('show_route', current_config.get('show_route', True))
        
        cursor.execute('''
            UPDATE server_configs SET 
            embed_color=?, embed_title=?, embed_thumbnail=?, embed_image=?,
            show_callsign=?, show_pilot=?, show_aircraft=?, show_departure=?, 
            show_arrival=?, show_flightlevel=?, show_flightrules=?, show_route=?,
            updated_at=CURRENT_TIMESTAMP
            WHERE guild_id=?
        ''', (
            embed_color, embed_title, embed_thumbnail, embed_image,
            show_callsign, show_pilot, show_aircraft, show_departure,
            show_arrival, show_flightlevel, show_flightrules, show_route,
            guild_id
        ))
        
        conn.commit()
        conn.close()
        
        # Update in-memory configuration
        if guild_id not in self.server_configs:
            self.server_configs[guild_id] = {}
        
        self.server_configs[guild_id].update({
            'embed_color': embed_color,
            'embed_title': embed_title,
            'embed_thumbnail': embed_thumbnail,
            'embed_image': embed_image,
            'show_callsign': show_callsign,
            'show_pilot': show_pilot,
            'show_aircraft': show_aircraft,
            'show_departure': show_departure,
            'show_arrival': show_arrival,
            'show_flightlevel': show_flightlevel,
            'show_flightrules': show_flightrules,
            'show_route': show_route
        })
    
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f'{self.user} has logged in!')
        
        # Start ATC 24 flight plan monitoring via WebSocket
        if not flight_plan_monitor.is_running():
            flight_plan_monitor.start()

# Create bot instance
bot = FlightPlanBot()

@bot.tree.command(name="setup", description="Configure flight plan monitoring for this server")
async def setup_command(interaction: discord.Interaction, callsign_prefix: str, channel: discord.TextChannel):
    """
    Set up flight plan monitoring for a callsign prefix
    
    Parameters:
    - callsign_prefix: The airline callsign prefix to monitor (e.g., SWA, UAL, DAL)
    - channel: The channel to send notifications to (required)
    """
    # Check if user has administrator permissions
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need 'Administrator' permissions to use this command.", ephemeral=True)
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
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need 'Administrator' permissions to use this command.", ephemeral=True)
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

@bot.tree.command(name="config", description="Configure embed appearance and field visibility")
async def config_command(interaction: discord.Interaction, 
                        embed_color: Optional[str] = None,
                        embed_title: Optional[str] = None, 
                        embed_thumbnail: Optional[str] = None,
                        embed_image: Optional[str] = None,
                        show_callsign: Optional[bool] = None,
                        show_pilot: Optional[bool] = None,
                        show_aircraft: Optional[bool] = None,
                        show_departure: Optional[bool] = None,
                        show_arrival: Optional[bool] = None,
                        show_flightlevel: Optional[bool] = None,
                        show_flightrules: Optional[bool] = None,
                        show_route: Optional[bool] = None):
    """
    Configure embed appearance and field visibility for flight plan notifications
    
    Parameters:
    - embed_color: Hex color code (e.g., #00FF00 or 0x00FF00)
    - embed_title: Custom title for flight plan embeds
    - embed_thumbnail: URL for thumbnail image
    - embed_image: URL for main embed image
    - show_callsign: Show/hide callsign field
    - show_pilot: Show/hide pilot name field
    - show_aircraft: Show/hide aircraft type field
    - show_departure: Show/hide departure airport field
    - show_arrival: Show/hide arrival airport field
    - show_flightlevel: Show/hide flight level field
    - show_flightrules: Show/hide flight rules field
    - show_route: Show/hide route field
    """
    # Check if user has administrator permissions
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need 'Administrator' permissions to use this command.", ephemeral=True)
        return
    
    guild_id = interaction.guild.id
    config = bot.server_configs.get(guild_id)
    
    if not config:
        await interaction.response.send_message("❌ No monitoring configuration found for this server. Please use `/setup` first.", ephemeral=True)
        return
    
    # Parse color if provided
    parsed_color = None
    if embed_color:
        try:
            # Remove # if present and convert to int
            color_str = embed_color.strip().lstrip('#')
            if color_str.startswith('0x'):
                parsed_color = int(color_str, 16)
            else:
                parsed_color = int(color_str, 16)
        except ValueError:
            await interaction.response.send_message("❌ Invalid color format. Use hex format like #00FF00 or 0x00FF00", ephemeral=True)
            return
    
    # Validate URLs if provided
    if embed_thumbnail and not (embed_thumbnail.startswith('http://') or embed_thumbnail.startswith('https://')):
        await interaction.response.send_message("❌ Thumbnail URL must start with http:// or https://", ephemeral=True)
        return
    
    if embed_image and not (embed_image.startswith('http://') or embed_image.startswith('https://')):
        await interaction.response.send_message("❌ Image URL must start with http:// or https://", ephemeral=True)
        return
    
    # Build update parameters
    update_params = {}
    if parsed_color is not None:
        update_params['embed_color'] = parsed_color
    if embed_title is not None:
        update_params['embed_title'] = embed_title
    if embed_thumbnail is not None:
        update_params['embed_thumbnail'] = embed_thumbnail
    if embed_image is not None:
        update_params['embed_image'] = embed_image
    if show_callsign is not None:
        update_params['show_callsign'] = show_callsign
    if show_pilot is not None:
        update_params['show_pilot'] = show_pilot
    if show_aircraft is not None:
        update_params['show_aircraft'] = show_aircraft
    if show_departure is not None:
        update_params['show_departure'] = show_departure
    if show_arrival is not None:
        update_params['show_arrival'] = show_arrival
    if show_flightlevel is not None:
        update_params['show_flightlevel'] = show_flightlevel
    if show_flightrules is not None:
        update_params['show_flightrules'] = show_flightrules
    if show_route is not None:
        update_params['show_route'] = show_route
    
    if not update_params:
        await interaction.response.send_message("❌ No configuration parameters provided. Please specify at least one parameter to update.", ephemeral=True)
        return
    
    # Save configuration
    await bot.save_embed_configuration(guild_id, **update_params)
    
    # Create response embed
    embed = discord.Embed(
        title="✅ Embed Configuration Updated",
        color=update_params.get('embed_color', config.get('embed_color', 0x00ff00)),
        description="Flight plan embed appearance has been configured"
    )
    
    # Show updated settings
    updated_config = bot.server_configs[guild_id]
    
    # Add configuration fields
    if 'embed_color' in update_params:
        embed.add_field(name="Color", value=f"#{updated_config['embed_color']:06x}", inline=True)
    if 'embed_title' in update_params:
        embed.add_field(name="Title", value=updated_config['embed_title'], inline=True)
    if 'embed_thumbnail' in update_params:
        embed.add_field(name="Thumbnail", value="Set" if updated_config['embed_thumbnail'] else "Removed", inline=True)
    if 'embed_image' in update_params:
        embed.add_field(name="Image", value="Set" if updated_config['embed_image'] else "Removed", inline=True)
    
    # Field visibility summary
    visible_fields = []
    field_configs = {
        'callsign': updated_config.get('show_callsign', True),
        'pilot': updated_config.get('show_pilot', True),
        'aircraft': updated_config.get('show_aircraft', True),
        'departure': updated_config.get('show_departure', True),
        'arrival': updated_config.get('show_arrival', True),
        'flight level': updated_config.get('show_flightlevel', True),
        'flight rules': updated_config.get('show_flightrules', True),
        'route': updated_config.get('show_route', True)
    }
    
    for field, visible in field_configs.items():
        if visible:
            visible_fields.append(field)
    
    embed.add_field(name="Visible Fields", value=", ".join(visible_fields) if visible_fields else "None", inline=False)
    
    await interaction.response.send_message(embed=embed)

@tasks.loop(reconnect=True)
async def flight_plan_monitor():
    """Monitor ATC 24 WebSocket for new flight plans"""
    if not bot.server_configs:
        await asyncio.sleep(10)  # Wait if no configurations
        return
    
    # ATC24 WebSocket endpoint
    ws_url = "wss://24data.ptfs.app/wss"
    
    try:
        logger.info(f"Attempting to connect to WebSocket: {ws_url}")
        async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as websocket:
            logger.info(f"Successfully connected to ATC24 WebSocket: {ws_url}")
            bot.websocket_connection = websocket
            
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await process_websocket_message(data)
                except json.JSONDecodeError:
                    logger.warning(f"Received invalid JSON from WebSocket: {message[:100]}...")
                except Exception as e:
                    logger.error(f"Error processing WebSocket message: {e}")
                    
    except websockets.exceptions.ConnectionClosed:
        logger.warning(f"WebSocket connection closed: {ws_url}")
        await asyncio.sleep(5)  # Wait before reconnecting
    except websockets.exceptions.InvalidURI:
        logger.warning(f"Invalid WebSocket URI: {ws_url}")
        await asyncio.sleep(30)
    except Exception as e:
        logger.error(f"WebSocket connection error for {ws_url}: {e}")
        await asyncio.sleep(5)

async def process_websocket_message(message_data):
    """Process WebSocket message from 24data API"""
    if not isinstance(message_data, dict) or 't' not in message_data or 'd' not in message_data:
        logger.debug(f"Invalid message format: {message_data}")
        return
    
    event_type = message_data['t']
    data = message_data['d']
    
    # Only process flight plan events
    if event_type not in ['FLIGHT_PLAN', 'EVENT_FLIGHT_PLAN']:
        return
        
    logger.debug(f"Processing {event_type} with data: {data}")
    await process_flight_plan(data)

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
            
        bot.processed_flight_plans.append(flight_plan_id)  # deque automatically maintains max size
        
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
    """Send flight plan notification to Discord channels with custom embed configurations"""
    callsign = flight_plan.get('callsign', 'Unknown')
    
    # Send to each matching server with their custom embed configuration
    for guild_id, config, matched_prefix in matching_configs:
        try:
            channel = bot.get_channel(config['channel_id'])
            if not channel or not isinstance(channel, discord.TextChannel):
                logger.warning(f"Could not find channel {config['channel_id']} for guild {guild_id}")
                continue
                
            # Check bot permissions before sending
            permissions = channel.permissions_for(channel.guild.me)
            if not (permissions.send_messages and permissions.embed_links):
                logger.warning(f"Missing permissions to send embeds in channel {config['channel_id']} for guild {guild_id}")
                continue
            
            # Create custom embed for this server
            embed = discord.Embed(
                title=config.get('embed_title', '✈️ New Flight Plan Filed'),
                color=config.get('embed_color', 0x00ff00),
                timestamp=discord.utils.utcnow()
            )
            
            embed.description = f"Flight **{callsign}** has filed a flight plan"
            
            # Add thumbnail if configured
            if config.get('embed_thumbnail'):
                try:
                    embed.set_thumbnail(url=config['embed_thumbnail'])
                except Exception as e:
                    logger.warning(f"Failed to set thumbnail for guild {guild_id}: {e}")
            
            # Add image if configured
            if config.get('embed_image'):
                try:
                    embed.set_image(url=config['embed_image'])
                except Exception as e:
                    logger.warning(f"Failed to set image for guild {guild_id}: {e}")
            
            # Add fields based on server configuration
            if config.get('show_callsign', True) and callsign:
                embed.add_field(name="Callsign", value=f"**{callsign}**", inline=True)
                
            if config.get('show_pilot', True):
                pilot_name = flight_plan.get('robloxName', 'Unknown')
                embed.add_field(name="Pilot", value=pilot_name, inline=True)
            
            if config.get('show_aircraft', True) and flight_plan.get('aircraft'):
                embed.add_field(name="Aircraft", value=flight_plan['aircraft'], inline=True)
            
            if config.get('show_departure', True) and flight_plan.get('departing'):
                embed.add_field(name="Departure", value=flight_plan['departing'], inline=True)
            
            if config.get('show_arrival', True) and flight_plan.get('arriving'):
                embed.add_field(name="Arrival", value=flight_plan['arriving'], inline=True)
            
            if config.get('show_flightlevel', True) and flight_plan.get('flightlevel'):
                embed.add_field(name="Flight Level", value=f"FL{flight_plan['flightlevel']}", inline=True)
            
            if config.get('show_flightrules', True) and flight_plan.get('flightrules'):
                embed.add_field(name="Flight Rules", value=flight_plan['flightrules'], inline=True)
            
            if config.get('show_route', True) and flight_plan.get('route') and flight_plan['route'] != 'N/A':
                embed.add_field(name="Route", value=flight_plan['route'], inline=False)
            
            embed.set_footer(text="ATC24 Flight Plan Monitor")
            
            # Send the customized embed
            await channel.send(embed=embed)
            logger.info(f"Sent flight plan notification for {callsign} (prefix: {matched_prefix}) to guild {guild_id}")
            
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
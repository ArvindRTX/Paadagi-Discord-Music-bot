import os
import asyncio
import logging
import traceback
import time
import math
import re
import aiohttp
from aiohttp import web
from collections import deque
from typing import Dict, Optional

# Setup logging configuration first to display upgrade logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("DiscordMusicBot")

# Update yt-dlp to the latest version on startup to prevent YouTube extractor/format breakage
logger.info("Checking and upgrading yt-dlp to the latest version...")
try:
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"])
    logger.info("Successfully updated/verified yt-dlp package.")
except Exception as e:
    logger.error(f"Failed to update yt-dlp on startup: {e}")

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from dotenv import load_dotenv

# Load environmental variables from .env
load_dotenv(override=True)
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

def sanitize_cookies_content(content: str) -> str:
    """
    Sanitizes cookie content by ensuring proper tab separation and newlines.
    Useful when environment variables mangle tabs into spaces or strip newlines.
    """
    lines = content.strip().split('\n')
    sanitized_lines = []
    
    # Netscape cookies file header
    if not any(line.startswith('# Netscape HTTP Cookie File') for line in lines[:3]):
        sanitized_lines.append('# Netscape HTTP Cookie File')
        sanitized_lines.append('# This file was auto-generated and sanitized from env variables')
        
    for line in lines:
        line_strip = line.strip()
        if not line_strip:
            continue
        if line_strip.startswith('#'):
            # Keep comments as is
            sanitized_lines.append(line_strip)
            continue
            
        # Split by tabs first
        parts = line_strip.split('\t')
        if len(parts) < 7:
            # If not split by tabs, try splitting by whitespace (spaces or multiple spaces)
            parts = line_strip.split()
            
        if len(parts) >= 7:
            # Reconstruct with tab separation
            domain = parts[0]
            include_subdomains = parts[1]
            path = parts[2]
            secure = parts[3]
            expiry = parts[4]
            name = parts[5]
            value = " ".join(parts[6:]) # Rejoin value in case value had spaces
            
            # Standardize boolean values to uppercase TRUE/FALSE
            include_subdomains = "TRUE" if include_subdomains.upper() in ("TRUE", "YES", "1") else "FALSE"
            secure = "TRUE" if secure.upper() in ("TRUE", "YES", "1") else "FALSE"
            
            sanitized_line = f"{domain}\t{include_subdomains}\t{path}\t{secure}\t{expiry}\t{name}\t{value}"
            sanitized_lines.append(sanitized_line)
        else:
            # Keep line as is if it doesn't match standard cookie structure
            sanitized_lines.append(line_strip)
            
    return '\n'.join(sanitized_lines) + '\n'


# Check for a User-Agent configuration
USER_AGENT = os.getenv("YTDL_USER_AGENT")
if not USER_AGENT:
    # Use a modern Chrome desktop user-agent as default
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# yt-dlp configurations
# We use options that restrict downloading, search for single audio stream, and suppress outputs.
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0', # Bind to IPv4 to prevent connection issues
    'extractor_args': {
        'youtube': {
            'player_client': ['default', '-android_sdkless']
        }
    },
    'http_headers': {
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-us,en;q=0.5',
        'Referer': 'https://www.google.com/',
    }
}

# Check for a cookie file or inline cookie content to bypass YouTube "Sign in to confirm you're not a bot" challenge
COOKIE_FILE = os.getenv("YTDL_COOKIE_FILE")
COOKIES_CONTENT = os.getenv("YTDL_COOKIES_CONTENT")

if COOKIES_CONTENT:
    # Write inline cookie content from env variable to a temporary file
    temp_cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_cookies.txt")
    try:
        import base64
        decoded_content = None
        
        # Strip whitespace and quotes
        stripped = COOKIES_CONTENT.strip()
        if (stripped.startswith('"') and stripped.endswith('"')) or (stripped.startswith("'") and stripped.endswith("'")):
            stripped = stripped[1:-1].strip()
            
        # Try base64 decoding first
        try:
            # Remove all whitespace (including newlines from wrapped base64 strings)
            b64_candidate = "".join(stripped.split())
            if re.match(r'^[A-Za-z0-9+/=]+$', b64_candidate):
                decoded_bytes = base64.b64decode(b64_candidate.encode('utf-8'), validate=True)
                decoded_str = decoded_bytes.decode('utf-8')
                # Check if it actually looks like a cookies file
                if "# Netscape" in decoded_str or any(line.strip().startswith('#') for line in decoded_str.split('\n')[:5]):
                    decoded_content = decoded_str
                    logger.info("Successfully decoded base64 cookies from YTDL_COOKIES_CONTENT.")
        except Exception as e:
            logger.debug(f"YTDL_COOKIES_CONTENT was not base64: {e}")
            
        if decoded_content is None:
            # If not base64, treat as raw text
            # Unescape common escaped representations of tabs/newlines
            decoded_content = COOKIES_CONTENT.replace('\\n', '\n').replace('\\t', '\t')
            if (decoded_content.startswith('"') and decoded_content.endswith('"')) or (decoded_content.startswith("'") and decoded_content.endswith("'")):
                decoded_content = decoded_content[1:-1]
            logger.info("Loaded YTDL_COOKIES_CONTENT as raw text.")
            
        # Sanitize/repair cookie structure (e.g. fix space-to-tab issues)
        sanitized_content = sanitize_cookies_content(decoded_content)
        
        with open(temp_cookie_path, "w", encoding="utf-8") as f:
            f.write(sanitized_content)
        COOKIE_FILE = temp_cookie_path
        logger.info("Created temporary cookies file from YTDL_COOKIES_CONTENT environment variable.")
    except Exception as e:
        logger.error(f"Failed to write temporary cookies file: {e}")

if not COOKIE_FILE:
    # Fallback to checking default cookies.txt in the current directory
    default_cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    if os.path.exists(default_cookie_path):
        COOKIE_FILE = default_cookie_path

if COOKIE_FILE:
    logger.info(f"Using cookie file for yt-dlp: {COOKIE_FILE}")
    YTDL_OPTIONS['cookiefile'] = COOKIE_FILE
else:
    logger.warning("No cookie file found/configured. If you experience 'Sign in to confirm you're not a bot' errors, please specify YTDL_COOKIE_FILE or YTDL_COOKIES_CONTENT in your env config, or place a cookies.txt file in the bot root directory.")

# FFmpeg configuration with reconnect options
# These parameters prevent the streams from abruptly dropping midway through playback
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

async def get_spotify_track_info(url: str) -> Optional[dict]:
    """Scrapes Spotify track page for title and artist metadata."""
    async with aiohttp.ClientSession() as session:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        try:
            async with session.get(url, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                    # Extract artist via twitter:audio:artist or fallback og:description
                    artist_match = re.search(r'<meta name="music:musician" content="([^"]+)"', html)
                    if not artist_match:
                        artist_match = re.search(r'<meta property="twitter:attr:author" content="([^"]+)"', html)
                    if not artist_match:
                        # Fallback for search: "Song by Artist on Spotify"
                        artist_match = re.search(r'<meta property="og:description" content="[^·]+· Song · ([^"]+)"', html)
                    
                    if title_match:
                        title = title_match.group(1)
                        artist = artist_match.group(1) if artist_match else "Unknown Artist"
                        return {"title": title, "artist": artist}
        except Exception as e:
            logger.error(f"Error scraping Spotify track details: {e}")
    return None



async def extract_info(query: str, loop: asyncio.AbstractEventLoop) -> dict:
    """
    Extracts audio metadata and streaming URL asynchronously using a thread executor
    to prevent blocking the discord.py event loop.
    """
    # If the query is not a direct URL, search YouTube for the term
    if not query.startswith(('http://', 'https://')):
        search_query = f"ytsearch1:{query}"
    else:
        search_query = query

    logger.info(f"Extracting info for query: {query}")
    # Run the blocking yt-dlp call in a thread pool
    data = await loop.run_in_executor(
        None,
        lambda: ytdl.extract_info(search_query, download=False)
    )

    if not data:
        raise ValueError("Could not extract details for the track.")

    # If it's a search result, it returns a list of entries
    if 'entries' in data:
        if not data['entries']:
            raise ValueError("No search results found.")
        info = data['entries'][0]
    else:
        info = data

    return {
        'title': info.get('title', 'Unknown Title'),
        'webpage_url': info.get('webpage_url', search_query),
        'stream_url': info.get('url'),
        'duration': info.get('duration', 0),
        'thumbnail': info.get('thumbnail'),
        'uploader': info.get('uploader', 'Unknown Artist'),
    }


class GuildMusicManager:
    """
    Manages the queue and playback state for a single Discord Guild.
    """
    def __init__(self, bot: commands.Bot, guild_id: int):
        self.bot = bot
        self.guild_id = guild_id
        self.queue = deque()
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_track: Optional[dict] = None
        self.text_channel: Optional[discord.TextChannel] = None
        # Timing states
        self.start_time = 0.0
        self.paused_time = None
        self.total_paused_duration = 0.0
        # Loop and Panel states
        self.loop_mode = "off" # "off", "single", "queue"
        self.active_panel_view: Optional['MusicPanelView'] = None
        self.active_panel_msg: Optional[discord.Message] = None
        # Advanced states
        self.volume = 1.0
        self.autoplay = False
        self.idle_task = None
        self.current_stream_id = 0

    def pause(self) -> bool:
        """Pauses playback and tracks the time when paused."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            self.paused_time = time.time()
            # Update active panel if exists
            if self.active_panel_view and self.active_panel_msg:
                self.active_panel_view.update_buttons()
                coro = self.active_panel_msg.edit(embed=self.active_panel_view.generate_embed(), view=self.active_panel_view)
                asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
            return True
        return False

    def resume(self) -> bool:
        """Resumes playback and updates the total paused duration."""
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            if self.paused_time:
                self.total_paused_duration += time.time() - self.paused_time
                self.paused_time = None
            # Update active panel if exists
            if self.active_panel_view and self.active_panel_msg:
                self.active_panel_view.update_buttons()
                coro = self.active_panel_msg.edit(embed=self.active_panel_view.generate_embed(), view=self.active_panel_view)
                asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
            return True
        return False

    def get_elapsed_time(self) -> float:
        """Calculates and returns the elapsed playback time of the current track."""
        if not self.current_track or not self.voice_client:
            return 0.0
        if self.paused_time:
            return self.paused_time - self.start_time - self.total_paused_duration
        if self.voice_client.is_paused():
            return self.total_paused_duration
        return time.time() - self.start_time - self.total_paused_duration

    def set_volume(self, val: float):
        """Sets playback volume level dynamically."""
        self.volume = max(0.0, min(2.0, val))
        if self.voice_client and self.voice_client.source:
            try:
                if isinstance(self.voice_client.source, discord.PCMVolumeTransformer):
                    self.voice_client.source.volume = self.volume
            except Exception:
                pass

    async def disconnect_idle(self):
        """Task that runs in the background and auto-disconnects if inactive."""
        await asyncio.sleep(300) # 5 minutes
        if not self.current_track and self.voice_client:
            logger.info(f"Auto-disconnecting from voice due to inactivity in guild {self.guild_id}")
            if self.text_channel:
                embed = discord.Embed(
                    title="💤 Auto Disconnected",
                    description="Disconnected from the voice channel due to inactivity.",
                    color=discord.Color.from_rgb(149, 165, 166)
                )
                msg = await self.text_channel.send(embed=embed)
                await msg.delete(delay=10.0)
            await self.voice_client.disconnect()
            self.voice_client = None

    def play_next_callback(self, error, stream_id: int):
        """
        Thread-safe callback executed when the current audio stream finishes.
        """
        if stream_id != self.current_stream_id:
            logger.info(f"Ignoring stale callback for stream_id {stream_id} (current is {self.current_stream_id})")
            return

        if error:
            logger.error(f"Playback error in guild {self.guild_id}: {error}")
        
        # Schedule the asynchronous queue progression in the bot's event loop
        coro = self.play_next()
        asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

    async def play_next(self):
        """
        Plays the next song in the queue or updates state if empty.
        """
        if not self.voice_client or not self.voice_client.is_connected():
            logger.warning(f"Voice client disconnected for guild {self.guild_id}. Stopping queue loop.")
            return

        # Delete previous panel message and disable view to avoid clashing
        if self.active_panel_msg:
            try:
                await self.active_panel_msg.delete()
            except Exception as e:
                logger.warning(f"Failed to delete previous panel message: {e}")
            self.active_panel_msg = None

        if self.active_panel_view:
            try:
                await self.active_panel_view.disable()
            except Exception as e:
                logger.warning(f"Failed to disable previous view: {e}")
            self.active_panel_view = None

        # Determine next track based on loop mode
        next_track = None
        if self.loop_mode == "single" and self.current_track:
            next_track = self.current_track
        elif self.loop_mode == "queue" and self.current_track:
            self.queue.append(self.current_track)

        if not next_track:
            if not self.queue:
                if self.autoplay and self.current_track:
                    if self.text_channel:
                        loading_embed = discord.Embed(
                            title="📻 Autoplay Recommended",
                            description="🔍 *Finding related tracks to keep the music playing...*",
                            color=discord.Color.from_rgb(88, 101, 242)
                        )
                        try:
                            auto_msg = await self.text_channel.send(embed=loading_embed)
                        except Exception:
                            auto_msg = None
                    try:
                        search_query = f"related to {self.current_track['title']}"
                        track = await extract_info(search_query, self.bot.loop)
                        track['requester'] = self.bot.user
                        self.queue.append(track)
                        if auto_msg:
                            try:
                                await auto_msg.delete()
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"Autoplay recommendation lookup failed: {e}")
                        if auto_msg:
                            try:
                                await auto_msg.delete()
                            except Exception:
                                pass

                if not self.queue:
                    self.current_track = None
                    if self.text_channel:
                        embed = discord.Embed(
                            title="🎵 Queue Finished",
                            description="There are no more songs left in the queue. Add more using `/paadu`!",
                            color=discord.Color.from_rgb(88, 101, 242) # Discord Purple/Blue
                        )
                        msg = await self.text_channel.send(embed=embed)
                        await msg.delete(delay=10.0)
                    # Start idle auto-disconnect timer since queue ended
                    if not self.idle_task:
                        self.idle_task = self.bot.loop.create_task(self.disconnect_idle())
                    return
            
            # Cancel idle task if we have a track to play
            if self.idle_task:
                self.idle_task.cancel()
                self.idle_task = None
            self.current_track = self.queue.popleft()
        else:
            # Cancel idle task if we have a track to play
            if self.idle_task:
                self.idle_task.cancel()
                self.idle_task = None
            self.current_track = next_track

        self.start_time = time.time()
        self.paused_time = None
        self.total_paused_duration = 0.0

        self.current_stream_id += 1
        stream_id = self.current_stream_id

        try:
            # Create a new FFmpeg PCMAudio source wrapped in PCMVolumeTransformer
            audio_source = discord.FFmpegPCMAudio(self.current_track['stream_url'], **FFMPEG_OPTIONS)
            audio_source = discord.PCMVolumeTransformer(audio_source, volume=self.volume)
            
            # play method expects a callback when finished
            self.voice_client.play(audio_source, after=lambda e: self.play_next_callback(e, stream_id))

            if self.text_channel:
                view = MusicPanelView(self)
                embed = view.generate_embed()
                msg = await self.text_channel.send(embed=embed, view=view)
                view.message = msg
                self.active_panel_view = view
                self.active_panel_msg = msg

        except Exception as e:
            logger.error(f"Error starting playback in guild {self.guild_id}: {e}")
            if self.text_channel:
                await self.text_channel.send(f"⚠️ Error trying to play **{self.current_track['title']}**: `{e}`")
            # Move on to the next track if current fails
            await self.play_next()

    def format_duration(self, seconds: int) -> str:
        """
        Formats duration in seconds to a human-readable HH:MM:SS format.
        """
        if not seconds:
            return "Live / Stream"
        mins, secs = divmod(seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"{hours:d}:{mins:02d}:{secs:02d}"
        return f"{mins:d}:{secs:02d}"


async def start_web_server():
    """Starts a simple aiohttp web server for Render health checks."""
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is online!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Render health check web server running on port {port}")


class MusicBot(commands.Bot):
    """
    Subclass of commands.Bot implementing setup hooks and managing guild music states.
    """
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True
        
        # Initialize bot commands
        super().__init__(command_prefix="!", intents=intents)
        
        # Guild ID -> GuildMusicManager mapping
        self.music_managers: Dict[int, GuildMusicManager] = {}

    async def setup_hook(self):
        """
        Runs before login. We sync our Command Tree to register Slash Commands.
        """
        logger.info("Registering slash commands globally...")
        try:
            synced = await self.tree.sync()
            logger.info(f"Successfully synced {len(synced)} application commands.")
        except Exception as e:
            logger.error(f"Failed to sync application commands: {e}")
            
        # Start web server for Render health check
        await start_web_server()

    def get_music_manager(self, guild_id: int) -> GuildMusicManager:
        """
        Retrieves or creates a GuildMusicManager instance for the given guild.
        """
        if guild_id not in self.music_managers:
            self.music_managers[guild_id] = GuildMusicManager(self, guild_id)
        return self.music_managers[guild_id]


bot = MusicBot()


@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("Bot is ready for voice connections.")
@bot.event
async def on_voice_state_update(member, before, after):
    voice_client = member.guild.voice_client
    if voice_client and voice_client.channel:
        if before.channel == voice_client.channel or after.channel == voice_client.channel:
            non_bots = [m for m in voice_client.channel.members if not m.bot]
            manager = bot.get_music_manager(member.guild.id)
            if len(non_bots) == 0:
                if not manager.idle_task:
                    manager.idle_task = bot.loop.create_task(manager.disconnect_idle())
            else:
                if manager.idle_task:
                    manager.idle_task.cancel()
                    manager.idle_task = None

class MusicPanelView(discord.ui.View):
    def __init__(self, manager: GuildMusicManager):
        super().__init__(timeout=None) # Persistent view
        self.manager = manager
        self.show_playlist = False
        self.message = None
        self.setup_buttons()
        self.update_buttons()

    def setup_buttons(self):
        # --- Main Panel buttons ---
        self.pause_resume_btn = discord.ui.Button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.blurple, row=0)
        self.pause_resume_btn.callback = self.pause_resume_callback
        
        self.skip_btn = discord.ui.Button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
        self.skip_btn.callback = self.skip_callback
        
        self.stop_btn = discord.ui.Button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
        self.stop_btn.callback = self.stop_callback
        
        self.shuffle_btn = discord.ui.Button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
        self.shuffle_btn.callback = self.shuffle_callback
        
        self.loop_btn = discord.ui.Button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
        self.loop_btn.callback = self.loop_callback
        
        self.playlist_btn = discord.ui.Button(label="Playlist", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
        self.playlist_btn.callback = self.playlist_callback

        self.autoplay_btn = discord.ui.Button(label="Autoplay", emoji="📻", style=discord.ButtonStyle.secondary, row=1)
        self.autoplay_btn.callback = self.autoplay_callback

    def generate_embed(self) -> discord.Embed:
        track = self.manager.current_track
        if not track:
            return discord.Embed(title="No track playing", color=discord.Color.red())

        embed = discord.Embed(
            title="⚡ MUSIC PANEL",
            description=f"**[{track['title']}]({track['webpage_url']})**",
            color=discord.Color.from_rgb(88, 101, 242) # Discord Blurple
        )
        
        # Set video thumbnail
        if track.get('thumbnail'):
            embed.set_thumbnail(url=track['thumbnail'])

        # Requested By metadata
        requester = track.get('requester')
        requester_mention = requester.mention if requester else "Unknown"

        embed.add_field(name="👤 Requested By", value=requester_mention, inline=True)
        embed.add_field(name="🎵 Music Duration", value=self.manager.format_duration(track['duration']), inline=True)
        
        uploader = track.get('uploader', 'Unknown Artist')
        embed.add_field(name="🎙️ Music Author", value=f"`{uploader}`", inline=True)

        # Playback progress tracking
        elapsed = self.manager.get_elapsed_time()
        duration = track.get('duration', 0)
        progress = make_progress_bar(elapsed, duration)
        elapsed_str = self.manager.format_duration(int(elapsed))
        duration_str = self.manager.format_duration(duration)
        
        loop_status = "Off"
        if self.manager.loop_mode == "single":
            loop_status = "🔂 Single"
        elif self.manager.loop_mode == "queue":
            loop_status = "🔁 Queue"

        autoplay_status = "Enabled" if self.manager.autoplay else "Disabled"

        embed.add_field(
            name="📊 Playback Status", 
            value=f"{progress} `{elapsed_str} / {duration_str}` (Loop: `{loop_status}` | Autoplay: `{autoplay_status}`)", 
            inline=False
        )

        # List playlist items below if toggled
        if self.show_playlist:
            queue_list = list(self.manager.queue)
            if queue_list:
                playlist_text = ""
                for idx, t in enumerate(queue_list[:5], 1):
                    dur = self.manager.format_duration(t['duration'])
                    playlist_text += f"`{idx}.` **[{t['title']}]({t['webpage_url']})** | `{dur}`\n"
                if len(queue_list) > 5:
                    playlist_text += f"*...and {len(queue_list) - 5} more tracks.*"
                embed.add_field(name="📜 Upcoming Playlist", value=playlist_text, inline=False)
            else:
                embed.add_field(name="📜 Upcoming Playlist", value="*Queue is empty!*", inline=False)

        return embed

    def update_buttons(self):
        self.clear_items()
        is_paused = self.manager.voice_client and self.manager.voice_client.is_paused()
        self.pause_resume_btn.label = "Resume" if is_paused else "Pause"
        self.pause_resume_btn.emoji = "▶️" if is_paused else "⏸️"
        self.pause_resume_btn.style = discord.ButtonStyle.green if is_paused else discord.ButtonStyle.blurple

        if self.manager.loop_mode == "off":
            self.loop_btn.style = discord.ButtonStyle.secondary
            self.loop_btn.label = "Loop"
        elif self.manager.loop_mode == "single":
            self.loop_btn.style = discord.ButtonStyle.success
            self.loop_btn.label = "Loop 1"
        elif self.manager.loop_mode == "queue":
            self.loop_btn.style = discord.ButtonStyle.primary
            self.loop_btn.label = "Loop Queue"

        self.playlist_btn.style = discord.ButtonStyle.success if self.show_playlist else discord.ButtonStyle.secondary
        self.autoplay_btn.style = discord.ButtonStyle.success if self.manager.autoplay else discord.ButtonStyle.secondary

        self.add_item(self.pause_resume_btn)
        self.add_item(self.skip_btn)
        self.add_item(self.stop_btn)
        self.add_item(self.shuffle_btn)
        self.add_item(self.loop_btn)
        self.add_item(self.playlist_btn)
        self.add_item(self.autoplay_btn)

    async def disable(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    # --- Main Callbacks ---
    async def pause_resume_callback(self, interaction: discord.Interaction):
        is_paused = self.manager.voice_client and self.manager.voice_client.is_paused()
        if is_paused:
            self.manager.resume()
        else:
            self.manager.pause()
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def skip_callback(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            if self.manager.loop_mode == "single":
                self.manager.loop_mode = "off"
            voice_client.stop()
            await interaction.response.send_message("⏭️ Track Skipped!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No track playing to skip.", ephemeral=True)

    async def stop_callback(self, interaction: discord.Interaction):
        voice_client = interaction.guild.voice_client
        self.manager.queue.clear()
        self.manager.current_track = None
        self.manager.loop_mode = "off"
        if voice_client:
            await voice_client.disconnect()
            self.manager.voice_client = None
        await interaction.response.send_message("⏹️ Playback stopped and bot disconnected.", ephemeral=True)

    async def shuffle_callback(self, interaction: discord.Interaction):
        import random
        q_list = list(self.manager.queue)
        random.shuffle(q_list)
        self.manager.queue.clear()
        self.manager.queue.extend(q_list)
        await interaction.response.send_message("🔀 Queue shuffled!", ephemeral=True)
        if self.show_playlist:
            await interaction.message.edit(embed=self.generate_embed(), view=self)

    async def loop_callback(self, interaction: discord.Interaction):
        if self.manager.loop_mode == "off":
            self.manager.loop_mode = "single"
        elif self.manager.loop_mode == "single":
            self.manager.loop_mode = "queue"
        else:
            self.manager.loop_mode = "off"
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def playlist_callback(self, interaction: discord.Interaction):
        self.show_playlist = not self.show_playlist
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    async def autoplay_callback(self, interaction: discord.Interaction):
        self.manager.autoplay = not self.manager.autoplay
        self.update_buttons()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)


def make_progress_bar(elapsed: float, duration: float, size: int = 15) -> str:
    if duration <= 0:
        return "🔘" + "▬" * (size - 1) + " (Live Stream)"
    percentage = elapsed / duration
    progress = int(percentage * size)
    progress = max(0, min(size - 1, progress))
    bar = "".join(["▬" if i != progress else "🔘" for i in range(size)])
    return bar


class QueueView(discord.ui.View):
    def __init__(self, manager: GuildMusicManager, author_id: int):
        super().__init__(timeout=60.0)
        self.manager = manager
        self.author_id = author_id
        self.current_page = 0
        self.items_per_page = 5
        self.message = None

    def generate_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📋 Paadagi Music Queue",
            color=discord.Color.from_rgb(155, 89, 182) # Purple
        )

        # Currently playing track
        current = self.manager.current_track
        if current:
            elapsed = self.manager.get_elapsed_time()
            duration = current.get('duration', 0)
            progress_bar = make_progress_bar(elapsed, duration)
            elapsed_str = self.manager.format_duration(int(elapsed))
            duration_str = self.manager.format_duration(duration)
            
            # Show status (Playing vs Paused)
            status_emoji = "▶️"
            if self.manager.voice_client and self.manager.voice_client.is_paused():
                status_emoji = "⏸️"
                
            embed.description = (
                f"**Now Playing:**\n"
                f"**[{current['title']}]({current['webpage_url']})**\n"
                f"{status_emoji} {progress_bar} `{elapsed_str} / {duration_str}`\n\n"
            )
        else:
            embed.description = "Nothing is currently playing. Use `/paadu` to get started!\n\n"

        # Upcoming queue
        queue_list = list(self.manager.queue)
        total_items = len(queue_list)
        
        if total_items > 0:
            total_pages = math.ceil(total_items / self.items_per_page)
            # Boundary check
            if self.current_page >= total_pages:
                self.current_page = total_pages - 1
            if self.current_page < 0:
                self.current_page = 0
                
            start = self.current_page * self.items_per_page
            end = start + self.items_per_page
            page_items = queue_list[start:end]

            queue_text = ""
            for idx, track in enumerate(page_items, start + 1):
                duration_str = self.manager.format_duration(track['duration'])
                queue_text += f"{idx}. **[{track['title']}]({track['webpage_url']})** | `{duration_str}`\n"
                
            embed.add_field(
                name="Upcoming Songs", 
                value=queue_text, 
                inline=False
            )
            embed.set_footer(text=f"Page {self.current_page + 1} of {total_pages} • Total Songs: {total_items}")
        else:
            embed.add_field(
                name="Upcoming Songs", 
                value="No songs in the queue.", 
                inline=False
            )
            embed.set_footer(text="Page 1 of 1 • Total Songs: 0")

        return embed

    def update_button_states(self):
        total_items = len(self.manager.queue)
        total_pages = math.ceil(total_items / self.items_per_page)

        # Disable previous button if on page 0 or no pages
        self.prev_button.disabled = (self.current_page <= 0)
        # Disable next button if on last page or no pages
        self.next_button.disabled = (self.current_page >= total_pages - 1 or total_pages <= 1)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_button_states()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    @discord.ui.button(label="▶️ Next", style=discord.ButtonStyle.secondary, disabled=True)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_button_states()
        await interaction.response.edit_message(embed=self.generate_embed(), view=self)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        loading_embed = self.generate_embed()
        for i, field in enumerate(loading_embed.fields):
            if field.name == "Upcoming Songs":
                loading_embed.set_field_at(
                    i, 
                    name="Upcoming Songs", 
                    value="⏳ *Refreshing queue...*", 
                    inline=False
                )
                break
        await interaction.response.edit_message(embed=loading_embed, view=self)
        
        await asyncio.sleep(0.8)
        
        self.update_button_states()
        await interaction.message.edit(embed=self.generate_embed(), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass


# ==============================================================================
# SLASH COMMANDS
# ==============================================================================

@bot.tree.command(name="paadu", description="Joins voice channel and plays/queues a song (url or search query).")
@app_commands.describe(query="The URL of the song or a search query (e.g. song title)")
async def paadu(interaction: discord.Interaction, query: str):
    # Check if the user is in a voice channel
    if not interaction.user.voice or not interaction.user.voice.channel:
        embed = discord.Embed(
            title="⚠️ Action Required",
            description="You must be connected to a voice channel to play music!",
            color=discord.Color.from_rgb(231, 76, 60) # Red
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Defer interaction to allow time for info extraction
    await interaction.response.defer(ephemeral=False)
    
    # Send an initial beautiful loading animation
    loading_embed = discord.Embed(
        title="🔍 Searching for music...",
        description="`[▱▱▱▱▱▱▱▱▱▱] 0%` ⏳ Processing request...",
        color=discord.Color.from_rgb(241, 196, 15)
    )
    loading_msg = await interaction.followup.send(embed=loading_embed)
    
    guild_id = interaction.guild_id
    manager = bot.get_music_manager(guild_id)
    manager.text_channel = interaction.channel

    # Resolve track info asynchronously with simulated step progress edits
    try:
        resolved_query = query
        if "open.spotify.com/track/" in query:
            loading_embed.description = "`[▰▱▱▱▱▱▱▱▱▱] 10%` 🟢 Resolving Spotify metadata..."
            await loading_msg.edit(embed=loading_embed)
            spotify_info = await get_spotify_track_info(query)
            if spotify_info:
                resolved_query = f"{spotify_info['artist']} - {spotify_info['title']}"
                loading_embed.description = f"`[▰▰▱▱▱▱▱▱▱▱] 20%` 🔍 Resolved: **{resolved_query}**"
                await loading_msg.edit(embed=loading_embed)
            else:
                logger.warning("Failed to resolve Spotify track info. Attempting fallback query.")

        loading_embed.description = "`[▰▰▰▱▱▱▱▱▱▱] 40%` 🔄 Fetching video metadata..."
        await loading_msg.edit(embed=loading_embed)
        
        track = await extract_info(resolved_query, bot.loop)
        
        loading_embed.description = "`[▰▰▰▰▰▰▰▱▱▱] 70%` ⚡ Preparing audio stream..."
        await loading_msg.edit(embed=loading_embed)
    except Exception as e:
        logger.error(f"Error extracting track info: {e}")
        embed = discord.Embed(
            title="⚠️ Extraction Error",
            description=f"Could not retrieve audio info for `{query}`. Error: `{e}`",
            color=discord.Color.from_rgb(231, 76, 60)
        )
        await loading_msg.edit(embed=embed)
        return

    # Handle voice channel connection
    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    bot_member = interaction.guild.me

    # Programmatically override voice channel connect/speak permissions
    channel_perms = voice_channel.permissions_for(bot_member)
    if not channel_perms.connect or not channel_perms.speak:
        if bot_member.guild_permissions.manage_channels or bot_member.guild_permissions.administrator:
            logger.info(f"Overriding locked permissions for voice channel: {voice_channel.name}")
            try:
                await voice_channel.set_permissions(bot_member, connect=True, speak=True)
            except Exception as e:
                logger.error(f"Failed to override voice channel permissions: {e}")

    # Bypassing user limit if channel is full and we have Manage Channels permission
    original_user_limit = voice_channel.user_limit
    limit_modified = False
    if original_user_limit > 0 and len(voice_channel.members) >= original_user_limit:
        if not bot_member.guild_permissions.administrator and not bot_member.guild_permissions.move_members:
            if bot_member.guild_permissions.manage_channels:
                logger.info(f"Temporarily removing user limit for voice channel: {voice_channel.name}")
                try:
                    await voice_channel.edit(user_limit=0)
                    limit_modified = True
                except Exception as e:
                    logger.error(f"Failed to clear user limit: {e}")

    if not voice_client:
        try:
            voice_client = await voice_channel.connect()
            manager.voice_client = voice_client
            logger.info(f"Connected to voice channel: {voice_channel.name}")
            if limit_modified:
                logger.info(f"Restoring original user limit {original_user_limit} to channel.")
                await voice_channel.edit(user_limit=original_user_limit)
        except Exception as e:
            logger.error(f"Error connecting to voice channel: {e}")
            if limit_modified:
                try:
                    await voice_channel.edit(user_limit=original_user_limit)
                except Exception:
                    pass
            embed = discord.Embed(
                title="⚠️ Connection Error",
                description=f"Could not connect to the voice channel: `{e}`",
                color=discord.Color.from_rgb(231, 76, 60)
            )
            await loading_msg.edit(embed=embed)
            return
    elif voice_client.channel != voice_channel:
        try:
            await voice_client.move_to(voice_channel)
            manager.voice_client = voice_client
            logger.info(f"Moved to voice channel: {voice_channel.name}")
            if limit_modified:
                logger.info(f"Restoring original user limit {original_user_limit} to channel.")
                await voice_channel.edit(user_limit=original_user_limit)
        except Exception as e:
            logger.error(f"Error moving to voice channel: {e}")
            if limit_modified:
                try:
                    await voice_channel.edit(user_limit=original_user_limit)
                except Exception:
                    pass
            embed = discord.Embed(
                title="⚠️ Connection Error",
                description=f"Could not move to your voice channel: `{e}`",
                color=discord.Color.from_rgb(231, 76, 60)
            )
            await loading_msg.edit(embed=embed)
            return
    else:
        manager.voice_client = voice_client

    # Add track to queue
    track['requester'] = interaction.user
    manager.queue.append(track)

    # Determine if we should play it immediately
    if not voice_client.is_playing() and not voice_client.is_paused():
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await manager.play_next()
    else:
        queued_embed = discord.Embed(
            title="📥 Track Queued",
            description=f"Added **[{track['title']}]({track['webpage_url']})** to the queue.",
            color=discord.Color.from_rgb(241, 196, 15) # Yellow
        )
        queued_embed.add_field(name="Position in Queue", value=str(len(manager.queue)), inline=True)
        queued_embed.add_field(name="Duration", value=manager.format_duration(track['duration']), inline=True)
        await loading_msg.edit(embed=queued_embed)
        try:
            await loading_msg.delete(delay=5.0)
        except Exception:
            pass


@bot.tree.command(name="skip", description="Skips the current song.")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    manager = bot.get_music_manager(interaction.guild_id)

    if not voice_client or (not voice_client.is_playing() and not voice_client.is_paused()):
        embed = discord.Embed(
            title="⚠️ Cannot Skip",
            description="There is no track playing right now.",
            color=discord.Color.from_rgb(231, 76, 60)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Stopping the player triggers the 'after' callback, which runs play_next()
    voice_client.stop()
    embed = discord.Embed(
        title="⏭️ Track Skipped",
        description="Skipping the current song...",
        color=discord.Color.from_rgb(52, 152, 219) # Blue
    )
    await interaction.response.send_message(embed=embed)
    try:
        msg = await interaction.original_response()
        await msg.delete(delay=5.0)
    except Exception:
        pass


@bot.tree.command(name="pause", description="Pauses the current playback.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    manager = bot.get_music_manager(interaction.guild_id)

    if not voice_client or not voice_client.is_playing():
        embed = discord.Embed(
            title="⚠️ Action Refused",
            description="No audio is playing to pause.",
            color=discord.Color.from_rgb(231, 76, 60)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    manager.pause()
    embed = discord.Embed(
        title="⏸️ Playback Paused",
        description="Audio playback has been paused. Resume using `/resume`.",
        color=discord.Color.from_rgb(241, 196, 15)
    )
    await interaction.response.send_message(embed=embed)
    try:
        msg = await interaction.original_response()
        await msg.delete(delay=5.0)
    except Exception:
        pass


@bot.tree.command(name="resume", description="Resumes paused playback.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    manager = bot.get_music_manager(interaction.guild_id)

    if not voice_client or not voice_client.is_paused():
        embed = discord.Embed(
            title="⚠️ Action Refused",
            description="There is no paused track to resume.",
            color=discord.Color.from_rgb(231, 76, 60)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    manager.resume()
    embed = discord.Embed(
        title="▶️ Playback Resumed",
        description="Audio playback has resumed.",
        color=discord.Color.from_rgb(46, 204, 113)
    )
    await interaction.response.send_message(embed=embed)
    try:
        msg = await interaction.original_response()
        await msg.delete(delay=5.0)
    except Exception:
        pass


@bot.tree.command(name="queue", description="Shows the current upcoming tracks in the queue.")
async def queue_list(interaction: discord.Interaction):
    manager = bot.get_music_manager(interaction.guild_id)
    view = QueueView(manager, interaction.user.id)
    view.update_button_states()
    embed = view.generate_embed()
    await interaction.response.send_message(embed=embed, view=view)
    view.message = await interaction.original_response()


@bot.tree.command(name="stop", description="Clears the queue and disconnects the bot completely.")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    manager = bot.get_music_manager(interaction.guild_id)

    # Clear queue
    manager.queue.clear()
    manager.current_track = None
    manager.loop_mode = "off"
    
    # Disable active control panel view
    if manager.active_panel_view:
        try:
            await manager.active_panel_view.disable()
        except Exception:
            pass
        manager.active_panel_view = None
        manager.active_panel_msg = None

    if not voice_client:
        embed = discord.Embed(
            title="⏹️ Queue Cleared",
            description="The queue was cleared, but the bot is not in a voice channel.",
            color=discord.Color.from_rgb(231, 76, 60)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Disconnect voice client
    await voice_client.disconnect()
    manager.voice_client = None

    embed = discord.Embed(
        title="⏹️ Bot Disconnected",
        description="Cleared the play queue and successfully disconnected from the voice channel.",
        color=discord.Color.from_rgb(149, 165, 166) # Grey
    )
    await interaction.response.send_message(embed=embed)
    try:
        msg = await interaction.original_response()
        await msg.delete(delay=5.0)
    except Exception:
        pass


# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    if not TOKEN:
        logger.critical("DISCORD_BOT_TOKEN environment variable not set in .env! Exiting.")
        exit(1)
        
    # Debug loaded token (length and mask) to verify if the real token is being read
    masked_token = f"{TOKEN[:5]}...{TOKEN[-5:]}" if len(TOKEN) > 10 else "TOO_SHORT"
    logger.info(f"Loaded Token Info - Length: {len(TOKEN)}, Masked: {masked_token}")
    
    if TOKEN == "your_bot_token_here":
        logger.critical("Error: The token loaded is still the placeholder 'your_bot_token_here'. Please ensure you have saved your .env file in the editor (Ctrl+S) and that it is in the correct directory.")
        exit(1)
        
    logger.info("Starting Discord Music Bot...")
    bot.run(TOKEN)

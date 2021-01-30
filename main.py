import os
import asyncio
import discord
import itertools
import sys
import traceback
from async_timeout import timeout
from discord.ext import commands
from functools import partial
from youtube_dl import YoutubeDL

from keep_alive import keep_alive

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
    # Changed to fix YTDL TLS errors - See https://github.com/Rapptz/discord.py/issues/315
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin',
    'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.

        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        await ctx.send(f'```ini\n[Added {data["title"]} to the Queue.]\n```', delete_after=15)

        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.

        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url']), data=data, requester=requester)


class MusicPlayer:
    """A class which is assigned to each guild using the bot for Music.

    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.

    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'There was an error processing your song.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            source.volume = self.volume
            self.current = source

            self._guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            self.np = await self._channel.send(f'**Now Playing:** `{source.title}` requested by '
                                               f'`{source.requester}`')
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

            try:
                # We are no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['c', 'join'])
    async def connect_(self, ctx, *, channel: discord.VoiceChannel=None):
        """Connect to voice.

        Parameters
        ------------
        channel: discord.VoiceChannel [Optional]
            The channel to connect to. If a channel is not specified, an attempt to join the voice channel you are in
            will be made.

        This command also handles moving the bot to different channels.
        """
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                return await ctx.channel.send(content="_You are not connected to a voice channel. Please join a voice channel before invoking this command._")
                # raise InvalidVoiceChannel('No channel to join. Please either specify a valid channel or join one.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')

        await ctx.send(f'Connected to: **{channel}**', delete_after=60)

    @commands.command(name='play', aliases=['p', 'silvousplay'])
    async def play_(self, ctx, *, search: str):
        """Request a song and add it to the queue.

        This command attempts to join a valid voice channel if the bot is not already in one.
        Uses YTDL to automatically search and retrieve a song.

        Parameters
        ------------
        search: str [Required]
            The song to search and retrieve using YTDL. This could be a simple search, an ID or URL.
        """
        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)

        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

        await player.queue.put(source)

    @commands.command(name='pause', aliases=['//', 'pp'])
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('_I am currently not playing anything!_', delete_after=60)
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'**`{ctx.author}`**: Paused the song!')

    @commands.command(name='resume', aliases=['r'])
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('_I am currently not playing anything!_', delete_after=60)
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'**`{ctx.author}`**: Resumed the song!')

    @commands.command(name='skip', aliases=['s', 'n'])
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('_I am currently not playing anything!_', delete_after=60)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        await ctx.send(f'**`{ctx.author}`**: Skipped the song!')

    @commands.command(name='queue', aliases=['q', 'list'])
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('_I am not connected to voice!_', delete_after=60)

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('_There are currently no more queued songs._')

        # Grab up to 5 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 5))

        fmt = '\n'.join(f'**`{_["title"]}`**' for _ in upcoming)
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed)

    @commands.command(name='nowplaying', aliases=['np', 'playing'])
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('_I am not connected to voice!_', delete_after=60)

        player = self.get_player(ctx)
        if not player.current:
            return await ctx.send('_I am currently not playing anything!_')

        try:
            # Remove our previous now_playing message.
            await player.np.delete()
        except discord.HTTPException:
            pass

        player.np = await ctx.send(f"**Now Playing:** `{vc.source.title}` "
                                   f"requested by `{vc.source.requester}`")

    @commands.command(name='volume', aliases=['v', 'vol'])
    async def change_volume(self, ctx, *, vol: float):
        """Change the player volume.

        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send("_I am not connected to voice!_", delete_after=60)

        if not 0 < vol < 101:
            return await ctx.send("_Please enter a value between 1 and 100._")

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        await ctx.send(f"**`{ctx.author}`**: _Set the volume to **{vol}%**_")

    @commands.command(name='increase', aliases=['<', 'inc'])
    async def increase_volume(self, ctx):
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send("_I am not connected to voice!_", delete_after=60)

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume += 0.05

        player.volume += 0.05
        await ctx.send(f"**`{ctx.author}`**: _Increased the volume by **5%**_")

    @commands.command(name='decrease', aliases=['>', 'dec'])
    async def decrease_volume(self, ctx):
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send("_I am not connected to voice!_", delete_after=60)

        player = self.get_player(ctx)

        if vc.source:
            vc.source.volume -= 0.05

        player.volume -= 0.05
        await ctx.send(f"_**`{ctx.author}`**: _Decreased the volume by **5%**_")

    @commands.command(name='stop', aliases=['dc'])
    async def stop_(self, ctx):
        """Stop the currently playing song and destroy the player.

        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send("_I am currently not playing anything!_", delete_after=60)

        await self.cleanup(ctx.guild)


# Bot Setup
bot = commands.Bot(command_prefix="|", help_command=None)

@bot.event
async def on_ready():
    print(f"{bot.user.name} is ready to jam now.")


@bot.command(name="help", aliases=["|h"], help="Shows help message and list of commands")
async def help(ctx):
    file = discord.File('./labaguettemusicien.jpg')
    file2 = discord.File('./labaguettemusicien.jpg')
    embed = discord.Embed(
        title="List of commands for LeBotMusicien",
        description="LeBotMusicien (née LeBaguetteMusicien) plays music using [`youtube-dl`](http://ytdl-org.github.io/youtube-dl/) and accepts links and playlists from [YouTube and supported sites](http://ytdl-org.github.io/youtube-dl/supportedsites.html).\n\n**Bot Pefix**: `|`\n\n**List of Commands:**",
        colour=discord.Colour.blurple(),
    )

    embed.set_thumbnail(url="attachment://labaguettemusicien.jpg")
    embed.add_field(name="`|connect`", value="Connects to a voice channel. Please join a voice channel before invoking this command.", inline=True)
    embed.add_field(name="aliases", value="`|c`, `|join`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="`|play <link-to-video-or-playlist>`", value="Adds a song or songs from a (public) playlist to the queue.", inline=True)
    embed.add_field(name="aliases", value="`|p`, `|silvousplay`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="`|pause`", value="Pauses the currently playing song.", inline=True)
    embed.add_field(name="aliases", value="`|pp`, `|//`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="`|resume`", value="Resumes the currently paused song.", inline=True)
    embed.add_field(name="aliases", value="`|r`, `|join`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="`|skip`", value="Skips the currently playing song.", inline=True)
    embed.add_field(name="aliases", value="`|s`, `|n`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="`|stop`", value="Stops the currently playing song and leaves the voice channel.", inline=True)
    embed.add_field(name="aliases", value="`|q`, `|list`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="`|nowplaying`", value="Displays information about the currently playing song.", inline=True)
    embed.add_field(name="aliases", value="`|np`, `|playing`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="`|queue`", value="Retrieves a basic queue of upcoming songs.", inline=True)
    embed.add_field(name="aliases", value="`|q`, `|list`", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed2 = discord.Embed(
        title="",
        description="",
        colour=discord.Colour.blurple(),
    )
    # embed2.set_thumbnail(url="attachment://labaguettemusicien.jpg")
    embed2.add_field(name="`|volume <float>`", value="Changes the player volume to the supplied value.", inline=True)
    embed2.add_field(name="aliases", value="`|v`, `|vol`", inline=True)
    embed2.add_field(name="\u200b", value="\u200b", inline=True)
    embed2.add_field(name="`|increase`", value="Increases the player volume by 5%.", inline=True)
    embed2.add_field(name="aliases", value="`|<`, `|inc`", inline=True)
    embed2.add_field(name="\u200b", value="\u200b", inline=True)
    embed2.add_field(name="`|decrease`", value="Decreases the player volume by 5%.", inline=True)
    embed2.add_field(name="aliases", value="`|>`, `|dec`", inline=True)
    embed2.add_field(name="\u200b", value="\u200b", inline=True)
    embed2.add_field(name="\u200b", value="[Wondering about > and < above?](http://www.piano-play-it.com/images/crescendo-diminuendo.png)", inline=True)

    embed2.set_footer(text="Custom Music Bot for NISER Coding Club Discord | Fork us on GitHub (https://github.com/sdgniser/lebotmusicien)", icon_url="attachment://labaguettemusicien.jpg")

    await ctx.channel.send(content=None, file=file, embed=embed)
    await ctx.channel.send(content=None, file=file2, embed=embed2)

# @bot.event
# async def on_error(event, *args, **kwargs):
#     with open('err.log', 'a') as f:
#         if event == 'on_message':
#             f.write(f'Unhandled message: {args[0]}\n')
#         else:
#             raise


bot.add_cog(Music(bot))
TOKEN = os.getenv("TOKEN")


keep_alive()
bot.run(TOKEN)

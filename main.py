import os
import sys
import shutil
import asyncio
import yaml
import logging
from datetime import timedelta
import discord
from discord.ext import commands # i dislike commands.cooldown, but i don't know any other simple way to do rate limits
from uwuipy import Uwuipy

# Logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("elector")
logger.level = logging.INFO

# App config
def validate_conf(source: dict, against: dict):
    for reqk, reqv in against.items():
        if reqk in source:
            if type(reqv) == type(source[reqk]): # pylint: disable=unidiomatic-typecheck # i dont want to include subclasses
                if isinstance(reqv, dict):
                    validate_conf(source[reqk], reqv)
            else:
                raise ValueError(f"{reqk} exists in your config, but is not the same type as the example file")
        else:
            raise ValueError(f"{reqk} is not in your config, copy it from the example file")

def load_config():
    with open("conf.example.yml", "r", encoding="UTF-8") as dc:
        default_config = yaml.safe_load(dc)

    if os.path.exists("data/config.yml"): # begone, windows developers
        with open("data/config.yml", "r", encoding="UTF-8") as c:
            tconf = yaml.safe_load(c)
            validate_conf(source=tconf, against=default_config)
            return tconf
    else:
        shutil.copyfile("conf.example.yml", "data/config.yml")
        print("Default config has been created")
        sys.exit(1)

config = load_config()

# Library setup

uwulib = Uwuipy(
    None,
    action_chance=0
)

# Global vars

SERVER: discord.Guild = None # type: ignore

ANNOUNCE_CHANNEL: discord.TextChannel = None # type: ignore
VOICE_CHANNEL: discord.VoiceChannel = None # type: ignore
LOG_CHANNEL: discord.TextChannel = None # type: ignore
VOICE_CATEGORY: discord.CategoryChannel = None # type: ignore
VOTE_CATEGORY: discord.CategoryChannel = None # type: ignore

LEADER_ROLE: discord.Role = None # type: ignore
VICE_ROLE: discord.Role = None # type: ignore
VIP_ROLE: discord.Role = None # type: ignore
PLUS_ROLE: discord.Role = None # type: ignore
GUEST_ROLE: discord.Role = None # type: ignore

LEVEL_ROLE_MAP: dict[int, discord.Role] = {}

# Bot setup

init_complete = False

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = discord.Bot(intents=intents)

@bot.event
async def on_ready():
    global init_complete, ANNOUNCE_CHANNEL, VOICE_CHANNEL, LOG_CHANNEL, VOICE_CATEGORY, VOTE_CATEGORY, LEADER_ROLE, VICE_ROLE, VIP_ROLE, PLUS_ROLE, GUEST_ROLE, SERVER, LEVEL_ROLE_MAP # pylint: disable=global-statement
    logger.info('Logged in as %s', bot.user)

    ANNOUNCE_CHANNEL = bot.get_channel(config['channels']['public']) # type: ignore # these return the right type, but pycord dosent know that
    VOICE_CHANNEL = bot.get_channel(config['channels']['voice']) # type: ignore
    LOG_CHANNEL = bot.get_channel(config['channels']['logs']) # type: ignore
    VOICE_CATEGORY = bot.get_channel(config['channels']['voice_rooms_category']) # type: ignore
    VOTE_CATEGORY = bot.get_channel(config['channels']['vote_category']) # type: ignore

    SERVER = ANNOUNCE_CHANNEL.guild

    LEADER_ROLE = SERVER.get_role(config['roles']['leader']) # type: ignore
    VICE_ROLE = SERVER.get_role(config['roles']['vice-leader']) # type: ignore
    VIP_ROLE = SERVER.get_role(config['roles']['vip_role']) # type: ignore
    PLUS_ROLE = SERVER.get_role(config['roles']['plus_role']) # type: ignore
    GUEST_ROLE = SERVER.get_role(config['roles']['guest_role']) # type: ignore

    LEVEL_ROLE_MAP = {
        -1: SERVER.default_role,
        0: GUEST_ROLE,
        1: PLUS_ROLE,
        2: VIP_ROLE,
        3: VICE_ROLE,
        4: LEADER_ROLE
    }

    for dc in VOICE_CATEGORY.channels:
        logger.info("Deleting old channel: %s", dc.name)
        await dc.delete(reason="Deleting voice rooms on bot start")

    await bot.sync_commands()
    init_complete = True

# Functions

def get_user_roles(member: discord.Member):
    """Get a members role ids in a set

    Args:
        member (discord.Member)

    Returns:
        set: The role ids of a member
    """
    return {role.id for role in member.roles}

async def is_bot_managed(member: discord.Member):
    """Check if someone has the guest role, if they do not, the bot should not perform actions against them.

    Args:
        member (discord.Member)

    Returns:
        bool: Do they have the guest role
    """
    user_roles = get_user_roles(member)
    if GUEST_ROLE.id in user_roles:
        return True
    return False

async def get_user_perm_level(member: discord.Member):
    """Get the permission level of a server member

    Args:
        member (discord.Member)

    Returns:
        int: The permission level of a member, -1 means they should not have any permissions, and should be refered as unmanaged
    """
    # People who are not real should not get permissions
    if not await is_bot_managed(member):
        return -1
    # Check and return their permission level
    # There is probably a better and faster way to do this
    user_roles = get_user_roles(member)
    if LEADER_ROLE.id in user_roles:
        return 4
    elif VICE_ROLE.id in user_roles:
        return 3
    elif VIP_ROLE.id in user_roles:
        return 2
    elif PLUS_ROLE.id in user_roles:
        return 1
    elif GUEST_ROLE.id in user_roles:
        return 0
    return -1 # should not be triggered, but just in case

async def public_log(embed: discord.Embed):
    await ANNOUNCE_CHANNEL.send(embed=embed)

async def admin_log(embed: discord.Embed):
    await LOG_CHANNEL.send(embed=embed)

# Commands

@bot.slash_command(name="ping", description="Make sure the bot is online")
@discord.guild_only()
@discord.default_permissions(administrator=True)
async def ping(ctx: discord.ApplicationContext):
    await ctx.respond("Pong! You have permission level " + str(await get_user_perm_level(ctx.user)), ephemeral=True) # type: ignore

## Voice rooms

# channelid: ownerid
vc_owners: dict[int, int] = {}

if config['features']['voice_rooms']['enabled']:
    # slash group because you cannot normally add spaces
    vc_cmds = bot.create_group("vc", "Voice channel commands")

    # VC create

    class CreateVCModal(discord.ui.DesignerModal):
        def __init__(self, name: str, priv: list[discord.SelectOption]):
            super().__init__(
                discord.ui.Label(
                    "Name",
                    discord.ui.InputText(
                        placeholder=name,
                        max_length=20,
                        required=False
                    )
                ),
                discord.ui.Label(
                    "Privacy",
                    discord.ui.Select(
                        placeholder="Lock your room",
                        options=priv,
                        required=False,
                        max_values=1
                    ),
                    description="Anyone who is this level or higher can join"
                ),
                discord.ui.Label(
                    "Features",
                    discord.ui.Select(
                        placeholder="Select what people can do",
                        options=[
                            discord.SelectOption(label="Voice", value="voice", emoji="📞", default=True),
                            discord.SelectOption(label="Text", value="text", emoji="💬", default=True),
                            discord.SelectOption(label="Video", value="video", emoji="📺", default=True),
                        ],
                        required=False,
                        min_values=0,
                        max_values=3
                    ),
                    description="Decide what other people can do in your room, you always get everything"
                ),
                discord.ui.Label(
                    "Max people",
                    discord.ui.InputText(
                        placeholder="Unlimited",
                        max_length=2,
                        min_length=0,
                        required=False
                    ),
                    description="The max amount of people who can be in your room"
                ),
                title="Create VC",
            )
        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            # Make sure they have less than the max amount of rooms
            owned = 0
            maxr = config['features']['voice_rooms']['max_rooms']
            for cvc in vc_owners.values():
                if cvc == interaction.user.id: # type: ignore
                    owned += 1
                    if owned >= maxr:
                        await interaction.respond(f"You can only have {str(maxr)} room" + ('' if maxr == 1 else 's'))
                        return
            # Get the responses
            name = self.children[0].item.value if self.children[0].item.value else interaction.user.name # type: ignore
            priv = int(self.children[1].item.values[0]) if len(self.children[1].item.values) == 1 else 0 # type: ignore
            can_talk = "voice" in self.children[2].item.values
            can_text = "text" in self.children[2].item.values
            can_stream = "video" in self.children[2].item.values
            user_limit = int(self.children[3].item.value) if self.children[3].item.value.isdigit() else 0
            # Set the permissions
            perms = {
                SERVER.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False, connect=False, speak=can_talk, stream=can_stream, set_voice_channel_status=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    connect=True,
                    priority_speaker=True,
                    mute_members=True,
                    deafen_members=True,
                    move_members=True,
                    speak=True,
                    stream=True,
                    manage_permissions=config['features']['voice_rooms']['allow_perm_change']
                )
            }
            if priv == 0:
                perms[GUEST_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True)
            else:
                perms[GUEST_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=False, connect=False)
            if priv <= 1:
                perms[PLUS_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True)
            if priv <= 2:
                perms[VIP_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True)
            if priv <= 3:
                perms[VICE_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True)
            # Create the channel
            crvc = await VOICE_CATEGORY.create_voice_channel(
                name=name,
                reason=f"{interaction.user.name} requested creation", # type: ignore
                overwrites=perms,
                user_limit=user_limit
            )
            logger.info("%s created a voice channel: '%s'", interaction.user.name, name)
            await crvc.set_status("Created by " + interaction.user.name) # type: ignore
            if interaction.user.voice: # type: ignore
                # Move them into their new voice channel
                await interaction.user.move_to(crvc, reason="User made voice channel") # type: ignore
                await interaction.followup.send(f"You have been moved to {crvc.mention}.", ephemeral=True, delete_after=10)
            else:
                # Tell them to join it
                await interaction.followup.send(f"Go join {crvc.mention}, the channel will close automatically in {str(config['features']['voice_rooms']['join_grace'])} seconds if nobody joins.", ephemeral=True, delete_after=config['features']['voice_rooms']['join_grace'] + 2)
                # Wait and see
                await asyncio.sleep(config['features']['voice_rooms']['join_grace'])
                # Check if anyone is in
                nvc = SERVER.get_channel(crvc.id) # this probably works
                # Delete if not
                if len(nvc.members) == 0: # type: ignore
                    await nvc.delete(reason="Nobody joined in time") # type: ignore
                    await interaction.followup.send("Nobody joined in time", ephemeral=True, delete_after=10)
                    logger.info("Nobody joined '%s' in time", name)

    @vc_cmds.command(name="create", description="Create a voice channel")
    @discord.guild_only()
    async def vc_create_cmd(ctx: discord.ApplicationContext):
        # Make sure they are allowed to make rooms
        perm = await get_user_perm_level(ctx.user) # type: ignore
        if config['permissions']['allow_create_room'] > perm:
            await ctx.respond(f"You must be at least {LEVEL_ROLE_MAP[config['permissions']['allow_create_room']].mention} to create a voice channel", ephemeral=True)
            return
        # Make sure they have less than the max amount of rooms
        owned = 0
        maxr = config['features']['voice_rooms']['max_rooms']
        for cvc in vc_owners.values():
            if cvc == ctx.user.id:
                owned += 1
                if owned >= maxr:
                    await ctx.respond(f"You can only have {str(maxr)} room" + ('' if maxr == 1 else 's'))
                    return
        # Check who the user can lock their room to
        pvalid = []
        pvalid.append(discord.SelectOption(label="Just me", value="4", emoji="🙋‍♂️"))
        if perm >= 3:
            pvalid.append(discord.SelectOption(label=VICE_ROLE.name, value="3", emoji="🤝"))
        if perm >= 2:
            pvalid.append(discord.SelectOption(label=VIP_ROLE.name, value="2", emoji="⭐"))
        if perm >= 1:
            pvalid.append(discord.SelectOption(label=PLUS_ROLE.name, value="1", emoji="👥"))
        pvalid.append(discord.SelectOption(label=GUEST_ROLE.name, value="0", emoji="👤"))
        await ctx.send_modal(
            CreateVCModal(ctx.user.name, pvalid)
        )

## Fun

### Timeout

if config['features']['fun']['timeout']['enabled']:
    @bot.user_command(name="timeout")
    @commands.cooldown(config['features']['fun']['timeout']['times'], config['features']['fun']['timeout']['cooldown'], commands.BucketType.user)
    async def timeout_cmd(ctx: discord.ApplicationContext, member: discord.Member):
        perm = await get_user_perm_level(ctx.user) # type: ignore
        if config['permissions']['allow_timeout'] > perm:
            await ctx.respond(f"You must be at least {LEVEL_ROLE_MAP[config['permissions']['allow_timeout']].mention} to time someone out", ephemeral=True)
            return
        await ctx.defer()
        dur = config['features']['fun']['timeout']['leader_duration'] if perm == 4 else config['features']['fun']['timeout']['duration']
        await member.timeout_for(timedelta(seconds=dur))
        await ctx.followup.send(f"{member.mention} has been timed out for {str(dur)} seconds.")
        logger.info("'%s' timed out '%s' for %s seconds", ctx.user.name, member.name, str(dur))

# Events

## Most of these functions will have redundant code

@bot.event
async def on_member_join(member: discord.Member):
    logger.info(f"'{member.name}' just joined the server")
    # Give them guest
    if not member.bot:
        await member.add_roles(GUEST_ROLE, reason="New member")
    # Give them vip if on the list
    if member.id in config['vips']:
        await member.add_roles(VIP_ROLE, reason="New VIP member")
    await admin_log(discord.Embed(color=discord.Color.green(), title="New member", description=f"{member.mention} just joined and was given permission level {await get_user_perm_level(member)}"))

@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    # Check if someone left a room, and it is now empty
    if config['features']['voice_rooms']['enabled'] and \
        before.channel and before.channel != after.channel \
        and before.channel.category and before.channel.category.id == VOICE_CATEGORY.id \
        and len(before.channel.members) == 0:
        # It is a room, and is now empty. Delete it
        if before.channel.id in vc_owners:
            del vc_owners[before.channel.id]
        await before.channel.delete(reason="The room is now empty")
        logger.info("Deleted stale voice channel")

# Errors

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    if isinstance(error, commands.CommandOnCooldown):
        hours, remainder = divmod(int(error.retry_after), 3600)
        minutes, seconds = divmod(remainder, 60)
        text = []
        if hours > 0:
            text.append(str(hours) + " hours")
        if minutes > 0:
            text.append(str(minutes) + " minutes")
        if seconds > 0:
            text.append(str(seconds) + " seconds")
        await ctx.respond("This command is currently on cooldown! Please wait " + ' '.join(text), ephemeral=True)
    else:
        raise error

# Background

# Let's run!

bot.run(config['token'])

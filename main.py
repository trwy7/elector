import os
import sys
import re
import random
import shutil
import asyncio
import logging
import functools
from datetime import timedelta, datetime
from threading import Lock
import yaml
import discord
from discord.commands import option
from discord.ext import commands, tasks # i dislike commands.cooldown, but i don't know any other simple way to do rate limits
from uwuipy import Uwuipy
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("elector")
logger.level = logging.DEBUG

# App config
def validate_conf(source: dict, against: dict, path: str="/"):
    for reqk, reqv in against.items():
        if reqk in source:
            if type(reqv) == type(source[reqk]): # pylint: disable=unidiomatic-typecheck # i dont want to include subclasses
                if isinstance(reqv, dict):
                    validate_conf(source[reqk], reqv, path + reqk + "/")
            else:
                raise ValueError(f"\u001b[31m{path}{reqk} exists in your config, but is not the same type as the example file\u001b[0m")
        else:
            raise ValueError(f"\u001b[31m{path}{reqk} is not in your config, copy it from conf.example.yml at https://raw.githubusercontent.com/trwy7/elector/refs/heads/main/conf.example.yml\u001b[0m")

def load_config():
    with open("conf.example.yml", "r", encoding="UTF-8") as dc:
        default_config = yaml.safe_load(dc)

    if os.path.exists(os.path.join("data", "config.yml")):
        with open(os.path.join("data", "config.yml"), "r", encoding="UTF-8") as c:
            tconf = yaml.safe_load(c)
            validate_conf(source=tconf, against=default_config)
            return tconf
    else:
        if not os.path.isdir("data"):
            os.mkdir("data")
        shutil.copyfile("conf.example.yml", os.path.join("data", "config.yml"))
        print("Default config has been created")
        sys.exit(1)

config = load_config()

# Library setup

uwulib = Uwuipy(
    None,
    action_chance=0
)

scheduler = AsyncIOScheduler()

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

# (user id, voicechannel id): (muted, deafaned)
voice_capability_map: dict[tuple[int, int], tuple[bool, bool]] = {}
# If the person joined when the bot is online, save their join time
# userid: datetime
join_dt: dict[int, datetime] = {}

# Bot setup

init_complete = False

intents = discord.Intents.default() # maybe consider fine tuning at some point to save bandwidth
intents.members = True
intents.message_content = True

bot = discord.Bot(intents=intents)

@bot.event
async def on_ready():
    global init_complete, ANNOUNCE_CHANNEL, VOICE_CHANNEL, LOG_CHANNEL, VOICE_CATEGORY, VOTE_CATEGORY, LEADER_ROLE, VICE_ROLE, VIP_ROLE, PLUS_ROLE, GUEST_ROLE, SERVER, LEVEL_ROLE_MAP # pylint: disable=global-statement
    logger.info('Logged in as %s', bot.user)

    # NOTE: If this becomes buggy, consider converting to lambda functions, and fallback to SERVER.fetch_channel

    ANNOUNCE_CHANNEL = bot.get_channel(config['channels']['public']) # type: ignore # these return the right type, but pylance dosent know that
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
        #-1: SERVER.default_role, # uncomment if needed, this breaks the rolerename function
        0: GUEST_ROLE,
        1: PLUS_ROLE,
        2: VIP_ROLE,
        3: VICE_ROLE,
        4: LEADER_ROLE
    }

    for dc in VOICE_CATEGORY.channels:
        logger.info("Deleting old channel: %s", dc.name)
        await dc.delete(reason="Deleting voice rooms on bot start")

    found_lv = None

    for pv in VOTE_CATEGORY.channels:
        if pv.topic and pv.topic.startswith("!"):
            await pv.delete(reason="Deleting stale vote channels on bot start")
        if pv.name == "election":
            found_lv = pv

    init_complete = True

    # Make sure the steg dict is all functional
    # Commented for release because rate limits
    # temp_testc = await VOTE_CATEGORY.create_text_channel("init-test-channel",
    #     topic="!" + conv_to_steg_topic(1234567890),
    #     reason="Testing steg",
    #     overwrites={SERVER.default_role: discord.PermissionOverwrite(view_channel=False)}
    # )
    # res_topic = str(conv_to_steg_topic_rev(temp_testc.topic.removeprefix("!")))
    # await temp_testc.delete(reason="Testing steg")
    # if res_topic != "1234567890":
    #     raise RuntimeError(f"Unicode steg is in wrong order: {res_topic}")
    
    if found_lv:
        await restore_election_state(found_lv) # TODO: after development, move after sync_commands
    await bot.sync_commands()
    scheduler.start()

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

def set_vote_channel_perms(privacy: int, by=None, to=None):
    perms = {
        SERVER.default_role: discord.PermissionOverwrite(view_channel=False, add_reactions=False)
    }
    if privacy == 0:
        perms[GUEST_ROLE] = discord.PermissionOverwrite(view_channel=True)
    if privacy <= 1:
        perms[PLUS_ROLE] = discord.PermissionOverwrite(view_channel=True)
    if privacy <= 2:
        perms[VIP_ROLE] = discord.PermissionOverwrite(view_channel=True)
    if privacy <= 3:
        perms[VICE_ROLE] = discord.PermissionOverwrite(view_channel=True)
    if privacy <= 4:
        perms[LEADER_ROLE] = discord.PermissionOverwrite(view_channel=True)
    if to:
        perms[to] = discord.PermissionOverwrite(view_channel=False)
    if by:
        perms[by] = discord.PermissionOverwrite(view_channel=True)
    return perms
def replace_line(string: str, replace: str, line: int):
    """Replace the line of a string

    Args:
        string (str): The original string
        replace (str): What to replace the line with
        line (int): The line number starting at 0

    Returns:
        str: The result
    """
    nstring = string.splitlines()
    nstring[line] = replace
    return "\n".join(nstring)

## Steg for hiding binary into a discord channel topic
## I was too lazy to store data in a dict and somehow i thought this was better

STEGV_OFF = " "
STEGV_ON = "؜"

def conv_to_steg_topic(original: int) -> str:
    return "_" + bin(original)[2:].replace("0", STEGV_OFF).replace("1", STEGV_ON) + "_"

def conv_to_steg_topic_rev(original: str) -> int:
    return int(original.replace(STEGV_OFF, "0").replace(STEGV_ON, "1").removeprefix("_").removesuffix("_"), 2)

## Decorators

def requireperm(level: int):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(ctx: discord.ApplicationContext, *args, **kwargs):
            if level > await get_user_perm_level(ctx.user):
                await ctx.respond(f"You must be at least {LEVEL_ROLE_MAP[level].mention} to run this command", ephemeral=True)
                return None
            return await func(ctx, *args, **kwargs)
        return wrapper
    return decorator

def election_lock(condition):
    def decorator(func):
        if not condition:
            return func
        @functools.wraps(func)
        async def wrapper(ctx: discord.ApplicationContext, *args, **kwargs):
            # there is probably a better way of doing this
            rvc = bot.get_channel(VOTE_CATEGORY.id)
            if not rvc:
                rvc: discord.CategoryChannel = SERVER.fetch_channel(VOTE_CATEGORY.id)
            for vc in rvc.channels:
                if vc.name == "election":
                    await ctx.respond("You cannot run this command during an election")
                    return
            return await func(ctx, *args, **kwargs)
        return wrapper
    return decorator

# Leader elections

leader_vote_lock = Lock()

async def restore_election_state(channel: discord.TextChannel):
    cs = channel.topic.splitlines()
    state = conv_to_steg_topic_rev(cs[2])
    logger.info("Restoring election channel from state %s", str(state))
    await admin_log(discord.Embed(color=discord.Color.red(), title="Restoring election", description=f"The bot was shut down during an election of state {str(state)}. Attempting to restore."))
    if state == 0:
        # Too early to do anything, restart the whole vote
        oreason = cs[1]
        await channel.delete(reason="Restoring vote channel from state 0")
        await election_start(oreason)
    elif state == 1:
        # Restart the timer
        await election_wait_and_tally(channel)
    elif state == 2:
        # state = [
        #   ignore,
        #   startreasonraw,
        #   stegstate,
        #   stegnewleaderid
        #]
        # This code is stolen from the original function, may have some bugs
        # Get new leader
        new_leader = await SERVER.fetch_member(conv_to_steg_topic_rev(cs[3]))
        if not new_leader:
            await admin_log(discord.Embed(color=discord.Color.red(), title="Election restore failed", description="Could not fetch the new leader by ID, they may have left the server."))
            await channel.delete(reason="Restore failed: New leader not found")
            return
        # Announce and ping
        await channel.send(f"{new_leader.mention} is the new {LEADER_ROLE.mention}!") # TODO: Send another message here when vice-leader selection is added
        await ANNOUNCE_CHANNEL.send(f"{new_leader.mention} is the new {LEADER_ROLE.mention}!")
        # Give the person the leader role
        await new_leader.add_roles(LEADER_ROLE, reason="Election finished!")
        # Log
        await admin_log(discord.Embed(color=discord.Color.yellow(), title="Election restore attempted", description=f"{new_leader.mention} has been given {LEADER_ROLE.mention}"))
        # Change to state 3
        cs = cs[:3]
        cs[2] = conv_to_steg_topic(3)
        cs.append(conv_to_steg_topic(round(datetime.now().timestamp())))
        # Apply new state
        await channel.edit(topic="\n".join(cs), reason="Changing election state")
        # Next phase!
        await election_cleanup(channel)
    elif state == 3:
        await election_cleanup(channel)

async def election_start(reason: str=""):
    """Start an election, This function may take multiple hours to run.

    Args:
        reason (str): Included in the initial channel, e.g. '<person> left the server.' or '<person> was overthrown.'

    Returns:
        _type_: _description_
    """
    with leader_vote_lock:
        # Double check no vote is running
        nc: discord.CategoryChannel = bot.get_channel(VOTE_CATEGORY.id) # Re-check cache
        if not nc:
            nc = SERVER.fetch_channel(VOTE_CATEGORY.id) # No cache for some reason, manually fetch the channel
        for vc in nc.channels:
            if vc.name == "election":
                return "There is already an election running"
        for vc in nc.channels:
            if vc.name == "overthrow":
                await vc.delete(reason="Election about to start")
        # Create the channel
        # The channel topic will store data so I dont need to have any disk writes
        votec = await VOTE_CATEGORY.create_text_channel(
            name="election",
            reason="Leader election started",
            position=0,
            overwrites={SERVER.default_role: discord.PermissionOverwrite(view_channel=False)},
            topic=f"Vote for a new {LEADER_ROLE.mention}!\n{reason}\n{conv_to_steg_topic(0)}" # Unicode to hide the state0 text
        )
        # Send initial message
        init_desc = f"{reason} It's time to elect a new {LEADER_ROLE.mention}! React with ✅ on each person you would like to vote for."
        init_msg = await votec.send(embed=discord.Embed(color=discord.Color.teal(), title="Election", description=init_desc))
        await init_msg.pin(reason="Pinning instruction message")
    # Add initial message to topic
    new_topic = votec.topic + "\n" + conv_to_steg_topic(init_msg.id)
    # Get members that can be promoted and send messages
    required_perm = config['permissions']['allow_leader']
    ns: discord.Guild = bot.get_guild(SERVER.id)
    if not ns:
        ns = await bot.fetch_guild(SERVER.id)
    last_member_msg = None
    for cm in ns.members:
        if await get_user_perm_level(cm) >= required_perm:
            last_member_msg = await votec.send(cm.mention)
            await last_member_msg.add_reaction("✅")
    if not last_member_msg:
        # Uh oh, nobody is eligible.
        logger.error("An election was supposed to happen, but nobody was eligible")
        await votec.delete(reason="Nobody is eligible for election")
        return "Nobody is eligible to be elected"
    # Calculate end time
    current_time = datetime.now()
    target_time = current_time.replace(hour=config['features']['leader']['election_end'], minute=0, second=0, microsecond=0) # Force end at 4 PM (default)
    if current_time > target_time:
        logger.debug("Vote started too late in the day, setting end to next day")
        target_time += timedelta(days=1)
    if (target_time - current_time) < timedelta(hours=5):
        logger.debug("Vote end time is too close to now, extending by 5 hours")
        target_time += timedelta(hours=5)
    timestamp = round(target_time.timestamp())
    logger.info("Vote ends at %s (%s)", target_time, str(timestamp))
    # Add end time to save state
    new_topic += "\n" + conv_to_steg_topic(timestamp)
    lm = await votec.send(f"Vote is open, it ends <t:{str(timestamp)}:R>!")
    # Add the final message to the topic to fetch later
    new_topic += "\n" + conv_to_steg_topic(lm.id)
    # Change the state to 1
    new_topic = replace_line(new_topic, conv_to_steg_topic(1), 2)
    # Save the state and unlock the channel
    await votec.edit(overwrites=set_vote_channel_perms(config['permissions']['allow_leader_vote']), topic=new_topic, reason="Unlocking vote channel")
    await asyncio.sleep(1) # Just in case discord does not automatically update
    await votec.send("@everyone ^") # TODO: add into config
    await admin_log(discord.Embed(
        color=discord.Color.yellow(),
        title="Election",
        description=f"An election has started. It is scheduled to end <t:{str(timestamp)}:R> (<t:{str(timestamp)}:F>)",
        fields=[discord.EmbedField("Reason", reason)] if reason.strip() else None
    ))
    return await election_wait_and_tally(votec)

if config['features']['leader']['scheduled_elections']:
    scheduler.add_job(election_start, 'cron', day_of_week=config['features']['leader']['election_day'], hour=config['features']['leader']['election_hour'])

async def election_wait_and_tally(channel: discord.TextChannel):
    """WARNING: channel MUST BE UP TO DATE, YOU MAY NEED TO REFRESH THE STATE WITH `SERVER.fetch_channel`"""
    state: list[str] = channel.topic.splitlines()
    # state = [
    #   ignore,
    #   startreasonraw,
    #   stegstate,
    #   steginitialmessageid,
    #   stegendtime,
    #   stegfinalmessageid
    #]
    # Get the end time
    end_timestamp = conv_to_steg_topic_rev(state[4])
    logger.debug("Got end timestamp %s", end_timestamp)
    end_time = datetime.fromtimestamp(int(end_timestamp))
    if not end_time:
        await channel.delete(reason="Unable to decode the end time from the channel topic. Did you manually modify it?")
        logger.error("Unable to decode the end time from the channel topic. Did you manually modify it?")
        return "Could not get/decode end time"
    end_time = datetime.now() + timedelta(seconds=5) # FIXME: TEST ONLY, REMOVE IN PROD
    # Wait until the end and try to be accurate, negative values continue instantly anyway
    await asyncio.sleep((end_time - datetime.now()).total_seconds() - 15)
    # Wait a little longer
    await asyncio.sleep((end_time - datetime.now()).total_seconds() - 10)
    # Send final call
    fmsg = await channel.send("Vote ends in 10 seconds")
    await fmsg.pin(reason="Election status message")
    await asyncio.sleep(5)
    await fmsg.edit("Vote ends in 5 seconds")
    await asyncio.sleep(5)
    await fmsg.edit("Tallying votes...") # Technically someone can react to a message while it is being fetched and get an extra vote in
    # Get the initial and final messages
    initial = await channel.fetch_message(conv_to_steg_topic_rev(state[3]))
    last = await channel.fetch_message(conv_to_steg_topic_rev(state[5]))
    # Get the votes
    vote_dict: dict[int, list[discord.Member]] = {} # i know this is a weird way of storing this
    async for usr_msg in channel.history(limit=500, oldest_first=True, after=initial, before=last): # if you have more than 100 people eligible to be voted in, you should already be finding another bot
        if not usr_msg.author.id == bot.user.id:
            logger.warning("%s was able to send a message during election init", usr_msg.author.name)
            continue
        if not len(usr_msg.mentions) == 1:
            logger.debug("Skipping message that has no mentions")
            continue
        # Get the user being voted on
        user = usr_msg.mentions[0]
        # Get the number of votes that were cast for a person
        votes = 0
        for reaction in usr_msg.reactions: # why cant this be a dict :sob:
            if reaction.emoji == "✅":
                # We only manually go through the list to validate who has voted
                async for reactor in reaction.users():
                    if reactor.bot:
                        continue
                    if user.id == reactor.id:
                        logger.debug("%s voted for themselves", user.name)
                        continue
                    votes += 1
        # Save it to the dict
        if votes in vote_dict:
            vote_dict[votes].append(user)
        else:
            vote_dict[votes] = [user]
    # Check if any votes were cast
    has_cast = any(vote_dict) # provided keys are all ints (if they arent something has gone very wrong), this should work
    if not has_cast:
        # No votes were cast, keep the current leader and vice-leader
        await channel.send("No votes were cast")
        # Change to state 3
        state = state[:3]
        state[2] = conv_to_steg_topic(3)
        state.append(conv_to_steg_topic(round(datetime.now().timestamp())))
        # Apply new state
        await channel.edit(topic="\n".join(state), reason="Changing election state")
        # Next phase!
        return await election_cleanup(channel)
    # Remove leader and vice-leader
    nleader = await SERVER.fetch_role(LEADER_ROLE.id)
    nvice = await SERVER.fetch_role(VICE_ROLE.id)
    for cleader in nleader.members:
        await cleader.remove_roles(nleader, reason="Election concluded!")
    for cvice in nvice.members:
        await cvice.remove_roles(nvice, reason="Election concluded!")
    # Get a sorted version
    sorted_vote_values = sorted(vote_dict.keys())
    # Get new leader if there more than one person won
    eligible_list = vote_dict[sorted_vote_values[-1]]
    if len(eligible_list) == 1:
        new_leader = eligible_list[0]
    else:
        # "Spin a wheel" (random.choice)
        new_leader = random.choice(eligible_list)
    # Change to state 2
    state = state[:3]
    state[2] = conv_to_steg_topic(2)
    state.append(conv_to_steg_topic(new_leader.id))
    # Apply new state
    await channel.edit(topic="\n".join(state), reason="Changing election state")
    # state = [
    #   ignore,
    #   startreasonraw,
    #   stegstate,
    #   stegnewleaderid
    #]
    # Delete the messages
    await channel.purge(reason="Vote has concluded", before=fmsg, limit=500)
    await channel.purge(reason="Vote has concluded", after=fmsg, limit=1000)
    # Set up the result embed
    res_embed = discord.Embed(
        color=discord.Color.blurple(),
        title="Results",
        description="The election has concluded"
    )
    # Show the embed
    await fmsg.edit(content=None, embed=res_embed)
    # Suspense...
    await asyncio.sleep(5)
    # Show placements one by one
    res_list = []
    if len(sorted_vote_values) >= 3:
        # Set the placement embed field
        res_list.insert(0, discord.EmbedField(name="3rd place",
            value=f"{str(sorted_vote_values[-3])} vote{'s' if sorted_vote_values[-3] != 1 else ''}\n" + "\n".join(m.mention for m in vote_dict[sorted_vote_values[-3]])
        ))
        # Replace the existing fields
        res_embed.fields = res_list
        # Set color to bronze ish
        res_embed.color = discord.Color.dark_orange()
        # Add to the message
        await fmsg.edit(embed=res_embed)
        # More suspense...
        await asyncio.sleep(5)
    if len(sorted_vote_values) >= 2:
        # Set the placement embed field
        res_list.insert(0, discord.EmbedField(name="2nd place",
            value=f"{str(sorted_vote_values[-2])} vote{'s' if sorted_vote_values[-2] != 1 else ''}\n" + "\n".join(m.mention for m in vote_dict[sorted_vote_values[-2]])
        ))
        # Replace the existing fields
        res_embed.fields = res_list
        # Set color to silver ish
        res_embed.color = discord.Color.light_grey()
        # Add to the message
        await fmsg.edit(embed=res_embed)
        # More suspense...
        await asyncio.sleep(5)
    # Same things as above
    res_list.insert(0, discord.EmbedField(name="1st place",
        value=f"{str(sorted_vote_values[-1])} vote{'s' if sorted_vote_values[-1] != 1 else ''}\n" + "\n".join(m.mention for m in vote_dict[sorted_vote_values[-1]])
    ))
    res_embed.fields = res_list
    # Set color to gold ish
    res_embed.color = discord.Color.yellow()
    # Show final list
    await fmsg.edit(embed=res_embed)
    await asyncio.sleep(1)
    # Send a log
    await admin_log(discord.Embed(
        color=discord.Color.yellow(),
        title="Election results",
        description=f"An election has finished. {new_leader.mention} was given {nleader.mention}",
        fields=res_list
    ))
    # Rewrite channel perms
    nwrites = set_vote_channel_perms(config['permissions']['allow_election_result_view'])
    nwrites[SERVER.default_role] = discord.PermissionOverwrite(send_messages=False, view_channel=False)
    nwrites[LEADER_ROLE] = discord.PermissionOverwrite(send_messages=True, view_channel=True)
    nwrites[VICE_ROLE] = discord.PermissionOverwrite(send_messages=True, view_channel=True)
    await channel.edit(overwrites=nwrites, reason="Election over, locking channel")
    # Purge messages again
    await channel.purge(reason="Vote has concluded", after=fmsg, limit=1000)
    # Announce and ping
    await channel.send(f"{new_leader.mention} is the new {LEADER_ROLE.mention}!") # TODO: Send another message here when vice-leader selection is added
    await ANNOUNCE_CHANNEL.send(f"{new_leader.mention} is the new {LEADER_ROLE.mention}!")
    # Give the person the leader role
    await new_leader.add_roles(nleader, reason="Election finished!")
    # Change to state 3
    state = state[:3]
    state[2] = conv_to_steg_topic(3)
    state.append(conv_to_steg_topic(round(datetime.now().timestamp())))
    # Apply new state
    await channel.edit(topic="\n".join(state), reason="Changing election state")
    # Next phase!
    return await election_cleanup(channel)

async def election_cleanup(channel: discord.TextChannel):
    state: list[str] = channel.topic.splitlines()
    # state = [
    #   ignore,
    #   startreasonraw,
    #   stegstate,
    #   stegelectionendtime
    #]
    # Get the end time
    ended_at = datetime.fromtimestamp(conv_to_steg_topic_rev(state[3]))
    if config['features']['leader']['vice-leader']:
        # Give them enough time to pick a vice leader
        end_time = ended_at + timedelta(hours=12) # TODO: add to config
    else:
        end_time = ended_at + timedelta(hours=1)
    end_time = ended_at + timedelta(seconds=10) # FIXME: TEST ONLY, ALSO TO BE REMOVED
    await asyncio.sleep((end_time - datetime.now()).total_seconds())
    leader_revoked = False
    if config['features']['leader']['vice-leader'] and config['features']['leader']['force_vice']:
        rvice = await SERVER.fetch_role(VICE_ROLE.id)
        if len(rvice.members) == 0:
            rleader = await SERVER.fetch_role(LEADER_ROLE.id)
            for m in rleader.members:
                await m.remove_roles(rleader, reason="No vice chosen")
            await channel.send(f"No {VICE_ROLE.mention} was chosen")
            leader_revoked = True
            await asyncio.sleep(300) # TODO: conf option to also start a re-election

    if config['features']['leader']['overthrow'] and not leader_revoked:
        await init_overthrow()

    await channel.delete(reason="Election complete!")

## Misc

async def init_overthrow():
    logger.info("Creating overthrow channel")
    overthrow_channel = await VOTE_CATEGORY.create_text_channel(
        name="overthrow",
        reason="Election ended",
        position=0,
        overwrites=set_vote_channel_perms(config['permissions']['allow_overthrow'], by=None, to=LEADER_ROLE)
    )
    omsg = await overthrow_channel.send(embed=discord.Embed(
        color=discord.Color.dark_red(),
        title="Overthrow",
        description=f"Vote to overthrow the {LEADER_ROLE.mention}. {str(config['features']['leader']['overthrow'] + 1)} reactions are required."
    ))
    await omsg.add_reaction("✅")
    await omsg.pin(reason="Overthrow init")

# Commands

## Debug

debug_group = bot.create_group("debug", "Debug commands")

@debug_group.command(name="startelection", description="Run an election") # TODO: Expand this command into one that can be used by the leader
@discord.guild_only()
@discord.default_permissions(administrator=True)
async def start_elect_cmd(ctx: discord.ApplicationContext, reason: str=''):
    await ctx.respond("Starting election...", ephemeral=True)
    await election_start(reason=reason)

## Voice rooms

# channelid: ownerid
vc_owners: dict[int, int] = {}

if config['features']['voice_rooms']['enabled']:
    # slash group because you cannot normally add spaces
    vc_cmds = bot.create_group("vc", "Voice channel commands")

    # require_own decorator

    def require_own_vc(func):
        @functools.wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            # Make sure they are in a voice channel
            if not ctx.user.voice:
                await ctx.respond("You are not in a voice channel", ephemeral=True)
                return
            # Get the channel they are in
            cvc = ctx.user.voice.channel
            # Make sure they own it
            if vc_owners.get(cvc.id) != ctx.user.id:
                logger.debug("%s does not own %s: %s", ctx.user.name, cvc.name, str(vc_owners))
                await ctx.respond("You do not own this voice channel", ephemeral=True)
                return
            return await func(ctx, *args, **kwargs)
        return wrapper

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
                            discord.SelectOption(label="Activities", value="play", emoji="🎮", default=True)
                        ],
                        required=False,
                        min_values=0,
                        max_values=4
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
                        await interaction.respond(f"You can only have {str(maxr)} room" + ('' if maxr == 1 else 's'), ephemeral=True)
                        return
            # Get the responses
            name = self.children[0].item.value if self.children[0].item.value else interaction.user.name # type: ignore
            priv = int(self.children[1].item.values[0]) if len(self.children[1].item.values) == 1 else 0 # type: ignore
            can_talk = "voice" in self.children[2].item.values
            can_text = "text" in self.children[2].item.values
            can_stream = "video" in self.children[2].item.values
            can_play = "play" in self.children[2].item.values
            user_limit = int(self.children[3].item.value) if self.children[3].item.value.isdigit() else 0
            # Set the permissions
            perms = {
                SERVER.default_role: discord.PermissionOverwrite(view_channel=False, send_messages=False, connect=False, speak=can_talk, stream=can_stream, set_voice_channel_status=False, start_embedded_activities=False),
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
                    start_embedded_activities=True,
                    manage_permissions=config['features']['voice_rooms']['allow_perm_change']
                )
            }
            if priv == 0:
                perms[GUEST_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True, start_embedded_activities=can_play)
            else:
                perms[GUEST_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=False, connect=False, start_embedded_activities=False)
            if priv <= 1:
                perms[PLUS_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True, start_embedded_activities=can_play)
            if priv <= 2:
                perms[VIP_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True, start_embedded_activities=can_play)
            if priv <= 3:
                perms[VICE_ROLE] = discord.PermissionOverwrite(view_channel=True, send_messages=can_text, connect=True, start_embedded_activities=can_play)
            # Create the channel
            crvc = await VOICE_CATEGORY.create_voice_channel(
                name=name,
                reason=f"{interaction.user.name} requested creation", # type: ignore
                overwrites=perms,
                user_limit=user_limit
            )
            vc_owners[crvc.id] = interaction.user.id
            logger.info("%s created a voice channel: '%s'", interaction.user.name, name)
            await crvc.set_status("Created by " + interaction.user.name, reason="Initial room setup")
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
                if nvc and len(nvc.members) == 0: # type: ignore
                    await nvc.delete(reason="Nobody joined in time") # type: ignore
                    await interaction.followup.send("Nobody joined in time", ephemeral=True, delete_after=10)
                    logger.info("Nobody joined '%s' in time", name)

    @vc_cmds.command(name="create", description="Create a voice channel")
    @discord.guild_only()
    @requireperm(config['permissions']['allow_create_room'])
    async def vc_create_cmd(ctx: discord.ApplicationContext):
        # Make sure they are allowed to make rooms
        perm = await get_user_perm_level(ctx.user) # type: ignore
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

    # VC end

    @vc_cmds.command(name="end", description="Delete your voice channel")
    @discord.guild_only()
    @option(name="move_to", description="Where to move everyone, leave blank to kick everyone")
    @require_own_vc
    async def vc_delete_cmd(ctx: discord.ApplicationContext, move_to: discord.VoiceChannel=None):
        await ctx.defer(ephemeral=True)
        cvc = ctx.user.voice.channel
        # Move everyone out
        cant_move = []
        if move_to and cvc.id != move_to.id:
            # Make sure the owner can connect
            # Right now discord will throw an "invalid channel id" if you can't connect anyway, but if they fix that bug this will catch it
            if not move_to.permissions_for(ctx.user).connect:
                await ctx.respond(f"You do not have permission to connect to {move_to.mention}", ephemeral=True)
                return
            # Move the owner first for call notification reasons
            await ctx.user.move_to(move_to)
            for mm in cvc.members:
                if mm.id == ctx.user.id:
                    continue # we already moved them
                # Make sure the individual can connect
                if not move_to.permissions_for(mm).connect:
                    cant_move.append(mm.mention)
                    continue
                # Move them
                await mm.move_to(move_to)
        # Delete the channel
        try:
            await cvc.delete(reason="Owner requested deletion")
        except discord.errors.NotFound:
            # Probably deleted because nobody is in it
            pass
        if cant_move:
            rmsg = f"The channel was deleted, but the following people were not able to join {move_to.mention}:\n- " + "\n- ".join(cant_move)
        else:
            rmsg = "Done"
        await ctx.respond(rmsg, ephemeral=True)

    # VC rename

    @vc_cmds.command(name="rename", description="Delete your voice channel")
    @discord.guild_only()
    @option(name="name", description="The new name for your channel")
    @require_own_vc
    async def vc_rename_cmd(ctx: discord.ApplicationContext, name: str):
        # Verify name
        if len(name) > 20:
            await ctx.respond("Name cannot be above 20 characters", ephemeral=True)
            return
        # Rename the channel
        await ctx.user.voice.channel.edit(name=name, reason="Owner requested rename")
        await ctx.respond("Done", ephemeral=True)

    # Invite to VC

    ## TODO: Modal for invite and kick to allow multi user input

    @vc_cmds.command(name="invite", description="Add someone to your voice channel")
    @discord.guild_only()
    @option(name="user", description="Who to invite")
    @require_own_vc
    async def vc_invite_cmd(ctx: discord.ApplicationContext, user: discord.Member):
        await ctx.defer(ephemeral=True)
        await ctx.user.voice.channel.set_permissions(user, connect=True, reason="Owner invited user")
        await ctx.respond("Done", ephemeral=True)

    # Kick from VC
    # Just invite but in reverse

    @vc_cmds.command(name="remove", description="Remove someone from your voice channel")
    @discord.guild_only()
    @option(name="user", description="Who to remove")
    @option(name="kick", description="If they are in the channel, kick them")
    @require_own_vc
    async def vc_kick_cmd(ctx: discord.ApplicationContext, user: discord.Member, kick: bool=True):
        await ctx.defer(ephemeral=True)
        await ctx.user.voice.channel.set_permissions(user, connect=False, reason="Owner removed user")
        if kick and user.voice and user.voice.channel.id == ctx.user.voice.channel.id:
            await user.move_to(None, reason="Owner kicked user, and they were in vc")
        await ctx.respond("Done", ephemeral=True)

## Kicking

vkick_lock = Lock()

### Votekick

if config['features']['kick']['votekick']['enabled']:
    @bot.user_command(name="votekick")
    @requireperm(config['permissions']['allow_kick_start'])
    @election_lock(config['features']['kick']['disable_on_election'])
    @commands.cooldown(config['features']['kick']['votekick']['times'], config['features']['kick']['votekick']['cooldown'], commands.BucketType.user)
    async def votekick_cmd(ctx: discord.ApplicationContext, member: discord.Member):
        # TODO: Delete the channel if they leave
        await ctx.defer(ephemeral=True)
        # Make sure they are in the server
        if not isinstance(member, discord.Member):
            await ctx.respond(member.mention + " is not in this server")
            return
        vperm = await get_user_perm_level(member)
        # Prevent self-kick
        if ctx.user.id == member.id:
            await ctx.respond("You cannot kick yourself", ephemeral=True)
            return
        # Make sure there is not already a vote
        nc: discord.CategoryChannel = bot.get_channel(VOTE_CATEGORY.id) # Re-check cache
        if not nc:
            nc = SERVER.fetch_channel(VOTE_CATEGORY.id) # No cache for some reason, manually fetch the channel
        for vc in nc.channels:
            if vc.name.startswith("kick-") and str(member.id) in vc.topic:
                await ctx.respond("There is already a kick vote going on in " + vc.mention, ephemeral=True)
                return
        # Make sure they can be kicked
        if vperm >= config['permissions']['bypass_votekick']:
            await ctx.respond("You cannot votekick " + member.mention + " because they have " + LEVEL_ROLE_MAP[vperm].mention, ephemeral=True)
            return
        if vperm < 0:
            await ctx.respond("You cannot kick " + member.mention, ephemeral=True)
            return
        # Set vote permissions
        perms = set_vote_channel_perms(config['permissions']['allow_kick_vote'], ctx.user, member)
        # Create the channel
        c = await VOTE_CATEGORY.create_text_channel("kick-" + member.name, reason="Votekick started", topic="Vote to kick " + member.mention, overwrites=perms)
        # Send the message
        m = await c.send(embed=discord.Embed(
            color=discord.Color.blurple(),
            title="Votekick",
            description=f"{ctx.user.mention} wants to kick {member.mention}. {str(config['features']['kick']['votekick']['required_votes'] + 1)} reactions are required."
        ))
        # Add tallys
        await m.add_reaction("✅")
        await m.add_reaction("❌")
        # Send a link to the channel
        await ctx.respond(f"Go to {c.mention}", ephemeral=True)
        await admin_log(discord.Embed(color=discord.Color.blue(), title="Votekick started", description=f"{ctx.user.mention} started a votekick for {member.mention}"))

### Forcekick

if config['features']['kick']['forcekick']['enabled']:
    @bot.user_command(name="kick")
    @requireperm(config['permissions']['allow_forcekick'])
    @election_lock(config['features']['kick']['disable_on_election'])
    @commands.cooldown(config['features']['kick']['forcekick']['times'], config['features']['kick']['forcekick']['cooldown'], commands.BucketType.user)
    async def forcekick_cmd(ctx: discord.ApplicationContext, member: discord.Member):
        # TODO: Delete the channel if they leave
        await ctx.defer(ephemeral=True)
        # Make sure they are in the server
        if not isinstance(member, discord.Member):
            await ctx.respond(member.mention + " is not in this server")
            return
        vperm = await get_user_perm_level(member)
        # Make sure they can be kicked
        if ctx.user.id == member.id:
            await ctx.respond("You cannot kick yourself", ephemeral=True)
            return
        if vperm >= config['permissions']['bypass_forcekick']:
            await ctx.respond("You cannot kick " + member.mention + " because they have " + LEVEL_ROLE_MAP[vperm].mention, ephemeral=True)
            return
        if vperm < 0:
            await ctx.respond("You cannot kick " + member.mention, ephemeral=True)
            return
        # Kick them
        await member.kick(reason=f"Forcekicked by {ctx.user.name} ({ctx.user.id})")
        await ctx.respond(f"{member.mention} was kicked", ephemeral=True)
        await ANNOUNCE_CHANNEL.send(f"{member.mention} was kicked by {ctx.user.mention}")
        await admin_log(discord.Embed(color=discord.Color.red(), title="Member was forcekicked", description=f"{ctx.user.mention} forcekicked {member.mention}"))

## Promotion (guest > plus)

if config['features']['plusvote']['enabled']:
    @bot.user_command(name="promote")
    @requireperm(config['permissions']['allow_promote_start'])
    @election_lock(config['features']['plusvote']['disable_during_election'])
    @commands.cooldown(config['features']['plusvote']['times'], config['features']['plusvote']['cooldown'], commands.BucketType.user)
    async def promote_user_cmd(ctx: discord.ApplicationContext, member: discord.Member):
        # TODO: Delete the channel if they leave
        # TODO: Make sure there can only be one vote per user
        await ctx.defer(ephemeral=True)
        # Make sure they are in the server
        if not isinstance(member, discord.Member):
            await ctx.respond(member.mention + " is not in this server")
            return
        # Prevent self-promote
        if ctx.user.id == member.id:
            await ctx.respond("You cannot promote yourself", ephemeral=True)
            return
        # Make sure there is not already a vote
        nc: discord.CategoryChannel = bot.get_channel(VOTE_CATEGORY.id) # Re-check cache
        if not nc:
            nc = SERVER.fetch_channel(VOTE_CATEGORY.id) # No cache for some reason, manually fetch the channel
        for vc in nc.channels:
            if vc.name.startswith("promote-") and str(member.id) in vc.topic:
                await ctx.respond("There is already a promotion vote going on in " + vc.mention, ephemeral=True)
                return
        # Make sure they can be promoted
        vperm = await get_user_perm_level(member)
        if vperm > 0:
            await ctx.respond(member.mention + " already has extra permissions", ephemeral=True)
            return
        if vperm < 0:
            await ctx.respond("You cannot promote " + member.mention, ephemeral=True)
            return
        if member.id in join_dt and (join_dt[member.id] + timedelta(hours=config['features']['plusvote']['required_wait'])) > datetime.now():
            await ctx.respond(f"{member.mention} needs to be in the server for at least {str(config['features']['plusvote']['required_wait'])} hours before you can promote them")
            return
        # Set vote permissions
        perms = set_vote_channel_perms(config['permissions']['allow_promote_vote'], ctx.user, member)
        # Create the channel
        c = await VOTE_CATEGORY.create_text_channel("promote-" + member.name, reason="Promotion started", topic="Vote to promote " + member.mention, overwrites=perms)
        # Send the message
        m = await c.send(embed=discord.Embed(
            color=discord.Color.blue(),
            title="Promotion",
            description=f"{ctx.user.mention} wants to promote {member.mention} to {PLUS_ROLE.mention}. {str(config['features']['plusvote']['required_votes'] + 1)} reactions are required."
        ))
        # Add tallys
        await m.add_reaction("✅")
        await m.add_reaction("❌")
        # Send a link to the channel
        await ctx.respond(f"Go to {c.mention}", ephemeral=True)
        await admin_log(discord.Embed(color=discord.Color.blue(), title="Promote vote started", description=f"{ctx.user.mention} started a promotion for {member.mention}"))

## Fun

### Timeout

if config['features']['fun']['timeout']['enabled']:
    @bot.user_command(name="timeout")
    @requireperm(config['permissions']['allow_timeout'])
    @commands.cooldown(config['features']['fun']['timeout']['times'], config['features']['fun']['timeout']['cooldown'], commands.BucketType.user)
    async def timeout_cmd(ctx: discord.ApplicationContext, member: discord.Member):
        perm = await get_user_perm_level(ctx.user)
        await ctx.defer()
        # Get the duration the timeout should last
        dur = config['features']['fun']['timeout']['leader_duration'] if perm == 4 else config['features']['fun']['timeout']['duration']
        # Time them out
        await member.timeout_for(timedelta(seconds=dur))
        # Log it
        logger.info("'%s' timed out '%s' for %s seconds", ctx.user.name, member.name, str(dur))
        await ctx.followup.send(f"{member.mention} has been timed out for {str(dur)} seconds.")

### UwU speak

# userid: expiration
uwuified: dict[int, datetime] = {}
uwu_dt_race_lock = Lock()

if config['features']['fun']['uwu']['enabled']:
    @bot.user_command(name="uwuify")
    @requireperm(config['permissions']['allow_uwuify'])
    @commands.cooldown(config['features']['fun']['uwu']['times'], config['features']['fun']['uwu']['cooldown'], commands.BucketType.user)
    async def uwuify_cmd(ctx: discord.ApplicationContext, member: discord.Member):
        await ctx.defer()
        perm = await get_user_perm_level(ctx.user)
        dur = config['features']['fun']['uwu']['leader_duration'] if perm == 4 else config['features']['fun']['uwu']['duration']
        with uwu_dt_race_lock:
            uwu_end = uwuified.get(member.id, None)
            if uwu_end and uwu_end > datetime.now():
                # extra second for lag
                uwuified[member.id] += timedelta(seconds=dur + 1)
                await ctx.respond(f"{member.mention} has been uwuified for an extra {str(dur)} seconds")
            else:
                uwuified[member.id] = datetime.now() + timedelta(seconds=dur)
                await ctx.respond(f"{member.mention} has been uwuified for {str(dur)} seconds")

## Server customization

if config['features']['modify']['rename']:
    # pre-compile the regex
    srv_rename_regex = re.compile(config['features']['modify']['rename_regex'])
    # actual command
    @bot.slash_command(name="rename", description="Rename the server")
    @discord.guild_only()
    @option("name", description="The new server name")
    @requireperm(config['permissions']['allow_server_rename'])
    async def srv_rename_cmd(ctx: discord.ApplicationContext, name: str):
        await ctx.defer(ephemeral=True)
        # Make sure the name complies
        if not re.fullmatch(srv_rename_regex, name):
            await ctx.respond(config['features']['modify']['rename_fail_msg'], ephemeral=True)
            return
        # Change the name
        await SERVER.edit(name=name, reason=f'{ctx.user.name} ({ctx.user.id}) requested name change')
        await ANNOUNCE_CHANNEL.send("Server name changed by " + ctx.user.mention + ": " + name)
        await ctx.respond("Name updated", ephemeral=True)

if config['features']['modify']['change_icon']:
    @bot.slash_command(name="newicon", description="Change the server icon")
    @discord.guild_only()
    @option("name", description="The new server name")
    @requireperm(config['permissions']['allow_icon_change'])
    async def srv_change_icon(ctx: discord.ApplicationContext, icon: discord.Attachment):
        await ctx.defer(ephemeral=True)
        await ctx.guild.edit(icon=await icon.read(), reason=f'{ctx.user.name} ({ctx.user.id}) requested icon change')
        await ANNOUNCE_CHANNEL.send("Server icon changed by " + ctx.user.mention)
        await ctx.respond("Icon changed")

if config['features']['modify']['rename_roles']:
    @bot.slash_command(name="renamerole", description="Rename a role") # could be named better, only for permissions roles
    @discord.guild_only()
    @option("role", description="The role to rename")
    @option("name", description="The new role name")
    @requireperm(config['permissions']['allow_perm_rename'])
    async def srv_role_rename_cmd(ctx: discord.ApplicationContext, role: discord.Role, name: str):
        logger.info("%s is requesting to rename %s to %s", ctx.user.name, role.name, name)
        level = None
        for v, r in LEVEL_ROLE_MAP.items():
            if r.id == role.id:
                logger.debug("Found correct role")
                level = v
                break
        ulevel = await get_user_perm_level(ctx.user)
        if not level:
            await ctx.respond("You can only rename these roles:\n- " + "\n- ".join([
                r.mention for l, r in LEVEL_ROLE_MAP.items() if ulevel >= l
            ]), ephemeral=True)
            return
        if ulevel < level:
            await ctx.respond("That role is higher than you")
        oname = role.name
        await role.edit(name=name, reason=f'{ctx.user.name} ({ctx.user.id}) requested a role name change')
        await ANNOUNCE_CHANNEL.send(f"Name of {role.mention} was changed by {ctx.user.mention}: {oname} -> {name}")
        await ctx.respond("Name updated", ephemeral=True)

# Events

## Most of these functions will have redundant code

@bot.event
async def on_member_join(member: discord.Member):
    logger.info("'%s' just joined the server", member.name)
    # Log their join time
    join_dt[member.id] = datetime.now()
    # Give them guest
    if not member.bot:
        await member.add_roles(GUEST_ROLE, reason="New member")
    # Give them vip if on the list
    if member.id in config['vips']:
        await member.add_roles(VIP_ROLE, reason="New VIP member")
    await admin_log(discord.Embed(color=discord.Color.orange(), title="New member", description=f"{member.mention} joined", fields=[discord.EmbedField("Is VIP", "Yes" if member.id in config['vips'] else "No")], timestamp=datetime.now()))

@bot.event
async def on_raw_member_remove(payload: discord.RawMemberRemoveEvent):
    logger.info("'%s' just left the server", payload.user.name)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    logger.debug("%s updated voice state", member.name)
    if not init_complete:
        return
    # Check if someone left a room, and it is now empty
    if config['features']['voice_rooms']['enabled'] and \
        before.channel and before.channel != after.channel \
        and before.channel.category and before.channel.category.id == VOICE_CATEGORY.id \
        and len(before.channel.members) == 0:
        # It is a room, and is now empty. Delete it
        if before.channel.id in vc_owners:
            del vc_owners[before.channel.id]
        try:
            await before.channel.delete(reason="The room is now empty")
            logger.info("Deleted stale voice channel")
        except discord.errors.NotFound:
            pass
        return
    # Save their current voice state
    if before.channel and before.channel == after.channel:
        logger.debug("Saving voice perms for %s", member.name)
        voice_capability_map[(member.id, after.channel.id)] = (after.mute, after.deaf, datetime.now() + timedelta(minutes=config['features']['voice_state_cache_duration']))
    # Restore that state, this might be buggy
    # TODO: add a Lock to this for race conditions
    if after.channel and before.channel != after.channel:
        if (member.id, after.channel.id) in voice_capability_map and voice_capability_map[(member.id, after.channel.id)][2] > datetime.now():
            logger.debug("Restoring voice perms for %s", member.name)
            vcm = voice_capability_map[(member.id, after.channel.id)]
            await member.edit(mute=vcm[0], deafen=vcm[1], reason="Restoring voice perms for channel")
        else:
            dmute = not after.channel.permissions_for(member).speak
            logger.debug("Clearing voice perms for %s, dmute is %s", member.name, str(dmute))
            await member.edit(mute=dmute, deafen=False, reason="Clearing voice perms for channel")
            voice_capability_map[(member.id, after.channel.id)] = (dmute, False, datetime.now() + timedelta(minutes=30))
    # Announce joins of the main channel
    if after.channel and after.channel.id == VOICE_CHANNEL.id and before.channel != after.channel:
        if len(after.channel.members) == 1 and config['features']['announce_main_call']['on_first_join']:
            await ANNOUNCE_CHANNEL.send(f"{member.mention} started a call in {VOICE_CHANNEL.mention}! @everyone")
        elif config['features']['announce_main_call']['on_join']:
            await ANNOUNCE_CHANNEL.send(f"{member.mention} joined {VOICE_CHANNEL.mention}")
    # Announce leaves of the main channel
    if before.channel and before.channel.id == VOICE_CHANNEL.id and before.channel != after.channel:
        if len(before.channel.members) == 0 and config['features']['announce_main_call']['on_last_leave']:
            if config['features']['announce_main_call']['on_leave']:
                await ANNOUNCE_CHANNEL.send(f"{member.mention} left and ended the call in {VOICE_CHANNEL.mention}")
            else:
                await ANNOUNCE_CHANNEL.send(f"The call in {VOICE_CHANNEL.mention} has ended")
        elif config['features']['announce_main_call']['on_leave']:
            await ANNOUNCE_CHANNEL.send(f"{member.mention} left {VOICE_CHANNEL.mention}")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    logger.debug("'%s' reacted to a message with '%s'", payload.member.name, payload.emoji.name)
    if payload.user_id == bot.user.id:
        return
    channel = bot.get_channel(payload.channel_id) # Check cache
    if not channel:
        channel = SERVER.fetch_channel(payload.channel_id)
    message: discord.Message = await channel.fetch_message(payload.message_id) # reminder: bot.get_message uses the cache, we cannot use the cache here because it does not get updated here for some reason
    # warning: whole lotta nesting ahead
    # I tried commenting as much as possible, idk if it actually helps readability
    # Reminder: Do not put a lock over this whole section because the overthrow section has a long running function
    if message.channel.category_id == VOTE_CATEGORY.id and message.author.id == bot.user.id:
        logger.debug("A message was reacted in the vote category")
        match message.channel.name.split("-")[0]:
            case "kick":
                # Votekick
                logger.debug("A message was reacted in a votekick channel")
                reaction = next((r for r in message.reactions if str(r.emoji) == str(payload.emoji)), None)
                # yes i know this is not >=, the bot adds one extra "vote" because of reaction limitations
                if reaction and reaction.count > config['features']['kick']['votekick']['required_votes']:
                    # Get everyone who was for kick, exclude bots
                    approved = [u for u in await reaction.users().flatten() if not u.bot]
                    if payload.emoji.name == "✅":
                        # Get all opposed, exclude bots
                        opposed = [u for u in await next((r for r in message.reactions if str(r.emoji) == "❌"), None).users().flatten() if not u.bot]
                        # Get the person
                        member = SERVER.get_member(int(message.channel.topic.split("<@")[-1].removesuffix(">")))
                        await message.clear_reactions()
                        await message.channel.edit(topic="! The vote passed!")
                        if member:
                            # Great! They are still in the server, kick them.
                            await member.kick(reason=f"Votekick passed! ({len(approved)}-{len(opposed)})")
                            # log it
                            await admin_log(discord.Embed(color=discord.Color.green(), title="Votekick passed!", description=f"{member.mention} was kicked.", fields=[discord.EmbedField("Yay", str(len(approved)) + " people voted for a kick\n" + (", ".join([vmember.mention for vmember in approved]))), discord.EmbedField("Nay", str(len(opposed)) + " people voted against a kick\n" + (", ".join([vmember.mention for vmember in opposed])))]))
                            await message.channel.send(f"{member.mention} was kicked! The results were {len(approved)}-{len(opposed)}")
                            await ANNOUNCE_CHANNEL.send(f"{member.mention} was kicked by a {len(approved)}-{len(opposed)} vote.")
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote passed!")
                        else:
                            # race condition (probably)
                            await message.channel.send("Could not find <@" + message.channel.topic.split("<@")[-1])
                            await admin_log(discord.Embed(color=discord.Color.yellow(), title="Votekick passed with error", description="The votekick passed, but the user was not found"))
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote passed and member was not found.")
                    elif payload.emoji.name == "❌":
                        # Get all opposed, exclude bots
                        opposed = [u for u in await next((r for r in message.reactions if str(r.emoji) == "✅"), None).users().flatten() if not u.bot]
                        # Get the person
                        member = SERVER.get_member(int(message.channel.topic.split("<@")[-1].removesuffix(">")))
                        await message.clear_reactions()
                        await message.channel.edit(topic="! The vote failed")
                        if member:
                            # Send confirmation
                            await message.channel.send(f"The vote failed. The results were {len(approved)}-{len(opposed)}")
                            # log it
                            await admin_log(discord.Embed(color=discord.Color.red(), title="Votekick failed", description=f"{member.mention} was not kicked. The results were {len(approved)}-{len(opposed)}", fields=[discord.EmbedField("Yay", str(len(opposed)) + " people voted for a kick\n" + (", ".join([vmember.mention for vmember in opposed]))), discord.EmbedField("Nay", str(len(approved)) + " people voted against a kick\n" + (", ".join([vmember.mention for vmember in approved])))]))
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote failed")
                        else:
                            # race condition (probably)
                            await message.channel.send("The vote failed")
                            await admin_log(discord.Embed(color=discord.Color.yellow(), title="Votekick failed with error", description="The votekick failed and the user was not found"))
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote failed and member was not found.")
            case "promote":
                # Copied from Votekick
                logger.debug("A message was reacted in a promotion channel")
                reaction = next((r for r in message.reactions if str(r.emoji) == str(payload.emoji)), None)
                # yes i know this is not >=, the bot adds one extra "vote" because of reaction limitations
                if reaction and reaction.count > config['features']['plusvote']['required_votes']:
                    # Get everyone who was for promotion, exclude bots
                    approved = [u for u in await reaction.users().flatten() if not u.bot]
                    if payload.emoji.name == "✅":
                        # Get all opposed, exclude bots
                        opposed = [u for u in await next((r for r in message.reactions if str(r.emoji) == "❌"), None).users().flatten() if not u.bot]
                        # Get the person
                        member = SERVER.get_member(int(message.channel.topic.split("<@")[-1].removesuffix(">")))
                        await message.clear_reactions()
                        await message.channel.edit(topic="! The vote passed!")
                        if member:
                            # Great! They are still in the server, promote them.
                            await member.add_roles(PLUS_ROLE, reason=f"Promotion passed! ({len(approved)}-{len(opposed)})")
                            # log it
                            await admin_log(discord.Embed(color=discord.Color.green(), title="Promotion passed!", description=f"{member.mention} was given {PLUS_ROLE.mention}.", fields=[discord.EmbedField("Yay", str(len(approved)) + " people voted for a promotion\n" + (", ".join([vmember.mention for vmember in approved]))), discord.EmbedField("Nay", str(len(opposed)) + " people voted against a promotion\n" + (", ".join([vmember.mention for vmember in opposed])))]))
                            await message.channel.send(f"{member.mention} was promoted to {PLUS_ROLE.name}! The results were {len(approved)}-{len(opposed)}")
                            # i dislike how this mentions everyone with the role, but considering this name can change, it's better to let discord show the role name
                            await ANNOUNCE_CHANNEL.send(f"{member.mention} was promoted to {PLUS_ROLE.mention} by a {len(approved)}-{len(opposed)} vote.")
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote passed!")
                        else:
                            # race condition (probably)
                            await message.channel.send("Could not find <@" + message.channel.topic.split("<@")[-1])
                            await message.clear_reactions()
                            await admin_log(discord.Embed(color=discord.Color.yellow(), title="Promotion passed with error", description="The promotion passed, but the user was not found"))
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote passed and member was not found.")
                    elif payload.emoji.name == "❌":
                        # Get all opposed, exclude bots
                        opposed = [u for u in await next((r for r in message.reactions if str(r.emoji) == "✅"), None).users().flatten() if not u.bot]
                        # Get the person
                        member = SERVER.get_member(int(message.channel.topic.split("<@")[-1].removesuffix(">")))
                        await message.clear_reactions()
                        await message.channel.edit(topic="! The vote failed")
                        if member:
                            # Send confirmation
                            await message.channel.send(f"The vote failed. The results were {len(approved)}-{len(opposed)}")
                            # log it
                            await admin_log(discord.Embed(color=discord.Color.red(), title="Promotion failed", description=f"{member.mention} was not promoted. The results were {len(approved)}-{len(opposed)}", fields=[discord.EmbedField("Yay", str(len(opposed)) + " people voted for a promotion\n" + (", ".join([vmember.mention for vmember in opposed]))), discord.EmbedField("Nay", str(len(approved)) + " people voted against a promotion\n" + (", ".join([vmember.mention for vmember in approved])))]))
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote failed")
                        else:
                            # race condition (probably)
                            await message.channel.send("The vote failed")
                            await admin_log(discord.Embed(color=discord.Color.yellow(), title="Promotion failed with error", description="The promotion failed and the user was not found"))
                            await asyncio.sleep(60)
                            await message.channel.delete(reason="Vote failed and member was not found.")
            case "overthrow":
                if payload.emoji.name == "✅":
                    reaction = next((r for r in message.reactions if str(r.emoji) == str(payload.emoji)), None)
                    if reaction and reaction.count > config['features']['leader']['overthrow']:
                        await message.clear_reactions()
                        # Remove the role from everyone who has it
                        nleader = SERVER.get_role(LEADER_ROLE.id)
                        if not nleader:
                            nleader = await SERVER.fetch_role(LEADER_ROLE.id)
                        m = None
                        for m in nleader.members:
                            if config['features']['leader']['overthrow_kick']:
                                await m.kick(reason="Overthrown")
                            else:
                                await m.remove_roles(nleader, reason="Overthrown")
                        if m:
                            await message.channel.send(f"{m.mention} has been overthrown!")
                            await ANNOUNCE_CHANNEL.send(f"{m.mention} has been overthrown!")
                            await admin_log(
                                discord.Embed(
                                    color=discord.Color.dark_teal(),
                                    title="Leader overthrown",
                                    description=f"{LEADER_ROLE.mention} ({m.mention}) has been overthrown"
                                )
                            )
                            await message.channel.edit(topic=f"! {m.mention} has been overthrown!")
                            await asyncio.sleep(120)
                            await message.channel.delete(reason="Vote passed!")
                            if config['features']['leader']['overthrow']:
                                await election_start(f"{m.mention} has been overthrown!")
                        else:
                            await message.channel.send("Could not find the current leader")
                            await ANNOUNCE_CHANNEL.send("Could not find the current leader")
                            await admin_log(
                                discord.Embed(
                                    color=discord.Color.dark_red(),
                                    title="Leader overthrown with error",
                                    description=f"{LEADER_ROLE.mention} was overthrown, but no user was found. Did they leave?"
                                )
                            )
                            await message.channel.edit(topic="! Could not find the current leader")
                            await asyncio.sleep(120)
                            await message.channel.delete(reason="Vote passed!")

arlist: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(s['match']), s['send'], s['delete'])
    for s in config['features']['fun']['autoreply']
]

@bot.event
async def on_message(message: discord.Message):
    if isinstance(message.channel, discord.DMChannel):
        return # guild only!
    logger.debug("New message from '%s' in %s", message.author.name, message.channel.name)
    if message.author.bot:
        return
    # Autoreply
    for ar in arlist:
        # Check the regex
        if re.fullmatch(ar[0], message.content):
            # It matches!
            logger.info("Sending autoreply: %s", ar[1])
            # If we are going to delete the message, do not reply to it
            sr = {"reference": message} if not ar[2] else {}
            # Send our reply
            await message.channel.send(ar[1], **sr)
            # If set to delete the message, delete it
            if ar[2]:
                await message.delete(reason="Autoreply")
                return # the message doesnt exist anymore, we should not continue
    # UWUified
    # If more types of channels support webhooks, make an issue
    if message.author.id in uwuified and (isinstance(message.channel, (discord.TextChannel, discord.VoiceChannel))):
        uwuify_end = uwuified[message.author.id]
        if uwuify_end > datetime.now():
            # They have been uwuified
            # Check for a webhook
            hooks = await message.channel.webhooks()
            uwu_hook = None
            for hook in hooks:
                if hook.name == "uwu" and hook.token:
                    uwu_hook = hook
                    break
            if not uwu_hook:
                uwu_hook = await message.channel.create_webhook(name="uwu", reason="UwUify used and no webhook available")
            await uwu_hook.send(content=uwulib.uwuify(message.content), username=message.author.display_name, avatar_url=message.author.avatar.url)
            await message.delete(reason="UwUified")
            return # the message doesnt exist anymore, we should not continue
        # Time is up, remove them from the dict
        uwuified.pop(message.author.id)

# Errors

@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):#
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
        try:
            await ctx.respond("Command failed: " + type(error).__name__, ephemeral=True)
        except:
            pass
        raise error

# Background

@tasks.loop(minutes=30)
async def expire_old_vars():
    now = datetime.now()
    # VCM
    expired_c = [k for k, v in voice_capability_map.items() if v[2] < now]
    if expired_c:
        for key in expired_c:
            voice_capability_map.pop(key, None)
        logger.debug("Cleaned %s expired vcms", str(len(expired_c)))
    # UwUified
    expired_c = [k for k, v in uwuified.items() if v < now]
    if expired_c:
        for key in expired_c:
            uwuified.pop(key, None)
        logger.debug("Cleaned %s expired uwuifies", str(len(expired_c)))

expire_old_vars.start()

# Let's run!

bot.run(config['token'])

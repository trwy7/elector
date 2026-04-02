# Electoric

A bot for medium sized friend group servers of 10-20 people, should only be used with up to 60 people, although techinally 100 may work.

## Important

Updating the bot may require manually editing the configuration file. After an update, always check the bots logs, you should not have this bot automatically update.

This bot requires that you have a discord alt account. This alt account needs to own the server, your main should not have admin permissions.

## Installation

### Create a server

Create a server using the template at https://discord.new/yp7wShJw3bkD

Feel free to rename channels and roles, but do not change permissions on the log channel, the rooms category and votes category. You may also assign additional permissions to roles, but you should not give them manage roles, manage channels, or administrator. If someone can do something they should not be able to, make sure they dont have a role that gives them that permission (use the audit log).

#### I already have a server!

Use the template to define permissions exactly as they are in the template. Your server should end looking as similar as possible to the main server.

### Create the bot

Head to https://discord.com/developers/applications and create a new application. Call it whatever you want.

Go to `Installation` on the sidebar, and disable `User Install`, and change the install link to `None`.

Go to `Bot` on the sidebar Disable `Public Bot`, and enable `Server Members Intent` and `Message Content Intent` and save your changes. Then scroll up and generate a new token, copy it and save it for later. Do not make your bot token public.

Go to `OAuth2` on the sidebar, scroll to `OAuth2 URL Generator`, and check `bot`, then scroll to `Bot Permissions` and enable `Administrator`. Scroll to the very bottom and go to the generated URL, and add the bot to your server.

### Configuration

Run the bot either using [docker compose](https://github.com/trwy7/elector/blob/main/docker-compose.yaml) (recommended), or by installing dependencies and running main.py

<details>
   <summary>
      <h4>▶️ docker-compose.yml template</h4>
   </summary>

Before starting the container, make a data directory and run `chown -R 10001:10001 data`. You may get permission errors if you do not. After the config has been created, stop the container, and turn it back on after you have filled out the config.

```yaml
name: elector
services:
  electoric:
    image: ghcr.io/trwy7/elector:1.0.0 # You may want to update this with the latest release version. "latest" is mapped to the latest commit, not latest release, and it may contain bugs
    user: "10001:10001"
    environment:
      - TZ=America/New_York # Remember to change to your timezone
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

</details>

After first launch, a config file will be created in ./data/config.yml

A lot of configuration requires you to input a user/role/channel id, you can get these by going into discord settings then advanced, then turning on developer mode. Finally, go to the user/role/channel you want the ID for, and right click it and pick copy ID, you can then paste it into the config file. If you are using the template, you should copy the ids of these into your config file:

- /channels
  - /public: #texting
  - /voice: #General
  - /voice_rooms_category: The rooms category on the sidebar (right click for id)
  - /vote_category: The votes category
  - /logs: #log
- /roles
  - /leader: `leader` role
  - /vice-leader: `vice` role
  - /vip_role: `vip` role
  - /plus_role: `plus` role
  - /guest_role: `guest` role

There is a lot of configuration, you should scroll through and read what everything does and configure what you do not like! Remember to fill out the token value! You should also fill out the VIP list with important people.

### Finalizing

The bot assumes you do not have administrator, so give server ownership to an alt account, and remove all it's roles (including guest). Now, invite everyone you want! Feel free to make an issue if there are any features you want or bugs you find.

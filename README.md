# tvh-m3u-genarator
Generate an M3U (.m3u) formatted Playlist from TVHeadend Server, based on user/s and tag/s access. 

This container intends to improve the formatting of channel lists from TVHeadend for IPTV Players e.g. Tivimate. A web server will be available offering the urls for an M3U formatted channel list, and a proxied URL for the EPG (including the retention of historic data, if applicable).

**NOTE**: The container has only been tested on a Raspberry Pi (Raspberry PI OS) with Tivimate IPTV Player.

## Method

### Channel Playlist

- Download all the tags set up on the TVH server.
- Download a channel list, for each user and tag combination, using the user credential provided in TVH_USERS (this can result in empty lists depending on users not having access to certain tags). Multiple users can be provided, seperated by a comma (see example Docker Compose below).
- Combine all the lists downloaded.
- Downloads will follow the tag order defined in TVHendend Server.
- Add a Group-Title TVG tag, based on the tag name.
- Add persistent password into the stream URLs (the first TVH_USERS persistent password or TVH_URL_AUTH if provided).
- System default Streaming Profile removed from channel URLs, which are automatically added when downloading channel lists. TVH will then choose streaming profile based on server setup/user access when a channel is played.
- Compile a single list and cache it, when the container is started. A refreshed list will be created once the refresh interval expires (see REFRESH_INTERVAL).

### Electronic Programme Guide (EPG)
  
- Proxy EPG
- EPG retreived using persistent password (TVH_URL_AUTH or TVH_USERS)
- TVH Server may return EPG with Start and End times of shows being set to local time including DST, and also set an offet "+0100". In tivimate this causes the EPG to move an additional hour. EPG_STRIP_OFFSET can remove any offset TVH applies.
- Optionally, Retain EPG, based on Days or File Size, and serve it with refreshed EPG data from TVH Server. 

## Web View

Web view added to do/see the following:

- Provide URL for Channel List and EPG XML files.
- Datetime stamp when cache list was last updated.
- Table showing the channels in the cached list (column for each attribute in the file).
- Light/Dark for easy viewing of different types of icons.
- Channel List manual refresh button, for use after TVH server changes and to avoid waiting or restarting of the container.

## Docker Compose Example:

```
services:
  tvh-m3u-generator:
    image: harrysingh1991/tvh-m3u-generator:latest
    container_name: tvh-m3u-generator # set name for container 
    ports:
      - "9985:9985" # Port to access web server
    environment:
    #Mandatory 
      TVH_USERS: "user1:persistentpdw1,user2:persistentpwd2" # User/s to retrieve lists for      
    #Optional - see defaults
      TVH_HOST: "192.168.0.2" # TVHeadend Server IP Address/URL
      TVH_PORT: "9981" # TVHeadend Server Port
      TZ: "Europe/London" # Account for DST time changes
      REFRESH_SCHEDULE: 0 5 * * * # Set Playlist  refresh schedule (and EPG refresh schedule if EPG_REFRESH_SCHEDULE not defined)
      TVH_URL_AUTH: "otherpersistentpassword" # Only needed if using a different account to retrieve EPG then first user defined in TVH_USERS
      TVH_APPEND_ICON_AUTH: "1" # Add 'auth?=PersistentPassword' to icon path
      EPG_STRIP_OFFSET: "1"  # Remove DST offset in epg file
      EPG_RETENTION_ENABLED: "1" # Turn on EPG retention
      EPG_RETENTION_DAYS: "2" # Days of EPG Retention
      EPG_RETENTION_SIZE_MB: "75" # EPG Size including retention
      EPG_REFRESH_SCHEDULE: 0 5 * * * # Set EPG refresh schedule, only if different schedule is required to playlist refresh
      CREATE_CACHE: 1 # Generate Playlist and EPG file during startup, only if missing
    volumes:
      - ./archive:/app/archive #If not defined. EPG retention will not persist between restarts
    restart: always # ensure container restarts after system restart
    depends_on:
      - tvheadend # Start after TVHeadend has been started
```
## Variables

| Variable | Type | Description | Default | Example |
| ------------- | ------------- | ------------- | ------------- | ------------- |
| `TVH_USERS` | Mandatory | Username and persistent password of TVHeadend User/s. Comma seperated if using multiple users. If TVH_URL_AUTH is not defined, the EPG data will be retrieved using User details of the 1st User declared | No Default. Must be defined | user:persistentpwd |
| `TVH_HOST` | Optional | Domain/IP Address of TVheadend Server | `127.0.0.1` | `192.168.0.2` |
| `TVH_PORT` | Optional | Port of the TVHeadend Web Server | `9985` | `9985` |
| `TZ` | Optional | If your country uses Daylight Savings Time for part of the year, the correct time may not be passed through to container. Set your local timezone to ensure the container uses your local time | No Default | `TZ: "Europe/London"` |
| `REFRESH_SCHEDULE` | Optional | Cron based schedule to refresh a cached playlist and EPG xml file | `0 5 * * *` | `0 5 * * *` |
| `TVH_URL_AUTH` | Optional | EPG data can be retrieved using a different user than the 1st User declared in TVH_USERS. Ensure the user of the password being declared, has access to all the channels you need EPG data for | No Default | `persistentpwd` |
| `TVH_APPEND_ICON_AUTH` | Optional | If icons for channels are stored locally, depending on TVHeadend server setup, authentication may be required. If variable is set to one of the following - `true,1,yes`, then the script will add `auth=persistentpwd` to the end of the Channel Icon URL | `0` | `1` |
| `EPG_STRIP_OFFSET` | Optional | TVHeadend may apply an offset to EPG data e.g.`+0100', and also adjust the time of the programme. so the time is no longer shown before your local timezone/DST is applied. Enabling this option will remove the offset. IPTV Players can apply the offset, on top of the adjustment made by TVHeadend, resulting in EPGs being out. To turn retention on, set the variable to one fo the following value: `1/true/yes` | `1` | `0` |
| `EPG_RETENTION_ENABLED` | Optional | Enable retention of historical EPG data. Retention can be based on Days or File Size, or both. See EPG_RETENTION_DAYS and EPG_RETENTION_SIZE_MB | `0` | `1` |
| `EPG_RETENTION_DAYS` | Optional | If retention is enabled, you can set a limit to how many days of historical EPG data you want to retain | `2` | `4` |
| `EPG_RETENTION_SIZE_MB` | Optional | If retention has been enabled, you can set a limit to the file size of the EPG data. This is in megabytes (MB) | `75` | `50` |
| `EPG_REFRESH_SCHEDULE` | Optional | Cron based schedule to refresh EPG data only, if you want to use a seperate schedule to the playlist refresh | See REFRESH_SCHEDULE default | `0 5 * * *` |
| `CREATE_CACHE` | Optional  | The script can run without having previously had a playlist and EPG data cached and saved. If this is the case, the script can generate a playlist and EPG xml file, before starting the web server. Set the variable to one of the following values: `1/true/yes`. If you want this off, set to `0/false/off`. See Volume `/app/Archive`. | `0` | `1` |

## Data Volumes

| Volume | Type | Description | Default | Example |
| ------------- | ------------- | ------------- | ------------- | ------------- |
| `/app/archive` | Optional | This folder is where the cached playlist and EPG data are saved. **IMPORTANT**: If it is not set, the cached files are saved in memory. The data will not be persistent and be retained between restarts if it is not set! | No Default. Should be defined if you want data to persist between restarts | `/your/local/folder:/app/archive` |

## Planned Improvements

1. Empty lists insert an EXTM3U tag (users with restricted access to certain tags). Find a way to ignore empty tag lists (looks cleaner)

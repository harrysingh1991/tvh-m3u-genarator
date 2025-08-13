import logging
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
import eventlet
eventlet.monkey_patch()
import os
import time
import requests
from flask import Flask, Response, redirect, url_for, request, render_template
import re
import urllib.parse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import atexit
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv
from lxml import etree
from datetime import datetime, timedelta
from cron_descriptor import get_description, ExpressionDescriptor

# --- 1. Environment Setup and Configuration ---
load_dotenv()

# Environment variables with defaults
TVH_HOST = os.getenv("TVH_HOST","127.0.0.1")
TVH_PORT = int(os.getenv("TVH_PORT","9981"))
REFRESH_SCHEDULE = os.getenv("REFRESH_SCHEDULE", "0 5 * * *")  # Default: 5am every day
EPG_REFRESH_SCHEDULE = os.getenv("EPG_REFRESH_SCHEDULE") or REFRESH_SCHEDULE
TVH_USERS = os.getenv("TVH_USERS")
TVH_APPEND_ICON_AUTH = os.getenv("TVH_APPEND_ICON_AUTH", "0").lower() in ("1", "true", "yes")
EPG_STRIP_OFFSET = os.getenv("EPG_STRIP_OFFSET", "0").lower() in ("1", "true", "yes")
EPG_RETENTION_ENABLED = os.getenv("EPG_RETENTION_ENABLED", "0").lower() in ("1", "true", "yes")
EPG_RETENTION_DAYS = int(os.getenv("EPG_RETENTION_DAYS", "2"))
EPG_RETENTION_SIZE_MB = int(os.getenv("EPG_RETENTION_SIZE_MB", "50"))
ARCHIVE_DIR = "archive"

# Function to check and log environment variables.
def check_and_log_env_vars():
    required_vars = [
        "TVH_USERS",
    ]
    missing_vars = []
    for var in required_vars:
        value = os.getenv(var)
        if value is None or value == "":
            logging.error(f"Missing required environment variable: {var}")
            missing_vars.append(var)
        else:
            logging.info(f"ENV CHECK: {var} is set to '{value}'")

    # Always check and ensure ARCHIVE_DIR exists and is writable
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        test_file = os.path.join(ARCHIVE_DIR, ".write_test")
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        logging.info(f"Archive directory '{ARCHIVE_DIR}' is accessible and writable.")
    except Exception as e:
        logging.error(f"Archive directory '{ARCHIVE_DIR}' cannot be created or accessed: {e}")
        missing_vars.append("ARCHIVE_DIR_ACCESS")

    if EPG_RETENTION_ENABLED:
        epg_retention_vars = [
            "EPG_RETENTION_DAYS",
            "EPG_RETENTION_SIZE_MB",
        ]
        for var in epg_retention_vars:
            value = os.getenv(var)
            if value is None or value == "":
                logging.error(f"Missing required environment variable for EPG retention: {var}")
                missing_vars.append(var)
            else:
                logging.info(f"ENV CHECK: {var} is set to '{value}'")

    if missing_vars:
        logging.error(f"Exiting due to missing or inaccessible environment variables: {', '.join(missing_vars)}")
        exit(1)
    #End of check_and_log_env_vars

# Check environment variables and log their status   
check_and_log_env_vars()

# Function to parse user credentials from the environment variable
def parse_users(creds_str):
    logging.info("Parsing user credentials")
    pairs = [u.strip() for u in creds_str.split(",") if ":" in u]
    users = [{"user": u.split(":")[0], "pass": u.split(":")[1]} for u in pairs]
    logging.info(f"Parsed users: {[u['user'] for u in users]}")
    return users

# Parse TVH users from the environment variable
USERS = parse_users(TVH_USERS)

# Function to get TVH URL authentication token
def get_tvh_url_auth(users):
    tvh_url_auth = os.getenv("TVH_URL_AUTH")
    if not tvh_url_auth or tvh_url_auth.strip() == "":
        if users and "pass" in users[0]:
            tvh_url_auth = users[0]["pass"]
            logging.info(f"TVH_URL_AUTH not set, using password of first user: {users[0]['user']}")
        else:
            tvh_url_auth = ""
            logging.warning("TVH_URL_AUTH not set and no users found; EPG fetching may fail.")
    return tvh_url_auth

# Get/Set TVH URL authentication token
TVH_URL_AUTH = get_tvh_url_auth(USERS)

def initial_refresh_check():
    if os.getenv("CREATE_CACHE", "0").lower() not in ("1", "true", "yes"):
        return

    playlist_path = os.path.join(ARCHIVE_DIR, "playlist.m3u")
    epg_path = os.path.join(ARCHIVE_DIR, "epg_retained.xml")

    playlist_missing = not os.path.exists(playlist_path)
    epg_missing = EPG_RETENTION_ENABLED and not os.path.exists(epg_path)

    if playlist_missing:
        logging.info("No persistent playlist found. Running initial playlist refresh...")
        build_and_cache_playlist_once(send_refresh=False)
    if epg_missing:
        logging.info("No persistent EPG found. Running initial EPG refresh...")
        merge_epg(send_refresh=False)

# --- 2. Flask App and Globals ---
app = Flask(__name__)
socketio = SocketIO(app)
last_refresh_time = 0
last_playlist_update = 0
cached_playlist = None
SERVER_START_TIME = int(time.time())

base_url = f"http://{TVH_HOST}:{TVH_PORT}"

# --- 3. Utility/Helper Functions ---

def humanify_cron(cron_str):
    try:
        desc = get_description(cron_str)
        # Optionally, tweak the output for your style
        if desc.startswith("At "):
            desc = desc.replace("At ", "Every day at ")
        return desc
    except Exception:
        return cron_str  # fallback to raw string if parsing fails

# Function to add authentication to a URL path
def url_with_auth(path: str, user_pass: str) -> str:
    separator = '&' if '?' in path else '?'
    url = f"{base_url}{path}{separator}auth={user_pass}"
    return url

# Function to remove a offet from an epg programme .e.g +0100 
def amend_epg_offsets(epg_text):
    return re.sub(r' [+-]\d{4}"', '"', epg_text)

# Function to merge historical and new EPG XML files, as per retention policy
def merge_epg_xmls(xml_list, retention_days):
    # xml_list[0] = retained (old), xml_list[1] = new
    now = datetime.now()
    cutoff = now - timedelta(days=retention_days)
    merged_root = etree.Element("tv")
    event_ids = set()

    # Parse new XML to get valid channel IDs and unique channel elements
    new_tree = etree.fromstring(xml_list[1].encode("utf-8"))
    valid_channel_ids = set()
    channel_elements_by_id = {}
    for elem in new_tree:
        if elem.tag == "channel":
            channel_id = elem.get("id")
            if channel_id:
                valid_channel_ids.add(channel_id)
                channel_elements_by_id[channel_id] = elem  # Only keep the latest

    # Add unique <channel> elements from the new EPG to merged_root
    for elem in channel_elements_by_id.values():
        merged_root.append(elem)

    # Merge <programme> elements from both XMLs, but only for valid channels and no duplicates
    for xml in xml_list:
        tree = etree.fromstring(xml.encode("utf-8"))
        for elem in tree:
            if elem.tag == "programme":
                channel = elem.get("channel")
                if channel not in valid_channel_ids:
                    continue  # Skip programmes for channels not in the new EPG
                start_str = elem.get("start")
                eid = (start_str, elem.get("stop"), channel)
                try:
                    start_dt = datetime.strptime(start_str[:14], "%Y%m%d%H%M%S")
                except Exception:
                    continue
                if start_dt < cutoff:
                    continue
                if eid in event_ids:
                    continue
                event_ids.add(eid)
                merged_root.append(elem)
    return etree.tostring(merged_root, encoding="utf-8", xml_declaration=True).decode("utf-8")

# Function to read XML files and find earliest and latest programme dates
def get_epg_programme_date_range(epg_path):
    if not os.path.exists(epg_path):
        return ("N/A", "N/A")
    try:
        tree = etree.parse(epg_path)
        root = tree.getroot()
        dates = []
        for elem in root.iter("programme"):
            start_str = elem.get("start")
            try:
                dt = datetime.strptime(start_str[:14], "%Y%m%d%H%M%S")
                dates.append(dt)
            except Exception:
                continue
        if not dates:
            return ("N/A", "N/A")
        epg_earliest = min(dates).strftime("%Y-%m-%d %H:%M")
        epg_latest = max(dates).strftime("%Y-%m-%d %H:%M")
        return (epg_earliest, epg_latest)
    except Exception as e:
        logging.error(f"Failed to parse EPG for date range: {e}")
        return ("N/A", "N/A")

# Function to add group titles to a channel
def inject_group_titles(m3u_text, group_name):
    lines = m3u_text.splitlines()
    updated_lines = []
    for line in lines:
        if line.startswith("#EXTINF"):
            if 'group-title' not in line:
                line = line.replace(",", f' group-title="{group_name}",', 1)
        updated_lines.append(line)
    return '\n'.join(updated_lines) + "\n"

# Consolidated function to inject authentication into URLs in both stream and tvg-logo lines
def inject_auth(m3u_text, user_pass, mode="stream"):
    """
    Injects authentication into URLs in the M3U text.
    mode: "stream" for stream URLs, "tvg-logo" for tvg-logo URLs, "both" for both.
    """
    def inject_url_auth(url, auth_token):
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "auth" not in query:
            query["auth"] = [auth_token]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        ))

    lines = m3u_text.splitlines()
    updated_lines = []

    for line in lines:
        # Inject auth into stream URLs
        if mode in ("stream", "both") and line.startswith("http"):
            updated_lines.append(inject_url_auth(line, user_pass))
        # Inject auth into tvg-logo attributes if enabled
        elif mode in ("tvg-logo", "both") and 'tvg-logo="' in line:
            def logo_repl(match):
                url = match.group(1)
                if TVH_APPEND_ICON_AUTH:
                    return f'tvg-logo="{inject_url_auth(url, user_pass)}"'
                else:
                    return f'tvg-logo="{url}"'
            pattern = r'tvg-logo="([^"]+)"'
            updated_lines.append(re.sub(pattern, logo_repl, line))
        else:
            updated_lines.append(line)
    return '\n'.join(updated_lines) + "\n"

# Function to remove a offet from an epg programme .e.g +0100 
def remove_profile_param(url):
    """
    Removes the 'profile' query parameter from a URL.
    """
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    query.pop("profile", None)
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))

# --- 4. TVHeadend API Functions ---
# Function to fetch tags from TVHeadend

# Function to wrap around other function and apply retries with delay
# This is useful for network requests that may fail intermittently    
def fetch_with_retries(request_func, *args, retries=3, delay=30, desc=""):
    attempt = 0
    while attempt < retries:
        try:
            return request_func(*args)
        except Exception as e:
            attempt += 1
            if attempt < retries:
                logging.warning(f"{desc} failed (attempt {attempt}/{retries}): {e}. Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logging.error(f"{desc} failed after {retries} attempts: {e}")
                raise

def fetch_tags(user_pass):
    def do_request():
        tags_url = url_with_auth("/playlist/tags", user_pass)
        resp = requests.get(tags_url)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        tags = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("#EXTINF"):
                name = line.split(',', 1)[1].strip()
                i += 1
                if i < len(lines):
                    url = lines[i].strip()
                    match = re.search(r'/tagid/(\d+)', url)
                    if match:
                        tag_id = match.group(1)
                        tags.append({"name": name, "tag_id": tag_id})
            i += 1
        return tags
    return fetch_with_retries(do_request, retries=3, delay=30, desc="fetch_tags")

# Function to fetch channels for a specific tag from TVHeadend
def fetch_channels_for_tag(tag_id, user_pass):
    def do_request():
        path = f"/playlist/tagid/{tag_id}"
        full_url = url_with_auth(path, user_pass)
        resp = requests.get(full_url)
        resp.raise_for_status()
        return resp.text
    return fetch_with_retries(do_request, retries=3, delay=30, desc=f"fetch_channels_for_tag {tag_id}")

# --- 5. Playlist Handling ---
# Function to amend the M3U playlist with authentication and group titles
def parse_m3u_channels(m3u_text):
    channels = []
    lines = m3u_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            group_title = re.search(r'group-title="([^"]+)"', line)
            tvg_id = re.search(r'tvg-id="([^"]+)"', line)
            tvg_logo = re.search(r'tvg-logo="([^"]+)"', line)
            channel_number = re.search(r'tvg-chno="([^"]+)"', line)
            channel_name = line.split(",", 1)[-1].strip()
            stream_url = lines[i+1] if i+1 < len(lines) else ""
            channelid_match = re.search(r'/channelid/(\d+)', stream_url)
            channelid = channelid_match.group(1) if channelid_match else ""
            channels.append({
                "group_title": group_title.group(1) if group_title else "",
                "channel_name": channel_name,
                "channel_number": channel_number.group(1) if channel_number else "",
                "tvg_id": tvg_id.group(1) if tvg_id else "",
                "tvg_logo": tvg_logo.group(1) if tvg_logo else "",
                "channelid": channelid,
                "stream_url": stream_url,
            })
            i += 2
        else:
            i += 1
    return channels

# Function to create M3U playlist friendly for IPTV players
def build_and_cache_playlist_once(send_refresh=True):
    global cached_playlist, last_refresh_time, last_playlist_update, cached_channels
    logging.info("Playlist build process started")
    current_time = time.time()
    combined_playlist = "#EXTM3U\n"
    for user in USERS:
        user_pass = user["pass"]
        try:
            tags = fetch_tags(user_pass)
        except Exception as e:
            combined_playlist += f"# Failed to fetch tags for user {user['user']}: {e}\n"
            continue
        if tags is None:
            tags = []
        for tag in tags:
            tag_id = tag["tag_id"]
            tag_name = tag["name"]
            try:
                m3u_text = fetch_channels_for_tag(tag_id, user_pass)
                m3u_with_injections = inject_group_titles(m3u_text, tag_name)
                # Remove 'profile' from all stream URLs before injecting auth
                lines = m3u_with_injections.splitlines()
                cleaned_lines = []
                for line in lines:
                    if line.startswith("http"):
                        line = remove_profile_param(line)
                    cleaned_lines.append(line)
                m3u_cleaned = "\n".join(cleaned_lines)
                mode = "both" if TVH_APPEND_ICON_AUTH else "stream"
                m3u_with_auth = inject_auth(m3u_cleaned, user_pass, mode=mode)
                combined_playlist += m3u_with_auth
            except Exception as e:
                combined_playlist += f"# Failed tag {tag_id} for user {user['user']}: {e}\n"
    if TVH_URL_AUTH:
        # Remove 'profile' param again in case any remain, then inject global auth
        lines = combined_playlist.splitlines()
        cleaned_lines = []
        for line in lines:
            if line.startswith("http"):
                line = remove_profile_param(line)
            cleaned_lines.append(line)
        combined_playlist = "\n".join(cleaned_lines)
        combined_playlist = inject_auth(combined_playlist, TVH_URL_AUTH, "stream")
    cached_playlist = combined_playlist
    cached_channels = parse_m3u_channels(combined_playlist)
    last_refresh_time = current_time
    last_playlist_update = int(time.time())
    logging.info(f"Playlist built with {len(cached_channels)} channels.")
    # --- Save playlist to disk ---
    playlist_path = os.path.join(ARCHIVE_DIR, "playlist.m3u")
    try:
        with open(playlist_path, "w", encoding="utf-8") as f:
            f.write(combined_playlist)
    except Exception as e:
        logging.error(f"Failed to save playlist.m3u: {e}")
    # -------------------------------------------------

    if send_refresh:
        socketio.emit('refresh')
    render_and_cache_index()
    logging.info("Playlist build process finished")

def manual_refresh_playlist():
    build_and_cache_playlist_once()

def load_cached_playlist():
    global cached_playlist, cached_channels
    playlist_path = os.path.join(ARCHIVE_DIR, "playlist.m3u")
    try:
        with open(playlist_path, "r", encoding="utf-8") as f:
            cached_playlist = f.read()
    except Exception:
        cached_playlist = None

    # Parse channels from the loaded playlist
    if cached_playlist:
        cached_channels = parse_m3u_channels(cached_playlist)
    else:
        cached_channels = []

# Call the function at startup
load_cached_playlist()

# --- 6. EPG Handling ---
#Function to handle EPG retention and merging
def fetch_epg_xml():
    epg_url = f"http://{TVH_HOST}:{TVH_PORT}/xmltv/channels?auth={TVH_URL_AUTH}"
    logging.info(f"Fetching EPG from: {epg_url}")
    def do_request():
        resp = requests.get(epg_url)
        resp.raise_for_status()
        new_xml = resp.text
        if EPG_STRIP_OFFSET:
            new_xml = amend_epg_offsets(new_xml)
        return resp.text
    return fetch_with_retries(do_request, retries=3, delay=30, desc="fetch_epg_xml")

def merge_epg(send_refresh=True):
    logging.info("EPG merge process started")
    retained_path = os.path.join(ARCHIVE_DIR, "epg_retained.xml")
    logging.info(f"EPG retained path: {retained_path}")

    # Fetch new EPG XML
    new_xml = fetch_epg_xml()
    if EPG_STRIP_OFFSET:
        new_xml = amend_epg_offsets(new_xml)

    if os.path.exists(retained_path):
        with open(retained_path, "r", encoding="utf-8") as f:
            retained_xml = f.read()
        if EPG_STRIP_OFFSET:
            retained_xml = amend_epg_offsets(retained_xml)
    else:
        retained_xml = "<tv></tv>"

    merged_xml = merge_epg_xmls([retained_xml, new_xml], retention_days=EPG_RETENTION_DAYS)

    logging.info("Writing merged EPG to retained file...")
    with open(retained_path, "w", encoding="utf-8") as f:
        f.write(merged_xml)
    logging.info("Write complete.")
    if send_refresh:
        socketio.emit('refresh')
    logging.info("EPG merge process finished")
    render_and_cache_index()

    # --- Validate EPG data for duplicates and old programmes ---
    logging.info("Validating EPG data for duplicates and old programmes...")
    epg_path = retained_path
    retention_days = EPG_RETENTION_DAYS  # configured value

    tree = etree.parse(epg_path)
    root = tree.getroot()

    now = datetime.now()
    cutoff = now - timedelta(days=retention_days)

    event_ids = set()
    duplicates = 0
    old_programmes = 0
    channels = set()
    for elem in root.iter("channel"):
        channels.add(elem.get("id"))

    for elem in root.iter("programme"):
        start_str = elem.get("start")
        stop_str = elem.get("stop")
        channel = elem.get("channel")
        eid = (start_str, stop_str, channel)
        try:
            start_dt = datetime.strptime(start_str[:14], "%Y%m%d%H%M%S")
        except Exception:
            continue
        if eid in event_ids:
            duplicates += 1
        else:
            event_ids.add(eid)
        if start_dt < cutoff:
            old_programmes += 1
        if channel not in channels:
            logging.warning(f"Orphaned programme: {eid}")

    logging.info(f"EPG Validation complete. Total programmes: {len(event_ids)}, Duplicate programmes: {duplicates}, Old programmes: {old_programmes}")

# --- 7. Web Page Rendering ---
# Function to render the index page and cache the HTML
def render_and_cache_index():
    with app.app_context():
        with app.test_request_context('/'):
            def format_time(ts):
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "N/A"

            container_start_str = datetime.fromtimestamp(SERVER_START_TIME).strftime("%Y-%m-%d %H:%M:%S")
            playlist_path = os.path.join(ARCHIVE_DIR, "playlist.m3u")
            if os.path.exists(playlist_path):
                last_update_str = format_time(os.path.getmtime(playlist_path))
            else:
                last_update_str = "Never"            
            next_playlist_update_str = "N/A"
            next_epg_update_str = "N/A"
            # Use the global scheduler if available
            sched = globals().get("scheduler", None)
            if sched:
                playlist_job = sched.get_job('playlist_refresh')
                if playlist_job and playlist_job.next_run_time:
                    next_playlist_update_str = playlist_job.next_run_time.strftime("%Y-%m-%d %H:%M")
                if EPG_RETENTION_ENABLED:
                    epg_job = sched.get_job('epg_refresh')
                    if epg_job and epg_job.next_run_time:
                        next_epg_update_str = epg_job.next_run_time.strftime("%Y-%m-%d %H:%M")

            if EPG_RETENTION_ENABLED:
                epg_retained_path = os.path.join(ARCHIVE_DIR, "epg_retained.xml")
                if os.path.exists(epg_retained_path):
                    epg_last_update = format_time(os.path.getmtime(epg_retained_path))
                    epg_earliest, epg_latest = get_epg_programme_date_range(epg_retained_path)
                else:
                    epg_last_update = "Never"
                    epg_earliest, epg_latest = "N/A", "N/A"
                epg_interval_str = EPG_REFRESH_SCHEDULE
            else:
                epg_last_update = "N/A"
                epg_interval_str = "N/A"
                epg_earliest, epg_latest = "N/A", "N/A"

            channel_rows = ""
            for ch in cached_channels:
                logo_html = ""
                if ch["tvg_logo"]:
                    logo_html = f'<img src="{ch["tvg_logo"]}" alt="logo" style="height:32px;">'
                copy_html = f'''
                <td style="text-align:center;">
                    <button onclick="copyToClipboard('{ch["stream_url"]}')">Copy Link</button>
                </td>
                '''
                channel_rows += f"""
                <tr>
                    <td>{ch["group_title"]}</td>
                    <td>{ch["channel_name"]}</td>
                    <td>{ch["channel_number"]}</td>
                    <td>{ch["tvg_id"]}</td>
                    <td>{ch.get("channelid", "")}</td>
                    <td class="centered">{logo_html}</td>
                    {copy_html}
                </tr>
                """
            user_list_str = ", ".join([u["user"] for u in USERS])

            return render_template(
                "index.html",
                container_start_str=container_start_str,
                TVH_HOST=TVH_HOST,
                TVH_PORT=TVH_PORT,
                user_list_str=user_list_str,
                last_update_str=last_update_str,
                next_playlist_update_str=next_playlist_update_str,
                channel_rows=channel_rows,
                EPG_RETENTION_ENABLED=EPG_RETENTION_ENABLED,
                EPG_RETENTION_DAYS=EPG_RETENTION_DAYS,
                REFRESH_SCHEDULE_HUMAN = humanify_cron(REFRESH_SCHEDULE),
                EPG_REFRESH_SCHEDULE_HUMAN = humanify_cron(EPG_REFRESH_SCHEDULE),
                epg_last_update=epg_last_update,
                epg_earliest=epg_earliest,
                epg_latest=epg_latest,
                next_epg_update_str=next_epg_update_str
            )

# --- 8. Flask Routes ---
# Flask route for the index page
@app.route("/")
def index():
   return render_and_cache_index()

# Flask route for the M3U playlist
@app.route("/playlist.m3u")
def playlist():
    if not cached_playlist:
        return Response(status=204)  # No Content
    return Response(cached_playlist, mimetype="application/x-mpegurl")

# Flask route for the EPG XML
@app.route("/epg.xml")
def epg():
    try:
        if not TVH_URL_AUTH:
            raise Exception("TVH_URL_AUTH is not set.")
        if EPG_RETENTION_ENABLED:
            retained_path = os.path.join(ARCHIVE_DIR, "epg_retained.xml")
            if os.path.exists(retained_path):
                with open(retained_path, "r", encoding="utf-8") as f:
                    merged_epg = f.read()
                return Response(merged_epg, mimetype="application/xml")
            else:
                return Response(status=204)  # No Content
        else:
            epg_url = f"http://{TVH_HOST}:{TVH_PORT}/xmltv/channels?auth={TVH_URL_AUTH}"
            resp = requests.get(epg_url)
            resp.raise_for_status()
            epg_text = resp.text
            if EPG_STRIP_OFFSET:
                epg_text = amend_epg_offsets(epg_text)
            return Response(epg_text, mimetype="application/xml")
    except Exception as e:
        logging.error(f"Failed to fetch EPG XML: {e}")
        return Response(status=204)  # No Content

# Flask route for page refresh
@app.route("/manualplaylistrefresh")
def refresh():
    manual_refresh_playlist()
    return redirect(url_for('index'))

# Flask route for manual EPG refresh
@app.route("/manualepgrefresh")
def manual_epg_refresh():
    merge_epg()
    return redirect(url_for('index'))

# Flask route for server status
@app.route("/server_status")
def server_status():
    return {"start_time": SERVER_START_TIME}

# --- 9. Startup Logic ---
if __name__ == "__main__":
    logging.info("Starting TVHeadend Playlist Server")
    logging.info(f"TVH_HOST: {TVH_HOST}")
    logging.info(f"TVH_PORT: {TVH_PORT}")
    logging.info(f"TVH_USERS: {TVH_USERS}")
    logging.info(f"TVH_URL_AUTH: {TVH_URL_AUTH}")
    logging.info(f"REFRESH_SCHEDULE: {REFRESH_SCHEDULE}")
    scheduler = BackgroundScheduler()
    # Playlist job
    playlist_trigger = CronTrigger.from_crontab(REFRESH_SCHEDULE)
    scheduler.add_job(build_and_cache_playlist_once, playlist_trigger, id='playlist_refresh')
    if EPG_RETENTION_ENABLED:
        logging.info(f"EPG_RETENTION_ENABLED: {EPG_RETENTION_ENABLED}")
        logging.info(f"EPG_RETENTION_DAYS: {EPG_RETENTION_DAYS}")
        logging.info(f"EPG_RETENTION_SIZE_MB: {EPG_RETENTION_SIZE_MB}")
        logging.info(f"EPG_REFRESH_SCHEDULE: {EPG_REFRESH_SCHEDULE}")
        epg_cron = EPG_REFRESH_SCHEDULE
        epg_trigger = CronTrigger.from_crontab(epg_cron)
        scheduler.add_job(merge_epg, epg_trigger, id='epg_refresh')
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown())
    initial_refresh_check()
    logging.info("Initial refresh check completed.")
    logging.info("Starting Flask server...")
    socketio.run(app, host="0.0.0.0", port=9985, debug=False, use_reloader=False)

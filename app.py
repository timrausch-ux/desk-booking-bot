import os
import psycopg2
import re
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from apscheduler.schedulers.background import BackgroundScheduler

# --- CONFIGURATION ---
# 1. PASTE YOUR CHANNEL ID HERE (Right-click channel -> Copy Link -> Ends in C0xxxx)
CHANNEL_ID = "C07GV929YRF" 

ROOMS_DISPLAY = ["Small 1", "Small 2", "Large 1", "Large 2", "Large 3", "Large 4"]
ROOMS_DB = ["Small Room 1", "Small Room 2", "Large Room 1", "Large Room 2", "Large Room 3", "Large Room 4"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# --- APP SETUP ---
app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# --- MEMORY CACHE ---
USER_CACHE = {}

# --- DATABASE CONNECTION ---
def get_db_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])

# --- DATE LOGIC (NEW) ---
def get_display_dates():
    """
    Calculates the dates for the dashboard.
    - If today is Mon-Thu: Returns dates for THIS week.
    - If today is Fri-Sun: Returns dates for NEXT week.
    Returns a list of strings: ["Mon (Oct 9)", "Tue (Oct 10)", ...]
    """
    today = datetime.now()
    current_weekday = today.weekday() # Mon=0, Sun=6
    
    # Calculate "Reference Monday"
    # If Friday (4) or later, jump to next week's Monday. 
    # Otherwise, go back to this week's Monday.
    if current_weekday >= 4:
        days_ahead = 7 - current_weekday
    else:
        days_ahead = 0 - current_weekday
        
    next_monday = today + timedelta(days=days_ahead)
    
    date_labels = []
    for i in range(5):
        future_day = next_monday + timedelta(days=i)
        # Format: "Monday (Oct 9)"
        label = f"{DAYS[i]} ({future_day.strftime('%b %d')})"
        date_labels.append(label)
        
    return date_labels

# --- DATABASE LOGIC ---
def get_weekly_bookings():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT day, room, user_id FROM bookings")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    data = {day: {room: None for room in ROOMS_DB} for day in DAYS}
    for row in rows:
        day, room, user = row
        if day in data and room in data[day]:
            data[day][room] = user
    return data

def toggle_booking(day, room_index, user_id):
    room_name = ROOMS_DB[room_index]
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT user_id FROM bookings WHERE day = %s AND room = %s", (day, room_name))
    row = cur.fetchone()
    current_owner = row[0] if row else None
    
    result = "error"
    if current_owner is None:
        cur.execute("""
            INSERT INTO bookings (day, room, user_id) VALUES (%s, %s, %s)
            ON CONFLICT (day, room) DO UPDATE SET user_id = EXCLUDED.user_id;
        """, (day, room_name, user_id))
        result = "booked"
    elif current_owner == user_id:
        cur.execute("DELETE FROM bookings WHERE day = %s AND room = %s;", (day, room_name))
        result = "unbooked"
    else:
        result = "taken"

    conn.commit()
    cur.close()
    conn.close()
    return result

def reset_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM bookings;") 
    conn.commit()
    cur.close()
    conn.close()

# --- HELPER: GET NAMES ---
def get_user_name(user_id):
    if user_id in USER_CACHE:
        return USER_CACHE[user_id]
    try:
        result = app.client.users_info(user=user_id)
        name = result["user"]["profile"].get("first_name") or result["user"]["real_name"]
        USER_CACHE[user_id] = name
        return name
    except Exception as e:
        print(f"Error fetching name: {e}")
        return "Taken"

# --- UI BUILDER ---
def get_dashboard_blocks():
    all_bookings = get_weekly_bookings()
    
    # Get the smart dates
    date_labels = get_display_dates()
    
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🗓️ Weekly Desk Dashboard"}},
        {"type": "divider"}
    ]
    
    for i, day in enumerate(DAYS):
        # Use the date label (e.g., "Monday (Oct 9)")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{date_labels[i]}*"}})
        buttons = []
        for j, room_full_name in enumerate(ROOMS_DB):
            user_id = all_bookings[day][room_full_name]
            style = "primary"
            label = ROOMS_DISPLAY[j]
            btn_text = label
            
            if user_id:
                style = "danger"
                first_name = get_user_name(user_id)
                btn_text = f"{label} ({first_name})"

            val = f"{day}|{j}"
            unique_action_id = f"toggle_{day}_{j}"
            
            buttons.append({
                "type": "button",
                "text": {"type": "plain_text", "text": btn_text},
                "style": style,
                "value": val,
                "action_id": unique_action_id
            })
        blocks.append({"type": "actions", "elements": buttons})
        blocks.append({"type": "divider"})
        
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Click Green to Book. Click Red to Cancel."}]
    })
    return blocks

# --- AUTOMATION (SCHEDULER) ---
def scheduled_reset_and_post():
    """Runs every Friday to wipe DB and post new week"""
    print("⏰ Auto-Reset Triggered!")
    try:
        # 1. Wipe DB
        reset_db()
        # 2. Post New Message
        app.client.chat_postMessage(
            channel=CHANNEL_ID,
            text="<!here> Desk Booking is Open for Next Week!", # <!here> notifies everyone
            blocks=get_dashboard_blocks()
        )
        print("✅ New week posted successfully.")
    except Exception as e:
        print(f"❌ Scheduler Error: {e}")

# --- SLACK HANDLERS ---
@app.command("/desk")
def open_dashboard(ack, say, command):
    ack()
    user_text = command.get('text', '').lower().strip()
    
    if user_text == "new":
        reset_db()
        say("🗑️ *Database Wiped manually!* Starting a fresh week.")
        
    say(blocks=get_dashboard_blocks(), text="Weekly Desk Dashboard")

@app.action(re.compile("toggle_.*"))
def handle_click(ack, body, client):
    ack()
    user = body['user']['id']
    day, room_idx_str = body['actions'][0]['value'].split("|")
    room_idx = int(room_idx_str)
    
    status = toggle_booking(day, room_idx, user)
    
    if status == "taken":
        client.chat_postEphemeral(
            channel=body['channel']['id'], user=user,
            text=f"❌ That desk is already booked by someone else."
        )
    else:
        client.chat_update(
            channel=body['channel']['id'],
            ts=body['message']['ts'],
            blocks=get_dashboard_blocks(),
            text="Dashboard Updated"
        )

# --- SERVER START ---
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

@flask_app.route("/")
def health():
    return "Dashboard Active", 200

# INIT
if __name__ != "__main__":
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                day TEXT NOT NULL,
                room TEXT NOT NULL,
                user_id TEXT,
                PRIMARY KEY (day, room)
            );
        """)
        conn.commit()
        conn.close()
        
        # START SCHEDULER
        # day_of_week='fri', hour=14 means Friday at 2:00 PM (Server Time - usually UTC)
        scheduler = BackgroundScheduler()
        scheduler.add_job(scheduled_reset_and_post, 'cron', day_of_week='fri', hour=14, minute=0)
        scheduler.start()
        print("⏳ Scheduler Active: Will reset every Friday at 14:00 UTC")
        
    except Exception as e:
        print(f"Init Error: {e}")

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

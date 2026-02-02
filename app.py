import os
import psycopg2
import re  # <--- NEW: Needed for the pattern matching
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request

# --- CONFIGURATION ---
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

# --- DATABASE CONNECTION ---
def get_db_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])

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

# --- UI BUILDER ---
def get_dashboard_blocks():
    all_bookings = get_weekly_bookings()
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🗓️ Weekly Desk Dashboard"}},
        {"type": "divider"}
    ]
    
    for day in DAYS:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{day}*"}})
        buttons = []
        for i, room_full_name in enumerate(ROOMS_DB):
            user = all_bookings[day][room_full_name]
            style = "danger" if user else "primary"
            label = ROOMS_DISPLAY[i]
            btn_text = f"{label} (X)" if user else label
            val = f"{day}|{i}"
            
            # UNIQUE ID FIX: We append the day and index to make every button unique
            # e.g., toggle_Monday_0, toggle_Monday_1, etc.
            unique_action_id = f"toggle_{day}_{i}"
            
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

# --- SLACK HANDLERS ---
@app.command("/desk")
def open_dashboard(ack, say):
    ack()
    say(blocks=get_dashboard_blocks(), text="Weekly Desk Dashboard")

# REGEX LISTENER: This listens for ANY action_id that starts with "toggle_"
@app.action(re.compile("toggle_.*"))
def handle_click(ack, body, client):
    ack()
    user = body['user']['id']
    # We still get the data we need from the 'value' field (Monday|0)
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

# Self-Healing DB Init
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
    except Exception as e:
        print(f"DB Error: {e}")

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

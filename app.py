import os
import psycopg2
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request

# --- CONFIGURATION ---
ROOMS = [
    "Small Room 1", "Small Room 2", 
    "Large Room 1", "Large Room 2", "Large Room 3", "Large Room 4"
]
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

def init_db():
    """Creates the table automatically if it doesn't exist"""
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
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"DB Error: {e}")

# Run DB init on startup
init_db()

# --- DATABASE LOGIC ---
def get_bookings_for_day(day):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT room, user_id FROM bookings WHERE day = %s", (day,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    booked_map = {row[0]: row[1] for row in rows}
    
    # Return dictionary ensuring all rooms are present
    result = {}
    for room in ROOMS:
        result[room] = booked_map.get(room, None)
    return result

def toggle_booking(day, room, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check current status
    cur.execute("SELECT user_id FROM bookings WHERE day = %s AND room = %s", (day, room))
    row = cur.fetchone()
    current_owner = row[0] if row else None
    
    status = ""
    
    if current_owner is None:
        # Book it
        cur.execute("""
            INSERT INTO bookings (day, room, user_id) VALUES (%s, %s, %s)
            ON CONFLICT (day, room) DO UPDATE SET user_id = EXCLUDED.user_id;
        """, (day, room, user_id))
        status = "booked"
    elif current_owner == user_id:
        # Unbook it
        cur.execute("UPDATE bookings SET user_id = NULL WHERE day = %s AND room = %s;", (day, room))
        status = "unbooked"
    else:
        status = "taken"

    conn.commit()
    cur.close()
    conn.close()
    return status

# --- UI BUILDER ---
def get_ui(day):
    # Dropdown for days
    day_options = [{"text": {"type": "plain_text", "text": d}, "value": d} for d in DAYS]
    
    # Buttons for rooms
    bookings = get_bookings_for_day(day)
    buttons = []
    for room in ROOMS:
        user = bookings[room]
        # Green if free, Red if taken
        style = "danger" if user else "primary"
        text_status = "Taken" if user else "Free"
        val = f"{day}|{room}"
        
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"{room} ({text_status})"},
            "style": style,
            "value": val,
            "action_id": "toggle_room"
        })

    return [
        {"type": "header", "text": {"type": "plain_text", "text": "🏢 Weekly Desk Booking"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Select a Day:*"},
            "accessory": {
                "type": "static_select",
                "placeholder": {"type": "plain_text", "text": "Select a day"},
                "options": day_options,
                "initial_option": {"text": {"type": "plain_text", "text": day}, "value": day},
                "action_id": "select_day"
            }
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Availability for {day}:*"}},
        {"type": "actions", "elements": buttons}
    ]

# --- SLACK HANDLERS ---
@app.command("/desk")
def start(ack, say):
    ack()
    say(blocks=get_ui("Monday"), text="Desk Booking")

@app.action("select_day")
def change_day(ack, body, client):
    ack()
    new_day = body['actions'][0]['selected_option']['value']
    client.chat_update(
        channel=body['channel']['id'], ts=body['message']['ts'],
        blocks=get_ui(new_day), text=f"Showing {new_day}"
    )

@app.action("toggle_room")
def click_room(ack, body, client):
    ack()
    user = body['user']['id']
    day, room = body['actions'][0]['value'].split("|")
    
    result = toggle_booking(day, room, user)
    
    if result == "taken":
        client.chat_postEphemeral(
            channel=body['channel']['id'], user=user,
            text=f"❌ {room} is already taken by someone else."
        )
    else:
        client.chat_update(
            channel=body['channel']['id'], ts=body['message']['ts'],
            blocks=get_ui(day), text="Updated"
        )

# --- START SERVER ---
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

@flask_app.route("/")
def health():
    return "OK", 200

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

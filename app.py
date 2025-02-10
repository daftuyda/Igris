import os
import math
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

########################################
# APP CONFIG
########################################
app = Flask(__name__)
app.secret_key = "YOUR_SECRET_KEY"  # Replace with a secure, random key

# Database config
app.config["SECRET_KEY"] = "some-secret"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

# Database config
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///database.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

########################################
# MODELS
########################################

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    # Per-user timezone
    timezone = db.Column(db.String(50), default="UTC")
    # The last local date (YYYY-MM-DD) we did the daily evaluation for them
    last_reset_date = db.Column(db.String(10), default="")

    xp = db.Column(db.Integer, default=0)      # total XP
    level = db.Column(db.Integer, default=1)   # user's level
    
    streak = db.Column(db.Integer, default=0)  # consecutive days of completing all tasks

    tasks = db.relationship("Task", backref="user", lazy=True)

class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    task_type = db.Column(db.String(20), default="count")
    days_of_week = db.Column(db.String(50), default="0,1,2,3,4,5,6")
    difficulty = db.Column(db.Integer, default=3)
    goal = db.Column(db.Integer, default=1)
    count = db.Column(db.Integer, default=0)
    is_done = db.Column(db.Boolean, default=False)

    # ADD THIS
    is_one_time = db.Column(db.Boolean, default=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    def get_days_set(self):
        """Return set of weekdays (Mon=0 ... Sun=6) the task is active."""
        if not self.days_of_week:
            return set()
        return set(int(d.strip()) for d in self.days_of_week.split(",") if d.strip().isdigit())

with app.app_context():
    db.create_all()

########################################
# LEVELING & RANKING SYSTEM
########################################

def xp_needed_for_level(level):
    """
    Let's define that level=1 => 0 XP needed.
    Then from level=2 onward, XP needed = 50 * (level-1)^2.
    Adjust as you like.
    """
    if level <= 1:
        return 0
    return 50 * ((level - 1) ** 2)

def get_rank_for_level(level):
    """
    Example Solo Leveling–style rank by level:
     1-5   => E
     6-10  => D
     11-15 => C
     16-20 => B
     21-30 => A
     31+   => S
    """
    if level <= 5:
        return "E"
    elif level <= 10:
        return "D"
    elif level <= 15:
        return "C"
    elif level <= 20:
        return "B"
    elif level <= 30:
        return "A"
    else:
        return "S"

def update_user_level(user):
    """Recalculate user's level from their XP."""
    current_xp = user.xp
    level = 1
    while True:
        needed_for_next = xp_needed_for_level(level + 1)
        if current_xp >= needed_for_next:
            level += 1
        else:
            break

    user.level = level
    db.session.commit()

########################################
# DAILY EVALUATION LOGIC
########################################

INCOMPLETE_TASK_PENALTY = 5  # XP penalty per difficulty if not done
# Or define a separate bonus if you want for "all tasks done"

class XpLog(db.Model):
    __tablename__ = "xp_log"
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    amount = db.Column(db.Integer, nullable=False)  # e.g. +10, -5, etc.
    reason = db.Column(db.String(255), nullable=False)  # e.g. "Completed Task: Push-ups"
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship to user if you want
    user = db.relationship("User", backref="xp_logs")

def is_task_completed(task):
    """Check if a task meets completion criteria."""
    if task.task_type == "count":
        return task.count >= task.goal
    elif task.task_type == "boolean":
        return task.is_done
    return False

def do_daily_evaluation_for_user(user):
    """
    Check the user's local day_of_week, award XP for tasks that are done,
    penalize tasks that aren't. Then reset those tasks.
    """
    # 1. Convert now to user's timezone
    try:
        tz = pytz.timezone(user.timezone)
    except:
        tz = pytz.timezone("UTC")

    local_now = datetime.now(tz)
    day_of_week = local_now.weekday()  # Monday=0, Tuesday=1, ... Sunday=6

    # 2. Filter tasks for 'today'
    todays_tasks = [t for t in user.tasks if day_of_week in t.get_days_set()]

    # 3. Award or penalize
    for t in todays_tasks:
        if is_task_completed(t):
            xp_gain = t.difficulty * t.goal if t.task_type=='count' else t.difficulty * 10
            user.xp += xp_gain

            db.session.add(XpLog(
                user_id=user.id,
                amount=xp_gain,
                reason=f"Task Completed: {t.name}"  # store the task name
            ))
        else:
            penalty = t.difficulty * 5
            user.xp = max(0, user.xp - penalty)
            db.session.add(XpLog(
                user_id=user.id,
                amount=-penalty,
                reason=f"Incomplete Task: {t.name}"
            ))
            
    all_completed = all(is_task_completed(t) for t in todays_tasks)
    if all_completed:
        user.streak += 1
        bonus_xp = 20
        user.xp += bonus_xp
        db.session.add(XpLog(
            user_id=user.id,
            amount=bonus_xp,
            reason="Daily Bonus for Completing All Tasks"
        ))
    else:
        user.streak = 0

    update_user_level(user)

    # 4. Reset tasks
    for t in todays_tasks:
        if t.is_one_time:
            db.session.delete(t)
        else:
            # normal reset
            t.count = 0
            t.is_done = False

    db.session.commit()
    print(f"[Daily Eval] Completed for user={user.username}, day_of_week={day_of_week}")

def daily_check_for_all_users():
    """
    Run every X minutes. 
    For each user:
     - Convert now (UTC) to user's local time
     - If local_date != user.last_reset_date => do_daily_evaluation_for_user(user)
     - Update user.last_reset_date
    """
    with app.app_context():
        now_utc = datetime.now(timezone.utc)
        all_users = User.query.all()

        for user in all_users:
            # user might have an invalid timezone => fallback
            try:
                tz = pytz.timezone(user.timezone)
            except:
                tz = pytz.timezone("UTC")

            local_now = now_utc.astimezone(tz)
            local_date_str = local_now.strftime("%Y-%m-%d")

            # If user's local date changed => new day => do daily eval
            if user.last_reset_date != local_date_str:
                do_daily_evaluation_for_user(user)
                user.last_reset_date = local_date_str

        db.session.commit()

########################################
# SCHEDULER
########################################

scheduler = BackgroundScheduler()
# We'll run the check every 15 minutes
scheduler.add_job(daily_check_for_all_users, "interval", minutes=15)
scheduler.start()

########################################
# HELPER / AUTH
########################################

def get_current_user():
    if "user_id" in session:
        user_id = session["user_id"]
        user = db.session.get(User, user_id)
        return user
    return None

@app.context_processor
def utility_processor():
    """
    Allows templates to call get_rank_for_level(...) directly.
    Example usage in Jinja: {{ get_rank_for_level(user.level) }}
    """
    return dict(get_rank_for_level=get_rank_for_level)

########################################
# AUTH ROUTES
########################################

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            return "Username is already taken. <a href='/register'>Try again</a>"

        pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")

        new_user = User(username=username, password_hash=pw_hash)
        db.session.add(new_user)
        db.session.commit()

        # Log in automatically
        session["user_id"] = new_user.id
        return redirect(url_for("view_tasks"))

    return render_template("register.html", user=get_current_user())

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            session.permanent = True
            return redirect(url_for("view_tasks"))
        else:
            return "Invalid username or password. <a href='/login'>Try again</a>"

    return render_template("login.html", user=get_current_user())

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("index"))

########################################
# CHANGE PASSWORD ROUTE
########################################

@app.route("/change_password", methods=["GET", "POST"])
def change_password():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        old_pass = request.form["old_password"]
        new_pass = request.form["new_password"]

        # Check old password
        if bcrypt.check_password_hash(user.password_hash, old_pass):
            # Update to new password
            user.password_hash = bcrypt.generate_password_hash(new_pass).decode("utf-8")
            db.session.commit()
            return "Password changed successfully! <a href='/profile'>Back to Profile</a>"
        else:
            return "Old password incorrect. <a href='/change_password'>Try again</a>"

    return render_template("change_password.html", user=user)

########################################
# CORE ROUTES
########################################

@app.route("/")
def index():
    user = get_current_user()
    return render_template("index.html", user=user)

@app.template_filter("local_time")
def local_time_filter(dt, timezone_str):
    import pytz
    if not dt:
        return ""
    try:
        tz = pytz.timezone(timezone_str)
    except:
        tz = pytz.utc
    local_dt = dt.astimezone(tz)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")

@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Show user's XP, Level, Rank, progress to next level, etc."""
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    
    timezones=pytz.common_timezones
    
    try:
        user_tz = pytz.timezone(user.timezone)
    except:
        user_tz = pytz.timezone("UTC")

    local_now = datetime.now(user_tz)
    
    yesterday_local = local_now - timedelta(days=0)
    
    y_midnight_start = yesterday_local.replace(hour=0, minute=0, second=0, microsecond=0)
    
    y_midnight_end = y_midnight_start + timedelta(days=1)
    
    start_utc = y_midnight_start.astimezone(pytz.utc)
    end_utc = y_midnight_end.astimezone(pytz.utc)
    
    xp_logs_yesterday = XpLog.query.filter(
        XpLog.user_id == user.id,
        XpLog.timestamp >= start_utc,
        XpLog.timestamp < end_utc
    ).order_by(XpLog.timestamp).all()
    
    total_gained = sum(e.amount for e in xp_logs_yesterday if e.amount > 0)
    total_lost = sum(-e.amount for e in xp_logs_yesterday if e.amount < 0)
    net_xp = total_gained - total_lost

    user_rank = get_rank_for_level(user.level)
    current_level_xp = xp_needed_for_level(user.level)      # XP needed to get to user's current level
    next_level_xp = xp_needed_for_level(user.level + 1)     # XP needed for the next level

    if request.method == "POST":
        new_tz = request.form.get("new_timezone", "UTC").strip()
        user.timezone = new_tz
        db.session.commit()
        return redirect(url_for("profile"))

    # e.g., progress from "start of this level" to "next level"
    xp_into_level = user.xp - current_level_xp  
    xp_range_for_level = next_level_xp - current_level_xp
    if xp_range_for_level <= 0:
        progress_percent = 100
    else:
        progress_percent = (xp_into_level / xp_range_for_level) * 100
        if progress_percent < 0:
            progress_percent = 0
        elif progress_percent > 100:
            progress_percent = 100

    return render_template("profile.html", user=user, rank=user_rank, 
                           progress_percent=progress_percent,
                           xp_into_level=xp_into_level,
                           xp_range_for_level=xp_range_for_level,
                           timezones=timezones,
                           xp_logs_yesterday=xp_logs_yesterday,)

@app.route("/change_timezone", methods=["POST"])
def change_timezone():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    # Grab the new timezone from the form
    new_tz = request.form.get("new_timezone", "UTC").strip()

    # You might validate it or just trust it for now
    user.timezone = new_tz
    db.session.commit()

    # Redirect back to the profile
    return redirect(url_for("profile"))

@app.route("/tasks")
def view_tasks():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    
    def xp_for_task(t):
        if t.task_type == "count":
            return t.difficulty * t.goal
        elif t.task_type == "boolean":
            return t.difficulty * 10
        else:
            return 

    # Determine local time for the user
    try:
        user_tz = pytz.timezone(user.timezone)
    except:
        user_tz = pytz.timezone("UTC")

    local_now = datetime.now(user_tz)

    # next midnight = take today's date in local time + 1 day, set time to 00:00
    # if it's not already past midnight. Here's an approach:
    tomorrow = local_now + timedelta(days=1)
    next_midnight_local = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)

    # pass tasks logic if you only want today’s tasks, etc.
    tasks_today = [t for t in user.tasks if local_now.weekday() in t.get_days_set()]
    
    for t in tasks_today:
        t.xp_reward = xp_for_task(t)

    return render_template("tasks.html",
                           user=user,
                           tasks=tasks_today,
                           next_midnight_local=next_midnight_local.isoformat())

@app.route("/create_task", methods=["GET", "POST"])
def create_task():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form["name"]
        task_type = request.form.get("task_type", "count")
        goal = int(request.form.get("goal", 1))

        selected_days = request.form.getlist("days_of_week")  # e.g. ["0","2"]
        days_str = ",".join(selected_days)

        difficulty = int(request.form.get("difficulty", 1))
        
        one_time_val = request.form.get("is_one_time", "false")
        is_one_time = (one_time_val == "true")

        new_task = Task(
            name=name,
            task_type=task_type,
            difficulty=difficulty,
            days_of_week=days_str,
            goal=goal,
            user_id=user.id,
            is_one_time=is_one_time
        )
        db.session.add(new_task)
        db.session.commit()

        return redirect(url_for("view_tasks"))

    return render_template("create_task.html", user=user)

@app.route("/edit_task/<int:task_id>", methods=["GET", "POST"])
def edit_task(task_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    task = Task.query.get_or_404(task_id)
    if task.user_id != user.id:
        return "That is not your task.", 403

    if request.method == "POST":
        task.name = request.form["name"]
        task.task_type = request.form["task_type"]
        task.goal = int(request.form.get("goal", 1))

        selected_days = request.form.getlist("days_of_week")
        days_str = ",".join(selected_days)
        task.days_of_week = days_str

        difficulty = int(request.form.get("difficulty", 1))
        task.difficulty = difficulty
        
        one_time_val = request.form.get("is_one_time", "false")
        task.is_one_time = (one_time_val == "true")

        # Reset if switching type
        if task.task_type == "count":
            task.is_done = False
        elif task.task_type == "boolean":
            task.count = 0

        db.session.commit()
        return redirect(url_for("view_tasks"))

    return render_template("edit_task.html", user=user, task=task)

@app.route("/toggle_task/<int:task_id>", methods=["POST"])
def toggle_task(task_id):
    """Toggle a boolean task; award XP if toggling to done."""
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    task = Task.query.get_or_404(task_id)
    if task.user_id != user.id:
        return "That is not your task.", 403

    old_done = task.is_done
    task.is_done = not task.is_done
    db.session.commit()

    return redirect(url_for("view_tasks"))

@app.route("/update_task/<int:task_id>", methods=["POST"])
def update_task(task_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    task = Task.query.get_or_404(task_id)
    if task.user_id != user.id:
        return "That is not your task.", 403

    amount = int(request.form["amount"])
    
    # new logic: if adding negative
    # we can ensure it doesn't go below zero (if you want)
    new_count = task.count + amount
    if new_count < 0:
        new_count = 0  # or block it entirely?
    
    task.count = new_count
    db.session.commit()
    
    return redirect(url_for("view_tasks"))

@app.route("/delete_task/<int:task_id>", methods=["POST"])
def delete_task(task_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    task = Task.query.get_or_404(task_id)
    if task.user_id != user.id:
        return "Not your task!", 403

    db.session.delete(task)
    db.session.commit()
    return redirect(url_for("view_tasks"))

# @app.route("/force_xp_update", methods=["POST"])
# def force_xp_update():
#     user = get_current_user()
#     if not user:
#         return "Not logged in", 403

#     # CAUTION: In a real app, you might want admin-only checks or environment checks
#     do_daily_evaluation_for_user(user)  # or whichever awarding function

#     return redirect(url_for("profile"))

########################################
# RUN
########################################
if __name__ == "__main__":
    with app.app_context():
        db.create_all() # Create tables if they don't exist
    app.run(debug=True, port=7000)
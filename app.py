import os
from flask import Flask, render_template, request, redirect, session, url_for
from werkzeug.utils import secure_filename
import textdistance
import docx

app = Flask(__name__)
app.secret_key = "supersecretkey"  # for session management
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ----------------- Helper Functions -----------------

def extract_text(file_path):
    if file_path.endswith(".docx"):
        doc = docx.Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])
    else:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

def similarity_score(text1, text2):
    # Quick lightweight similarity
    return textdistance.jaccard.normalized_similarity(text1, text2) * 100

# ----------------- Routes -----------------

users = {}  # username: password (for demo; in production use DB)
history = {}  # username: list of uploaded files

@app.route("/")
def home():
    if "username" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if username in users:
            return "Username already exists"
        users[username] = password
        history[username] = []
        os.makedirs(os.path.join(UPLOAD_FOLDER, username), exist_ok=True)
        session["username"] = username
        return redirect(url_for("dashboard"))
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        if users.get(username) == password:
            session["username"] = username
            return redirect(url_for("dashboard"))
        return "Invalid credentials"
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if "username" not in session:
        return redirect(url_for("login"))
    user_files = history.get(session["username"], [])
    return render_template("dashboard.html", files=user_files)

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "username" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        file = request.files["file"]
        if file:
            filename = secure_filename(file.filename)
            user_folder = os.path.join(UPLOAD_FOLDER, session["username"])
            filepath = os.path.join(user_folder, filename)
            file.save(filepath)
            # check similarity against previous uploads of all users
            results = []
            for user, files in history.items():
                for f in files:
                    fpath = os.path.join(UPLOAD_FOLDER, user, f)
                    score = similarity_score(extract_text(filepath), extract_text(fpath))
                    results.append({"file": f, "user": user, "score": round(score,2)})
            history[session["username"]].append(filename)
            return render_template("results.html", results=results, filename=filename)
    return render_template("upload.html")
    
@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)

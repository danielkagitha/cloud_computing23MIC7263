import os
import zipfile
import json
import requests
import xml.etree.ElementTree as ET
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, url_for, send_from_directory, flash
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import docx
import textdistance

# ---------- Config ----------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey-change-in-production")

# Upload storage
BASE_DIR = os.getcwd()
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXT = (".docx", ".txt", ".pdf", ".md")

# SQLite database
DATABASE_PATH = os.path.join(BASE_DIR, "plagiarism.db")

def get_db_conn():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ---------- Database Initialization ----------
def init_database():
    """Automatically create tables if they don't exist"""
    conn = get_db_conn()
    cursor = conn.cursor()
    
    try:
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(100) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create uploads table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                filename VARCHAR(255) NOT NULL,
                original_name VARCHAR(255) NOT NULL,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        
        conn.commit()
        print("✅ Database tables created successfully")
        return True
        
    except Exception as e:
        print(f"❌ Error creating tables: {e}")
        return False
    finally:
        conn.close()

# Initialize database
init_database()

# ---------- Helper Functions ----------
def extract_text(file_path):
    """Extract text from .docx or .txt files"""
    if not os.path.exists(file_path):
        return ""
    
    try:
        if file_path.lower().endswith(".docx"):
            doc = docx.Document(file_path)
            full_text = []
            for para in doc.paragraphs:
                if para.text.strip():
                    full_text.append(para.text)
            return "\n".join(full_text)
        else:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        print(f"Error extracting text from {file_path}: {e}")
        return ""

def similarity_score(text1, text2):
    """Compute similarity percentage between two texts using Jaccard similarity"""
    if not text1 or not text2:
        return 0.0
    
    t1 = " ".join(text1.split())[:5000]
    t2 = " ".join(text2.split())[:5000]
    
    return textdistance.jaccard.normalized_similarity(t1, t2) * 100

def save_upload_file(user_folder, file_storage):
    """Save uploaded file with timestamp prefix for uniqueness"""
    filename = secure_filename(file_storage.filename)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    stored_filename = f"{ts}_{filename}"
    dest_path = os.path.join(user_folder, stored_filename)
    
    os.makedirs(user_folder, exist_ok=True)
    file_storage.save(dest_path)
    
    return stored_filename, filename, dest_path

# ---------- Database Operations ----------
def create_user(username, password):
    """Create new user in database"""
    pw_hash = generate_password_hash(password)
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)", 
            (username, pw_hash)
        )
        conn.commit()
        user_id = cursor.lastrowid
        return user_id
    except Exception as e:
        print(f"Error creating user: {e}")
        return None
    finally:
        conn.close()

def get_user_by_username(username):
    """Get user by username from database"""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?", 
            (username,)
        )
        result = cursor.fetchone()
        if result:
            return {'id': result[0], 'username': result[1], 'password_hash': result[2]}
        return None
    except Exception as e:
        print(f"Error getting user: {e}")
        return None
    finally:
        conn.close()

def add_upload_record(user_id, stored_filename, original_name):
    """Add upload record to database"""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO uploads (user_id, filename, original_name) VALUES (?, ?, ?)",
            (user_id, stored_filename, original_name)
        )
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        print(f"Error adding upload record: {e}")
        return None
    finally:
        conn.close()

def get_user_uploads(user_id):
    """Get all uploads for a user"""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT filename, original_name, uploaded_at FROM uploads WHERE user_id = ? ORDER BY uploaded_at DESC", 
            (user_id,)
        )
        results = cursor.fetchall()
        return [{'filename': row[0], 'original_name': row[1], 'uploaded_at': row[2]} for row in results]
    except Exception as e:
        print(f"Error getting user uploads: {e}")
        return []
    finally:
        conn.close()

def get_all_uploads_except_user(user_id):
    """Get all uploads except current user's for comparison"""
    conn = get_db_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT u.filename, u.original_name, us.username 
            FROM uploads u 
            JOIN users us ON u.user_id = us.id 
            WHERE u.user_id != ?
        """, (user_id,))
        results = cursor.fetchall()
        return [{'filename': row[0], 'original_name': row[1], 'username': row[2]} for row in results]
    except Exception as e:
        print(f"Error getting all uploads: {e}")
        return []
    finally:
        conn.close()

# ---------- Generic API Parser Functions ----------
def parse_generic_api_response(api_url, response_content, content_type):
    papers = []
    
    try:
        if content_type == 'application/json' or api_url.endswith('.json'):
            data = json.loads(response_content)
            papers = extract_papers_from_json(data)
        elif content_type == 'application/xml' or content_type == 'text/xml' or api_url.endswith('.xml'):
            papers = extract_papers_from_xml(response_content)
        elif 'rss' in content_type or api_url.endswith('.rss'):
            papers = extract_papers_from_rss(response_content)
        else:
            papers = auto_detect_format(response_content)
    except Exception as e:
        print(f"Error parsing API response: {e}")
        papers = fallback_parse(response_content)
    
    return papers

def extract_papers_from_json(data):
    papers = []
    possible_paths = [
        data.get('papers', []),
        data.get('data', []),
        data.get('results', []),
        data.get('works', []),
        data.get('documents', []),
        data.get('items', []),
        data.get('publications', []),
        data if isinstance(data, list) else []
    ]
    
    for papers_list in possible_paths:
        if papers_list and isinstance(papers_list, list):
            for paper in papers_list:
                if isinstance(paper, dict):
                    extracted = extract_paper_info(paper)
                    if extracted:
                        papers.append(extracted)
            if papers:
                break
    return papers

def extract_papers_from_xml(xml_content):
    papers = []
    try:
        root = ET.fromstring(xml_content)
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        
        if root.tag.endswith('feed') or root.tag == '{http://www.w3.org/2005/Atom}feed':
            entries = root.findall('atom:entry', ns) or root.findall('entry')
            for entry in entries:
                paper_info = extract_paper_from_arxiv_entry(entry)
                if paper_info and paper_info.get('title') != "No Title Available":
                    papers.append(paper_info)
        elif root.tag == 'rss' or root.tag.endswith('rss'):
            for item in root.findall('.//item'):
                paper_info = extract_paper_from_rss_item(item)
                if paper_info:
                    papers.append(paper_info)
        else:
            for item in root.findall('.//item') + root.findall('.//entry') + root.findall('.//paper'):
                paper_info = extract_paper_from_generic_xml(item)
                if paper_info:
                    papers.append(paper_info)
    except Exception as e:
        print(f"XML parsing error: {e}")
    return papers

def extract_paper_from_arxiv_entry(entry):
    paper = {}
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    
    try:
        title_elem = entry.find('atom:title', ns) or entry.find('title')
        paper['title'] = ' '.join(title_elem.text.split()) if title_elem is not None and title_elem.text else "No Title Available"
        
        summary_elem = entry.find('atom:summary', ns) or entry.find('summary')
        paper['abstract'] = ' '.join(summary_elem.text.split()) if summary_elem is not None and summary_elem.text else ""
        
        authors = []
        author_elems = entry.findall('atom:author', ns) or entry.findall('author')
        for author_elem in author_elems:
            name_elem = author_elem.find('atom:name', ns) or author_elem.find('name')
            if name_elem is not None and name_elem.text:
                authors.append(' '.join(name_elem.text.split()))
        paper['authors'] = ", ".join(authors) if authors else "Unknown Authors"
        
        published_elem = entry.find('atom:published', ns) or entry.find('published')
        paper['published'] = published_elem.text if published_elem is not None else ""
        
        id_elem = entry.find('atom:id', ns) or entry.find('id')
        paper['arxiv_id'] = id_elem.text.split('/')[-1] if id_elem is not None and id_elem.text else ""
    except Exception as e:
        print(f"Error parsing arXiv entry: {e}")
        paper['title'] = "Error parsing paper"
        paper['abstract'] = ""
        paper['authors'] = "Unknown"
    return paper

def extract_paper_from_rss_item(item):
    paper = {}
    title_elem = item.find('title')
    paper['title'] = ' '.join(title_elem.text.split()) if title_elem is not None and title_elem.text else "No Title Available"
    
    desc_elem = item.find('description')
    paper['abstract'] = ' '.join(desc_elem.text.split()) if desc_elem is not None and desc_elem.text else ""
    
    author_elem = item.find('author')
    paper['authors'] = ' '.join(author_elem.text.split()) if author_elem is not None and author_elem.text else "Unknown Authors"
    
    date_elem = item.find('pubDate')
    paper['published'] = date_elem.text if date_elem is not None and date_elem.text else ""
    return paper

def extract_paper_from_generic_xml(item):
    paper = {}
    paper['title'] = get_xml_text(item, 'title')
    paper['abstract'] = get_xml_text(item, 'abstract') or get_xml_text(item, 'summary') or get_xml_text(item, 'description')
    paper['authors'] = get_xml_text(item, 'author') or get_xml_text(item, 'creator')
    paper['published'] = get_xml_text(item, 'published') or get_xml_text(item, 'date') or get_xml_text(item, 'pubDate')
    return paper

def extract_papers_from_rss(rss_content):
    papers = []
    try:
        root = ET.fromstring(rss_content)
        for item in root.findall('.//item'):
            paper = {}
            paper['title'] = get_xml_text(item, 'title')
            paper['abstract'] = get_xml_text(item, 'description') or get_xml_text(item, 'summary')
            paper['authors'] = get_xml_text(item, 'author') or get_xml_text(item, 'creator')
            paper['published'] = get_xml_text(item, 'pubDate') or get_xml_text(item, 'date')
            if paper['title']:
                papers.append(paper)
    except Exception as e:
        print(f"RSS parsing error: {e}")
    return papers

def extract_paper_info(paper_dict):
    paper = {}
    title_fields = ['title', 'name', 'document_title', 'paper_title', 'heading']
    paper['title'] = find_value(paper_dict, title_fields) or "Untitled Paper"
    
    abstract_fields = ['abstract', 'summary', 'description', 'content', 'paper_abstract']
    paper['abstract'] = find_value(paper_dict, abstract_fields) or ""
    
    author_fields = ['authors', 'author', 'creators', 'contributors', 'writer']
    authors_data = find_value(paper_dict, author_fields)
    
    if isinstance(authors_data, list):
        paper['authors'] = ", ".join([str(author) for author in authors_data])
    elif isinstance(authors_data, str):
        paper['authors'] = authors_data
    else:
        paper['authors'] = "Unknown Author"
    
    date_fields = ['published', 'publication_date', 'date', 'created', 'year']
    paper['published'] = find_value(paper_dict, date_fields) or ""
    return paper

def find_value(data_dict, possible_keys):
    for key in possible_keys:
        if key in data_dict and data_dict[key]:
            return data_dict[key]
    return None

def get_xml_text(element, tag_name, namespace=None):
    if namespace:
        elem = element.find(f'{namespace}:{tag_name}', {'namespace': namespace})
    else:
        elem = element.find(tag_name)
    return elem.text if elem is not None and elem.text else ""

def auto_detect_format(content):
    papers = []
    try:
        data = json.loads(content)
        papers = extract_papers_from_json(data)
        if papers:
            return papers
    except:
        pass
    
    try:
        papers = extract_papers_from_xml(content)
        if papers:
            return papers
    except:
        pass
    return papers

def fallback_parse(content):
    papers = []
    lines = content.split('\n')
    current_paper = {}
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if len(line) > 20 and len(line) < 200 and not current_paper.get('title'):
            if any(keyword in line.lower() for keyword in ['title', 'paper', 'research', 'study', 'analysis']):
                current_paper['title'] = line
            else:
                current_paper['title'] = line[:100] + "..." if len(line) > 100 else line
        elif len(line) > 50 and not current_paper.get('abstract'):
            current_paper['abstract'] = line[:500]
            
        if current_paper.get('title') and current_paper.get('abstract'):
            current_paper['authors'] = "Unknown"
            papers.append(current_paper)
            current_paper = {}
    return papers

# ---------- Routes ----------
@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        if not username or not password:
            flash("Please enter both username and password", "error")
            return redirect(url_for("signup"))
        
        if len(username) < 3:
            flash("Username must be at least 3 characters long", "error")
            return redirect(url_for("signup"))
        
        if len(password) < 6:
            flash("Password must be at least 6 characters long", "error")
            return redirect(url_for("signup"))
        
        if get_user_by_username(username):
            flash("Username already exists", "error")
            return redirect(url_for("signup"))
        
        user_id = create_user(username, password)
        if user_id:
            user_folder = os.path.join(UPLOAD_FOLDER, username)
            os.makedirs(user_folder, exist_ok=True)
            session["user"] = {"id": user_id, "username": username}
            flash("Account created successfully!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Error creating account. Please try again.", "error")
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        user = get_user_by_username(username)
        if user and check_password_hash(user["password_hash"], password):
            session["user"] = {"id": user["id"], "username": user["username"]}
            os.makedirs(os.path.join(UPLOAD_FOLDER, username), exist_ok=True)
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid username or password", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out successfully", "success")
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    uploads = get_user_uploads(user["id"])
    return render_template("dashboard.html", username=user["username"], files=uploads)

@app.route("/compare_file_file", methods=["POST"])
def compare_file_file():
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    file1 = request.files.get("file1")
    file2 = request.files.get("file2")
    
    if not file1 or not file2:
        flash("Please select both files to compare", "error")
        return redirect(url_for("dashboard"))
    
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    stored1, orig1, path1 = save_upload_file(user_folder, file1)
    stored2, orig2, path2 = save_upload_file(user_folder, file2)
    add_upload_record(user["id"], stored1, orig1)
    add_upload_record(user["id"], stored2, orig2)
    
    text1 = extract_text(path1)
    text2 = extract_text(path2)
    
    if not text1 or not text2:
        flash("Could not extract text from one or both files", "error")
        return redirect(url_for("dashboard"))
    
    score = round(similarity_score(text1, text2), 2)
    results = [{"file": orig2, "score": score, "status": "High plagiarism" if score > 70 else "Medium plagiarism" if score > 40 else "Low plagiarism"}]
    return render_template("results.html", results=results, filename=orig1, comparison_type="File vs File")

@app.route("/compare_file_folder", methods=["POST"])
def compare_file_folder():
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    main_file = request.files.get("main_file")
    zip_file = request.files.get("zip_folder")
    
    if not main_file or not zip_file:
        flash("Please select both a main file and a ZIP folder", "error")
        return redirect(url_for("dashboard"))
    
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    stored_main, orig_main, path_main = save_upload_file(user_folder, main_file)
    add_upload_record(user["id"], stored_main, orig_main)
    
    temp_dir = os.path.join(user_folder, f"temp_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(zip_file, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
    except zipfile.BadZipFile:
        flash("Invalid ZIP file", "error")
        return redirect(url_for("dashboard"))
    
    main_text = extract_text(path_main)
    if not main_text:
        flash("Could not extract text from the main file", "error")
        return redirect(url_for("dashboard"))
    
    results = []
    for root, dirs, files in os.walk(temp_dir):
        for file in files:
            if file.lower().endswith(('.docx', '.txt', '.pdf')):
                file_path = os.path.join(root, file)
                file_text = extract_text(file_path)
                if file_text:
                    score = round(similarity_score(main_text, file_text), 2)
                    results.append({"file": file, "score": score, "status": "High plagiarism" if score > 70 else "Medium plagiarism" if score > 40 else "Low plagiarism"})
    
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
    results = sorted(results, key=lambda x: x["score"], reverse=True)[:10]
    return render_template("results.html", results=results, filename=orig_main, comparison_type="File vs Folder")

@app.route("/compare_file_api", methods=["POST"])
def compare_file_api():
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    main_file = request.files.get("main_file")
    api_url = request.form.get("api_url", "").strip()
    
    if not main_file:
        flash("Please select a file to check", "error")
        return redirect(url_for("dashboard"))
    
    if not api_url:
        flash("Please provide a research API URL", "error")
        return redirect(url_for("dashboard"))
    
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    stored_main, orig_main, path_main = save_upload_file(user_folder, main_file)
    add_upload_record(user["id"], stored_main, orig_main)
    
    main_text = extract_text(path_main)
    if not main_text:
        flash("Could not extract text from the file", "error")
        return redirect(url_for("dashboard"))
    
    results = []
    try:
        headers = {'User-Agent': 'Plagiarism-Checker/1.0', 'Accept': 'application/json,application/xml,text/xml'}
        response = requests.get(api_url, headers=headers, timeout=20)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '').split(';')[0]
        papers = parse_generic_api_response(api_url, response.text, content_type)
        
        if not papers:
            papers = auto_detect_format(response.text)
        
        for paper in papers[:50]:
            paper_text = f"{paper.get('title', '')}\n{paper.get('abstract', '')}"
            if paper_text.strip():
                score = round(similarity_score(main_text, paper_text), 2)
                status = "High plagiarism" if score > 70 else "Medium plagiarism" if score > 40 else "Low plagiarism"
                results.append({"file": paper.get('title', 'Unknown Paper'), "score": score, "status": status, "authors": paper.get('authors', 'Unknown'), "published": paper.get('published', '')})
        
        if not results:
            results.append({"file": "No papers could be extracted from the API", "score": 0, "status": "No data", "authors": "", "published": ""})
    except requests.exceptions.RequestException as e:
        results.append({"file": f"API Connection Error: {str(e)}", "score": 0, "status": "Error", "authors": "", "published": ""})
    except Exception as e:
        results.append({"file": f"Processing Error: {str(e)}", "score": 0, "status": "Error", "authors": "", "published": ""})
    
    results = sorted(results, key=lambda x: x["score"], reverse=True)[:15]
    return render_template("results.html", results=results, filename=orig_main, comparison_type="File vs Research API", api_url=api_url)

@app.route("/compare_api", methods=["POST"])
def compare_api():
    return compare_file_api()

@app.route("/history")
def history():
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    uploads = get_user_uploads(user["id"])
    return render_template("history.html", username=user["username"], files=uploads)

@app.route("/download/<filename>")
def download_file(filename):
    if "user" not in session:
        return redirect(url_for("login"))
    user = session["user"]
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    return send_from_directory(user_folder, filename, as_attachment=True)

# ---------- Run Application ----------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

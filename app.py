import os
import io
import zipfile
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, url_for, send_from_directory, flash
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import mysql.connector
from mysql.connector import pooling
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

# DB config from environment
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASS", ""),
    "database": os.getenv("DB_NAME", "plagiarism_db"),
    "auth_plugin": "mysql_native_password"
}


# Add this right after your DB_CONFIG in app.py

def init_database():
    """Automatically create tables if they don't exist"""
    conn = get_db_conn()
    if not conn:
        print("❌ Cannot connect to database")
        return False
    
    cursor = conn.cursor()
    try:
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create uploads table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL,
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
        cursor.close()
        conn.close()

# Call this function when app starts
init_database()

# Create connection pool
try:
    db_pool = pooling.MySQLConnectionPool(
        pool_name="mypool", 
        pool_size=5, 
        **{k: v for k, v in DB_CONFIG.items() if v is not None}
    )
    print("✅ Database connection pool created successfully")
except Exception as e:
    print(f"❌ Database connection failed: {e}")
    db_pool = None

# ---------- Generic API Parser Functions ----------

def parse_generic_api_response(api_url, response_content, content_type):
    """
    Universal API parser that handles ANY research organization's API
    Supports: JSON, XML, RSS formats
    """
    papers = []
    
    try:
        # Handle JSON responses
        if content_type == 'application/json' or api_url.endswith('.json'):
            data = json.loads(response_content)
            papers = extract_papers_from_json(data)
        
        # Handle XML responses (arXiv, RSS feeds)
        elif content_type == 'application/xml' or content_type == 'text/xml' or api_url.endswith('.xml'):
            papers = extract_papers_from_xml(response_content)
        
        # Handle RSS feeds
        elif 'rss' in content_type or api_url.endswith('.rss'):
            papers = extract_papers_from_rss(response_content)
        
        # Auto-detect format
        else:
            papers = auto_detect_format(response_content)
            
    except Exception as e:
        print(f"Error parsing API response: {e}")
        # Try fallback parsing
        papers = fallback_parse(response_content)
    
    return papers

def extract_papers_from_json(data):
    """Extract papers from JSON API responses"""
    papers = []
    
    # Common JSON structures across research APIs
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
            if papers:  # Stop at first successful extraction
                break
                
    return papers

def extract_papers_from_xml(xml_content):
    """Extract papers from XML API responses - FIXED VERSION"""
    papers = []
    
    try:
        # Parse XML content
        root = ET.fromstring(xml_content)
        
        # Define namespaces
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        
        # Check if this is arXiv format (has 'feed' as root with entries)
        if root.tag.endswith('feed') or root.tag == '{http://www.w3.org/2005/Atom}feed':
            # arXiv format - find all entries
            entries = root.findall('atom:entry', ns)
            if not entries:
                entries = root.findall('entry')
            
            print(f"Found {len(entries)} entries in arXiv XML")
            
            for entry in entries:
                paper_info = extract_paper_from_arxiv_entry(entry)
                if paper_info and paper_info.get('title') != "No Title Available":
                    papers.append(paper_info)
        
        # Handle RSS format
        elif root.tag == 'rss' or root.tag.endswith('rss'):
            for item in root.findall('.//item'):
                paper_info = extract_paper_from_rss_item(item)
                if paper_info:
                    papers.append(paper_info)
        
        # Generic XML with items
        else:
            for item in root.findall('.//item') + root.findall('.//entry') + root.findall('.//paper'):
                paper_info = extract_paper_from_generic_xml(item)
                if paper_info:
                    papers.append(paper_info)
                    
        print(f"Successfully extracted {len(papers)} papers from XML")
                    
    except Exception as e:
        print(f"XML parsing error: {e}")
        import traceback
        traceback.print_exc()
        
    return papers

def extract_paper_from_arxiv_entry(entry):
    """Extract paper info from arXiv XML entry - FIXED VERSION"""
    paper = {}
    
    # Define namespaces for arXiv XML
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    
    try:
        # Get title - handle namespace properly
        title_elem = entry.find('atom:title', ns)
        if title_elem is None:
            title_elem = entry.find('title')
        
        if title_elem is not None and title_elem.text:
            # Clean up title - remove extra spaces and newlines
            paper['title'] = ' '.join(title_elem.text.split())
        else:
            paper['title'] = "No Title Available"
        
        # Get summary (abstract)
        summary_elem = entry.find('atom:summary', ns)
        if summary_elem is None:
            summary_elem = entry.find('summary')
        
        if summary_elem is not None and summary_elem.text:
            paper['abstract'] = ' '.join(summary_elem.text.split())
        else:
            paper['abstract'] = ""
        
        # Get authors
        authors = []
        author_elems = entry.findall('atom:author', ns)
        if not author_elems:
            author_elems = entry.findall('author')
        
        for author_elem in author_elems:
            name_elem = author_elem.find('atom:name', ns)
            if name_elem is None:
                name_elem = author_elem.find('name')
            if name_elem is not None and name_elem.text:
                authors.append(' '.join(name_elem.text.split()))
        
        paper['authors'] = ", ".join(authors) if authors else "Unknown Authors"
        
        # Get published date
        published_elem = entry.find('atom:published', ns)
        if published_elem is None:
            published_elem = entry.find('published')
        
        paper['published'] = published_elem.text if published_elem is not None else ""
        
        # Get arXiv ID
        id_elem = entry.find('atom:id', ns)
        if id_elem is None:
            id_elem = entry.find('id')
        
        if id_elem is not None and id_elem.text:
            paper['arxiv_id'] = id_elem.text.split('/')[-1]
        else:
            paper['arxiv_id'] = ""
            
    except Exception as e:
        print(f"Error parsing arXiv entry: {e}")
        paper['title'] = "Error parsing paper"
        paper['abstract'] = ""
        paper['authors'] = "Unknown"
        
    return paper

def extract_paper_from_rss_item(item):
    """Extract paper info from RSS item"""
    paper = {}
    
    # Get title
    title_elem = item.find('title')
    if title_elem is not None and title_elem.text:
        paper['title'] = ' '.join(title_elem.text.split())
    else:
        paper['title'] = "No Title Available"
    
    # Get description/abstract
    desc_elem = item.find('description')
    if desc_elem is not None and desc_elem.text:
        paper['abstract'] = ' '.join(desc_elem.text.split())
    else:
        paper['abstract'] = ""
    
    # Get authors
    author_elem = item.find('author')
    if author_elem is not None and author_elem.text:
        paper['authors'] = ' '.join(author_elem.text.split())
    else:
        paper['authors'] = "Unknown Authors"
    
    # Get publication date
    date_elem = item.find('pubDate')
    if date_elem is not None and date_elem.text:
        paper['published'] = date_elem.text
    else:
        paper['published'] = ""
    
    return paper

def extract_paper_from_generic_xml(item):
    """Extract paper info from generic XML item"""
    paper = {}
    paper['title'] = get_xml_text(item, 'title')
    paper['abstract'] = get_xml_text(item, 'abstract') or get_xml_text(item, 'summary') or get_xml_text(item, 'description')
    paper['authors'] = get_xml_text(item, 'author') or get_xml_text(item, 'creator')
    paper['published'] = get_xml_text(item, 'published') or get_xml_text(item, 'date') or get_xml_text(item, 'pubDate')
    return paper

def extract_papers_from_rss(rss_content):
    """Extract papers from RSS feeds"""
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
    """Extract standardized paper info from any dictionary structure"""
    paper = {}
    
    # Title extraction from common field names
    title_fields = ['title', 'name', 'document_title', 'paper_title', 'heading']
    paper['title'] = find_value(paper_dict, title_fields) or "Untitled Paper"
    
    # Abstract extraction from common field names
    abstract_fields = ['abstract', 'summary', 'description', 'content', 'paper_abstract']
    paper['abstract'] = find_value(paper_dict, abstract_fields) or ""
    
    # Authors extraction
    author_fields = ['authors', 'author', 'creators', 'contributors', 'writer']
    authors_data = find_value(paper_dict, author_fields)
    
    if isinstance(authors_data, list):
        paper['authors'] = ", ".join([str(author) for author in authors_data])
    elif isinstance(authors_data, str):
        paper['authors'] = authors_data
    else:
        paper['authors'] = "Unknown Author"
    
    # Published date
    date_fields = ['published', 'publication_date', 'date', 'created', 'year']
    paper['published'] = find_value(paper_dict, date_fields) or ""
    
    return paper

def find_value(data_dict, possible_keys):
    """Find value in dictionary using multiple possible keys"""
    for key in possible_keys:
        if key in data_dict and data_dict[key]:
            return data_dict[key]
    return None

def get_xml_text(element, tag_name, namespace=None):
    """Safely get text from XML element"""
    if namespace:
        elem = element.find(f'{namespace}:{tag_name}', {'namespace': namespace})
    else:
        elem = element.find(tag_name)
    return elem.text if elem is not None and elem.text else ""

def auto_detect_format(content):
    """Auto-detect content format and parse accordingly"""
    papers = []
    
    # Try JSON first
    try:
        data = json.loads(content)
        papers = extract_papers_from_json(data)
        if papers:
            return papers
    except:
        pass
    
    # Try XML
    try:
        papers = extract_papers_from_xml(content)
        if papers:
            return papers
    except:
        pass
    
    return papers

def fallback_parse(content):
    """Fallback parsing for unknown formats"""
    papers = []
    
    # Simple text-based extraction as last resort
    lines = content.split('\n')
    current_paper = {}
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Look for title-like lines
        if len(line) > 20 and len(line) < 200 and not current_paper.get('title'):
            if any(keyword in line.lower() for keyword in ['title', 'paper', 'research', 'study', 'analysis']):
                current_paper['title'] = line
            else:
                current_paper['title'] = line[:100] + "..." if len(line) > 100 else line
                
        # Look for abstract-like content
        elif len(line) > 50 and not current_paper.get('abstract'):
            current_paper['abstract'] = line[:500]  # Limit abstract length
            
        # If we have both title and abstract, save the paper
        if current_paper.get('title') and current_paper.get('abstract'):
            current_paper['authors'] = "Unknown"
            papers.append(current_paper)
            current_paper = {}
    
    return papers

# ---------- Helper Functions ----------
def get_db_conn():
    if db_pool:
        return db_pool.get_connection()
    return None

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
    
    # Normalize and limit text length for performance
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
    if not conn:
        return None
    
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, %s)", 
            (username, pw_hash)
        )
        conn.commit()
        user_id = cursor.lastrowid
        return user_id
    except mysql.connector.Error as e:
        conn.rollback()
        print(f"Error creating user: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def get_user_by_username(username):
    """Get user by username from database"""
    conn = get_db_conn()
    if not conn:
        return None
    
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, username, password_hash FROM users WHERE username = %s", 
            (username,)
        )
        return cursor.fetchone()
    except mysql.connector.Error as e:
        print(f"Error getting user: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def add_upload_record(user_id, stored_filename, original_name):
    """Add upload record to database"""
    conn = get_db_conn()
    if not conn:
        return None
    
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO uploads (user_id, filename, original_name) VALUES (%s, %s, %s)",
            (user_id, stored_filename, original_name)
        )
        conn.commit()
        return cursor.lastrowid
    except mysql.connector.Error as e:
        conn.rollback()
        print(f"Error adding upload record: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def get_user_uploads(user_id):
    """Get all uploads for a user"""
    conn = get_db_conn()
    if not conn:
        return []
    
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT filename, original_name, uploaded_at FROM uploads WHERE user_id = %s ORDER BY uploaded_at DESC", 
            (user_id,)
        )
        return cursor.fetchall()
    except mysql.connector.Error as e:
        print(f"Error getting user uploads: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

def get_all_uploads_except_user(user_id):
    """Get all uploads except current user's for comparison"""
    conn = get_db_conn()
    if not conn:
        return []
    
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT u.filename, u.original_name, us.username 
            FROM uploads u 
            JOIN users us ON u.user_id = us.id 
            WHERE u.user_id != %s
        """, (user_id,))
        return cursor.fetchall()
    except mysql.connector.Error as e:
        print(f"Error getting all uploads: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

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
        
        # Check if user exists
        if get_user_by_username(username):
            flash("Username already exists", "error")
            return redirect(url_for("signup"))
        
        # Create user
        user_id = create_user(username, password)
        if user_id:
            # Create user upload folder
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
            
            # Ensure user folder exists
            user_folder = os.path.join(UPLOAD_FOLDER, username)
            os.makedirs(user_folder, exist_ok=True)
            
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid username or password", "error")
    
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("You have been logged out successfully", "success")
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))
    
    user = session["user"]
    uploads = get_user_uploads(user["id"])
    
    return render_template("dashboard.html", 
                         username=user["username"], 
                         files=uploads)

# ---------- Plagiarism Detection Routes ----------

@app.route("/compare_file_file", methods=["POST"])
def compare_file_file():
    """Compare two individual files for plagiarism"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user = session["user"]
    file1 = request.files.get("file1")
    file2 = request.files.get("file2")
    
    if not file1 or not file2:
        flash("Please select both files to compare", "error")
        return redirect(url_for("dashboard"))
    
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    
    # Save both files
    stored1, orig1, path1 = save_upload_file(user_folder, file1)
    stored2, orig2, path2 = save_upload_file(user_folder, file2)
    
    # Add to upload history
    add_upload_record(user["id"], stored1, orig1)
    add_upload_record(user["id"], stored2, orig2)
    
    # Extract text and compare
    text1 = extract_text(path1)
    text2 = extract_text(path2)
    
    if not text1 or not text2:
        flash("Could not extract text from one or both files", "error")
        return redirect(url_for("dashboard"))
    
    score = round(similarity_score(text1, text2), 2)
    
    results = [{
        "file": orig2,
        "score": score,
        "status": "High plagiarism" if score > 70 else "Medium plagiarism" if score > 40 else "Low plagiarism"
    }]
    
    return render_template("results.html", 
                         results=results, 
                         filename=orig1,
                         comparison_type="File vs File")

@app.route("/compare_file_folder", methods=["POST"])
def compare_file_folder():
    """Compare a file against all files in a ZIP folder"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user = session["user"]
    main_file = request.files.get("main_file")
    zip_file = request.files.get("zip_folder")
    
    if not main_file or not zip_file:
        flash("Please select both a main file and a ZIP folder", "error")
        return redirect(url_for("dashboard"))
    
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    
    # Save main file
    stored_main, orig_main, path_main = save_upload_file(user_folder, main_file)
    add_upload_record(user["id"], stored_main, orig_main)
    
    # Extract ZIP file
    temp_dir = os.path.join(user_folder, f"temp_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(zip_file, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
    except zipfile.BadZipFile:
        flash("Invalid ZIP file", "error")
        return redirect(url_for("dashboard"))
    
    # Extract text from main file
    main_text = extract_text(path_main)
    if not main_text:
        flash("Could not extract text from the main file", "error")
        return redirect(url_for("dashboard"))
    
    # Compare with all files in extracted folder
    results = []
    for root, dirs, files in os.walk(temp_dir):
        for file in files:
            if file.lower().endswith(('.docx', '.txt', '.pdf')):
                file_path = os.path.join(root, file)
                file_text = extract_text(file_path)
                
                if file_text:
                    score = round(similarity_score(main_text, file_text), 2)
                    results.append({
                        "file": file,
                        "score": score,
                        "status": "High plagiarism" if score > 70 else "Medium plagiarism" if score > 40 else "Low plagiarism"
                    })
    
    # Cleanup temp directory
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)
    
    # Sort by score and get top 10
    results = sorted(results, key=lambda x: x["score"], reverse=True)[:10]
    
    return render_template("results.html", 
                         results=results, 
                         filename=orig_main,
                         comparison_type="File vs Folder")

@app.route("/compare_file_database", methods=["POST"])
def compare_file_database():
    """Compare a file against all files in database (except user's own)"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user = session["user"]
    main_file = request.files.get("main_file")
    
    if not main_file:
        flash("Please select a file to compare", "error")
        return redirect(url_for("dashboard"))
    
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    
    # Save main file
    stored_main, orig_main, path_main = save_upload_file(user_folder, main_file)
    add_upload_record(user["id"], stored_main, orig_main)
    
    # Extract text from main file
    main_text = extract_text(path_main)
    if not main_text:
        flash("Could not extract text from the file", "error")
        return redirect(url_for("dashboard"))
    
    # Get all other uploads from database
    other_uploads = get_all_uploads_except_user(user["id"])
    
    results = []
    for upload in other_uploads:
        other_user_folder = os.path.join(UPLOAD_FOLDER, upload["username"])
        other_file_path = os.path.join(other_user_folder, upload["filename"])
        
        if os.path.exists(other_file_path):
            other_text = extract_text(other_file_path)
            if other_text:
                score = round(similarity_score(main_text, other_text), 2)
                results.append({
                    "file": f"{upload['original_name']} (by {upload['username']})",
                    "score": score,
                    "status": "High plagiarism" if score > 70 else "Medium plagiarism" if score > 40 else "Low plagiarism"
                })
    
    # Sort by score and get top 10
    results = sorted(results, key=lambda x: x["score"], reverse=True)[:10]
    
    return render_template("results.html", 
                         results=results, 
                         filename=orig_main,
                         comparison_type="File vs Database")

@app.route("/compare_file_api", methods=["POST"])
def compare_file_api():
    """Universal API comparison - works with ANY research organization's API"""
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
        # Fetch data from ANY API
        headers = {
            'User-Agent': 'Plagiarism-Checker/1.0',
            'Accept': 'application/json,application/xml,text/xml'
        }
        
        response = requests.get(api_url, headers=headers, timeout=20)
        response.raise_for_status()
        
        # Get content type
        content_type = response.headers.get('content-type', '').split(';')[0]
        
        # Parse ANY API response using generic parser
        papers = parse_generic_api_response(api_url, response.text, content_type)
        
        print(f"DEBUG: Found {len(papers)} papers from API")
        for i, paper in enumerate(papers[:3]):
            print(f"Paper {i}: '{paper.get('title', 'No title')}'")
        
        if not papers:
            flash("No papers found in the API response. Trying alternative parsing...", "warning")
            # Try alternative parsing
            papers = auto_detect_format(response.text)
        
        # Compare with each paper
        for paper in papers[:50]:  # Limit to 50 papers for performance
            paper_text = f"{paper.get('title', '')}\n{paper.get('abstract', '')}"
            if paper_text.strip():
                score = round(similarity_score(main_text, paper_text), 2)
                status = "High plagiarism" if score > 70 else "Medium plagiarism" if score > 40 else "Low plagiarism"
                
                results.append({
                    "file": paper.get('title', 'Unknown Paper'),
                    "score": score,
                    "status": status,
                    "authors": paper.get('authors', 'Unknown'),
                    "published": paper.get('published', '')
                })
        
        if not results:
            results.append({
                "file": "No papers could be extracted from the API",
                "score": 0,
                "status": "No data",
                "authors": "",
                "published": ""
            })
            
    except requests.exceptions.RequestException as e:
        results.append({
            "file": f"API Connection Error: {str(e)}",
            "score": 0,
            "status": "Error",
            "authors": "",
            "published": ""
        })
    except Exception as e:
        results.append({
            "file": f"Processing Error: {str(e)}",
            "score": 0,
            "status": "Error",
            "authors": "",
            "published": ""
        })
    
    # Sort by similarity score
    results = sorted(results, key=lambda x: x["score"], reverse=True)[:15]
    
    return render_template("results.html", 
                         results=results, 
                         filename=orig_main,
                         comparison_type="File vs Research API",
                         api_url=api_url)

# Add alias for compatibility
@app.route("/compare_api", methods=["POST"])
def compare_api():
    return compare_file_api()

@app.route("/history")
def history():
    """Show user's upload history"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user = session["user"]
    uploads = get_user_uploads(user["id"])
    
    return render_template("history.html", 
                         username=user["username"],
                         files=uploads)

@app.route("/download/<filename>")
def download_file(filename):
    """Download uploaded file"""
    if "user" not in session:
        return redirect(url_for("login"))
    
    user = session["user"]
    user_folder = os.path.join(UPLOAD_FOLDER, user["username"])
    
    return send_from_directory(user_folder, filename, as_attachment=True)

# ---------- Error Handlers ----------
@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template("500.html"), 500

# ---------- Run Application ----------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))


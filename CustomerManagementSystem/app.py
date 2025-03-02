from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session, send_file
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime, timedelta
from functools import wraps
import io
import pandas as pd
import requests  # 用於 LINE 推送通知

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.urandom(24),
    UPLOAD_FOLDER=os.path.join(os.path.dirname(__file__), 'uploads'),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # 5MB
    ALLOWED_EXTENSIONS={'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'},
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=False  # 若使用 HTTPS 建議設為 True
)

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

def get_db():
    conn = sqlite3.connect('customer.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def init_db():
    conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                camera_remote_url TEXT,
                internal_ip TEXT,
                communication_port TEXT,
                monitoring_credentials TEXT,
                router_password TEXT,
                google_map_url TEXT,
                floor_plan TEXT,
                created_at DATETIME DEFAULT (datetime('now', 'localtime'))
            );
            CREATE TABLE IF NOT EXISTS repairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                price REAL,
                photo_path TEXT,
                created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                edited_by TEXT,
                FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS repair_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repair_id INTEGER NOT NULL,
                photo_path TEXT,
                created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (repair_id) REFERENCES repairs(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                can_modify_customers INTEGER DEFAULT 0,
                can_edit_repairs INTEGER DEFAULT 0,
                can_add_repairs INTEGER DEFAULT 0,
                can_delete_repairs INTEGER DEFAULT 0,
                avatar TEXT,
                line_id TEXT
            );
            CREATE TABLE IF NOT EXISTS billing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                unpaid_amount REAL DEFAULT 0.0,
                current_step INTEGER DEFAULT 0,
                quotation_path TEXT,
                last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
                last_editor TEXT,
                reminder_date DATETIME,
                notified INTEGER DEFAULT 0,
                FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS billing_quotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                billing_id INTEGER NOT NULL,
                file_path TEXT,
                created_at DATETIME DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (billing_id) REFERENCES billing(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                quotation_confirm_file TEXT,
                quotation_confirm_date DATETIME,
                start_construction_date DATETIME,
                completed_date DATETIME,
                last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_repairs_status ON repairs(status);
        ''')
        conn.commit()
        cur = conn.execute("SELECT COUNT(*) as cnt FROM users")
        count = cur.fetchone()['cnt']
        if count == 0:
            conn.execute("INSERT INTO users (username, password, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ('admin', generate_password_hash('admin'), 'admin', 1, 1, 1, 1))
            conn.execute("INSERT INTO users (username, password, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ('user', generate_password_hash('user'), 'user', 0, 0, 0, 0))
            conn.commit()
    except sqlite3.Error as e:
        print("Database initialization failed:", str(e))
    finally:
        conn.close()

def upgrade_tables():
    conn = get_db()
    try:
        # 升級 billing 表：新增 reminder_date 與 notified 欄位
        cursor = conn.execute("PRAGMA table_info(billing)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "reminder_date" not in columns:
            conn.execute("ALTER TABLE billing ADD COLUMN reminder_date DATETIME")
            conn.commit()
            print("已新增 reminder_date 欄位到 billing 表")
        if "notified" not in columns:
            conn.execute("ALTER TABLE billing ADD COLUMN notified INTEGER DEFAULT 0")
            conn.commit()
            print("已新增 notified 欄位到 billing 表")
        # 升級 users 表：新增 avatar 與 line_id 欄位
        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "avatar" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
            conn.commit()
            print("已新增 avatar 欄位到 users 表")
        if "line_id" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN line_id TEXT")
            conn.commit()
            print("已新增 line_id 欄位到 users 表")
    except Exception as e:
        print("升級表錯誤:", e)
    finally:
        conn.close()

upgrade_tables()
init_db()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user'):
            flash("請先登入", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = session.get('user')
        if not user or user.get('role') != 'admin':
            flash("僅管理者有此操作權限", "danger")
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return decorated_function

def require_permission(permission):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            user = session.get('user')
            if not user or user.get(permission) != 1:
                flash("您没有权限进行此操作", "danger")
                return redirect(url_for('home'))
            return f(*args, **kwargs)
        return wrapped
    return decorator

# 登入/登出
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        remember = request.form.get('remember') == 'on'
        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if user and check_password_hash(user['password'], password):
                session['user'] = {
                    'id': user['id'],
                    'username': user['username'],
                    'role': user['role'],
                    'can_modify_customers': user['can_modify_customers'],
                    'can_edit_repairs': user['can_edit_repairs'],
                    'can_add_repairs': user['can_add_repairs'],
                    'can_delete_repairs': user['can_delete_repairs']
                }
                session.permanent = remember
                flash(f"歡迎 {username}", "success")
                return redirect(url_for('home'))
            else:
                flash("帳號或密碼錯誤", "danger")
        except Exception as e:
            flash("伺服器錯誤: " + str(e), "danger")
        finally:
            conn.close()
    return render_template("login.html")

@app.route('/logout')
def logout():
    session.pop('user', None)
    flash("已登出", "info")
    return redirect(url_for('login'))

@app.route('/')
@login_required
def home():
    return render_template("index.html")

# 使用者管理
@app.route('/manage_users')
@admin_required
def manage_users():
    conn = get_db()
    try:
        users = conn.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
        return render_template("manage_users.html", users=users)
    except Exception as e:
        flash("读取用户数据失败: " + str(e), "danger")
        return redirect(url_for('home'))
    finally:
        conn.close()

# 新增使用者路由
@app.route('/add_user', methods=['GET','POST'])
@admin_required
def add_user():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        role = request.form.get('role','user').strip()
        can_modify_customers = int(request.form.get('can_modify_customers', 0))
        can_edit_repairs = int(request.form.get('can_edit_repairs', 0))
        can_add_repairs = int(request.form.get('can_add_repairs', 0))
        can_delete_repairs = int(request.form.get('can_delete_repairs', 0))
        line_id = request.form.get('line_id','').strip()  # 新增 LINE ID 欄位
        avatar_filename = None
        if 'avatar' in request.files:
            file = request.files['avatar']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                avatar_filename = f"avatar_{timestamp}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], avatar_filename))
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs, avatar, line_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, generate_password_hash(password), role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs, avatar_filename, line_id)
            )
            conn.commit()
            flash("使用者新增成功", "success")
            return redirect(url_for('manage_users'))
        except sqlite3.IntegrityError as e:
            flash("新增失败：帳號可能已存在", "danger")
        except Exception as e:
            flash("伺服器錯誤: " + str(e), "danger")
            conn.rollback()
        finally:
            conn.close()
    return render_template("add_user.html")

# 編輯使用者路由
@app.route('/edit_user/<int:id>', methods=['GET','POST'])
@admin_required
def edit_user(id):
    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (id,)).fetchone()
        if not user:
            flash("找不到該使用者", "danger")
            return redirect(url_for('manage_users'))
        if request.method == 'POST':
            username = request.form.get('username','').strip()
            password = request.form.get('password','').strip()
            role = request.form.get('role','user').strip()
            can_modify_customers = int(request.form.get('can_modify_customers', 0))
            can_edit_repairs = int(request.form.get('can_edit_repairs', 0))
            can_add_repairs = int(request.form.get('can_add_repairs', 0))
            can_delete_repairs = int(request.form.get('can_delete_repairs', 0))
            line_id = request.form.get('line_id','').strip()  # 讀取 LINE ID 欄位
            avatar_filename = user['avatar']
            if 'avatar' in request.files:
                file = request.files['avatar']
                if file and allowed_file(file.filename):
                    if avatar_filename:
                        old_avatar_path = os.path.join(app.config['UPLOAD_FOLDER'], avatar_filename)
                        if os.path.exists(old_avatar_path):
                            os.remove(old_avatar_path)
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    avatar_filename = f"avatar_{timestamp}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], avatar_filename))
            if password:
                conn.execute(
                    "UPDATE users SET username=?, password=?, role=?, can_modify_customers=?, can_edit_repairs=?, can_add_repairs=?, can_delete_repairs=?, avatar=?, line_id=? WHERE id=?",
                    (username, generate_password_hash(password), role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs, avatar_filename, line_id, id)
                )
            else:
                conn.execute(
                    "UPDATE users SET username=?, role=?, can_modify_customers=?, can_edit_repairs=?, can_add_repairs=?, can_delete_repairs=?, avatar=?, line_id=? WHERE id=?",
                    (username, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs, avatar_filename, line_id, id)
                )
            conn.commit()
            flash("使用者資料更新成功", "success")
            return redirect(url_for('manage_users'))
        return render_template("edit_user.html", user=user)
    except Exception as e:
        flash("伺服器錯誤: " + str(e), "danger")
        return redirect(url_for('manage_users'))
    finally:
        conn.close()

@app.route('/delete_user/<int:id>', methods=['POST'])
@admin_required
def delete_user(id):
    if session.get('user', {}).get('id') == id:
        flash("不允許刪除自己", "danger")
        return redirect(url_for('manage_users'))
    conn = get_db()
    try:
        conn.execute("DELETE FROM users WHERE id=?", (id,))
        conn.commit()
        flash("使用者已刪除", "success")
    except Exception as e:
        flash("刪除失敗: " + str(e), "danger")
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('manage_users'))

# 客戶管理
@app.route('/customers')
@login_required
def customers():
    query = request.args.get('q', '').strip()
    conn = get_db()
    try:
        if query:
            customers_data = conn.execute('''
                SELECT *,
                (SELECT COUNT(*) FROM repairs WHERE customer_id = customers.id) AS repair_count
                FROM customers
                WHERE name LIKE ?
                ORDER BY created_at DESC
            ''', ('%' + query + '%',)).fetchall()
        else:
            customers_data = conn.execute('''
                SELECT *,
                (SELECT COUNT(*) FROM repairs WHERE customer_id = customers.id) AS repair_count
                FROM customers
                ORDER BY created_at DESC
            ''').fetchall()
        return render_template("customers.html", customers=customers_data, query=query)
    except Exception as e:
        flash("读取数据失败: " + str(e), "danger")
        return redirect(url_for('home'))
    finally:
        conn.close()

@app.route('/add_customer', methods=['GET','POST'])
@login_required
def add_customer():
    if request.method == 'POST':
        data = {
            'name': request.form.get('name','').strip(),
            'camera_remote_url': request.form.get('camera_remote_url','').strip(),
            'internal_ip': request.form.get('internal_ip','').strip(),
            'communication_port': request.form.get('communication_port','').strip(),
            'monitoring_credentials': request.form.get('monitoring_credentials','').strip(),
            'router_password': request.form.get('router_password','').strip(),
            'google_map_url': request.form.get('google_map_url','').strip()
        }
        floor_plan = None
        if 'floor_plan' in request.files:
            file = request.files['floor_plan']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                floor_plan = f"floor_{timestamp}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], floor_plan))
        conn = get_db()
        try:
            conn.execute('''
                INSERT INTO customers (name, camera_remote_url, internal_ip, communication_port, monitoring_credentials, router_password, google_map_url, floor_plan)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (data['name'], data['camera_remote_url'], data['internal_ip'], data['communication_port'], data['monitoring_credentials'], data['router_password'], data['google_map_url'], floor_plan))
            conn.commit()
            flash("客户新增成功", "success")
            return redirect(url_for('customers'))
        except sqlite3.IntegrityError as e:
            flash("新增失败: 数据验证失败", "danger")
        except Exception as e:
            flash("服务器错误: " + str(e), "danger")
            conn.rollback()
        finally:
            conn.close()
    return render_template("add_customer.html")

@app.route('/edit_customer/<int:id>', methods=['GET','POST'])
@login_required
@require_permission('can_modify_customers')
def edit_customer(id):
    conn = get_db()
    try:
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (id,)).fetchone()
        if not customer:
            flash("找不到该客户", "danger")
            return redirect(url_for('customers'))
        if request.method == 'POST':
            # 取得多個攝影機遠端網址，將非空的網址以換行符號合併
            camera_urls = request.form.getlist('camera_remote_url[]')
            camera_remote_url = "\n".join(url.strip() for url in camera_urls if url.strip() != "")
            # 取得多個內部IP，將非空的 IP 以換行符號合併
            ip_list = request.form.getlist('internal_ip[]')
            internal_ip = "\n".join(ip.strip() for ip in ip_list if ip.strip() != "")
            data = {
                'name': request.form.get('name','').strip(),
                'camera_remote_url': camera_remote_url,
                'internal_ip': internal_ip,
                'communication_port': request.form.get('communication_port','').strip(),
                'monitoring_credentials': request.form.get('monitoring_credentials','').strip(),
                'router_password': request.form.get('router_password','').strip(),
                'google_map_url': request.form.get('google_map_url','').strip()
            }
            # 新增讀取 LINE ID 欄位
            line_id = request.form.get('line_id','').strip()
            floor_plan = customer['floor_plan']
            delete_floor = request.form.get('delete_floor') == 'on'
            if delete_floor and floor_plan:
                fp_path = os.path.join(app.config['UPLOAD_FOLDER'], floor_plan)
                if os.path.exists(fp_path):
                    os.remove(fp_path)
                floor_plan = None
            if 'floor_plan' in request.files:
                file = request.files['floor_plan']
                if file and allowed_file(file.filename):
                    if floor_plan:
                        old_fp = os.path.join(app.config['UPLOAD_FOLDER'], floor_plan)
                        if os.path.exists(old_fp):
                            os.remove(old_fp)
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    floor_plan = f"floor_{timestamp}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], floor_plan))
            try:
                conn.execute('''
                    UPDATE customers SET
                        name = ?,
                        camera_remote_url = ?,
                        internal_ip = ?,
                        communication_port = ?,
                        monitoring_credentials = ?,
                        router_password = ?,
                        google_map_url = ?,
                        floor_plan = ?
                    WHERE id = ?
                ''', (data['name'], data['camera_remote_url'], data['internal_ip'], data['communication_port'],
                      data['monitoring_credentials'], data['router_password'], data['google_map_url'], floor_plan, id))
                conn.commit()
                flash("客户资料更新成功", "success")
                return redirect(url_for('customers'))
            except sqlite3.IntegrityError as e:
                flash("数据验证失败，请检查输入格式", "danger")
        return render_template("edit_customer.html", customer=customer)
    except Exception as e:
        flash("服务器错误: " + str(e), "danger")
        return redirect(url_for('customers'))
    finally:
        conn.close()

@app.route('/delete_customer/<int:id>', methods=['POST'])
@admin_required
def delete_customer(id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM customers WHERE id=?", (id,))
        conn.commit()
        flash("客户删除成功", "success")
    except Exception as e:
        flash("删除失败: " + str(e), "danger")
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('customers'))

# 報修相關
@app.route('/view_repairs/<int:customer_id>')
@login_required
def view_repairs(customer_id):
    conn = get_db()
    try:
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            flash("找不到客户资料", "danger")
            return redirect(url_for('customers'))
        q = request.args.get('q', '').strip()
        date_search = request.args.get('date', '').strip()
        conditions = ["customer_id = ?"]
        params = [customer_id]
        if q:
            conditions.append("description LIKE ?")
            params.append("%" + q + "%")
        if date_search:
            conditions.append("date(created_at) = ?")
            params.append(date_search)
        sql = "SELECT * FROM repairs WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC"
        repairs = conn.execute(sql, params).fetchall()
        ongoing = []
        completed = []
        total_price = 0.0
        for r in repairs:
            r_dict = dict(r)
            if r_dict.get("price") is not None:
                try:
                    total_price += float(r_dict["price"])
                except:
                    pass
            photos = conn.execute("SELECT * FROM repair_photos WHERE repair_id=?", (r['id'],)).fetchall()
            r_dict['photos'] = [dict(p) for p in photos]
            if r_dict.get("status") == "已完成":
                completed.append(r_dict)
            else:
                ongoing.append(r_dict)
        return render_template("view_repairs.html", customer=customer, ongoing=ongoing, completed=completed, q=q, date_search=date_search, total_price=total_price)
    except Exception as e:
        flash("读取数据失败: " + str(e), "danger")
        return redirect(url_for('customers'))
    finally:
        conn.close()

@app.route('/add_repair/<int:customer_id>', methods=['GET','POST'])
@login_required
@require_permission('can_add_repairs')
def add_repair(customer_id):
    if request.method == 'POST':
        data = {
            'customer_id': customer_id,
            'description': request.form.get('description','').strip(),
            'status': request.form.get('status','待處理')
        }
        price_str = request.form.get('price','').strip()
        try:
            price = float(price_str) if price_str else None
        except ValueError:
            price = None
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO repairs (customer_id, description, status, price)
                VALUES (?, ?, ?, ?)
            ''', (data['customer_id'], data['description'], data['status'], price))
            repair_id = cur.lastrowid
            photos = request.files.getlist('photos')
            for file in photos:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    photo_filename = f"{customer_id}_{timestamp}_{filename}"
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
                    try:
                        from PIL import Image, ImageOps
                        import io
                        file.seek(0)
                        image = Image.open(file)
                        image = ImageOps.exif_transpose(image)
                        if image.mode in ("RGBA", "P"):
                            image = image.convert("RGB")
                        output = io.BytesIO()
                        quality = 85
                        image.save(output, format="JPEG", quality=quality, optimize=True)
                        while output.tell() > 500 * 1024 and quality > 10:
                            quality -= 5
                            output.seek(0)
                            output.truncate()
                            image.save(output, format="JPEG", quality=quality, optimize=True)
                        with open(filepath, "wb") as f_out:
                            f_out.write(output.getvalue())
                    except Exception as e:
                        file.seek(0)
                        file.save(filepath)
                    conn.execute('''
                        INSERT INTO repair_photos (repair_id, photo_path)
                        VALUES (?, ?)
                    ''', (repair_id, photo_filename))
            conn.commit()
            flash("報修記錄新增成功", "success")
            return redirect(url_for('view_repairs', customer_id=customer_id))
        except sqlite3.IntegrityError as e:
            flash("數據驗證失敗，請檢查輸入內容", "danger")
        except Exception as e:
            flash("伺服器錯誤: " + str(e), "danger")
            conn.rollback()
        finally:
            conn.close()
    return render_template("add_repair.html", customer_id=customer_id)

@app.route('/edit_repair/<int:repair_id>', methods=['GET','POST'])
@login_required
@require_permission('can_edit_repairs')
def edit_repair(repair_id):
    conn = get_db()
    try:
        repair = conn.execute('''
            SELECT r.*, c.name, c.id AS customer_id
            FROM repairs r
            JOIN customers c ON r.customer_id = c.id
            WHERE r.id=?
        ''', (repair_id,)).fetchone()
        if not repair:
            flash("找不到報修記錄", "danger")
            return redirect(url_for('home'))
        customer = {'name': repair['name'], 'id': repair['customer_id']}
        photos = conn.execute("SELECT * FROM repair_photos WHERE repair_id=?", (repair_id,)).fetchall()
        repair_photos = [dict(p) for p in photos]
        if request.method == 'POST':
            data = {
                'description': request.form.get('description','').strip(),
                'status': request.form.get('status', repair['status'])
            }
            price_str = request.form.get('price','').strip().replace(',', '')
            try:
                price = float(price_str) if price_str else None
            except ValueError:
                price = None
            for photo in repair_photos:
                if request.form.get(f'delete_photo_{photo["id"]}') == 'on':
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], photo['photo_path'])
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    conn.execute("DELETE FROM repair_photos WHERE id=?", (photo['id'],))
            new_photos = request.files.getlist('photos')
            for file in new_photos:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    photo_filename = f"{repair['customer_id']}_{timestamp}_{filename}"
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
                    try:
                        from PIL import Image, ImageOps
                        import io
                        file.seek(0)
                        image = Image.open(file)
                        image = ImageOps.exif_transpose(image)
                        if image.mode in ("RGBA", "P"):
                            image = image.convert("RGB")
                        output = io.BytesIO()
                        quality = 85
                        image.save(output, format="JPEG", quality=quality, optimize=True)
                        while output.tell() > 500 * 1024 and quality > 10:
                            quality -= 5
                            output.seek(0)
                            output.truncate()
                            image.save(output, format="JPEG", quality=quality, optimize=True)
                        with open(filepath, "wb") as f_out:
                            f_out.write(output.getvalue())
                    except Exception as e:
                        file.seek(0)
                        file.save(filepath)
                    conn.execute('''
                        INSERT INTO repair_photos (repair_id, photo_path)
                        VALUES (?, ?)
                    ''', (repair_id, photo_filename))
            edited_by = session.get('user', {}).get('username', 'unknown')
            conn.execute('''
                UPDATE repairs SET
                    description = ?,
                    status = ?,
                    price = ?,
                    edited_by = ?
                WHERE id = ?
            ''', (data['description'], data['status'], price, edited_by, repair_id))
            conn.commit()
            flash("報修記錄更新成功", "success")
            return redirect(url_for('view_repairs', customer_id=repair['customer_id']))
        return render_template("edit_repair.html", repair=repair, customer=customer, photos=repair_photos)
    except Exception as e:
        flash("伺服器錯誤: " + str(e), "danger")
        conn.rollback()
        return redirect(url_for('view_repairs', customer_id=repair['customer_id'] if repair else 0))
    finally:
        conn.close()

@app.route('/delete_repair/<int:repair_id>', methods=['POST'])
@admin_required
def delete_repair(repair_id):
    conn = get_db()
    try:
        repair = conn.execute("SELECT * FROM repairs WHERE id=?", (repair_id,)).fetchone()
        if repair:
            photos = conn.execute("SELECT * FROM repair_photos WHERE repair_id=?", (repair_id,)).fetchall()
            for p in photos:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], p['photo_path'])
                if os.path.exists(file_path):
                    os.remove(file_path)
            conn.execute("DELETE FROM repairs WHERE id=?", (repair_id,))
            conn.commit()
            flash("报修记录删除成功", "success")
        else:
            flash("找不到该记录", "danger")
    except Exception as e:
        flash("删除失败: " + str(e), "danger")
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('view_repairs', customer_id=repair['customer_id'] if repair else 0))

# 請款流程相關
@app.route('/billing/delete/<int:id>', methods=['POST'])
@login_required
def billing_delete(id):
    conn = get_db()
    try:
        quotations = conn.execute("SELECT * FROM billing_quotations WHERE billing_id=?", (id,)).fetchall()
        for q in quotations:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], q['file_path'])
            if os.path.exists(file_path):
                os.remove(file_path)
        conn.execute("DELETE FROM billing_quotations WHERE billing_id=?", (id,))
        conn.execute("DELETE FROM billing WHERE id=?", (id,))
        conn.commit()
        flash("請款記錄已刪除", "success")
    except Exception as e:
        flash("刪除請款記錄失敗: " + str(e), "danger")
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('billing'))

@app.route('/billing/next/<int:id>', methods=['POST'])
@login_required
def billing_next(id):
    conn = get_db()
    try:
        record = conn.execute("SELECT current_step FROM billing WHERE id=?", (id,)).fetchone()
        if not record:
            flash("找不到請款記錄", "danger")
            return redirect(url_for('billing'))
        current_step = record['current_step']
        if current_step < 5:
            current_step += 1
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE billing SET current_step=?, last_updated=? WHERE id=?", (current_step, now_str, id))
            conn.commit()
            flash("已進入下一步", "success")
        else:
            flash("已經是最後一步", "info")
    except Exception as e:
        flash("更新請款流程失敗: " + str(e), "danger")
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('billing'))

@app.route('/billing', methods=['GET'])
@login_required
def billing():
    conn = get_db()
    try:
        billing_records = conn.execute('''
            SELECT b.*, c.name as customer_name
            FROM billing b
            JOIN customers c ON b.customer_id = c.id
            ORDER BY c.name
        ''').fetchall()
        billing_data = []
        for record in billing_records:
            rec = dict(record)
            quotations = conn.execute("SELECT * FROM billing_quotations WHERE billing_id=?", (record['id'],)).fetchall()
            rec['quotations'] = [dict(q) for q in quotations]
            billing_data.append(rec)
        ongoing = [r for r in billing_data if r['current_step'] < 5]
        completed = [r for r in billing_data if r['current_step'] == 5]
        return render_template("billing.html", ongoing=ongoing, completed=completed)
    except Exception as e:
        flash("請款流程讀取失敗: " + str(e), "danger")
        return redirect(url_for('home'))
    finally:
        conn.close()

@app.route('/billing/edit/<int:id>', methods=['GET','POST'])
@login_required
def edit_billing(id):
    conn = get_db()
    try:
        record = conn.execute('''
            SELECT b.*, c.name as customer_name
            FROM billing b
            JOIN customers c ON b.customer_id = c.id
            WHERE b.id=?
        ''', (id,)).fetchone()
        if not record:
            flash("找不到請款記錄", "danger")
            return redirect(url_for('billing'))
        if request.method=='POST':
            unpaid_amount_str = request.form.get('unpaid_amount','').strip()
            try:
                unpaid_amount = float(unpaid_amount_str) if unpaid_amount_str else 0.0
            except ValueError:
                unpaid_amount = 0.0
            current_step = int(request.form.get('current_step', 0))
            reminder_date = request.form.get('reminder_date','').strip()
            new_files = request.files.getlist('quotations')
            for file in new_files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    file_path = f"quotation_{record['customer_id']}_{timestamp}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], file_path))
                    conn.execute('''
                        INSERT INTO billing_quotations (billing_id, file_path)
                        VALUES (?, ?)
                    ''', (id, file_path))
            existing_quotations = conn.execute("SELECT * FROM billing_quotations WHERE billing_id=?", (id,)).fetchall()
            for q in existing_quotations:
                if request.form.get(f'delete_quotation_{q["id"]}') == 'on':
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], q['file_path'])
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    conn.execute("DELETE FROM billing_quotations WHERE id=?", (q['id'],))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            last_editor = session.get('user', {}).get('username', 'unknown')
            conn.execute("UPDATE billing SET unpaid_amount=?, current_step=?, last_updated=?, last_editor=?, reminder_date=? WHERE id=?",
                         (unpaid_amount, current_step, now_str, last_editor, reminder_date if reminder_date else None, id))
            conn.commit()
            flash("請款記錄更新成功", "success")
            return redirect(url_for('billing'))
        quotations = conn.execute("SELECT * FROM billing_quotations WHERE billing_id=?", (id,)).fetchall()
        record = dict(record)
        record['quotations'] = [dict(q) for q in quotations]
        return render_template("billing_edit.html", record=record)
    except Exception as e:
        flash("更新請款記錄失敗: " + str(e), "danger")
        return redirect(url_for('billing'))
    finally:
        conn.close()

@app.route('/billing/add', methods=['GET','POST'])
@login_required
def add_billing():
    conn = get_db()
    try:
        available_customers = conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
        if request.method=='POST':
            customer_id = request.form.get('customer_id')
            if not customer_id:
                flash("請選擇客戶", "danger")
                return redirect(url_for('add_billing'))
            reminder_date = request.form.get('reminder_date','').strip()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.cursor()
            cur.execute("INSERT INTO billing (customer_id, unpaid_amount, current_step, last_updated, reminder_date) VALUES (?, ?, ?, ?, ?)",
                        (customer_id, 0.0, 0, now_str, reminder_date if reminder_date else None))
            billing_id = cur.lastrowid
            files = request.files.getlist('quotations')
            for file in files:
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    file_path = f"quotation_{customer_id}_{timestamp}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], file_path))
                    conn.execute('''
                        INSERT INTO billing_quotations (billing_id, file_path)
                        VALUES (?, ?)
                    ''', (billing_id, file_path))
            conn.commit()
            flash("請款記錄新增成功", "success")
            return redirect(url_for('billing'))
        return render_template("billing_add.html", available_customers=available_customers)
    except Exception as e:
        flash("新增請款記錄失敗: " + str(e), "danger")
        return redirect(url_for('billing'))
    finally:
        conn.close()

# 維修追蹤
@app.route('/tracking')
@login_required
def tracking():
    conn = get_db()
    try:
        q = request.args.get('q', '').strip()
        date_search = request.args.get('date', '').strip()
        conditions = ["r.status = '待處理'"]
        params = []
        if q:
            conditions.append("r.description LIKE ?")
            params.append("%" + q + "%")
        if date_search:
            conditions.append("date(r.created_at) = ?")
            params.append(date_search)
        condition_sql = " AND ".join(conditions)
        sql = f'''
            SELECT r.*, c.name as customer_name
            FROM repairs r
            JOIN customers c ON r.customer_id = c.id
            WHERE {condition_sql}
            ORDER BY r.created_at ASC
        '''
        repairs = conn.execute(sql, params).fetchall()
        repairs_data = []
        for r in repairs:
            r_dict = dict(r)
            photos = conn.execute("SELECT * FROM repair_photos WHERE repair_id=?", (r['id'],)).fetchall()
            r_dict['photos'] = [dict(p) for p in photos]
            repairs_data.append(r_dict)
        return render_template("tracking.html", repairs=repairs_data, q=q, date_search=date_search)
    except Exception as e:
        flash("維修追蹤讀取失敗: " + str(e), "danger")
        return redirect(url_for('home'))
    finally:
        conn.close()

# 新增選擇客戶以新增報修的功能
@app.route('/select_customer_for_repair', methods=['GET','POST'], endpoint='select_customer_for_repair')
@login_required
def select_customer_for_repair_route():
    conn = get_db()
    try:
        customers = conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
        if request.method == 'POST':
            customer_id = request.form.get('customer_id')
            if customer_id:
                return redirect(url_for('add_repair', customer_id=customer_id))
            else:
                flash("請選擇客戶", "danger")
        return render_template("select_customer_for_repair.html", customers=customers)
    except Exception as e:
        flash("錯誤: " + str(e), "danger")
        return redirect(url_for('tracking'))
    finally:
        conn.close()

# 工程進度管理
@app.route('/progress/add', methods=['GET','POST'])
@login_required
def add_progress():
    conn = get_db()
    try:
        customers_list = conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
        if request.method == 'POST':
            customer_id = request.form.get('customer_id')
            if not customer_id:
                flash("請選擇客戶", "danger")
                return redirect(url_for('add_progress'))
            quotation_confirm_file = None
            if 'quotation_confirm_file' in request.files:
                file = request.files['quotation_confirm_file']
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    quotation_confirm_file = f"quotation_confirm_{customer_id}_{timestamp}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], quotation_confirm_file))
            quotation_confirm_date = request.form.get('quotation_confirm_date')
            start_construction_date = request.form.get('start_construction_date')
            completed_date = request.form.get('completed_date')
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute('''
                INSERT INTO progress (customer_id, quotation_confirm_file, quotation_confirm_date, start_construction_date, completed_date, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (customer_id, quotation_confirm_file, quotation_confirm_date, start_construction_date, completed_date, now_str))
            conn.commit()
            flash("工程進度記錄新增成功", "success")
            return redirect(url_for('progress'))
        return render_template("progress_add.html", customers=customers_list)
    except Exception as e:
        flash("新增工程進度失敗: " + str(e), "danger")
        return redirect(url_for('progress'))
    finally:
        conn.close()

@app.route('/progress', methods=['GET'])
@login_required
def progress():
    conn = get_db()
    try:
        progress_records = conn.execute('''
            SELECT p.*, c.name as customer_name
            FROM progress p
            JOIN customers c ON p.customer_id = c.id
            ORDER BY c.name
        ''').fetchall()
        return render_template("progress.html", progress_records=progress_records)
    except Exception as e:
        flash("工程進度讀取失敗: " + str(e), "danger")
        return redirect(url_for('home'))
    finally:
        conn.close()

@app.route('/progress/edit/<int:id>', methods=['GET','POST'])
@login_required
def edit_progress(id):
    conn = get_db()
    try:
        record = conn.execute('''
            SELECT p.*, c.name as customer_name
            FROM progress p
            JOIN customers c ON p.customer_id = c.id
            WHERE p.id=?
        ''', (id,)).fetchone()
        if not record:
            flash("找不到工程進度記錄", "danger")
            return redirect(url_for('progress'))
        if request.method == 'POST':
            quotation_confirm_file = record['quotation_confirm_file']
            if 'quotation_confirm_file' in request.files:
                file = request.files['quotation_confirm_file']
                if file and allowed_file(file.filename):
                    if quotation_confirm_file:
                        old_path = os.path.join(app.config['UPLOAD_FOLDER'], quotation_confirm_file)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    filename = secure_filename(file.filename)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    quotation_confirm_file = f"quotation_confirm_{record['customer_id']}_{timestamp}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], quotation_confirm_file))
            quotation_confirm_date = request.form.get('quotation_confirm_date')
            start_construction_date = request.form.get('start_construction_date')
            completed_date = request.form.get('completed_date')
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute('''
                UPDATE progress SET
                    quotation_confirm_file = ?,
                    quotation_confirm_date = ?,
                    start_construction_date = ?,
                    completed_date = ?,
                    last_updated = ?
                WHERE id = ?
            ''', (quotation_confirm_file, quotation_confirm_date, start_construction_date, completed_date, now_str, id))
            conn.commit()
            flash("工程進度更新成功", "success")
            return redirect(url_for('progress'))
        return render_template("progress_edit.html", record=record)
    except Exception as e:
        flash("更新工程進度失敗: " + str(e), "danger")
        return redirect(url_for('progress'))
    finally:
        conn.close()

@app.route('/progress/delete/<int:id>', methods=['POST'])
@login_required
def delete_progress(id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM progress WHERE id=?", (id,))
        conn.commit()
        flash("工程進度記錄已刪除", "success")
    except Exception as e:
        flash("刪除工程進度記錄失敗: " + str(e), "danger")
        conn.rollback()
    finally:
        conn.close()
    return redirect(url_for('progress'))

# 匯出客戶資料至 Excel
@app.route('/export_customers')
@login_required
def export_customers():
    conn = get_db()
    try:
        customers = conn.execute("SELECT * FROM customers ORDER BY id ASC").fetchall()
        data = [dict(row) for row in customers]
        df = pd.DataFrame(data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Customers')
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name="customers.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        flash("匯出 Excel 失敗: " + str(e), "danger")
        return redirect(url_for('customers'))
    finally:
        conn.close()

# 從 Excel 匯入客戶資料
@app.route('/import_customers', methods=['GET','POST'])
@login_required
def import_customers():
    if request.method == 'POST':
        if 'excel_file' not in request.files:
            flash("沒有上傳檔案", "danger")
            return redirect(request.url)
        file = request.files['excel_file']
        if file.filename == "":
            flash("沒有選擇檔案", "danger")
            return redirect(request.url)
        if file and file.filename.lower().endswith(('.xlsx', '.xls')):
            try:
                df = pd.read_excel(file)
                conn = get_db()
                imported_count = 0
                for _, row in df.iterrows():
                    name = row.get('name')
                    if pd.isnull(name) or str(name).strip() == '':
                        continue
                    conn.execute('''
                        INSERT INTO customers (name, camera_remote_url, internal_ip, communication_port, monitoring_credentials, router_password, google_map_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        name,
                        row.get('camera_remote_url', ''),
                        row.get('internal_ip', ''),
                        row.get('communication_port', ''),
                        row.get('monitoring_credentials', ''),
                        row.get('router_password', ''),
                        row.get('google_map_url', '')
                    ))
                    imported_count += 1
                conn.commit()
                flash(f"客戶資料匯入成功，共匯入 {imported_count} 筆資料", "success")
            except Exception as e:
                flash("匯入失敗: " + str(e), "danger")
            finally:
                conn.close()
            return redirect(url_for('customers'))
        else:
            flash("請上傳 Excel 檔案", "danger")
            return redirect(request.url)
    return render_template("import_customers.html")

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ------------------ LINE 通知功能 ------------------
# 使用 LINE Messaging API 推送提醒訊息
LINE_CHANNEL_ACCESS_TOKEN = "V1uumlus5APbb+bkez+DOWhJVcatlH0dQY7OuqSo1OAWOSlv5UO/nmZGj6yBlf7gjb6XoMNxdOAywedyAEU1liNPAaXPPXIoPfLt/tIAuLUDEHoq5CTrltEcCz8gCml7ia+yWm83NmZXOr+AMkBvAgdB04t89/1O/w1cDnyilFU="
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"

def send_line_message(message, to):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": to,
        "messages": [
            {
                "type": "text",
                "text": message
            }
        ]
    }
    response = requests.post(LINE_PUSH_API, headers=headers, json=payload)
    return response.status_code, response.text

# Context processor 將提醒資料注入所有模板，同時自動推送尚未通知的提醒
@app.context_processor
def inject_notifications():
    conn = get_db()
    try:
        # 取得尚未推送 LINE 通知的提醒記錄
        reminders = conn.execute("""
            SELECT b.*, c.name as customer_name 
            FROM billing b
            JOIN customers c ON b.customer_id = c.id
            WHERE b.reminder_date IS NOT NULL 
              AND DATE(b.reminder_date) <= DATE('now','localtime')
              AND b.notified = 0
            ORDER BY b.reminder_date ASC
        """).fetchall()
        # 取得所有有設定 LINE ID 的使用者 (從 users 表中)
        recipient_rows = conn.execute("""
            SELECT line_id FROM users WHERE line_id IS NOT NULL AND TRIM(line_id) <> ''
        """).fetchall()
        recipients = [row["line_id"] for row in recipient_rows]
        for reminder in reminders:
            message = f"請款提醒：{reminder['customer_name']} 的請款提醒日期已到 ({reminder['reminder_date']})，請確認請款流程！"
            for recipient in recipients:
                status, resp_text = send_line_message(message, to=recipient)
                print("LINE 推送狀態給", recipient, ":", status, resp_text)
            conn.execute("UPDATE billing SET notified = 1 WHERE id = ?", (reminder['id'],))
        conn.commit()
        # 再查詢所有提醒資料（不論已通知與否）供前端顯示
        reminders = conn.execute("""
            SELECT b.*, c.name as customer_name 
            FROM billing b
            JOIN customers c ON b.customer_id = c.id
            WHERE b.reminder_date IS NOT NULL 
              AND DATE(b.reminder_date) <= DATE('now','localtime')
            ORDER BY b.reminder_date ASC
        """).fetchall()
    except Exception as e:
        reminders = []
    finally:
        conn.close()
    return dict(notifications=reminders)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5005)

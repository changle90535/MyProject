from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, session
from werkzeug.utils import secure_filename
import sqlite3
import os
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.urandom(24),
    UPLOAD_FOLDER=os.path.join(os.path.dirname(__file__), 'uploads'),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # 5MB
    ALLOWED_EXTENSIONS={'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}
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
                address TEXT,
                phone TEXT,
                email TEXT,
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
                FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                role TEXT NOT NULL,
                can_modify_customers INTEGER DEFAULT 0,
                can_edit_repairs INTEGER DEFAULT 0,
                can_add_repairs INTEGER DEFAULT 0,
                can_delete_repairs INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS billing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                unpaid_amount REAL DEFAULT 0.0,
                current_step INTEGER DEFAULT 0,
                quotation_path TEXT,
                last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
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
                         ('admin', 'admin', 'admin', 1, 1, 1, 1))
            conn.execute("INSERT INTO users (username, password, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ('user', 'user', 'user', 0, 0, 0, 0))
            conn.commit()
    except sqlite3.Error as e:
        print("Database initialization failed:", str(e))
    finally:
        conn.close()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

init_db()

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

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        conn = get_db()
        try:
            user = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password)).fetchone()
            if user:
                session['user'] = {
                    'id': user['id'],
                    'username': user['username'],
                    'role': user['role'],
                    'can_modify_customers': user['can_modify_customers'],
                    'can_edit_repairs': user['can_edit_repairs'],
                    'can_add_repairs': user['can_add_repairs'],
                    'can_delete_repairs': user['can_delete_repairs']
                }
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
            'address': request.form.get('address','').strip(),
            'phone': request.form.get('phone','').replace(' ',''), 
            'email': request.form.get('email','').lower().strip(),
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
                INSERT INTO customers (name, address, phone, email, google_map_url, floor_plan)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (data['name'], data['address'], data['phone'], data['email'], data['google_map_url'], floor_plan))
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
            data = {
                'name': request.form.get('name','').strip(),
                'address': request.form.get('address','').strip(),
                'phone': request.form.get('phone','').replace(' ',''), 
                'email': request.form.get('email','').lower().strip(),
                'google_map_url': request.form.get('google_map_url','').strip()
            }
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
                        address = ?,
                        phone = ?,
                        email = ?,
                        google_map_url = ?,
                        floor_plan = ?
                    WHERE id = ?
                ''', (data['name'], data['address'], data['phone'], data['email'], data['google_map_url'], floor_plan, id))
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

@app.route('/view_repairs/<int:customer_id>')
@login_required
def view_repairs(customer_id):
    conn = get_db()
    try:
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        repairs = conn.execute('''
            SELECT * FROM repairs
            WHERE customer_id=?
            ORDER BY created_at DESC
        ''', (customer_id,)).fetchall()
        if not customer:
            flash("找不到客户资料", "danger")
            return redirect(url_for('customers'))
        return render_template("view_repairs.html", customer=customer, repairs=repairs)
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
        photo_path = None
        if 'photo' in request.files:
            file = request.files['photo']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                photo_path = f"{customer_id}_{timestamp}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], photo_path))
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO repairs (customer_id, description, status, price, photo_path)
                VALUES (?, ?, ?, ?, ?)
            ''', (data['customer_id'], data['description'], data['status'], price, photo_path))
            conn.commit()
            flash("报修记录新增成功", "success")
            return redirect(url_for('view_repairs', customer_id=customer_id))
        except sqlite3.IntegrityError as e:
            flash("数据验证失败，请检查输入内容", "danger")
        except Exception as e:
            flash("服务器错误: " + str(e), "danger")
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
            flash("找不到报修记录", "danger")
            return redirect(url_for('home'))
        customer = {'name': repair['name'], 'id': repair['customer_id']}
        if request.method == 'POST':
            data = {
                'description': request.form.get('description','').strip(),
                'status': request.form.get('status', repair['status'])
            }
            price_str = request.form.get('price','').strip()
            try:
                price = float(price_str) if price_str else None
            except ValueError:
                price = None
            photo_path = repair['photo_path']
            delete_photo = request.form.get('delete_photo') == 'on'
            if delete_photo and photo_path:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], photo_path)
                if os.path.exists(file_path):
                    os.remove(file_path)
                photo_path = None
            if 'photo' in request.files:
                file = request.files['photo']
                if file and allowed_file(file.filename):
                    if photo_path:
                        old_path = os.path.join(app.config['UPLOAD_FOLDER'], photo_path)
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    filename = secure_filename(file.filename)
                    photo_path = f"{repair['customer_id']}_{timestamp}_{filename}"
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], photo_path))
            try:
                conn.execute('''
                    UPDATE repairs SET
                        description = ?,
                        status = ?,
                        price = ?,
                        photo_path = ?
                    WHERE id = ?
                ''', (data['description'], data['status'], price, photo_path, repair_id))
                conn.commit()
                flash("报修记录更新成功", "success")
                return redirect(url_for('view_repairs', customer_id=repair['customer_id']))
            except sqlite3.IntegrityError as e:
                flash("数据验证失败，请检查输入内容", "danger")
        return render_template("edit_repair.html", repair=repair, customer=customer)
    except Exception as e:
        flash("服务器错误: " + str(e), "danger")
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
            if repair['photo_path']:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], repair['photo_path'])
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
        conn = get_db()
        try:
            conn.execute("INSERT INTO users (username, password, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (username, password, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs))
            conn.commit()
            flash("使用者新增成功", "success")
            return redirect(url_for('manage_users'))
        except sqlite3.IntegrityError as e:
            flash("新增失敗：帳號可能已存在", "danger")
        except Exception as e:
            flash("伺服器錯誤: " + str(e), "danger")
            conn.rollback()
        finally:
            conn.close()
    return render_template("add_user.html")

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
            if password:
                conn.execute("UPDATE users SET username=?, password=?, role=?, can_modify_customers=?, can_edit_repairs=?, can_add_repairs=?, can_delete_repairs=? WHERE id=?",
                             (username, password, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs, id))
            else:
                conn.execute("UPDATE users SET username=?, role=?, can_modify_customers=?, can_edit_repairs=?, can_add_repairs=?, can_delete_repairs=? WHERE id=?",
                             (username, role, can_modify_customers, can_edit_repairs, can_add_repairs, can_delete_repairs, id))
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
        return render_template("billing.html", billing_records=billing_records)
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
            quotation_file = request.files.get('quotation')
            quotation_path = record['quotation_path']
            if quotation_file and allowed_file(quotation_file.filename):
                if quotation_path:
                    old_path = os.path.join(app.config['UPLOAD_FOLDER'], quotation_path)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                filename = secure_filename(quotation_file.filename)
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                quotation_path = f"quotation_{record['customer_id']}_{timestamp}_{filename}"
                quotation_file.save(os.path.join(app.config['UPLOAD_FOLDER'], quotation_path))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE billing SET unpaid_amount=?, current_step=?, quotation_path=?, last_updated=? WHERE id=?",
                         (unpaid_amount, current_step, quotation_path, now_str, id))
            conn.commit()
            flash("請款流程資料更新成功", "success")
            return redirect(url_for('billing'))
        return render_template("billing_edit.html", record=record)
    except Exception as e:
        flash("更新請款流程失敗: " + str(e), "danger")
        return redirect(url_for('billing'))
    finally:
        conn.close()

@app.route('/billing/add', methods=['GET','POST'])
@login_required
def add_billing():
    conn = get_db()
    try:
        tracked = conn.execute("SELECT customer_id FROM billing").fetchall()
        tracked_ids = [row['customer_id'] for row in tracked]
        if tracked_ids:
            query = "SELECT id, name FROM customers WHERE id NOT IN ({seq}) ORDER BY name".format(seq=','.join(['?']*len(tracked_ids)))
            available_customers = conn.execute(query, tracked_ids).fetchall()
        else:
            available_customers = conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
        if request.method=='POST':
            customer_id = request.form.get('customer_id')
            if not customer_id:
                flash("請選擇客戶", "danger")
                return redirect(url_for('add_billing'))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO billing (customer_id, unpaid_amount, current_step, last_updated) VALUES (?, ?, ?, ?)",
                         (customer_id, 0.0, 0, now_str))
            conn.commit()
            flash("請款記錄新增成功", "success")
            return redirect(url_for('billing'))
        return render_template("billing_add.html", available_customers=available_customers)
    except Exception as e:
        flash("新增請款記錄失敗: " + str(e), "danger")
        return redirect(url_for('billing'))
    finally:
        conn.close()

@app.route('/tracking')
@login_required
def tracking():
    conn = get_db()
    try:
        repairs = conn.execute('''
            SELECT r.*, c.name as customer_name
            FROM repairs r
            JOIN customers c ON r.customer_id = c.id
            WHERE r.status = '待處理'
            ORDER BY r.created_at ASC
        ''').fetchall()
        return render_template("tracking.html", repairs=repairs)
    except Exception as e:
        flash("維修追蹤讀取失敗: " + str(e), "danger")
        return redirect(url_for('home'))
    finally:
        conn.close()

@app.route('/progress/add', methods=['GET','POST'], endpoint='progress_add')
@login_required
def add_progress():
    conn = get_db()
    try:
        customers_list = conn.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
        if request.method == 'POST':
            customer_id = request.form.get('customer_id')
            if not customer_id:
                flash("請選擇客戶", "danger")
                return redirect(url_for('progress_add'))
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

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5005)

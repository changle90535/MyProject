import sqlite3
import os
import re
from datetime import datetime

def get_connection():
    """
    取得資料庫連線，並設定 row_factory 與外鍵約束
    """
    conn = sqlite3.connect('customer.db')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def create_tables():
    """
    建立資料庫表結構，包含 customers（加入 floor_plan 欄位）、repairs（加入 price 欄位）與 users 表
    """
    conn = get_connection()
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
                role TEXT NOT NULL
            );
            
            CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_repairs_status ON repairs(status);
        ''')
        conn.commit()
    except sqlite3.Error as e:
        print(f"数据库初始化失败: {str(e)}")
        raise
    finally:
        conn.close()

def upgrade_tables():
    """
    檢查並升級現有資料表，若缺少欄位則進行 ALTER TABLE 升級
    """
    conn = get_connection()
    try:
        # 升級 customers 表：檢查是否有 floor_plan 欄位
        cursor = conn.execute("PRAGMA table_info(customers)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "floor_plan" not in columns:
            conn.execute("ALTER TABLE customers ADD COLUMN floor_plan TEXT")
            conn.commit()
            print("已新增 'floor_plan' 欄位到 customers 表。")
        
        # 升級 repairs 表：檢查是否有 price 欄位
        cursor = conn.execute("PRAGMA table_info(repairs)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "price" not in columns:
            conn.execute("ALTER TABLE repairs ADD COLUMN price REAL")
            conn.commit()
            print("已新增 'price' 欄位到 repairs 表。")
    except sqlite3.Error as e:
        print(f"升级表结构错误: {str(e)}")
    finally:
        conn.close()

def initialize_database():
    """
    完整的資料庫初始化流程：
      1. 建立資料表
      2. 升級缺少的欄位
      3. 建立預設管理者與一般使用者帳號（若 users 表為空）
    """
    create_tables()
    upgrade_tables()
    
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT COUNT(*) as cnt FROM users")
        count = cursor.fetchone()["cnt"]
        if count == 0:
            # 預設管理者：admin/admin，普通使用者：user/user
            conn.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", ("admin", "admin", "admin"))
            conn.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", ("user", "user", "user"))
            conn.commit()
            print("已建立預設帳號：admin/admin (管理者) 與 user/user (一般使用者)。")
    except sqlite3.Error as e:
        print(f"建立預設使用者錯誤: {str(e)}")
    finally:
        conn.close()

if __name__ == '__main__':
    initialize_database()

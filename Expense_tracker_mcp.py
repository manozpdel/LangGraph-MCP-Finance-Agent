import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP

# --------------------- LOGGING SETUP ---------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --------------------- INIT ---------------------

mcp = FastMCP("ExpenseTracker")
DB_FILE = os.getenv("EXPENSES_DB", "/tmp/expenses.db")


# --------------------- DATABASE SETUP ---------------------

def init_db():
    """Initialize SQLite database and create tables if not exists"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            date TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            user_id TEXT,
            month INTEGER,
            year INTEGER,
            amount REAL,
            PRIMARY KEY(user_id, month, year)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS recurring_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            day_of_month INTEGER NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")


def get_connection():
    """Create new database connection"""
    return sqlite3.connect(DB_FILE)


init_db()


# --------------------- INPUT VALIDATION ---------------------

def validate_amount(amount: float) -> Optional[str]:
    if amount <= 0:
        return "❌ Amount must be greater than zero"
    return None

def validate_category(category: str) -> Optional[str]:
    if not category.strip():
        return "❌ Category cannot be empty"
    return None

def validate_day_of_month(day: int) -> Optional[str]:
    if not (1 <= day <= 31):
        return "❌ day_of_month must be between 1 and 31"
    return None


# --------------------- MCP TOOLS ---------------------

# ---- 1. Add Expense ----

@mcp.tool()
def add_expense(user_id: str, amount: float, category: str, description: str = "") -> str:
    """Add a new expense for a user"""
    if err := validate_amount(amount):
        return err
    if err := validate_category(category):
        return err

    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now()

    cursor.execute("""
        INSERT INTO expenses (user_id, amount, category, description, date, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, amount, category.strip(), description, now.isoformat(), now.timestamp()))

    conn.commit()
    conn.close()

    logger.info(f"User '{user_id}' added expense ${amount:.2f} in category '{category}'")
    return f"✅ Expense added: ${amount:.2f} for {category}"


# ---- 2. Get Expenses ----

@mcp.tool()
def get_expenses(user_id: str, category: str = None, limit: int = 10) -> str:
    """Get recent expenses for a user, optionally filtered by category"""
    conn = get_connection()
    cursor = conn.cursor()

    if category:
        cursor.execute("""
            SELECT id, amount, category, description, date
            FROM expenses
            WHERE user_id = ? AND LOWER(category) = LOWER(?)
            ORDER BY timestamp DESC
            LIMIT ?
        """, (user_id, category, limit))
    else:
        cursor.execute("""
            SELECT id, amount, category, description, date
            FROM expenses
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (user_id, limit))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "No expenses found"

    result = f"📊 Recent Expenses for '{user_id}' ({len(rows)} shown):\n\n"
    for row in rows:
        exp_id, amount, cat, desc, date_str = row
        date = datetime.fromisoformat(date_str).strftime("%Y-%m-%d %H:%M")
        result += f"• [#{exp_id}] ${amount:.2f} - {cat} - {date}\n"
        if desc:
            result += f"  Description: {desc}\n"

    logger.info(f"User '{user_id}' fetched expenses (category={category}, limit={limit})")
    return result


# ---- 3. Get Total by Category ----

@mcp.tool()
def get_total_by_category(user_id: str) -> str:
    """Get total expenses grouped by category for a user"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT category, SUM(amount)
        FROM expenses
        WHERE user_id = ?
        GROUP BY category
        ORDER BY SUM(amount) DESC
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "No expenses recorded yet"

    result = f"💰 Total Expenses by Category for '{user_id}':\n\n"
    total = 0
    for category, amount in rows:
        result += f"• {category}: ${amount:.2f}\n"
        total += amount

    result += f"\n🔢 Grand Total: ${total:.2f}"
    logger.info(f"User '{user_id}' fetched category totals")
    return result


# ---- 4. Delete Expense ----

@mcp.tool()
def delete_expense(user_id: str, expense_id: int) -> str:
    """Delete an expense by ID (only if it belongs to the user)"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
    conn.commit()

    if cursor.rowcount > 0:
        conn.close()
        logger.info(f"User '{user_id}' deleted expense #{expense_id}")
        return f"✅ Expense #{expense_id} deleted successfully"
    else:
        conn.close()
        return f"❌ Expense #{expense_id} not found or doesn't belong to user '{user_id}'"


# ---- 5. Monthly Summary (SQL-level filtering) ----

@mcp.tool()
def get_monthly_summary(user_id: str, month: int = None, year: int = None) -> str:
    """Get expense summary for a specific month using SQL filtering"""
    now = datetime.now()
    month = month or now.month
    year = year or now.year

    month_str = str(month).zfill(2)
    year_str = str(year)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT amount, category
        FROM expenses
        WHERE user_id = ?
          AND strftime('%m', date) = ?
          AND strftime('%Y', date) = ?
    """, (user_id, month_str, year_str))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return f"No expenses found for {month}/{year}"

    total = sum(r[0] for r in rows)
    category_totals = {}
    for amount, category in rows:
        category_totals[category] = category_totals.get(category, 0) + amount

    result = f"📅 Summary for '{user_id}' — {month}/{year}:\n\n"
    result += f"Total Expenses: ${total:.2f}\n"
    result += f"Number of Transactions: {len(rows)}\n\n"
    result += "By Category:\n"
    for cat, amt in sorted(category_totals.items(), key=lambda x: x[1], reverse=True):
        pct = (amt / total) * 100
        result += f"• {cat}: ${amt:.2f} ({pct:.1f}%)\n"

    logger.info(f"User '{user_id}' fetched monthly summary for {month}/{year}")
    return result


# ---- 6. Update Expense ----

@mcp.tool()
def update_expense(user_id: str, expense_id: int, amount: float = None, category: str = None, description: str = None) -> str:
    """Update an existing expense's amount, category, or description"""
    if amount is not None:
        if err := validate_amount(amount):
            return err
    if category is not None:
        if err := validate_category(category):
            return err

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
    if not cursor.fetchone():
        conn.close()
        return f"❌ Expense #{expense_id} not found or doesn't belong to user '{user_id}'"

    if amount is not None:
        cursor.execute("UPDATE expenses SET amount = ? WHERE id = ?", (amount, expense_id))
    if category is not None:
        cursor.execute("UPDATE expenses SET category = ? WHERE id = ?", (category.strip(), expense_id))
    if description is not None:
        cursor.execute("UPDATE expenses SET description = ? WHERE id = ?", (description, expense_id))

    conn.commit()
    conn.close()

    logger.info(f"User '{user_id}' updated expense #{expense_id}")
    return f"✅ Expense #{expense_id} updated successfully"


# ---- 7. Budget System ----

@mcp.tool()
def set_budget(user_id: str, amount: float, month: int = None, year: int = None) -> str:
    """Set a monthly budget for a user"""
    if err := validate_amount(amount):
        return err

    now = datetime.now()
    month = month or now.month
    year = year or now.year

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO budgets (user_id, month, year, amount)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, month, year) DO UPDATE SET amount = excluded.amount
    """, (user_id, month, year, amount))

    conn.commit()
    conn.close()

    logger.info(f"User '{user_id}' set budget ${amount:.2f} for {month}/{year}")
    return f"✅ Budget set to ${amount:.2f} for {month}/{year}"


@mcp.tool()
def check_budget_status(user_id: str, month: int = None, year: int = None) -> str:
    """Check budget status: spent, remaining, and warning if over 80%"""
    now = datetime.now()
    month = month or now.month
    year = year or now.year

    month_str = str(month).zfill(2)
    year_str = str(year)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT amount FROM budgets
        WHERE user_id = ? AND month = ? AND year = ?
    """, (user_id, month, year))

    budget_row = cursor.fetchone()
    if not budget_row:
        conn.close()
        return f"❌ No budget set for '{user_id}' in {month}/{year}. Use set_budget to create one."

    budget = budget_row[0]

    cursor.execute("""
        SELECT SUM(amount) FROM expenses
        WHERE user_id = ?
          AND strftime('%m', date) = ?
          AND strftime('%Y', date) = ?
    """, (user_id, month_str, year_str))

    spent_row = cursor.fetchone()
    conn.close()

    spent = spent_row[0] or 0.0
    remaining = budget - spent
    pct_used = (spent / budget) * 100

    result = f"📊 Budget Status for '{user_id}' — {month}/{year}:\n\n"
    result += f"Budget:    ${budget:.2f}\n"
    result += f"Spent:     ${spent:.2f} ({pct_used:.1f}%)\n"
    result += f"Remaining: ${remaining:.2f}\n"

    if spent > budget:
        result += "\n🚨 WARNING: You have EXCEEDED your budget!"
    elif pct_used >= 80:
        result += "\n⚠️  WARNING: You have used over 80% of your budget!"
    else:
        result += "\n✅ You are within your budget."

    logger.info(f"User '{user_id}' checked budget status for {month}/{year}")
    return result


# ---- 8. Spending Trend (Last 6 Months) ----

@mcp.tool()
def get_spending_trend(user_id: str) -> str:
    """Get spending totals for the last 6 months for a user"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT strftime('%Y-%m', date) as month, SUM(amount)
        FROM expenses
        WHERE user_id = ?
        GROUP BY month
        ORDER BY month DESC
        LIMIT 6
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return f"No spending data found for '{user_id}'"

    result = f"📈 Spending Trend for '{user_id}' (Last 6 Months):\n\n"
    for month_label, total in reversed(rows):
        bar_len = int((total / max(r[1] for r in rows)) * 20)
        bar = "█" * bar_len
        result += f"{month_label}  {bar}  ${total:.2f}\n"

    logger.info(f"User '{user_id}' fetched spending trend")
    return result


# ---- 9. Recurring Expenses ----

@mcp.tool()
def add_recurring_expense(user_id: str, amount: float, category: str, day_of_month: int, description: str = "") -> str:
    """Register a recurring monthly expense for a user"""
    if err := validate_amount(amount):
        return err
    if err := validate_category(category):
        return err
    if err := validate_day_of_month(day_of_month):
        return err

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO recurring_expenses (user_id, amount, category, description, day_of_month)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, amount, category.strip(), description, day_of_month))

    conn.commit()
    conn.close()

    logger.info(f"User '{user_id}' added recurring expense ${amount:.2f} in '{category}' on day {day_of_month}")
    return f"✅ Recurring expense added: ${amount:.2f} for {category} on day {day_of_month} of each month"


@mcp.tool()
def get_recurring_expenses(user_id: str) -> str:
    """List all recurring expenses for a user"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, amount, category, description, day_of_month
        FROM recurring_expenses
        WHERE user_id = ?
        ORDER BY day_of_month
    """, (user_id,))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return f"No recurring expenses found for '{user_id}'"

    total = sum(r[1] for r in rows)
    result = f"🔁 Recurring Expenses for '{user_id}':\n\n"
    for exp_id, amount, cat, desc, day in rows:
        result += f"• [#{exp_id}] ${amount:.2f} - {cat} - Every month on day {day}\n"
        if desc:
            result += f"  Description: {desc}\n"

    result += f"\n🔢 Total Monthly Recurring: ${total:.2f}"
    return result


@mcp.tool()
def delete_recurring_expense(user_id: str, expense_id: int) -> str:
    """Delete a recurring expense by ID"""
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM recurring_expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
    conn.commit()

    if cursor.rowcount > 0:
        conn.close()
        logger.info(f"User '{user_id}' deleted recurring expense #{expense_id}")
        return f"✅ Recurring expense #{expense_id} deleted"
    else:
        conn.close()
        return f"❌ Recurring expense #{expense_id} not found or doesn't belong to user '{user_id}'"


# --------------------- RUN SERVER ---------------------

if __name__ == "__main__":
    mcp.run(
        transport='http',
        host="0.0.0.0",
        port=8000
    )
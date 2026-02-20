import logging
import os
import requests
from datetime import datetime
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from fastmcp import FastMCP

# --------------------- LOGGING SETUP ---------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# --------------------- INIT ---------------------

mcp = FastMCP("ExpenseTracker")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("SUPABASE_URL and SUPABASE_KEY environment variables must be set")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}


# --------------------- TABLE INIT ---------------------

def init_tables():
    """Create tables if they do not exist using Supabase SQL endpoint"""
    sql = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS expenses (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        amount FLOAT NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        date TEXT NOT NULL,
        timestamp FLOAT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS budgets (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        month INTEGER NOT NULL,
        year INTEGER NOT NULL,
        amount FLOAT NOT NULL,
        UNIQUE(user_id, month, year)
    );

    CREATE TABLE IF NOT EXISTS recurring_expenses (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        amount FLOAT NOT NULL,
        category TEXT NOT NULL,
        description TEXT,
        day_of_month INTEGER NOT NULL
    );
    """
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/exec_sql",
        headers=HEADERS,
        json={"query": sql}
    )

    # Fallback: use pg endpoint directly
    if not res.ok:
        res = requests.post(
            f"{SUPABASE_URL}/pg/query",
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"query": sql}
        )

    if res.ok:
        logger.info("Tables initialized successfully")
    else:
        logger.warning(f"Table init response: {res.status_code} - {res.text}")
        logger.warning("If tables do not exist, please create them manually in Supabase SQL Editor")


init_tables()


# --------------------- SUPABASE HELPERS ---------------------

def sb_get(table: str, params: dict = {}) -> list:
    res = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params)
    return res.json() if res.ok else []

def sb_post(table: str, data: dict) -> dict:
    res = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, json=data)
    result = res.json()
    return result[0] if isinstance(result, list) and result else result

def sb_patch(table: str, params: dict, data: dict) -> dict:
    res = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=HEADERS, params=params, json=data)
    result = res.json()
    return result[0] if isinstance(result, list) and result else result

def sb_delete(table: str, params: dict) -> int:
    h = {**HEADERS, "Prefer": "count=exact"}
    res = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, params=params)
    count = res.headers.get("content-range", "0")
    return int(count.split("/")[-1]) if "/" in count else (1 if res.ok else 0)

def sb_upsert(table: str, data: dict, on_conflict: str) -> dict:
    h = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    res = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}", headers=h, json=data)
    result = res.json()
    return result[0] if isinstance(result, list) and result else result

def auth(username: str, password: str):
    """Authenticate user, return user dict or None"""
    rows = sb_get("users", {"username": f"eq.{username.strip().lower()}", "password": f"eq.{password}"})
    return rows[0] if rows else None


# --------------------- INPUT VALIDATION ---------------------

def validate_amount(amount: float) -> Optional[str]:
    if amount <= 0:
        return "Amount must be greater than zero"
    return None

def validate_category(category: str) -> Optional[str]:
    if not category.strip():
        return "Category cannot be empty"
    return None

def validate_day_of_month(day: int) -> Optional[str]:
    if not (1 <= day <= 31):
        return "day_of_month must be between 1 and 31"
    return None


# --------------------- AUTH TOOLS ---------------------

@mcp.tool()
def register_user(name: str, username: str, password: str) -> str:
    """Register a new user with name, username and password"""
    if not name.strip():
        return "Name cannot be empty"
    if not username.strip():
        return "Username cannot be empty"
    if len(password) < 6:
        return "Password must be at least 6 characters"

    existing = sb_get("users", {"username": f"eq.{username.strip().lower()}"})
    if existing:
        return f"Username '{username}' is already taken"

    result = sb_post("users", {
        "name": name.strip(),
        "username": username.strip().lower(),
        "password": password
    })

    if "id" in result:
        logger.info(f"New user registered: '{username}'")
        return f"Welcome, {name}! Your account has been created. You can now login with username '{username.strip().lower()}'"
    return f"Registration failed: {result}"


@mcp.tool()
def login_user(username: str, password: str) -> str:
    """Login with username and password"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"
    logger.info(f"User '{username}' logged in")
    return (
        f"Login successful!\n"
        f"Welcome back, {user['name']}!\n"
        f"User ID: {user['id']}\n"
        f"Username: {user['username']}"
    )


@mcp.tool()
def change_password(username: str, old_password: str, new_password: str) -> str:
    """Change password for a user"""
    user = auth(username, old_password)
    if not user:
        return "Invalid username or old password"
    if len(new_password) < 6:
        return "New password must be at least 6 characters"
    sb_patch("users", {"username": f"eq.{username.strip().lower()}"}, {"password": new_password})
    return "Password changed successfully"


# --------------------- EXPENSE TOOLS ---------------------

@mcp.tool()
def add_expense(username: str, password: str, amount: float, category: str, description: str = "") -> str:
    """Add a new expense (requires login credentials)"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"
    if err := validate_amount(amount): return err
    if err := validate_category(category): return err

    now = datetime.now()
    sb_post("expenses", {
        "user_id": user["id"],
        "amount": amount,
        "category": category.strip(),
        "description": description,
        "date": now.isoformat(),
        "timestamp": now.timestamp()
    })

    logger.info(f"User '{username}' added expense ${amount:.2f} in '{category}'")
    return f"Expense added: ${amount:.2f} for {category}"


@mcp.tool()
def get_expenses(username: str, password: str, category: str = None, limit: int = 10) -> str:
    """Get recent expenses (requires login credentials)"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    params = {"user_id": f"eq.{user['id']}", "order": "timestamp.desc", "limit": str(limit)}
    if category:
        params["category"] = f"ilike.{category}"

    rows = sb_get("expenses", params)
    if not rows:
        return "No expenses found"

    result = f"Recent Expenses for '{user['name']}' ({len(rows)} shown):\n\n"
    for row in rows:
        date = datetime.fromisoformat(row["date"]).strftime("%Y-%m-%d %H:%M")
        result += f"- [#{row['id']}] ${row['amount']:.2f} - {row['category']} - {date}\n"
        if row.get("description"):
            result += f"  Description: {row['description']}\n"
    return result


@mcp.tool()
def get_total_by_category(username: str, password: str) -> str:
    """Get total expenses grouped by category"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    rows = sb_get("expenses", {"user_id": f"eq.{user['id']}"})
    if not rows:
        return "No expenses recorded yet"

    category_totals = {}
    for row in rows:
        category_totals[row["category"]] = category_totals.get(row["category"], 0) + row["amount"]

    result = f"Total Expenses by Category for '{user['name']}':\n\n"
    grand_total = 0
    for cat, amt in sorted(category_totals.items(), key=lambda x: x[1], reverse=True):
        result += f"- {cat}: ${amt:.2f}\n"
        grand_total += amt
    result += f"\nGrand Total: ${grand_total:.2f}"
    return result


@mcp.tool()
def delete_expense(username: str, password: str, expense_id: int) -> str:
    """Delete an expense by ID"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    count = sb_delete("expenses", {"id": f"eq.{expense_id}", "user_id": f"eq.{user['id']}"})
    if count > 0:
        return f"Expense #{expense_id} deleted successfully"
    return f"Expense #{expense_id} not found or does not belong to you"


@mcp.tool()
def update_expense(username: str, password: str, expense_id: int, amount: float = None, category: str = None, description: str = None) -> str:
    """Update an existing expense"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    if amount is not None:
        if err := validate_amount(amount): return err
    if category is not None:
        if err := validate_category(category): return err

    existing = sb_get("expenses", {"id": f"eq.{expense_id}", "user_id": f"eq.{user['id']}"})
    if not existing:
        return f"Expense #{expense_id} not found or does not belong to you"

    updates = {}
    if amount is not None: updates["amount"] = amount
    if category is not None: updates["category"] = category.strip()
    if description is not None: updates["description"] = description

    if updates:
        sb_patch("expenses", {"id": f"eq.{expense_id}"}, updates)
    return f"Expense #{expense_id} updated successfully"


@mcp.tool()
def get_monthly_summary(username: str, password: str, month: int = None, year: int = None) -> str:
    """Get expense summary for a specific month"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    now = datetime.now()
    month = month or now.month
    year = year or now.year
    prefix = f"{year}-{str(month).zfill(2)}"

    rows = sb_get("expenses", {"user_id": f"eq.{user['id']}", "date": f"like.{prefix}%"})
    if not rows:
        return f"No expenses found for {month}/{year}"

    total = sum(r["amount"] for r in rows)
    category_totals = {}
    for row in rows:
        category_totals[row["category"]] = category_totals.get(row["category"], 0) + row["amount"]

    result = f"Summary for '{user['name']}' - {month}/{year}:\n\n"
    result += f"Total Expenses: ${total:.2f}\nNumber of Transactions: {len(rows)}\n\nBy Category:\n"
    for cat, amt in sorted(category_totals.items(), key=lambda x: x[1], reverse=True):
        result += f"- {cat}: ${amt:.2f} ({(amt/total)*100:.1f}%)\n"
    return result


# --------------------- BUDGET TOOLS ---------------------

@mcp.tool()
def set_budget(username: str, password: str, amount: float, month: int = None, year: int = None) -> str:
    """Set a monthly budget"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    if err := validate_amount(amount): return err
    now = datetime.now()
    month = month or now.month
    year = year or now.year

    sb_upsert("budgets", {"user_id": user["id"], "month": month, "year": year, "amount": amount}, "user_id,month,year")
    return f"Budget set to ${amount:.2f} for {month}/{year}"


@mcp.tool()
def check_budget_status(username: str, password: str, month: int = None, year: int = None) -> str:
    """Check budget status"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    now = datetime.now()
    month = month or now.month
    year = year or now.year

    budget_rows = sb_get("budgets", {"user_id": f"eq.{user['id']}", "month": f"eq.{month}", "year": f"eq.{year}"})
    if not budget_rows:
        return f"No budget set for {month}/{year}. Use set_budget to create one."

    budget = budget_rows[0]["amount"]
    prefix = f"{year}-{str(month).zfill(2)}"
    expense_rows = sb_get("expenses", {"user_id": f"eq.{user['id']}", "date": f"like.{prefix}%"})
    spent = sum(r["amount"] for r in expense_rows)

    remaining = budget - spent
    pct_used = (spent / budget) * 100

    result = f"Budget Status for '{user['name']}' - {month}/{year}:\n\n"
    result += f"Budget:    ${budget:.2f}\nSpent:     ${spent:.2f} ({pct_used:.1f}%)\nRemaining: ${remaining:.2f}\n"

    if spent > budget:
        result += "\nWARNING: You have EXCEEDED your budget!"
    elif pct_used >= 80:
        result += "\nWARNING: You have used over 80% of your budget!"
    else:
        result += "\nYou are within your budget."
    return result


# --------------------- SPENDING TREND ---------------------

@mcp.tool()
def get_spending_trend(username: str, password: str) -> str:
    """Get spending totals for the last 6 months"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    rows = sb_get("expenses", {"user_id": f"eq.{user['id']}", "order": "date.desc"})
    if not rows:
        return "No spending data found"

    monthly = {}
    for row in rows:
        key = row["date"][:7]
        monthly[key] = monthly.get(key, 0) + row["amount"]

    sorted_months = sorted(monthly.keys(), reverse=True)[:6]
    sorted_months = list(reversed(sorted_months))

    max_total = max(monthly[m] for m in sorted_months)
    result = f"Spending Trend for '{user['name']}' (Last 6 Months):\n\n"
    for m in sorted_months:
        bar = "#" * int((monthly[m] / max_total) * 20)
        result += f"{m}  {bar}  ${monthly[m]:.2f}\n"
    return result


# --------------------- RECURRING EXPENSES ---------------------

@mcp.tool()
def add_recurring_expense(username: str, password: str, amount: float, category: str, day_of_month: int, description: str = "") -> str:
    """Register a recurring monthly expense"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    if err := validate_amount(amount): return err
    if err := validate_category(category): return err
    if err := validate_day_of_month(day_of_month): return err

    sb_post("recurring_expenses", {
        "user_id": user["id"], "amount": amount,
        "category": category.strip(), "description": description,
        "day_of_month": day_of_month
    })
    return f"Recurring expense added: ${amount:.2f} for {category} on day {day_of_month} of each month"


@mcp.tool()
def get_recurring_expenses(username: str, password: str) -> str:
    """List all recurring expenses"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    rows = sb_get("recurring_expenses", {"user_id": f"eq.{user['id']}", "order": "day_of_month.asc"})
    if not rows:
        return "No recurring expenses found"

    total = sum(r["amount"] for r in rows)
    result = f"Recurring Expenses for '{user['name']}':\n\n"
    for row in rows:
        result += f"- [#{row['id']}] ${row['amount']:.2f} - {row['category']} - Every month on day {row['day_of_month']}\n"
        if row.get("description"):
            result += f"  Description: {row['description']}\n"
    result += f"\nTotal Monthly Recurring: ${total:.2f}"
    return result


@mcp.tool()
def delete_recurring_expense(username: str, password: str, expense_id: int) -> str:
    """Delete a recurring expense by ID"""
    user = auth(username, password)
    if not user:
        return "Invalid username or password"

    count = sb_delete("recurring_expenses", {"id": f"eq.{expense_id}", "user_id": f"eq.{user['id']}"})
    if count > 0:
        return f"Recurring expense #{expense_id} deleted"
    return f"Recurring expense #{expense_id} not found or does not belong to you"


# --------------------- RUN SERVER ---------------------

if __name__ == "__main__":
    mcp.run(transport='http', host="0.0.0.0", port=8000)
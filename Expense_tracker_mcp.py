import logging
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastmcp import FastMCP
from pymongo import MongoClient

# --------------------- LOGGING SETUP ---------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# --------------------- INIT ---------------------

mcp = FastMCP("ExpenseTracker")

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise ValueError("MONGO_URI environment variable is not set")

client = MongoClient(
    MONGO_URI,
    tls=True,
    tlsAllowInvalidCertificates=True
)
db = client["expense_tracker"]

expenses_col = db["expenses"]
budgets_col = db["budgets"]
recurring_col = db["recurring_expenses"]

logger.info("Connected to MongoDB Atlas successfully")


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

    now = datetime.now()
    expenses_col.insert_one({
        "user_id": user_id,
        "amount": amount,
        "category": category.strip(),
        "description": description,
        "date": now.isoformat(),
        "timestamp": now.timestamp()
    })

    logger.info(f"User '{user_id}' added expense ${amount:.2f} in category '{category}'")
    return f"✅ Expense added: ${amount:.2f} for {category}"


# ---- 2. Get Expenses ----

@mcp.tool()
def get_expenses(user_id: str, category: str = None, limit: int = 10) -> str:
    """Get recent expenses for a user, optionally filtered by category"""
    query = {"user_id": user_id}
    if category:
        query["category"] = {"$regex": f"^{category}$", "$options": "i"}

    rows = list(expenses_col.find(query).sort("timestamp", -1).limit(limit))

    if not rows:
        return "No expenses found"

    result = f"📊 Recent Expenses for '{user_id}' ({len(rows)} shown):\n\n"
    for row in rows:
        exp_id = str(row["_id"])
        amount = row["amount"]
        cat = row["category"]
        desc = row.get("description", "")
        date = datetime.fromisoformat(row["date"]).strftime("%Y-%m-%d %H:%M")
        result += f"• [#{exp_id[-6:]}] ${amount:.2f} - {cat} - {date}\n"
        if desc:
            result += f"  Description: {desc}\n"

    logger.info(f"User '{user_id}' fetched expenses (category={category}, limit={limit})")
    return result


# ---- 3. Get Total by Category ----

@mcp.tool()
def get_total_by_category(user_id: str) -> str:
    """Get total expenses grouped by category for a user"""
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}},
        {"$sort": {"total": -1}}
    ]
    rows = list(expenses_col.aggregate(pipeline))

    if not rows:
        return "No expenses recorded yet"

    result = f"💰 Total Expenses by Category for '{user_id}':\n\n"
    grand_total = 0
    for row in rows:
        result += f"• {row['_id']}: ${row['total']:.2f}\n"
        grand_total += row["total"]

    result += f"\n🔢 Grand Total: ${grand_total:.2f}"
    logger.info(f"User '{user_id}' fetched category totals")
    return result


# ---- 4. Delete Expense ----

@mcp.tool()
def delete_expense(user_id: str, expense_id: str) -> str:
    """Delete an expense by ID (only if it belongs to the user)"""
    from bson import ObjectId
    try:
        result = expenses_col.delete_one({"_id": ObjectId(expense_id), "user_id": user_id})
    except Exception:
        return f"❌ Invalid expense ID format"

    if result.deleted_count > 0:
        logger.info(f"User '{user_id}' deleted expense #{expense_id}")
        return f"✅ Expense #{expense_id} deleted successfully"
    else:
        return f"❌ Expense #{expense_id} not found or doesn't belong to user '{user_id}'"


# ---- 5. Monthly Summary ----

@mcp.tool()
def get_monthly_summary(user_id: str, month: int = None, year: int = None) -> str:
    """Get expense summary for a specific month"""
    now = datetime.now()
    month = month or now.month
    year = year or now.year

    month_str = str(month).zfill(2)
    year_str = str(year)
    prefix = f"{year_str}-{month_str}"

    rows = list(expenses_col.find({
        "user_id": user_id,
        "date": {"$regex": f"^{prefix}"}
    }))

    if not rows:
        return f"No expenses found for {month}/{year}"

    total = sum(r["amount"] for r in rows)
    category_totals = {}
    for row in rows:
        cat = row["category"]
        category_totals[cat] = category_totals.get(cat, 0) + row["amount"]

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
def update_expense(user_id: str, expense_id: str, amount: float = None, category: str = None, description: str = None) -> str:
    """Update an existing expense's amount, category, or description"""
    from bson import ObjectId

    if amount is not None:
        if err := validate_amount(amount):
            return err
    if category is not None:
        if err := validate_category(category):
            return err

    try:
        obj_id = ObjectId(expense_id)
    except Exception:
        return "❌ Invalid expense ID format"

    if not expenses_col.find_one({"_id": obj_id, "user_id": user_id}):
        return f"❌ Expense #{expense_id} not found or doesn't belong to user '{user_id}'"

    updates = {}
    if amount is not None:
        updates["amount"] = amount
    if category is not None:
        updates["category"] = category.strip()
    if description is not None:
        updates["description"] = description

    if updates:
        expenses_col.update_one({"_id": obj_id}, {"$set": updates})

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

    budgets_col.update_one(
        {"user_id": user_id, "month": month, "year": year},
        {"$set": {"amount": amount}},
        upsert=True
    )

    logger.info(f"User '{user_id}' set budget ${amount:.2f} for {month}/{year}")
    return f"✅ Budget set to ${amount:.2f} for {month}/{year}"


@mcp.tool()
def check_budget_status(user_id: str, month: int = None, year: int = None) -> str:
    """Check budget status: spent, remaining, and warning if over 80%"""
    now = datetime.now()
    month = month or now.month
    year = year or now.year

    budget_doc = budgets_col.find_one({"user_id": user_id, "month": month, "year": year})
    if not budget_doc:
        return f"❌ No budget set for '{user_id}' in {month}/{year}. Use set_budget to create one."

    budget = budget_doc["amount"]

    month_str = str(month).zfill(2)
    year_str = str(year)
    prefix = f"{year_str}-{month_str}"

    pipeline = [
        {"$match": {"user_id": user_id, "date": {"$regex": f"^{prefix}"}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]
    result_agg = list(expenses_col.aggregate(pipeline))
    spent = result_agg[0]["total"] if result_agg else 0.0

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
    pipeline = [
        {"$match": {"user_id": user_id}},
        {"$group": {
            "_id": {"$substr": ["$date", 0, 7]},
            "total": {"$sum": "$amount"}
        }},
        {"$sort": {"_id": -1}},
        {"$limit": 6}
    ]
    rows = list(expenses_col.aggregate(pipeline))

    if not rows:
        return f"No spending data found for '{user_id}'"

    rows = list(reversed(rows))
    max_total = max(r["total"] for r in rows)

    result = f"📈 Spending Trend for '{user_id}' (Last 6 Months):\n\n"
    for row in rows:
        bar_len = int((row["total"] / max_total) * 20)
        bar = "█" * bar_len
        result += f"{row['_id']}  {bar}  ${row['total']:.2f}\n"

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

    recurring_col.insert_one({
        "user_id": user_id,
        "amount": amount,
        "category": category.strip(),
        "description": description,
        "day_of_month": day_of_month
    })

    logger.info(f"User '{user_id}' added recurring expense ${amount:.2f} in '{category}' on day {day_of_month}")
    return f"✅ Recurring expense added: ${amount:.2f} for {category} on day {day_of_month} of each month"


@mcp.tool()
def get_recurring_expenses(user_id: str) -> str:
    """List all recurring expenses for a user"""
    rows = list(recurring_col.find({"user_id": user_id}).sort("day_of_month", 1))

    if not rows:
        return f"No recurring expenses found for '{user_id}'"

    total = sum(r["amount"] for r in rows)
    result = f"🔁 Recurring Expenses for '{user_id}':\n\n"
    for row in rows:
        exp_id = str(row["_id"])[-6:]
        result += f"• [#{exp_id}] ${row['amount']:.2f} - {row['category']} - Every month on day {row['day_of_month']}\n"
        if row.get("description"):
            result += f"  Description: {row['description']}\n"

    result += f"\n🔢 Total Monthly Recurring: ${total:.2f}"
    return result


@mcp.tool()
def delete_recurring_expense(user_id: str, expense_id: str) -> str:
    """Delete a recurring expense by ID"""
    from bson import ObjectId
    try:
        result = recurring_col.delete_one({"_id": ObjectId(expense_id), "user_id": user_id})
    except Exception:
        return "❌ Invalid expense ID format"

    if result.deleted_count > 0:
        logger.info(f"User '{user_id}' deleted recurring expense #{expense_id}")
        return f"✅ Recurring expense #{expense_id} deleted"
    else:
        return f"❌ Recurring expense #{expense_id} not found or doesn't belong to user '{user_id}'"


# --------------------- RUN SERVER ---------------------

if __name__ == "__main__":
    mcp.run(
        transport='http',
        host="0.0.0.0",
        port=8000
    )
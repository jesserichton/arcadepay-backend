"""
ArcadePay Backend Server
===========================
Deploy to Railway: push this folder to GitHub, connect to Railway, done.
"""

import os
import hashlib
import uuid
from datetime import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# Railway provides DATABASE_URL automatically when you add a Postgres plugin
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///arcadepay.db"
).replace("postgres://", "postgresql://")  # Fix Railway's legacy postgres:// URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

MACHINE_SECRET = os.environ.get("MACHINE_SECRET", "change-this-in-railway-variables")


# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id            = db.Column(db.String(64), primary_key=True)
    display_name  = db.Column(db.String(128), default="Player")
    token_balance = db.Column(db.Integer, default=0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    id           = db.Column(db.String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id      = db.Column(db.String(64), db.ForeignKey("user.id"), nullable=False)
    machine_id   = db.Column(db.String(64), nullable=True)
    machine_name = db.Column(db.String(128), nullable=True)
    tokens       = db.Column(db.Integer, nullable=False)
    txn_type     = db.Column(db.String(20), nullable=False)  # spend | purchase | refund
    status       = db.Column(db.String(20), default="pending")  # pending | verified | failed
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

class Machine(db.Model):
    id                = db.Column(db.String(64), primary_key=True)
    name              = db.Column(db.String(128))
    tokens_per_credit = db.Column(db.Integer, default=1)
    location          = db.Column(db.String(256), nullable=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_or_create_user(user_id: str) -> User:
    user = db.session.get(User, user_id)
    if not user:
        user = User(id=user_id, token_balance=0)
        db.session.add(user)
        db.session.commit()
    return user

def verify_machine_signature(machine_id, user_id, tokens, txn_id, signature) -> bool:
    sig_data = f"{machine_id}:{user_id}:{tokens}:{txn_id}"
    expected = hashlib.sha256(f"{MACHINE_SECRET}{sig_data}".encode()).hexdigest()
    return expected == signature


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


# ── Wallet ────────────────────────────────────────────────────────────────────

@app.route("/api/wallet/<user_id>", methods=["GET"])
def get_wallet(user_id):
    user = get_or_create_user(user_id)
    return jsonify({"user_id": user.id, "token_balance": user.token_balance})


# ── Free tokens for testing ───────────────────────────────────────────────────
# Remove this endpoint before going live!

@app.route("/api/debug/give-tokens", methods=["POST"])
def give_tokens():
    data    = request.get_json()
    user_id = data.get("user_id")
    amount  = int(data.get("amount", 10))

    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400

    user = get_or_create_user(user_id)
    user.token_balance += amount
    db.session.commit()
    return jsonify({"success": True, "new_balance": user.token_balance})


# ── Pay (called by app) ───────────────────────────────────────────────────────

@app.route("/api/pay", methods=["POST"])
def app_pay():
    data       = request.get_json()
    user_id    = data.get("user_id")
    machine_id = data.get("machine_id")
    tokens     = int(data.get("tokens", 1))

    if not user_id or not machine_id:
        return jsonify({"error": "Missing user_id or machine_id"}), 400

    user = get_or_create_user(user_id)

    if user.token_balance < tokens:
        return jsonify({"error": f"Insufficient tokens. Have {user.token_balance}, need {tokens}."}), 402

    user.token_balance -= tokens

    # Look up machine name
    machine = db.session.get(Machine, machine_id)
    machine_name = machine.name if machine else machine_id

    txn = Transaction(
        user_id=user_id,
        machine_id=machine_id,
        machine_name=machine_name,
        tokens=-tokens,
        txn_type="spend",
        status="pending",
    )
    db.session.add(txn)
    db.session.commit()

    return jsonify({
        "success": True,
        "transaction_id": txn.id,
        "new_balance": user.token_balance,
        "expires_in": 60,
    })


# ── Purchase tokens (called by app) ──────────────────────────────────────────

@app.route("/api/purchase", methods=["POST"])
def purchase_tokens():
    data          = request.get_json()
    user_id       = data.get("user_id")
    tokens        = int(data.get("tokens", 0))
    payment_token = data.get("payment_token", "")

    if not user_id or tokens <= 0:
        return jsonify({"error": "Invalid purchase"}), 400

    # Accept "demo" for testing — replace with Stripe verification for production
    if payment_token != "demo":
        # TODO: verify Stripe payment intent here before going live
        pass

    user = get_or_create_user(user_id)
    user.token_balance += tokens

    txn = Transaction(
        user_id=user_id,
        tokens=tokens,
        txn_type="purchase",
        status="verified",
        machine_name="Token Purchase",
    )
    db.session.add(txn)
    db.session.commit()

    return jsonify({"success": True, "new_balance": user.token_balance})


# ── Verify (called by arcade machine Pi) ──────────────────────────────────────

@app.route("/api/machine/verify", methods=["POST"])
def machine_verify():
    data       = request.get_json()
    machine_id = data.get("machine_id")
    user_id    = data.get("user_id")
    tokens     = int(data.get("tokens", 1))
    txn_id     = data.get("transaction_id")
    signature  = request.headers.get("X-Machine-Signature", "")

    if not verify_machine_signature(machine_id, user_id, tokens, txn_id, signature):
        return jsonify({"success": False, "message": "Invalid signature"}), 403

    txn = db.session.get(Transaction, txn_id)
    if not txn:
        return jsonify({"success": False, "message": "Transaction not found"}), 404
    if txn.status != "pending":
        return jsonify({"success": False, "message": "Transaction already processed"}), 409

    age = (datetime.utcnow() - txn.created_at).total_seconds()
    if age > 60:
        txn.status = "failed"
        user = get_or_create_user(txn.user_id)
        user.token_balance += abs(txn.tokens)
        db.session.commit()
        return jsonify({"success": False, "message": "Transaction expired — tokens refunded"}), 410

    txn.status = "verified"
    txn.machine_id = machine_id
    db.session.commit()

    machine = db.session.get(Machine, machine_id)
    credits = max(1, tokens // (machine.tokens_per_credit if machine else 1))

    return jsonify({"success": True, "credits": credits, "transaction_id": txn_id})


# ── History ───────────────────────────────────────────────────────────────────

@app.route("/api/history/<user_id>", methods=["DELETE"])
def clear_history(user_id):
    Transaction.query.filter_by(user_id=user_id).delete()
    db.session.commit()
    return jsonify({"success": True})


@app.route("/api/history/<user_id>", methods=["GET"])
def get_history(user_id):
    txns = (Transaction.query
            .filter_by(user_id=user_id)
            .order_by(Transaction.created_at.desc())
            .limit(50).all())

    return jsonify({"transactions": [{
        "id": t.id,
        "type": t.txn_type,
        "tokens": t.tokens,
        "machine": t.machine_id or "Top Up",
        "machine_name": t.machine_name or t.machine_id or "Token Purchase",
        "status": t.status,
        "time": t.created_at.isoformat(),
    } for t in txns]})


# ── Machine registration ─────────────────────────────────────────────────────

@app.route("/api/machine/register", methods=["POST"])
def register_machine():
    data = request.get_json()
    machine_id = data.get("machine_id")
    name       = data.get("name", machine_id)
    tpc        = int(data.get("tokens_per_credit", 1))

    if not machine_id:
        return jsonify({"error": "Missing machine_id"}), 400

    machine = db.session.get(Machine, machine_id)
    if machine:
        machine.name = name
        machine.tokens_per_credit = tpc
    else:
        machine = Machine(id=machine_id, name=name, tokens_per_credit=tpc)
        db.session.add(machine)
    db.session.commit()
    return jsonify({"success": True, "machine_id": machine_id, "name": name})


# ── Pending transactions (polled by machine controller) ───────────────────────

@app.route("/api/machine/pending/<machine_id>", methods=["GET"])
def get_pending(machine_id):
    txns = (Transaction.query
            .filter_by(machine_id=machine_id, status="pending")
            .order_by(Transaction.created_at.asc())
            .all())

    result = []
    for t in txns:
        age = (datetime.utcnow() - t.created_at).total_seconds()
        if age > 60:
            # Expired — refund tokens
            t.status = "failed"
            user = get_or_create_user(t.user_id)
            user.token_balance += abs(t.tokens)
            db.session.commit()
        else:
            # Mark as verified so it won't be returned again
            t.status = "verified"
            db.session.commit()
            result.append({"id": t.id, "tokens": abs(t.tokens), "user_id": t.user_id})

    return jsonify({"transactions": result})


# ── Startup ───────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    if not db.session.get(Machine, "M001"):
        db.session.add(Machine(id="M001", name="Double Dragon", tokens_per_credit=1))
        db.session.commit()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

import os
import io
import csv
import json
import secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for, flash,
                   send_file, session, Response)
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev')

MONGO_URI = os.environ.get('MONGO_URI')
if not MONGO_URI:
    raise RuntimeError('MONGO_URI environment variable is not set')

client = MongoClient(MONGO_URI)
db = client.get_database('nimora_pos')
items_col = db.items
invoices_col = db.invoices
settings_col = db.settings
suppliers_col = db.suppliers
returns_col = db.returns
customers_col = db.customers
users_col = db.users
expenses_col = db.expenses
purchases_col = db.purchases


# ================= HELPERS =================
def get_settings():
    s = settings_col.find_one({'_id': 'main'})
    if not s:
        s = {
            '_id': 'main',
            'business_name': os.environ.get('BUSINESS_NAME', 'Nimora Brand'),
            'phone': os.environ.get('BUSINESS_PHONE', '0784280074'),
            'logo_url': os.environ.get('LOGO_URL', ''),
            'address': '',
            'tax_rate': 0,
            'currency': 'Rs.',
            'footer_note': 'Thank you for your business!',
            'low_stock_threshold': 5,
            'whatsapp_country': '94'
        }
        settings_col.insert_one(s)
    return s


def ensure_admin():
    if users_col.count_documents({}) == 0:
        users_col.insert_one({
            'username': 'admin',
            'password': generate_password_hash('admin123'),
            'role': 'admin',
            'created_at': datetime.utcnow()
        })


ensure_admin()


def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrap


def admin_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Admin access required')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return wrap


# ================= AUTH =================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = users_col.find_one({'username': request.form.get('username', '').strip()})
        if u and check_password_hash(u['password'], request.form.get('password', '')):
            session['user'] = u['username']
            session['role'] = u.get('role', 'staff')
            flash(f"Welcome, {u['username']}")
            return redirect(url_for('dashboard'))
        flash('Invalid username or password')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
def users():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        role = request.form.get('role', 'staff')
        if username and password:
            if users_col.find_one({'username': username}):
                flash('Username already exists')
            else:
                users_col.insert_one({
                    'username': username,
                    'password': generate_password_hash(password),
                    'role': role,
                    'created_at': datetime.utcnow()
                })
                flash('User created')
        return redirect(url_for('users'))
    return render_template('users.html', users=list(users_col.find()))


@app.route('/user/delete/<uid>')
@login_required
@admin_required
def delete_user(uid):
    if str(uid) == 'admin':
        flash('Cannot delete admin')
        return redirect(url_for('users'))
    users_col.delete_one({'_id': ObjectId(uid)})
    flash('User deleted')
    return redirect(url_for('users'))


# ================= DASHBOARD =================
@app.route('/')
@login_required
def dashboard():
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_invoices = list(invoices_col.find({'created_at': {'$gte': today}}))
    today_sales = sum(i['total'] for i in today_invoices)
    settings = get_settings()
    low_stock = list(items_col.find({'stock': {'$lte': settings['low_stock_threshold']}}).limit(5))
    recent = list(invoices_col.find().sort('created_at', -1).limit(5))
    week_ago = today - timedelta(days=7)
    week_sales = sum(i['total'] for i in invoices_col.find({'created_at': {'$gte': week_ago}}))

    # profit
    profit_today = 0
    for inv in today_invoices:
        for it in inv['items']:
            item = items_col.find_one({'_id': ObjectId(it['item_id'])}) if it.get('item_id') else None
            cost = item.get('cost', 0) if item else 0
            profit_today += (it['price'] - cost)

    return render_template('dashboard.html',
                           today_sales=today_sales,
                           today_count=len(today_invoices),
                           total_items=items_col.count_documents({}),
                           low_stock=low_stock,
                           recent=recent,
                           week_sales=week_sales,
                           profit_today=profit_today)


# ================= POS =================
@app.route('/pos')
@login_required
def index():
    q = request.args.get('q', '').strip()
    if q:
        items = list(items_col.find({'$or': [
            {'name': {'$regex': q, '$options': 'i'}},
            {'barcode': {'$regex': q, '$options': 'i'}}
        ]}))
    else:
        items = list(items_col.find())
    settings = get_settings()
    customers = list(customers_col.find().sort('name', 1))
    return render_template('index.html', items=items, q=q, settings=settings, customers=customers)


@app.route('/invoice/create', methods=['POST'])
@login_required
def create_invoice():
    selected = request.form.getlist('item_ids')
    if not selected:
        flash('Please select at least one item')
        return redirect(url_for('index'))

    ids = [ObjectId(i) for i in selected]
    items = list(items_col.find({'_id': {'$in': ids}}))
    if not items:
        flash('Selected items could not be found')
        return redirect(url_for('index'))

    subtotal = sum(i['price'] for i in items)
    discount = float(request.form.get('discount', 0) or 0)
    tax_rate = float(request.form.get('tax_rate', 0) or 0)
    customer = request.form.get('customer', '').strip()
    payment = request.form.get('payment', 'Cash')
    notes = request.form.get('notes', '').strip()

    tax_amount = (subtotal - discount) * tax_rate / 100
    total = subtotal - discount + tax_amount
    invoice_no = f"NIM-{secrets.token_hex(3).upper()}"

    profit = sum((i['price'] - i.get('cost', 0)) for i in items) - discount

    invoice_doc = {
        'invoice_no': invoice_no,
        'created_at': datetime.utcnow(),
        'customer': customer,
        'payment': payment,
        'notes': notes,
        'subtotal': subtotal,
        'discount': discount,
        'tax_rate': tax_rate,
        'tax_amount': tax_amount,
        'total': total,
        'profit': profit,
        'user': session.get('user', 'unknown'),
        'items': [{'item_id': str(i['_id']), 'name': i['name'], 'price': i['price'], 'cost': i.get('cost', 0)} for i in items]
    }
    result = invoices_col.insert_one(invoice_doc)

    for i in items:
        items_col.update_one({'_id': i['_id']}, {'$inc': {'stock': -1}})

    return redirect(url_for('view_invoice', inv_id=str(result.inserted_id)))


@app.route('/invoice/<inv_id>')
@login_required
def view_invoice(inv_id):
    inv = invoices_col.find_one({'_id': ObjectId(inv_id)})
    if not inv:
        flash('Invoice not found')
        return redirect(url_for('dashboard'))
    settings = get_settings()
    return render_template('invoice.html', inv=inv, items=inv['items'], settings=settings)


@app.route('/invoice/<inv_id>/pdf')
@login_required
def invoice_pdf(inv_id):
    inv = invoices_col.find_one({'_id': ObjectId(inv_id)})
    if not inv:
        flash('Invoice not found')
        return redirect(url_for('dashboard'))
    settings = get_settings()

    buf = io.BytesIO()
    p = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    p.setFont('Helvetica-Bold', 18)
    p.drawString(50, height - 50, settings['business_name'])
    p.setFont('Helvetica', 10)
    p.drawString(50, height - 70, f"Tel: {settings['phone']}")
    if settings.get('address'):
        p.drawString(50, height - 85, settings['address'])
    p.setFont('Helvetica-Bold', 12)
    p.drawString(50, height - 120, f"Invoice: {inv['invoice_no']}")
    p.drawString(50, height - 135, f"Date: {inv['created_at'].strftime('%Y-%m-%d %H:%M')}")
    if inv.get('customer'):
        p.drawString(50, height - 150, f"Customer: {inv['customer']}")
    p.drawString(50, height - 165, f"Payment: {inv['payment']}")
    y = height - 200
    p.setFont('Helvetica-Bold', 11)
    p.drawString(50, y, "Item")
    p.drawString(400, y, "Price")
    y -= 15
    p.setFont('Helvetica', 10)
    for it in inv['items']:
        p.drawString(50, y, it['name'][:60])
        p.drawString(400, y, f"Rs. {it['price']:.2f}")
        y -= 15
    y -= 10
    p.line(50, y, width - 50, y)
    y -= 20
    p.drawString(350, y, f"Subtotal: Rs. {inv['subtotal']:.2f}")
    y -= 15
    if inv.get('discount'):
        p.drawString(350, y, f"Discount: -Rs. {inv['discount']:.2f}")
        y -= 15
    if inv.get('tax_amount'):
        p.drawString(350, y, f"Tax ({inv['tax_rate']}%): Rs. {inv['tax_amount']:.2f}")
        y -= 15
    p.setFont('Helvetica-Bold', 12)
    p.drawString(350, y, f"Total: Rs. {inv['total']:.2f}")
    p.setFont('Helvetica-Oblique', 9)
    p.drawString(50, 50, settings.get('footer_note', ''))
    p.showPage()
    p.save()
    buf.seek(0)
    return send_file(buf, mimetype='application/pdf',
                     download_name=f"{inv['invoice_no']}.pdf", as_attachment=True)


@app.route('/invoice/<inv_id>/whatsapp')
@login_required
def invoice_whatsapp(inv_id):
    inv = invoices_col.find_one({'_id': ObjectId(inv_id)})
    if not inv:
        return redirect(url_for('dashboard'))
    s = get_settings()
    lines = [f"*{s['business_name']}*", f"Invoice: {inv['invoice_no']}",
             f"Date: {inv['created_at'].strftime('%Y-%m-%d')}"]
    for it in inv['items']:
        lines.append(f"- {it['name']}: Rs.{it['price']:.2f}")
    lines.append(f"*Total: Rs.{inv['total']:.2f}*")
    lines.append(s['footer_note'])
    msg = '\n'.join(lines)
    phone = inv.get('customer_phone', '')
    if phone.startswith('0'):
        phone = s['whatsapp_country'] + phone[1:]
    return redirect(f"https://wa.me/{phone}?text=" + msg.replace('\n', '%0A').replace(' ', '%20'))


@app.route('/invoices')
@login_required
def invoices_list():
    q = request.args.get('q', '').strip()
    query = {}
    if q:
        query = {'$or': [
            {'invoice_no': {'$regex': q, '$options': 'i'}},
            {'customer': {'$regex': q, '$options': 'i'}}
        ]}
    invs = list(invoices_col.find(query).sort('created_at', -1).limit(200))
    return render_template('invoices_list.html', invoices=invs, q=q)


# ================= ITEMS =================
@app.route('/items', methods=['GET', 'POST'])
@login_required
def manage_items():
    if request.method == 'POST':
        try:
            price = float(request.form.get('price', 0))
            stock = int(request.form.get('stock', 0))
            cost = float(request.form.get('cost', 0) or 0)
        except ValueError:
            flash('Numbers invalid')
            return redirect(url_for('manage_items'))
        name = request.form.get('name', '').strip()
        if name and price > 0:
            items_col.insert_one({
                'name': name, 'price': price, 'stock': stock, 'cost': cost,
                'barcode': request.form.get('barcode', '').strip(),
                'supplier': request.form.get('supplier', '').strip(),
                'created_at': datetime.utcnow()
            })
            flash('Item added')
        return redirect(url_for('manage_items'))
    items = list(items_col.find().sort('name', 1))
    suppliers = list(suppliers_col.find())
    return render_template('items.html', items=items, suppliers=suppliers)


@app.route('/item/delete/<item_id>')
@login_required
def delete_item(item_id):
    items_col.delete_one({'_id': ObjectId(item_id)})
    flash('Item deleted')
    return redirect(url_for('manage_items'))


@app.route('/item/edit/<item_id>', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    item = items_col.find_one({'_id': ObjectId(item_id)})
    if not item:
        return redirect(url_for('manage_items'))
    if request.method == 'POST':
        try:
            price = float(request.form.get('price', 0))
            stock = int(request.form.get('stock', 0))
            cost = float(request.form.get('cost', 0) or 0)
        except ValueError:
            flash('Numbers invalid')
            return redirect(url_for('edit_item', item_id=item_id))
        items_col.update_one({'_id': ObjectId(item_id)}, {'$set': {
            'name': request.form.get('name', '').strip(),
            'price': price, 'stock': stock, 'cost': cost,
            'barcode': request.form.get('barcode', '').strip(),
            'supplier': request.form.get('supplier', '').strip()
        }})
        flash('Item updated')
        return redirect(url_for('manage_items'))
    return render_template('items.html',
                           items=list(items_col.find().sort('name', 1)),
                           suppliers=list(suppliers_col.find()), edit_item=item)


# ================= CUSTOMERS =================
@app.route('/customers', methods=['GET', 'POST'])
@login_required
def customers():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if name:
            customers_col.insert_one({
                'name': name,
                'phone': request.form.get('phone', '').strip(),
                'email': request.form.get('email', '').strip(),
                'address': request.form.get('address', '').strip(),
                'created_at': datetime.utcnow()
            })
            flash('Customer added')
        return redirect(url_for('customers'))
    return render_template('customers.html', customers=list(customers_col.find().sort('name', 1)))


@app.route('/customer/delete/<cid>')
@login_required
def delete_customer(cid):
    customers_col.delete_one({'_id': ObjectId(cid)})
    flash('Customer deleted')
    return redirect(url_for('customers'))


# ================= SUPPLIERS =================
@app.route('/suppliers', methods=['GET', 'POST'])
@login_required
def suppliers():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if name:
            suppliers_col.insert_one({
                'name': name,
                'phone': request.form.get('phone', '').strip(),
                'email': request.form.get('email', '').strip(),
                'address': request.form.get('address', '').strip(),
                'created_at': datetime.utcnow()
            })
            flash('Supplier added')
        return redirect(url_for('suppliers'))
    return render_template('suppliers.html', suppliers=list(suppliers_col.find().sort('name', 1)))


@app.route('/supplier/delete/<sid>')
@login_required
def delete_supplier(sid):
    suppliers_col.delete_one({'_id': ObjectId(sid)})
    flash('Supplier deleted')
    return redirect(url_for('suppliers'))


# ================= PURCHASES =================
@app.route('/purchases', methods=['GET', 'POST'])
@login_required
def purchases():
    if request.method == 'POST':
        try:
            qty = int(request.form.get('quantity', 0))
            total = float(request.form.get('total', 0))
        except ValueError:
            flash('Invalid numbers')
            return redirect(url_for('purchases'))
        purchases_col.insert_one({
            'item': request.form.get('item', '').strip(),
            'supplier': request.form.get('supplier', '').strip(),
            'quantity': qty,
            'total': total,
            'created_at': datetime.utcnow()
        })
        # stock increase
        it = items_col.find_one({'name': request.form.get('item', '').strip()})
        if it:
            items_col.update_one({'_id': it['_id']}, {'$inc': {'stock': qty}})
        flash('Purchase recorded and stock updated')
        return redirect(url_for('purchases'))
    return render_template('purchases.html',
                           purchases=list(purchases_col.find().sort('created_at', -1)),
                           items=list(items_col.find().sort('name', 1)),
                           suppliers=list(suppliers_col.find()))


# ================= EXPENSES =================
@app.route('/expenses', methods=['GET', 'POST'])
@login_required
def expenses():
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
        except ValueError:
            flash('Invalid amount')
            return redirect(url_for('expenses'))
        expenses_col.insert_one({
            'title': request.form.get('title', '').strip(),
            'category': request.form.get('category', 'Other'),
            'amount': amount,
            'notes': request.form.get('notes', '').strip(),
            'created_at': datetime.utcnow()
        })
        flash('Expense added')
        return redirect(url_for('expenses'))
    exp = list(expenses_col.find().sort('created_at', -1))
    total = sum(e['amount'] for e in exp)
    return render_template('expenses.html', expenses=exp, total=total)


@app.route('/expense/delete/<eid>')
@login_required
def delete_expense(eid):
    expenses_col.delete_one({'_id': ObjectId(eid)})
    flash('Expense deleted')
    return redirect(url_for('expenses'))


# ================= RETURNS =================
@app.route('/returns', methods=['GET', 'POST'])
@login_required
def returns():
    if request.method == 'POST':
        inv_no = request.form.get('invoice_no', '').strip()
        inv = invoices_col.find_one({'invoice_no': inv_no})
        if not inv:
            flash('Invoice not found')
            return redirect(url_for('returns'))
        returns_col.insert_one({
            'invoice_no': inv_no,
            'created_at': datetime.utcnow(),
            'reason': request.form.get('reason', '').strip(),
            'amount': inv['total'],
            'items': inv['items']
        })
        for it in inv['items']:
            if it.get('item_id'):
                items_col.update_one({'_id': ObjectId(it['item_id'])}, {'$inc': {'stock': 1}})
        flash('Return recorded')
        return redirect(url_for('returns'))
    return render_template('returns.html', returns=list(returns_col.find().sort('created_at', -1)))


# ================= REPORTS =================
@app.route('/reports')
@login_required
def reports():
    start = request.args.get('start')
    end = request.args.get('end')
    query = {}
    if start or end:
        query['created_at'] = {}
        if start:
            query['created_at']['$gte'] = datetime.strptime(start, '%Y-%m-%d')
        if end:
            query['created_at']['$lte'] = datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
    invs = list(invoices_col.find(query).sort('created_at', -1))
    total = sum(i['total'] for i in invs)
    profit = sum(i.get('profit', 0) for i in invs)

    item_summary = {}
    for inv in invs:
        for it in inv['items']:
            k = it['name']
            if k not in item_summary:
                item_summary[k] = {'qty': 0, 'total': 0}
            item_summary[k]['qty'] += 1
            item_summary[k]['total'] += it['price']
    return render_template('reports.html', invoices=invs, total=total, profit=profit,
                           count=len(invs),
                           item_summary=sorted(item_summary.items(), key=lambda x: x[1]['total'], reverse=True),
                           start=start, end=end)


@app.route('/reports/export')
@login_required
def reports_export():
    start = request.args.get('start')
    end = request.args.get('end')
    query = {}
    if start or end:
        query['created_at'] = {}
        if start:
            query['created_at']['$gte'] = datetime.strptime(start, '%Y-%m-%d')
        if end:
            query['created_at']['$lte'] = datetime.strptime(end, '%Y-%m-%d').replace(hour=23, minute=59, second=59)
    invs = list(invoices_col.find(query))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Invoice', 'Date', 'Customer', 'Payment', 'Subtotal', 'Discount', 'Tax', 'Total', 'Profit'])
    for inv in invs:
        w.writerow([inv['invoice_no'], inv['created_at'].strftime('%Y-%m-%d %H:%M'),
                    inv.get('customer', ''), inv.get('payment', ''),
                    inv['subtotal'], inv['discount'], inv['tax_amount'], inv['total'], inv.get('profit', 0)])
    buf.seek(0)
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment;filename=report.csv'})


# ================= SETTINGS =================
@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    s = get_settings()
    if request.method == 'POST':
        try:
            tax_rate = float(request.form.get('tax_rate', 0) or 0)
            low_stock = int(request.form.get('low_stock_threshold', 5) or 5)
        except ValueError:
            flash('Invalid numbers')
            return redirect(url_for('settings'))
        settings_col.update_one({'_id': 'main'}, {'$set': {
            'business_name': request.form.get('business_name', s['business_name']),
            'phone': request.form.get('phone', s['phone']),
            'logo_url': request.form.get('logo_url', s['logo_url']),
            'address': request.form.get('address', ''),
            'tax_rate': tax_rate,
            'currency': request.form.get('currency', 'Rs.'),
            'footer_note': request.form.get('footer_note', ''),
            'low_stock_threshold': low_stock,
            'whatsapp_country': request.form.get('whatsapp_country', '94')
        }})
        flash('Settings saved')
        return redirect(url_for('settings'))
    return render_template('settings.html', settings=s)


# ================= BACKUP =================
@app.route('/backup')
@login_required
@admin_required
def backup():
    data = {
        'items': [],
        'invoices': [],
        'customers': [],
        'suppliers': [],
        'expenses': [],
        'purchases': [],
        'returns': [],
        'settings': get_settings()
    }
    for name, col in [('items', items_col), ('invoices', invoices_col),
                      ('customers', customers_col), ('suppliers', suppliers_col),
                      ('expenses', expenses_col), ('purchases', purchases_col),
                      ('returns', returns_col)]:
        for doc in col.find():
            doc['_id'] = str(doc['_id'])
            if 'created_at' in doc:
                doc['created_at'] = doc['created_at'].isoformat()
            data[name].append(doc)
    data['settings'].pop('_id', None)
    out = json.dumps(data, indent=2, default=str)
    return Response(out, mimetype='application/json',
                    headers={'Content-Disposition': 'attachment;filename=nimora_backup.json'})


if __name__ == '__main__':
    app.run(debug=True)
